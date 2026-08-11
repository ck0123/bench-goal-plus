"""Campaign preparation, container Agent execution, and official evaluation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from bench_goal_plus.codex_provider import codex_responses_provider_args
from bench_runtime_paths import configure_temp_environment, ensure_temp_root

from .config import (
    GOAL_PLUS_ROOT,
    ROOT,
    SWEBENCH_ROOT,
    SweBenchContractError,
    campaign_dir,
    preserve_conflict,
    read_json,
    utc_now,
    write_json,
)
from .environment import (
    CODEX_HOME_TMPFS,
    CODEX_RUNTIME_TMPFS,
    codex_container_responses_probe,
    goal_plus_install_script,
    goal_plus_runtime_environment,
    has_pi_worker_override,
    openai_responses_probe,
    pi_provider_proxy_environment,
    resolve_codex_runtime,
    resolve_goal_plus_codex_runtime,
    resolve_goal_plus_codex_pi_runtime,
    resolve_goal_plus_pi_runtime,
    resolve_pi_runtime,
    routed_codex_runtime,
    routed_goal_plus_codex_runtime,
    routed_goal_plus_codex_pi_runtime,
    routed_goal_plus_pi_runtime,
    routed_pi_runtime,
)
from .goal_plus_evidence import collect_goal_plus_state, record_completion_check


MANIFEST = "campaign.json"
TERMINAL_STATES = {"completed", "partial", "failed"}
HIDDEN_INSTANCE_FIELDS = {
    "patch",
    "test_patch",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
}
ISOLATED_CONTAINER_NETWORK_POLICIES = {
    "internal-api-only",
    "internal-provider-proxy",
}
GOAL_PLUS_METHODS = {"goal-plus-codex", "goal-plus-codex-pi", "goal-plus-pi"}
CODEX_MAIN_METHODS = {"goal-plus-codex", "goal-plus-codex-pi"}
PI_WORKER_METHODS = {"goal-plus-codex-pi", "goal-plus-pi"}


def _configure_huggingface_cache() -> Path:
    """Keep the default dataset cache inside this repository's ignored state."""
    os.environ.setdefault("HF_HOME", str(ensure_temp_root("huggingface")))
    return Path(os.environ["HF_HOME"])


def _run(
    command: list[str],
    *,
    cwd: Path = ROOT,
    input_text: str | None = None,
    timeout: int | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=environment,
    )


def _git_value(path: Path, *args: str) -> str:
    result = _run(["git", "-C", str(path), *args])
    if result.returncode != 0:
        raise SweBenchContractError(
            result.stderr.strip() or f"git {' '.join(args)} failed in {path}"
        )
    return result.stdout.strip()


_MANIFEST_LOCK = threading.RLock()


def _load_pinned_instances(profile: dict[str, Any]) -> list[dict[str, Any]]:
    _configure_huggingface_cache()
    from datasets import load_dataset

    dataset = load_dataset(
        profile["dataset"]["name"],
        split=profile["dataset"]["split"],
        revision=profile["dataset"]["revision"],
    )
    requested = set(profile["task_ids"])
    matches = {
        str(row["instance_id"]): dict(row)
        for row in dataset
        if row.get("instance_id") in requested
    }
    if set(matches) != requested:
        missing = sorted(requested - set(matches))
        raise SweBenchContractError(
            "pinned dataset is missing task ids: " + ", ".join(missing)
        )
    return [matches[task_id] for task_id in profile["task_ids"]]


def _load_pinned_instance(profile: dict[str, Any]) -> dict[str, Any]:
    """Preserve the one-task helper used by existing integrations and tests."""
    return _load_pinned_instances(profile)[0]


def _validate_instance_image(instance: dict[str, Any], task: dict[str, Any]) -> None:
    if instance.get("repo") != task["repo"]:
        raise SweBenchContractError("dataset repo does not match the pinned profile")
    if instance.get("base_commit") != task["base_commit"]:
        raise SweBenchContractError(
            "dataset base_commit does not match the pinned profile"
        )
    instance_id = str(instance["instance_id"]).lower()
    expected_image = (
        f"swebench/sweb.eval.x86_64.{instance_id}:latest".replace("__", "_1776_")
    )
    if expected_image != task["image"]:
        raise SweBenchContractError(
            "official harness image key does not match the local inventory tag: "
            f"{expected_image!r} != {task['image']!r}"
        )


