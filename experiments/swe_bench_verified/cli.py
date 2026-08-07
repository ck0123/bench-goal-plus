"""CLI boundary for the native SWE-bench Verified lifecycle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bench_runtime_paths import configure_temp_environment

from .config import SUPPORTED_METHODS, campaign_dir, load_profile, resolve_profile
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
            parser = build_parser()
            parser.error("swe-bench-native does not support detached execution")
        from .runtime import execute_campaign

        return execute_campaign(destination)
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
