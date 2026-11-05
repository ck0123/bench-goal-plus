#!/usr/bin/env python3
"""Adapter for the ReposVul repository-level vulnerability audit benchmark.

One task = one (project, fixing commit) group from the ReposVul catalog
(ICSE 2024). Workers audit a checkout of the *parent* (vulnerable) commit
under ``source/`` and submit one JSON finding per ground-truth vulnerability
under ``submission/``. The controller-owned judge applies the same
location-then-type policy as the zsoft-detect oracle: a finding matches a
ground-truth item when it names the same file and either overlaps its line
span or names the same function, and its CWE label equals the accepted type;
matching is one-to-one, so duplicate reports count as false positives.

Worker-visible validation checks only the public finding format; the ground
truth never enters the workspace.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER_PATH = Path(__file__).resolve()
sys.path.insert(0, str(ROOT))
from adapters import zsoft_blind  # noqa: E402
from adapters.zsoft_blind import (  # noqa: E402
    DETECT_VALIDATION_KIND,
    PUBLIC_CHECKER_NAME,
    PUBLIC_METRIC,
    diagnostics_valid,
    ensure_single_final_claim,
    validate_detect_submission,
)

HERE = Path(__file__).resolve().parent
CATALOG_PATH = HERE / "tasks" / "catalog.json"
SCHEMA_SOURCE = HERE / "assets" / "finding.schema.json"
CHECKOUT_CACHE = Path(
    os.environ.get(
        "REPOSVUL_CHECKOUT_CACHE",
        "/home/tangzhiyun/workspace/ReposVul/checkouts",
    )
).expanduser()

BENCHMARK_NAME = "reposvul"
UPSTREAM_KEY = "reposvul"
# 基准源(任务目录)自含于本仓库,不走 environment/upstreams.json 托管检出;
# 任务源码在物化时按 clone_url 单独克隆到 REPOSVUL_CHECKOUT_CACHE。
LOCAL_SOURCE_RELATIVE = "adapters/reposvul"
ARTIFACT_NAME = "submission"
PRIMARY_METRIC = "f1"
DIRECTION = "maximize"
GOAL_PLUS_PROCESS_METRIC = PUBLIC_METRIC
PUBLIC_FORMAT_METRIC = PUBLIC_METRIC
CONTROLLER_ONLY_OFFICIAL_EVALUATION = True
EVALUATION_MODE = "blind"
REQUIRES_PROTECTED_PI_WORKERS = True
GOAL_PLUS_POSTHOC_SELECTION_CONTRACT = {
    "enabled": True,
    "metric_name": PRIMARY_METRIC,
    "metric_direction": DIRECTION,
    "candidate_scope": "all_publicly_compliant_iterations",
    "tie_break": "lowest_candidate_id_then_latest_iteration",
    "timing": "after_agent_exit_and_controller_closeout",
    "visible_to_workers": False,
}
CASE_SET_DESCRIPTION = (
    "one ReposVul repository-level audit task: findings on a pinned "
    "vulnerable commit"
)
CODEX_SANDBOX = "workspace-write"
PI_WORKER_SANDBOX = {
    "engine": "bubblewrap",
    "evaluation_mode": EVALUATION_MODE,
    "workspace_access": "read_only",
    "read_only_workspace_paths": ["source", "schemas"],
    "writable_workspace_paths": [ARTIFACT_NAME],
    "pass_env": [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    ],
}
OFFICIAL_EVALUATOR_TIMEOUT_SECONDS = 600
VERIFIER_TIMEOUT_SECONDS = 30

TASK_ID: str | None = None
TASK_SKILLS_ENV = "BENCH_GOAL_PLUS_REPOSVUL_TASK_SKILLS"
REA_SKILLS_ENV = "BENCH_GOAL_PLUS_REPOSVUL_REA_TOOLS"
TASK_SKILLS_SOURCE = HERE / "skills"


DEFAULT_TASK_ID: str | None = None
ACTIVE_TASK_ID: str | None = None


class AdapterError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- catalog


def load_catalog() -> dict[str, Any]:
    if not CATALOG_PATH.is_file():
        raise AdapterError(
            f"ReposVul catalog is missing: {CATALOG_PATH}; run "
            "adapters/reposvul/build_catalog.py first"
        )
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _default_task(catalog: dict[str, Any]) -> str:
    preferred = None
    fallback = None
    for task in catalog["tasks"]:
        if task.get("language") != "C":
            continue
        count = task.get("finding_count", 0)
        if 5 <= count <= 25:
            return task["task_id"]
        if fallback is None and count >= 3:
            fallback = task["task_id"]
    if preferred:
        return preferred
    if fallback:
        return fallback
    if catalog["tasks"]:
        return catalog["tasks"][0]["task_id"]
    raise AdapterError("ReposVul catalog is empty")


def catalog_task(task_id: str) -> dict[str, Any]:
    catalog = load_catalog()
    for task in catalog["tasks"]:
        if task["task_id"] == task_id:
            return task
    raise AdapterError(f"unknown reposvul task: {task_id}")


def list_task_ids() -> list[str]:
    return [task["task_id"] for task in load_catalog()["tasks"]]


def configure_task(task_id: str | None) -> None:
    global ACTIVE_TASK_ID, DEFAULT_TASK_ID, TASK_ID
    if task_id is None:
        if DEFAULT_TASK_ID is None:
            DEFAULT_TASK_ID = _default_task(load_catalog())
        ACTIVE_TASK_ID = DEFAULT_TASK_ID
    else:
        catalog_task(task_id)  # raises for unknown ids
        ACTIVE_TASK_ID = task_id
    TASK_ID = ACTIVE_TASK_ID


def active_task() -> dict[str, Any]:
    if ACTIVE_TASK_ID is None:
        configure_task(None)
    assert ACTIVE_TASK_ID is not None
    return catalog_task(ACTIVE_TASK_ID)


def copy_task_skills(workspace: Path) -> list[str]:
    """Copy adapter-owned task-local Skills into the workspace.

    Goal Plus discovers Git-tracked ``.agents/skills/<name>/SKILL.md`` bundles
    at freeze time and instructs every candidate worker to read them, so the
    copies must exist before ``init_git`` commits the workspace. Both skills
    are opt-in: path-validation via ``BENCH_GOAL_PLUS_REPOSVUL_TASK_SKILLS=1``
    (it drives the shared-knowledge board, so enable it together with
    ``--shared-cache``), REA binary verification via
    ``BENCH_GOAL_PLUS_REPOSVUL_REA_TOOLS=1``.
    """
    copied: list[str] = []
    skills_enabled = os.environ.get(TASK_SKILLS_ENV) == "1"
    rea_enabled = os.environ.get(REA_SKILLS_ENV) == "1"
    if not TASK_SKILLS_SOURCE.is_dir():
        return copied
    for skill_dir in sorted(TASK_SKILLS_SOURCE.iterdir()):
        if skill_dir.name == "rea-analysis" and not rea_enabled:
            continue
        if skill_dir.name != "rea-analysis" and not skills_enabled:
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_dir.is_dir() or not skill_file.is_file():
            continue
        if skill_dir.is_symlink() or skill_file.is_symlink():
            raise AdapterError(
                f"task-local Skill cannot be a symlink: {skill_dir.name}"
            )
        destination = workspace / ".agents" / "skills" / skill_dir.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            skill_dir,
            destination,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        copied.append(f".agents/skills/{skill_dir.name}/SKILL.md")
    return copied


# ------------------------------------------------------------- source tree


def _git(source: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(source), *arguments],
        capture_output=True,
        text=True,
        check=check,
    )


def validate_source_checkout(source: Path, commit: str, *, label: str) -> Path:
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise AdapterError(f"{label} is not a directory: {source}")
    top = _git(source, "rev-parse", "--show-toplevel", check=False)
    head = _git(source, "rev-parse", "HEAD", check=False)
    status = _git(
        source, "status", "--porcelain=v1", "--untracked-files=all", check=False
    )
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != source:
        raise AdapterError(f"{label} is not its Git top level: {source}")
    if head.returncode != 0 or head.stdout.strip() != commit:
        actual = head.stdout.strip() or "unavailable"
        raise AdapterError(
            f"{label} has wrong commit: expected {commit}, got {actual}"
        )
    if status.returncode != 0 or status.stdout.strip():
        raise AdapterError(f"{label} is not clean: {source}")
    return source


def fetch_source_checkout(
    clone_url: str, commit: str, destination: Path
) -> Path:
    """Blobless clone + checkout so old commits stay cheap on big repos."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--filter=blob:none",
            "--no-checkout",
            clone_url,
            str(destination),
        ],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AdapterError(
            f"git clone failed for {clone_url}: {completed.stderr[-300:]}"
        )
    fetch = _git(
        destination,
        "fetch",
        "--quiet",
        "--depth=1",
        "origin",
        commit,
        check=False,
    )
    if fetch.returncode != 0:
        # commit not directly fetchable: fall back to the full object store
        _git(destination, "fetch", "--quiet", "--unshallow", check=False)
    checkout = _git(destination, "checkout", "--quiet", "--force", commit, check=False)
    if checkout.returncode != 0:
        shutil.rmtree(destination, ignore_errors=True)
        raise AdapterError(
            f"cannot checkout {commit} from {clone_url}: {checkout.stderr[-300:]}"
        )
    return destination