def _visible_task(instance: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    visible = {
        "instance_id": instance["instance_id"],
        "repo": instance["repo"],
        "base_commit": instance["base_commit"],
        "problem_statement": instance["problem_statement"],
        "version": instance.get("version"),
        "image": task["image"],
    }
    if set(visible) & HIDDEN_INSTANCE_FIELDS:
        raise AssertionError("visible task allowlist includes a hidden field")
    return visible


def prepare(campaign_id: str, profile: dict[str, Any]) -> Path:
    destination = campaign_dir(campaign_id)
    preserved = preserve_conflict(destination)
    destination.mkdir(parents=True, exist_ok=False)
    evaluator_dir = destination / "evaluator"
    evaluator_dir.mkdir(parents=True)

    instances = (
        [_load_pinned_instance(profile)]
        if len(profile["task_ids"]) == 1
        else _load_pinned_instances(profile)
    )

    source_commit = _git_value(ROOT, "rev-parse", "HEAD")
    swebench_commit = _git_value(SWEBENCH_ROOT, "rev-parse", "HEAD")
    goal_plus_commit = (
        _git_value(GOAL_PLUS_ROOT, "rev-parse", "HEAD")
        if profile["methods"][0] in GOAL_PLUS_METHODS
        else None
    )
    provider_contract = (
        dict(profile["agent_provider"])
        if profile.get("agent_provider") is not None
        else {"auth_mode": "chatgpt"}
        if profile["methods"][0] in CODEX_MAIN_METHODS
        else {
            "auth_mode": "provider-api",
            "provider": profile["model"].partition("/")[0],
        }
    )
    cells = []
    visible_fields: set[str] = set()
    for index, (task, instance) in enumerate(zip(profile["tasks"], instances, strict=True)):
        _validate_instance_image(instance, task)
        visible = _visible_task(instance, task)
        visible_fields.update(visible)
        cell_id = f"{profile['methods'][0]}--{instance['instance_id']}"
        single_task = len(instances) == 1
        cell_dir = (
            destination / "cells" / profile["methods"][0]
            if single_task
            else destination / "cells" / f"{index:02d}-{instance['instance_id']}"
        )
        cell_evaluator_dir = (
            evaluator_dir
            if single_task
            else evaluator_dir / f"{index:02d}-{instance['instance_id']}"
        )
        cell_dir.mkdir(parents=True)
        cell_evaluator_dir.mkdir(parents=True, exist_ok=True)
        task_file = cell_dir / "task.json"
        evaluator_instances = cell_evaluator_dir / "instances.json"
        write_json(task_file, visible)
        evaluator_instances.write_text(
            json.dumps([instance], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        evaluator_instances.chmod(0o600)
        cells.append(
            {
                "cell_id": cell_id,
                "task_id": instance["instance_id"],
                "repo": instance["repo"],
                "base_commit": instance["base_commit"],
                "image": task["image"],
                "method": profile["methods"][0],
                "model": profile["model"],
                "reasoning_effort": profile["reasoning_effort"],
                "worker_model": (profile.get("goal_plus") or {}).get("worker_model"),
                "worker_reasoning_effort": (profile.get("goal_plus") or {}).get(
                    "worker_reasoning_effort"
                ),
                "seed": profile.get("seed", 1),
                "agent_provider": provider_contract,
                "supplemental_evaluation_enabled": (
                    profile["goal_plus"]["supplemental_evaluation_enabled"]
                    if profile["methods"][0] in GOAL_PLUS_METHODS
                    else None
                ),
                "state": "prepared",
                "task_file": str(task_file.relative_to(destination)),
                "patch_file": str((cell_dir / "model.patch").relative_to(destination)),
                "evaluator_instances_file": str(
                    evaluator_instances.relative_to(destination)
                ),
                "evaluator_dir": str(cell_evaluator_dir.relative_to(destination)),
                "agent": {"state": "pending"},
                "evaluation": {"state": "pending", "calls": 0},
            }
        )
    manifest = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "benchmark_id": "swe-bench-verified",
        "report_kind": "swe-bench-verified",
        "state": "prepared",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "profile": profile["id"],
        "methods": profile["methods"],
        "model": profile["model"],
        "reasoning_effort": profile["reasoning_effort"],
        "seed": profile.get("seed", 1),
        "agent_provider": provider_contract,
        "budget": {
            "wall_time_seconds": profile["wall_time_seconds"],
            "live_search_concurrency": profile["concurrency"],
            "cell_concurrency": profile["cell_concurrency"],
            "attempts": 1,
        },
        "container_retention": {
            "requested": profile["retain_containers"],
            "scope": "agent",
            "evaluator_container_owned_by": "official-swebench-harness",
            "network_policy": profile.get("container_network", "default"),
        },
        "dataset": {
            **profile["dataset"],
            "task_ids": profile["task_ids"],
            "agent_visible_fields": sorted(visible_fields),
            "hidden_fields_excluded_from_agent": sorted(HIDDEN_INSTANCE_FIELDS),
        },
        "source": {
            "bench_goal_plus_commit": source_commit,
            "bench_goal_plus_dirty_at_prepare": bool(
                _git_value(ROOT, "status", "--porcelain")
            ),
            "swebench_commit": swebench_commit,
            "swebench_checkout": str(SWEBENCH_ROOT),
            "goal_plus_commit": goal_plus_commit,
            "goal_plus_checkout": (
                str(GOAL_PLUS_ROOT) if goal_plus_commit is not None else None
            ),
        },
        "profile_snapshot": profile,
        "preserved_conflict": str(preserved) if preserved else None,
        "cells": cells,
    }
    if len(cells) == 1:
        manifest["dataset"]["evaluator_instances_file"] = cells[0][
            "evaluator_instances_file"
        ]
    write_json(destination / MANIFEST, manifest)
    print(json.dumps({"campaign": str(destination), "state": "prepared"}, indent=2))
    return destination


def _manifest(campaign: Path) -> dict[str, Any]:
    path = campaign / MANIFEST
    if not path.is_file():
        raise SweBenchContractError(f"campaign manifest does not exist: {path}")
    payload = read_json(path)
    if payload.get("schema_version") != 1:
        raise SweBenchContractError("unsupported SWE-bench campaign schema")
    return payload


def _save_manifest(campaign: Path, manifest: dict[str, Any]) -> None:
    with _MANIFEST_LOCK:
        manifest["updated_at"] = utc_now()
        write_json(campaign / MANIFEST, manifest)


def _docker_checked(command: list[str], *, timeout: int = 120) -> str:
    result = _run(command, timeout=timeout)
    if result.returncode != 0:
        raise SweBenchContractError(
            result.stderr.strip() or result.stdout.strip() or "Docker command failed"
        )
    return result.stdout.rstrip("\n")


def _profile_for_cell(profile: dict[str, Any], cell: dict[str, Any]) -> dict[str, Any]:
    task_id = cell.get("task_id")
    task = next(
        (item for item in profile["tasks"] if item["instance_id"] == task_id),
        None,
    )
    if task is None and len(profile["tasks"]) == 1:
        task = profile["tasks"][0]
    if task is None:
        raise SweBenchContractError(
            f"profile snapshot has no task metadata for {task_id}"
        )
    resolved = dict(profile)
    resolved["task_ids"] = [str(task["instance_id"])]
    resolved["tasks"] = [dict(task)]
    return resolved


def _container_name(
    campaign_id: str, method: str, cell_id: str | None = None
) -> str:
    digest = hashlib.sha256(
        f"{campaign_id}:{method}:{cell_id or ''}".encode()
    ).hexdigest()[:16]
    return f"bgp-swe-agent-{digest}"


def _network_name(campaign_id: str) -> str:
    digest = hashlib.sha256(campaign_id.encode()).hexdigest()[:16]
    return f"bgp-swe-net-{digest}"


@contextmanager
def _agent_network(
    campaign_id: str, profile: dict[str, Any]
) -> Iterable[dict[str, Any]]:
    policy = profile.get("agent_network_policy", "default")
    if policy == "default":
        yield {
            "policy": "default",
            "enforced": False,
            "docker_internal": False,
            "cleanup": {"attempted": False, "removed": None, "error": None},
        }
        return

    name = _network_name(campaign_id)
    network_id: str | None = None
    metadata: dict[str, Any] = {
        "policy": policy,
        "enforced": False,
        "name": name,
        "id": None,
        "docker_internal": None,
        "gateway": None,
        "cleanup": {"attempted": False, "removed": False, "error": None},
    }
    try:
        network_id = _docker_checked(
            [
                "docker",
                "network",
                "create",
                "--driver",
                "bridge",
                "--internal",
                "--label",
                "bench-goal-plus.owner=swe-bench-native",
                "--label",
                f"bench-goal-plus.campaign={campaign_id}",
                name,
            ]
        )
        inspected = json.loads(
            _docker_checked(["docker", "network", "inspect", network_id])
        )
        network = inspected[0] if isinstance(inspected, list) and inspected else None
        ipam = network.get("IPAM") if isinstance(network, dict) else None
        configs = ipam.get("Config") if isinstance(ipam, dict) else None
        gateway = (
            configs[0].get("Gateway")
            if isinstance(configs, list)
            and configs
            and isinstance(configs[0], dict)
            else None
        )
        internal = network.get("Internal") if isinstance(network, dict) else None
        if internal is not True or not isinstance(gateway, str) or not gateway:
            raise SweBenchContractError(
                "Docker did not create the required internal Agent network"
            )
        metadata.update(
            {
                "enforced": True,
                "id": network_id,
                "docker_internal": True,
                "gateway": gateway,
            }
        )
        yield metadata
    finally:
        if network_id is not None and profile["retain_containers"]:
            metadata["cleanup"] = {
                "attempted": False,
                "removed": False,
                "retained_with_agent_container": True,
                "error": None,
            }
        elif network_id is not None:
            try:
                result = _run(["docker", "network", "rm", network_id], timeout=120)
                removed = result.returncode == 0
                metadata["cleanup"] = {
                    "attempted": True,
                    "removed": removed,
                    "error": (
                        result.stderr.strip()
                        or result.stdout.strip()
                        or "docker network rm failed"
                    )
                    if not removed
                    else None,
                }
            except (OSError, subprocess.TimeoutExpired) as error:
                metadata["cleanup"] = {
                    "attempted": True,
                    "removed": False,
                    "error": f"{type(error).__name__}: {error}",
                }


_PUBLIC_EGRESS_PROBE = """
import socket

try:
    connection = socket.create_connection(("1.1.1.1", 443), timeout=3)
except OSError as error:
    print("PUBLIC_EGRESS_BLOCKED", type(error).__name__)
else:
    connection.close()
    raise SystemExit("PUBLIC_EGRESS_AVAILABLE")
"""


@contextmanager
def _temporary_setup_egress(
    container_id: str, network: dict[str, Any]
) -> Iterable[dict[str, Any]]:
    setup_egress = {
        "required": network["policy"] != "default",
        "network": None,
        "connected": False,
        "disconnected_before_agent": None,
    }
    network["setup_egress"] = setup_egress
    if not setup_egress["required"]:
        yield setup_egress
        return

    _docker_checked(["docker", "network", "connect", "bridge", container_id])
    setup_egress.update({"network": "bridge", "connected": True})
    try:
        yield setup_egress
    finally:
        _docker_checked(
            ["docker", "network", "disconnect", "bridge", container_id]
        )
        setup_egress["disconnected_before_agent"] = True


def _verify_agent_network(
    container_id: str, network: dict[str, Any]
) -> dict[str, Any]:
    if network["policy"] == "default":
        return {
            "passed": True,
            "policy": "default",
            "docker_network_mode": None,
            "public_egress_probe": "not_required",
        }
    inspected = json.loads(_docker_checked(["docker", "inspect", container_id]))
    container = inspected[0] if isinstance(inspected, list) and inspected else None
    host_config = container.get("HostConfig") if isinstance(container, dict) else None
    network_mode = (
        host_config.get("NetworkMode") if isinstance(host_config, dict) else None
    )
    network_settings = (
        container.get("NetworkSettings") if isinstance(container, dict) else None
    )
    attached = (
        network_settings.get("Networks")
        if isinstance(network_settings, dict)
        else None
    )
    attached_networks = sorted(attached) if isinstance(attached, dict) else []
    probe = _run(
        ["docker", "exec", container_id, "python", "-c", _PUBLIC_EGRESS_PROBE],
        timeout=15,
    )
    probe_passed = (
        probe.returncode == 0 and "PUBLIC_EGRESS_BLOCKED" in probe.stdout
    )
    passed = (
        network_mode == network["name"]
        and attached_networks == [network["name"]]
        and probe_passed
    )
    result = {
        "passed": passed,
        "policy": network["policy"],
        "docker_network_mode": network_mode,
        "expected_network_mode": network["name"],
        "attached_networks": attached_networks,
        "public_egress_probe": "blocked" if probe_passed else "available_or_failed",
        "probe_returncode": probe.returncode,
        "probe_stdout": probe.stdout.strip(),
        "probe_stderr": probe.stderr.strip(),
    }
    if not passed:
        raise SweBenchContractError(
            "Agent public-egress isolation probe failed: "
            + json.dumps(result, sort_keys=True)
        )
    return result


def _create_internal_network(campaign_id: str) -> dict[str, Any]:
    digest = hashlib.sha256(campaign_id.encode()).hexdigest()[:16]
    name = f"bgp-swe-net-{digest}"
    network_id = _docker_checked(
        [
            "docker",
            "network",
            "create",
            "--driver",
            "bridge",
            "--internal",
            "--label",
            "bench-goal-plus.owner=swe-bench-native",
            "--label",
            f"bench-goal-plus.campaign={campaign_id}",
            name,
        ]
    )
    payload = json.loads(_docker_checked(["docker", "network", "inspect", name]))
    gateway = ((payload[0].get("IPAM") or {}).get("Config") or [{}])[0].get(
        "Gateway"
    )
    if not isinstance(gateway, str) or not gateway:
        raise SweBenchContractError("internal Docker network has no IPv4 gateway")
    return {
        "id": network_id,
        "name": name,
        "driver": "bridge",
        "internal": True,
        "gateway": gateway,
        "external_route": False,
        "removed": False,
    }


def _dispose_internal_network(network: dict[str, Any]) -> dict[str, Any]:
    result = _run(["docker", "network", "rm", str(network["name"])], timeout=120)
    return {
        **network,
        "removed": result.returncode == 0,
        "remove_error": (
            result.stderr.strip() or result.stdout.strip() or None
            if result.returncode != 0
            else None
        ),
    }


def _dispose_agent_container(container_id: str, *, retain: bool) -> dict[str, Any]:
    if retain:
        stop_error = None
        try:
            stop_result = _run(
                ["docker", "stop", "--time", "10", container_id],
                timeout=30,
            )
            if stop_result.returncode != 0:
                stop_error = (
                    stop_result.stderr.strip()
                    or stop_result.stdout.strip()
                    or "docker stop failed"
                )
        except (OSError, subprocess.TimeoutExpired) as error:
            stop_error = f"{type(error).__name__}: {error}"

        state: dict[str, Any] = {}
        inspect_error = None
        try:
            inspect_result = _run(
                ["docker", "inspect", "--format", "{{json .State}}", container_id],
                timeout=30,
            )
            if inspect_result.returncode == 0:
                try:
                    observed = json.loads(inspect_result.stdout)
                    if isinstance(observed, dict):
                        state = {
                            "status": observed.get("Status"),
                            "running": observed.get("Running"),
                            "exit_code": observed.get("ExitCode"),
                        }
                except json.JSONDecodeError as error:
                    inspect_error = f"invalid docker inspect state: {error}"
            else:
                inspect_error = (
                    inspect_result.stderr.strip()
                    or inspect_result.stdout.strip()
                    or "docker inspect failed"
                )
        except (OSError, subprocess.TimeoutExpired) as error:
            inspect_error = f"{type(error).__name__}: {error}"

        retained = bool(state)
        stopped = retained and state.get("running") is False
        return {
            "policy": "retain",
            "attempted": True,
            "removed": False,
            "retained": retained,
            "stopped": stopped,
            "observed_state": state or None,
            "error": (
                None
                if stopped
                else inspect_error
                or stop_error
                or "container is still running after docker stop"
            ),
        }
    try:
        result = _run(["docker", "rm", "-f", container_id], timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "policy": "remove",
            "attempted": True,
            "removed": False,
            "retained": False,
            "stopped": None,
            "error": f"{type(error).__name__}: {error}",
        }
    removed = result.returncode == 0
    return {
        "policy": "remove",
        "attempted": True,
        "removed": removed,
        "retained": False,
        "stopped": None,
        "error": (result.stderr.strip() or result.stdout.strip() or None)
        if not removed
        else None,
    }


def _container_disposition_isolated(disposition: dict[str, Any]) -> bool:
    return bool(
        disposition.get("removed")
        or (disposition.get("retained") and disposition.get("stopped"))
    )


def _create_agent_container(
    campaign_id: str,
    profile: dict[str, Any],
    runtime: dict[str, Any] | None = None,
    *,
    cell_id: str | None = None,
    network_name: str | None = None,
) -> tuple[str, dict[str, Any]]:
    method = profile["methods"][0]
    name = _container_name(campaign_id, method, cell_id)
    command = [
        "docker",
        "create",
        "--pull",
        "never",
        "--name",
        name,
        "--label",
        "bench-goal-plus.owner=swe-bench-native",
        "--label",
        f"bench-goal-plus.campaign={campaign_id}",
        "--workdir",
        "/testbed",
        "--tmpfs",
        "/opt/agent-tmp:rw,nosuid,nodev,size=256m",
    ]
    if network_name is not None:
        command.extend(["--network", network_name])
    if method in {"plain-codex", *CODEX_MAIN_METHODS}:
        runtime = runtime or (
            resolve_goal_plus_codex_pi_runtime(profile)
            if method == "goal-plus-codex-pi"
            else resolve_goal_plus_codex_runtime(profile)
            if method == "goal-plus-codex"
            else resolve_codex_runtime(profile)
        )
        if (
            not runtime["archive_present"]
            or not runtime["credential_present"]
            or (
                runtime.get("auth_mode") == "openai-compatible"
                and (
                    not runtime["api_base_url"]
                    or not runtime.get("runtime_api_base_url")
                )
            )
        ):
            raise SweBenchContractError(
                "Codex runtime archive or credential is missing"
            )
        command.extend(
            [
                "--tmpfs",
                CODEX_HOME_TMPFS,
                "--tmpfs",
                CODEX_RUNTIME_TMPFS,
                "--mount",
                f"type=bind,src={runtime['archive']},dst=/opt/runtime/codex.tgz,readonly",
            ]
        )
        if method in CODEX_MAIN_METHODS and runtime["auth_mode"] == "chatgpt":
            command.extend(
                [
                    "--mount",
                    "type=bind,"
                    f"src={runtime['auth_file']},"
                    "dst=/opt/codex-home/auth.json,readonly",
                ]
            )
        if method == "goal-plus-codex-pi":
            if not runtime.get("worker_credential_present"):
                raise SweBenchContractError(
                    f"Pi worker credential for provider {runtime.get('worker_provider')} is missing"
                )
            if not all(runtime.get(key) for key in ("node_root", "package_root")):
                raise SweBenchContractError("Pi worker Node.js or package runtime is missing")
            command.extend(
                [
                    "--tmpfs",
                    "/opt/pi-home:rw,nosuid,nodev,size=128m",
                    "--mount",
                    f"type=bind,src={runtime['node_root']},dst=/opt/node,readonly",
                    "--mount",
                    f"type=bind,src={runtime['package_root']},dst=/opt/pi,readonly",
                ]
            )
    else:
        runtime = runtime or (
            resolve_goal_plus_pi_runtime(profile)
            if method == "goal-plus-pi"
            else resolve_pi_runtime(profile)
        )
        if not runtime["credential_present"]:
            raise SweBenchContractError(
                f"Pi credential for provider {runtime['provider']} is missing"
            )
        if (
            method == "goal-plus-pi"
            and has_pi_worker_override(profile)
            and not runtime.get("worker_credential_present")
        ):
            raise SweBenchContractError(
                f"Pi worker credential for provider {runtime.get('worker_provider')} is missing"
            )
        if not all(runtime.get(name) for name in ("node_root", "package_root")):
            raise SweBenchContractError("Pi Node.js or package runtime is missing")
        command.extend(
            [
                "--tmpfs",
                "/opt/pi-home:rw,nosuid,nodev,size=128m",
                "--mount",
                f"type=bind,src={runtime['node_root']},dst=/opt/node,readonly",
                "--mount",
                f"type=bind,src={runtime['package_root']},dst=/opt/pi,readonly",
            ]
        )
        models_file = runtime.get("models_file")
        if isinstance(models_file, Path):
            if not models_file.is_file():
                raise SweBenchContractError(
                    f"Pi custom provider config is missing: {models_file}"
                )
            command.extend(
                [
                    "--mount",
                    f"type=bind,src={models_file.parent},dst=/opt/pi-provider,readonly",
                ]
            )
    if method in GOAL_PLUS_METHODS:
        required_assets = (
            "goal_plus_root",
            "goal_plus_dependency_lock",
            "goal_plus_visible_verifier",
            "goal_plus_controller",
            "goal_plus_pip_cache",
        )
        if (
            method == "goal-plus-pi"
            and isinstance(runtime.get("goal_plus_evidence_annotator"), dict)
            and runtime["goal_plus_evidence_annotator"].get("kind") == "codex"
        ):
            required_assets = (*required_assets, "goal_plus_codex_archive")
        missing = [
            name
            for name in required_assets
            if not isinstance(runtime.get(name), Path) or not runtime[name].exists()
        ]
        if missing:
            raise SweBenchContractError(
                "Goal Plus container assets are missing: " + ", ".join(missing)
            )
        command.extend(
            [
                "--tmpfs",
                "/opt/goal-plus-runtime:rw,exec,nosuid,nodev,size=512m",
                "--mount",
                "type=bind,"
                f"src={runtime['goal_plus_root']},"
                "dst=/opt/goal-plus,readonly",
                "--mount",
                "type=bind,"
                f"src={runtime['goal_plus_dependency_lock']},"
                "dst=/opt/goal-plus-runtime-requirements.lock,readonly",
                "--mount",
                "type=bind,"
                f"src={runtime['goal_plus_visible_verifier'].parent},"
                "dst=/testbed/.goal-plus-verifiers,readonly",
                "--mount",
                "type=bind,"
                f"src={runtime['goal_plus_controller']},"
                "dst=/opt/swebench-goal-plus-controller.py,readonly",
                "--mount",
                "type=bind,"
                f"src={runtime['goal_plus_pip_cache']},"
                "dst=/opt/pip-cache",
            ]
        )
        if (
            method == "goal-plus-pi"
            and isinstance(runtime.get("goal_plus_evidence_annotator"), dict)
            and runtime["goal_plus_evidence_annotator"].get("kind") == "codex"
        ):
            command.extend(
                [
                    "--tmpfs",
                    CODEX_RUNTIME_TMPFS,
                    "--tmpfs",
                    CODEX_HOME_TMPFS,
                    "--mount",
                    "type=bind,"
                    f"src={runtime['goal_plus_codex_archive']},"
                    "dst=/opt/runtime/codex.tgz,readonly",
                ]
            )
    if runtime is None:
        raise AssertionError("Agent runtime was not resolved")
    command.extend([profile["tasks"][0]["image"], "sleep", "infinity"])
    container_id = _docker_checked(command)
    _docker_checked(["docker", "start", container_id])
    return container_id, runtime


def _initialize_agent_container(
    container_id: str, profile: dict[str, Any], runtime: dict[str, Any]
) -> dict[str, Any]:
    task = profile["tasks"][0]
    base_commit = task["base_commit"]
    observed = _docker_checked(
        ["docker", "exec", container_id, "git", "-C", "/testbed", "rev-parse", "HEAD"]
    )
    base_tree = _docker_checked(
        [
            "docker",
            "exec",
            container_id,
            "git",
            "-C",
            "/testbed",
            "rev-parse",
            f"{base_commit}^{{tree}}",
        ]
    )
    observed_tree = _docker_checked(
        [
            "docker",
            "exec",
            container_id,
            "git",
            "-C",
            "/testbed",
            "rev-parse",
            "HEAD^{tree}",
        ]
    )
    image_setup_verification = None
    if observed_tree != base_tree:
        image_setup = task.get("image_setup")
        setup_patch = _docker_checked(
            [
                "docker",
                "exec",
                container_id,
                "git",
                "-C",
                "/testbed",
                "diff",
                "--binary",
                f"{base_commit}..HEAD",
            ]
        )
        setup_files = _docker_checked(
            [
                "docker",
                "exec",
                container_id,
                "git",
                "-C",
                "/testbed",
                "diff",
                "--name-only",
                f"{base_commit}..HEAD",
            ]
        ).splitlines()
        canonical_patch = setup_patch.rstrip("\n") + "\n" if setup_patch else ""
        patch_sha256 = hashlib.sha256(canonical_patch.encode("utf-8")).hexdigest()
        setup_valid = bool(
            isinstance(image_setup, dict)
            and observed == image_setup.get("head")
            and observed_tree == image_setup.get("tree")
            and patch_sha256 == image_setup.get("patch_sha256")
            and setup_files == image_setup.get("files")
        )
        if not setup_valid:
            raise SweBenchContractError(
                "Agent image checkout tree does not match the dataset base commit or "
                "the profile-frozen official image setup: "
                f"HEAD {observed} ({observed_tree}) vs {base_commit} ({base_tree})"
            )
        _docker_checked(
            [
                "docker",
                "exec",
                container_id,
                "git",
                "-C",
                "/testbed",
                "merge-base",
                "--is-ancestor",
                base_commit,
                "HEAD",
            ]
        )
        image_setup_verification = {
            "head": observed,
            "tree": observed_tree,
            "patch_sha256": patch_sha256,
            "files": setup_files,
            "provenance": image_setup["provenance"],
            "passed": True,
        }
    _docker_checked(
        ["docker", "exec", container_id, "git", "-C", "/testbed", "reset", "--hard", base_commit]
    )
    _docker_checked(
        [
            "docker",
            "exec",
            container_id,
            "git",
            "-C",
            "/testbed",
            "clean",
            "-fdx",
            "-e",
            ".goal-plus-verifiers/",
        ]
    )
    if isinstance(runtime.get("models_file"), Path):
        _docker_checked(
            [
                "docker",
                "exec",
                container_id,
                "sh",
                "-lc",
                "mkdir -p /opt/pi-home/.pi/agent && "
                "cp /opt/pi-provider/models.json /opt/pi-home/.pi/agent/models.json",
            ]
        )
    method = profile["methods"][0]
    if method in {"plain-codex", *CODEX_MAIN_METHODS}:
        _docker_checked(
            [
                "docker",
                "exec",
                container_id,
                "sh",
                "-lc",
                "tar -xzf /opt/runtime/codex.tgz -C /opt/codex",
            ],
            timeout=120,
        )
    elif (
        method == "goal-plus-pi"
        and isinstance(runtime.get("goal_plus_evidence_annotator"), dict)
        and runtime["goal_plus_evidence_annotator"].get("kind") == "codex"
    ):
        _docker_checked(
            [
                "docker",
                "exec",
                container_id,
                "sh",
                "-lc",
                "mkdir -p /opt/codex && "
                "tar -xzf /opt/runtime/codex.tgz -C /opt/codex",
            ],
            timeout=120,
        )
    if method in GOAL_PLUS_METHODS:
        environment = goal_plus_runtime_environment()
        install_command = ["docker", "exec"]
        for name, value in environment.items():
            install_command.extend(["-e", f"{name}={value}"])
        if os.environ.get("PIP_INDEX_URL"):
            install_command.extend(["-e", "PIP_INDEX_URL"])
        install_script = goal_plus_install_script(include_pi=method in PI_WORKER_METHODS)
        annotator = runtime.get("goal_plus_evidence_annotator")
        if isinstance(annotator, dict) and annotator.get("kind") == "codex":
            install_script += (
                " && ln -sf "
                "/opt/codex/package/vendor/x86_64-unknown-linux-musl/bin/codex "
                "/opt/goal-plus-bin/codex"
            )
        version_probe = (
            "codex --version && pi --version"
            if method == "goal-plus-codex-pi"
            else "pi --version"
            if method == "goal-plus-pi"
            else "codex --version"
        )
        install_command.extend(
            [
                container_id,
                "sh",
                "-lc",
                install_script
                + " && python -c \"import fastmcp, goal_plus, plotly, pydantic\""
                + f" && {version_probe}",
            ]
        )
        _docker_checked(install_command, timeout=600)
        asset_copy = (
            "cp -a /opt/goal-plus/.codex /testbed/.codex && "
            if method in CODEX_MAIN_METHODS
            else ""
        )
        _docker_checked(
            [
                "docker",
                "exec",
                container_id,
                "sh",
                "-lc",
                asset_copy
                + "printf '\\n.gp/\\n.codex/\\n.goal-plus-verifiers/\\n' "
                ">> /testbed/.git/info/exclude",
            ]
        )
        observed_verifier = _docker_checked(
            [
                "docker",
                "exec",
                container_id,
                "sha256sum",
                "/testbed/.goal-plus-verifiers/visible_test_verifier.py",
            ]
        ).split()
        expected_verifier = hashlib.sha256(
            runtime["goal_plus_visible_verifier"].read_bytes()
        ).hexdigest()
        if not observed_verifier or observed_verifier[0] != expected_verifier:
            raise SweBenchContractError(
                "read-only visible verifier mount does not match the controller asset"
            )
    return {
        "observed_head": observed,
        "base_commit": base_commit,
        "base_tree": base_tree,
        "observed_tree": observed_tree,
        "synthetic_head": observed != base_commit,
        "image_setup": image_setup_verification,
        "goal_plus_initialized": profile["methods"][0] in GOAL_PLUS_METHODS,
    }


def build_agent_prompt(task: dict[str, Any]) -> str:
    hidden = set(task) & HIDDEN_INSTANCE_FIELDS
    if hidden:
        raise SweBenchContractError(
            "Agent task unexpectedly contains hidden fields: " + ", ".join(sorted(hidden))
        )
    return (
        "Work on the repository in /testbed and solve the issue below. "
        "Inspect the code, implement the smallest robust fix, and run relevant visible tests. "
        "Do not look for benchmark metadata, hidden tests, or an answer patch. "
        "Leave the final solution in the working tree.\n\n"
        f"Issue:\n{task['problem_statement']}\n"
    )


def build_goal_plus_prompt(task: dict[str, Any], profile: dict[str, Any]) -> str:
    hidden = set(task) & HIDDEN_INSTANCE_FIELDS
    if hidden:
        raise SweBenchContractError(
            "Agent task unexpectedly contains hidden fields: " + ", ".join(sorted(hidden))
        )
    goal_plus = profile["goal_plus"]
    annotator = goal_plus["evidence_annotator"]
    annotator_timeout = (
        annotator["timeout_seconds"] if isinstance(annotator, dict) else 300
    )
    annotator_host = (
        "pi-rpc"
        if isinstance(annotator, dict) and annotator.get("kind") == "pi"
        else "codex"
    )
    codex_worker = profile["methods"][0] == "goal-plus-codex"
    worker_host = "codex" if codex_worker else "pi-rpc"
    if codex_worker and profile["concurrency"] > 1:
        worker_instruction = (
            "Keep each candidate on its existing bound Codex worker session; "
            "do not create replacement lanes."
        )
    elif codex_worker:
        worker_instruction = (
            "Use the bound Codex worker session; do not create replacement lanes."
        )
    else:
        worker_instruction = (
            "Continue the same bound Pi worker session; do not create replacement lanes."
        )
    global_evidence_mode = str(goal_plus.get("global_evidence_mode", "manual"))
    minimum_budget_instruction = ""
    if "worker_min_runtime_seconds" in goal_plus:
        minimum_budget_instruction = (
            "Set strategy.worker_budget.min_runtime_seconds="
            f"{goal_plus['worker_min_runtime_seconds']} and "
            "strategy.worker_budget.min_verifier_runs="
            f"{goal_plus['worker_min_verifier_runs']}. These are lower-bound search "
            "gates: keep the same worker active until both are satisfied. "
        )
    worker_launch_instruction = ""
    if has_pi_worker_override(profile):
        worker_launch_instruction = (
            "Set strategy.worker_launch.model="
            f"{goal_plus['worker_model']} and "
            "strategy.worker_launch.reasoning_effort="
            f"{goal_plus['worker_reasoning_effort']}. "
        )
    if profile["concurrency"] == 1:
        candidate_instruction = "Use one fixed initial candidate. "
    else:
        candidate_instruction = (
            f"Use exactly {profile['concurrency']} fixed initial candidates with "
            "distinct public-evidence-based hypotheses. Start them together in one "
            "batch and bind exactly one worker session to each candidate; do not "
            "serialize or create replacement lanes. After at least two candidates have "
            "settled Evidence, keep searching so at least one worker reads a completed "
            "peer supplemental View before its next verifier attempt. "
        )
    return (
        "/goal-plus mode=autonomous Solve the public repository issue below in "
        "/testbed. Treat this as verifier-guided code repair and enter Search Mode. "
        "Do not inspect benchmark metadata, hidden tests, dataset rows, reports, or "
        "answer patches. The official SWE-bench harness remains hidden and runs only "
        "after this Goal Plus session.\n\n"
        "Freeze exactly one SearchSpec discovered from the public issue and repository. "
        "Use source_path=/testbed, metric_name=visible_test_score, direction=maximize, "
        f"strategy.name=random, strategy.worker_host={worker_host}, and "
        "strategy.orchestration_mode=parallel_loops. Set budget.max_parallel="
        f"{profile['concurrency']} and do not set the deprecated max_candidates field. "
        "Set strategy.worker_budget.max_runtime_seconds="
        f"{goal_plus['worker_runtime_seconds']}. "
        f"{minimum_budget_instruction}"
        f"{worker_launch_instruction}"
        "Set "
        "strategy.config.closeout_reserve_seconds="
        f"{goal_plus['closeout_reserve_seconds']} and strategy.config.seed="
        f"{profile.get('seed', 1)} and strategy.config.global_evidence_mode="
        f"{global_evidence_mode}. {candidate_instruction}"
        f"Set strategy.evidence_annotator.host={annotator_host} and "
        "strategy.evidence_annotator.timeout_seconds="
        f"{annotator_timeout}; "
        "leave its model and provider unset because the harness supplies the ViewAgent. "
        f"{worker_instruction}\n\n"
        "Do not add acceptance_view, a soft rubric, or predefined evaluation dimensions "
        "to SearchSpec. The harness may enable an independent ViewAgent after each "
        "verifier-settled Evidence commit; it derives open-ended, task-specific observations "
        "from the actual cumulative diff and an immutable snapshot of other candidates' "
        "settled hard-score incumbents. Those observations are non-gating, do not choose a "
        "winner, and never change candidate settlement or the official binary result.\n\n"
        "Choose a focused visible test command using only the public issue and repository. "
        "Before freeze, inspect the repository's native test instructions and confirm the "
        "chosen runner and imports exist in the task image. A command that fails because "
        "pytest, a plugin, or another dependency is unavailable is an invalid verifier; "
        "use the repository-native runner or a focused Python assertion that works in the "
        "existing environment. When translating public tests into a focused assertion, "
        "cover both the reported failure and adjacent existing assertions for the same "
        "API or branch. First write a public behavior inventory covering the reported "
        "failure, compatibility obligations evidenced by the current implementation or "
        "tests, and nearby branches that distinguish a narrow patch from a robust repair. "
        "Make the underlying visible command fail on the unfixed target behavior and pass "
        "only when every runnable item in that inventory passes. Avoid a single substring, "
        "single return value, or happy-path-only assertion when the repository exposes a "
        "more structured or precise public contract. Preserve existing assertions "
        "byte-for-byte where practical, "
        "and do not turn wording, formatting, or behavior that the public issue leaves "
        "ambiguous into a stricter hard requirement. Prefer the repository's established "
        "diagnostic form unless the issue explicitly requires an exact replacement. "
        "Include both implementation files and the relevant existing test files in "
        "edit_surface when a regression test is an appropriate repair artifact; do not "
        "exclude tests merely to minimize the edit surface. Added tests remain candidate "
        "artifacts and must not redefine or weaken the frozen verifier. Do not edit public "
        "tests merely to make a candidate pass the focused verifier. "
        "Do not install packages or access the network. "
        "Freeze exactly one process_verifiers entry with role=ranking_signal and "
        "one separate promotion_verifiers entry with role=promotion_gate. Never put "
        "the promotion gate in process_verifiers. Both entries must directly invoke the "
        "materialized artifact /testbed/.goal-plus-verifiers/visible_test_verifier.py "
        "which is benchmark-owned and read-only; never create, copy, replace, chmod, "
        "or otherwise modify it. Set cwd=/testbed. The ranking command must use: "
        "python .goal-plus-verifiers/visible_test_verifier.py --ranking-signal "
        f"--timeout-seconds {goal_plus['visible_verifier_timeout_seconds']} -- "
        "<your visible test command>. This permits a legitimate failing baseline to emit "
        "visible_test_score=0 while freeze preflight still succeeds. The promotion command "
        "must use the same candidate-relative command without --ranking-signal so a failing "
        "test exits nonzero and blocks promotion. Do not put either invocation inside an "
        "outer shell, Python subprocess normalizer, or other exit-code suppressor. Before "
        "freeze, run the underlying visible command directly and confirm any failure is the "
        "target behavior rather than a missing runner, import, plugin, or dependency. "
        "Include that wrapper path in verifier_artifacts. "
        "Keep .gp and .goal-plus-verifiers outside the editable artifact surface. "
        "After worker completion, close the pool, select and promote verifier-backed "
        "Evidence, apply the promotion patch to /testbed, record the Search result, "
        "and finish the Goal Plus record.\n\n"
        f"Public issue:\n{task['problem_statement']}\n"
    )


def _goal_plus_supplemental_evaluation_environment(
    profile: dict[str, Any],
) -> dict[str, str]:
    enabled = bool(
        profile["goal_plus"].get("supplemental_evaluation_enabled", False)
    )
    return {
        "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED": "1" if enabled else "0",
        "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED": "1" if enabled else "0",
        "GOAL_PLUS_GLOBAL_EVIDENCE_MODE": str(
            profile["goal_plus"].get("global_evidence_mode", "manual")
        ),
    }


def _goal_plus_evidence_annotator_environment(
    profile: dict[str, Any], runtime: dict[str, Any]
) -> dict[str, str]:
    annotator = profile["goal_plus"]["evidence_annotator"]
    if annotator == "disabled":
        return {"GOAL_PLUS_EVIDENCE_ANNOTATOR_DISABLED": "1"}
    environment = {
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_DISABLED": "0",
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_MODEL": str(annotator["model"]),
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_REASONING_EFFORT": str(
            annotator["reasoning_effort"]
        ),
    }
    if annotator["kind"] == "pi":
        return environment
    environment["CODEX_HOME"] = "/opt/codex-home"
    provider = profile.get("agent_provider")
    if provider is None:
        return environment
    environment.update(
        {
            "GOAL_PLUS_EVIDENCE_ANNOTATOR_BASE_URL": str(
                runtime["runtime_api_base_url"]
            ),
            "GOAL_PLUS_EVIDENCE_ANNOTATOR_PROVIDER_ID": str(provider["id"]),
            "GOAL_PLUS_EVIDENCE_ANNOTATOR_PROVIDER_NAME": str(provider["name"]),
            "GOAL_PLUS_EVIDENCE_ANNOTATOR_API_KEY_ENV": str(
                provider["api_key_env"]
            ),
            "GOAL_PLUS_EVIDENCE_ANNOTATOR_WIRE_API": str(provider["wire_api"]),
        }
    )
    return environment


def _goal_plus_evidence_annotator_public(profile: dict[str, Any]) -> Any:
    annotator = profile["goal_plus"]["evidence_annotator"]
    if annotator == "disabled":
        return "disabled"
    provider = profile.get("agent_provider")
    result = {
        "kind": annotator["kind"],
        "model": annotator["model"],
        "reasoning_effort": annotator["reasoning_effort"],
        "timeout_seconds": annotator["timeout_seconds"],
    }
    if annotator["kind"] == "codex":
        result.update(
            {
                "provider_id": provider["id"] if provider else "chatgpt",
                "wire_api": provider["wire_api"] if provider else "native-codex",
            }
        )
    return result


def _agent_command(
    container_id: str, profile: dict[str, Any], runtime: dict[str, Any]
) -> list[str]:
    common = [
        "docker",
        "exec",
        "-i",
        "-e",
        "TMPDIR=/opt/agent-tmp",
        "-e",
        "TMP=/opt/agent-tmp",
        "-e",
        "TEMP=/opt/agent-tmp",
    ]
    if profile.get("agent_network_policy") == "public-egress-blocked":
        proxy = "http://127.0.0.1:9"
        no_proxy = str(runtime.get("bridge_host") or "")
        for name, value in (
            ("HTTP_PROXY", proxy),
            ("HTTPS_PROXY", proxy),
            ("ALL_PROXY", proxy),
            ("http_proxy", proxy),
            ("https_proxy", proxy),
            ("all_proxy", proxy),
            ("NO_PROXY", no_proxy),
            ("no_proxy", no_proxy),
        ):
            common.extend(["-e", f"{name}={value}"])
    if profile["methods"][0] in CODEX_MAIN_METHODS:
        mixed_pi_workers = profile["methods"][0] == "goal-plus-codex-pi"
        custom_provider = profile.get("agent_provider") is not None
        goal_plus_environment = {
            **goal_plus_runtime_environment(),
            **_goal_plus_supplemental_evaluation_environment(profile),
            **_goal_plus_evidence_annotator_environment(profile, runtime),
            "HOME": "/opt/codex-home",
            "CODEX_HOME": "/opt/codex-home",
            "GOAL_PLUS_ROOT": "/testbed/.gp",
            "GOAL_PLUS_SEARCH_ROOT": "/testbed/.gp",
            "GOAL_PLUS_SOURCE_PATH": "/opt/goal-plus",
            "GOAL_PLUS_OUTER_DEADLINE_AT": str(runtime["outer_deadline_at"]),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if mixed_pi_workers:
            goal_plus_environment.update(
                {
                    "PI_CODING_AGENT_DIR": "/opt/pi-home/.pi/agent",
                    "GOAL_PLUS_PI_MODEL": str(profile["goal_plus"]["worker_model"]),
                    **pi_provider_proxy_environment(runtime),
                }
            )
        command = [*common]
        for name, value in goal_plus_environment.items():
            command.extend(["-e", f"{name}={value}"])
        provider_args: list[str] = []
        if custom_provider:
            command.extend(["-e", str(runtime["api_key_env"])])
            if runtime.get("bridge_host"):
                command.extend(
                    [
                        "-e",
                        f"NO_PROXY={runtime['bridge_host']}",
                        "-e",
                        f"no_proxy={runtime['bridge_host']}",
                    ]
                )
            provider_args = codex_responses_provider_args(
                str(runtime["runtime_api_base_url"]),
                provider_id=str(runtime["provider_id"]),
                provider_name=str(runtime["provider_name"]),
                api_key_env=str(runtime["api_key_env"]),
            )
        if mixed_pi_workers:
            command.extend(["-e", str(runtime["worker_credential_env"])])
        command.extend(
            [
                container_id,
                "sh",
                "-lc",
                'export PATH=/opt/goal-plus-bin:/opt/node/bin:$PATH; exec "$@"',
                "swe-bench-goal-plus-codex",
                "/opt/codex/package/vendor/x86_64-unknown-linux-musl/bin/codex",
                "exec",
                "--json",
                "--color",
                "never",
                "--dangerously-bypass-approvals-and-sandbox",
                "--dangerously-bypass-hook-trust",
                *provider_args,
                "--config",
                f'model_reasoning_effort="{profile["reasoning_effort"]}"',
                "--config",
                'mcp_servers.goal-plus.command="/opt/goal-plus-bin/goal-plus"',
                "--config",
                'mcp_servers.goal-plus.args=["--root", ".gp"]',
                "--config",
                "mcp_servers.goal-plus.startup_timeout_sec=10",
                "--config",
                "mcp_servers.goal-plus.tool_timeout_sec=300",
                "-C",
                "/testbed",
                "-m",
                profile["model"],
                "-",
            ]
        )
        return command
    if profile["methods"][0] == "plain-codex":
        provider_args = codex_responses_provider_args(
            str(runtime["runtime_api_base_url"]),
            provider_id=str(runtime["provider_id"]),
            provider_name=str(runtime["provider_name"]),
            api_key_env=str(runtime["api_key_env"]),
        )
        bridge_environment = (
            [
                "-e",
                f"NO_PROXY={runtime['bridge_host']}",
                "-e",
                f"no_proxy={runtime['bridge_host']}",
            ]
            if runtime.get("bridge_host")
            else []
        )
        return [
            *common,
            "-e",
            "HOME=/opt/codex-home",
            "-e",
            "CODEX_HOME=/opt/codex-home",
            "-e",
            str(runtime["api_key_env"]),
            *bridge_environment,
            container_id,
            "/opt/codex/package/vendor/x86_64-unknown-linux-musl/bin/codex",
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--json",
            "--color",
            "never",
            "--dangerously-bypass-approvals-and-sandbox",
            *provider_args,
            "-C",
            "/testbed",
            "-m",
            profile["model"],
            "-c",
            f'model_reasoning_effort="{profile["reasoning_effort"]}"',
            "-",
        ]
    credential_env = str(runtime["credential_env"])
    if profile["methods"][0] == "goal-plus-pi":
        goal_plus_environment = {
            **goal_plus_runtime_environment(),
            **_goal_plus_supplemental_evaluation_environment(profile),
            **_goal_plus_evidence_annotator_environment(profile, runtime),
            "HOME": "/opt/pi-home",
            "PI_CODING_AGENT_DIR": "/opt/pi-home/.pi/agent",
            "GOAL_PLUS_ROOT": "/testbed/.gp",
            "GOAL_PLUS_SEARCH_ROOT": "/testbed/.gp",
            "GOAL_PLUS_SOURCE_PATH": "/opt/goal-plus",
            "GOAL_PLUS_PI_MODEL": str(
                profile["goal_plus"].get("worker_model", profile["model"])
            ),
            "GOAL_PLUS_OUTER_DEADLINE_AT": str(runtime["outer_deadline_at"]),
            "PYTHONDONTWRITEBYTECODE": "1",
            **pi_provider_proxy_environment(runtime),
        }
        command = [*common]
        for name, value in goal_plus_environment.items():
            command.extend(["-e", f"{name}={value}"])
        if runtime.get("bridge_host"):
            command.extend(
                [
                    "-e",
                    f"NO_PROXY={runtime['bridge_host']}",
                    "-e",
                    f"no_proxy={runtime['bridge_host']}",
                ]
            )
        credential_names = [credential_env]
        worker_credential = runtime.get("worker_credential_env")
        if (
            isinstance(worker_credential, str)
            and worker_credential not in credential_names
        ):
            credential_names.append(worker_credential)
        for name in credential_names:
            command.extend(["-e", name])
        command.extend(
            [
                container_id,
                "sh",
                "-lc",
                'export PATH=/opt/goal-plus-bin:/opt/node/bin:$PATH; exec "$@"',
                "swe-bench-goal-plus",
                "/opt/node/bin/node",
                "/opt/pi/dist/cli.js",
                "--mode",
                "json",
                "--print",
                "--provider",
                str(runtime["provider"]),
                "--model",
                profile["model"],
                "--thinking",
                profile["reasoning_effort"],
                "--approve",
                "--session-dir",
                "/testbed/.gp/host-sessions/pi-main",
                "--session-id",
                str(runtime["main_session_id"]),
                "--no-extensions",
                "--no-skills",
                "--no-prompt-templates",
                "--no-context-files",
                "--extension",
                "/opt/goal-plus/.pi/extensions/goal-plus.ts",
                "--skill",
                "/opt/goal-plus/.pi/skills/goal-plus/SKILL.md",
                str(runtime["goal_prompt"]),
            ]
        )
        return command
    bridge_environment = (
        [
            "-e",
            f"NO_PROXY={runtime['bridge_host']}",
            "-e",
            f"no_proxy={runtime['bridge_host']}",
        ]
        if runtime.get("bridge_host")
        else []
    )
    return [
        *common,
        "-e",
        "HOME=/opt/pi-home",
        "-e",
        "PI_CODING_AGENT_DIR=/opt/pi-home/.pi/agent",
        *[
            item
            for name, value in pi_provider_proxy_environment(runtime).items()
            for item in ("-e", f"{name}={value}")
        ],
        "-e",
        credential_env,
        *bridge_environment,
        container_id,
        "/opt/node/bin/node",
        "/opt/pi/dist/cli.js",
        "--mode",
        "json",
        "--print",
        "--no-session",
        "--no-context-files",
        "--no-skills",
        "--no-extensions",
        "--no-prompt-templates",
        "--no-themes",
        "--approve",
        "--tools",
        "read,bash,edit,write,grep,find,ls",
        "--model",
        profile["model"],
        "--thinking",
        profile["reasoning_effort"],
    ]


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def extract_usage(output: str) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    aliases = {
        "input_tokens": ("input_tokens", "inputTokens"),
        "cached_input_tokens": (
            "cached_input_tokens",
            "cachedInputTokens",
            "cacheReadTokens",
        ),
        "output_tokens": ("output_tokens", "outputTokens"),
        "reasoning_tokens": ("reasoning_tokens", "reasoningTokens"),
    }
    for line in output.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        for candidate in _walk_dicts(payload):
            for target, names in aliases.items():
                for name in names:
                    value = candidate.get(name)
                    if isinstance(value, int) and not isinstance(value, bool):
                        normalized[target] = value
                        break
    return {
        **normalized,
        "coverage": "agent_reported" if normalized else "unavailable",
    }


def _goal_plus_closeout(
    container_id: str,
    profile: dict[str, Any],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    is_pi_main = profile["methods"][0] == "goal-plus-pi"
    uses_pi_workers = profile["methods"][0] in PI_WORKER_METHODS
    annotator = profile["goal_plus"]["evidence_annotator"]
    annotator_timeout = (
        int(annotator["timeout_seconds"]) if isinstance(annotator, dict) else 0
    )
    environment = {
        **goal_plus_runtime_environment(),
        **_goal_plus_supplemental_evaluation_environment(profile),
        **_goal_plus_evidence_annotator_environment(profile, runtime),
        "HOME": "/opt/pi-home" if is_pi_main else "/opt/codex-home",
        "PATH": "/opt/goal-plus-bin:/opt/node/bin:/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "GOAL_PLUS_ROOT": "/testbed/.gp",
        "GOAL_PLUS_SEARCH_ROOT": "/testbed/.gp",
        "GOAL_PLUS_SOURCE_PATH": "/opt/goal-plus",
        "GOAL_PLUS_OUTER_DEADLINE_AT": str(runtime["outer_deadline_at"]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if uses_pi_workers:
        environment["PI_CODING_AGENT_DIR"] = "/opt/pi-home/.pi/agent"
        environment["GOAL_PLUS_PI_MODEL"] = str(
            profile["goal_plus"].get("worker_model", profile["model"])
        )
        environment.update(pi_provider_proxy_environment(runtime))
    command = ["docker", "exec"]
    for name, value in environment.items():
        command.extend(["-e", f"{name}={value}"])
    if uses_pi_workers:
        credential_names = []
        if is_pi_main:
            credential_names.append(runtime.get("credential_env"))
        credential_names.append(
            runtime.get("worker_credential_env", runtime.get("credential_env"))
        )
        for credential_env in dict.fromkeys(credential_names):
            if isinstance(credential_env, str) and credential_env:
                command.extend(["-e", credential_env])
    if is_pi_main and runtime.get("bridge_host"):
        no_proxy = f"127.0.0.1,localhost,::1,{runtime['bridge_host']}"
        command.extend(
            ["-e", f"NO_PROXY={no_proxy}", "-e", f"no_proxy={no_proxy}"]
        )
    if profile.get("agent_provider") is not None and not is_pi_main:
        command.extend(["-e", str(runtime["api_key_env"])])
        if runtime.get("bridge_host"):
            command.extend(
                [
                    "-e",
                    f"NO_PROXY={runtime['bridge_host']}",
                    "-e",
                    f"no_proxy={runtime['bridge_host']}",
                ]
            )
    command.extend(
        [
            container_id,
            "python",
            "/opt/swebench-goal-plus-controller.py",
            "--root",
            "/testbed/.gp",
            "--source",
            "/testbed",
            "--pool-timeout-seconds",
            str(min(60, profile["goal_plus"]["closeout_reserve_seconds"])),
        ]
    )
    timeout = max(
        600,
        profile["goal_plus"]["closeout_reserve_seconds"]
        + profile["goal_plus"]["visible_verifier_timeout_seconds"]
        + annotator_timeout
        + 120,
    )
    try:
        completed = _run(command, timeout=timeout)
        error = None
    except (OSError, subprocess.TimeoutExpired) as caught:
        completed = None
        error = f"{type(caught).__name__}: {caught}"
    payload: dict[str, Any] = {}
    if completed is not None:
        for line in reversed(completed.stdout.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break
        payload.update(
            {
                "returncode": completed.returncode,
                "stdout_tail": completed.stdout[-4000:],
                "stderr_tail": completed.stderr[-4000:],
            }
        )
        if completed.returncode != 0 and not payload.get("error"):
            payload["error"] = "Goal Plus controller closeout returned nonzero"
    else:
        payload = {
            "completed": False,
            "returncode": None,
            "error": error,
        }
    payload["command"] = [*command]
    return payload


def _export_goal_plus_state(
    container_id: str,
    destination: Path,
    profile: dict[str, Any],
) -> dict[str, Any]:
    preserved = preserve_conflict(destination)
    destination.mkdir(parents=True, exist_ok=False)
    command = [
        "docker",
        "cp",
        f"{container_id}:/testbed/.gp/.",
        str(destination),
    ]
    try:
        completed = _run(command, timeout=180)
        exported = completed.returncode == 0
        error = (
            None
            if exported
            else completed.stderr.strip()
            or completed.stdout.strip()
            or "docker cp failed"
        )
    except (OSError, subprocess.TimeoutExpired) as caught:
        exported = False
        error = f"{type(caught).__name__}: {caught}"
    state = collect_goal_plus_state(
        destination,
        expected_k=profile["concurrency"],
        expected_worker_runtime_seconds=profile["goal_plus"][
            "worker_runtime_seconds"
        ],
        expected_closeout_reserve_seconds=profile["goal_plus"][
            "closeout_reserve_seconds"
        ],
        expected_visible_verifier_timeout_seconds=profile["goal_plus"][
            "visible_verifier_timeout_seconds"
        ],
        expected_worker_min_runtime_seconds=profile["goal_plus"].get(
            "worker_min_runtime_seconds"
        ),
        expected_worker_min_verifier_runs=profile["goal_plus"].get(
            "worker_min_verifier_runs"
        ),
        expected_supplemental_evaluation_enabled=profile["goal_plus"].get(
            "supplemental_evaluation_enabled", False
        ),
        expected_evidence_annotator_enabled=isinstance(
            profile["goal_plus"]["evidence_annotator"], dict
        ),
        expected_global_evidence_mode=profile["goal_plus"].get(
            "global_evidence_mode", "manual"
        ),
        expected_worker_host=(
            "codex" if profile["methods"][0] == "goal-plus-codex" else "pi-rpc"
        ),
    )
    record_completion_check(
        state,
        "state_export",
        expected=True,
        actual=exported,
        passed=exported,
    )
    return {
        **state,
        "export": {
            "completed": exported,
            "command": command,
            "destination": str(destination),
            "preserved_conflict": str(preserved) if preserved else None,
            "error": error,
        },
    }


def _container_responses_probe_with_retry(
    container_id: str,
    runtime: dict[str, Any],
    *,
    model: str,
    max_attempts: int = 3,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    result: dict[str, Any] = {"passed": False, "http_status": None}
    for attempt in range(1, max_attempts + 1):
        result = codex_container_responses_probe(
            container_id,
            runtime,
            model=model,
            existing_container=True,
        )
        attempts.append({"attempt": attempt, **result})
        if result.get("passed") is True:
            break
        status = result.get("http_status")
        retryable = (
            status is None
            or status in {408, 425, 429}
            or (isinstance(status, int) and 500 <= status <= 599)
        )
        if not retryable or attempt == max_attempts:
            break
        time.sleep(float(attempt))
    return {
        **result,
        "attempt_count": len(attempts),
        "attempts": attempts,
    }


def _run_agent(
    campaign: Path, manifest: dict[str, Any], cell: dict[str, Any]
) -> dict[str, Any]:
    profile = _profile_for_cell(manifest["profile_snapshot"], cell)
    method = profile["methods"][0]
    task = read_json(campaign / cell["task_file"])
    prompt = (
        build_goal_plus_prompt(task, profile)
        if method in GOAL_PLUS_METHODS
        else build_agent_prompt(task)
    )
    cell_dir = (campaign / cell["task_file"]).parent
    (cell_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    container_id = None
    started_at = utc_now()
    started = time.monotonic()
    setup_runtime_seconds: float | None = None
    trajectory_runtime_seconds: float | None = None
    finalization_started: float | None = None
    stdout = ""
    stderr = ""
    returncode: int | None = None
    timed_out = False
    retain_container = bool(profile["retain_containers"])
    container_name = _container_name(
        manifest["campaign_id"],
        profile["methods"][0],
        str(cell.get("cell_id") or cell.get("task_id") or "single"),
    )
    campaign_network = manifest.get("container_network_runtime") or {}
    network_name = (
        campaign_network.get("name")
        if campaign_network.get("internal") is True
        else None
    )
    bridge_host = campaign_network.get("gateway") if network_name else None
    network = (
        {
            "policy": profile.get("container_network"),
            "enforced": True,
            "docker_internal": True,
            "name": network_name,
            "id": campaign_network.get("id"),
            "gateway": bridge_host,
            "cleanup": {"managed_by": "campaign"},
        }
        if network_name
        else None
    )
    cleanup: dict[str, Any] = {
        "policy": "retain" if retain_container else "remove",
        "attempted": False,
        "removed": False,
        "retained": False,
        "stopped": None,
        "error": None,
    }
    runtime_public: dict[str, Any] = {}
    image_checkout: dict[str, Any] = {}
    goal_plus_closeout: dict[str, Any] | None = None
    goal_plus_state: dict[str, Any] | None = None
    recorded_command: list[str] | None = None
    agent_error: str | None = None
    resources = ExitStack()
    try:
        if network is None:
            network = resources.enter_context(
                _agent_network(manifest["campaign_id"], profile)
            )
            if network.get("enforced"):
                network_name = str(network["name"])
                bridge_host = str(network["gateway"])
        bridge_listen_host = (
            str(network["gateway"]) if network.get("enforced") else None
        )
        if method == "plain-codex":
            runtime = resources.enter_context(
                routed_codex_runtime(
                    profile,
                    campaign,
                    bridge_listen_host=bridge_listen_host,
                )
            )
            host_probe = openai_responses_probe(
                str(runtime["runtime_api_base_url"]),
                api_key_env=str(runtime["api_key_env"]),
                model=str(profile["model"]),
            )
            if not host_probe["passed"]:
                raise SweBenchContractError(
                    "OpenAI-compatible Responses probe failed through the runtime route"
                )
        elif method == "goal-plus-codex-pi":
            runtime = resources.enter_context(
                routed_goal_plus_codex_pi_runtime(
                    profile,
                    cell_dir,
                    bridge_listen_host=bridge_listen_host,
                )
            )
            host_probe = openai_responses_probe(
                str(runtime["runtime_api_base_url"]),
                api_key_env=str(runtime["api_key_env"]),
                model=str(profile["model"]),
            )
            if not host_probe["passed"]:
                raise SweBenchContractError(
                    "Goal Plus Codex MainAgent Responses probe failed through the runtime route"
                )
        elif method == "goal-plus-codex":
            if profile.get("agent_provider") is not None:
                runtime = resources.enter_context(
                    routed_goal_plus_codex_runtime(
                        profile,
                        campaign,
                        bridge_listen_host=bridge_listen_host,
                    )
                )
                host_probe = openai_responses_probe(
                    str(runtime["runtime_api_base_url"]),
                    api_key_env=str(runtime["api_key_env"]),
                    model=str(profile["model"]),
                )
                if not host_probe["passed"]:
                    raise SweBenchContractError(
                        "Goal Plus Codex Responses probe failed through the runtime route"
                    )
            else:
                runtime = resolve_goal_plus_codex_runtime(profile)
                host_probe = None
        else:
            if (
                profile.get("agent_provider") is not None
                or profile.get("container_network") == "internal-provider-proxy"
            ):
                runtime = resources.enter_context(
                    routed_goal_plus_pi_runtime(
                        profile,
                        cell_dir,
                        bridge_listen_host=bridge_listen_host,
                        bridge_host=bridge_host,
                    )
                    if method == "goal-plus-pi"
                    else routed_pi_runtime(
                        profile,
                        cell_dir,
                        bridge_listen_host=bridge_listen_host,
                        bridge_host=bridge_host,
                    )
                )
                if runtime.get("custom_provider"):
                    host_probe = openai_responses_probe(
                        str(runtime["runtime_api_base_url"]),
                        api_key_env=str(runtime["api_key_env"]),
                        model=str(runtime["model_id"]),
                    )
                    if not host_probe["passed"]:
                        raise SweBenchContractError(
                            "Pi OpenAI-compatible Responses probe failed through the runtime route"
                        )
                else:
                    host_probe = None
            else:
                runtime = (
                    resolve_goal_plus_pi_runtime(profile)
                    if method == "goal-plus-pi"
                    else resolve_pi_runtime(profile)
                )
                host_probe = None
        if (
            network.get("enforced")
            and runtime.get("bridge") is None
            and runtime.get("provider_proxy") is None
        ):
            raise SweBenchContractError(
                "public-egress-blocked requires a loopback provider endpoint "
                "routed through the internal-network gateway"
            )
        container_id, runtime = _create_agent_container(
            manifest["campaign_id"],
            profile,
            runtime,
            cell_id=str(cell.get("cell_id") or cell.get("task_id") or "single"),
            network_name=network_name,
        )
        with _MANIFEST_LOCK:
            cell["agent"] = {
                "state": "running",
                "container": {
                    "id": container_id,
                    "name": container_name,
                    "retention_requested": retain_container,
                    "credentials_persisted": False,
                    "network": network_name or "default",
                },
            }
            _save_manifest(campaign, manifest)
        if method == "plain-codex":
            runtime_public = {
                "kind": "codex-openai-compatible-responses",
                "archive": str(runtime["archive"]),
                "provider": runtime["provider_id"],
                "auth_mode": runtime["auth_mode"],
                "wire_api": runtime["wire_api"],
                "base_url_env": runtime["base_url_env"],
                "api_key_env": runtime["api_key_env"],
                "api_base_url": runtime["api_base_url"],
                "runtime_api_base_url": runtime["runtime_api_base_url"],
                "bridge": (
                    {
                        key: value
                        for key, value in runtime["bridge"].items()
                        if key != "pid"
                    }
                    if runtime["bridge"]
                    else None
                ),
                "host_responses_probe": host_probe,
            }
        elif method in CODEX_MAIN_METHODS:
            runtime_public = {
                "kind": (
                    "goal-plus-codex-pi-openai-compatible-responses"
                    if method == "goal-plus-codex-pi"
                    else "goal-plus-codex-openai-compatible-responses"
                    if profile.get("agent_provider") is not None
                    else "goal-plus-codex-chatgpt"
                ),
                "archive": str(runtime["archive"]),
                "auth_mode": runtime["auth_mode"],
                "goal_plus_root": str(runtime["goal_plus_root"]),
                "goal_plus_commit": manifest["source"].get("goal_plus_commit"),
                "dependency_lock": str(runtime["goal_plus_dependency_lock"]),
                "visible_verifier": str(runtime["goal_plus_visible_verifier"]),
                "controller": str(runtime["goal_plus_controller"]),
                "pip_cache": str(runtime["goal_plus_pip_cache"]),
                "evidence_annotator": _goal_plus_evidence_annotator_public(profile),
                "global_evidence_mode": profile["goal_plus"].get(
                    "global_evidence_mode", "manual"
                ),
                "credentials_persisted": False,
            }
            if profile.get("agent_provider") is not None:
                runtime_public.update(
                    {
                        "provider": runtime["provider_id"],
                        "wire_api": runtime["wire_api"],
                        "base_url_env": runtime["base_url_env"],
                        "api_key_env": runtime["api_key_env"],
                        "api_base_url": runtime["api_base_url"],
                        "runtime_api_base_url": runtime["runtime_api_base_url"],
                        "bridge": (
                            {
                                key: value
                                for key, value in runtime["bridge"].items()
                                if key != "pid"
                            }
                            if runtime["bridge"]
                            else None
                        ),
                        "host_responses_probe": host_probe,
                    }
                )
            if method == "goal-plus-codex-pi":
                runtime_public["worker"] = {
                    "host": "pi-rpc",
                    "model": profile["goal_plus"]["worker_model"],
                    "reasoning_effort": profile["goal_plus"][
                        "worker_reasoning_effort"
                    ],
                    "node_root": str(runtime["node_root"]),
                    "package_root": str(runtime["package_root"]),
                    "provider": runtime["worker_provider"],
                    "credential_env": runtime["worker_credential_env"],
                    "provider_proxy": runtime.get("provider_proxy"),
                }
        else:
            runtime_public = {
                "kind": (
                    "goal-plus-pi-container-runtime"
                    if method == "goal-plus-pi"
                    else "pi-container-runtime"
                ),
                "node_root": str(runtime["node_root"]),
                "package_root": str(runtime["package_root"]),
                "provider": runtime["provider"],
                "credential_env": runtime["credential_env"],
            }
            if runtime.get("custom_provider"):
                runtime_public.update(
                    {
                        "provider_name": runtime["provider_name"],
                        "auth_mode": runtime["auth_mode"],
                        "wire_api": runtime["wire_api"],
                        "base_url_env": runtime["base_url_env"],
                        "api_key_env": runtime["api_key_env"],
                        "api_base_url": runtime["api_base_url"],
                        "runtime_api_base_url": runtime["runtime_api_base_url"],
                        "models_file": str(runtime["models_file"]),
                        "bridge": (
                            {
                                key: value
                                for key, value in runtime["bridge"].items()
                                if key != "pid"
                            }
                            if runtime["bridge"]
                            else None
                        ),
                        "host_responses_probe": host_probe,
                    }
                )
            elif runtime.get("provider_proxy"):
                runtime_public.update(
                    {
                        "wire_api": runtime["provider_endpoint"]["wire_api"],
                        "api_base_url": runtime["provider_endpoint"]["base_url"],
                        "provider_proxy": runtime["provider_proxy"],
                    }
                )
            if method == "goal-plus-pi":
                runtime_public.update(
                    {
                        "goal_plus_root": str(runtime["goal_plus_root"]),
                        "goal_plus_commit": manifest["source"].get(
                            "goal_plus_commit"
                        ),
                        "dependency_lock": str(
                            runtime["goal_plus_dependency_lock"]
                        ),
                        "visible_verifier": str(
                            runtime["goal_plus_visible_verifier"]
                        ),
                        "controller": str(runtime["goal_plus_controller"]),
                        "pip_cache": str(runtime["goal_plus_pip_cache"]),
                        "evidence_annotator": _goal_plus_evidence_annotator_public(
                            profile
                        ),
                        "global_evidence_mode": profile["goal_plus"].get(
                            "global_evidence_mode", "manual"
                        ),
                        "codex_archive": (
                            str(runtime["goal_plus_codex_archive"])
                            if isinstance(
                                runtime.get("goal_plus_evidence_annotator"), dict
                            )
                            and runtime["goal_plus_evidence_annotator"].get("kind")
                            == "codex"
                            else None
                        ),
                    }
                )
                if has_pi_worker_override(profile):
                    runtime_public["worker"] = {
                        "host": "pi-rpc",
                        "model": profile["goal_plus"]["worker_model"],
                        "reasoning_effort": profile["goal_plus"][
                            "worker_reasoning_effort"
                        ],
                        "provider": runtime["worker_provider"],
                        "credential_env": runtime["worker_credential_env"],
                        "provider_proxy": runtime.get("provider_proxy"),
                    }
        runtime_public["agent_network"] = network
        runtime_public["container_network"] = (
            {
                "name": network_name,
                "internal": True,
                "external_route": False,
                "api_bridge_host": bridge_host,
                "provider_proxy": runtime.get("provider_proxy"),
            }
            if network_name
            else {"name": "default", "internal": False}
        )
        if profile.get("agent_network_policy") == "public-egress-blocked":
            with _temporary_setup_egress(container_id, network):
                image_checkout = _initialize_agent_container(
                    container_id, profile, runtime
                )
        else:
            image_checkout = _initialize_agent_container(
                container_id, profile, runtime
            )
        if network.get("enforced"):
            network["verification"] = _verify_agent_network(container_id, network)
        if method == "plain-codex":
            container_probe = _container_responses_probe_with_retry(
                container_id,
                runtime,
                model=str(profile["model"]),
            )
            runtime_public["container_responses_probe"] = container_probe
            if not container_probe["passed"]:
                raise SweBenchContractError(
                    "OpenAI-compatible Responses probe failed in the Agent container"
                )
        elif (
            method in CODEX_MAIN_METHODS
            and profile.get("agent_provider") is not None
        ):
            container_probe = _container_responses_probe_with_retry(
                container_id,
                runtime,
                model=str(profile["model"]),
            )
            runtime_public["container_responses_probe"] = container_probe
            if not container_probe["passed"]:
                raise SweBenchContractError(
                    "Goal Plus Codex Responses probe failed in the Agent container"
                )
        elif method not in CODEX_MAIN_METHODS and runtime.get("custom_provider"):
            container_probe = _container_responses_probe_with_retry(
                container_id,
                runtime,
                model=str(runtime["model_id"]),
            )
            runtime_public["container_responses_probe"] = container_probe
            if not container_probe["passed"]:
                raise SweBenchContractError(
                    "Pi OpenAI-compatible Responses probe failed in the Agent container"
                )
        setup_runtime_seconds = time.monotonic() - started
        if method in GOAL_PLUS_METHODS:
            runtime["outer_deadline_at"] = (
                datetime.now(timezone.utc)
                + timedelta(seconds=profile["wall_time_seconds"])
            ).isoformat()
            runtime["main_session_id"] = "swe-bench-main-" + hashlib.sha256(
                (
                    f"{manifest['campaign_id']}:"
                    f"{cell.get('cell_id') or cell.get('task_id') or 'single'}"
                ).encode("utf-8")
            ).hexdigest()[:12]
            runtime["goal_prompt"] = prompt
            runtime_public["outer_deadline_at"] = runtime["outer_deadline_at"]
            runtime_public["main_session_id"] = runtime["main_session_id"]
        command = _agent_command(container_id, profile, runtime)
        recorded_command = (
            [*command[:-1], "<goal-prompt>"]
            if method == "goal-plus-pi"
            else [*command]
        )
        trajectory_started = time.monotonic()
        try:
            result = _run(
                command,
                # Pi reads piped stdin before it processes positional prompts. An
                # explicit empty input closes stdin immediately for this one-shot run.
                input_text="" if method == "goal-plus-pi" else prompt,
                timeout=profile["wall_time_seconds"],
            )
            stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
            trajectory_runtime_seconds = time.monotonic() - trajectory_started
        except subprocess.TimeoutExpired as error:
            stdout = _text(error.stdout)
            stderr = _text(error.stderr)
            timed_out = True
            trajectory_runtime_seconds = time.monotonic() - trajectory_started
            if method in CODEX_MAIN_METHODS:
                _docker_checked(
                    [
                        "docker",
                        "exec",
                        container_id,
                        "sh",
                        "-lc",
                        "pkill -TERM -x codex 2>/dev/null || true",
                    ],
                    timeout=30,
                )
            elif method == "goal-plus-pi":
                _docker_checked(
                    [
                        "docker",
                        "exec",
                        container_id,
                        "sh",
                        "-lc",
                        "pkill -TERM -x node 2>/dev/null || true",
                    ],
                    timeout=30,
                )
            else:
                _docker_checked(
                    ["docker", "stop", "--time", "10", container_id], timeout=30
                )
                _docker_checked(["docker", "start", container_id], timeout=30)

        finalization_started = time.monotonic()
        if method in GOAL_PLUS_METHODS:
            goal_plus_closeout = _goal_plus_closeout(container_id, profile, runtime)
            goal_plus_state = _export_goal_plus_state(
                container_id,
                cell_dir / "goal-plus-state",
                profile,
            )
            record_completion_check(
                goal_plus_state,
                "controller_closeout",
                expected=True,
                actual=goal_plus_closeout.get("completed"),
                passed=goal_plus_closeout.get("completed") is True,
            )
            record_completion_check(
                goal_plus_state,
                "evidence_annotator_runtime",
                expected=_goal_plus_evidence_annotator_public(profile),
                actual=runtime_public.get("evidence_annotator"),
                passed=(
                    runtime_public.get("evidence_annotator")
                    == _goal_plus_evidence_annotator_public(profile)
                ),
            )
        _docker_checked(
            ["docker", "exec", container_id, "git", "-C", "/testbed", "add", "-N", "."]
        )
        patch = _docker_checked(
            [
                "docker",
                "exec",
                container_id,
                "git",
                "-C",
                "/testbed",
                "diff",
                "--binary",
                "--full-index",
                profile["tasks"][0]["base_commit"],
            ]
        )
        status = _docker_checked(
            [
                "docker",
                "exec",
                container_id,
                "git",
                "-C",
                "/testbed",
                "status",
                "--porcelain=v1",
            ]
        )
        patch_path = campaign / cell["patch_file"]
        patch_path.write_text(patch + ("\n" if patch else ""), encoding="utf-8")
        (cell_dir / "git-status.txt").write_text(
            status + ("\n" if status else ""), encoding="utf-8"
        )
    except Exception as error:
        agent_error = f"{type(error).__name__}: {error}"
        raise
    finally:
        if container_id:
            cleanup = _dispose_agent_container(
                container_id,
                retain=retain_container,
            )
            with _MANIFEST_LOCK:
                cell["agent"] = {
                    **(cell.get("agent") or {}),
                    "state": "failed" if agent_error else "finalizing",
                    "container": {
                        "id": container_id,
                        "name": container_name,
                        "retention_requested": retain_container,
                        "cleanup": cleanup,
                        "credentials_persisted": False,
                    },
                }
                if agent_error:
                    cell["agent"]["error"] = agent_error
                _save_manifest(campaign, manifest)
        resources.close()
        if container_id:
            if runtime_public:
                cell["agent"]["runtime"] = runtime_public
            _save_manifest(campaign, manifest)

    (cell_dir / "agent-events.jsonl").write_text(stdout, encoding="utf-8")
    (cell_dir / "agent-stderr.txt").write_text(stderr, encoding="utf-8")
    duration = time.monotonic() - started
    finalization_grace_seconds = (
        time.monotonic() - finalization_started
        if finalization_started is not None
        else None
    )
    patch_path = campaign / cell["patch_file"]
    patch_exists = patch_path.is_file() and bool(patch_path.read_text(encoding="utf-8").strip())
    goal_plus_complete = bool(
        goal_plus_state
        and (goal_plus_state.get("completion") or {}).get("passed") is True
    )
    agent_completed = bool(
        patch_exists
        and (
            goal_plus_complete
            if method in GOAL_PLUS_METHODS
            else returncode == 0
        )
    )
    return {
        "state": "completed" if agent_completed else "partial",
        "started_at": started_at,
        "completed_at": utc_now(),
        "runtime_seconds": trajectory_runtime_seconds,
        "total_runtime_seconds": duration,
        "setup_runtime_seconds": setup_runtime_seconds,
        "finalization_grace_seconds": finalization_grace_seconds,
        "returncode": returncode,
        "timed_out": timed_out,
        "patch_exists": patch_exists,
        "usage": extract_usage(stdout),
        "command": recorded_command,
        "runtime": runtime_public,
        "image_checkout": image_checkout,
        "goal_plus_closeout": goal_plus_closeout,
        "goal_plus": goal_plus_state,
        "container": {
            "id": container_id,
            "name": container_name,
            "retention_requested": retain_container,
            "cleanup": cleanup,
            "credentials_persisted": False,
            "network": network_name or "default",
        },
        "stdout_file": str((cell_dir / "agent-events.jsonl").relative_to(campaign)),
        "stderr_file": str((cell_dir / "agent-stderr.txt").relative_to(campaign)),
    }


def _official_evaluation(
    campaign: Path, manifest: dict[str, Any], cell: dict[str, Any]
) -> dict[str, Any]:
    if cell["evaluation"].get("calls") != 0:
        raise SweBenchContractError(
            "official evaluator has already been attempted; create a new campaign"
        )
    profile = _profile_for_cell(manifest["profile_snapshot"], cell)
    evaluator_dir = campaign / cell.get("evaluator_dir", "evaluator")
    patch = (campaign / cell["patch_file"]).read_text(encoding="utf-8")
    model_label = (
        f"bench-goal-plus-{cell['method']}-{cell['model']}".replace("/", "-")
    )
    predictions = [
        {
            "instance_id": cell["task_id"],
            "model_name_or_path": model_label,
            "model_patch": patch,
        }
    ]
    predictions_path = evaluator_dir / "predictions.json"
    predictions_path.write_text(
        json.dumps(predictions, indent=2) + "\n", encoding="utf-8"
    )
    run_id = manifest["campaign_id"]
    if len(manifest.get("cells") or []) > 1:
        run_id += "-" + hashlib.sha256(cell["task_id"].encode()).hexdigest()[:10]
    command = [
        str(ROOT / ".bench-env" / "venv" / "bin" / "python"),
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        str(
            campaign
            / cell.get(
                "evaluator_instances_file",
                manifest.get("dataset", {}).get(
                    "evaluator_instances_file", "evaluator/instances.json"
                ),
            )
        ),
        "--split",
        profile["dataset"]["split"],
        "--instance_ids",
        cell["task_id"],
        "--predictions_path",
        str(predictions_path),
        "--max_workers",
        "1",
        "--run_id",
        run_id,
        "--timeout",
        str(profile["evaluator_timeout_seconds"]),
        "--namespace",
        "swebench",
        "--cache_level",
        "instance",
        "--clean",
        "false",
        "--force_rebuild",
        "false",
        "--report_dir",
        str(evaluator_dir),
    ]
    evaluation = {
        "state": "running",
        "calls": 1,
        "started_at": utc_now(),
        "command": command,
        "dataset_revision": profile["dataset"]["revision"],
        "container_network": {
            "requested_mode": (
                "none"
                if profile.get("container_network")
                in ISOLATED_CONTAINER_NETWORK_POLICIES
                else "default"
            ),
            "mechanism": (
                "docker-sdk-sitecustomize"
                if profile.get("container_network")
                in ISOLATED_CONTAINER_NETWORK_POLICIES
                else "official-harness-default"
            ),
        },
    }
    with _MANIFEST_LOCK:
        cell["evaluation"] = dict(evaluation)
        _save_manifest(campaign, manifest)
    started = time.monotonic()
    timed_out = False
    try:
        child_environment = configure_temp_environment(dict(os.environ))
        pythonpath_entries = [str(SWEBENCH_ROOT)]
        if profile.get("container_network") in ISOLATED_CONTAINER_NETWORK_POLICIES:
            sitecustomize = evaluator_dir / "sitecustomize.py"
            sitecustomize.write_text(
                "from docker.models.containers import ContainerCollection\n"
                "_original_create = ContainerCollection.create\n"
                "def _offline_create(self, *args, **kwargs):\n"
                "    if 'network' not in kwargs and 'network_mode' not in kwargs:\n"
                "        kwargs['network_mode'] = 'none'\n"
                "    return _original_create(self, *args, **kwargs)\n"
                "ContainerCollection.create = _offline_create\n",
                encoding="utf-8",
            )
            pythonpath_entries.insert(0, str(evaluator_dir))
        existing_pythonpath = child_environment.get("PYTHONPATH")
        if existing_pythonpath:
            pythonpath_entries.append(existing_pythonpath)
        child_environment["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
        result = _run(
            command,
            cwd=evaluator_dir,
            timeout=profile["evaluator_timeout_seconds"] + 300,
            environment=dict(child_environment),
        )
        stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as error:
        stdout, stderr, returncode = _text(error.stdout), _text(error.stderr), None
        timed_out = True
    duration = time.monotonic() - started
    (evaluator_dir / "harness.stdout.txt").write_text(stdout, encoding="utf-8")
    (evaluator_dir / "harness.stderr.txt").write_text(stderr, encoding="utf-8")

    report_path = (
        evaluator_dir
        / "logs"
        / "run_evaluation"
        / run_id
        / model_label.replace("/", "__")
        / cell["task_id"]
        / "report.json"
    )
    raw_report: dict[str, Any] | None = None
    if report_path.is_file():
        raw_report = read_json(report_path)
    instance_report = (raw_report or {}).get(cell["task_id"])
    if not isinstance(instance_report, dict):
        instance_report = None
    instance_log = report_path.with_name("run_instance.log")
    log_text = (
        instance_log.read_text(encoding="utf-8", errors="replace")
        if instance_log.is_file()
        else ""
    )
    patch_applied: bool | None = None
    resolved: bool | None = None
    if instance_report is not None:
        value = instance_report.get("patch_successfully_applied")
        patch_applied = value if isinstance(value, bool) else None
        value = instance_report.get("resolved")
        resolved = value if isinstance(value, bool) else None
    elif "APPLY_PATCH_FAIL" in log_text:
        patch_applied = False
    evaluation.update(
        {
            "state": "completed" if instance_report is not None else "failed",
            "completed_at": utc_now(),
            "runtime_seconds": duration,
            "returncode": returncode,
            "timed_out": timed_out,
            "container_network": evaluation["container_network"],
            "report_file": (
                str(report_path.relative_to(campaign)) if report_path.is_file() else None
            ),
            "patch_applied": patch_applied,
            "resolved": resolved,
            "stdout_file": str(
                (evaluator_dir / "harness.stdout.txt").relative_to(campaign)
            ),
            "stderr_file": str(
                (evaluator_dir / "harness.stderr.txt").relative_to(campaign)
            ),
        }
    )
    return evaluation


def _execute_cell(
    campaign: Path,
    manifest: dict[str, Any],
    cell: dict[str, Any],
    scheduler: dict[str, Any],
) -> int:
    with _MANIFEST_LOCK:
        scheduler["active"] += 1
        scheduler["max_observed"] = max(
            scheduler["max_observed"], scheduler["active"]
        )
        cell["state"] = "running"
        cell["started_at"] = utc_now()
        _save_manifest(campaign, manifest)
    try:
        agent = _run_agent(campaign, manifest, cell)
        with _MANIFEST_LOCK:
            cell["agent"] = agent
            _save_manifest(campaign, manifest)
        cleanup = (cell["agent"].get("container") or {}).get("cleanup") or {}
        if not _container_disposition_isolated(cleanup):
            raise SweBenchContractError(
                "Agent container removal or stopped retention was not confirmed; "
                "official evaluation is blocked"
            )
        if cell["agent"]["patch_exists"]:
            evaluation = _official_evaluation(campaign, manifest, cell)
            with _MANIFEST_LOCK:
                cell["evaluation"] = evaluation
        with _MANIFEST_LOCK:
            goal_plus_completion = (
                ((cell["agent"].get("goal_plus") or {}).get("completion") or {})
                if cell.get("method") in GOAL_PLUS_METHODS
                else {"passed": True, "reason": None}
            )
            score_complete = cell["evaluation"].get("state") == "completed"
            topology_complete = goal_plus_completion.get("passed") is True
            if score_complete and topology_complete:
                cell["state"] = "completed"
                exit_code = 0
            else:
                cell["state"] = "partial"
                reasons = []
                if not cell["agent"]["patch_exists"]:
                    reasons.append("Agent did not produce a patch")
                elif not score_complete:
                    reasons.append("official evaluator did not produce a valid report")
                if not topology_complete:
                    reasons.append(
                        str(
                            goal_plus_completion.get("reason")
                            or "Goal Plus completion evidence is incomplete"
                        )
                    )
                cell["incomplete_reason"] = "; ".join(reasons)
                exit_code = 1
    except Exception as error:
        with _MANIFEST_LOCK:
            cell["state"] = "failed"
            cell["error"] = f"{type(error).__name__}: {error}"
        exit_code = 1
    finally:
        with _MANIFEST_LOCK:
            cell["completed_at"] = utc_now()
            scheduler["active"] -= 1
            scheduler["completed"] += 1
            _save_manifest(campaign, manifest)
    return exit_code


def execute_campaign(campaign: Path) -> int:
    manifest = _manifest(campaign)
    if manifest["state"] != "prepared":
        raise SweBenchContractError(
            f"campaign must be prepared, got {manifest['state']!r}"
        )
    manifest["state"] = "running"
    manifest["started_at"] = utc_now()
    scheduler = {
        "requested": int(manifest["budget"].get("cell_concurrency", 1)),
        "active": 0,
        "max_observed": 0,
        "completed": 0,
    }
    manifest["scheduler"] = scheduler
    network: dict[str, Any] | None = None
    if (manifest.get("profile_snapshot") or {}).get(
        "container_network"
    ) in ISOLATED_CONTAINER_NETWORK_POLICIES:
        network = _create_internal_network(manifest["campaign_id"])
        manifest["container_network_runtime"] = network
    _save_manifest(campaign, manifest)

    exit_code = 0
    try:
        with ThreadPoolExecutor(max_workers=scheduler["requested"]) as executor:
            futures = {
                executor.submit(
                    _execute_cell, campaign, manifest, cell, scheduler
                ): cell["cell_id"]
                for cell in manifest["cells"]
            }
            for future in as_completed(futures):
                exit_code = max(exit_code, future.result())
    finally:
        if network is not None:
            manifest["container_network_runtime"] = _dispose_internal_network(network)

    states = {str(cell.get("state")) for cell in manifest["cells"]}
    network_removed = (
        network is None
        or manifest["container_network_runtime"].get("removed") is True
    )
    if states == {"completed"} and network_removed:
        manifest["state"] = "completed"
    elif "failed" in states:
        manifest["state"] = "failed"
        exit_code = 1
    else:
        manifest["state"] = "partial"
        exit_code = 1
        if not network_removed:
            manifest["incomplete_reason"] = "campaign internal network was not removed"
    manifest["completed_at"] = utc_now()
    _save_manifest(campaign, manifest)
    print(json.dumps(status_payload(campaign), indent=2, ensure_ascii=False))
    return exit_code


def status_payload(campaign: Path) -> dict[str, Any]:
    manifest = _manifest(campaign)
    counts: dict[str, int] = {}
    for cell in manifest.get("cells", []):
        state = str(cell.get("state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    return {
        "schema_version": 1,
        "campaign_id": manifest["campaign_id"],
        "benchmark_id": manifest["benchmark_id"],
        "state": manifest["state"],
        "terminal": manifest["state"] in TERMINAL_STATES,
        "counts": counts,
        "method": manifest["methods"][0],
        "model": manifest["model"],
        "agent_provider": manifest.get("agent_provider"),
        "budget": manifest["budget"],
        "cells": [
            {
                "cell_id": cell["cell_id"],
                "task_id": cell["task_id"],
                "state": cell["state"],
                "agent_state": (cell.get("agent") or {}).get("state"),
                "evaluation_state": (cell.get("evaluation") or {}).get("state"),
                "evaluator_calls": (cell.get("evaluation") or {}).get("calls"),
                "resolved": (cell.get("evaluation") or {}).get("resolved"),
                "actual_subagent_count": (
                    ((cell.get("agent") or {}).get("goal_plus") or {}).get(
                        "actual_subagent_count"
                    )
                ),
                "goal_plus_completion": (
                    ((cell.get("agent") or {}).get("goal_plus") or {}).get(
                        "completion"
                    )
                ),
                "incomplete_reason": cell.get("incomplete_reason"),
                "error": cell.get("error"),
                "retained_container": (
                    (cell.get("agent") or {}).get("container")
                    if (
                        ((cell.get("agent") or {}).get("container") or {})
                        .get("cleanup", {})
                        .get("retained")
                    )
                    else None
                ),
            }
            for cell in manifest.get("cells", [])
        ],
    }
