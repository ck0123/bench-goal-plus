"""Profile and durable-path contracts for SWE-bench Verified."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = Path(__file__).resolve().parent / "profiles"
RUNS_ROOT = ROOT / "runs" / "swe-bench-verified"
SWEBENCH_ROOT = ROOT / "third_party" / "swebench"
GOAL_PLUS_ROOT = ROOT / "third_party" / "goal-plus"
UPSTREAM_MANIFEST = ROOT / "environment" / "upstreams.json"
SUPPORTED_METHODS = {"plain-codex", "plain-pi", "goal-plus-pi"}
MAX_CELL_CONCURRENCY = {
    "plain-codex": 2,
    "plain-pi": 2,
    "goal-plus-pi": 1,
}
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
FULL_SHA = re.compile(r"[0-9a-f]{40}")
PROVIDER_ID = re.compile(r"[a-z][a-z0-9_-]*")
ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}


class SweBenchContractError(ValueError):
    """Raised when a profile or campaign violates the native contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SweBenchContractError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise SweBenchContractError(f"expected a JSON object: {path}")
    return value


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.new")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _validate_openai_provider(
    profile_id: str, provider: Any, *, label: str
) -> dict[str, Any]:
    required_fields = {
        "id",
        "name",
        "auth_mode",
        "base_url_env",
        "api_key_env",
        "wire_api",
    }
    if not isinstance(provider, dict) or set(provider) != required_fields:
        raise SweBenchContractError(
            f"{profile_id}: {label} requires an exact agent_provider contract"
        )
    if PROVIDER_ID.fullmatch(str(provider["id"])) is None:
        raise SweBenchContractError(f"{profile_id}: invalid agent_provider.id")
    if not isinstance(provider["name"], str) or not provider["name"]:
        raise SweBenchContractError(f"{profile_id}: agent_provider.name is required")
    if provider["auth_mode"] != "openai-compatible":
        raise SweBenchContractError(
            f"{profile_id}: {label} auth_mode must be openai-compatible"
        )
    if provider["wire_api"] != "responses":
        raise SweBenchContractError(
            f"{profile_id}: {label} wire_api must be responses"
        )
    for field in ("base_url_env", "api_key_env"):
        if ENVIRONMENT_NAME.fullmatch(str(provider[field])) is None:
            raise SweBenchContractError(
                f"{profile_id}: agent_provider.{field} must be an environment name"
            )
    return provider


def load_profile(profile_id: str) -> tuple[Path, dict[str, Any]]:
    if SAFE_ID.fullmatch(profile_id) is None:
        raise SweBenchContractError(f"unsafe profile id: {profile_id!r}")
    path = PROFILE_DIR / f"{profile_id}.json"
    profile = read_json(path)
    validate_profile(profile_id, profile)
    return path, profile


