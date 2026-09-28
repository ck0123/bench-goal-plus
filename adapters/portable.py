"""Shared controller utilities for single-artifact benchmark adapters."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bench_runtime_paths import configure_temp_environment

configure_temp_environment()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_confined_symlinks(root: Path, *, label: str) -> None:
    """Reject links whose fully resolved target leaves a published tree."""
    root = Path(root).absolute()
    if root.is_symlink():
        raise RuntimeError(f"{label} root must not be a symlink: {root}")
    if not root.is_dir():
        raise FileNotFoundError(f"{label} root is not a directory: {root}")
    resolved_root = root.resolve(strict=True)
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in (*dirnames, *filenames):
            path = parent / name
            if not path.is_symlink():
                continue
            try:
                target = path.resolve(strict=False)
            except RuntimeError as exc:
                raise RuntimeError(
                    f"{label} contains an unresolvable symlink: "
                    f"{path.relative_to(root)}"
                ) from exc
            if target != resolved_root and resolved_root not in target.parents:
                raise RuntimeError(
                    f"{label} symlink escapes its root: "
                    f"{path.relative_to(root)} -> {os.readlink(path)}"
                )


def copytree_confined(
    source: Path,
    destination: Path,
    *,
    label: str,
    ignore: Any = None,
) -> Path:
    """Copy a public tree while preserving only root-confined symlinks."""
    validate_confined_symlinks(source, label=label)
    return Path(
        shutil.copytree(
            source,
            destination,
            symlinks=True,
            ignore=ignore,
        )
    )


def git_commit(path: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def init_git(workspace: Path, message: str) -> str:
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.name", "Benchmark Controller"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "config",
            "user.email",
            "benchmark-controller@example.invalid",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-q", "-m", message],
        check=True,
    )
    return git_commit(workspace)


def changed_workspace_paths(workspace: Path) -> set[str]:
    tracked = subprocess.run(
        ["git", "-C", str(workspace), "diff", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    untracked = subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "ls-files",
            "--others",
            "--exclude-standard",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        line
        for line in (*tracked.stdout.splitlines(), *untracked.stdout.splitlines())
        if line
    }


def candidate_changed_paths(workspace: Path) -> set[str]:
    """Return candidate-owned changes, excluding controller/runtime artifacts."""
    return {
        path
        for path in changed_workspace_paths(workspace)
        if not path.startswith(".bench-runtime/")
        and not path.startswith(".tmp/")
        and path != "results.tsv"
    }


def runtime_dir(workspace: Path) -> Path:
    verifier_tmpdir = os.getenv("GOAL_PLUS_VERIFIER_TMPDIR")
    return (
        Path(verifier_tmpdir) / "benchmark-runtime"
        if verifier_tmpdir
        else workspace / ".bench-runtime"
    )


def claim_evaluator_call(workspace: Path, mode: str) -> tuple[Path, dict[str, Any]]:
    destination = runtime_dir(workspace)
    destination.mkdir(parents=True, exist_ok=True)
    lock_path = destination / "budget.lock"
    budget_path = destination / "budget.json"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        budget = (
            json.loads(budget_path.read_text())
            if budget_path.is_file()
            else {
                "schema_version": 1,
                "total_claimed": 0,
                "public_claimed": 0,
                "final_claimed": 0,
            }
        )
        budget["total_claimed"] += 1
        budget[f"{mode}_claimed"] += 1
        write_json(budget_path, budget)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return destination, budget


def append_history(destination: Path, report: dict[str, Any]) -> None:
    with (destination / "history.jsonl").open("a") as history:
        history.write(json.dumps(report, sort_keys=True) + "\n")


def render_evaluate_wrapper(controller_path: Path, upstream_root: Path) -> str:
    return (
        "#!/usr/bin/env python3\n"
        '"""Controller-owned public evaluator wrapper; do not edit."""\n'
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        f"CONTROLLER = Path({str(controller_path)!r})\n"
        f"UPSTREAM = Path({str(upstream_root)!r})\n"
        "raise SystemExit(subprocess.call([sys.executable, str(CONTROLLER), "
        "'evaluate', '--workspace', str(Path(__file__).resolve().parent), "
        "'--upstream-root', str(UPSTREAM), '--mode', 'public']))\n"
    )


def render_goal_plus_verifier(
    controller_path: Path,
    upstream_root: Path,
    metric_name: str,
    *,
    include_controller_evidence: bool = False,
) -> str:
    payload_code = (
        f"payload = {{{metric_name!r}: float(value), 'valid': True}}\n"
        "payload['controller_evidence'] = report.get('controller_evidence')\n"
        if include_controller_evidence
        else f"payload = {{{metric_name!r}: float(value), 'valid': True}}\n"
    )
    return (
        "#!/usr/bin/env python3\n"
        '"""Controller-owned Goal Plus verifier; do not edit."""\n'
        "import json\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        f"CONTROLLER = Path({str(controller_path)!r})\n"
        f"UPSTREAM = Path({str(upstream_root)!r})\n"
        "completed = subprocess.run(\n"
        "    [sys.executable, str(CONTROLLER), 'evaluate', '--workspace', str(Path.cwd()),\n"
        "     '--upstream-root', str(UPSTREAM), '--mode', 'public'],\n"
        "    capture_output=True, text=True,\n"
        ")\n"
        "if completed.returncode != 0:\n"
        "    sys.stderr.write(completed.stderr)\n"
        "    raise SystemExit(completed.returncode)\n"
        "report = json.loads(completed.stdout)\n"
        "metric = report.get('primary_metric') or {}\n"
        "value = metric.get('value')\n"
        "if report.get('valid') is not True or not isinstance(value, (int, float)):\n"
        "    raise SystemExit('official evaluator rejected the candidate')\n"
        + payload_code
        + "print(json.dumps(payload))\n"
    )
