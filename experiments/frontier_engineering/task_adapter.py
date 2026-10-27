#!/usr/bin/env python3
"""Task-family bridge from Frontier-Engineering v1-lite to Agent workspaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
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
    sha256_file,
    utc_now,
    write_json,
)
from bench_runtime_paths import configure_temp_environment  # noqa: E402
from experiments.frontier_engineering.config import (  # noqa: E402
    TaskContract,
    UPSTREAM_KEY,
    V1_LITE_TASKS,
)


CONTROLLER_PATH = Path(__file__).resolve()
BRIDGE_PATH = Path(__file__).resolve().with_name("evaluator_bridge.py")
SHELL_PATH = Path(__file__).resolve().with_name("evaluator_shell.sh")
BENCHMARK_NAME = "Frontier-Engineering v1-lite"
PRIMARY_METRIC = "combined_score"
DIRECTION = "maximize"
CODEX_SANDBOX = "workspace-write"
INVALID_SCORE = -1e18
OFFICIAL_BENCHMARK_COMPARABLE = True

TASK_ID = "ComputerSystems/MallocLab"
ARTIFACT_NAME = "mm.c"
CASE_SET_DESCRIPTION = "Frontier-Engineering v1-lite official UnifiedTask evaluator"
VERIFIER_TIMEOUT_SECONDS = 300
QUANTUM_TASK = "QuantumComputing/task_01_routing_qftentangled"


def evaluator_inputs(upstream_root: Path, task: TaskContract) -> list[Path]:
    """Freeze executable scoring and sandbox inputs, not just the outer bridge."""
    task_dir = upstream_root / "benchmarks" / task.task_id
    paths = {
        upstream_root / "frontier_eval/tasks/unified/evaluator/python.py",
        upstream_root / "frontier_eval/tasks/unified/spec.py",
        BRIDGE_PATH,
        SHELL_PATH,
        CONTROLLER_PATH,
        *[task_dir / "frontier_eval" / name for name in (
            "initial_program.txt", "candidate_destination.txt", "eval_command.txt",
            "copy_files.txt", "readonly_files.txt",
        )],
    }
    paths.update((upstream_root / "frontier_eval/tasks/unified").rglob("*.py"))
    paths.update((task_dir / "verification").rglob("*.py"))
    paths.update((task_dir / "tests").glob("case_*.json"))
    paths.update((task_dir / "frontier_eval").glob("*.txt"))
    paths.update((task_dir / "frontier_eval").rglob("*.sh"))
    for relative in metadata_list(task_dir, "agent_files.txt"):
        path = task_dir / relative
        if path.is_file():
            paths.add(path)
    if task.task_id == QUANTUM_TASK:
        paths.update((upstream_root / "benchmarks/_shared").glob("*.py"))
        paths.add(task_dir / "baseline/structural_optimizer.py")
    return sorted(paths, key=str)


def validate_runtime_assets(upstream_root: Path) -> None:
    if TASK_ID != QUANTUM_TASK:
        return
    for relative in (
        "benchmarks/_shared/candidate_sandbox.py",
        "benchmarks/_shared/qiskit_candidate_runner.py",
        f"benchmarks/{QUANTUM_TASK}/verification/utils.py",
    ):
        if not (upstream_root / relative).is_file():
            raise ValueError(f"corrected official Quantum evaluator asset missing: {relative}")


def configure_task(task_id: str) -> None:
    try:
        task = V1_LITE_TASKS[task_id]
    except KeyError as error:
        raise ValueError(f"unknown Frontier-Engineering v1-lite task: {task_id}") from error
    global TASK_ID, ARTIFACT_NAME, VERIFIER_TIMEOUT_SECONDS
    TASK_ID = task.task_id
    ARTIFACT_NAME = task.artifact_name
    VERIFIER_TIMEOUT_SECONDS = task.evaluator_timeout_seconds


def task_contract(task_id: str | None = None) -> TaskContract:
    return V1_LITE_TASKS[task_id or TASK_ID]


def safe_relative(value: str, *, label: str) -> Path:
    normalized = PurePosixPath(value.strip())
    if normalized.is_absolute() or not normalized.parts or ".." in normalized.parts:
        raise ValueError(f"unsafe {label}: {value!r}")
    return Path(*normalized.parts)


def metadata_scalar(task_dir: Path, name: str) -> str:
    path = task_dir / "frontier_eval" / name
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"empty Frontier-Engineering metadata: {path}")
    return value


def metadata_list(task_dir: Path, name: str) -> list[Path]:
    path = task_dir / "frontier_eval" / name
    if not path.is_file():
        return []
    return [
        safe_relative(line, label=name)
        for raw in path.read_text(encoding="utf-8").splitlines()
        for line in [raw.strip()]
        if line and not line.startswith("#")
    ]


def combined_sha256(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=str):
        digest.update(str(path).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def task_text(task_dir: Path, task: TaskContract) -> str:
    sections = [
        "# Objective",
        "",
        f"Optimize `{task.artifact_name}` for Frontier-Engineering task `{task.task_id}`.",
        "Preserve feasibility and maximize the official `combined_score`.",
    ]
    for relative in metadata_list(task_dir, "agent_files.txt"):
        path = task_dir / relative
        if path.is_file():
            sections.extend(
                [
                    "",
                    f"## Upstream context: {relative.as_posix()}",
                    "",
                    path.read_text(encoding="utf-8"),
                ]
            )
    constraints = task_dir / "frontier_eval" / "constraints.txt"
    if constraints.is_file():
        sections.extend(["", "## Evaluator constraints", "", constraints.read_text(encoding="utf-8")])
    sections.extend(
        [
            "",
            "## Evaluation",
            "",
            "Run `python3 evaluate.py` for official feedback.",
            f"Only edit `{task.artifact_name}`; leave controller and verifier files unchanged.",
            "Leave the best evaluator-verified artifact in the workspace.",
        ]
    )
    return "\n".join(sections).rstrip() + "\n"


def materialize_workspace(upstream_root: Path, workspace: Path) -> dict[str, Any]:
    task = task_contract()
    upstream_root = upstream_root.expanduser().absolute()
    workspace = workspace.expanduser().absolute()
    validate_runtime_assets(upstream_root)
    task_dir = upstream_root / "benchmarks" / task.task_id
    initial_relative = safe_relative(
        metadata_scalar(task_dir, "initial_program.txt"), label="initial_program"
    )
    seed = task_dir / initial_relative
    metadata_paths = [
        task_dir / "frontier_eval" / name
        for name in (
            "initial_program.txt",
            "candidate_destination.txt",
            "eval_command.txt",
            "copy_files.txt",
            "readonly_files.txt",
        )
    ]
    official_evaluator = upstream_root / "frontier_eval/tasks/unified/evaluator/python.py"
    official_spec = upstream_root / "frontier_eval/tasks/unified/spec.py"
    for path in (seed, official_evaluator, official_spec, BRIDGE_PATH, SHELL_PATH, *metadata_paths):
        if not path.is_file():
            raise FileNotFoundError(path)
    if seed.name != task.artifact_name:
        raise ValueError(
            f"{task.task_id}: profile artifact {task.artifact_name!r} does not match {seed.name!r}"
        )
    if workspace.exists():
        raise FileExistsError(workspace)
    workspace.mkdir(parents=True)
    (workspace / task.artifact_name).write_bytes(seed.read_bytes())
    (workspace / "TASK.md").write_text(task_text(task_dir, task), encoding="utf-8")
    if task.task_id == QUANTUM_TASK:
        # A relative command survives the Pi workspace guard and fork worktrees.
        # Both Plain and GP use the same task interpreter and read-only helper.
        python = upstream_root / ".venvs" / task.runtime_env / "bin/python"
        (workspace / "task_python.py").write_text(
            "\"\"\"Read-only entry to the benchmark's quantum Python runtime.\"\"\"\n"
            "import os, sys\n"
            f"python = {str(python)!r}\n"
            "os.environ['PYTHONPATH'] = os.getcwd() + os.pathsep + os.environ.get('PYTHONPATH', '')\n"
            "os.execv(python, [python, *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        (workspace / "structural_optimizer.py").write_bytes(
            (task_dir / "baseline/structural_optimizer.py").read_bytes()
        )
        with (workspace / "TASK.md").open("a", encoding="utf-8") as context:
            context.write(
                "\n## Local quantum diagnostics\n\n"
                "Use `python3 task_python.py .tmp/script.py` or `python3 task_python.py -` "
                "for Qiskit/MQT/Statevector experiments in this workspace. "
                "This is the same locked runtime used by the evaluator. "
                "`structural_optimizer.py` is a read-only copy of the official helper. "
                "Only `solve.py` is editable. Diagnostics do not produce official scores; "
                "use `python3 evaluate.py` for scoring.\n"
            )
    (workspace / "AGENTS.md").write_text(
        f"# Frontier-Engineering task rules\n\n- Only edit `{task.artifact_name}`.\n"
        "- Use `python3 evaluate.py` for official feedback.\n"
        "- Do not inspect parent directories, credentials, or network resources.\n",
        encoding="utf-8",
    )
    (workspace / "evaluate.py").write_text(
        render_evaluate_wrapper(CONTROLLER_PATH, upstream_root), encoding="utf-8"
    )
    verifier_dir = workspace / ".goal-plus-verifiers"
    verifier_dir.mkdir()
    (verifier_dir / "primary_metric.py").write_text(
        render_goal_plus_verifier(CONTROLLER_PATH, upstream_root, PRIMARY_METRIC),
        encoding="utf-8",
    )
    (workspace / ".gitignore").write_text(
        ".bench-runtime/\n.gp/\n.codex-log/\n.pi-log/\n__pycache__/\n*.pyc\n",
        encoding="utf-8",
    )
    frozen_inputs = evaluator_inputs(upstream_root, task)
    evaluator_hash = combined_sha256(frozen_inputs)
    metadata = {
        "schema_version": 1,
        "adapter": "frontier-engineering-v1-lite-native-task",
        "suite": "v1-lite",
        "task_id": task.task_id,
        "artifact_name": task.artifact_name,
        "artifact_source_relative": initial_relative.as_posix(),
        "upstream_root": str(upstream_root),
        "upstream_commit": git_commit(upstream_root),
        "source_revision": git_commit(upstream_root),
        "seed_sha256": sha256_file(seed),
        "evaluator": str(official_evaluator),
        "evaluator_sha256": evaluator_hash,
        "evaluator_inputs": [str(path) for path in frozen_inputs],
        "evaluation_protocol": "official-main-unitary" if task.task_id == QUANTUM_TASK else "official-main",
        "primary_metric": PRIMARY_METRIC,
        "direction": DIRECTION,
        "runtime_env": task.runtime_env,
        "runtime_python_env": task.runtime_python_env,
        "evaluator_timeout_seconds": task.evaluator_timeout_seconds,
    }
    write_json(workspace / "task.json", metadata)
    workspace_commit = init_git(
        workspace, f"materialize Frontier-Engineering {task.task_id}"
    )
    return {**metadata, "workspace": str(workspace), "workspace_commit": workspace_commit}


def evaluate_workspace(workspace: Path, upstream_root: Path, mode: str) -> dict[str, Any]:
    configure_temp_environment()
    started = time.monotonic()
    workspace = workspace.expanduser().absolute()
    upstream_root = upstream_root.expanduser().absolute()
    # Goal Plus checkpoints ignored Git files with -f; its runtime exclusion
    # contract covers .tmp, so public feedback must keep bookkeeping there.
    # An explicit controller GOAL_PLUS_VERIFIER_TMPDIR still takes precedence.
    destination, budget = claim_evaluator_call(workspace / ".tmp", mode)
    metadata = json.loads((workspace / "task.json").read_text(encoding="utf-8"))
    task = task_contract(str(metadata["task_id"]))
    candidate = workspace / task.artifact_name
    changes = candidate_changed_paths(workspace)
    unauthorized = sorted(changes - {task.artifact_name})
    metrics: dict[str, Any] = {}
    artifacts: dict[str, Any] = {}
    diagnostics = ""
    error: str | None = None
    if unauthorized or not candidate.is_file():
        error = (
            "candidate changed files outside the artifact: " + ", ".join(unauthorized)
            if unauthorized
            else f"candidate removed {task.artifact_name}"
        )
    elif git_commit(upstream_root) != metadata["upstream_commit"]:
        raise RuntimeError("pinned Frontier-Engineering checkout changed")
    else:
        task_dir = upstream_root / "benchmarks" / task.task_id
        pinned_paths = evaluator_inputs(upstream_root, task) if "evaluator_inputs" in metadata else [
            upstream_root / "frontier_eval/tasks/unified/evaluator/python.py",
            upstream_root / "frontier_eval/tasks/unified/spec.py",
            BRIDGE_PATH,
            SHELL_PATH,
            *[
                task_dir / "frontier_eval" / name
                for name in (
                    "initial_program.txt",
                    "candidate_destination.txt",
                    "eval_command.txt",
                    "copy_files.txt",
                    "readonly_files.txt",
                )
            ],
        ]
        if combined_sha256(pinned_paths) != metadata["evaluator_sha256"]:
            raise RuntimeError("pinned Frontier-Engineering evaluator inputs changed")
        driver_python = upstream_root / ".venvs/frontier-eval-driver/bin/python"
        command = [
            str(driver_python),
            str(BRIDGE_PATH),
            "--upstream-root",
            str(upstream_root),
            "--task-id",
            task.task_id,
            "--candidate",
            str(candidate),
            "--runtime-env",
            task.runtime_env,
        ]
        if task.runtime_python_env:
            command.extend(["--runtime-python-env", task.runtime_python_env])
        environment = os.environ.copy()
        environment["FRONTIER_ENGINEERING_ROOT"] = str(upstream_root)
        environment["FRONTIER_EVAL_EVALUATOR_TIMEOUT_S"] = str(
            task.evaluator_timeout_seconds
        )
        environment["PYTHONNOUSERSITE"] = "1"
        try:
            completed = subprocess.run(
                command,
                cwd=upstream_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=task.evaluator_timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                error = completed.stderr.strip() or completed.stdout.strip()
            else:
                payload = json.loads(completed.stdout)
                metrics = payload.get("metrics") or {}
                artifacts = payload.get("artifacts") or {}
                diagnostics = str(payload.get("diagnostics") or "")
        except Exception as exception:
            error = f"{type(exception).__name__}: {exception}"
    value = metrics.get(PRIMARY_METRIC)
    valid = bool(
        metrics.get("valid") in {True, 1, 1.0}
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
    score = float(value) if valid else INVALID_SCORE
    report = {
        "schema_version": 1,
        "benchmark": "frontier-engineering",
        "suite": "v1-lite",
        "task_id": task.task_id,
        "evaluation_protocol": metadata.get("evaluation_protocol", "legacy-frozen"),
        "source_revision": metadata["upstream_commit"],
        "evaluator_sha256": metadata["evaluator_sha256"],
        "mode": mode,
        "valid": valid,
        "primary_metric": {
            "name": PRIMARY_METRIC,
            "value": score,
            "direction": DIRECTION,
        },
        PRIMARY_METRIC: score,
        "raw_metrics": metrics,
        "artifacts": artifacts,
        "diagnostics": diagnostics,
        "error": error,
        "artifact_sha256": sha256_file(candidate) if candidate.is_file() else None,
        "duration_seconds": time.monotonic() - started,
        "budget": budget,
        "evaluated_at": utc_now(),
    }
    append_history(destination, report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    children = parser.add_subparsers(dest="command", required=True)
    evaluate = children.add_parser("evaluate")
    evaluate.add_argument("--workspace", type=Path, required=True)
    evaluate.add_argument("--upstream-root", type=Path, required=True)
    evaluate.add_argument("--mode", choices=("public", "final"), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    metadata = json.loads((args.workspace / "task.json").read_text(encoding="utf-8"))
    configure_task(str(metadata["task_id"]))
    report = evaluate_workspace(args.workspace, args.upstream_root, args.mode)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