def source_checkout(task: dict[str, Any]) -> tuple[Path, str]:
    """Return a cached, validated checkout of the vulnerable commit."""
    commit = task["commit_before"]
    checkout = CHECKOUT_CACHE / f"{task['project'].replace('/', '__')}-{commit}"
    if checkout.exists():
        return (
            validate_source_checkout(
                checkout, commit, label="cached ReposVul checkout"
            ),
            "cached",
        )
    parent = checkout.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".staging-{checkout.name}"
    if staging.exists():
        shutil.rmtree(staging)
    fetch_source_checkout(task["clone_url"], commit, staging)
    validate_source_checkout(
        staging, commit, label="fetched ReposVul checkout"
    )
    staging.rename(checkout)
    return checkout, "fetched"


# --------------------------------------------------------------- materialize


def task_text(task: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"# ReposVul audit task: {task['project']}",
            "",
            "Perform a static security audit of the source tree in `source/`.",
            "The checkout is the last revision before a security fix; find the",
            "vulnerabilities that the subsequent fix addressed.",
            "",
            f"Write each finding as one JSON file under `{ARTIFACT_NAME}/`,",
            "matching `schemas/finding.schema.json`: location (repo-relative",
            "path, function, 1-based inclusive line range), bug_type (a CWE id",
            "such as CWE-119 or cwe-119), and root_cause (cause/trigger/impact).",
            "",
            "Submit incrementally: as soon as one finding is verified against",
            "the source, write its JSON file immediately — partial submissions",
            "are valid and each verified file counts; an unfinished batch at",
            "the deadline counts as nothing.",
            "",
            f"Use `python3 {PUBLIC_CHECKER_NAME}` to check only the public JSON",
            "structure. It does not assess whether a reported issue is real.",
        ]
    ) + "\n"


