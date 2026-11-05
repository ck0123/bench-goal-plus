#!/usr/bin/env python3
"""Build the ReposVul task catalog consumed by the bench adapter.

Groups the upstream ``ReposVul.jsonl`` records by (project, fixing commit),
reconstructs each task's ground-truth findings at the *parent* (vulnerable)
commit, and writes ``tasks/catalog.json``.

Ground-truth reconstruction, per patch file (``details[]`` entry):

1. primary: ``function_before`` entries with ``target == 1`` are the
   vulnerable functions; their text is located inside ``code_before`` to
   recover the 1-based inclusive line span in the vulnerable file, and the
   function name is parsed from the signature;
2. fallback: when no vulnerable function text is recoverable, the patch
   hunk headers (``@@ -a,b ...``) provide the changed old-side line span.

Usage:
    python3 build_catalog.py [--dataset PATH] [--output PATH]

The dataset path defaults to ``REPOSVUL_DATASET`` or the local deployment at
``/home/tangzhiyun/workspace/ReposVul/data/ReposVul.jsonl``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_DATASET = Path(
    "/home/tangzhiyun/workspace/ReposVul/data/ReposVul.jsonl"
)
GITHUB_PATH = re.compile(r"^[\w.\-]+/[\w.\-]+$")
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def _function_name(text: str) -> str | None:
    """Best-effort identifier extraction from a C/Java/Python signature."""
    head = text.split("{", 1)[0]
    match = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", head)
    if match:
        return match[-1]
    tokens = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)", head)
    return tokens[-1] if tokens else None


def _locate_span(function_text: str, code_before: str) -> tuple[int, int] | None:
    """Locate the function text in the vulnerable file, return 1-based span."""
    needle = function_text.strip()
    if not needle:
        return None
    for candidate in (needle, function_text):
        index = code_before.find(candidate)
        if index >= 0:
            start = code_before.count("\n", 0, index) + 1
            end = start + candidate.count("\n")
            return start, max(start, end)
    # tolerate whitespace drift: slide a whitespace-normalized line window
    def _norm(line: str) -> str:
        return " ".join(line.split())

    needle_lines = [_norm(x) for x in needle.splitlines() if x.strip()]
    code_lines = code_before.splitlines()
    if not needle_lines or len(needle_lines) > len(code_lines):
        return None
    first, width = needle_lines[0], len(needle_lines)
    tail = needle_lines[-1]
    for offset in range(len(code_lines) - width + 1):
        if _norm(code_lines[offset]) != first:
            continue
        if _norm(code_lines[offset + width - 1]) != tail:
            continue
        return offset + 1, offset + width
    return None


def _hunk_spans(patch: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for line in patch.splitlines():
        match = HUNK_HEADER.match(line.strip())
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        if count == 0:
            spans.append((start, start))
        else:
            spans.append((start, start + count - 1))
    return spans


def _detail_findings(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """Ground-truth findings for one patch file, at the vulnerable revision."""
    if not isinstance(detail, dict):
        return []
    file_name = detail.get("file_name")
    if not isinstance(file_name, str) or not file_name:
        return []
    code_before = detail.get("code_before")
    if not isinstance(code_before, str):
        code_before = ""
    findings: list[dict[str, Any]] = []
    for entry in detail.get("function_before") or []:
        if not isinstance(entry, dict) or entry.get("target") != 1:
            continue
        text = entry.get("function")
        if not isinstance(text, str) or not text.strip():
            continue
        name = _function_name(text)
        span = _locate_span(text, code_before) if code_before else None
        if span is None:
            line = entry.get("line")
            if isinstance(line, int) and line >= 1:
                span = (line, line)
        if span is None:
            continue
        findings.append(
            {
                "path": file_name,
                "function": name,
                "start_line": span[0],
                "end_line": span[1],
                "source": "function_before",
            }
        )
    if findings:
        return findings
    spans = _hunk_spans(detail.get("patch") or "")
    if not spans:
        return []
    start = min(span[0] for span in spans)
    end = max(span[1] for span in spans)
    return [
        {
            "path": file_name,
            "function": None,
            "start_line": start,
            "end_line": end,
            "source": "patch_hunks",
        }
    ]


def _cwes(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _clone_url(project: str, html_url: Any) -> str | None:
    if isinstance(html_url, str) and html_url.startswith(
        "https://github.com/"
    ):
        parts = html_url.split("/")
        if len(parts) >= 5:
            return f"https://github.com/{parts[3]}/{parts[4]}"
    if GITHUB_PATH.fullmatch(project or ""):
        return f"https://github.com/{project}"
    return None


def build_catalog(dataset: Path) -> dict[str, Any]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    skipped = Counter()
    with dataset.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                skipped["invalid_json"] += 1
                continue
            if record.get("outdated"):
                skipped["outdated"] += 1
                continue
            project = record.get("project")
            commit = record.get("commit_id")
            parents = record.get("parents") or []
            commit_before = None
            if parents and isinstance(parents[0], dict):
                commit_before = parents[0].get("commit_id_before")
            if (
                not isinstance(project, str)
                or not isinstance(commit, str)
                or not isinstance(commit_before, str)
            ):
                skipped["identity_missing"] += 1
                continue
            clone_url = _clone_url(project, record.get("html_url"))
            if clone_url is None:
                skipped["no_clone_url"] += 1
                continue
            cwes = _cwes(record.get("cwe_id"))
            if not cwes:
                skipped["cwe_missing"] += 1
                continue
            group = groups.setdefault(
                (project, commit),
                {
                    "project": project,
                    "fix_commit": commit,
                    "commit_before": commit_before,
                    "clone_url": clone_url,
                    "language": record.get("cve_language"),
                    "cve_ids": [],
                    "publish_date": None,
                    "cwes": set(),
                    "findings": [],
                },
            )
            if group["commit_before"] != commit_before:
                # same fix commit reached from different parents: keep first
                skipped["parent_conflict"] += 1
                continue
            if group["language"] != record.get("cve_language"):
                group["language"] = group["language"] or record.get("cve_language")
            cve = record.get("cve_id")
            if isinstance(cve, str) and cve not in group["cve_ids"]:
                group["cve_ids"].append(cve)
            published = record.get("publish_date")
            if group.get("publish_date") is None and isinstance(published, str):
                group["publish_date"] = published
            group["cwes"].update(cwes)
            details = record.get("details")
            if not isinstance(details, list):
                details = []
            for detail in details:
                for finding in _detail_findings(detail):
                    finding = dict(finding)
                    finding["cwes"] = cwes
                    group["findings"].append(finding)

    tasks: list[dict[str, Any]] = []
    for (project, commit), group in groups.items():
        # dedupe findings by (path, function, span)
        unique: dict[tuple, dict[str, Any]] = {}
        for finding in group["findings"]:
            key = (
                finding["path"],
                finding["function"],
                finding["start_line"],
                finding["end_line"],
            )
            existing = unique.get(key)
            if existing is not None:
                for cwe in finding["cwes"]:
                    if cwe not in existing["cwes"]:
                        existing["cwes"].append(cwe)
            else:
                unique[key] = finding
        findings = sorted(
            unique.values(),
            key=lambda item: (item["path"], item["start_line"]),
        )
        if not findings:
            skipped["no_findings"] += 1
            continue
        slug = project.replace("/", "-")
        # task_id must NOT embed the fix commit or CVE number: both are
        # network lookup keys that turn the ground truth into a single query.
        opaque = hashlib.sha256(f"{project}:{commit}".encode()).hexdigest()[:8]
        tasks.append(
            {
                "task_id": f"{slug}-{opaque}",
                "project": project,
                "fix_commit": commit,
                "commit_before": group["commit_before"],
                "clone_url": group["clone_url"],
                "language": group["language"],
                "cve_ids": group["cve_ids"],
                "publish_date": group.get("publish_date"),
                "cwes": sorted(group["cwes"]),
                "finding_count": len(findings),
                "findings": findings,
            }
        )
    tasks.sort(key=lambda item: (item["language"] or "", -item["finding_count"], item["task_id"]))
    return {
        "schema_version": 1,
        "built_from": str(dataset),
        "skipped": dict(skipped),
        "task_count": len(tasks),
        "tasks": tasks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(
            __import__("os").environ.get(
                "REPOSVUL_DATASET", str(DEFAULT_DATASET)
            )
        ),
    )
    parser.add_argument(
        "--output", type=Path, default=HERE / "tasks" / "catalog.json"
    )
    args = parser.parse_args()
    catalog = build_catalog(args.dataset)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(catalog, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    languages = Counter(task["language"] or "?" for task in catalog["tasks"])
    dense = sum(1 for task in catalog["tasks"] if task["finding_count"] >= 3)
    print(f"tasks: {catalog['task_count']} | languages: {dict(languages)}")
    print(f"finding_count>=3: {dense} | skipped: {catalog['skipped']}")
    print(f"written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