def validate_profile(profile_id: str, profile: dict[str, Any]) -> None:
    if profile.get("schema_version") != 1:
        raise SweBenchContractError(f"{profile_id}: schema_version must be 1")
    if profile.get("id") != profile_id:
        raise SweBenchContractError(f"{profile_id}: profile id does not match filename")
    if profile.get("benchmark_id") != "swe-bench-verified":
        raise SweBenchContractError(f"{profile_id}: wrong benchmark_id")

    dataset = profile.get("dataset")
    if not isinstance(dataset, dict):
        raise SweBenchContractError(f"{profile_id}: dataset must be an object")
    for field in ("name", "split", "revision"):
        if not isinstance(dataset.get(field), str) or not dataset[field]:
            raise SweBenchContractError(
                f"{profile_id}: dataset.{field} must be non-empty"
            )
    if FULL_SHA.fullmatch(dataset["revision"]) is None:
        raise SweBenchContractError(
            f"{profile_id}: dataset.revision must be a full commit SHA"
        )

    task_ids = profile.get("task_ids")
    tasks = profile.get("tasks")
    if (
        not isinstance(task_ids, list)
        or not task_ids
        or any(not isinstance(task_id, str) or not task_id for task_id in task_ids)
    ):
        raise SweBenchContractError(f"{profile_id}: task_ids must be non-empty strings")
    if len(task_ids) != len(set(task_ids)):
        raise SweBenchContractError(f"{profile_id}: task_ids must be unique")
    if (
        not isinstance(tasks, list)
        or len(tasks) != len(task_ids)
        or any(not isinstance(task, dict) for task in tasks)
    ):
        raise SweBenchContractError(
            f"{profile_id}: tasks must contain one object per task_id"
        )
    mapped_ids = [task.get("instance_id") for task in tasks]
    if mapped_ids != task_ids or len(mapped_ids) != len(set(mapped_ids)):
        raise SweBenchContractError(
            f"{profile_id}: tasks must match task_ids in the same unique order"
        )
    for task in tasks:
        task_id = str(task["instance_id"])
        for field in ("repo", "image", "base_commit"):
            if not isinstance(task.get(field), str) or not task[field]:
                raise SweBenchContractError(
                    f"{profile_id}: task {task_id} field {field} is required"
                )
        if FULL_SHA.fullmatch(task["base_commit"]) is None:
            raise SweBenchContractError(
                f"{profile_id}: task {task_id} base_commit must be a full commit SHA"
            )
        if not task["image"].endswith(":latest"):
            raise SweBenchContractError(
                f"{profile_id}: task {task_id} image must be an exact tagged reference"
            )

    methods = profile.get("methods")
    if (
        not isinstance(methods, list)
        or len(methods) != 1
        or methods[0] not in SUPPORTED_METHODS
    ):
        raise SweBenchContractError(
            f"{profile_id}: methods must select one supported method"
        )
    if not isinstance(profile.get("model"), str) or not profile["model"]:
        raise SweBenchContractError(f"{profile_id}: model is required")
    if profile.get("reasoning_effort") not in REASONING_EFFORTS:
        raise SweBenchContractError(f"{profile_id}: unsupported reasoning_effort")
    if not isinstance(profile.get("wall_time_seconds"), int) or profile["wall_time_seconds"] < 1:
        raise SweBenchContractError(f"{profile_id}: wall_time_seconds must be positive")
    cell_concurrency = profile.get("cell_concurrency")
    if profile.get("concurrency") != 1:
        raise SweBenchContractError(
            f"{profile_id}: SWE-bench native is restricted to K=1"
        )
    if (
        not isinstance(cell_concurrency, int)
        or isinstance(cell_concurrency, bool)
        or cell_concurrency < 1
        or cell_concurrency > len(task_ids)
    ):
        raise SweBenchContractError(
            f"{profile_id}: C must be positive and cannot exceed task count"
        )
    max_cell_concurrency = MAX_CELL_CONCURRENCY[methods[0]]
    if cell_concurrency > max_cell_concurrency:
        raise SweBenchContractError(
            f"{profile_id}: method {methods[0]} supports at most C={max_cell_concurrency}"
        )
    evaluator_timeout = profile.get("evaluator_timeout_seconds")
    if not isinstance(evaluator_timeout, int) or evaluator_timeout < 1:
        raise SweBenchContractError(
            f"{profile_id}: evaluator_timeout_seconds must be positive"
        )
    if not isinstance(profile.get("retain_containers"), bool):
        raise SweBenchContractError(
            f"{profile_id}: retain_containers must be boolean"
        )
    if methods[0] == "plain-codex":
        _validate_openai_provider(
            profile_id, profile.get("agent_provider"), label="Plain Codex"
        )
    elif methods[0] not in {"plain-pi", "goal-plus-pi"} and profile.get(
        "agent_provider"
    ) is not None:
        raise SweBenchContractError(
            f"{profile_id}: Pi provider is selected by PROVIDER/MODEL"
        )

    if methods[0] in {"plain-pi", "goal-plus-pi"}:
        provider, separator, model_id = profile["model"].partition("/")
        if not separator or not provider or not model_id:
            raise SweBenchContractError(
                f"{profile_id}: Pi model must be PROVIDER/MODEL"
            )
        provider_contract = profile.get("agent_provider")
        if provider_contract is not None:
            provider_contract = _validate_openai_provider(
                profile_id, provider_contract, label="Pi custom provider"
            )
            if provider_contract["id"] != provider:
                raise SweBenchContractError(
                    f"{profile_id}: agent_provider.id must match the Pi model provider"
                )
    if methods[0] == "goal-plus-pi":
        goal_plus = profile.get("goal_plus")
        required_fields = {
            "worker_runtime_seconds",
            "closeout_reserve_seconds",
            "visible_verifier_timeout_seconds",
            "evidence_annotator",
        }
        if not isinstance(goal_plus, dict) or set(goal_plus) != required_fields:
            raise SweBenchContractError(
                f"{profile_id}: Goal Plus + Pi requires an exact goal_plus contract"
            )
        worker_runtime = goal_plus["worker_runtime_seconds"]
        closeout_reserve = goal_plus["closeout_reserve_seconds"]
        verifier_timeout = goal_plus["visible_verifier_timeout_seconds"]
        if (
            not isinstance(worker_runtime, int)
            or isinstance(worker_runtime, bool)
            or worker_runtime < 1
        ):
            raise SweBenchContractError(
                f"{profile_id}: goal_plus.worker_runtime_seconds must be positive"
            )
        if (
            not isinstance(closeout_reserve, int)
            or isinstance(closeout_reserve, bool)
            or closeout_reserve < 1
        ):
            raise SweBenchContractError(
                f"{profile_id}: goal_plus.closeout_reserve_seconds must be positive"
            )
        if worker_runtime + closeout_reserve > profile["wall_time_seconds"]:
            raise SweBenchContractError(
                f"{profile_id}: Goal Plus worker runtime and closeout reserve must fit T"
            )
        if (
            not isinstance(verifier_timeout, int)
            or isinstance(verifier_timeout, bool)
            or verifier_timeout < 1
        ):
            raise SweBenchContractError(
                f"{profile_id}: visible verifier timeout must be positive"
            )
        if goal_plus["evidence_annotator"] != "disabled":
            raise SweBenchContractError(
                f"{profile_id}: initial Goal Plus path requires disabled evidence annotator"
            )
    elif profile.get("goal_plus") is not None:
        raise SweBenchContractError(
            f"{profile_id}: goal_plus configuration is only valid for goal-plus-pi"
        )


