"""Frozen profiles and durable paths for aibench coding campaigns."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from bench_goal_plus.upstreams import registered_upstream_source_path


ROOT = Path(__file__).resolve().parents[2]
PUBLIC_ROOT = ROOT / "experiments" / "aibench-coding"
PROFILE_DIR = PUBLIC_ROOT / "profiles"
RUNS_ROOT = ROOT / "runs" / "aibench-coding"
UPSTREAM_CHECKOUT = ROOT / "third_party" / "aibench-coding"
UPSTREAM_ROOT = registered_upstream_source_path(
    "aibench_coding", repository_root=ROOT
)
GOAL_PLUS_ROOT = registered_upstream_source_path("goal_plus", repository_root=ROOT)
RUNTIME_ROOT = ROOT / ".bench-env" / "aibench-coding"
RUNTIME_PYTHON = RUNTIME_ROOT / "venv" / "bin" / "python"
SUPPORTED_METHODS = {
    "plain-codex",
    "plain-pi",
    "goal-plus-codex",
    "goal-plus-pi",
}
GOAL_PLUS_METHODS = {"goal-plus-codex", "goal-plus-pi"}
PI_METHODS = {"plain-pi", "goal-plus-pi"}
REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
CASE_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
SET_FINGERPRINT = re.compile(r"[0-9a-f]{16}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
VERIFIER_TIMEOUT_SECONDS = 120
PROVIDER_APIS = {
    ("openai-compatible", "responses"): "openai-responses",
    ("anthropic-compatible", "anthropic-messages"): "anthropic-messages",
}


class AIBenchContractError(ValueError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AIBenchContractError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(payload, dict):
        raise AIBenchContractError(f"expected JSON object: {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.new")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_profile(profile_id: str) -> tuple[Path, dict[str, Any]]:
    if SAFE_ID.fullmatch(profile_id) is None:
        raise AIBenchContractError(f"unsafe profile id: {profile_id!r}")
    path = PROFILE_DIR / f"{profile_id}.json"
    profile = read_json(path)
    validate_profile(profile_id, profile)
    return path, profile


def _validate_provider(profile_id: str, provider: Any) -> None:
    required = {
        "id",
        "name",
        "auth_mode",
        "base_url_env",
        "api_key_env",
        "wire_api",
    }
    if not isinstance(provider, dict) or set(provider) != required:
        raise AIBenchContractError(
            f"{profile_id}: agent_provider must use the exact provider contract"
        )
    protocol = (provider["auth_mode"], provider["wire_api"])
    if protocol not in PROVIDER_APIS:
        raise AIBenchContractError(
            f"{profile_id}: agent_provider must use openai-compatible/responses "
            "or anthropic-compatible/anthropic-messages"
        )
    if not isinstance(provider["id"], str) or not provider["id"]:
        raise AIBenchContractError(f"{profile_id}: agent_provider.id is required")
    for field in ("base_url_env", "api_key_env"):
        if ENV_NAME.fullmatch(str(provider[field])) is None:
            raise AIBenchContractError(
                f"{profile_id}: agent_provider.{field} must be an environment name"
            )


def validate_profile(profile_id: str, profile: dict[str, Any]) -> None:
    if profile.get("schema_version") != 1 or profile.get("id") != profile_id:
        raise AIBenchContractError(
            f"{profile_id}: schema_version/id does not match profile"
        )
    if profile.get("benchmark_id") != "aibench-coding":
        raise AIBenchContractError(f"{profile_id}: wrong benchmark_id")
    if profile.get("case_set") != "_clean2026":
        raise AIBenchContractError(f"{profile_id}: case_set must be _clean2026")
    if SET_FINGERPRINT.fullmatch(
        str(profile.get("expected_case_set_fingerprint") or "")
    ) is None:
        raise AIBenchContractError(
            f"{profile_id}: expected_case_set_fingerprint must be 16 hex characters"
        )
    task_ids = profile.get("task_ids")
    if (
        not isinstance(task_ids, list)
        or not task_ids
        or len(set(task_ids)) != len(task_ids)
        or any(CASE_ID.fullmatch(str(task_id)) is None for task_id in task_ids)
    ):
        raise AIBenchContractError(f"{profile_id}: task_ids must be unique safe ids")
    if profile.get("validity_policy") not in {"valid-only", "legacy-all"}:
        raise AIBenchContractError(
            f"{profile_id}: validity_policy must be valid-only or legacy-all"
        )
    methods = profile.get("methods")
    if (
        not isinstance(methods, list)
        or not methods
        or len(set(methods)) != len(methods)
        or set(methods) - SUPPORTED_METHODS
    ):
        raise AIBenchContractError(f"{profile_id}: methods are invalid")
    model = profile.get("model")
    if not isinstance(model, str) or not model.strip():
        raise AIBenchContractError(f"{profile_id}: model is required")
    if any(method in PI_METHODS for method in methods):
        provider_id, separator, model_id = model.partition("/")
        if not separator or not provider_id or not model_id:
            raise AIBenchContractError(
                f"{profile_id}: Pi model must use PROVIDER/MODEL"
            )
    _validate_provider(profile_id, profile.get("agent_provider"))
    if any("codex" in method for method in methods):
        provider = profile["agent_provider"]
        if (provider["auth_mode"], provider["wire_api"]) != (
            "openai-compatible",
            "responses",
        ):
            raise AIBenchContractError(
                f"{profile_id}: Codex methods require openai-compatible responses"
            )
        if provider["api_key_env"] != "OPENAI_API_KEY":
            raise AIBenchContractError(
                f"{profile_id}: Codex methods require agent_provider.api_key_env=OPENAI_API_KEY"
            )
    if any(method in PI_METHODS for method in methods):
        if model.partition("/")[0] != profile["agent_provider"]["id"]:
            raise AIBenchContractError(
                f"{profile_id}: Pi model provider must match agent_provider.id"
            )
    if profile.get("reasoning_effort") not in REASONING_EFFORTS:
        raise AIBenchContractError(f"{profile_id}: invalid reasoning_effort")
    for field in (
        "wall_time_seconds",
        "concurrency",
        "cell_concurrency",
        "soft_closeout_seconds",
        "hard_kill_grace_seconds",
        "worker_runtime_seconds",
        "verifier_timeout_seconds",
    ):
        value = profile.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise AIBenchContractError(f"{profile_id}: {field} must be positive")
    if profile["soft_closeout_seconds"] >= profile["wall_time_seconds"]:
        raise AIBenchContractError(
            f"{profile_id}: soft closeout must fit inside wall time"
        )
    if profile["worker_runtime_seconds"] > (
        profile["wall_time_seconds"] - profile["soft_closeout_seconds"]
    ):
        raise AIBenchContractError(
            f"{profile_id}: worker runtime must fit inside exploration time"
        )
    if profile["verifier_timeout_seconds"] != VERIFIER_TIMEOUT_SECONDS:
        raise AIBenchContractError(
            f"{profile_id}: verifier_timeout_seconds must be {VERIFIER_TIMEOUT_SECONDS}"
        )
    seeds = profile.get("seeds")
    if (
        not isinstance(seeds, list)
        or not seeds
        or len(set(seeds)) != len(seeds)
        or any(not isinstance(seed, int) or isinstance(seed, bool) for seed in seeds)
    ):
        raise AIBenchContractError(f"{profile_id}: seeds must be unique integers")


def resolve_profile(
    profile: dict[str, Any],
    *,
    methods: list[str] | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    wall_time_seconds: int | None = None,
    concurrency: int | None = None,
    cell_concurrency: int | None = None,
    seeds: list[int] | None = None,
) -> dict[str, Any]:
    resolved = json.loads(json.dumps(profile))
    for field, value in (
        ("methods", methods),
        ("model", model),
        ("reasoning_effort", reasoning_effort),
        ("wall_time_seconds", wall_time_seconds),
        ("concurrency", concurrency),
        ("cell_concurrency", cell_concurrency),
        ("seeds", seeds),
    ):
        if value is not None:
            resolved[field] = value
    validate_profile(str(resolved["id"]), resolved)
    return resolved


def campaign_dir(campaign: str | Path) -> Path:
    value = Path(campaign)
    if value.is_absolute():
        return value.expanduser().absolute()
    if SAFE_ID.fullmatch(str(value)) is None:
        raise AIBenchContractError(f"unsafe campaign id: {campaign!r}")
    return RUNS_ROOT / value


def preserve_conflict(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    for index in range(10_000):
        suffix = f"_{stamp}_bak" if index == 0 else f"_{stamp}_{index}_bak"
        backup = path.with_name(path.name + suffix)
        if not backup.exists():
            path.rename(backup)
            return backup
    raise RuntimeError(f"cannot preserve conflicting campaign path: {path}")


def split_model(profile: dict[str, Any]) -> tuple[str, str]:
    provider_id = str(profile["agent_provider"]["id"])
    model = str(profile["model"])
    selected_provider, separator, model_id = model.partition("/")
    if separator:
        return selected_provider, model_id
    return provider_id, model


def pi_api(profile: dict[str, Any]) -> str:
    provider = profile["agent_provider"]
    return PROVIDER_APIS[(provider["auth_mode"], provider["wire_api"])]
