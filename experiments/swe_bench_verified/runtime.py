"""Campaign preparation, container Agent execution, and official evaluation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from bench_goal_plus.cell_scheduler import execute_cell_queue as execute_bounded_cell_queue
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
from .checkout import CHECKOUT_PROBE_SCRIPT, validate_checkout_probe
from .environment import (
    CODEX_RUNTIME_TMPFS,
    codex_container_responses_probe,
    goal_plus_install_script,
    goal_plus_runtime_environment,
    openai_responses_probe,
    resolve_codex_runtime,
    resolve_goal_plus_runtime,
    resolve_pi_runtime,
    routed_codex_runtime,
    routed_pi_runtime,
)
from .goal_plus_evidence import collect_goal_plus_state, record_completion_check


MANIFEST = "campaign.json"
CONTROLLER = "controller.json"
TERMINAL_STATES = {"completed", "partial", "failed"}
HIDDEN_INSTANCE_FIELDS = {
    "patch",
    "test_patch",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
}


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


def _load_pinned_instances(profile: dict[str, Any]) -> list[dict[str, Any]]:
    _configure_huggingface_cache()
    from datasets import load_dataset

    dataset = load_dataset(
        profile["dataset"]["name"],
        split=profile["dataset"]["split"],
        revision=profile["dataset"]["revision"],
    )
    requested = list(profile["task_ids"])
    matches: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in requested}
    for row in dataset:
        task_id = row.get("instance_id")
        if task_id in matches:
            matches[str(task_id)].append(dict(row))
    for task_id, rows in matches.items():
        if len(rows) != 1:
            raise SweBenchContractError(
                f"pinned dataset returned {len(rows)} rows for {task_id}"
            )
    return [matches[task_id][0] for task_id in requested]


def _load_pinned_instance(profile: dict[str, Any]) -> dict[str, Any]:
    """Compatibility helper for existing one-task profiles and focused tests."""

    if len(profile["task_ids"]) != 1:
        raise SweBenchContractError("use _load_pinned_instances for multi-task profiles")
    return _load_pinned_instances(profile)[0]


def _validate_instance_image(
    instance: dict[str, Any],
    profile: dict[str, Any],
    task: dict[str, Any] | None = None,
) -> None:
    from swebench.harness.test_spec.test_spec import make_test_spec

    task = task or profile["tasks"][0]
    if instance.get("repo") != task["repo"]:
        raise SweBenchContractError("dataset repo does not match the pinned profile")
    if instance.get("base_commit") != task["base_commit"]:
        raise SweBenchContractError(
            "dataset base_commit does not match the pinned profile"
        )
    spec = make_test_spec(instance, namespace="swebench")
    if spec.instance_image_key != task["image"]:
        raise SweBenchContractError(
            "official harness image key does not match the local inventory tag: "
            f"{spec.instance_image_key!r} != {task['image']!r}"
        )


def _visible_task(
    instance: dict[str, Any],
    profile: dict[str, Any],
    task: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task = task or profile["tasks"][0]
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
    cells: list[dict[str, Any]] = []
    visible_fields: set[str] = set()
    method = profile["methods"][0]
    for instance, task in zip(instances, profile["tasks"], strict=True):
        _validate_instance_image(instance, profile, task)
        visible = _visible_task(instance, profile, task)
        visible_fields.update(visible)
        cell_id = f"{method}--{instance['instance_id']}"
        cell_dir = destination / "cells" / cell_id
        cell_dir.mkdir(parents=True)
        write_json(cell_dir / "task.json", visible)
        cell = {
            "cell_id": cell_id,
            "task_id": instance["instance_id"],
            "repo": instance["repo"],
            "base_commit": instance["base_commit"],
            "image": task["image"],
            "method": method,
            "model": profile["model"],
            "reasoning_effort": profile["reasoning_effort"],
            "state": "prepared",
            "cell_file": str((cell_dir / "cell.json").relative_to(destination)),
            "task_file": str((cell_dir / "task.json").relative_to(destination)),
            "patch_file": str((cell_dir / "model.patch").relative_to(destination)),
            "evaluator_dir": str(
                (evaluator_dir / cell_id).relative_to(destination)
            ),
            "agent": {"state": "pending"},
            "evaluation": {"state": "pending", "calls": 0},
        }
        cells.append(cell)
    evaluator_instances = evaluator_dir / "instances.json"
    evaluator_instances.write_text(
        json.dumps(instances, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    evaluator_instances.chmod(0o600)

    source_commit = _git_value(ROOT, "rev-parse", "HEAD")
    swebench_commit = _git_value(SWEBENCH_ROOT, "rev-parse", "HEAD")
    goal_plus_commit = (
        _git_value(GOAL_PLUS_ROOT, "rev-parse", "HEAD")
        if profile["methods"][0] == "goal-plus-pi"
        else None
    )
    provider_contract = (
        dict(profile["agent_provider"])
        if profile.get("agent_provider") is not None
        else {
            "auth_mode": "provider-api",
            "provider": profile["model"].partition("/")[0],
        }
    )
    for cell in cells:
        cell["agent_provider"] = provider_contract
        write_json(destination / cell["cell_file"], cell)
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
        "agent_provider": provider_contract,
        "budget": {
            "wall_time_seconds": profile["wall_time_seconds"],
            "live_search_concurrency": 1,
            "cell_concurrency": profile["cell_concurrency"],
            "attempts": 1,
        },
        "container_retention": {
            "requested": profile["retain_containers"],
            "scope": "agent",
            "evaluator_container_owned_by": "official-swebench-harness",
        },
        "dataset": {
            **profile["dataset"],
            "task_ids": profile["task_ids"],
            "evaluator_instances_file": str(
                evaluator_instances.relative_to(destination)
            ),
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
    write_json(destination / MANIFEST, manifest)
    write_json(
        destination / CONTROLLER,
        {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "state": "prepared",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "pid": None,
            "pgid": None,
            "command": None,
            "log_file": "controller.log",
            "returncode": None,
        },
    )
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
    manifest["updated_at"] = utc_now()
    write_json(campaign / MANIFEST, manifest)


def _controller(campaign: Path) -> dict[str, Any]:
    path = campaign / CONTROLLER
    if path.is_file():
        payload = read_json(path)
        if payload.get("schema_version") != 1:
            raise SweBenchContractError("unsupported SWE-bench controller schema")
        return payload
    manifest = _manifest(campaign)
    return {
        "schema_version": 1,
        "campaign_id": manifest["campaign_id"],
        "state": manifest["state"],
        "created_at": manifest.get("created_at") or utc_now(),
        "updated_at": utc_now(),
        "pid": None,
        "pgid": None,
        "command": None,
        "log_file": "controller.log",
        "returncode": None,
    }


def _save_controller(campaign: Path, controller: dict[str, Any]) -> None:
    controller["updated_at"] = utc_now()
    write_json(campaign / CONTROLLER, controller)


def process_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cell_file(campaign: Path, cell: dict[str, Any]) -> Path:
    if cell.get("cell_file"):
        return campaign / str(cell["cell_file"])
    if cell.get("task_file"):
        return (campaign / str(cell["task_file"])).parent / "cell.json"
    if cell.get("cell_id"):
        return campaign / "cells" / str(cell["cell_id"]) / "cell.json"
    raise SweBenchContractError("cell has no durable path")


def _save_cell(campaign: Path, cell: dict[str, Any]) -> None:
    cell["updated_at"] = utc_now()
    write_json(_cell_file(campaign, cell), cell)


def _persist_runtime_state(
    campaign: Path,
    manifest: dict[str, Any],
    cell: dict[str, Any],
    *,
    persist_manifest: bool,
) -> None:
    if persist_manifest:
        _save_manifest(campaign, manifest)
    else:
        _save_cell(campaign, cell)


def _docker_checked(command: list[str], *, timeout: int = 120) -> str:
    result = _run(command, timeout=timeout)
    if result.returncode != 0:
        raise SweBenchContractError(
            result.stderr.strip() or result.stdout.strip() or "Docker command failed"
        )
    return result.stdout.rstrip("\n")


def _container_name(campaign_id: str, cell_id: str) -> str:
    digest = hashlib.sha256(f"{campaign_id}:{cell_id}".encode()).hexdigest()[:16]
    return f"bgp-swe-agent-{digest}"


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
    cell: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    method = profile["methods"][0]
    cell_id = str((cell or {}).get("cell_id") or method)
    name = _container_name(campaign_id, cell_id)
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
        "--label",
        f"bench-goal-plus.cell={cell_id}",
        "--workdir",
        "/testbed",
        "--tmpfs",
        "/opt/agent-tmp:rw,nosuid,nodev,size=256m",
    ]
    if method == "plain-codex":
        runtime = runtime or resolve_codex_runtime(profile)
        if (
            not runtime["archive_present"]
            or not runtime["credential_present"]
            or not runtime["api_base_url"]
            or not runtime.get("runtime_api_base_url")
        ):
            raise SweBenchContractError(
                "Codex runtime archive or OpenAI-compatible provider is missing"
            )
        command.extend(
            [
                "--tmpfs",
                "/opt/codex-home:rw,nosuid,nodev,size=32m",
                "--tmpfs",
                CODEX_RUNTIME_TMPFS,
                "--mount",
                f"type=bind,src={runtime['archive']},dst=/opt/runtime/codex.tgz,readonly",
            ]
        )
    else:
        runtime = runtime or (
            resolve_goal_plus_runtime(profile)
            if method == "goal-plus-pi"
            else resolve_pi_runtime(profile)
        )
        if not runtime["credential_present"]:
            raise SweBenchContractError(
                f"Pi credential for provider {runtime['provider']} is missing"
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
        if method == "goal-plus-pi":
            required_assets = (
                "goal_plus_root",
                "goal_plus_dependency_lock",
                "goal_plus_visible_verifier",
                "goal_plus_controller",
                "goal_plus_pip_cache",
            )
            missing = [
                name
                for name in required_assets
                if not isinstance(runtime.get(name), Path)
                or not runtime[name].exists()
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
                    f"src={runtime['goal_plus_visible_verifier']},"
                    "dst=/opt/swebench-visible-test-verifier.py,readonly",
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
    if runtime is None:
        raise AssertionError("Agent runtime was not resolved")
    command.extend([(cell or profile["tasks"][0])["image"], "sleep", "infinity"])
    container_id = _docker_checked(command)
    _docker_checked(["docker", "start", container_id])
    return container_id, runtime


def _initialize_agent_container(
    container_id: str,
    profile: dict[str, Any],
    runtime: dict[str, Any],
    *,
    cell: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base_commit = (cell or profile["tasks"][0])["base_commit"]
    probe_output = _docker_checked(
        [
            "docker",
            "exec",
            container_id,
            "sh",
            "-c",
            CHECKOUT_PROBE_SCRIPT,
            "swe-bench-checkout-probe",
            base_commit,
        ]
    )
    checkout = validate_checkout_probe(probe_output, base_commit)
    if not checkout["passed"]:
        raise SweBenchContractError(
            "Agent image checkout is not a valid official baseline: "
            + str(checkout["failure_reason"])
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
    if profile["methods"][0] == "plain-codex":
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
    elif profile["methods"][0] == "goal-plus-pi":
        environment = goal_plus_runtime_environment()
        install_command = ["docker", "exec"]
        for name, value in environment.items():
            install_command.extend(["-e", f"{name}={value}"])
        if os.environ.get("PIP_INDEX_URL"):
            install_command.extend(["-e", "PIP_INDEX_URL"])
        install_command.extend(
            [
                container_id,
                "sh",
                "-lc",
                goal_plus_install_script()
                + " && python -c \"import fastmcp, goal_plus, plotly, pydantic\""
                + " && pi --version",
            ]
        )
        _docker_checked(install_command, timeout=600)
        _docker_checked(
            [
                "docker",
                "exec",
                container_id,
                "sh",
                "-lc",
                "mkdir -p /testbed/.goal-plus-verifiers && "
                "printf '\\n.gp/\\n.goal-plus-verifiers/\\n' "
                ">> /testbed/.git/info/exclude && "
                "cp /opt/swebench-visible-test-verifier.py "
                "/testbed/.goal-plus-verifiers/visible_test_verifier.py && "
                "chmod 0555 "
                "/testbed/.goal-plus-verifiers/visible_test_verifier.py",
            ]
        )
    return {
        **checkout,
        "goal_plus_initialized": profile["methods"][0] == "goal-plus-pi",
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
    return (
        "/goal-plus mode=autonomous Solve the public repository issue below in "
        "/testbed. Treat this as verifier-guided code repair and enter Search Mode. "
        "Do not inspect benchmark metadata, hidden tests, dataset rows, reports, or "
        "answer patches. The official SWE-bench harness remains hidden and runs only "
        "after this Goal Plus session.\n\n"
        "Freeze exactly one SearchSpec discovered from the public issue and repository. "
        "Use source_path=/testbed, metric_name=visible_test_score, direction=maximize, "
        "strategy.name=random, strategy.worker_host=pi-rpc, and "
        "strategy.orchestration_mode=parallel_loops. Set budget.max_parallel="
        f"{profile['concurrency']} and do not set the deprecated max_candidates field. "
        "Set strategy.worker_budget.max_runtime_seconds="
        f"{goal_plus['worker_runtime_seconds']} and "
        "strategy.config.closeout_reserve_seconds="
        f"{goal_plus['closeout_reserve_seconds']}. Use one fixed initial candidate and "
        "continue the same bound Pi worker session; do not create replacement lanes.\n\n"
        "Choose a focused visible test command using only the public issue and repository. "
        "Both process and promotion ranking verifiers must invoke the materialized "
        "artifact /testbed/.goal-plus-verifiers/visible_test_verifier.py with cwd=/testbed "
        "and the candidate-relative command: "
        "python .goal-plus-verifiers/visible_test_verifier.py "
        f"--timeout-seconds {goal_plus['visible_verifier_timeout_seconds']} -- "
        "<your visible test command>. Include that wrapper path in verifier_artifacts. "
        "Keep .gp and .goal-plus-verifiers outside the editable artifact surface. "
        "After worker completion, close the pool, select and promote verifier-backed "
        "Evidence, apply the promotion patch to /testbed, record the Search result, "
        "and finish the Goal Plus record.\n\n"
        f"Public issue:\n{task['problem_statement']}\n"
    )


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
            "HOME": "/opt/pi-home",
            "PI_CODING_AGENT_DIR": "/opt/pi-home/.pi/agent",
            "GOAL_PLUS_ROOT": "/testbed/.gp",
            "GOAL_PLUS_SEARCH_ROOT": "/testbed/.gp",
            "GOAL_PLUS_SOURCE_PATH": "/opt/goal-plus",
            "GOAL_PLUS_PI_MODEL": profile["model"],
            "GOAL_PLUS_EVIDENCE_ANNOTATOR_DISABLED": "1",
            "GOAL_PLUS_OUTER_DEADLINE_AT": str(runtime["outer_deadline_at"]),
            "PYTHONDONTWRITEBYTECODE": "1",
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
        command.extend(
            [
                "-e",
                credential_env,
                container_id,
                "sh",
                "-lc",
                'export PATH=/opt/goal-plus-bin:/opt/node/bin:$PATH; exec "$@"',
                "swe-bench-goal-plus",
                "/opt/node/bin/node",
                "/opt/pi/dist/cli.js",
                "--mode",
                "json",
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
    environment = {
        **goal_plus_runtime_environment(),
        "HOME": "/opt/pi-home",
        "PI_CODING_AGENT_DIR": "/opt/pi-home/.pi/agent",
        "GOAL_PLUS_ROOT": "/testbed/.gp",
        "GOAL_PLUS_SEARCH_ROOT": "/testbed/.gp",
        "GOAL_PLUS_SOURCE_PATH": "/opt/goal-plus",
        "GOAL_PLUS_PI_MODEL": profile["model"],
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_DISABLED": "1",
        "GOAL_PLUS_OUTER_DEADLINE_AT": str(runtime["outer_deadline_at"]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    command = ["docker", "exec"]
    for name, value in environment.items():
        command.extend(["-e", f"{name}={value}"])
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


def _run_agent(
    campaign: Path,
    manifest: dict[str, Any],
    cell: dict[str, Any],
    *,
    persist_manifest: bool = True,
) -> dict[str, Any]:
    profile = manifest["profile_snapshot"]
    method = profile["methods"][0]
    task = read_json(campaign / cell["task_file"])
    prompt = (
        build_goal_plus_prompt(task, profile)
        if method == "goal-plus-pi"
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
        manifest["campaign_id"], str(cell.get("cell_id") or method)
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
        if method == "plain-codex":
            runtime = resources.enter_context(
                routed_codex_runtime(profile, cell_dir)
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
        else:
            if profile.get("agent_provider") is not None:
                runtime = resources.enter_context(
                    routed_pi_runtime(
                        profile,
                        cell_dir,
                        goal_plus=method == "goal-plus-pi",
                    )
                )
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
                runtime = (
                    resolve_goal_plus_runtime(profile)
                    if method == "goal-plus-pi"
                    else resolve_pi_runtime(profile)
                )
                host_probe = None
        container_id, runtime = _create_agent_container(
            manifest["campaign_id"], profile, runtime, cell=cell
        )
        cell["agent"] = {
            "state": "running",
            "container": {
                "id": container_id,
                "name": container_name,
                "retention_requested": retain_container,
                "credentials_persisted": False,
            },
        }
        _persist_runtime_state(
            campaign,
            manifest,
            cell,
            persist_manifest=persist_manifest,
        )
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
                        "evidence_annotator": "disabled",
                    }
                )
        image_checkout = _initialize_agent_container(
            container_id, profile, runtime, cell=cell
        )
        if method == "plain-codex":
            container_probe = codex_container_responses_probe(
                container_id,
                runtime,
                model=str(profile["model"]),
                existing_container=True,
            )
            runtime_public["container_responses_probe"] = container_probe
            if not container_probe["passed"]:
                raise SweBenchContractError(
                    "OpenAI-compatible Responses probe failed in the Agent container"
                )
        elif runtime.get("custom_provider"):
            container_probe = codex_container_responses_probe(
                container_id,
                runtime,
                model=str(runtime["model_id"]),
                existing_container=True,
            )
            runtime_public["container_responses_probe"] = container_probe
            if not container_probe["passed"]:
                raise SweBenchContractError(
                    "Pi OpenAI-compatible Responses probe failed in the Agent container"
                )
        setup_runtime_seconds = time.monotonic() - started
        if method == "goal-plus-pi":
            runtime["outer_deadline_at"] = (
                datetime.now(timezone.utc)
                + timedelta(seconds=profile["wall_time_seconds"])
            ).isoformat()
            runtime["main_session_id"] = "swe-bench-main-" + hashlib.sha256(
                f"{manifest['campaign_id']}:{cell.get('cell_id') or method}".encode(
                    "utf-8"
                )
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
                input_text=None if method == "goal-plus-pi" else prompt,
                timeout=profile["wall_time_seconds"],
            )
            stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
            trajectory_runtime_seconds = time.monotonic() - trajectory_started
        except subprocess.TimeoutExpired as error:
            stdout = _text(error.stdout)
            stderr = _text(error.stderr)
            timed_out = True
            trajectory_runtime_seconds = time.monotonic() - trajectory_started
            _docker_checked(["docker", "stop", "--time", "10", container_id], timeout=30)
            _docker_checked(["docker", "start", container_id], timeout=30)

        finalization_started = time.monotonic()
        if method == "goal-plus-pi":
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
                "evidence_annotator_disabled",
                expected="disabled",
                actual=runtime_public.get("evidence_annotator"),
                passed=runtime_public.get("evidence_annotator") == "disabled",
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
                str(image_checkout["patch_base_commit"]),
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
            _persist_runtime_state(
                campaign,
                manifest,
                cell,
                persist_manifest=persist_manifest,
            )
        resources.close()

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
            if method == "goal-plus-pi"
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
        },
        "stdout_file": str((cell_dir / "agent-events.jsonl").relative_to(campaign)),
        "stderr_file": str((cell_dir / "agent-stderr.txt").relative_to(campaign)),
    }


def _official_evaluation(
    campaign: Path,
    manifest: dict[str, Any],
    cell: dict[str, Any],
    *,
    persist_manifest: bool = True,
) -> dict[str, Any]:
    if cell["evaluation"].get("calls") != 0:
        raise SweBenchContractError(
            "official evaluator has already been attempted; create a new campaign"
        )
    profile = manifest["profile_snapshot"]
    evaluator_dir = campaign / str(cell.get("evaluator_dir") or "evaluator")
    evaluator_dir.mkdir(parents=True, exist_ok=True)
    instances_file = campaign / str(
        (manifest.get("dataset") or {}).get(
            "evaluator_instances_file", "evaluator/instances.json"
        )
    )
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
    run_id = str(manifest["campaign_id"])
    if cell.get("evaluator_dir"):
        run_id += "-" + hashlib.sha256(str(cell["cell_id"]).encode()).hexdigest()[:12]
    command = [
        str(ROOT / ".bench-env" / "venv" / "bin" / "python"),
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        str(instances_file),
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
    }
    cell["evaluation"] = evaluation
    _persist_runtime_state(
        campaign,
        manifest,
        cell,
        persist_manifest=persist_manifest,
    )
    started = time.monotonic()
    timed_out = False
    try:
        child_environment = configure_temp_environment(dict(os.environ))
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


def _execute_campaign_cell(campaign: Path, cell_id: str) -> int:
    manifest = _manifest(campaign)
    summary = next(
        (cell for cell in manifest["cells"] if cell["cell_id"] == cell_id), None
    )
    if summary is None:
        raise SweBenchContractError(f"unknown campaign cell: {cell_id}")
    cell = read_json(_cell_file(campaign, summary))
    cell.update({"state": "running", "started_at": utc_now()})
    _save_cell(campaign, cell)
    exit_code = 0
    try:
        cell["agent"] = _run_agent(
            campaign,
            manifest,
            cell,
            persist_manifest=False,
        )
        _save_cell(campaign, cell)
        cleanup = (cell["agent"].get("container") or {}).get("cleanup") or {}
        if not _container_disposition_isolated(cleanup):
            raise SweBenchContractError(
                "Agent container removal or stopped retention was not confirmed; "
                "official evaluation is blocked"
            )
        if cell["agent"]["patch_exists"]:
            cell["evaluation"] = _official_evaluation(
                campaign,
                manifest,
                cell,
                persist_manifest=False,
            )
        goal_plus_completion = (
            ((cell["agent"].get("goal_plus") or {}).get("completion") or {})
            if cell.get("method") == "goal-plus-pi"
            else {"passed": True, "reason": None}
        )
        score_complete = cell["evaluation"].get("state") == "completed"
        topology_complete = goal_plus_completion.get("passed") is True
        if score_complete and topology_complete:
            cell["state"] = "completed"
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
        cell["state"] = "failed"
        cell["error"] = f"{type(error).__name__}: {error}"
        exit_code = 1
    cell["completed_at"] = utc_now()
    _save_cell(campaign, cell)
    return exit_code


def _sync_manifest_cell(
    campaign: Path, manifest: dict[str, Any], cell_id: str
) -> dict[str, Any]:
    for index, summary in enumerate(manifest["cells"]):
        if summary["cell_id"] == cell_id:
            cell = read_json(_cell_file(campaign, summary))
            manifest["cells"][index] = cell
            return cell
    raise SweBenchContractError(f"unknown campaign cell: {cell_id}")


def _execute_campaign_body(campaign: Path) -> int:
    manifest = _manifest(campaign)
    if manifest["state"] != "prepared":
        raise SweBenchContractError(
            f"campaign must be prepared, got {manifest['state']!r}"
        )
    cell_concurrency = int(
        (manifest.get("budget") or {}).get("cell_concurrency") or 1
    )
    if cell_concurrency > len(manifest["cells"]):
        raise SweBenchContractError("C cannot exceed the number of campaign cells")

    for cell in manifest["cells"]:
        if not cell.get("cell_file"):
            cell["cell_file"] = str(
                Path("cells") / str(cell["cell_id"]) / "cell.json"
            )
        cell_path = _cell_file(campaign, cell)
        if not cell_path.is_file():
            write_json(cell_path, cell)

    manifest["state"] = "running"
    manifest["started_at"] = utc_now()
    manifest["scheduler"] = {
        "cell_concurrency": cell_concurrency,
        "active_cell_ids": [],
        "max_active_observed": 0,
        "events": [],
    }
    _save_manifest(campaign, manifest)

    executor = ThreadPoolExecutor(
        max_workers=cell_concurrency,
        thread_name_prefix="swe-bench-cell",
    )
    try:
        def record_active(active: Any) -> None:
            cell_ids = sorted(active)
            scheduler = manifest["scheduler"]
            scheduler["active_cell_ids"] = cell_ids
            scheduler["max_active_observed"] = max(
                int(scheduler["max_active_observed"]), len(cell_ids)
            )
            scheduler["events"].append(
                {"at": utc_now(), "active_cell_ids": cell_ids}
            )
            _save_manifest(campaign, manifest)

        def start(cell: dict[str, Any]) -> Future[int]:
            return executor.submit(
                _execute_campaign_cell, campaign, str(cell["cell_id"])
            )

        def finish(future: Future[int], _stopping: bool) -> int:
            completed_id = next(
                cell_id
                for cell_id, active_future in active_futures.items()
                if active_future is future
            )
            try:
                returncode = int(future.result())
            except Exception as error:
                failed = read_json(
                    _cell_file(
                        campaign,
                        next(
                            cell
                            for cell in manifest["cells"]
                            if cell["cell_id"] == completed_id
                        ),
                    )
                )
                failed.update(
                    {
                        "state": "failed",
                        "completed_at": utc_now(),
                        "controller_error": f"{type(error).__name__}: {error}",
                    }
                )
                _save_cell(campaign, failed)
                returncode = 1
            _sync_manifest_cell(campaign, manifest, completed_id)
            _save_manifest(campaign, manifest)
            return returncode

        def fail(cell: dict[str, Any], error: Exception) -> int:
            failed = read_json(_cell_file(campaign, cell))
            failed.update(
                {
                    "state": "failed",
                    "completed_at": utc_now(),
                    "launch_error": f"{type(error).__name__}: {error}",
                }
            )
            _save_cell(campaign, failed)
            _sync_manifest_cell(campaign, manifest, str(cell["cell_id"]))
            _save_manifest(campaign, manifest)
            return 1

        active_futures: dict[str, Future[int]] = {}

        def track_active(active: Any) -> None:
            active_futures.clear()
            active_futures.update(active)
            record_active(active)

        scheduler_returncode = execute_bounded_cell_queue(
            list(manifest["cells"]),
            concurrency=cell_concurrency,
            item_id=lambda cell: str(cell["cell_id"]),
            start=start,
            poll=lambda future: future.done(),
            finish=finish,
            fail=fail,
            record_active=track_active,
            stop_requested=lambda: False,
        )
    finally:
        executor.shutdown(wait=True)

    for cell in list(manifest["cells"]):
        _sync_manifest_cell(campaign, manifest, str(cell["cell_id"]))
    states = {str(cell.get("state")) for cell in manifest["cells"]}
    if states == {"completed"}:
        manifest["state"] = "completed"
        exit_code = 0
    elif states & {"completed", "partial"}:
        manifest["state"] = "partial"
        exit_code = scheduler_returncode or 1
    else:
        manifest["state"] = "failed"
        exit_code = scheduler_returncode or 1
    manifest["completed_at"] = utc_now()
    manifest["scheduler"]["returncode"] = scheduler_returncode
    _save_manifest(campaign, manifest)
    return exit_code


def execute_campaign(campaign: Path) -> int:
    manifest = _manifest(campaign)
    if manifest["state"] != "prepared":
        raise SweBenchContractError(
            f"campaign must be prepared, got {manifest['state']!r}"
        )
    controller = _controller(campaign)
    controller.update(
        {
            "state": "running",
            "started_at": controller.get("started_at") or utc_now(),
            "pid": os.getpid(),
            "pgid": os.getpgrp(),
            "returncode": None,
        }
    )
    _save_controller(campaign, controller)
    try:
        returncode = _execute_campaign_body(campaign)
    except Exception as error:
        failed_manifest = _manifest(campaign)
        if failed_manifest.get("state") in {"prepared", "running"}:
            failed_manifest.update(
                {
                    "state": "failed",
                    "completed_at": utc_now(),
                    "controller_error": f"{type(error).__name__}: {error}",
                }
            )
            _save_manifest(campaign, failed_manifest)
        controller.update(
            {
                "state": "failed",
                "finished_at": utc_now(),
                "returncode": 1,
                "error": f"{type(error).__name__}: {error}",
            }
        )
        _save_controller(campaign, controller)
        raise

    final_state = _manifest(campaign)["state"]
    controller.update(
        {
            "state": final_state,
            "finished_at": utc_now(),
            "returncode": returncode,
        }
    )
    _save_controller(campaign, controller)
    print(json.dumps(status_payload(campaign), indent=2, ensure_ascii=False))
    return returncode


def launch(campaign: Path, *, detach: bool) -> int:
    manifest = _manifest(campaign)
    controller = _controller(campaign)
    if process_alive(controller.get("pid")):
        raise SweBenchContractError(
            f"campaign controller is already running: {controller['pid']}"
        )
    if manifest["state"] != "prepared":
        raise SweBenchContractError(
            f"campaign must be prepared, got {manifest['state']!r}"
        )
    if not detach:
        return execute_campaign(campaign)

    command = [
        sys.executable,
        str(ROOT / "experiments" / "swe_bench_verified" / "experiment.py"),
        "_execute",
        "--campaign",
        manifest["campaign_id"],
    ]
    controller.update(
        {
            "state": "launching",
            "launched_at": controller.get("launched_at") or utc_now(),
            "command": command,
            "log_file": "controller.log",
            "returncode": None,
        }
    )
    _save_controller(campaign, controller)
    log = (campaign / "controller.log").open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=dict(configure_temp_environment(dict(os.environ))),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except Exception as error:
        controller.update(
            {
                "state": "failed",
                "finished_at": utc_now(),
                "returncode": 1,
                "error": f"{type(error).__name__}: {error}",
            }
        )
        _save_controller(campaign, controller)
        raise
    finally:
        log.close()

    controller = _controller(campaign)
    controller.update(
        {
            "pid": process.pid,
            "pgid": process.pid,
            "command": command,
            "launched_at": controller.get("launched_at") or utc_now(),
        }
    )
    if controller.get("state") in {"prepared", "launching"}:
        controller["state"] = "launching"
    _save_controller(campaign, controller)
    print(json.dumps({"pid": process.pid, "campaign": str(campaign)}))
    return 0


def status_payload(campaign: Path) -> dict[str, Any]:
    manifest = _manifest(campaign)
    controller = _controller(campaign)
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
        "controller": {
            "state": controller.get("state"),
            "pid": controller.get("pid"),
            "pgid": controller.get("pgid"),
            "alive": process_alive(controller.get("pid")),
            "returncode": controller.get("returncode"),
            "log_file": controller.get("log_file"),
        },
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
