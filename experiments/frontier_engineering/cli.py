"""CLI boundary for the native Frontier-Engineering lifecycle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bench_runtime_paths import configure_temp_environment

from .config import SUPPORTED_METHODS, campaign_dir, load_profile, pi_provider_config, resolve_profile
from .environment import doctor, provision, runtime_source


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    children = parser.add_subparsers(dest="command", required=True)

    provision_parser = children.add_parser("provision")
    provision_parser.add_argument("--profile", required=True)

    source_parser = children.add_parser("runtime-source")
    source_parser.add_argument("--profile", required=True)
    source_parser.add_argument("--method", action="append", choices=sorted(SUPPORTED_METHODS))

    doctor_parser = children.add_parser("doctor")
    doctor_parser.add_argument("--profile", required=True)
    doctor_parser.add_argument("--output", type=Path)
    doctor_parser.add_argument("--method", action="append", choices=sorted(SUPPORTED_METHODS))
    doctor_parser.add_argument("--model")
    doctor_parser.add_argument("--reasoning-effort")
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
    if args.command == "runtime-source":
        _, profile = load_profile(args.profile)
        resolved = resolve_profile(profile, methods=args.method)
        print(json.dumps({
            "schema_version": 1,
            "runtime_sources": {"goal_plus": runtime_source()},
            "runtime_configuration": {"pi_provider": pi_provider_config(resolved),
                                      "goal_plus_spawn_mode": resolved.get("goal_plus_spawn_mode", "fresh")},
        }))
        return 0
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
            reasoning_effort=args.reasoning_effort,
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
            raise ValueError("frontier-engineering does not own retainable containers")
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
