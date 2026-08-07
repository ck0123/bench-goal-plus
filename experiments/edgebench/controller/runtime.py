"""SForge commands, process resources, cell queue, and campaign lifecycle."""

from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from bench_runtime_paths import configure_temp_environment
from bench_goal_plus.cell_scheduler import execute_cell_queue as execute_bounded_cell_queue

from . import io
from .context import current_paths
from .environment import (
    append_no_proxy,
    authenticated_api_probe,
    bridged_base_url,
    default_route_ipv4,
    docker_http_probe,
    judge_server_environment,
    loopback_api_target,
    resolve_agent_api_config,
    start_socket_bridge,
    task_images,
)
from .profiles import GOAL_PLUS_METHODS, METHODS, api_protocol_for_methods


EVIDENCE_ANNOTATOR_PROVIDER_ID = "edgebench-evidence"


def build_sforge_command(destination: Path, cell: dict[str, Any]) -> list[str]:
    paths = current_paths()
    cell_path = destination / "cells" / cell["cell_id"]
    command = [
        str(paths.sforge),
        "--log-dir",
        str(cell_path / "sforge"),
        "--tasks-dir",
        str(paths.tasks_dir),
        "--silent",
        "run",
        "--backend",
        str(cell.get("backend") or "docker"),
        "--task",
        str(cell["task_id"]),
        "--agent",
        str(cell["sforge_agent"]),
        "--model",
        str(cell["model"]),
        "--timeout",
        str(cell["wall_time_seconds"]),
        "--eval-interval",
        str(cell["eval_interval_seconds"]),
        "--run-id",
        str(cell["sforge_run_id"]),
        "--replicas",
        str(cell["outer_replicas"]),
        "--replica-concurrency",
        str(cell["outer_replica_concurrency"]),
        "--judge-concurrency",
        str(cell["judge_concurrency"]),
        "--judge-url",
        str(
            cell.get("judge_url")
            or f"http://host.docker.internal:{cell.get('judge_port', 8080)}"
        ),
    ]
    for flag, field in (
        ("--work-cpu-limit", "work_cpu_limit"),
        ("--work-mem-limit", "work_mem_limit"),
        ("--judge-cpu-limit", "judge_cpu_limit"),
        ("--judge-mem-limit", "judge_mem_limit"),
        ("--submission-cooldown", "submission_cooldown"),
        ("--max-submissions", "max_submissions"),
    ):
        if cell.get(field) is not None:
            command.extend([flag, str(cell[field])])
    if not cell.get("auto_eval_enabled", True):
        command.append("--disable-auto-eval")
    if not cell.get("auto_resume_enabled", True):
        command.append("--disable-auto-resume")
    if not cell.get("stop_hook_enabled", True):
        command.append("--disable-stop-hook")
    command.append("--enable-internet" if cell["internet"] else "--disable-internet")
    return command


def merge_agent_extra_env(
    env: dict[str, str],
    additions: dict[str, str],
    *,
    removals: Iterable[str] = (),
) -> None:
    entries: dict[str, str] = {}
    for item in env.get("SFORGE_AGENT_EXTRA_ENV", "").split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            if key.strip():
                entries[key.strip()] = value.strip()
    for key in removals:
        entries.pop(key, None)
    entries.update(additions)
    if entries:
        env["SFORGE_AGENT_EXTRA_ENV"] = ",".join(
            f"{key}={value}" for key, value in entries.items()
        )
    else:
        env.pop("SFORGE_AGENT_EXTRA_ENV", None)


