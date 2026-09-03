"""CLI boundary for the aibench coding native lifecycle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bench_runtime_paths import configure_temp_environment

from .config import SUPPORTED_METHODS, campaign_dir, load_profile, resolve_profile


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    children = parser.add_subparsers(dest="command", required=True)

    provision = children.add_parser("provision")
    provision.add_argument("--profile", required=True)

    doctor = children.add_parser("doctor")
    doctor.add_argument("--profile", required=True)
    doctor.add_argument("--output", type=Path)
    doctor.add_argument("--method", action="append", choices=sorted(SUPPORTED_METHODS))
    doctor.add_argument("--model")
    doctor.add_argument("--reasoning-effort")
    doctor.add_argument("--local-assets-only", action="store_true")
    doctor.add_argument("--allow-missing-local-assets", action="store_true")

    prepare = children.add_parser("prepare")
    prepare.add_argument("--profile", required=True)
    prepare.add_argument("--campaign-id", required=True)
    prepare.add_argument("--method", action="append", choices=sorted(SUPPORTED_METHODS))
    prepare.add_argument("--seeds", nargs="+", type=int)
    prepare.add_argument("--model")
    prepare.add_argument("--reasoning-effort")
    prepare.add_argument("--wall-time-seconds", type=int)
    prepare.add_argument("--concurrency", type=int)
    prepare.add_argument("--cell-concurrency", type=int)
    prepare.add_argument("--retain-containers", action="store_true")

    run = children.add_parser("run")
    run.add_argument("--campaign", required=True)
    run.add_argument("--detach", action="store_true")

    status = children.add_parser("status")
    status.add_argument("--campaign", required=True)
    status.add_argument("--json", action="store_true")

    finalize = children.add_parser("finalize")
    finalize.add_argument("--campaign", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_temp_environment()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {"provision", "doctor", "prepare"}:
        profile_path, profile = load_profile(args.profile)
        if args.command == "provision":
            from .runtime import provision

            print(json.dumps(provision(profile), indent=2, ensure_ascii=False))
            return 0
        resolved = resolve_profile(
            profile,
            methods=args.method,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            wall_time_seconds=(args.wall_time_seconds if args.command == "prepare" else None),
            concurrency=(args.concurrency if args.command == "prepare" else None),
            cell_concurrency=(args.cell_concurrency if args.command == "prepare" else None),
            seeds=(args.seeds if args.command == "prepare" else None),
        )
        if args.command == "doctor":
            from .runtime import doctor

            return doctor(
                resolved,
                output=args.output,
                local_assets_only=args.local_assets_only,
                allow_missing_local_assets=args.allow_missing_local_assets,
            )
        if args.retain_containers:
            parser.error("aibench-coding does not own retainable containers")
        from .runtime import prepare

        destination = prepare(args.campaign_id, resolved, profile_path)
        print(destination)
        return 0

    destination = campaign_dir(args.campaign)
    if args.command == "run":
        if args.detach:
            parser.error("aibench-coding runs in the foreground")
        from .runtime import execute_campaign

        return execute_campaign(destination)
    if args.command == "status":
        from .runtime import status_payload

        payload = status_payload(destination)
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"{payload['campaign_id']}: {payload['state']} cells={payload['counts']}")
        return 0
    if args.command == "finalize":
        from .reporting import finalize_campaign

        print(json.dumps(finalize_campaign(destination), indent=2, ensure_ascii=False))
        return 0
    raise AssertionError(args.command)
