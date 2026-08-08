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
SUPPORTED_METHODS = {
    "plain-codex",
    "plain-pi",
    "goal-plus-codex",
    "goal-plus-pi",
}
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
FULL_SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
PROVIDER_ID = re.compile(r"[a-z][a-z0-9_-]*")
ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
AGENT_NETWORK_POLICIES = {"default", "public-egress-blocked"}


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


def managed_upstream_branch(name: str) -> str:
    entry = (read_json(UPSTREAM_MANIFEST).get("upstreams") or {}).get(name)
    branch = entry.get("tracking_branch") if isinstance(entry, dict) else None
    if not isinstance(branch, str) or not branch:
        raise SweBenchContractError(
            f"managed upstream {name!r} has no tracking_branch"
        )
    return branch


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
    if not isinstance(task_ids, list) or len(task_ids) != 1:
        raise SweBenchContractError(
            f"{profile_id}: initial acceptance requires exactly one task"
        )
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
        raise SweBenchContractError(f"{profile_id}: tasks must contain one object")
    task = tasks[0]
    if task.get("instance_id") != task_ids[0]:
        raise SweBenchContractError(f"{profile_id}: task id mapping is inconsistent")
    for field in ("repo", "image", "base_commit"):
        if not isinstance(task.get(field), str) or not task[field]:
            raise SweBenchContractError(f"{profile_id}: task.{field} is required")
    if FULL_SHA.fullmatch(task["base_commit"]) is None:
        raise SweBenchContractError(
            f"{profile_id}: task.base_commit must be a full commit SHA"
        )
    image_setup = task.get("image_setup")
    if image_setup is not None:
        image_setup_fields = {
            "head",
            "tree",
            "patch_sha256",
            "files",
            "provenance",
        }
        if not isinstance(image_setup, dict) or set(image_setup) != image_setup_fields:
            raise SweBenchContractError(
                f"{profile_id}: task.image_setup contract is invalid"
            )
        for field in ("head", "tree"):
            if FULL_SHA.fullmatch(str(image_setup[field])) is None:
                raise SweBenchContractError(
                    f"{profile_id}: task.image_setup.{field} must be a full SHA"
                )
        if SHA256.fullmatch(str(image_setup["patch_sha256"])) is None:
            raise SweBenchContractError(
                f"{profile_id}: task.image_setup.patch_sha256 must be SHA-256"
            )
        files = image_setup["files"]
        if (
            not isinstance(files, list)
            or not files
            or any(
                not isinstance(path, str)
                or not path
                or path.startswith("/")
                or ".." in Path(path).parts
                for path in files
            )
            or len(set(files)) != len(files)
        ):
            raise SweBenchContractError(
                f"{profile_id}: task.image_setup.files must be unique relative paths"
            )
        if (
            not isinstance(image_setup["provenance"], str)
            or not image_setup["provenance"].strip()
        ):
            raise SweBenchContractError(
                f"{profile_id}: task.image_setup.provenance is required"
            )
    if not task["image"].endswith(":latest"):
        raise SweBenchContractError(
            f"{profile_id}: task.image must be an exact tagged reference"
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
    concurrency = profile.get("concurrency")
    if concurrency not in {1, 2} or profile.get("cell_concurrency") != 1:
        raise SweBenchContractError(
            f"{profile_id}: accepted concurrency is K=1..2 and C=1"
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
    seed = profile.get("seed", 1)
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 1:
        raise SweBenchContractError(f"{profile_id}: seed must be a positive integer")
    network_policy = profile.get("agent_network_policy", "default")
    if network_policy not in AGENT_NETWORK_POLICIES:
        raise SweBenchContractError(
            f"{profile_id}: unsupported agent_network_policy"
        )
    if network_policy == "public-egress-blocked" and profile.get(
        "agent_provider"
    ) is None:
        raise SweBenchContractError(
            f"{profile_id}: public-egress-blocked requires an agent_provider bridge"
        )
    if methods[0] == "plain-codex":
        _validate_openai_provider(
            profile_id, profile.get("agent_provider"), label="Plain Codex"
        )
    elif methods[0] == "goal-plus-codex" and profile.get(
        "agent_provider"
    ) is not None:
        _validate_openai_provider(
            profile_id,
            profile["agent_provider"],
            label="Goal Plus + Codex",
        )
    elif (
        methods[0] not in {"plain-pi", "goal-plus-pi", "goal-plus-codex"}
        and profile.get("agent_provider") is not None
    ):
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
    if methods[0] in {"goal-plus-codex", "goal-plus-pi"}:
        goal_plus = profile.get("goal_plus")
        required_fields = {
            "worker_runtime_seconds",
            "closeout_reserve_seconds",
            "visible_verifier_timeout_seconds",
            "evidence_annotator",
            "supplemental_evaluation_enabled",
        }
        optional_fields = {
            "worker_min_runtime_seconds",
            "worker_min_verifier_runs",
        }
        if (
            not isinstance(goal_plus, dict)
            or not required_fields.issubset(goal_plus)
            or not set(goal_plus).issubset(required_fields | optional_fields)
        ):
            raise SweBenchContractError(
                f"{profile_id}: Goal Plus requires an exact goal_plus contract"
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
        minimum_fields_present = optional_fields.intersection(goal_plus)
        if minimum_fields_present and minimum_fields_present != optional_fields:
            raise SweBenchContractError(
                f"{profile_id}: Goal Plus worker minimum fields must be configured together"
            )
        if minimum_fields_present:
            minimum_runtime = goal_plus["worker_min_runtime_seconds"]
            minimum_verifier_runs = goal_plus["worker_min_verifier_runs"]
            if (
                not isinstance(minimum_runtime, int)
                or isinstance(minimum_runtime, bool)
                or minimum_runtime < 1
                or minimum_runtime >= worker_runtime
            ):
                raise SweBenchContractError(
                    f"{profile_id}: goal_plus.worker_min_runtime_seconds must be "
                    "positive and less than worker_runtime_seconds"
                )
            if (
                not isinstance(minimum_verifier_runs, int)
                or isinstance(minimum_verifier_runs, bool)
                or minimum_verifier_runs < 1
            ):
                raise SweBenchContractError(
                    f"{profile_id}: goal_plus.worker_min_verifier_runs must be positive"
                )
        if (
            not isinstance(verifier_timeout, int)
            or isinstance(verifier_timeout, bool)
            or verifier_timeout < 1
        ):
            raise SweBenchContractError(
                f"{profile_id}: visible verifier timeout must be positive"
            )
        annotator = goal_plus["evidence_annotator"]
        if annotator != "disabled":
            annotator_fields = {
                "kind",
                "model",
                "reasoning_effort",
                "timeout_seconds",
            }
            if not isinstance(annotator, dict) or set(annotator) != annotator_fields:
                raise SweBenchContractError(
                    f"{profile_id}: Goal Plus Evidence annotator contract is invalid"
                )
            if annotator["kind"] != "codex":
                raise SweBenchContractError(
                    f"{profile_id}: Goal Plus Evidence annotator kind must be codex"
                )
            if (
                not isinstance(annotator["model"], str)
                or not annotator["model"].strip()
            ):
                raise SweBenchContractError(
                    f"{profile_id}: Goal Plus Evidence annotator model is required"
                )
            if (
                not isinstance(annotator["reasoning_effort"], str)
                or not annotator["reasoning_effort"].strip()
            ):
                raise SweBenchContractError(
                    f"{profile_id}: Goal Plus Evidence annotator reasoning is required"
                )
            annotator_timeout = annotator["timeout_seconds"]
            if (
                not isinstance(annotator_timeout, int)
                or isinstance(annotator_timeout, bool)
                or not 1 <= annotator_timeout <= 600
            ):
                raise SweBenchContractError(
                    f"{profile_id}: Goal Plus Evidence annotator timeout must be 1..600"
                )
            if methods[0] == "goal-plus-pi" and provider_contract is None:
                raise SweBenchContractError(
                    f"{profile_id}: Codex Evidence annotator requires agent_provider"
                )
        if not isinstance(goal_plus["supplemental_evaluation_enabled"], bool):
            raise SweBenchContractError(
                f"{profile_id}: goal_plus.supplemental_evaluation_enabled must be boolean"
            )
        if goal_plus["supplemental_evaluation_enabled"] and annotator == "disabled":
            raise SweBenchContractError(
                f"{profile_id}: supplemental evaluation requires the Evidence annotator"
            )
        if concurrency == 2 and not (
            methods == ["goal-plus-codex"]
            and task_ids == ["astropy__astropy-13033"]
            and goal_plus["supplemental_evaluation_enabled"] is True
            and isinstance(annotator, dict)
            and minimum_fields_present == optional_fields
        ):
            raise SweBenchContractError(
                f"{profile_id}: K=2 is restricted to the Astropy Goal Plus + Codex "
                "supplemental peer-comparison profile"
            )
    elif profile.get("goal_plus") is not None:
        raise SweBenchContractError(
            f"{profile_id}: goal_plus configuration requires a Goal Plus method"
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
    seed: int | None = None,
    retain_containers: bool | None = None,
) -> dict[str, Any]:
    if concurrency is not None and concurrency != profile["concurrency"]:
        raise SweBenchContractError(
            f"{profile['id']}: concurrency is profile-frozen at K="
            f"{profile['concurrency']}"
        )
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
    resolved["seed"] = profile.get("seed", 1) if seed is None else seed
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