def cell_environment(
    cell: dict[str, Any],
    *,
    api_key: str | None = None,
    api_base_url: str | None = None,
    bridge_host: str | None = None,
) -> dict[str, str]:
    env = dict(os.environ)
    configure_temp_environment(env)
    internet = bool(cell.get("internet", True))
    if internet:
        for sforge_key, candidates in (
            ("SFORGE_HTTP_PROXY", ("SFORGE_HTTP_PROXY", "HTTP_PROXY", "http_proxy")),
            (
                "SFORGE_HTTPS_PROXY",
                ("SFORGE_HTTPS_PROXY", "HTTPS_PROXY", "https_proxy"),
            ),
        ):
            value = next((env[key] for key in candidates if env.get(key)), None)
            if value:
                env[sforge_key] = value.replace(
                    "127.0.0.1", "host.docker.internal"
                ).replace("localhost", "host.docker.internal")
    else:
        for key in (
            "ALL_PROXY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "SFORGE_HTTP_PROXY",
            "SFORGE_HTTPS_PROXY",
            "all_proxy",
            "http_proxy",
            "https_proxy",
        ):
            env.pop(key, None)
    env.setdefault("SFORGE_NODEJS_MIRROR_URL", "https://npmmirror.com/mirrors/node")
    env.setdefault("SFORGE_NPM_REGISTRY_URL", "https://registry.npmmirror.com")
    if api_key:
        env["SFORGE_AGENT_API_KEY"] = api_key
    if api_base_url:
        env["SFORGE_AGENT_API_BASE_URL"] = api_base_url
    if bridge_host:
        append_no_proxy(env, bridge_host)
    agent = str(cell.get("sforge_agent") or METHODS[cell["method"]]["agent"])
    if agent.startswith("codex"):
        env["SFORGE_CODEX_REASONING_EFFORT"] = str(cell["reasoning_effort"])
    elif agent.startswith("pi"):
        pi_env = {"SFORGE_PI_REASONING_EFFORT": str(cell["reasoning_effort"])}
        if cell.get("pi_package_version"):
            pi_env["SFORGE_PI_PACKAGE_VERSION"] = str(
                cell["pi_package_version"]
            )
        merge_agent_extra_env(
            env,
            pi_env,
        )
    elif agent == "claude-code":
        env["SFORGE_CLAUDE_CACHE_OPT"] = "1"
        model = str(cell.get("model") or "")
        claude_env: dict[str, str] = {}
        if model:
            claude_env.update(
                {
                    "ANTHROPIC_MODEL": model,
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
                    "CLAUDE_CODE_SUBAGENT_MODEL": model,
                }
            )
        context_window = cell.get("claude_context_window_tokens")
        compact_percent = cell.get("claude_autocompact_percent")
        if context_window is not None:
            claude_env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(context_window)
            claude_env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(compact_percent)
        thinking_type = str((cell.get("thinking") or {}).get("type") or "")
        reasoning_value = cell.get("reasoning_effort")
        reasoning_effort = str(reasoning_value or "")
        thinking_controls = (
            "MAX_THINKING_TOKENS",
            "CLAUDE_CODE_DISABLE_THINKING",
            "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
            "CLAUDE_CODE_EFFORT_LEVEL",
            "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT",
        )
        for key in thinking_controls:
            env.pop(key, None)
        env.update(claude_env)
        if thinking_type == "adaptive" and reasoning_value is None:
            merge_agent_extra_env(env, claude_env, removals=thinking_controls)
        elif reasoning_effort in {"none", "minimal"}:
            merge_agent_extra_env(
                env,
                {
                    **claude_env,
                    "MAX_THINKING_TOKENS": "0",
                    "CLAUDE_CODE_DISABLE_THINKING": "1",
                },
                removals=thinking_controls,
            )
        else:
            merge_agent_extra_env(
                env,
                {
                    **claude_env,
                    "CLAUDE_CODE_EFFORT_LEVEL": reasoning_effort,
                    "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT": "1",
                },
                removals=thinking_controls,
            )
    if cell["method"] in GOAL_PLUS_METHODS:
        annotator_model = str(cell["model"])
        if cell["method"] == "goal-plus-pi-provider":
            annotator_model = annotator_model.partition("/")[2]
        env["SFORGE_GOAL_PLUS_SOURCE_DIR"] = str(current_paths().goal_plus_root)
        extra_env = {
            "SFORGE_GOAL_PLUS_PARALLEL_NUM": str(cell["inner_search_concurrency"]),
            "SFORGE_GOAL_PLUS_WORKER_RUNTIME_SECONDS": str(
                cell["worker_runtime_seconds"]
            ),
            "SFORGE_GOAL_PLUS_FINALIZATION_GRACE_SECONDS": str(
                cell.get("goal_plus_finalization_grace_seconds", 300)
            ),
            "GOAL_PLUS_EVIDENCE_ANNOTATOR_MODEL": annotator_model,
            "GOAL_PLUS_EVIDENCE_ANNOTATOR_REASONING_EFFORT": str(
                cell["reasoning_effort"]
            ),
        }
        if cell["method"] in {"goal-plus-pi", "goal-plus-pi-provider"}:
            extra_env.update(
                {
                    "SFORGE_GOAL_PLUS_WORKER_MIN_RUNTIME_SECONDS": str(
                        cell.get("worker_min_runtime_seconds", 0)
                    ),
                    "SFORGE_GOAL_PLUS_MIN_VERIFIER_RUNS": str(
                        cell.get("worker_min_verifier_runs", 0)
                    ),
                    "SFORGE_GOAL_PLUS_CLOSEOUT_RESERVE_SECONDS": str(
                        cell.get("closeout_reserve_seconds", 0)
                    ),
                }
            )
        if api_base_url:
            extra_env.update(
                {
                    "GOAL_PLUS_EVIDENCE_ANNOTATOR_BASE_URL": api_base_url,
                    "GOAL_PLUS_EVIDENCE_ANNOTATOR_PROVIDER_ID": EVIDENCE_ANNOTATOR_PROVIDER_ID,
                    "GOAL_PLUS_EVIDENCE_ANNOTATOR_PROVIDER_NAME": "EdgeBench Evidence provider",
                    "GOAL_PLUS_EVIDENCE_ANNOTATOR_API_KEY_ENV": "SFORGE_AGENT_API_KEY",
                    "GOAL_PLUS_EVIDENCE_ANNOTATOR_WIRE_API": "responses",
                }
            )
        merge_agent_extra_env(env, extra_env)
    return env


