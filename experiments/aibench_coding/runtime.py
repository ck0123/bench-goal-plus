"""Native lifecycle for aibench coding campaigns."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from bench_artifacts import sanitize_id, utc_now
from bench_goal_plus.upstreams import registered_upstream_branch
from bench_runtime_paths import configure_temp_environment
from experiments.benchmark_compare import experiment as standalone
from experiments.benchmark_compare.pi_worker_launcher import REAL_PI_BIN_ENV

from . import task_adapter
from .config import (
    GOAL_PLUS_METHODS,
    GOAL_PLUS_ROOT,
    PI_METHODS,
    ROOT,
    RUNTIME_PYTHON,
    RUNTIME_ROOT,
    UPSTREAM_CHECKOUT,
    UPSTREAM_ROOT,
    campaign_dir,
    preserve_conflict,
    split_model,
    write_json,
)


ADAPTER_ID = "aibench-coding-native"
ADAPTER_MODULE = "experiments.aibench_coding.task_adapter"
STANDALONE_CONTROLLER = ROOT / "experiments" / "benchmark_compare" / "experiment.py"
SANDBOX_SOURCE = Path(__file__).resolve().with_name("sandbox.py")
TERMINAL_CELL_STATES = {"completed", "partial", "failed", "interrupted"}
_NODE_VERSION = re.compile(r"v?(\d+)(?:\.|$)")


def _capture(command: list[str], *, cwd: Path | None = None) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"{type(error).__name__}: {error}"
    return completed.returncode == 0, (completed.stdout or completed.stderr).strip()


def _git(path: Path, *arguments: str) -> str | None:
    ok, output = _capture(["git", "-C", str(path), *arguments])
    return output if ok else None


def _checkout_check(name: str, path: Path, expected_branch: str) -> dict[str, Any]:
    branch = _git(path, "symbolic-ref", "--short", "HEAD")
    dirty = _git(path, "status", "--porcelain")
    commit = _git(path, "rev-parse", "HEAD")
    return {
        "kind": "managed-checkout",
        "name": name,
        "path": str(path),
        "expected_branch": expected_branch,
        "branch": branch,
        "commit": commit,
        "dirty": bool(dirty),
        "passed": bool(commit and branch == expected_branch and dirty == ""),
    }


def _selected_languages(profile: dict[str, Any]) -> set[str]:
    case_root = (
        UPSTREAM_ROOT
        / "benchmarks"
        / "ai_coding"
        / "cases"
        / profile["case_set"]
    )
    selected = set(profile["task_ids"])
    languages: set[str] = set()
    for path in case_root.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("case_id") in selected and isinstance(payload.get("language"), str):
            languages.add(payload["language"])
    return languages


def local_inventory(profile: dict[str, Any]) -> dict[str, Any]:
    checks = [
        {
            "kind": "path",
            "name": "aibench-source",
            "path": str(UPSTREAM_ROOT),
            "present": (UPSTREAM_ROOT / "pyproject.toml").is_file(),
        },
        {
            "kind": "path",
            "name": "aibench-case-set",
            "path": str(
                UPSTREAM_ROOT
                / "benchmarks"
                / "ai_coding"
                / "cases"
                / profile["case_set"]
            ),
            "present": (
                UPSTREAM_ROOT
                / "benchmarks"
                / "ai_coding"
                / "cases"
                / profile["case_set"]
            ).is_dir(),
        },
        {
            "kind": "runtime",
            "name": "aibench-python",
            "path": str(RUNTIME_PYTHON),
            "present": RUNTIME_PYTHON.is_file(),
        },
    ]
    return {
        "schema_version": 1,
        "benchmark": "aibench-coding",
        "profile": profile["id"],
        "read_only": True,
        "acquisition_attempted": False,
        "checks": checks,
        "passed": all(check["present"] for check in checks),
    }


def _pinned_python_version(source_root: Path) -> str:
    version = (source_root / ".python-version").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+", version):
        raise RuntimeError("aibench .python-version must pin MAJOR.MINOR")
    return version


def provision(profile: dict[str, Any]) -> dict[str, Any]:
    managed_uv = ROOT / ".bench-env" / "venv" / "bin" / "uv"
    uv = shutil.which("uv") or (str(managed_uv) if managed_uv.is_file() else None)
    if uv is None:
        raise FileNotFoundError("uv is required to provision the aibench runtime")
    if not (UPSTREAM_ROOT / "uv.lock").is_file():
        raise FileNotFoundError("managed aibench source is missing uv.lock")
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    environment = dict(configure_temp_environment(os.environ.copy()))
    python_version = _pinned_python_version(UPSTREAM_ROOT)
    current_runtime = (
        _capture([str(RUNTIME_PYTHON), "--version"])[1]
        if RUNTIME_PYTHON.is_file()
        else ""
    )
    preserved_runtime = None
    if current_runtime and not current_runtime.startswith(f"Python {python_version}."):
        preserved_runtime = preserve_conflict(RUNTIME_ROOT / "venv")
    python_source = os.environ.get("AIBENCH_PYTHON", python_version)
    commands = []
    if not RUNTIME_PYTHON.is_file():
        commands.append(
            [uv, "venv", "--python", python_source, str(RUNTIME_ROOT / "venv")]
        )
    commands.append(
        [
            uv,
            "sync",
            "--frozen",
            "--extra",
            "grading",
            "--active",
            "--project",
            str(UPSTREAM_ROOT),
        ]
    )
    if len(commands) == 2:
        subprocess.run(commands[0], cwd=ROOT, env=environment, check=True)
    environment["VIRTUAL_ENV"] = str(RUNTIME_ROOT / "venv")
    subprocess.run(commands[-1], cwd=UPSTREAM_ROOT, env=environment, check=True)
    payload = {
        "schema_version": 1,
        "benchmark": "aibench-coding",
        "profile": profile["id"],
        "runtime_python": str(RUNTIME_PYTHON),
        "runtime_version": python_version,
        "preserved_runtime": str(preserved_runtime) if preserved_runtime else None,
        "source_commit": _git(UPSTREAM_CHECKOUT, "rev-parse", "HEAD"),
        "commands": commands,
    }
    write_json(RUNTIME_ROOT / "provision.json", payload)
    return payload


def doctor(
    profile: dict[str, Any],
    *,
    output: Path | None = None,
    local_assets_only: bool = False,
    allow_missing_local_assets: bool = False,
) -> int:
    inventory = local_inventory(profile)
    if local_assets_only:
        print(json.dumps(inventory, indent=2, ensure_ascii=False))
        return 0 if inventory["passed"] or allow_missing_local_assets else 2
    checks: list[dict[str, Any]] = [
        {"kind": "local-inventory", "passed": inventory["passed"], "details": inventory},
        {
            "kind": "host",
            "name": "linux-bubblewrap",
            "platform": platform.system(),
            "path": shutil.which("bwrap"),
            "passed": platform.system() == "Linux" and shutil.which("bwrap") is not None,
        },
    ]
    checks.append(
        _checkout_check(
            "aibench_coding",
            UPSTREAM_CHECKOUT,
            registered_upstream_branch("aibench_coding", repository_root=ROOT),
        )
    )
    if any(method in GOAL_PLUS_METHODS for method in profile["methods"]):
        checks.append(
            _checkout_check(
                "goal_plus",
                GOAL_PLUS_ROOT.parents[1],
                registered_upstream_branch("goal_plus", repository_root=ROOT),
            )
        )
    if "javascript" in _selected_languages(profile):
        node = shutil.which("node")
        node_ok, node_version = _capture([node, "--version"]) if node else (False, "")
        match = _NODE_VERSION.search(node_version)
        checks.append(
            {
                "kind": "runtime",
                "name": "node",
                "path": node,
                "version": node_version or None,
                "passed": node_ok and match is not None and int(match.group(1)) >= 22,
            }
        )
    runtime_ok, runtime_detail = (
        _capture(
            [
                str(RUNTIME_PYTHON),
                "-c",
                "import aibench, av, flask, jsonschema, matplotlib, numpy, pandas, pytest, yaml",
            ],
            cwd=UPSTREAM_ROOT,
        )
        if RUNTIME_PYTHON.is_file()
        else (False, "runtime not provisioned")
    )
    checks.append(
        {
            "kind": "runtime",
            "name": "aibench-grading",
            "path": str(RUNTIME_PYTHON),
            "detail": runtime_detail or None,
            "passed": runtime_ok,
        }
    )
    for name in (
        "codex" if any("codex" in method for method in profile["methods"]) else None,
        "pi" if any(method in PI_METHODS for method in profile["methods"]) else None,
    ):
        if name is None:
            continue
        path = shutil.which(name)
        ok, version = _capture([path, "--version"]) if path else (False, "")
        checks.append(
            {
                "kind": "agent",
                "name": name,
                "path": path,
                "version": version or None,
                "passed": ok,
            }
        )
    provider = profile["agent_provider"]
    checks.extend(
        [
            {
                "kind": "provider",
                "name": "base-url",
                "environment": provider["base_url_env"],
                "passed": bool(os.environ.get(provider["base_url_env"])),
            },
            {
                "kind": "credential",
                "name": "api-key",
                "environment": provider["api_key_env"],
                "passed": bool(os.environ.get(provider["api_key_env"])),
            },
        ]
    )
    payload = {
        "schema_version": 1,
        "benchmark": "aibench-coding",
        "profile": profile["id"],
        "methods": profile["methods"],
        "model": profile["model"],
        "checks": checks,
        "passed": all(check["passed"] for check in checks),
    }
    if output is not None:
        write_json(output.expanduser().absolute(), payload)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if payload["passed"] else 2


def prepare(campaign_id: str, profile: dict[str, Any], profile_path: Path) -> Path:
    destination = campaign_dir(campaign_id)
    backup = preserve_conflict(destination)
    destination.mkdir(parents=True)
    source_commit = _git(UPSTREAM_CHECKOUT, "rev-parse", "HEAD")
    goal_plus_commit = _git(GOAL_PLUS_ROOT.parents[1], "rev-parse", "HEAD")
    campaign = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "benchmark": "aibench-coding",
        "profile": profile["id"],
        "profile_path": str(profile_path),
        "profile_snapshot": profile,
        "state": "preparing",
        "prepared_at": utc_now(),
        "methods": profile["methods"],
        "task_ids": profile["task_ids"],
        "seeds": profile["seeds"],
        "model": profile["model"],
        "reasoning_effort": profile["reasoning_effort"],
        "budget": {
            "wall_time_seconds": profile["wall_time_seconds"],
            "live_search_concurrency": profile["concurrency"],
            "cell_concurrency": profile["cell_concurrency"],
            "repeats": len(profile["seeds"]),
        },
        "source": {
            "repository": "https://gitcode.com/caohaotiantian/muyuan.git",
            "tracking_branch": "coding-benchmark",
            "commit": source_commit,
            "source_subdir": "benchmarks/coding",
            "case_set": profile["case_set"],
            "case_set_fingerprint": profile["expected_case_set_fingerprint"],
            "goal_plus_commit": goal_plus_commit,
        },
        "preserved_conflict": str(backup) if backup else None,
        "secret_policy": "provider URL and credential values are inherited, not serialized",
        "cells": [],
    }
    campaign_path = destination / "campaign.json"
    write_json(campaign_path, campaign)
    task_adapter.configure_case_set(
        profile["case_set"],
        profile["expected_case_set_fingerprint"],
        profile["validity_policy"],
    )
    for task_id in profile["task_ids"]:
        for method in profile["methods"]:
            provider_id, model_id = split_model(profile)
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
                    standalone.prepare(
                        standalone.PrepareConfig(
                            benchmark=ADAPTER_ID,
                            adapter_module=ADAPTER_MODULE,
                            task_id=task_id,
                            method=method,
                            model=model_id,
                            pi_provider_id=provider_id,
                            pi_api="openai-responses",
                            pi_api_key_env=profile["agent_provider"]["api_key_env"],
                            wall_time_seconds=profile["wall_time_seconds"],
                            concurrency=profile["concurrency"],
                            soft_closeout_seconds=profile["soft_closeout_seconds"],
                            hard_kill_grace_seconds=profile["hard_kill_grace_seconds"],
                            worker_runtime_seconds=profile["worker_runtime_seconds"],
                            worker_min_runtime_seconds=None,
                            iterations_ceiling=1,
                            seed=seed,
                            reasoning_effort=profile["reasoning_effort"],
                            run_dir=run_dir,
                            environment_manifest=ROOT / "environment" / "upstreams.json",
                            checkout_root=ROOT / "third_party",
                            venv=ROOT / ".bench-env" / "venv",
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
        raise RuntimeError(f"aibench campaign preparation was partial: {destination}")
    return destination


def _sandbox_binaries(run_dir: Path) -> tuple[Path, Path]:
    destination = run_dir / "controller-runtime" / "agent-sandbox"
    destination.mkdir(parents=True, exist_ok=True)
    wrappers = []
    for role in ("codex", "pi"):
        path = destination / f"{role}-sandbox"
        shutil.copy2(SANDBOX_SOURCE, path)
        path.chmod(0o755)
        wrappers.append(path)
    return wrappers[0], wrappers[1]


def _agent_environment(
    run_dir: Path,
    profile: dict[str, Any],
    method: str,
) -> dict[str, str]:
    provider = profile["agent_provider"]
    provider_env_names = {
        name
        for name in (provider["base_url_env"], provider["api_key_env"])
        if isinstance(name, str)
    }
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        *provider_env_names,
    }
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment = dict(configure_temp_environment(environment))
    real_codex = shutil.which("codex")
    real_pi = shutil.which("pi")
    environment.update(
        {
            "AIBENCH_AGENT_ROLE": "pi" if method in PI_METHODS else "codex",
            "AIBENCH_METHOD": method,
            "AIBENCH_HIDDEN_CHECKOUT": str(UPSTREAM_CHECKOUT),
            "AIBENCH_CELL_ROOT": str(run_dir),
            "AIBENCH_REAL_CODEX_BIN": str(Path(real_codex or "")),
            "AIBENCH_REAL_PI_BIN": str(Path(real_pi or "")),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if real_pi is not None:
        environment[REAL_PI_BIN_ENV] = real_pi
    return environment


def _run_cell(
    profile: dict[str, Any],
    cell: dict[str, Any],
) -> dict[str, Any]:
    run_dir = Path(cell["run_dir"])
    codex_wrapper, pi_wrapper = _sandbox_binaries(run_dir)
    provider_id, model_id = split_model(profile)
    provider = profile["agent_provider"]
    base_url = os.environ.get(provider["base_url_env"])
    if not base_url or not os.environ.get(provider["api_key_env"]):
        raise RuntimeError("profile-selected provider URL and credential are required")
    command = [
        sys.executable,
        str(STANDALONE_CONTROLLER),
        "run",
        "--run-dir",
        str(run_dir),
        "--codex-bin",
        str(codex_wrapper),
        "--pi-bin",
        str(pi_wrapper),
        "--model",
        model_id,
        "--pi-provider-id",
        provider_id,
        "--pi-api",
        "openai-responses",
        "--pi-api-key-env",
        provider["api_key_env"],
        "--api-base",
        base_url,
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=_agent_environment(run_dir, profile, cell["method"]),
        capture_output=True,
        text=True,
        check=False,
    )
    (run_dir / "controller.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (run_dir / "controller.stderr.log").write_text(completed.stderr, encoding="utf-8")
    experiment = json.loads((run_dir / "experiment.json").read_text(encoding="utf-8"))
    complete = completed.returncode == 0 and experiment.get("status") == "finished"
    return {
        "state": "completed" if complete else "partial",
        "returncode": completed.returncode,
        "sandbox": {
            "kind": "bubblewrap",
            "hidden_checkout_masked": True,
            "wrapper": str(pi_wrapper if cell["method"] in PI_METHODS else codex_wrapper),
        },
        "error": (
            None
            if complete
            else (experiment.get("execution") or {}).get("result_incomplete_reason")
            or completed.stderr.strip()
            or "aibench method did not finish"
        ),
    }


def execute_campaign(destination: Path) -> int:
    campaign_path = destination / "campaign.json"
    campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    if campaign["state"] != "prepared":
        raise RuntimeError(f"campaign is not prepared: {campaign['state']}")
    profile = campaign["profile_snapshot"]
    campaign["state"] = "running"
    campaign["execution_started_at"] = utc_now()
    write_json(campaign_path, campaign)
    pending = [cell for cell in campaign["cells"] if cell["state"] == "prepared"]
    with ThreadPoolExecutor(max_workers=profile["cell_concurrency"]) as pool:
        futures = {}
        for cell in pending:
            cell["state"] = "running"
            cell["started_at"] = utc_now()
            futures[pool.submit(_run_cell, profile, cell)] = cell
        write_json(campaign_path, campaign)
        for future in as_completed(futures):
            cell = futures[future]
            try:
                cell.update(future.result())
            except BaseException as error:
                cell["state"] = "failed"
                cell["error"] = f"{type(error).__name__}: {error}"
            cell["finished_at"] = utc_now()
            write_json(campaign_path, campaign)
    if all(cell["state"] == "completed" for cell in campaign["cells"]):
        campaign["state"] = "completed"
    elif any(cell["state"] in TERMINAL_CELL_STATES for cell in campaign["cells"]):
        campaign["state"] = "partial"
    else:
        campaign["state"] = "failed"
    campaign["execution_finished_at"] = utc_now()
    write_json(campaign_path, campaign)
    return 0 if campaign["state"] == "completed" else 2


def status_payload(destination: Path) -> dict[str, Any]:
    campaign = json.loads((destination / "campaign.json").read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    cells = []
    for cell in campaign["cells"]:
        observed = dict(cell)
        experiment = Path(cell["run_dir"]) / "experiment.json"
        if experiment.is_file():
            payload = json.loads(experiment.read_text(encoding="utf-8"))
            observed["method_state"] = payload.get("status")
            observed["selected_lane"] = (payload.get("execution") or {}).get(
                "selected_lane"
            )
        counts[cell["state"]] = counts.get(cell["state"], 0) + 1
        cells.append(observed)
    return {
        "campaign_id": campaign["campaign_id"],
        "state": campaign["state"],
        "counts": counts,
        "cells": cells,
    }
