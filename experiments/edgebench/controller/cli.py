"""Command-line interface for the native EdgeBench controller."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bench_runtime_paths import configure_temp_environment
from bench_goal_plus.search_scheduler import (
    add_internal_search_scheduler_argument,
    search_scheduler_from_namespace,
)

from .environment import doctor, provision, resolve_goal_plus_source
from .io import campaign_dir
from .preparation import prepare
from .profiles import (
    GOAL_PLUS_METHODS,
    METHODS,
    api_protocol_for_methods,
    load_profile,
    validate_pi_provider_model,
)
from .reporting import finalize_campaign
from .runtime import execute_campaign, launch, print_status, stop_campaign


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("provision", "doctor"):
        child = subparsers.add_parser(name)
        child.add_argument("--profile", default="vliw-smoke")
        if name == "doctor":
            child.add_argument("--output", type=Path)
            child.add_argument("--method", action="append", choices=sorted(METHODS))
            child.add_argument("--model")
            child.add_argument("--reasoning-effort")
            child.add_argument("--local-assets-only", action="store_true")
            child.add_argument(
                "--allow-missing-local-assets", action="store_true"
            )
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--profile", default="vliw-smoke")
    prepare_parser.add_argument("--campaign-id")
    prepare_parser.add_argument("--method", action="append", choices=sorted(METHODS))
    prepare_parser.add_argument("--model")
    prepare_parser.add_argument("--reasoning-effort")
    prepare_parser.add_argument("--wall-time-seconds", type=int)
    prepare_parser.add_argument("--concurrency", type=int)
    prepare_parser.add_argument("--cell-concurrency", type=int)
    add_internal_search_scheduler_argument(prepare_parser)
    metadata_parser = subparsers.add_parser("plan-metadata")
    metadata_parser.add_argument("--profile", default="vliw-smoke")
    metadata_parser.add_argument("--method", action="append", choices=sorted(METHODS))
    add_internal_search_scheduler_argument(metadata_parser)
    for name in ("run", "status", "stop", "finalize", "_execute"):
        child = subparsers.add_parser(name)
        child.add_argument("--campaign", required=True)
        if name == "run":
            child.add_argument("--detach", action="store_true")
        elif name == "status":
            child.add_argument("--json", action="store_true")
        elif name == "stop":
            child.add_argument("--wait-seconds", type=int, default=60)
        elif name == "finalize":
            child.add_argument("--local-fast-reference", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_temp_environment()
    args = build_parser().parse_args(argv)
    if args.command in {"provision", "doctor", "prepare", "plan-metadata"}:
        _, profile = load_profile(args.profile)
        search_scheduler = search_scheduler_from_namespace(args)
        if search_scheduler is not None:
            selected_concurrency = int(
                getattr(args, "concurrency", None) or profile["concurrency"]
            )
            search_scheduler.validate_max_candidates(selected_concurrency)
            profile = {**profile, "search_scheduler": search_scheduler.as_dict()}
        if args.command == "plan-metadata":
            if args.method:
                profile = {**profile, "methods": args.method}
            source = resolve_goal_plus_source(methods=profile["methods"])
            if not source["valid"]:
                raise RuntimeError(
                    f"Goal Plus source validation failed: {source['error']}"
                )
            visible_source = {
                key: source.get(key)
                for key in (
                    "source_kind",
                    "source_path",
                    "expected_ref",
                    "branch",
                    "commit",
                )
            }
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "runtime_sources": {"goal_plus": visible_source},
                        "runtime_configuration": {
                            "goal_plus": {
                                "shared_dir_enabled": bool(
                                    profile.get("shared_dir_enabled", False)
                                ),
                                "supplemental_evaluation_enabled": bool(
                                    profile.get(
                                        "supplemental_evaluation_enabled", False
                                    )
                                ),
                                **(
                                    {
                                        "search_scheduler": (
                                            search_scheduler.as_dict()
                                        )
                                    }
                                    if search_scheduler is not None
                                    else {}
                                ),
                            }
                        }
                        if set(profile["methods"]) & GOAL_PLUS_METHODS
                        else {},
                    },
                    indent=2,
                )
            )
            return 0
        if args.command == "provision":
            return provision(profile)
        if args.command == "doctor":
            profile = dict(profile)
            if args.method:
                profile["methods"] = args.method
            if args.model:
                profile["model"] = args.model
            if args.reasoning_effort:
                profile["reasoning_effort"] = args.reasoning_effort
            protocol = api_protocol_for_methods(profile["methods"])
            if protocol == "pi-provider":
                validate_pi_provider_model(profile["model"])
            return doctor(
                profile,
                output=args.output,
                local_assets_only=args.local_assets_only,
                allow_missing_local_assets=args.allow_missing_local_assets,
            )
        prepare(args, profile)
        return 0
    destination = campaign_dir(args.campaign)
    if args.command == "run":
        return launch(destination, detach=args.detach)
    if args.command == "_execute":
        return execute_campaign(destination)
    if args.command == "status":
        return print_status(destination, as_json=args.json)
    if args.command == "stop":
        return stop_campaign(destination, wait_seconds=args.wait_seconds)
    if args.command == "finalize":
        payload = finalize_campaign(destination, args.local_fast_reference)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    raise AssertionError(args.command)