def materialize_workspace(
    source_root: Path,
    workspace: Path,
) -> dict[str, Any]:
    workspace = Path(workspace).expanduser().absolute()
    if workspace.exists() or workspace.is_symlink():
        raise FileExistsError(workspace)
    task = active_task()
    checkout, materialization = source_checkout(task)
    if checkout.resolve() in workspace.resolve(strict=False).parents or (
        workspace.resolve(strict=False) in checkout.resolve(strict=True).parents
    ):
        raise AdapterError("workspace must be disjoint from the source checkout")

    workspace.mkdir(parents=True)
    from adapters.portable import copytree_confined

    copytree_confined(
        checkout,
        workspace / "source",
        label="ReposVul source checkout",
        ignore=shutil.ignore_patterns(".git"),
    )
    schema_destination = workspace / "schemas" / "finding.schema.json"
    schema_destination.parent.mkdir(parents=True)
    shutil.copy2(SCHEMA_SOURCE, schema_destination, follow_symlinks=False)
    (workspace / ARTIFACT_NAME).mkdir()
    (workspace / ARTIFACT_NAME / ".gitkeep").write_text("")
    (workspace / "TASK.md").write_text(task_text(task))
    (workspace / "AGENTS.md").write_text(
        "# ReposVul audit task rules\n\n"
        "- Audit only the tree under `source/`.\n"
        f"- Write finding JSON files into `{ARTIFACT_NAME}/`.\n"
        "- `schemas/finding.schema.json` and `bench-contract.json` are"
        " read-only reference.\n"
        f"- `{PUBLIC_CHECKER_NAME}` checks structure only and is read-only.\n"
        "- Hidden task data and other run directories are forbidden.\n"
    )
    shutil.copy2(
        Path(zsoft_blind.__file__),
        workspace / PUBLIC_CHECKER_NAME,
        follow_symlinks=False,
    )
    (workspace / ".gitignore").write_text(
        ".bench-runtime/\n.gp/\n.codex-log/\n__pycache__/\n*.pyc\n"
    )

    copied_skills = copy_task_skills(workspace)

    # Worker-visible manifest intentionally omits every ground-truth lookup
    # key: no fix commit, no CVE/CWE ids, no source revision (each can be
    # resolved against the remote repository to recover the patch).
    metadata = {
        "schema_version": 1,
        "adapter": BENCHMARK_NAME,
        "task_id": task["task_id"],
        "project_id": task["project"],
        "artifact_name": ARTIFACT_NAME,
        "source_materialization": materialization,
        "controller_only_official_evaluation": True,
        "evaluation_mode": EVALUATION_MODE,
        "requires_protected_pi_workers": REQUIRES_PROTECTED_PI_WORKERS,
        "public_validation_kind": DETECT_VALIDATION_KIND,
        "scan_roots": [],
        "task_skills": copied_skills,
        "primary_metric": GOAL_PLUS_PROCESS_METRIC,
        "direction": DIRECTION,
    }
    (workspace / "task.json").write_text(json.dumps(metadata, indent=2) + "\n")

    from adapters.portable import init_git

    workspace_commit = init_git(
        workspace, f"reposvul {task['task_id']}"
    )
    return {
        "task_id": task["task_id"],
        "workspace_commit": workspace_commit,
        "upstream_commit": task["fix_commit"],
    }