def process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def judge_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/openapi.json", timeout=1.0
        ) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def start_or_reuse_judge(
    destination: Path,
    port: int,
    controller: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[str] | None, Any]:
    paths = current_paths()
    controller_path = destination / "controller.json"
    if judge_ready(port):
        controller.update(
            {
                "judge_owned": False,
                "judge_pid": None,
                "judge_host_url": f"http://127.0.0.1:{port}",
                "judge_container_url": f"http://host.docker.internal:{port}",
            }
        )
        io.write_json(controller_path, controller)
        return None, lambda: None

    command = [
        str(paths.sforge),
        "--log-dir",
        str(destination / "judge"),
        "--tasks-dir",
        str(paths.tasks_dir),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    log = (destination / "judge.log").open("a", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=paths.root,
        env=env or dict(configure_temp_environment(dict(os.environ))),
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    log.close()
    controller.update(
        {
            "judge_owned": True,
            "judge_pid": process.pid,
            "judge_command": io.portable_command(command),
            "judge_host_url": f"http://127.0.0.1:{port}",
            "judge_container_url": f"http://host.docker.internal:{port}",
        }
    )
    io.write_json(controller_path, controller)

    def close_judge() -> None:
        if process.poll() is not None:
            return
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            controller["judge_closeout_incomplete"] = True
            io.write_json(controller_path, controller)

    atexit.register(close_judge)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if judge_ready(port):
            return process, close_judge
        if process.poll() is not None:
            break
        time.sleep(0.25)
    close_judge()
    raise RuntimeError(
        "SForge judge did not become ready; inspect "
        f"{io.portable_path(destination / 'judge.log')}"
    )


def cell_has_scored_results(destination: Path, cell: dict[str, Any]) -> bool:
    cell_path = destination / "cells" / cell["cell_id"]
    task_runs = sorted((cell_path / "sforge" / "runs").glob(f"*/{cell['task_id']}"))
    if len(task_runs) < int(cell["outer_replicas"]):
        return False
    for task_run in task_runs:
        final_path = task_run / "final_result.json"
        if not final_path.is_file():
            return False
        final = io.read_json(final_path)
        scored_reports = list((task_run / "submissions").glob("*/report.json"))
        if final.get("best_score") is None and not scored_reports:
            return False
    return True


def update_campaign_cell(destination: Path, cell_id: str, state: str) -> None:
    campaign = io.read_json(destination / "campaign.json")
    for item in campaign["cells"]:
        if item["cell_id"] == cell_id:
            item["state"] = state
            break
    campaign["updated_at"] = io.utc_now()
    io.write_json(destination / "campaign.json", campaign)


def start_campaign_cell(
    destination: Path,
    cell_summary: dict[str, Any],
    *,
    judge_container_url: str,
    api_config: dict[str, str | None],
    api_key: str | None,
    runtime_api_base_url: str | None,
    bridge_host: str | None,
) -> dict[str, Any] | None:
    cell_id = str(cell_summary["cell_id"])
    cell_path = destination / "cells" / cell_id
    cell_file = cell_path / "cell.json"
    cell = io.read_json(cell_file)
    if cell.get("state") == "completed":
        return None
    cell["judge_url"] = judge_container_url
    command = build_sforge_command(destination, cell)
    io.write_json(
        cell_path / "command.json",
        {
            "command": io.portable_command(command),
            "environment_policy": {
                "credentials": (
                    "host API environment mapped to SForge; values are never persisted"
                    if api_key
                    else "host Codex OAuth; auth contents are never persisted"
                ),
                "api_key_source": api_config["api_key_source"],
                "api_base_url_source": api_config["api_base_url_source"],
                "temp": ".tmp",
                "goal_plus_source": (
                    "third_party/goal-plus"
                    if cell["method"] in GOAL_PLUS_METHODS
                    else None
                ),
            },
        },
    )
    log = (cell_path / "controller.log").open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command,
            cwd=current_paths().root,
            env=cell_environment(
                cell,
                api_key=api_key,
                api_base_url=runtime_api_base_url,
                bridge_host=bridge_host,
            ),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except Exception:
        log.close()
        raise
    cell.update({"state": "running", "started_at": io.utc_now(), "pid": process.pid})
    io.write_json(cell_file, cell)
    update_campaign_cell(destination, cell_id, "running")
    return {"cell": cell, "cell_file": cell_file, "log": log, "process": process}


def finish_campaign_cell(
    destination: Path,
    active: dict[str, Any],
    *,
    stop_requested: bool,
) -> int:
    process: subprocess.Popen[str] = active["process"]
    returncode = process.poll()
    if returncode is None:
        returncode = process.wait()
    active["log"].close()
    cell = active["cell"]
    scored = cell_has_scored_results(destination, cell)
    if returncode == 0 and not stop_requested and not scored:
        returncode = 1
        cell["result_validation_error"] = (
            "SForge exited without the expected scored final result"
        )
    cell.update(
        {
            "state": (
                "interrupted"
                if stop_requested
                else "completed"
                if returncode == 0
                else "failed"
            ),
            "returncode": returncode,
            "finished_at": io.utc_now(),
        }
    )
    io.write_json(active["cell_file"], cell)
    update_campaign_cell(destination, str(cell["cell_id"]), str(cell["state"]))
    return int(returncode)


def execute_cell_queue(
    destination: Path,
    campaign: dict[str, Any],
    controller: dict[str, Any],
    *,
    cell_concurrency: int,
    judge_container_url: str,
    api_config: dict[str, str | None],
    api_key: str | None,
    runtime_api_base_url: str | None,
    bridge_host: str | None,
    stop_requested: Any,
) -> int:
    controller_path = destination / "controller.json"
    def record_active(active: Any) -> None:
        controller["active_children"] = {
            cell_id: {
                "pid": running["process"].pid,
                "task_id": running["cell"]["task_id"],
                "started_at": running["cell"]["started_at"],
            }
            for cell_id, running in sorted(active.items())
        }
        io.write_json(controller_path, controller)

    controller["cell_concurrency"] = cell_concurrency

    def start(cell_summary: dict[str, Any]) -> dict[str, Any] | None:
        return start_campaign_cell(
            destination,
            cell_summary,
            judge_container_url=judge_container_url,
            api_config=api_config,
            api_key=api_key,
            runtime_api_base_url=runtime_api_base_url,
            bridge_host=bridge_host,
        )

    def fail(cell_summary: dict[str, Any], error: Exception) -> int:
        cell_id = str(cell_summary["cell_id"])
        cell_file = destination / "cells" / cell_id / "cell.json"
        cell = io.read_json(cell_file)
        cell.update(
            {
                "state": "failed",
                "returncode": 1,
                "finished_at": io.utc_now(),
                "launch_error": str(error),
            }
        )
        io.write_json(cell_file, cell)
        update_campaign_cell(destination, cell_id, "failed")
        return 1

    def stop(running: dict[str, Any]) -> None:
        process = running["process"]
        if process.poll() is None:
            try:
                process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass

    return execute_bounded_cell_queue(
        campaign["cells"],
        concurrency=cell_concurrency,
        item_id=lambda cell: str(cell["cell_id"]),
        start=start,
        poll=lambda running: running["process"].poll() is not None,
        finish=lambda running, stopping: finish_campaign_cell(
            destination, running, stop_requested=stopping
        ),
        fail=fail,
        record_active=record_active,
        stop_requested=stop_requested,
        stop=stop,
    )


class RuntimeResources:
    """Own the Judge and any host/container socket bridges for one campaign."""

    def __init__(self) -> None:
        self.judge_process: subprocess.Popen[str] | None = None
        self.close_judge: Any = lambda: None
        self.bridge_processes: list[subprocess.Popen[str]] = []
        self.bridge_closers: list[Any] = []
        self.api_config: dict[str, str | None] = {}
        self.api_key: str | None = None
        self.runtime_api_base_url: str | None = None
        self.bridge_host: str | None = None
        self.judge_container_url = ""

    def close(self) -> None:
        self.close_judge()
        for closer in reversed(self.bridge_closers):
            closer()

    def record_closeout(self) -> dict[str, Any]:
        return {
            "judge_alive_after_closeout": process_alive(
                self.judge_process.pid if self.judge_process is not None else None
            ),
            "bridges_alive_after_closeout": [
                process_alive(process.pid) for process in self.bridge_processes
            ],
        }


def prepare_runtime_resources(
    destination: Path,
    profile: dict[str, Any],
    controller: dict[str, Any],
) -> RuntimeResources:
    resources = RuntimeResources()
    judge_port = int(profile.get("judge_port", 8080))
    api_protocol = str(
        profile.get("api_protocol")
        or api_protocol_for_methods(profile["methods"])
    )
    resources.judge_container_url = f"http://host.docker.internal:{judge_port}"
    resources.api_config = resolve_agent_api_config(protocol=api_protocol)
    resources.api_key = resources.api_config["api_key"]
    api_base_url = resources.api_config["api_base_url"]
    resources.runtime_api_base_url = str(api_base_url) if api_base_url else None
    controller["bridges"] = []
    try:
        if api_protocol == "anthropic" and (
            not resources.api_key or not resources.runtime_api_base_url
        ):
            raise RuntimeError(
                "Claude Code campaigns require an API key and Anthropic base URL"
            )
        if resources.runtime_api_base_url and loopback_api_target(
            resources.runtime_api_base_url
        ):
            resources.bridge_host = default_route_ipv4()
            target_host, target_port = loopback_api_target(
                resources.runtime_api_base_url
            ) or ("", 0)
            process, metadata, closer = start_socket_bridge(
                destination,
                name="agent-api",
                listen_host=resources.bridge_host,
                target_host=target_host,
                target_port=target_port,
            )
            resources.bridge_processes.append(process)
            resources.bridge_closers.append(closer)
            controller.setdefault("bridges", []).append(metadata)
            resources.runtime_api_base_url = bridged_base_url(
                resources.runtime_api_base_url,
                resources.bridge_host,
                int(metadata["listen_port"]),
            )
            api_probe = authenticated_api_probe(
                resources.runtime_api_base_url,
                str(resources.api_key or ""),
                protocol=api_protocol,
                model=str(profile["model"]),
                thinking=profile.get("thinking"),
                reasoning_effort=profile.get("reasoning_effort"),
            )
            if not resources.api_key or not api_probe["passed"]:
                raise RuntimeError(
                    "authenticated agent API bridge probe failed "
                    f"(HTTP {api_probe.get('status')})"
                )
        if resources.api_key and resources.runtime_api_base_url:
            probe_image = task_images(str(profile["task_ids"][0]))[0]
            container_probe = docker_http_probe(
                probe_image,
                resources.runtime_api_base_url,
                api_key=str(resources.api_key),
                protocol=api_protocol,
                model=str(profile["model"]),
                thinking_type=str((profile.get("thinking") or {}).get("type") or ""),
                reasoning_effort=str(profile.get("reasoning_effort") or ""),
            )
            if not container_probe["passed"]:
                raise RuntimeError(
                    "agent API is not reachable from an EdgeBench Work container "
                    f"(HTTP {container_probe.get('status')}; "
                    f"{container_probe.get('stderr') or 'no stderr'})"
                )
        judge_env = judge_server_environment(
            api_key=str(resources.api_key) if resources.api_key else None,
            api_base_url=resources.runtime_api_base_url,
            bridge_host=resources.bridge_host,
        )
        resources.judge_process, resources.close_judge = start_or_reuse_judge(
            destination, judge_port, controller, env=judge_env
        )
        if sys.platform.startswith("linux"):
            resources.bridge_host = resources.bridge_host or default_route_ipv4()
            process, metadata, closer = start_socket_bridge(
                destination,
                name="judge",
                listen_host=resources.bridge_host,
                target_host="127.0.0.1",
                target_port=judge_port,
            )
            resources.bridge_processes.append(process)
            resources.bridge_closers.append(closer)
            controller.setdefault("bridges", []).append(metadata)
            resources.judge_container_url = (
                f"http://{resources.bridge_host}:{int(metadata['listen_port'])}"
            )
            judge_probe = docker_http_probe(
                task_images(str(profile["task_ids"][0]))[0],
                resources.judge_container_url + "/openapi.json",
            )
            if not judge_probe["passed"]:
                raise RuntimeError(
                    "Judge is not reachable from an EdgeBench Work container "
                    f"(HTTP {judge_probe.get('status')}; "
                    f"{judge_probe.get('stderr') or 'no stderr'})"
                )
        controller.update(
            {
                "agent_auth_mode": "api_key" if resources.api_key else "oauth",
                "agent_api_protocol": api_protocol,
                "agent_api_key_source": resources.api_config["api_key_source"],
                "agent_api_base_url_source": resources.api_config[
                    "api_base_url_source"
                ],
                "agent_container_api_base_url": resources.runtime_api_base_url,
                "judge_container_url": resources.judge_container_url,
            }
        )
        io.write_json(destination / "controller.json", controller)
        return resources
    except Exception:
        resources.close()
        raise


def _mark_controller_failure(
    destination: Path,
    controller: dict[str, Any],
    error: Exception,
) -> None:
    campaign = io.read_json(destination / "campaign.json")
    campaign.update(
        {
            "state": "failed",
            "finished_at": io.utc_now(),
            "controller_error": str(error),
        }
    )
    io.write_json(destination / "campaign.json", campaign)
    controller.update(
        {
            "state": "failed",
            "finished_at": io.utc_now(),
            "returncode": 1,
            "error": str(error),
        }
    )
    io.write_json(destination / "controller.json", controller)


def execute_campaign(destination: Path) -> int:
    controller_path = destination / "controller.json"
    controller = io.read_json(controller_path)
    controller.update(
        {
            "state": "running",
            "started_at": io.utc_now(),
            "pid": os.getpid(),
            "pgid": os.getpgrp(),
        }
    )
    io.write_json(controller_path, controller)
    campaign = io.read_json(destination / "campaign.json")
    campaign.update({"state": "running", "started_at": io.utc_now()})
    io.write_json(destination / "campaign.json", campaign)
    stop_requested = False

    def handle_stop(signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True
        controller["stop_requested_at"] = io.utc_now()
        controller["stop_signal"] = signal.Signals(signum).name
        io.write_json(controller_path, controller)

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    profile = io.read_json(destination / "profile.json")
    try:
        resources = prepare_runtime_resources(destination, profile, controller)
    except Exception as exc:
        _mark_controller_failure(destination, controller, exc)
        return 1

    overall_returncode = execute_cell_queue(
        destination,
        campaign,
        controller,
        cell_concurrency=int(profile.get("cell_concurrency", 1)),
        judge_container_url=resources.judge_container_url,
        api_config=resources.api_config,
        api_key=str(resources.api_key) if resources.api_key else None,
        runtime_api_base_url=resources.runtime_api_base_url,
        bridge_host=resources.bridge_host,
        stop_requested=lambda: stop_requested,
    )
    campaign = io.read_json(destination / "campaign.json")
    states = {cell["state"] for cell in campaign["cells"]}
    if stop_requested:
        final_state = "interrupted"
        overall_returncode = overall_returncode or 130
    elif states == {"completed"}:
        final_state = "completed"
    elif "failed" in states:
        final_state = "failed"
        overall_returncode = overall_returncode or 1
    else:
        final_state = "partial"
    campaign.update({"state": final_state, "finished_at": io.utc_now()})
    io.write_json(destination / "campaign.json", campaign)
    resources.close()
    controller.update(
        {
            "state": final_state,
            "finished_at": io.utc_now(),
            "returncode": overall_returncode,
            "active_children": {},
            **resources.record_closeout(),
        }
    )
    io.write_json(controller_path, controller)
    from .reporting import finalize_campaign

    finalized = finalize_campaign(destination)
    controller["completion_evidence_passed"] = bool(
        finalized["completion_evidence_passed"]
    )
    if not finalized["completion_evidence_passed"]:
        overall_returncode = overall_returncode or 2
        controller.update({"state": "partial", "returncode": overall_returncode})
    io.write_json(controller_path, controller)
    return overall_returncode


def launch(destination: Path, *, detach: bool) -> int:
    paths = current_paths()
    controller = io.read_json(destination / "controller.json")
    if process_alive(controller.get("pid")):
        raise RuntimeError(
            f"campaign controller is already running: {controller['pid']}"
        )
    if not detach:
        return execute_campaign(destination)
    command = [
        str(paths.venv_python if paths.venv_python.is_file() else Path(sys.executable)),
        str(paths.root / "experiments" / "edgebench" / "experiment.py"),
        "_execute",
        "--campaign",
        io.portable_path(destination),
    ]
    log = (destination / "controller.log").open("a", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=paths.root,
        env=dict(configure_temp_environment(dict(os.environ))),
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    log.close()
    controller = io.read_json(destination / "controller.json")
    controller.update(
        {
            "schema_version": 1,
            "launched_at": controller.get("launched_at") or io.utc_now(),
            "pid": process.pid,
            "pgid": process.pid,
            "command": io.portable_command(command),
        }
    )
    if controller.get("state") in {"prepared", "launching"}:
        controller["state"] = "launching"
    io.write_json(destination / "controller.json", controller)
    print(json.dumps({"pid": process.pid, "campaign": io.portable_path(destination)}))
    return 0


def status_payload(destination: Path) -> dict[str, Any]:
    from .evidence import live_goal_plus_status

    campaign = io.read_json(destination / "campaign.json")
    controller = io.read_json(destination / "controller.json")
    cells: list[dict[str, Any]] = []
    for item in campaign["cells"]:
        cell_path = destination / "cells" / item["cell_id"]
        cell = io.read_json(cell_path / "cell.json")
        task_runs = sorted(
            (cell_path / "sforge" / "runs").glob(f"*/{cell['task_id']}")
        )
        final_results = [
            run / "final_result.json"
            for run in task_runs
            if (run / "final_result.json").is_file()
        ]
        cell_status = {
            "cell_id": item["cell_id"],
            "task_id": item["task_id"],
            "method": item["method"],
            "state": cell["state"],
            "pid": cell.get("pid"),
            "pid_alive": process_alive(cell.get("pid")),
            "completed_trajectories": len(final_results),
            "expected_trajectories": cell["outer_replicas"],
            "summary": (
                io.portable_path(cell_path / "summary.json")
                if (cell_path / "summary.json").is_file()
                else None
            ),
        }
        if item["method"] in GOAL_PLUS_METHODS:
            latest_task_run = task_runs[-1] if task_runs else None
            cell_status["goal_plus"] = live_goal_plus_status(
                destination, cell, latest_task_run
            )
        cells.append(cell_status)
    return {
        "campaign": campaign["campaign_id"],
        "state": campaign["state"],
        "controller": {
            "state": controller["state"],
            "pid": controller.get("pid"),
            "pgid": controller.get("pgid"),
            "alive": process_alive(controller.get("pid")),
            "judge_owned": controller.get("judge_owned"),
            "judge_pid": controller.get("judge_pid"),
            "judge_alive": process_alive(controller.get("judge_pid")),
        },
        "cells": cells,
    }


def print_status(destination: Path, *, as_json: bool) -> int:
    payload = status_payload(destination)
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print(
        f"{payload['campaign']}: {payload['state']} "
        f"(controller alive={payload['controller']['alive']})"
    )
    for cell in payload["cells"]:
        print(
            f"- {cell['cell_id']}: {cell['state']}; "
            f"{cell['completed_trajectories']}/{cell['expected_trajectories']} trajectories"
        )
        goal_plus = cell.get("goal_plus")
        if isinstance(goal_plus, dict):
            statuses = ", ".join(
                f"{item.get('goal_plus_id', 'goal')}={item.get('status', 'unknown')}"
                for item in goal_plus.get("goal_statuses") or []
                if isinstance(item, dict)
            )
            print(
                "  Goal Plus: "
                f"{goal_plus['candidate_count']} candidates; "
                f"{goal_plus['agent_session_count']} sessions; "
                f"{goal_plus['actual_worker_launch_count']} workers; "
                f"{goal_plus['worker_verifier_runs']} verifier runs; "
                f"selected={goal_plus['selected_candidate_ids'] or '-'}; "
                f"promoted={goal_plus['promoted_candidate_ids'] or '-'}; "
                f"status={statuses or '-'}; "
                f"snapshot={goal_plus.get('snapshot_at') or '-'}"
            )
    return 0


def stop_campaign(destination: Path, *, wait_seconds: int) -> int:
    controller_path = destination / "controller.json"
    controller = io.read_json(controller_path)
    pid = controller.get("pid")
    pgid = controller.get("pgid")
    if not process_alive(pid):
        print("controller is not running; no signal sent")
        return 0
    if not pgid:
        raise RuntimeError("running controller has no recorded process group")
    os.kill(int(pid), signal.SIGINT)
    controller["state"] = "stopping"
    controller["stop_requested_at"] = io.utc_now()
    io.write_json(controller_path, controller)
    deadline = time.monotonic() + max(0, wait_seconds)
    while process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.25)
    payload = {
        "signal": "SIGINT",
        "pid": pid,
        "pgid": pgid,
        "alive_after_wait": process_alive(pid),
        "artifacts_preserved": True,
    }
    print(json.dumps(payload, indent=2))
    return 0 if not payload["alive_after_wait"] else 2