def resolve_profile(
    profile: dict[str, Any],
    *,
    methods: list[str] | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    wall_time_seconds: int | None = None,
    concurrency: int | None = None,
    cell_concurrency: int | None = None,
    retain_containers: bool | None = None,
) -> dict[str, Any]:
    resolved = dict(profile)
    resolved["methods"] = list(methods or profile["methods"])
    resolved["model"] = model or profile["model"]
    resolved["reasoning_effort"] = reasoning_effort or profile["reasoning_effort"]
    resolved["wall_time_seconds"] = (
        wall_time_seconds or profile["wall_time_seconds"]
    )
    resolved["concurrency"] = concurrency or profile["concurrency"]
    resolved["cell_concurrency"] = (
        cell_concurrency or profile["cell_concurrency"]
    )
    resolved["retain_containers"] = (
        profile["retain_containers"]
        if retain_containers is None
        else retain_containers
    )
    validate_profile(str(resolved["id"]), resolved)
    return resolved


def campaign_dir(campaign_id: str) -> Path:
    if SAFE_ID.fullmatch(campaign_id) is None:
        raise SweBenchContractError(f"unsafe campaign id: {campaign_id!r}")
    return RUNS_ROOT / campaign_id


def preserve_conflict(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}_{stamp}_bak")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}_{stamp}_{suffix}_bak")
        suffix += 1
    path.rename(candidate)
    return candidate
