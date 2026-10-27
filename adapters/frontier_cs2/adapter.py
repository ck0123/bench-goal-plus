"""Frontier-CS 2 task-level verifier boundary for local (non-site) runs.

Each task tree under ``tasks/frontier-cs-2.0-demo/problems/<Category>/<task_id>``
carries its own agent-base and verifier Dockerfiles plus judge materials
(evaluator, reference patch, beat spec, workloads). This adapter builds the
official verifier image from the task tree and invokes it exactly as the
platform does (``verify.py`` / ``verify_reference.py`` with the five
``FRONTIER_*`` environment contracts), so CPU-only tasks verify locally with
Docker alone and no AWS EKS site.

Validated on binary_fuse_filter at upstream commit
57b97dcbf1793789dd30fe9667c2daa11b94d1ad; see
evidence/environment/2026-10-27-frontier-cs-2-local-verifier-smoke.json.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from adapters.portable import write_json  # noqa: E402
from bench_runtime_paths import make_preserved_temp_directory  # noqa: E402

UPSTREAM_KEY = "frontier_cs2"
BENCHMARK_ID = "frontier-cs-2"
BENCHMARK_NAME = "Frontier-CS 2"
CHECKOUT_DIRNAME = "frontier-cs-2"
PROBLEMS_SUBDIR = Path("tasks/frontier-cs-2.0-demo/problems")
DEFAULT_TASK = "binary_fuse_filter"
PRIMARY_METRIC = "score_unbounded"
DIRECTION = "maximize"
VERIFY_ENTRYPOINT = "/opt/frontier/verify.py"
REFERENCE_ENTRYPOINT = "/opt/frontier/verify_reference.py"
TAR_TOOLCHAIN_IMAGE = "ubuntu:24.04"
MAX_PATCH_BYTES = 524_288
MAX_SUBMISSION_BYTES = 1_048_576
VERIFY_TIMEOUT_SECONDS = 900


class FrontierCS2Error(RuntimeError):
    """Raised for adapter-contract violations (missing files, bad payloads)."""


def checkout_root(explicit: str | None = None) -> Path:
    return Path(explicit).resolve() if explicit else ROOT / "third_party" / CHECKOUT_DIRNAME


def discover_tasks(checkout: Path) -> list[str]:
    problems = checkout / PROBLEMS_SUBDIR
    if not problems.is_dir():
        raise FrontierCS2Error(f"missing problems tree: {problems}")
    return sorted(
        f"{category.name}/{task.name}"
        for category in problems.iterdir()
        if category.is_dir()
        for task in category.iterdir()
        if task.is_dir() and (task / "policy.json").is_file()
    )


def resolve_task_dir(checkout: Path, task_id: str) -> Path:
    """Accept either ``Category/task`` or a bare ``task`` id."""
    if "/" in task_id:
        candidate = checkout / PROBLEMS_SUBDIR / task_id
        if not candidate.is_dir():
            raise FrontierCS2Error(f"unknown task: {task_id}")
        return candidate
    matches = [
        path
        for path in (checkout / PROBLEMS_SUBDIR).glob(f"*/{task_id}")
        if path.is_dir()
    ]
    if not matches:
        raise FrontierCS2Error(f"unknown task: {task_id}")
    if len(matches) > 1:
        raise FrontierCS2Error(
            f"ambiguous task id {task_id}: {[str(m) for m in matches]}"
        )
    return matches[0]


def image_tag(task_id: str) -> str:
    return f"bench-goal-plus/fcs2-{task_id.replace('/', '-')}:local"


def _run(command: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess:
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise FrontierCS2Error(
            f"command failed ({completed.returncode}): {' '.join(command[:6])}..."
            f"\nstderr: {completed.stderr[-2000:]}"
        )
    return completed


def build_verifier_image(task_dir: Path, tag: str) -> str:
    dockerfile = task_dir / "verifier" / "Dockerfile"
    if not dockerfile.is_file():
        raise FrontierCS2Error(f"missing verifier/Dockerfile under {task_dir}")
    _run(["docker", "build", "-f", str(dockerfile), "-t", tag, str(task_dir)])
    return tag


def _git(workspace: Path, *command: str) -> None:
    subprocess.run(
        ["git", *command], cwd=workspace, check=True, capture_output=True, text=True
    )


def prepare_workspace(task_dir: Path, dest: Path) -> Path:
    """Mirror the task's prepare_workspace.sh: copy repo, baseline git commit."""
    repo_source = task_dir / "agent-base" / "frontier" / "task" / "repo"
    if not repo_source.is_dir():
        raise FrontierCS2Error(f"missing agent-base repo tree under {task_dir}")
    if dest.exists():
        raise FrontierCS2Error(f"workspace already exists (no auto-delete): {dest}")
    shutil.copytree(repo_source, dest)
    for path in dest.rglob("*"):
        if path.is_file():
            path.chmod(path.stat().st_mode | 0o200)
    readme = task_dir / "agent-base" / "frontier" / "task" / "README.md"
    if readme.is_file():
        shutil.copyfile(readme, dest / "TASK.md")
    _git(dest, "init", "-q")
    _git(dest, "add", ".")
    _git(
        dest,
        "-c", "user.name=solver", "-c", "user.email=solver@localhost",
        "commit", "-qm", "baseline",
    )
    return dest


