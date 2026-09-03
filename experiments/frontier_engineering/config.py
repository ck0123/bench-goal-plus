"""Profiles and task contracts for Frontier-Engineering v1-lite."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench_goal_plus.errors import ContractError
from bench_goal_plus.search_scheduler import search_scheduler_from_json
from bench_goal_plus.upstreams import registered_upstream_source_path


ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = Path(__file__).resolve().parent / "profiles"
RUNS_ROOT = ROOT / "runs" / "frontier-engineering"
UPSTREAM_ROOT = ROOT / "third_party" / "frontier-engineering"
GOAL_PLUS_ROOT = registered_upstream_source_path(
    "goal_plus",
    repository_root=ROOT,
)
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
SUPPORTED_METHODS = {
    "openevolve",
    "plain-codex",
    "plain-pi",
    "goal-plus-codex",
    "goal-plus-pi",
}
REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
ACCELERATOR_POLICIES = {"cpu-only", "nvidia-cuda-opt-in"}
NVIDIA_CUDA_ACCELERATOR = "nvidia-cuda"
NVIDIA_CUDA_TASK_PREFIXES = ("KernelEngineering/",)
NVIDIA_CUDA_TASKS = frozenset(
    {
        "Aerodynamics/CarAerodynamicsSensing",
        "Robotics/QuadrupedGaitOptimization",
        "Robotics/RobotArmCycleTimeOptimization",
    }
)


class FrontierEngineeringContractError(ValueError):
    pass


@dataclass(frozen=True)
class TaskContract:
    task_id: str
    artifact_name: str
    runtime_env: str
    runtime_python_env: str | None = None
    evaluator_timeout_seconds: int = 300
    accelerator: str = "cpu"


V1_LITE_TASKS: dict[str, TaskContract] = {
    item.task_id: item
    for item in (
        TaskContract("ComputerSystems/MallocLab", "mm.c", "frontier-eval-driver"),
        TaskContract(
            "QuantumComputing/task_01_routing_qftentangled",
            "solve.py",
            "frontier-v1-main",
        ),
        TaskContract(
            "JobShop/abz",
            "init.py",
            "frontier-eval-driver",
            runtime_python_env="frontier-v1-main",
        ),
        TaskContract(
            "InventoryOptimization/disruption_eoqd",
            "init.py",
            "frontier-v1-main",
        ),
        TaskContract(
            "EnergyStorage/BatteryFastChargingSPMe",
            "init.py",
            "frontier-eval-driver",
        ),
        TaskContract(
            "Robotics/RobotArmCycleTimeOptimization",
            "solution.py",
            "frontier-v1-main",
            evaluator_timeout_seconds=600,
            accelerator=NVIDIA_CUDA_ACCELERATOR,
        ),
        TaskContract(
            "Optics/holographic_multiplane_focusing",
            "init.py",
            "frontier-v1-main",
            evaluator_timeout_seconds=600,
        ),
        TaskContract(
            "WirelessChannelSimulation/HighReliableSimulation",
            "init.py",
            "frontier-eval-driver",
        ),
        TaskContract(
            "ReactionOptimisation/snar_multiobjective",
            "solution.py",
            "frontier-eval-driver",
            runtime_python_env="frontier-v1-summit",
            evaluator_timeout_seconds=600,
        ),
        TaskContract(
            "StructuralOptimization/TopologyOptimization",
            "init.py",
            "frontier-v1-main",
        ),
    )
}
V1_LITE_CPU_TASKS = tuple(
    task_id
    for task_id, task in V1_LITE_TASKS.items()
    if task.accelerator != NVIDIA_CUDA_ACCELERATOR
)


def task_requires_nvidia_cuda(task_id: str) -> bool:
    task = V1_LITE_TASKS.get(task_id)
    if task is not None:
        return task.accelerator == NVIDIA_CUDA_ACCELERATOR
    return task_id in NVIDIA_CUDA_TASKS or task_id.startswith(NVIDIA_CUDA_TASK_PREFIXES)


def profile_nvidia_cuda_tasks(profile: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        task_id
        for task_id in profile.get("task_ids", [])
        if isinstance(task_id, str) and task_requires_nvidia_cuda(task_id)
    )


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FrontierEngineeringContractError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(payload, dict):
        raise FrontierEngineeringContractError(f"expected JSON object: {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.new")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def load_profile(profile_id: str) -> tuple[Path, dict[str, Any]]:
    if SAFE_ID.fullmatch(profile_id) is None:
        raise FrontierEngineeringContractError(f"unsafe profile id: {profile_id!r}")
    path = PROFILE_DIR / f"{profile_id}.json"
    profile = read_json(path)
    validate_profile(profile_id, profile)
    return path, profile


def validate_profile(profile_id: str, profile: dict[str, Any]) -> None:
    if profile.get("schema_version") != 1 or profile.get("id") != profile_id:
        raise FrontierEngineeringContractError(
            f"{profile_id}: schema_version/id does not match profile"
        )
    if profile.get("benchmark_id") != "frontier-engineering":
        raise FrontierEngineeringContractError(f"{profile_id}: wrong benchmark_id")
    if profile.get("suite") != "v1-lite":
        raise FrontierEngineeringContractError(f"{profile_id}: suite must be v1-lite")
    task_ids = profile.get("task_ids")
    if not isinstance(task_ids, list) or not task_ids or len(set(task_ids)) != len(task_ids):
        raise FrontierEngineeringContractError(f"{profile_id}: task_ids must be unique")
    accelerator_policy = profile.get("accelerator_policy")
    if accelerator_policy not in ACCELERATOR_POLICIES:
        raise FrontierEngineeringContractError(
            f"{profile_id}: accelerator_policy must be cpu-only or nvidia-cuda-opt-in"
        )
    cuda_tasks = profile_nvidia_cuda_tasks(profile)
    if accelerator_policy == "cpu-only" and cuda_tasks:
        raise FrontierEngineeringContractError(
            f"{profile_id}: NVIDIA CUDA tasks are excluded by cpu-only policy: "
            f"{', '.join(cuda_tasks)}"
        )
    if accelerator_policy == "nvidia-cuda-opt-in" and not cuda_tasks:
        raise FrontierEngineeringContractError(
            f"{profile_id}: nvidia-cuda-opt-in requires at least one CUDA task"
        )
    unknown_tasks = set(task_ids) - set(V1_LITE_TASKS)
    if unknown_tasks:
        raise FrontierEngineeringContractError(
            f"{profile_id}: unknown v1-lite tasks: {', '.join(sorted(unknown_tasks))}"
        )
    methods = profile.get("methods")
    if not isinstance(methods, list) or not methods or len(set(methods)) != len(methods):
        raise FrontierEngineeringContractError(f"{profile_id}: methods must be unique")
    unknown_methods = set(methods) - SUPPORTED_METHODS
    if unknown_methods:
        raise FrontierEngineeringContractError(
            f"{profile_id}: unsupported methods: {', '.join(sorted(unknown_methods))}"
        )
    if "openevolve" in methods:
        if methods != ["openevolve"]:
            raise FrontierEngineeringContractError(
                f"{profile_id}: OpenEvolve must use a dedicated profile"
            )
        iterations = profile.get("iterations")
        if not isinstance(iterations, int) or isinstance(iterations, bool) or iterations < 1:
            raise FrontierEngineeringContractError(
                f"{profile_id}: OpenEvolve iterations must be positive"
            )
        protocol = profile.get("openevolve_protocol")
        if protocol not in {"paper", "smoke"}:
            raise FrontierEngineeringContractError(
                f"{profile_id}: OpenEvolve protocol must be paper or smoke"
            )
        if protocol == "paper" and iterations != 100:
            raise FrontierEngineeringContractError(
                f"{profile_id}: paper OpenEvolve requires 100 iterations"
            )
    if not isinstance(profile.get("model"), str) or not profile["model"]:
        raise FrontierEngineeringContractError(f"{profile_id}: model is required")
    if profile.get("reasoning_effort") not in REASONING_EFFORTS:
        raise FrontierEngineeringContractError(f"{profile_id}: unsupported reasoning_effort")
    for field in (
        "wall_time_seconds",
        "concurrency",
        "cell_concurrency",
        "soft_closeout_seconds",
        "hard_kill_grace_seconds",
        "worker_runtime_seconds",
    ):
        value = profile.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise FrontierEngineeringContractError(f"{profile_id}: {field} must be positive")
    if profile["concurrency"] != 1 and any(
        not method.startswith("goal-plus-") for method in methods
    ):
        raise FrontierEngineeringContractError(
            f"{profile_id}: non-Goal-Plus methods require K=1"
        )
    try:
        search_scheduler = search_scheduler_from_json(
            profile.get("search_scheduler")
        )
    except ContractError as error:
        raise FrontierEngineeringContractError(
            f"{profile_id}: {error}"
        ) from error
    if search_scheduler is not None:
        try:
            search_scheduler.validate_max_candidates(profile["concurrency"])
        except ContractError as error:
            raise FrontierEngineeringContractError(
                f"{profile_id}: {error}"
            ) from error
    if profile["cell_concurrency"] != 1:
        raise FrontierEngineeringContractError(
            f"{profile_id}: Frontier-Engineering initially supports C=1"
        )
    if profile["wall_time_seconds"] <= profile["soft_closeout_seconds"]:
        raise FrontierEngineeringContractError(
            f"{profile_id}: wall time must exceed closeout reserve"
        )
    if profile["worker_runtime_seconds"] > (
        profile["wall_time_seconds"] - profile["soft_closeout_seconds"]
    ):
        raise FrontierEngineeringContractError(
            f"{profile_id}: worker runtime must fit exploration budget"
        )
    worker_minimum = profile.get("worker_min_runtime_seconds")
    if worker_minimum is not None and (
        not isinstance(worker_minimum, int)
        or isinstance(worker_minimum, bool)
        or not 1 <= worker_minimum <= profile["worker_runtime_seconds"]
    ):
        raise FrontierEngineeringContractError(
            f"{profile_id}: worker minimum must fit worker runtime"
        )
    seeds = profile.get("seeds")
    if not isinstance(seeds, list) or not seeds or not all(
        isinstance(seed, int) and not isinstance(seed, bool) for seed in seeds
    ):
        raise FrontierEngineeringContractError(f"{profile_id}: seeds must be integers")
    if len(set(seeds)) != len(seeds):
        raise FrontierEngineeringContractError(f"{profile_id}: seeds must be unique")
    if not isinstance(profile.get("doctor_seed_evaluation"), bool):
        raise FrontierEngineeringContractError(
            f"{profile_id}: doctor_seed_evaluation must be boolean"
        )


def resolve_profile(
    profile: dict[str, Any],
    *,
    methods: list[str] | None = None,
    seeds: list[int] | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    wall_time_seconds: int | None = None,
    concurrency: int | None = None,
    cell_concurrency: int | None = None,
) -> dict[str, Any]:
    resolved = json.loads(json.dumps(profile))
    for field, value in (
        ("methods", methods),
        ("seeds", seeds),
        ("model", model),
        ("reasoning_effort", reasoning_effort),
        ("wall_time_seconds", wall_time_seconds),
        ("concurrency", concurrency),
        ("cell_concurrency", cell_concurrency),
    ):
        if value is not None:
            resolved[field] = value
    validate_profile(str(resolved["id"]), resolved)
    return resolved


def campaign_dir(campaign: str | Path) -> Path:
    value = Path(campaign)
    return value.expanduser().absolute() if value.is_absolute() else RUNS_ROOT / value
