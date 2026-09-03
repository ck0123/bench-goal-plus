"""Durable native campaign lifecycle for Frontier-Engineering v1-lite."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from bench_artifacts import sanitize_id, utc_now
from bench_goal_plus.search_scheduler import search_scheduler_from_json

from experiments.benchmark_compare import experiment as standalone

from .config import (
    GOAL_PLUS_ROOT,
    ROOT,
    UPSTREAM_ROOT,
    campaign_dir,
    write_json,
)
from . import openevolve_runtime


ADAPTER_ID = "frontier-engineering-v1-lite"
ADAPTER_MODULE = "experiments.frontier_engineering.task_adapter"
TERMINAL_CELL_STATES = {"completed", "partial", "failed", "interrupted"}


def preserve_conflict(path: Path) -> Path | None:
    if not path.exists():
        return None
    for index in range(1, 10_000):
        suffix = "_bak" if index == 1 else f"_bak{index}"
        backup = path.with_name(path.name + suffix)
        if not backup.exists():
            path.rename(backup)
            return backup
    raise RuntimeError(f"cannot preserve conflicting campaign path: {path}")


def prepare(campaign_id: str, profile: dict[str, Any], profile_path: Path) -> Path:
    search_scheduler = search_scheduler_from_json(profile.get("search_scheduler"))
    destination = campaign_dir(campaign_id)
    backup = preserve_conflict(destination)
    destination.mkdir(parents=True)
    campaign = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "benchmark": "frontier-engineering",
        "suite": "v1-lite",
        "profile": profile["id"],
        "profile_path": str(profile_path),
        "state": "preparing",
        "prepared_at": utc_now(),
        "methods": profile["methods"],
        "task_ids": profile["task_ids"],
        "seeds": profile["seeds"],
        "model": profile["model"],
        "reasoning_effort": profile["reasoning_effort"],
        **(
            {"search_scheduler": search_scheduler.as_dict()}
            if search_scheduler is not None
            else {}
        ),
        "budget": {
            "wall_time_seconds": profile["wall_time_seconds"],
            "live_search_concurrency": profile["concurrency"],
            "cell_concurrency": profile["cell_concurrency"],
            "attempts": len(profile["seeds"]),
            "soft_closeout_seconds": profile["soft_closeout_seconds"],
            "hard_kill_grace_seconds": profile["hard_kill_grace_seconds"],
            "worker_runtime_seconds": profile["worker_runtime_seconds"],
            "worker_min_runtime_seconds": profile.get("worker_min_runtime_seconds"),
            "iterations": profile.get("iterations"),
        },
        "source": {
            "frontier_engineering_root": str(UPSTREAM_ROOT),
            "goal_plus_root": str(GOAL_PLUS_ROOT),
        },
        "preserved_conflict": str(backup) if backup else None,
        "cells": [],
        "secret_policy": "credentials and provider URLs are inherited and never serialized",
    }
    campaign_path = destination / "campaign.json"
    write_json(campaign_path, campaign)
    for task_id in profile["task_ids"]:
        for method in profile["methods"]:
            for seed in profile["seeds"]:
                cell_id = sanitize_id(f"{task_id}-{method}-seed-{seed}")
                run_dir = destination / "cells" / cell_id
                cell = {
                    "cell_id": cell_id,
                    "task_id": task_id,
                    "method": method,
                    "seed": seed,
                    "run_dir": str(run_dir),
                    "state": "preparing",
                    "error": None,
                }
                campaign["cells"].append(cell)
                write_json(campaign_path, campaign)
                try:
                    if method == "openevolve":
                        openevolve_runtime.prepare_cell(
                            run_dir,
                            task_id=task_id,
                            seed=seed,
                            profile=profile,
                        )
                    else:
                        standalone.prepare(
                            standalone.PrepareConfig(
                                benchmark=ADAPTER_ID,
                                adapter_module=ADAPTER_MODULE,
                                task_id=task_id,
                                method=method,
                                model=profile["model"],
                                reasoning_effort=profile["reasoning_effort"],
                                wall_time_seconds=profile["wall_time_seconds"],
                                concurrency=profile["concurrency"],
                                soft_closeout_seconds=profile["soft_closeout_seconds"],
                                hard_kill_grace_seconds=profile["hard_kill_grace_seconds"],
                                worker_runtime_seconds=profile["worker_runtime_seconds"],
                                worker_min_runtime_seconds=profile.get("worker_min_runtime_seconds"),
                                iterations_ceiling=1,
                                seed=seed,
                                run_dir=run_dir,
                                environment_manifest=ROOT / "environment/upstreams.json",
                                checkout_root=ROOT / "third_party",
                                venv=ROOT / ".bench-env/venv",
                                search_scheduler=search_scheduler,
                            ).to_namespace()
                        )
                    cell["state"] = "prepared"
                except Exception as error:
                    cell["state"] = "failed"
                    cell["error"] = f"{type(error).__name__}: {error}"
                write_json(campaign_path, campaign)
    campaign["state"] = (
        "prepared"
        if all(cell["state"] == "prepared" for cell in campaign["cells"])
        else "partial"
    )
    campaign["preparation_finished_at"] = utc_now()
    write_json(campaign_path, campaign)
    if campaign["state"] != "prepared":
        raise RuntimeError(f"Frontier-Engineering campaign preparation was partial: {destination}")
    return destination


def _set_controller(destination: Path, *, active: bool, current_cell: str | None = None) -> None:
    write_json(
        destination / "controller.json",
        {
            "pid": os.getpid(),
            "active": active,
            "current_cell": current_cell,
            "updated_at": utc_now(),
        },
    )


def execute_campaign(destination: Path) -> int:
    campaign_path = destination / "campaign.json"
    campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    if campaign["state"] != "prepared":
        raise RuntimeError(f"campaign is not prepared: {campaign['state']}")
    stop_requested = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    previous = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    campaign["state"] = "running"
    campaign["execution_started_at"] = utc_now()
    write_json(campaign_path, campaign)
    _set_controller(destination, active=True)
    try:
        for cell in campaign["cells"]:
            if stop_requested:
                break
            if cell["state"] in TERMINAL_CELL_STATES:
                continue
            cell["state"] = "running"
            cell["started_at"] = utc_now()
            _set_controller(destination, active=True, current_cell=cell["cell_id"])
            write_json(campaign_path, campaign)
            try:
                if cell["method"] == "openevolve":
                    returncode = openevolve_runtime.execute_cell(Path(cell["run_dir"]))
                else:
                    returncode = standalone.execute(
                        standalone.RunConfig(
                            run_dir=Path(cell["run_dir"]),
                            model=campaign["model"],
                            codex_bin="codex",
                            pi_bin="pi",
                            api_base=os.environ.get("OPENAI_BASE_URL"),
                        ).to_namespace()
                    )
                manifest = json.loads(
                    (Path(cell["run_dir"]) / "experiment.json").read_text(encoding="utf-8")
                )
                final_path = Path(cell["run_dir"]) / "final-eval.json"
                final = (
                    json.loads(final_path.read_text(encoding="utf-8"))
                    if final_path.is_file()
                    else {}
                )
                completed = (
                    returncode == 0
                    and manifest.get("status") == "finished"
                    and final.get("valid") is True
                )
                cell["state"] = "completed" if completed else "partial"
                cell["returncode"] = returncode
                if not completed:
                    cell["error"] = (
                        (manifest.get("execution") or {}).get(
                            "result_incomplete_reason"
                        )
                        or "official final evaluator did not produce a valid score"
                    )
            except BaseException as error:
                cell["state"] = "interrupted" if stop_requested else "failed"
                cell["error"] = f"{type(error).__name__}: {error}"
            cell["finished_at"] = utc_now()
            write_json(campaign_path, campaign)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        _set_controller(destination, active=False)
    if stop_requested:
        for cell in campaign["cells"]:
            if cell["state"] == "running":
                cell["state"] = "interrupted"
        campaign["state"] = "interrupted"
    elif all(cell["state"] == "completed" for cell in campaign["cells"]):
        campaign["state"] = "completed"
    elif any(cell["state"] in TERMINAL_CELL_STATES for cell in campaign["cells"]):
        campaign["state"] = "partial"
    else:
        campaign["state"] = "failed"
    campaign["execution_finished_at"] = utc_now()
    write_json(campaign_path, campaign)
    return 0 if campaign["state"] == "completed" else 2


def launch_detached(destination: Path) -> int:
    command = [
        sys.executable,
        str(Path(__file__).resolve().with_name("experiment.py")),
        "run",
        "--campaign",
        destination.name,
        "--controller-child",
    ]
    with (
        (destination / "controller.stdout.log").open("ab") as stdout,
        (destination / "controller.stderr.log").open("ab") as stderr,
    ):
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    write_json(
        destination / "controller.json",
        {
            "pid": process.pid,
            "active": True,
            "detached": True,
            "command": command,
            "updated_at": utc_now(),
        },
    )
    return 0


def stop_campaign(destination: Path) -> int:
    controller_path = destination / "controller.json"
    controller = json.loads(controller_path.read_text(encoding="utf-8"))
    pid = controller.get("pid")
    if not isinstance(pid, int) or pid < 1:
        raise RuntimeError("campaign has no controller PID")
    try:
        process_group = os.getpgid(pid)
    except ProcessLookupError:
        return 0
    if process_group == pid:
        os.killpg(process_group, signal.SIGTERM)
    else:
        os.kill(pid, signal.SIGTERM)
    return 0


def status_payload(destination: Path) -> dict[str, Any]:
    campaign = json.loads((destination / "campaign.json").read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    cells = []
    for cell in campaign["cells"]:
        observed = dict(cell)
        manifest = Path(cell["run_dir"]) / "experiment.json"
        if manifest.is_file():
            observed_manifest = json.loads(manifest.read_text())
            observed["run_status"] = observed_manifest.get("status")
            if observed_manifest.get("method") == "openevolve":
                observed["iterations"] = openevolve_runtime.iteration_progress(
                    Path(cell["run_dir"])
                )
        counts[cell["state"]] = counts.get(cell["state"], 0) + 1
        cells.append(observed)
    controller_path = destination / "controller.json"
    controller = json.loads(controller_path.read_text()) if controller_path.is_file() else None
    return {
        "campaign_id": campaign["campaign_id"],
        "state": campaign["state"],
        "counts": counts,
        "controller": controller,
        "cells": cells,
    }