def make_submission(workspace: Path, run_dir: Path, edit_paths: str = "include") -> Path:
    """Reproduce make_submission.sh: git diff -> payload/solution.patch -> tar.

    The deterministic GNU tar flags come from the official script; on hosts
    without GNU tar they run inside the official ubuntu:24.04 toolchain.
    """
    _git(workspace, "add", "-N", edit_paths)
    diff = subprocess.run(
        [
            "git", "diff", "--no-ext-diff", "--no-textconv",
            "--src-prefix=a/", "--dst-prefix=b/", "HEAD", "--", edit_paths,
        ],
        cwd=workspace, check=True, capture_output=True, text=True,
    ).stdout
    if not diff.strip():
        raise FrontierCS2Error(f"empty patch for paths '{edit_paths}'")
    payload_dir = run_dir / "payload"
    payload_dir.mkdir(parents=True, exist_ok=True)
    patch_path = payload_dir / "solution.patch"
    patch_path.write_text(diff)
    if patch_path.stat().st_size > MAX_PATCH_BYTES:
        raise FrontierCS2Error(
            f"patch exceeds {MAX_PATCH_BYTES} bytes ({patch_path.stat().st_size})"
        )
    tar_path = run_dir / "answer.tar"
    tar_flags = [
        "--format=ustar", "--sort=name", "--mtime=@0",
        "--owner=0", "--group=0", "--numeric-owner",
    ]
    local_tar = shutil.which("gtar") or shutil.which("tar")
    if local_tar:
        probe = subprocess.run([local_tar, "--version"], capture_output=True, text=True)
        if "GNU tar" in probe.stdout + probe.stderr:
            _run([
                local_tar, *tar_flags,
                "-cf", str(tar_path), "-C", str(run_dir), "payload/solution.patch",
            ])
            return _checked_submission(tar_path)
    _run([
        "docker", "run", "--rm", "-v", f"{run_dir}:/work", TAR_TOOLCHAIN_IMAGE,
        "tar", *tar_flags,
        "-cf", "/work/answer.tar", "-C", "/work", "payload/solution.patch",
    ])
    return _checked_submission(tar_path)


def _checked_submission(tar_path: Path) -> Path:
    if tar_path.stat().st_size > MAX_SUBMISSION_BYTES:
        raise FrontierCS2Error(
            f"submission exceeds {MAX_SUBMISSION_BYTES} bytes ({tar_path.stat().st_size})"
        )
    return tar_path


