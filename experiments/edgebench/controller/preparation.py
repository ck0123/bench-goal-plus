"""Resolve an EdgeBench profile into immutable campaign and cell manifests."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from bench_goal_plus.goal_plus_command import (
    goal_plus_command_config,
    goal_plus_entrypoint,
)
from bench_goal_plus.search_scheduler import search_scheduler_from_json

from . import io
from .context import current_paths
from .environment import (
    codex_provider_contract,
    pi_provider_bundle_contract,
    require_api_only_network,
    resolve_goal_plus_source,
    resolve_pi_provider_bundle,
    task_config,
)
from .profiles import (
    ALLOWED_PROTOCOL_OVERRIDE_FIELDS,
    GOAL_PLUS_METHODS,
    METHODS,
    OFFICIAL_SCHEDULED_RUNS,
    api_protocol_for_methods,
    load_official_codex_protocol,
    official_task_protocol,
    methods_require_codex,
    profile_task_protocol,
    protocol_diff,
    validate_claude_thinking_contract,
    validate_pi_provider_model,
)


def _goal_plus_command_config(
    *,
    method: str,
    model: str,
    role_config: dict[str, Any],
    profile: dict[str, Any],
    concurrency: int,
) -> dict[str, str | int]:
    worker_model = str(role_config.get("worker_model") or model)
    if method == "goal-plus-pi" and "/" not in worker_model:
        worker_model = f"openai-codex/{worker_model}"
    annotator_model = str(role_config.get("evidence_annotator_model") or model)
    if (
        method == "goal-plus-pi-provider"
        and profile.get("evidence_annotator_model") is None
    ):
        annotator_model = str(model).partition("/")[2]
    return goal_plus_command_config(
        max_parallel=concurrency,
        strategy="agent_guided",
        worker_model=worker_model,
        annotator_model=annotator_model,
        workspace_backend="git_worktree",
        promotion_mode="artifact_only",
    )


def _resolved_override_reasons(
    *,
    method: str,
    method_config: dict[str, Any],
    model: str,
    reasoning: str | None,
    backend: str,
    wall_time: int,
    concurrency: int,
    cell_concurrency: int,
    judge_concurrency: int,
    eval_interval: int,
    internet: bool,
    internet_source: str,
) -> dict[str, str]:
    if method_config["inner_search"]:
        attempts_reason = (
            "R=1 uses one outer Goal Plus trajectory and maps "
            f"K={concurrency} to internal workers"
        )
    else:
        attempts_reason = (
            f"K={concurrency} runs {concurrency} isolated outer Agent trajectories"
        )
    return {
        "agent": f"method={method} selects SForge agent={method_config['agent']}",
        "attempts_per_task": attempts_reason,
        "backend": f"resolved campaign backend={backend}",
        "cell_concurrency": (f"C={cell_concurrency} controls concurrent task cells"),
        "judge_concurrency": (
            f"judge_concurrency={judge_concurrency} caps official Judge execution"
        ),
        "model": f"resolved campaign model={model}",
        "reasoning_effort": f"resolved reasoning_effort={reasoning}",
        "timeout": f"T={wall_time} seconds per task trajectory or Search run",
        "eval_interval": (
            f"effective hidden-Judge eval_interval={eval_interval} seconds"
        ),
        "internet": (
            f"effective internet={str(internet).lower()} from {internet_source}"
        ),
    }


def prepare(args: argparse.Namespace, profile: dict[str, Any]) -> Path:
    paths = current_paths()
    official_protocol = load_official_codex_protocol()
    methods = args.method or list(profile["methods"])
    unknown = set(methods) - set(METHODS)
    if unknown:
        raise ValueError("unknown EdgeBench method(s): " + ", ".join(sorted(unknown)))
    api_protocol = api_protocol_for_methods(methods)
    wall_time = int(args.wall_time_seconds or profile["wall_time_seconds"])
    concurrency = int(args.concurrency or profile["concurrency"])
    search_scheduler = search_scheduler_from_json(profile.get("search_scheduler"))
    cell_concurrency = int(
        getattr(args, "cell_concurrency", None) or profile.get("cell_concurrency", 1)
    )
    model = args.model or profile["model"]
    if api_protocol == "pi-provider":
        validate_pi_provider_model(model)
    requested_reasoning = getattr(args, "reasoning_effort", None)
    if requested_reasoning is not None:
        reasoning = requested_reasoning
    elif "reasoning_effort" in profile:
        reasoning = profile["reasoning_effort"]
    elif api_protocol == "anthropic":
        reasoning = None
    else:
        reasoning = "high"
    thinking = profile.get("thinking") if api_protocol == "anthropic" else None
    if api_protocol == "anthropic":
        validate_claude_thinking_contract(thinking, reasoning)
    backend = str(profile.get("backend") or "docker")
    judge_concurrency = int(profile.get("judge_concurrency", 1))
    worker_runtime = min(
        wall_time,
        int(profile.get("worker_runtime_seconds", wall_time)),
    )
    worker_min_runtime = int(profile.get("worker_min_runtime_seconds", 0))
    worker_min_verifiers = int(profile.get("worker_min_verifier_runs", 0))
    closeout_reserve = int(profile.get("closeout_reserve_seconds", 0))
    goal_plus_verifier_timeout = int(
        profile.get("goal_plus_verifier_timeout_seconds", 120)
    )
    has_goal_plus = bool(set(methods) & GOAL_PLUS_METHODS)
    global_evidence_config = (
        {"global_evidence_mode": str(profile.get("global_evidence_mode", "manual"))}
        if has_goal_plus
        else {}
    )
    goal_plus_feature_config = (
        {
            "shared_dir_enabled": bool(profile.get("shared_dir_enabled", False)),
            "supplemental_evaluation_enabled": bool(
                profile.get("supplemental_evaluation_enabled", False)
            ),
        }
        if has_goal_plus
        else {}
    )
    role_fields = (
        "worker_model",
        "worker_reasoning_effort",
        "evidence_annotator_model",
        "evidence_annotator_reasoning_effort",
        "evidence_annotator_timeout_seconds",
    )
    role_config: dict[str, Any] = {}
    if api_protocol == "pi-provider" and any(field in profile for field in role_fields):
        role_config = {
            "worker_model": str(profile.get("worker_model") or model),
            "worker_reasoning_effort": str(
                profile.get("worker_reasoning_effort") or reasoning or "high"
            ),
            "evidence_annotator_model": str(
                profile.get("evidence_annotator_model") or model
            ),
            "evidence_annotator_reasoning_effort": str(
                profile.get("evidence_annotator_reasoning_effort")
                or reasoning
                or "high"
            ),
            "evidence_annotator_timeout_seconds": int(
                profile.get("evidence_annotator_timeout_seconds", 1800)
            ),
        }
    resolved_execution_profile = {
        **profile,
        "methods": methods,
        "model": model,
        "reasoning_effort": reasoning,
        **role_config,
    }
    pi_provider_contract: dict[str, Any] | None = None
    if api_protocol == "pi-provider":
        bundle = resolve_pi_provider_bundle(
            [
                str(resolved_execution_profile["model"]),
                str(
                    resolved_execution_profile.get("worker_model")
                    or resolved_execution_profile["model"]
                ),
                str(
                    resolved_execution_profile.get("evidence_annotator_model")
                    or resolved_execution_profile["model"]
                ),
            ]
        )
        if not bundle["valid"]:
            raise ValueError(f"Pi provider resolution failed: {bundle['error']}")
        pi_provider_contract = pi_provider_bundle_contract(bundle)

    codex_contract: dict[str, Any] | None = None
    if api_protocol == "openai" and methods_require_codex(methods):
        codex_contract = codex_provider_contract(resolved_execution_profile)
        if not codex_contract["valid"]:
            raise ValueError(
                f"Codex provider resolution failed: {codex_contract['error']}"
            )

    goal_plus_source: dict[str, Any] | None = None
    if has_goal_plus:
        goal_plus_source = resolve_goal_plus_source(methods=methods)
        if not goal_plus_source["valid"]:
            raise ValueError(
                f"Goal Plus source validation failed: {goal_plus_source['error']}"
            )
    profile_protocol_overrides = dict(profile.get("protocol_overrides") or {})
    allowed_protocol_override_fields = ALLOWED_PROTOCOL_OVERRIDE_FIELDS | set(
        profile_protocol_overrides
    )
    if wall_time < 1 or concurrency < 1 or cell_concurrency < 1:
        raise ValueError(
            "wall time, concurrency, and cell concurrency must be positive"
        )
    if worker_min_runtime and worker_min_runtime >= worker_runtime:
        raise ValueError(
            "worker minimum runtime must be less than the effective worker runtime"
        )
    if worker_runtime + closeout_reserve > wall_time:
        raise ValueError(
            "effective worker runtime and closeout reserve must fit within wall time"
        )

    resolved_network_profile = {
        **profile,
        "methods": methods,
        "model": model,
        **role_config,
    }
    require_api_only_network(resolved_network_profile, official_protocol)

    campaign_id = io.sanitize_id(
        args.campaign_id or f"{profile['id']}-{io.campaign_stamp()}"
    )
    destination = paths.runs_root / campaign_id
    if destination.exists():
        raise FileExistsError(
            f"campaign already exists and will not be overwritten: {destination}"
        )
    destination.mkdir(parents=True)

    cells: list[dict[str, Any]] = []
    for task_id in profile["task_ids"]:
        config = task_config(task_id)
        official_effective = official_task_protocol(official_protocol, task_id, config)
        profile_effective = profile_task_protocol(
            profile, official_protocol, task_id, config
        )
        official_contract = {
            **official_effective,
            "attempts_per_task": OFFICIAL_SCHEDULED_RUNS,
            "cell_concurrency": None,
            "judge_concurrency": None,
            "model": official_protocol["official_model"],
            "reasoning_effort": None,
        }
        prompt = str(config["work"]["agent_query"])
        for method in methods:
            method_config = METHODS[method]
            cell_id = io.sanitize_id(f"{task_id}--{method}")
            outer_replicas = (
                concurrency
                if method_config["outer_replicas"] == "concurrency"
                else int(method_config["outer_replicas"])
            )
            effective_contract = {
                **profile_effective,
                "agent": method_config["agent"],
                "attempts_per_task": outer_replicas,
                "backend": backend,
                "cell_concurrency": cell_concurrency,
                "judge_concurrency": judge_concurrency,
                "model": model,
                "reasoning_effort": reasoning,
                "timeout": wall_time,
            }
            for role_field in ("worker_model", "evidence_annotator_model"):
                if role_field in profile:
                    effective_contract[role_field] = role_config[role_field]
            internet_source = (
                f"profiles/{profile['id']}.protocol_overrides.internet"
                if "internet" in profile_protocol_overrides
                else f"tasks/{task_id}.json"
            )
            override_reasons = _resolved_override_reasons(
                method=method,
                method_config=method_config,
                model=model,
                reasoning=reasoning,
                backend=backend,
                wall_time=wall_time,
                concurrency=concurrency,
                cell_concurrency=cell_concurrency,
                judge_concurrency=judge_concurrency,
                eval_interval=int(effective_contract["eval_interval"]),
                internet=bool(effective_contract["internet"]),
                internet_source=internet_source,
            )
            for role_field in ("worker_model", "evidence_annotator_model"):
                if role_field in profile:
                    override_reasons[role_field] = (
                        f"resolved campaign {role_field}=" f"{role_config[role_field]}"
                    )
            differences = protocol_diff(
                official=official_contract,
                effective=effective_contract,
                reasons=override_reasons,
                allowed_fields=allowed_protocol_override_fields,
            )
            cell = {
                "schema_version": 1,
                "cell_id": cell_id,
                "task_id": task_id,
                "method": method,
                "sforge_agent": method_config["agent"],
                "api_protocol": method_config["api_protocol"],
                "backend": backend,
                "model": model,
                "reasoning_effort": reasoning,
                **role_config,
                **(global_evidence_config if method in GOAL_PLUS_METHODS else {}),
                **(
                    goal_plus_feature_config
                    if method in GOAL_PLUS_METHODS
                    else {}
                ),
                **(
                    {"goal_plus_source": goal_plus_source}
                    if method in GOAL_PLUS_METHODS
                    else {}
                ),
                **(
                    {
                        "goal_plus_config": {
                            "entrypoint": goal_plus_entrypoint(
                                "codex"
                                if method == "goal-plus-codex"
                                else "pi-rpc"
                            ),
                            "command_config": _goal_plus_command_config(
                                method=method,
                                model=model,
                                role_config=role_config,
                                profile=profile,
                                concurrency=concurrency,
                            ),
                            **(
                                {"search_scheduler": search_scheduler.as_dict()}
                                if search_scheduler is not None
                                else {}
                            ),
                        }
                    }
                    if method in GOAL_PLUS_METHODS
                    else {}
                ),
                "thinking": thinking,
                "claude_context_window_tokens": profile.get(
                    "claude_context_window_tokens"
                ),
                "claude_autocompact_percent": profile.get("claude_autocompact_percent"),
                "pi_package_version": profile.get("pi_package_version"),
                "wall_time_seconds": wall_time,
                "live_search_concurrency": concurrency,
                "outer_replicas": outer_replicas,
                "outer_replica_concurrency": concurrency if outer_replicas > 1 else 1,
                "inner_search_concurrency": (
                    concurrency if method_config["inner_search"] else 0
                ),
                "worker_runtime_seconds": worker_runtime,
                "worker_min_runtime_seconds": worker_min_runtime,
                "worker_min_verifier_runs": worker_min_verifiers,
                "closeout_reserve_seconds": closeout_reserve,
                "goal_plus_verifier_timeout_seconds": goal_plus_verifier_timeout,
                "goal_plus_finalization_grace_seconds": int(
                    profile.get("goal_plus_finalization_grace_seconds", 300)
                ),
                "eval_interval_seconds": int(effective_contract["eval_interval"]),
                "judge_concurrency": judge_concurrency,
                "judge_port": int(profile.get("judge_port", 8080)),
                "work_cpu_limit": effective_contract["work_cpu_limit"],
                "work_mem_limit": effective_contract["work_mem_limit"],
                "judge_cpu_limit": effective_contract["judge_cpu_limit"],
                "judge_mem_limit": effective_contract["judge_mem_limit"],
                "submission_cooldown": effective_contract["submission_cooldown"],
                "max_submissions": effective_contract["max_submissions"],
                "auto_eval_enabled": not effective_contract["disable_auto_eval"],
                "auto_resume_enabled": not effective_contract["disable_auto_resume"],
                "stop_hook_enabled": not effective_contract["disable_stop_hook"],
                "internet": effective_contract["internet"],
                "internet_source": internet_source,
                "protocol_source": {
                    "path": official_protocol["source"],
                    "sha256": official_protocol["source_sha256"],
                },
                "official_defaults": official_protocol["defaults"],
                "official_task_overrides": official_protocol["tasks"][task_id],
                "official_effective_protocol": official_contract,
                "effective_protocol": effective_contract,
                "intentional_overrides": {
                    entry["field"]: {
                        "value": entry["effective"],
                        "reason": entry["reason"],
                    }
                    for entry in differences
                },
                "protocol_diff": differences,
                "protocol_classification": (
                    "official_protocol"
                    if not differences
                    else "official_protocol_with_intentional_overrides"
                ),
                "official_edgebench_comparable": not differences,
                "prompt_sha256": io.sha256_text(prompt),
                "metric_direction": config["judge"].get("score_direction", "maximize"),
                "sforge_run_id": io.sanitize_id(f"{campaign_id}-{task_id}-{method}"),
                "state": "prepared",
                "created_at": io.utc_now(),
            }
            cell_path = destination / "cells" / cell_id
            cell_path.mkdir(parents=True)
            io.write_json(cell_path / "cell.json", cell)
            cells.append(
                {
                    "cell_id": cell_id,
                    "task_id": task_id,
                    "method": method,
                    "state": "prepared",
                    "official_edgebench_comparable": not differences,
                }
            )

    snapshot = {
        **profile,
        "methods": methods,
        "model": model,
        "reasoning_effort": reasoning,
        **(
            {"search_scheduler": search_scheduler.as_dict()}
            if search_scheduler is not None
            else {}
        ),
        **role_config,
        **global_evidence_config,
        **goal_plus_feature_config,
        "api_protocol": api_protocol,
        "thinking": thinking,
        "wall_time_seconds": wall_time,
        "concurrency": concurrency,
        "cell_concurrency": cell_concurrency,
        "worker_runtime_seconds": worker_runtime,
        "worker_min_runtime_seconds": worker_min_runtime,
        "worker_min_verifier_runs": worker_min_verifiers,
        "closeout_reserve_seconds": closeout_reserve,
        "goal_plus_verifier_timeout_seconds": goal_plus_verifier_timeout,
        "protocol_source": {
            "path": official_protocol["source"],
            "sha256": official_protocol["source_sha256"],
        },
        "pi_provider_contract": pi_provider_contract,
        "codex_provider_contract": codex_contract,
        "goal_plus_source": goal_plus_source,
    }
    io.write_json(destination / "profile.json", snapshot)
    campaign_official_comparable = all(
        item["official_edgebench_comparable"] for item in cells
    )
    io.write_json(
        destination / "campaign.json",
        {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "profile": profile["id"],
            "state": "prepared",
            "created_at": io.utc_now(),
            "edgebench_tracking_branch": io.upstream_entry("edgebench")[
                "tracking_branch"
            ],
            "edgebench_branch": io.git_branch(paths.edge_root),
            "edgebench_commit": io.git_head(paths.edge_root),
            "goal_plus_tracking_branch": (
                io.upstream_entry("goal_plus")["tracking_branch"]
                if goal_plus_source is not None
                and goal_plus_source["source_kind"] == "managed"
                else None
            ),
            "goal_plus_source": goal_plus_source,
            "goal_plus_branch": (
                goal_plus_source["branch"] if goal_plus_source is not None else None
            ),
            "goal_plus_commit": (
                goal_plus_source["commit"] if goal_plus_source is not None else None
            ),
            "dataset_revision": profile["dataset_revision"],
            "task_ids": list(profile["task_ids"]),
            "methods": methods,
            "model": model,
            "reasoning_effort": reasoning,
            **(
                {"search_scheduler": search_scheduler.as_dict()}
                if search_scheduler is not None
                else {}
            ),
            **role_config,
            **global_evidence_config,
            **goal_plus_feature_config,
            "pi_package_version": profile.get("pi_package_version"),
            "api_protocol": api_protocol,
            "thinking": thinking,
            "pi_provider_contract": pi_provider_contract,
            "codex_provider_contract": codex_contract,
            "wall_time_seconds": wall_time,
            "concurrency": concurrency,
            "cell_concurrency": cell_concurrency,
            "worker_runtime_seconds": worker_runtime,
            "worker_min_runtime_seconds": worker_min_runtime,
            "worker_min_verifier_runs": worker_min_verifiers,
            "closeout_reserve_seconds": closeout_reserve,
            "goal_plus_verifier_timeout_seconds": goal_plus_verifier_timeout,
            "goal_plus_finalization_grace_seconds": int(
                profile.get("goal_plus_finalization_grace_seconds", 300)
            ),
            "protocol_source": {
                "path": official_protocol["source"],
                "sha256": official_protocol["source_sha256"],
                "official_model": official_protocol["official_model"],
                "stagger_seconds": official_protocol["stagger_seconds"],
            },
            "protocol_classification": (
                "official_protocol"
                if campaign_official_comparable
                else "official_protocol_with_intentional_overrides"
            ),
            "official_edgebench_comparable": campaign_official_comparable,
            "cells": cells,
        },
    )
    io.write_json(
        destination / "controller.json",
        {
            "schema_version": 1,
            "state": "prepared",
            "created_at": io.utc_now(),
            "pid": None,
            "pgid": None,
        },
    )
    print(io.portable_path(destination))
    return destination
