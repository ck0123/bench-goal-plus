"""EdgeBench method, profile, and official-protocol contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import yaml

from bench_goal_plus.errors import ContractError
from bench_goal_plus.search_scheduler import search_scheduler_from_json

from . import io
from .asset_issues import excluded_task_issues
from .context import current_paths


METHODS = {
    "plain-codex": {
        "agent": "codex",
        "outer_replicas": "concurrency",
        "inner_search": False,
        "api_protocol": "openai",
    },
    "goal-plus-codex": {
        "agent": "codex-goal-plus",
        "outer_replicas": 1,
        "inner_search": True,
        "api_protocol": "openai",
    },
    "plain-pi": {
        "agent": "pi",
        "outer_replicas": "concurrency",
        "inner_search": False,
        "api_protocol": "openai",
    },
    "plain-pi-provider": {
        "agent": "pi-provider",
        "outer_replicas": "concurrency",
        "inner_search": False,
        "api_protocol": "pi-provider",
    },
    "goal-plus-pi": {
        "agent": "pi-goal-plus",
        "outer_replicas": 1,
        "inner_search": True,
        "api_protocol": "openai",
    },
    "goal-plus-pi-provider": {
        "agent": "pi-goal-plus-provider",
        "outer_replicas": 1,
        "inner_search": True,
        "api_protocol": "pi-provider",
    },
    "plain-claude": {
        "agent": "claude-code",
        "outer_replicas": "concurrency",
        "inner_search": False,
        "api_protocol": "anthropic",
    },
}
GOAL_PLUS_METHODS = frozenset(
    {"goal-plus-codex", "goal-plus-pi", "goal-plus-pi-provider"}
)
GLOBAL_EVIDENCE_MODES = frozenset({"manual", "auto", "independent"})
GOAL_PLUS_FEATURE_FLAGS = (
    "shared_dir_enabled",
    "supplemental_evaluation_enabled",
)
CLAUDE_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
PAPER_LARGE_GAP_THRESHOLD_PP = 20.0
LEGACY_PAPER_PROTOCOL_ISSUES = {
    "borden_source_inversion": "no cooldown; unusually high evaluator-call frequency",
    "exchange_core_throughput": "Internet access and unbounded CPU/hardware-sensitive score",
    "schemathesis_config_modernization": "Internet access used by the agent; no official cooldown",
    "schemathesis_datagen_pipeline": "Internet access used by the agent; no official cooldown",
    "schemathesis_reporting_observability": "Internet access used by the agent; no official cooldown",
}


def methods_require_codex(methods: Iterable[str]) -> bool:
    """Return whether any selected method launches a Codex-family agent."""
    return any(str(METHODS[method]["agent"]).startswith("codex") for method in methods)
OFFICIAL_PROTOCOL_FIELDS = frozenset(
    {
        "agent",
        "backend",
        "disable_auto_eval",
        "disable_auto_resume",
        "disable_stop_hook",
        "eval_interval",
        "judge_cpu_limit",
        "judge_mem_limit",
        "max_submissions",
        "submission_cooldown",
        "timeout",
        "work_cpu_limit",
        "work_mem_limit",
    }
)
OFFICIAL_REQUIRED_DEFAULTS = frozenset(
    {
        "agent",
        "backend",
        "eval_interval",
        "judge_cpu_limit",
        "judge_mem_limit",
        "submission_cooldown",
        "timeout",
        "work_cpu_limit",
        "work_mem_limit",
    }
)
ALLOWED_PROTOCOL_OVERRIDE_FIELDS = frozenset(
    {
        "agent",
        "attempts_per_task",
        "backend",
        "cell_concurrency",
        "evidence_annotator_model",
        "judge_concurrency",
        "model",
        "reasoning_effort",
        "timeout",
        "worker_model",
    }
)
PROFILE_PROTOCOL_OVERRIDE_FIELDS = frozenset({"eval_interval", "internet"})
OFFICIAL_TASK_COUNT = 51
OFFICIAL_SCHEDULED_RUNS = 3


def api_protocol_for_methods(methods: Iterable[str]) -> str:
    protocols = {str(METHODS[method]["api_protocol"]) for method in methods}
    if len(protocols) != 1:
        raise ValueError(
            "one EdgeBench campaign cannot mix agent API protocols: "
            + ", ".join(sorted(protocols))
        )
    return next(iter(protocols))


def validate_pi_provider_model(model_ref: Any) -> None:
    provider, separator, model_id = str(model_ref).partition("/")
    if not separator or not provider or not model_id:
        raise ValueError("Pi provider profiles must set model to PROVIDER/MODEL")


def pi_provider_role_model_refs(
    profile: dict[str, Any],
    *,
    main_model: str | None = None,
) -> list[str]:
    refs = [
        str(main_model or profile["model"]),
        str(profile.get("worker_model") or main_model or profile["model"]),
        str(
            profile.get("evidence_annotator_model")
            or main_model
            or profile["model"]
        ),
    ]
    return list(dict.fromkeys(refs))


def validate_claude_thinking_contract(
    thinking: Any, reasoning_effort: Any
) -> None:
    if thinking == {"type": "adaptive"}:
        if reasoning_effort is not None:
            raise ValueError(
                "adaptive Claude EdgeBench profiles must not set reasoning effort"
            )
        return
    effort = str(reasoning_effort or "")
    if effort not in CLAUDE_REASONING_EFFORTS:
        raise ValueError(
            "Claude EdgeBench profiles must use adaptive thinking without effort "
            "or pin a supported reasoning effort"
        )
    expected_type = "disabled" if effort in {"none", "minimal"} else "enabled"
    if thinking != {"type": expected_type}:
        raise ValueError(
            "Claude EdgeBench profiles must pair "
            f"reasoning_effort={effort!r} with thinking.type={expected_type!r}"
        )


def load_profile(value: str | Path) -> tuple[Path, dict[str, Any]]:
    paths = current_paths()
    candidate = Path(value)
    if not candidate.suffix:
        candidate = paths.profile_dir / f"{candidate.name}.json"
    elif not candidate.is_absolute():
        candidate = (paths.root / candidate).resolve()
    if not candidate.is_file():
        raise FileNotFoundError(f"EdgeBench profile not found: {candidate}")
    profile = io.read_json(candidate)
    if profile.get("schema_version") != 1:
        raise ValueError("unsupported EdgeBench profile schema")
    for key in (
        "id",
        "dataset_repository",
        "dataset_revision",
        "task_ids",
        "methods",
        "model",
        "wall_time_seconds",
        "concurrency",
    ):
        if key not in profile:
            raise ValueError(f"EdgeBench profile is missing {key!r}")
    excluded = excluded_task_issues(
        profile["task_ids"], str(profile["dataset_revision"])
    )
    if excluded:
        details = "; ".join(
            f"{issue['task_id']} ({issue['id']}, "
            f"disposition={issue['disposition']}: {issue['reason']})"
            for issue in excluded
        )
        raise ValueError(
            "EdgeBench profile schedules task(s) excluded from campaigns for "
            f"dataset revision {profile['dataset_revision']}: {details}"
        )
    unknown = set(profile["methods"]) - set(METHODS)
    if unknown:
        raise ValueError("unknown EdgeBench method(s): " + ", ".join(sorted(unknown)))
    goal_plus_methods = set(profile["methods"]) & GOAL_PLUS_METHODS
    if goal_plus_methods:
        global_evidence_mode = profile.get("global_evidence_mode", "manual")
        if global_evidence_mode not in GLOBAL_EVIDENCE_MODES:
            allowed = ", ".join(sorted(GLOBAL_EVIDENCE_MODES))
            raise ValueError(f"global_evidence_mode must be one of {allowed}")
        profile["global_evidence_mode"] = global_evidence_mode
        for field in GOAL_PLUS_FEATURE_FLAGS:
            value = profile.get(field, False)
            if not isinstance(value, bool):
                raise ValueError(f"{field} must be boolean")
            profile[field] = value
        try:
            search_scheduler = search_scheduler_from_json(
                profile.get("search_scheduler")
            )
        except ContractError as error:
            raise ValueError(f"invalid Search Scheduler profile: {error}") from error
        if search_scheduler is not None:
            try:
                search_scheduler.validate_max_candidates(int(profile["concurrency"]))
            except ContractError as error:
                raise ValueError(str(error)) from error
    else:
        goal_plus_fields = {
            "global_evidence_mode",
            *GOAL_PLUS_FEATURE_FLAGS,
        }
        configured = sorted(goal_plus_fields & set(profile))
        if configured:
            raise ValueError(
                "Goal Plus profile fields require a Goal Plus method: "
                + ", ".join(configured)
            )
    api_protocol = api_protocol_for_methods(profile["methods"])
    if api_protocol == "pi-provider":
        for model_ref in pi_provider_role_model_refs(profile):
            validate_pi_provider_model(model_ref)
    elif any(
        key in profile
        for key in (
            "worker_model",
            "worker_reasoning_effort",
            "evidence_annotator_model",
            "evidence_annotator_reasoning_effort",
            "evidence_annotator_timeout_seconds",
        )
    ):
        raise ValueError(
            "role-specific Pi models require goal-plus-pi-provider"
        )
    if api_protocol == "anthropic":
        validate_claude_thinking_contract(
            profile.get("thinking"), profile.get("reasoning_effort")
        )
        context_window = profile.get("claude_context_window_tokens")
        compact_percent = profile.get("claude_autocompact_percent")
        if (context_window is None) != (compact_percent is None):
            raise ValueError(
                "Claude context window and autocompact percent must be set together"
            )
        if context_window is not None and (
            not isinstance(context_window, int)
            or isinstance(context_window, bool)
            or context_window < 1
        ):
            raise ValueError("claude_context_window_tokens must be positive")
        if compact_percent is not None and (
            not isinstance(compact_percent, int)
            or isinstance(compact_percent, bool)
            or not 1 <= compact_percent <= 100
        ):
            raise ValueError("claude_autocompact_percent must be between 1 and 100")
    if int(profile["wall_time_seconds"]) < 1 or int(profile["concurrency"]) < 1:
        raise ValueError("wall_time_seconds and concurrency must be positive")
    if int(profile.get("cell_concurrency", 1)) < 1:
        raise ValueError("cell_concurrency must be positive")
    for key in (
        "worker_reasoning_effort",
        "evidence_annotator_reasoning_effort",
    ):
        value = profile.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{key} must be a non-empty string")
    annotator_timeout = profile.get("evidence_annotator_timeout_seconds")
    if annotator_timeout is not None and (
        not isinstance(annotator_timeout, int)
        or isinstance(annotator_timeout, bool)
        or annotator_timeout < 1
    ):
        raise ValueError("evidence_annotator_timeout_seconds must be positive")
    verifier_timeout = profile.get("goal_plus_verifier_timeout_seconds")
    if verifier_timeout is not None and (
        not isinstance(verifier_timeout, int)
        or isinstance(verifier_timeout, bool)
        or verifier_timeout < 1
    ):
        raise ValueError("goal_plus_verifier_timeout_seconds must be positive")
    worker_runtime = int(
        profile.get("worker_runtime_seconds", profile["wall_time_seconds"])
    )
    worker_min_runtime = int(profile.get("worker_min_runtime_seconds", 0))
    worker_min_verifiers = int(profile.get("worker_min_verifier_runs", 0))
    closeout_reserve = int(profile.get("closeout_reserve_seconds", 0))
    if worker_runtime < 1:
        raise ValueError("worker_runtime_seconds must be positive")
    if min(worker_min_runtime, worker_min_verifiers, closeout_reserve) < 0:
        raise ValueError("Goal Plus worker lease values must be non-negative")
    if worker_min_runtime and worker_min_runtime >= worker_runtime:
        raise ValueError(
            "worker_min_runtime_seconds must be less than worker_runtime_seconds"
        )
    if worker_runtime + closeout_reserve > int(profile["wall_time_seconds"]):
        raise ValueError("worker runtime and closeout reserve must fit within wall time")
    if int(profile.get("goal_plus_finalization_grace_seconds", 300)) < 0:
        raise ValueError(
            "goal_plus_finalization_grace_seconds must be non-negative"
        )
    pi_package_version = profile.get("pi_package_version")
    if pi_package_version is not None and (
        not isinstance(pi_package_version, str) or not pi_package_version.strip()
    ):
        raise ValueError("pi_package_version must be a non-empty string")
    if profile.get("protocol_source") != "edgebench-official-codex":
        raise ValueError("EdgeBench profile must use edgebench-official-codex")
    reasons = profile.get("protocol_override_reasons")
    if not isinstance(reasons, dict) or not reasons:
        raise ValueError("EdgeBench profile must record protocol_override_reasons")
    protocol_overrides = profile.get("protocol_overrides", {})
    if not isinstance(protocol_overrides, dict):
        raise ValueError("EdgeBench profile protocol_overrides must be an object")
    unknown_overrides = set(protocol_overrides) - PROFILE_PROTOCOL_OVERRIDE_FIELDS
    if unknown_overrides:
        raise ValueError(
            "EdgeBench profile has unsupported protocol overrides: "
            f"{sorted(unknown_overrides)}"
        )
    if "internet" in protocol_overrides and not isinstance(
        protocol_overrides["internet"], bool
    ):
        raise ValueError("EdgeBench profile internet override must be boolean")
    if "eval_interval" in protocol_overrides and (
        not isinstance(protocol_overrides["eval_interval"], int)
        or isinstance(protocol_overrides["eval_interval"], bool)
        or protocol_overrides["eval_interval"] < 1
    ):
        raise ValueError(
            "EdgeBench profile eval_interval override must be a positive integer"
        )
    missing_override_reasons = set(protocol_overrides) - set(reasons)
    if missing_override_reasons:
        raise ValueError(
            "EdgeBench profile protocol overrides are missing reasons: "
            f"{sorted(missing_override_reasons)}"
        )
    unknown_reasons = set(reasons) - (
        ALLOWED_PROTOCOL_OVERRIDE_FIELDS | PROFILE_PROTOCOL_OVERRIDE_FIELDS
    )
    if unknown_reasons:
        raise ValueError(
            "EdgeBench profile has unsupported protocol override reasons: "
            f"{sorted(unknown_reasons)}"
        )
    invalid_reasons = sorted(
        key
        for key, reason in reasons.items()
        if not isinstance(reason, str) or not reason.strip()
    )
    if invalid_reasons:
        raise ValueError(
            "EdgeBench profile has invalid protocol override reasons: "
            f"{invalid_reasons}"
        )
    return candidate, profile


def _normalize_protocol_fields(data: Any, *, context: str) -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{context} must be a mapping")
    unknown = set(data) - OFFICIAL_PROTOCOL_FIELDS
    if unknown:
        raise ValueError(f"{context} has unsupported fields: {sorted(unknown)}")
    result: dict[str, Any] = {}
    for key, value in data.items():
        if key in {
            "disable_auto_eval",
            "disable_auto_resume",
            "disable_stop_hook",
        }:
            if not isinstance(value, bool):
                raise ValueError(f"{context}.{key} must be boolean")
        elif key in {
            "eval_interval",
            "judge_cpu_limit",
            "max_submissions",
            "submission_cooldown",
            "timeout",
            "work_cpu_limit",
        }:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{context}.{key} must be a non-negative integer")
        elif key in {"judge_mem_limit", "work_mem_limit", "agent", "backend"}:
            if not isinstance(value, str) or not value:
                raise ValueError(f"{context}.{key} must be a non-empty string")
        result[key] = value
    return result


def load_official_codex_protocol(path: Path | None = None) -> dict[str, Any]:
    selected_path = path or current_paths().official_codex_protocol_path
    raw = yaml.safe_load(selected_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"official EdgeBench protocol must be a mapping: {selected_path}"
        )
    allowed_top = {"defaults", "env", "model", "stagger", "tasks"}
    unknown_top = {
        key for key in raw if key not in allowed_top and not str(key).startswith("x-")
    }
    if unknown_top:
        raise ValueError(
            "official EdgeBench protocol has unsupported top-level fields: "
            f"{sorted(unknown_top)}"
        )
    defaults = _normalize_protocol_fields(
        raw.get("defaults"), context="official defaults"
    )
    missing_defaults = OFFICIAL_REQUIRED_DEFAULTS - set(defaults)
    if missing_defaults:
        raise ValueError(
            f"official EdgeBench defaults are missing: {sorted(missing_defaults)}"
        )
    tasks_raw = raw.get("tasks")
    if not isinstance(tasks_raw, dict) or not tasks_raw:
        raise ValueError("official EdgeBench protocol must define tasks")
    tasks = {
        str(task_id): _normalize_protocol_fields(
            overrides, context=f"official task {task_id}"
        )
        for task_id, overrides in tasks_raw.items()
    }
    if len(tasks) != OFFICIAL_TASK_COUNT:
        raise ValueError(
            "official EdgeBench protocol must define exactly "
            f"{OFFICIAL_TASK_COUNT} tasks, found {len(tasks)}"
        )
    model_raw = raw.get("model")
    if not isinstance(model_raw, dict) or not isinstance(model_raw.get("model"), str):
        raise ValueError("official EdgeBench protocol must define model.model")
    stagger = raw.get("stagger", 0)
    if not isinstance(stagger, int) or isinstance(stagger, bool) or stagger < 0:
        raise ValueError(
            "official EdgeBench protocol stagger must be a non-negative integer"
        )
    return {
        "schema_version": 1,
        "source": io.portable_path(selected_path),
        "source_sha256": io.sha256_file(selected_path),
        "official_model": str(model_raw["model"]),
        "stagger_seconds": stagger,
        "defaults": defaults,
        "tasks": tasks,
    }


def official_task_protocol(
    protocol: dict[str, Any], task_id: str, config: dict[str, Any]
) -> dict[str, Any]:
    if task_id not in protocol["tasks"]:
        raise ValueError(f"official EdgeBench protocol is missing task {task_id}")
    internet = config.get("internet")
    if not isinstance(internet, bool):
        raise ValueError(f"task {task_id} must define boolean internet")
    resolved = {**protocol["defaults"], **protocol["tasks"][task_id]}
    resolved.setdefault("disable_auto_eval", False)
    resolved.setdefault("disable_auto_resume", False)
    resolved.setdefault("disable_stop_hook", False)
    resolved.setdefault("max_submissions", None)
    resolved["internet"] = internet
    return resolved


def profile_task_protocol(
    profile: dict[str, Any],
    protocol: dict[str, Any],
    task_id: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    resolved = official_task_protocol(protocol, task_id, config)
    return {**resolved, **dict(profile.get("protocol_overrides") or {})}


def protocol_diff(
    *,
    official: dict[str, Any],
    effective: dict[str, Any],
    reasons: dict[str, Any],
    allowed_fields: frozenset[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    permitted = (
        ALLOWED_PROTOCOL_OVERRIDE_FIELDS
        if allowed_fields is None
        else frozenset(allowed_fields)
    )
    fields = sorted(set(official) | set(effective))
    result: list[dict[str, Any]] = []
    for field in fields:
        before = official.get(field)
        after = effective.get(field)
        if before == after:
            continue
        if field not in permitted:
            raise ValueError(f"unsupported EdgeBench protocol override: {field}")
        reason = reasons.get(field)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"protocol override {field!r} is missing a reason")
        result.append(
            {
                "field": field,
                "official": before,
                "effective": after,
                "reason": reason,
            }
        )
    return result


_protocol_diff = protocol_diff