# -------------------------------------------------------------------- judge


_CWE_NORMALIZER = re.compile(r"^cwe[-_ ]?(\d+)$", re.IGNORECASE)


def _normalize_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _CWE_NORMALIZER.match(value.strip())
    if match:
        return f"CWE-{match.group(1)}"
    return value.strip().upper()


def _normalize_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = value.strip().lstrip("./")
    # workers audit the tree under ``source/`` and naturally prefix finding
    # paths with it; ground truth is repo-relative, so strip the workspace
    # mount prefix instead of failing every match on a cosmetic difference.
    if path.startswith("source/"):
        path = path[len("source/"):]
    return path or None


def _finding_location(finding: dict[str, Any]) -> dict[str, Any]:
    location = finding.get("location")
    return location if isinstance(location, dict) else {}


def _spans_overlap(
    finding: dict[str, Any], truth: dict[str, Any]
) -> bool:
    location = _finding_location(finding)
    try:
        start = max(location["start_line"], truth["start_line"])
        end = min(location["end_line"], truth["end_line"])
    except (KeyError, TypeError):
        return False
    return start <= end


def _same_function(finding: dict[str, Any], truth: dict[str, Any]) -> bool:
    left = _finding_location(finding).get("function")
    right = truth.get("function")
    return (
        isinstance(left, str)
        and isinstance(right, str)
        and left.strip() == right.strip()
        and bool(left.strip())
    )


def _types_compatible(finding: dict[str, Any], truth: dict[str, Any]) -> bool:
    label = _normalize_type(finding.get("bug_type"))
    if label is None:
        return False
    return label in {_normalize_type(cwe) for cwe in truth.get("cwes") or []}


def score_findings(
    findings: list[dict[str, Any]], truths: list[dict[str, Any]]
) -> dict[str, Any]:
    """Location-then-type judge with one-to-one matching (zsoft-detect style)."""
    matched_findings: set[int] = set()
    matches: list[dict[str, Any]] = []
    for truth_index, truth in enumerate(truths):
        truth_path = _normalize_path(truth.get("path"))
        claimed: int | None = None
        for finding_index, finding in enumerate(findings):
            if finding_index in matched_findings:
                continue
            if (
                _normalize_path(_finding_location(finding).get("path"))
                != truth_path
            ):
                continue
            if not (
                _spans_overlap(finding, truth) or _same_function(finding, truth)
            ):
                continue
            if not _types_compatible(finding, truth):
                continue
            claimed = finding_index
            break
        if claimed is not None:
            matched_findings.add(claimed)
            matches.append(
                {"truth_index": truth_index, "finding_index": claimed}
            )
    tp = len(matches)
    fp = len(findings) - tp
    fn = len(truths) - tp
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "matches": matches,
    }


