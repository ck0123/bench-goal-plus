#!/usr/bin/env python3
"""Public-workspace and hidden-grade boundary for one aibench coding case."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import shutil
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
from bench_runtime_paths import (  # noqa: E402
    configure_temp_environment,
    temporary_directory,
)
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
EVALUATION_MODE = "visible"
OFFICIAL_BENCHMARK_COMPARABLE = True
GOAL_PLUS_MCP_ENV_VARS = (
    "AIBENCH_PUBLIC_TESTS",
    "AIBENCH_PUBLIC_TESTS_SHA256",
)
PI_WORKER_SANDBOX = {
    "engine": "bubblewrap",
    "evaluation_mode": "visible",
    "workspace_access": "read_only",
    "read_only_host_paths": ["/aibench-public-tests"],
    "read_only_workspace_paths": [],
    "writable_workspace_paths": [ARTIFACT_NAME],
    "pass_env": [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "AIBENCH_PUBLIC_TESTS",
        "AIBENCH_PUBLIC_TESTS_SHA256",
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
_SHA256 = re.compile(r"[0-9a-f]{64}")
PUBLIC_TESTS_RELATIVE = Path(".bench-runtime/public-tests")
PUBLIC_TESTS_MOUNT = Path("/aibench-public-tests")
BOUNDARY_FILE = ".aibench-boundary.json"
TRUSTED_EVALUATOR_PATH = os.pathsep.join(
    (str(RUNTIME_PYTHON.parent), "/usr/local/bin", "/usr/bin", "/bin")
)


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
        "Public tests are available read-only at `/aibench-public-tests`. Plain "
        "and main trajectories can run `python3 evaluate.py`; Goal Plus workers "
        "must request process verification through Goal Plus. The controller runs "
        "hidden tests exactly once after selection. Do not inspect parent "
        "directories or benchmark metadata. Leave the complete solution under "
        "`submission/`.\n"
    )


def materialize_workspace(source_root: Path, workspace: Path) -> dict[str, Any]:
    source_root = source_root.expanduser().absolute()
    workspace = workspace.expanduser().absolute()
    if workspace.exists():
        raise FileExistsError(workspace)
    workspace.mkdir(parents=True)
    public_tests = workspace / PUBLIC_TESTS_RELATIVE
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
            "--public-tests",
            str(public_tests),
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
    public_test_files = _public_test_records(metadata.get("public_test_files"))
    boundary_sha256 = metadata.get("public_test_boundary_sha256")
    if not isinstance(boundary_sha256, str) or _SHA256.fullmatch(boundary_sha256) is None:
        raise RuntimeError("aibench public-test boundary hash is invalid")
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
            "public_test_boundary_sha256",
            "validity_ok",
        )
    }
    write_json(workspace / "task.json", public_metadata)
    (workspace / "TASK.md").write_text(_task_text(public_metadata), encoding="utf-8")
    (workspace / "AGENTS.md").write_text(
        "# aibench task rules\n\n"
        "- Edit only files below `submission/`.\n"
        "- Public tests are read-only at `/aibench-public-tests`.\n"
        "- Plain and main trajectories use `python3 evaluate.py` for public feedback.\n"
        "- Goal Plus workers request process verification through Goal Plus.\n"
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
        "public_test_count": len(public_test_files),
        "workspace": str(workspace),
        "workspace_commit": commit,
        "source_revision": git_commit(source_root.parents[1]),
        "primary_metric": PRIMARY_METRIC,
        "direction": DIRECTION,
    }


def _visible_ratio(output: str, language: str, returncode: int) -> float:
    if language == "javascript":
        counts = {
            match["kind"]: int(match["count"])
            for match in _NODE_COUNT.finditer(output)
        }
        total = counts.get("pass", 0) + counts.get("fail", 0)
        return counts.get("pass", 0) / total if total else float(returncode == 0)
    counts: dict[str, int] = {}
    for match in _PYTEST_COUNTS.finditer(output):
        counts[match["kind"]] = counts.get(match["kind"], 0) + int(match["count"])
    total = sum(counts.values())
    return counts.get("passed", 0) / total if total else float(returncode == 0)


def _public_test_records(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise RuntimeError("public_test_files must be a list")
    records = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise RuntimeError("public test record has an invalid schema")
        path, digest = item["path"], item["sha256"]
        relative = Path(path) if isinstance(path, str) else Path()
        if (
            not isinstance(path, str)
            or relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise RuntimeError("public test record contains an invalid path or hash")
        records.append({"path": relative.as_posix(), "sha256": digest})
    if len(records) != len({item["path"] for item in records}):
        raise RuntimeError("public test paths must be unique")
    return records


def _public_test_bundle(
    workspace: Path, metadata: dict[str, Any]
) -> tuple[Path, dict[str, Any], list[dict[str, str]]]:
    mounted = os.environ.get("AIBENCH_PUBLIC_TESTS")
    if mounted is not None and Path(mounted) != PUBLIC_TESTS_MOUNT:
        raise RuntimeError("aibench public-test mount path is not controller-owned")
    bundle = (
        PUBLIC_TESTS_MOUNT
        if PUBLIC_TESTS_MOUNT.is_dir() or mounted is not None
        else workspace / PUBLIC_TESTS_RELATIVE
    )
    if bundle.is_symlink() or not bundle.is_dir():
        raise RuntimeError("aibench public-test bundle is unavailable")
    bundle_root = bundle.resolve(strict=True)
    boundary_path = bundle / BOUNDARY_FILE
    if boundary_path.is_symlink() or not boundary_path.is_file():
        raise RuntimeError("aibench public-test boundary manifest is unavailable")
    boundary_bytes = boundary_path.read_bytes()
    expected_sha256 = os.environ.get("AIBENCH_PUBLIC_TESTS_SHA256")
    if expected_sha256 is None:
        if os.environ.get("GOAL_PLUS_VERIFIER_TMPDIR"):
            raise RuntimeError(
                "Goal Plus verifier is missing the controller-owned public-test hash"
            )
        expected_sha256 = metadata.get("public_test_boundary_sha256")
    if (
        not isinstance(expected_sha256, str)
        or _SHA256.fullmatch(expected_sha256) is None
        or hashlib.sha256(boundary_bytes).hexdigest() != expected_sha256
    ):
        raise RuntimeError("aibench public-test boundary hash does not match the task")
    boundary = json.loads(boundary_bytes)
    if not isinstance(boundary, dict) or boundary.get("schema_version") != 1:
        raise RuntimeError("aibench public-test boundary is invalid")
    for name in (
        "case_id",
        "case_set",
        "case_set_fingerprint",
        "case_fingerprint",
        "language",
        "grader_command",
    ):
        if not isinstance(boundary.get(name), str) or not boundary[name]:
            raise RuntimeError(f"aibench public-test boundary is missing {name}")
    records = _public_test_records(boundary.get("public_test_files"))
    for item in records:
        test = bundle / item["path"]
        if test.is_symlink() or not test.is_file():
            raise RuntimeError(f"aibench public test is unavailable: {item['path']}")
        try:
            test.resolve(strict=True).relative_to(bundle_root)
        except ValueError as error:
            raise RuntimeError(
                f"aibench public test escapes its bundle: {item['path']}"
            ) from error
        if hashlib.sha256(test.read_bytes()).hexdigest() != item["sha256"]:
            raise RuntimeError(f"aibench public test changed: {item['path']}")
    return bundle, boundary, records


def _reserved_public_test(
    submission: Path, records: list[dict[str, str]]
) -> str | None:
    root = submission.resolve(strict=True)
    for item in records:
        relative = Path(item["path"])
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                return item["path"]
            if current.exists() and current != root / relative and not current.is_dir():
                return item["path"]
        if (root / relative).exists():
            return item["path"]
    return None


def _submission_root(workspace: Path) -> Path:
    submission = workspace / ARTIFACT_NAME
    if submission.is_symlink() or not submission.is_dir():
        raise RuntimeError("aibench submission root must be a real directory")
    return submission.resolve(strict=True)


def _copy_public_tests(
    bundle: Path, records: list[dict[str, str]], destination: Path
) -> None:
    root = destination.resolve(strict=True)
    for item in records:
        relative = Path(item["path"])
        target = destination / relative
        if target.exists() or target.is_symlink():
            raise RuntimeError(
                f"candidate evaluation contains reserved public test: {relative}"
            )
        current = destination
        for part in relative.parts[:-1]:
            current /= part
            if current.is_symlink() or (current.exists() and not current.is_dir()):
                raise RuntimeError(
                    f"candidate evaluation conflicts with public test parent: {relative}"
                )
            current.mkdir(exist_ok=True)
        try:
            target.parent.resolve(strict=True).relative_to(root)
        except ValueError as error:
            raise RuntimeError(
                f"public test destination escapes evaluation workspace: {relative}"
            ) from error
        shutil.copy2(bundle / relative, target)


def _public_evaluation(
    workspace: Path,
    boundary: dict[str, Any],
    bundle: Path,
    records: list[dict[str, str]],
) -> dict[str, Any]:
    submission = _submission_root(workspace)
    reserved = _reserved_public_test(submission, records)
    if reserved is not None:
        return {
            "valid": False,
            "value": None,
            "diagnostics": f"candidate created controller-owned public test: {reserved}",
            "integrity_violation": f"reserved_public_test_path: {reserved}",
        }
    command = shlex.split(boundary["grader_command"])
    if not command:
        raise RuntimeError("aibench public grader command is empty")
    if command[0] == "python":
        command[0] = str(RUNTIME_PYTHON)
    environment = {
        name: os.environ[name]
        for name in (
            "LANG",
            "LC_ALL",
            "TZ",
            "SYSTEMROOT",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        )
        if name in os.environ
    }
    environment["PATH"] = TRUSTED_EVALUATOR_PATH
    with temporary_directory(
        prefix="public-evaluation-",
        namespace="aibench-coding/public-evaluation",
    ) as temporary:
        home = temporary / "home"
        scratch = temporary / "tmp"
        home.mkdir()
        scratch.mkdir()
        environment.update(
            {
                "HOME": str(home),
                "TMPDIR": str(scratch),
                "TMP": str(scratch),
                "TEMP": str(scratch),
                "PYTHONHASHSEED": "0",
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        evaluated = temporary / ARTIFACT_NAME
        shutil.copytree(submission, evaluated, symlinks=True)
        _copy_public_tests(bundle, records, evaluated)
        completed = subprocess.run(
            command,
            cwd=evaluated,
            env=environment,
            capture_output=True,
            text=True,
            timeout=VERIFIER_TIMEOUT_SECONDS,
            check=False,
        )
    output = completed.stdout + "\n" + completed.stderr
    ratio = _visible_ratio(output, boundary["language"], completed.returncode)
    valid = completed.returncode in {0, 1} and math.isfinite(ratio)
    return {
        "valid": valid,
        "value": ratio if valid else None,
        "returncode": completed.returncode,
        "diagnostics": output[-4000:],
    }


def _official_evaluation(
    workspace: Path, source_root: Path, boundary: dict[str, Any]
) -> dict[str, Any]:
    payload = _bridge(
        [
            "grade",
            "--source-root",
            str(source_root),
            "--case-set",
            boundary["case_set"],
            "--case-id",
            boundary["case_id"],
            "--submission",
            str(workspace / ARTIFACT_NAME),
            "--case-set-fingerprint",
            boundary["case_set_fingerprint"],
            "--case-fingerprint",
            boundary["case_fingerprint"],
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
    report_identity = metadata
    if unauthorized:
        result = {
            "valid": False,
            "value": None,
            "diagnostics": "candidate changed controller files: " + ", ".join(unauthorized),
        }
    elif mode == "public":
        bundle, boundary, records = _public_test_bundle(workspace, metadata)
        report_identity = boundary
        result = _public_evaluation(workspace, boundary, bundle, records)
    elif mode == "final":
        _bundle, boundary, _records = _public_test_bundle(workspace, metadata)
        report_identity = boundary
        result = _official_evaluation(workspace, source_root, boundary)
    else:
        raise ValueError(f"unsupported evaluator mode: {mode}")
    report = {
        "schema_version": 1,
        "benchmark": "aibench-coding",
        "case_id": report_identity["case_id"],
        "case_set": report_identity["case_set"],
        "case_set_fingerprint": report_identity["case_set_fingerprint"],
        "case_fingerprint": report_identity["case_fingerprint"],
        "mode": mode,
        "valid": result["valid"],
        "primary_metric": {
            "name": GOAL_PLUS_PROCESS_METRIC if mode == "public" else PRIMARY_METRIC,
            "value": result["value"],
            "direction": DIRECTION,
        },
        "grade": result.get("grade"),
        "diagnostics": result.get("diagnostics"),
        "integrity_violation": result.get("integrity_violation"),
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
