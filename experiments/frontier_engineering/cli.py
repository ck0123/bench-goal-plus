"""CLI boundary for the native Frontier-Engineering lifecycle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bench_runtime_paths import configure_temp_environment
from bench_goal_plus.search_scheduler import (
    add_internal_search_scheduler_argument,
    search_scheduler_from_namespace,
)

from .config import SUPPORTED_METHODS, campaign_dir, load_profile, resolve_profile
from .environment import doctor, provision


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    children = parser.add_subparsers(dest="command", required=True)

    provision_parser = children.add_parser("provision")
    provision_parser.add_argument("--profile", required=True)

    doctor_parser = children.add_parser("doctor")
    doctor_parser.add_argument("--profile", required=True)
    doctor_parser.add_argument("--output", type=Path)
    doctor_parser.add_argument("--method", action="append", choices=sorted(SUPPORTED_METHODS))
    doctor_parser.add_argument("--model")
    doctor_parser.add_argument("--local-assets-only", action="store_true")
    doctor_parser.add_argument("--allow-missing-local-assets", action="store_true")

    prepare_parser = children.add_parser("prepare")
    prepare_parser.add_argument("--profile", required=True)
    prepare_parser.add_argument("--campaign-id", required=True)
    prepare_parser.add_argument("--method", action="append", choices=sorted(SUPPORTED_METHODS))
    prepare_parser.add_argument("--seeds", nargs="+", type=int)
    prepare_parser.add_argument("--model")
    prepare_parser.add_argument("--reasoning-effort")
    prepare_parser.add_argument("--wall-time-seconds", type=int)
    prepare_parser.add_argument("--concurrency", type=int)
    prepare_parser.add_argument("--cell-concurrency", type=int)
    prepare_parser.add_argument("--retain-containers", action="store_true")
    add_internal_search_scheduler_argument(prepare_parser)

    run_parser = children.add_parser("run")
    run_parser.add_argument("--campaign", required=True)
    run_parser.add_argument("--detach", action="store_true")
    run_parser.add_argument("--controller-child", action="store_true", help=argparse.SUPPRESS)

    status_parser = children.add_parser("status")
    status_parser.add_argument("--campaign", required=True)
    status_parser.add_argument("--json", action="store_true")

    stop_parser = children.add_parser("stop")
    stop_parser.add_argument("--campaign", required=True)

    finalize_parser = children.add_parser("finalize")
    finalize_parser.add_argument("--campaign", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_temp_environment()
    args = build_parser().parse_args(argv)
    if args.command in {"provision", "doctor", "prepare"}:
        profile_path, profile = load_profile(args.profile)
        if args.command == "provision":
            print(json.dumps(provision(profile), indent=2))
            return 0
        resolved = resolve_profile(
            profile,
            methods=args.method,
            seeds=(args.seeds if args.command == "prepare" else None),
            model=args.model,
            reasoning_effort=(args.reasoning_effort if args.command == "prepare" else None),
            wall_time_seconds=(args.wall_time_seconds if args.command == "prepare" else None),
            concurrency=(args.concurrency if args.command == "prepare" else None),
            cell_concurrency=(args.cell_concurrency if args.command == "prepare" else None),
        )
        if args.command == "doctor":
            return doctor(
                resolved,
                output=args.output,
                local_assets_only=args.local_assets_only,
                allow_missing_local_assets=args.allow_missing_local_assets,
            )
        if args.retain_containers:
            parser.error("frontier-engineering does not own retainable containers")
        search_scheduler = search_scheduler_from_namespace(args)
        if search_scheduler is not None:
            search_scheduler.validate_max_candidates(resolved["concurrency"])
            resolved["search_scheduler"] = search_scheduler.as_dict()
        from .runtime import prepare

        destination = prepare(args.campaign_id, resolved, profile_path)
        print(destination)
        return 0

    destination = campaign_dir(args.campaign)
    if args.command == "run":
        from .runtime import execute_campaign, launch_detached

        if args.detach and not args.controller_child:
            return launch_detached(destination)
        return execute_campaign(destination)
    if args.command == "status":
        from .runtime import status_payload

        payload = status_payload(destination)
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"{payload['campaign_id']}: {payload['state']} cells={payload['counts']}")
        return 0
    if args.command == "stop":
        from .runtime import stop_campaign

        return stop_campaign(destination)
    if args.command == "finalize":
        from .reporting import finalize_campaign

        print(json.dumps(finalize_campaign(destination), indent=2, ensure_ascii=False))
        return 0
    raise AssertionError(args.command)