def _snapshot_submission(source: Path, destination: Path) -> Path:
    """Copy direct regular files without following worker-created links."""
    destination.mkdir(mode=0o700, exist_ok=False)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise AdapterError("submission snapshots require O_NOFOLLOW support")
    with os.scandir(source) as entries:
        for entry in entries:
            entry_path = Path(entry.path)
            if entry.name == ".gitkeep":
                continue
            if entry.is_symlink():
                raise AdapterError(
                    f"submission entry is a symlink: {entry.name}"
                )
            if not entry.is_file(follow_symlinks=False):
                raise AdapterError(
                    f"submission entry is not a regular file: {entry.name}"
                )
            descriptor = os.open(entry_path, os.O_RDONLY | nofollow)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise AdapterError(
                        f"submission entry changed type while reading: {entry.name}"
                    )
                with os.fdopen(descriptor, "rb", closefd=False) as candidate:
                    payload = candidate.read()
            finally:
                os.close(descriptor)
            (destination / entry.name).write_bytes(payload)
    return destination


def evaluate_workspace(
    workspace: Path, upstream_root: Path, mode: str
) -> dict[str, Any]:
    from adapters.portable import append_history, claim_evaluator_call

    if mode not in {"public", "final"}:
        raise ValueError(f"unsupported evaluation mode: {mode}")
    started = time.monotonic()
    workspace = Path(workspace).expanduser().absolute()
    metadata = json.loads((workspace / "task.json").read_text(encoding="utf-8"))
    runtime_dir, budget = claim_evaluator_call(workspace, mode)
    ensure_single_final_claim(mode, budget)
    submission = workspace / metadata["artifact_name"]
    public_diagnostics = validate_detect_submission(submission)
    format_valid = diagnostics_valid(public_diagnostics)

    if mode == "public":
        report = {
            "schema_version": 1,
            "task_id": metadata["task_id"],
            "mode": mode,
            "valid": format_valid,
            "primary_metric": {
                "name": GOAL_PLUS_PROCESS_METRIC,
                "value": 1.0 if format_valid else 0.0,
                "direction": "maximize",
            },
            GOAL_PLUS_PROCESS_METRIC: 1.0 if format_valid else 0.0,
            "public_diagnostics": public_diagnostics,
            "budget": budget,
            "duration_seconds": time.monotonic() - started,
            "evaluated_at": _utc_now(),
        }
        append_history(runtime_dir, report)
        return report

    valid = format_valid
    message = "ok" if valid else "candidate artifact failed public validation"
    score: dict[str, Any] | None = None
    if valid:
        task = catalog_task(metadata["task_id"])
        try:
            snapshot = _snapshot_submission(
                submission,
                runtime_dir / f"submission-{budget['total_claimed']:06d}",
            )
        except (AdapterError, OSError) as exc:
            valid = False
            message = f"candidate artifact is unsafe: {exc}"
        else:
            findings: list[dict[str, Any]] = []
            for path in sorted(snapshot.glob("*.json")):
                try:
                    findings.append(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                except (OSError, json.JSONDecodeError):
                    continue
            score = score_findings(findings, task["findings"])

    f1 = float(score["f1"]) if score else 0.0
    report = {
        "schema_version": 1,
        "task_id": metadata["task_id"],
        "mode": mode,
        "valid": valid,
        "primary_metric": {
            "name": PRIMARY_METRIC,
            "value": f1,
            "direction": DIRECTION,
        },
        PRIMARY_METRIC: f1,
        "reposvul_score": score,
        "message": message,
        "format_valid": format_valid,
        "public_diagnostics": public_diagnostics,
        "budget": budget,
        "duration_seconds": time.monotonic() - started,
        "evaluated_at": _utc_now(),
    }
    append_history(runtime_dir, report)
    return report


def git_commit(path: Path) -> str:
    target = Path(path).expanduser().absolute()
    completed = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        return completed.stdout.strip()
    catalog = load_catalog()
    return f"reposvul-catalog-{catalog.get('task_count', 0)}-tasks"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    materialize = subparsers.add_parser("materialize")
    materialize.add_argument("--upstream-root", type=Path, required=True)
    materialize.add_argument("--workspace", type=Path, required=True)
    materialize.add_argument("--task-id", default=None)
    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--workspace", type=Path, required=True)
    evaluate.add_argument("--upstream-root", type=Path, required=True)
    evaluate.add_argument("--mode", choices=("public", "final"), default="public")
    evaluate.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "materialize":
        configure_task(getattr(args, "task_id", None))
        print(
            json.dumps(
                materialize_workspace(args.upstream_root, args.workspace),
                indent=2,
            )
        )
        return 0
    report = evaluate_workspace(args.workspace, args.upstream_root, args.mode)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