def run_verify(
    image: str,
    run_dir: Path,
    mode: str,
    *,
    dataset: Path,
    reference_only: bool = False,
    timeout: int = VERIFY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run the official verifier container for train/eval and parse its result."""
    if mode not in ("train", "eval"):
        raise FrontierCS2Error(f"mode must be train or eval, got {mode!r}")
    result_path = run_dir / f"result-{mode}.json"
    evidence_path = run_dir / f"evidence-{mode}.tar.zst"
    _run([
        "docker", "run", "--rm",
        "-v", f"{run_dir}:/work",
        "-e", "FRONTIER_PAYLOAD_PATH=/work/answer.tar",
        "-e", f"FRONTIER_DATASET_PATH=/work/{dataset.name}",
        "-e", f"FRONTIER_VERIFICATION_MODE={mode}",
        "-e", f"FRONTIER_RESULT_PATH=/work/{result_path.name}",
        "-e", f"FRONTIER_EVIDENCE_PATH=/work/{evidence_path.name}",
        "--entrypoint", "python3", image,
        REFERENCE_ENTRYPOINT if reference_only else VERIFY_ENTRYPOINT,
    ], timeout=timeout)
    if not result_path.is_file():
        raise FrontierCS2Error(f"verifier produced no result file: {result_path}")
    return parse_result(json.loads(result_path.read_text()))


def parse_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Project the verifier result JSON onto the shared summary fields."""
    metrics = payload.get("metrics", {}) or {}
    private = payload.get("private_metrics", {}) or {}
    return {
        "status": payload.get("status"),
        "summary": payload.get("summary"),
        "raw_score": payload.get("raw_score"),
        PRIMARY_METRIC: metrics.get(PRIMARY_METRIC),
        "beats_reference": private.get("beats_reference"),
        "margin": private.get("margin"),
        "reference_score": private.get("reference_score"),
        "beat_spec_progress": private.get("beat_spec_progress")
        or metrics.get("beat_spec_progress"),
    }


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description=f"{BENCHMARK_NAME} task-level verifier boundary"
    )
    parser.add_argument("--checkout", default=None, help="explicit FrontierCS-2 checkout path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("discover", help="list Category/task ids in the checkout")

    build = sub.add_parser("build", help="build the official verifier image")
    build.add_argument("--task", default=DEFAULT_TASK)

    workspace = sub.add_parser("workspace", help="materialize a solver workspace")
    workspace.add_argument("--task", default=DEFAULT_TASK)
    workspace.add_argument("--dest", default=None)

    submit = sub.add_parser("submit", help="diff workspace into answer.tar")
    submit.add_argument("--workspace", required=True)
    submit.add_argument("--run-dir", required=True)

    verify = sub.add_parser("verify", help="run official verifier (train/eval)")
    verify.add_argument("--task", default=DEFAULT_TASK)
    verify.add_argument("--image", default=None)
    verify.add_argument("--run-dir", required=True)
    verify.add_argument("--mode", choices=["train", "eval"], required=True)
    verify.add_argument("--reference-only", action="store_true")

    args = parser.parse_args()
    checkout = checkout_root(args.checkout)

    if args.command == "discover":
        for task_id in discover_tasks(checkout):
            print(task_id)
        return 0
    if args.command == "build":
        print(build_verifier_image(resolve_task_dir(checkout, args.task), image_tag(args.task)))
        return 0
    if args.command == "workspace":
        dest = (
            Path(args.dest).resolve()
            if args.dest
            else make_preserved_temp_directory(
                prefix=f"fcs2-{args.task.replace('/', '-')}-", namespace=BENCHMARK_ID
            )
        )
        print(prepare_workspace(resolve_task_dir(checkout, args.task), dest))
        return 0
    if args.command == "submit":
        run_dir = Path(args.run_dir).resolve()
        print(make_submission(Path(args.workspace).resolve(), run_dir))
        return 0
    if args.command == "verify":
        run_dir = Path(args.run_dir).resolve()
        task = resolve_task_dir(checkout, args.task)
        image = args.image or image_tag(args.task)
        dataset = task / "datasets" / f"{args.mode}.json"
        staged = run_dir / dataset.name
        if not staged.is_file():
            shutil.copyfile(dataset, staged)
        result = run_verify(
            image, run_dir, args.mode,
            dataset=staged, reference_only=args.reference_only,
        )
        write_json(run_dir / f"summary-{args.mode}.json", result)
        print(json.dumps(result, indent=2))
        return 0
    raise FrontierCS2Error(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(_cli())
