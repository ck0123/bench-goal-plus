"""CLI boundary for the native SWE-bench Verified lifecycle."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from bench_runtime_paths import configure_temp_environment

from .config import ROOT, SUPPORTED_METHODS, campaign_dir, load_profile, resolve_profile, utc_now, write_json
from .environment import doctor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    children = parser.add_subparsers(dest="command", required=True)

    doctor_parser = children.add_parser("doctor")
    doctor_parser.add_argument("--profile", required=True)
    doctor_parser.add_argument("--output", type=Path)
    doctor_parser.add_argument(
        "--method", action="append", choices=sorted(SUPPORTED_METHODS)
    )
    doctor_parser.add_argument("--model")
    doctor_parser.add_argument("--local-assets-only", action="store_true")
    doctor_parser.add_argument("--allow-missing-local-assets", action="store_true")

    prepare_parser = children.add_parser("prepare")
    prepare_parser.add_argument("--profile", required=True)
    prepare_parser.add_argument("--campaign-id", required=True)
    prepare_parser.add_argument(
        "--method", action="append", choices=sorted(SUPPORTED_METHODS)
    )
    prepare_parser.add_argument("--model")
    prepare_parser.add_argument("--reasoning-effort")
    prepare_parser.add_argument("--wall-time-seconds", type=int)
    prepare_parser.add_argument("--concurrency", type=int)
    prepare_parser.add_argument("--cell-concurrency", type=int)
    prepare_parser.add_argument("--seed", type=int, default=1)
    prepare_parser.add_argument("--retain-containers", action="store_true")

    for command in ("run", "status", "finalize"):
        child = children.add_parser(command)
        child.add_argument("--campaign", required=True)
        if command == "run":
            child.add_argument("--detach", action="store_true")
        if command == "status":
            child.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_temp_environment()
    args = build_parser().parse_args(argv)
    if args.command in {"doctor", "prepare"}:
        _, profile = load_profile(args.profile)
        resolved = resolve_profile(
            profile,
            methods=args.method,
            model=args.model,
            reasoning_effort=(
                args.reasoning_effort if args.command == "prepare" else None
            ),
            wall_time_seconds=(
                args.wall_time_seconds if args.command == "prepare" else None
            ),
            concurrency=args.concurrency if args.command == "prepare" else None,
            cell_concurrency=(
                args.cell_concurrency if args.command == "prepare" else None
            ),
            seed=args.seed if args.command == "prepare" else None,
            retain_containers=(
                args.retain_containers if args.command == "prepare" else None
            ),
        )
        if args.command == "doctor":
            return doctor(
                resolved,
                output=args.output,
                local_assets_only=args.local_assets_only,
                allow_missing_local_assets=args.allow_missing_local_assets,
            )
        from .runtime import prepare

        prepare(args.campaign_id, resolved)
        return 0

    destination = campaign_dir(args.campaign)
    if args.command == "run":
        if args.detach:
            controller_path = destination / "controller.json"
            stdout_path = destination / "controller.stdout.txt"
            stderr_path = destination / "controller.stderr.txt"
            command = [
                sys.executable,
                str(Path(__file__).with_name("experiment.py")),
                "run",
                "--campaign",
                args.campaign,
            ]
            with stdout_path.open("a", encoding="utf-8") as stdout, stderr_path.open(
                "a", encoding="utf-8"
            ) as stderr:
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=dict(os.environ),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
            write_json(
                controller_path,
                {
                    "pid": process.pid,
                    "state": "running",
                    "started_at": utc_now(),
                    "stdout_file": stdout_path.name,
                    "stderr_file": stderr_path.name,
                },
            )
            print(json.dumps({"campaign": str(destination), "pid": process.pid}, indent=2))
            return 0
        from .runtime import execute_campaign

        returncode = execute_campaign(destination)
        controller_path = destination / "controller.json"
        if controller_path.is_file():
            controller = json.loads(controller_path.read_text(encoding="utf-8"))
            if controller.get("pid") == os.getpid():
                controller.update(
                    {
                        "state": "completed" if returncode == 0 else "failed",
                        "completed_at": utc_now(),
                        "returncode": returncode,
                    }
                )
                write_json(controller_path, controller)
        return returncode
    if args.command == "status":
        from .runtime import status_payload

        payload = status_payload(destination)
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(
                f"{payload['campaign_id']}: {payload['state']} "
                f"cells={payload['counts']}"
            )
        return 0
    if args.command == "finalize":
        from .reporting import finalize_campaign

        payload = finalize_campaign(destination)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    raise AssertionError(args.command)
