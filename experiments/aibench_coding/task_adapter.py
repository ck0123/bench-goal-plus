#!/usr/bin/env python3
"""Public-workspace and hidden-grade boundary for one aibench coding case."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from adapters.portable import (  # noqa: E402
    append_history,
    candidate_changed_paths,
    claim_evaluator_call,
    git_commit,
    init_git,
    render_evaluate_wrapper,
    render_goal_plus_verifier,
    utc_now,
    write_json,
)
from bench_runtime_paths import configure_temp_environment  # noqa: E402
from experiments.aibench_coding.config import (  # noqa: E402
    RUNTIME_PYTHON,
    VERIFIER_TIMEOUT_SECONDS,
)


UPSTREAM_KEY = "aibench_coding"
BENCHMARK_NAME = "aibench AI-Coding-Assist"
CASE_SET_DESCRIPTION = "aibench _clean2026 coding repair cases"
ARTIFACT_NAME = "submission"
PRIMARY_METRIC = "task_success"
GOAL_PLUS_PROCESS_METRIC = "visible_test_score"
DIRECTION = "maximize"
CODEX_SANDBOX = "workspace-write"
CONTROLLER_ONLY_OFFICIAL_EVALUATION = True
OFFICIAL_BENCHMARK_COMPARABLE = True
PI_WORKER_SANDBOX = {
    "engine": "bubblewrap",
    "workspace_access": "read_only",
    "read_only_workspace_paths": [],
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
TASK_ID = "rev-09f7740f614d3ea9"
ACTIVE_CASE_SET = "_clean2026"
EXPECTED_SET_FINGERPRINT = "9149d02169845dc5"
VALIDITY_POLICY = "valid-only"
BRIDGE = Path(__file__).resolve().with_name("bridge.py")
CONTROLLER = Path(__file__).resolve()
_PYTEST_COUNTS = re.compile(r"(?P<count>\d+) (?P<kind>passed|failed|errors?)")
_NODE_COUNT = re.compile(r"^# (?P<kind>pass|fail) (?P<count>\d+)\s*$", re.MULTILINE)


def configure_task(task_id: str | None) -> None:
    global TASK_ID
    if task_id is not None:
        TASK_ID = task_id


def configure_case_set(
    case_set: str,
    expected_fingerprint: str,
    validity_policy: str,
) -> None:
    global ACTIVE_CASE_SET, EXPECTED_SET_FINGERPRINT, VALIDITY_POLICY
    ACTIVE_CASE_SET = case_set
    EXPECTED_SET_FINGERPRINT = expected_fingerprint
    VALIDITY_POLICY = validity_policy


def _bridge_environment(source_root: Path) -> dict[str, str]:
    environment = configure_temp_environment(os.environ.copy())
    environment["PATH"] = str(RUNTIME_PYTHON.parent) + os.pathsep + environment.get(
        "PATH", ""
    )
    environment["PYTHONPATH"] = str(source_root / "src")
    environment["PYTHONNOUSERSITE"] = "1"
    return dict(environment)


def _bridge(command: list[str], source_root: Path, timeout: int = 300) -> dict[str, Any]:
    if not RUNTIME_PYTHON.is_file():
        raise FileNotFoundError(
            f"aibench runtime is missing: {RUNTIME_PYTHON}; run benchmark setup"
        )
    completed = subprocess.run(
        [str(RUNTIME_PYTHON), str(BRIDGE), *command],
        cwd=source_root,
        env=_bridge_environment(source_root),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("aibench bridge returned a non-object")
    return payload


def _task_text(metadata: dict[str, Any]) -> str:
    return (
        "# Objective\n\n"
        f"Solve aibench case `{metadata['case_id']}` in `submission/`.\n\n"
        f"{metadata['prompt']}\n\n"
        "# Verification\n\n"
        "Run `python3 evaluate.py` for public tests. The controller runs hidden "
        "tests exactly once after selection. Do not inspect parent directories or "
        "benchmark metadata. Leave the complete solution under `submission/`.\n"
    )


def materialize_workspace(source_root: Path, workspace: Path) -> dict[str, Any]:
    source_root = source_root.expanduser().absolute()
    workspace = workspace.expanduser().absolute()
    if workspace.exists():
        raise FileExistsError(workspace)
    workspace.mkdir(parents=True)
    metadata = _bridge(
        [
            "materialize",
            "--source-root",
            str(source_root),
            "--case-set",
            ACTIVE_CASE_SET,
            "--case-id",
            TASK_ID,
            "--destination",
            str(workspace / ARTIFACT_NAME),
        ],
        source_root,
    )
    if metadata.get("case_set_fingerprint") != EXPECTED_SET_FINGERPRINT:
        raise RuntimeError(
            "aibench case-set fingerprint mismatch: expected "
            f"{EXPECTED_SET_FINGERPRINT}, got {metadata.get('case_set_fingerprint')}"
        )
    if VALIDITY_POLICY == "valid-only" and metadata.get("validity_ok") is not True:
        raise RuntimeError(f"aibench case {TASK_ID} does not pass the validity gate")
    public_metadata = {
        key: metadata[key]
        for key in (
            "schema_version",
            "case_id",
            "case_set",
            "case_set_fingerprint",
            "case_fingerprint",
            "task_type",
            "language",
            "prompt",
            "grader_command",
            "validity_ok",
        )
    }
    write_json(workspace / "task.json", public_metadata)
    (workspace / "TASK.md").write_text(_task_text(public_metadata), encoding="utf-8")
    (workspace / "AGENTS.md").write_text(
        "# aibench task rules\n\n"
        "- Edit only files below `submission/`.\n"
        "- Use `python3 evaluate.py` for public feedback.\n"
        "- Do not inspect parent directories, benchmark cases, or hidden tests.\n",
        encoding="utf-8",
    )
    (workspace / "evaluate.py").write_text(
        render_evaluate_wrapper(CONTROLLER, source_root), encoding="utf-8"
    )
    goal_plus_verifier = render_goal_plus_verifier(
        CONTROLLER, source_root, GOAL_PLUS_PROCESS_METRIC
    )
    (workspace / "public_check.py").write_text(
        goal_plus_verifier, encoding="utf-8"
    )
    verifier_dir = workspace / ".goal-plus-verifiers"
    verifier_dir.mkdir()
    (verifier_dir / "primary_metric.py").write_text(
        goal_plus_verifier, encoding="utf-8"
    )
    (workspace / ".gitignore").write_text(
        ".bench-runtime/\n.gp/\n.codex-log/\n.pi-log/\n__pycache__/\n*.pyc\n",
        encoding="utf-8",
    )
    commit = init_git(workspace, f"materialize aibench {TASK_ID}")
    return {
        **public_metadata,
        "workspace": str(workspace),
        "workspace_commit": commit,
        "source_revision": git_commit(source_root.parents[1]),
        "primary_metric": PRIMARY_METRIC,
        "direction": DIRECTION,
    }


def _visible_ratio(output: str, language: str, returncode: int) -> float:
    if language == "javascript":
        counts = {match["kind"]: int(match["count"]) for match in _NODE_COUNT.finditer(output)}
        total = counts.get("pass", 0) + counts.get("fail", 0)
        return counts.get("pass", 0) / total if total else float(returncode == 0)
    counts: dict[str, int] = {}
    for match in _PYTEST_COUNTS.finditer(output):
        counts[match["kind"]] = counts.get(match["kind"], 0) + int(match["count"])
    total = sum(counts.values())
    return counts.get("passed", 0) / total if total else float(returncode == 0)


def _public_evaluation(workspace: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    command = shlex.split(str(metadata["grader_command"]))
    if not command:
        raise RuntimeError("aibench public grader command is empty")
    if command[0] == "python":
        command[0] = str(RUNTIME_PYTHON)
    environment = configure_temp_environment(os.environ.copy())
    environment["PYTHONNOUSERSITE"] = "1"
    completed = subprocess.run(
        command,
        cwd=workspace / ARTIFACT_NAME,
        env=environment,
        capture_output=True,
        text=True,
        timeout=VERIFIER_TIMEOUT_SECONDS,
        check=False,
    )
    output = completed.stdout + "\n" + completed.stderr
    ratio = _visible_ratio(output, str(metadata["language"]), completed.returncode)
    valid = completed.returncode in {0, 1} and math.isfinite(ratio)
    return {
        "valid": valid,
        "value": ratio if valid else None,
        "returncode": completed.returncode,
        "diagnostics": output[-4000:],
    }


def _official_evaluation(
    workspace: Path, source_root: Path, metadata: dict[str, Any]
) -> dict[str, Any]:
    payload = _bridge(
        [
            "grade",
            "--source-root",
            str(source_root),
            "--case-set",
            str(metadata["case_set"]),
            "--case-id",
            str(metadata["case_id"]),
            "--submission",
            str(workspace / ARTIFACT_NAME),
            "--public-metadata",
            str(workspace / "task.json"),
        ],
        source_root,
        timeout=VERIFIER_TIMEOUT_SECONDS + 60,
    )
    grade = payload.get("grade") or {}
    invalid = bool(grade.get("infra_error") or grade.get("collection_error"))
    return {
        "valid": not invalid,
        "value": bool(grade.get("passed")) if not invalid else None,
        "grade": grade,
        "diagnostics": str(grade.get("detail") or ""),
    }


def evaluate_workspace(workspace: Path, source_root: Path, mode: str) -> dict[str, Any]:
    started = time.monotonic()
    workspace = workspace.expanduser().absolute()
    source_root = source_root.expanduser().absolute()
    destination, budget = claim_evaluator_call(workspace, mode)
    metadata = json.loads((workspace / "task.json").read_text(encoding="utf-8"))
    changed = candidate_changed_paths(workspace)
    unauthorized = sorted(path for path in changed if not path.startswith("submission/"))
    result: dict[str, Any]
    if unauthorized:
        result = {
            "valid": False,
            "value": None,
            "diagnostics": "candidate changed controller files: " + ", ".join(unauthorized),
        }
    elif mode == "public":
        result = _public_evaluation(workspace, metadata)
    elif mode == "final":
        result = _official_evaluation(workspace, source_root, metadata)
    else:
        raise ValueError(f"unsupported evaluator mode: {mode}")
    report = {
        "schema_version": 1,
        "benchmark": "aibench-coding",
        "case_id": metadata["case_id"],
        "case_set": metadata["case_set"],
        "case_set_fingerprint": metadata["case_set_fingerprint"],
        "case_fingerprint": metadata["case_fingerprint"],
        "mode": mode,
        "valid": result["valid"],
        "primary_metric": {
            "name": GOAL_PLUS_PROCESS_METRIC if mode == "public" else PRIMARY_METRIC,
            "value": result["value"],
            "direction": DIRECTION,
        },
        "grade": result.get("grade"),
        "diagnostics": result.get("diagnostics"),
        "unauthorized_changes": unauthorized,
        "runtime_seconds": time.monotonic() - started,
        "evaluated_at": utc_now(),
        "budget": budget,
    }
    write_json(destination / f"{mode}-{budget['total_claimed']:04d}.json", report)
    append_history(destination, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("evaluate",))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--upstream-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("public", "final"), required=True)
    args = parser.parse_args(argv)
    report = evaluate_workspace(args.workspace, args.upstream_root, args.mode)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
