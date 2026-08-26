"""Inventory, provisioning, and doctor gates for Frontier-Engineering."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from bench_runtime_paths import configure_temp_environment
from bench_goal_plus.upstreams import registered_upstream_branch

from .config import (
    GOAL_PLUS_ROOT,
    ROOT,
    UPSTREAM_ROOT,
    V1_LITE_TASKS,
    profile_nvidia_cuda_tasks,
    write_json,
)


REQUIRED_METADATA = (
    "initial_program.txt",
    "candidate_destination.txt",
    "eval_command.txt",
    "copy_files.txt",
    "readonly_files.txt",
)
REQUIRED_CONTROL_FILES = (
    "frontier_eval/conf/batch/v1_lite.yaml",
    "frontier_eval/conf/algorithm/openevolve.yaml",
    "frontier_eval/conf/llm/openai_compatible.yaml",
    "frontier_eval/tasks/unified/evaluator/python.py",
    "frontier_eval/tasks/unified/spec.py",
    "scripts/env/ensure_uv_env.py",
)


def _openevolve_chat_probe(profile: dict[str, Any]) -> dict[str, Any]:
    base_url = str(os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
    api_key = str(os.environ.get("OPENAI_API_KEY") or "")
    if not base_url or not api_key:
        return {
            "ok": False,
            "http_status": None,
            "error": "OPENAI_BASE_URL and OPENAI_API_KEY are required",
        }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(
            {
                "model": profile["model"],
                "messages": [
                    {"role": "user", "content": "Reply with exactly WIRE_OK."}
                ],
                "max_tokens": 64,
                "temperature": 0.7,
                "reasoning_effort": profile["reasoning_effort"],
            }
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "bench-goal-plus-frontier-openevolve-doctor/1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read()
        status = error.code
    except urllib.error.URLError as error:
        return {
            "ok": False,
            "http_status": None,
            "transport_error": type(error.reason).__name__,
        }
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    result: dict[str, Any] = {
        "ok": 200 <= status < 300,
        "http_status": status,
    }
    if result["ok"]:
        if isinstance(payload.get("object"), str):
            result["object"] = payload["object"]
        if isinstance(payload.get("model"), str):
            result["model"] = payload["model"]
    elif isinstance(payload.get("error"), dict):
        result["error"] = {
            key: payload["error"][key]
            for key in ("type", "code", "param")
            if isinstance(payload["error"].get(key), (str, int, float, bool))
        }
    return result


def _openevolve_agent_checks(profile: dict[str, Any]) -> list[dict[str, Any]]:
    runtime_ok, version = command_output(
        [
            str(UPSTREAM_ROOT / ".venvs/frontier-eval-driver/bin/python"),
            "-c",
            (
                "from importlib.metadata import version; "
                "print(version('openevolve'))"
            ),
        ],
        cwd=UPSTREAM_ROOT,
    )
    base_present = bool(os.environ.get("OPENAI_BASE_URL"))
    key_present = bool(os.environ.get("OPENAI_API_KEY"))
    probe = (
        _openevolve_chat_probe(profile)
        if runtime_ok and base_present and key_present
        else {"ok": False, "http_status": None, "error": "provider input missing"}
    )
    return [
        {
            "kind": "search-runtime",
            "name": "openevolve",
            "version": version,
            "expected_version": "0.2.26",
            "passed": runtime_ok and version == "0.2.26",
        },
        {
            "kind": "agent-provider",
            "name": "openevolve-openai-compatible",
            "model": profile["model"],
            "reasoning_effort": profile["reasoning_effort"],
            "wire_api": "openai-completions",
            "api_base_env": "OPENAI_BASE_URL",
            "api_base_present": base_present,
            "api_key_env": "OPENAI_API_KEY",
            "api_key_present": key_present,
            "probe": probe,
            "passed": bool(probe.get("ok")),
        },
    ]


def _openevolve_config_check(profile: dict[str, Any]) -> dict[str, Any]:
    from .openevolve_runtime import build_command

    output_dir = ROOT / ".tmp/frontier-engineering/openevolve-config-probe"
    command = build_command(
        profile,
        task_id=str(profile["task_ids"][0]),
        output_dir=output_dir,
    )
    command.extend(["--cfg", "job"])
    probe_environment = configure_temp_environment(os.environ.copy())
    probe_environment["OPENAI_API_BASE"] = "https://invalid.example/v1"
    probe_environment["OPENAI_API_KEY"] = "DUMMY_CONFIG_PROBE_KEY"
    ok, output = command_output(
        command,
        cwd=UPSTREAM_ROOT,
        environment=probe_environment,
    )
    iterations = int(profile["iterations"])
    expected = {
        f"iterations: {iterations}": "iterations",
        "random_seed: 42": "random_seed",
        "parallel_evaluations: 1": "parallel_evaluations",
        f"reasoning_effort: {profile['reasoning_effort']}": "reasoning_effort",
        f"model: {profile['model']}": "model",
    }
    missing = [label for text, label in expected.items() if text not in output]
    return {
        "kind": "native-config-resolution",
        "name": f"frontier-engineering-openevolve-{profile['openevolve_protocol']}-protocol",
        "iterations": iterations,
        "random_seed": 42,
        "parallel_evaluations": 1,
        "model": profile["model"],
        "reasoning_effort": profile["reasoning_effort"],
        "missing": missing,
        "passed": ok and not missing,
    }


def command_output(
    command: list[str],
    *,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[bool, str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (completed.stdout or completed.stderr).strip()
    return completed.returncode == 0, output


def _pi_agent_checks(profile: dict[str, Any]) -> list[dict[str, Any]]:
    from experiments.openevolve_compare.experiment import (
        PI_PROVIDER_ID,
        write_pi_models_config,
    )

    path = shutil.which("pi")
    version_ok, version = command_output([path, "--version"]) if path else (False, "")
    base_present = bool(os.environ.get("OPENAI_BASE_URL"))
    key_present = bool(os.environ.get("OPENAI_API_KEY"))
    qualified_model = f"{PI_PROVIDER_ID}/{profile['model']}"
    model_visible = False
    model_error = None
    if path and version_ok and base_present and key_present:
        pi_home = ROOT / ".tmp/frontier-engineering/pi-doctor" / profile["id"]
        write_pi_models_config(
            pi_home,
            api_base=str(os.environ["OPENAI_BASE_URL"]),
            model=str(profile["model"]),
            reasoning_effort=str(profile["reasoning_effort"]),
            pi_bin=path,
        )
        probe_environment = configure_temp_environment(os.environ.copy())
        probe_environment["PI_CODING_AGENT_DIR"] = str(pi_home)
        visible_ok, output = command_output(
            [path, "--offline", "--list-models", qualified_model],
            environment=probe_environment,
        )
        model_visible = visible_ok and any(
            columns[:2] == [PI_PROVIDER_ID, str(profile["model"])]
            for line in output.splitlines()
            for columns in [line.split()]
            if len(columns) >= 2
        )
        if not model_visible:
            model_error = "configured provider/model was not visible to Pi"
    return [
        {
            "kind": "agent",
            "name": "pi",
            "path": path,
            "version": version,
            "passed": version_ok,
        },
        {
            "kind": "agent-provider",
            "name": "pi-openai-compatible",
            "provider": PI_PROVIDER_ID,
            "model": qualified_model,
            "api_base_env": "OPENAI_BASE_URL",
            "api_base_present": base_present,
            "api_key_env": "OPENAI_API_KEY",
            "api_key_present": key_present,
            "model_visible": model_visible,
            "error": model_error,
            "passed": version_ok
            and base_present
            and key_present
            and model_visible,
        },
    ]


def git_value(root: Path, *args: str) -> str | None:
    ok, output = command_output(["git", "-C", str(root), *args])
    return output if ok and output else None


def local_inventory(profile: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    checkout_present = (UPSTREAM_ROOT / ".git").exists()
    checks.append(
        {
            "kind": "checkout",
            "path": str(UPSTREAM_ROOT),
            "present": checkout_present,
            "branch": git_value(UPSTREAM_ROOT, "symbolic-ref", "--short", "HEAD")
            if checkout_present
            else None,
            "commit": git_value(UPSTREAM_ROOT, "rev-parse", "HEAD")
            if checkout_present
            else None,
        }
    )
    cuda_tasks = profile_nvidia_cuda_tasks(profile)
    checks.append(
        {
            "kind": "accelerator-policy",
            "policy": profile["accelerator_policy"],
            "selected_cuda_tasks": list(cuda_tasks),
            "gpu_probe_stage": "doctor" if cuda_tasks else "not-required",
            "present": profile["accelerator_policy"] != "cpu-only" or not cuda_tasks,
        }
    )
    control_missing = [
        str(UPSTREAM_ROOT / relative)
        for relative in REQUIRED_CONTROL_FILES
        if not (UPSTREAM_ROOT / relative).is_file()
    ]
    checks.append(
        {
            "kind": "native-control",
            "path": str(UPSTREAM_ROOT),
            "present": not control_missing,
            "missing": control_missing,
        }
    )
    for task_id in profile["task_ids"]:
        task = V1_LITE_TASKS[task_id]
        task_dir = UPSTREAM_ROOT / "benchmarks" / task_id
        metadata = [task_dir / "frontier_eval" / name for name in REQUIRED_METADATA]
        seed_reference = task_dir / "frontier_eval/initial_program.txt"
        seed = None
        if seed_reference.is_file():
            value = seed_reference.read_text(encoding="utf-8").strip()
            seed = task_dir / value if value else None
        missing = [str(path) for path in metadata if not path.is_file()]
        if seed is None or not seed.is_file():
            missing_path = str(seed or seed_reference)
            if missing_path not in missing:
                missing.append(missing_path)
        checks.append(
            {
                "kind": "task",
                "task_id": task_id,
                "path": str(task_dir),
                "present": not missing,
                "missing": missing,
            }
        )
    for env_name in sorted(
        {"frontier-eval-driver"}
        | {V1_LITE_TASKS[task_id].runtime_env for task_id in profile["task_ids"]}
        | {
            task.runtime_python_env
            for task_id in profile["task_ids"]
            for task in [V1_LITE_TASKS[task_id]]
            if task.runtime_python_env
        }
    ):
        python = UPSTREAM_ROOT / ".venvs" / env_name / "bin/python"
        checks.append(
            {
                "kind": "runtime",
                "name": env_name,
                "path": str(python),
                "present": python.is_file() and os.access(python, os.X_OK),
            }
        )
    return {
        "schema_version": 1,
        "benchmark": "frontier-engineering",
        "suite": "v1-lite",
        "profile": profile["id"],
        "read_only": True,
        "acquisition_attempted": False,
        "checks": checks,
        "passed": all(item["present"] for item in checks),
    }


def provision(profile: dict[str, Any]) -> dict[str, Any]:
    configure_temp_environment()
    if not (UPSTREAM_ROOT / "scripts/env/ensure_uv_env.py").is_file():
        raise FileNotFoundError(
            "full Frontier-Engineering checkout is missing; run managed bootstrap first"
        )
    env_names = sorted(
        {"frontier-eval-driver"}
        | {V1_LITE_TASKS[task_id].runtime_env for task_id in profile["task_ids"]}
        | {
            task.runtime_python_env
            for task_id in profile["task_ids"]
            for task in [V1_LITE_TASKS[task_id]]
            if task.runtime_python_env
        }
    )
    commands = []
    environment = configure_temp_environment(os.environ.copy())
    environment["PYTHONNOUSERSITE"] = "1"
    for env_name in env_names:
        spec = UPSTREAM_ROOT / "scripts/env/specs" / f"{env_name}.json"
        command = [
            "python3",
            "scripts/env/ensure_uv_env.py",
            str(spec),
            "--root",
            str(UPSTREAM_ROOT),
            "--envs-dir",
            str(UPSTREAM_ROOT / ".venvs"),
        ]
        subprocess.run(command, cwd=UPSTREAM_ROOT, env=environment, check=True)
        commands.append(command)
    return {
        "schema_version": 1,
        "profile": profile["id"],
        "environments": env_names,
        "commands": commands,
    }


def _runtime_probe(env_name: str, modules: list[str]) -> dict[str, Any]:
    python = UPSTREAM_ROOT / ".venvs" / env_name / "bin/python"
    code = "; ".join(f"import {module}" for module in modules)
    ok, output = command_output([str(python), "-c", code], cwd=UPSTREAM_ROOT)
    return {
        "kind": "runtime-import",
        "name": env_name,
        "modules": modules,
        "passed": ok,
        "detail": output or None,
    }


def _nvidia_driver_probe() -> dict[str, Any]:
    path = shutil.which("nvidia-smi")
    command = [
        path,
        "--query-gpu=name,driver_version",
        "--format=csv,noheader",
    ] if path else []
    ok, output = command_output(command) if command else (False, "nvidia-smi not found")
    return {
        "kind": "host-accelerator",
        "name": "nvidia-driver",
        "path": path,
        "passed": ok and bool(output),
        "detail": output or None,
    }


def _cuda_runtime_probe(env_name: str) -> dict[str, Any]:
    python = UPSTREAM_ROOT / ".venvs" / env_name / "bin/python"
    code = (
        "import json, torch; "
        "print(json.dumps({'torch_version': torch.__version__, "
        "'cuda_runtime': torch.version.cuda, "
        "'cuda_available': torch.cuda.is_available(), "
        "'device_count': torch.cuda.device_count()}))"
    )
    ok, output = command_output([str(python), "-c", code], cwd=UPSTREAM_ROOT)
    details: dict[str, Any] | None = None
    if ok:
        try:
            parsed = json.loads(output)
            details = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            details = None
    passed = bool(
        ok
        and details
        and details.get("cuda_available") is True
        and isinstance(details.get("device_count"), int)
        and details["device_count"] > 0
        and details.get("cuda_runtime")
    )
    return {
        "kind": "runtime-accelerator",
        "name": env_name,
        "python": str(python),
        "passed": passed,
        "details": details,
        "error": None if details is not None else output or "invalid CUDA probe output",
    }


def _accelerator_checks(profile: dict[str, Any]) -> list[dict[str, Any]]:
    cuda_tasks = profile_nvidia_cuda_tasks(profile)
    if not cuda_tasks:
        return []
    env_names = sorted(
        {
            V1_LITE_TASKS[task_id].runtime_python_env
            or V1_LITE_TASKS[task_id].runtime_env
            for task_id in cuda_tasks
        }
    )
    return [_nvidia_driver_probe(), *(_cuda_runtime_probe(name) for name in env_names)]


def _seed_probe(task_id: str) -> dict[str, Any]:
    task = V1_LITE_TASKS[task_id]
    task_dir = UPSTREAM_ROOT / "benchmarks" / task_id
    initial = (task_dir / "frontier_eval/initial_program.txt").read_text().strip()
    candidate = task_dir / initial
    command = [
        str(UPSTREAM_ROOT / ".venvs/frontier-eval-driver/bin/python"),
        str(Path(__file__).resolve().with_name("evaluator_bridge.py")),
        "--upstream-root",
        str(UPSTREAM_ROOT),
        "--task-id",
        task_id,
        "--candidate",
        str(candidate),
        "--runtime-env",
        task.runtime_env,
    ]
    if task.runtime_python_env:
        command.extend(["--runtime-python-env", task.runtime_python_env])
    probe_environment = os.environ.copy()
    probe_environment["FRONTIER_EVAL_EVALUATOR_TIMEOUT_S"] = str(
        task.evaluator_timeout_seconds
    )
    completed = subprocess.run(
        command,
        cwd=UPSTREAM_ROOT,
        env=probe_environment,
        capture_output=True,
        text=True,
        timeout=task.evaluator_timeout_seconds,
        check=False,
    )
    metrics: dict[str, Any] | None = None
    error = completed.stderr.strip() or None
    if completed.returncode == 0:
        try:
            metrics = (json.loads(completed.stdout).get("metrics") or {})
        except json.JSONDecodeError as exception:
            error = f"invalid evaluator JSON: {exception}"
    valid = bool(
        metrics
        and metrics.get("valid") in {True, 1, 1.0}
        and isinstance(metrics.get("combined_score"), (int, float))
    )
    return {
        "kind": "official-seed-evaluation",
        "task_id": task_id,
        "passed": completed.returncode == 0 and valid,
        "returncode": completed.returncode,
        "metrics": metrics,
        "error": error,
    }


def doctor(
    profile: dict[str, Any],
    *,
    output: Path | None = None,
    local_assets_only: bool = False,
    allow_missing_local_assets: bool = False,
) -> int:
    configure_temp_environment()
    inventory = local_inventory(profile)
    if local_assets_only:
        print(json.dumps(inventory, indent=2, ensure_ascii=False))
        return 0 if inventory["passed"] or allow_missing_local_assets else 2

    checks: list[dict[str, Any]] = [
        {"kind": "local-inventory", "passed": inventory["passed"], "details": inventory},
    ]
    for executable in ("git", "uv"):
        path = shutil.which(executable)
        checks.append(
            {"kind": "executable", "name": executable, "path": path, "passed": path is not None}
        )
    checks.extend(_accelerator_checks(profile))
    if any("codex" in method for method in profile["methods"]):
        path = shutil.which("codex")
        ok, version = command_output([path, "--version"]) if path else (False, "")
        checks.append(
            {"kind": "agent", "name": "codex", "path": path, "version": version, "passed": ok}
        )
    if any(method.endswith("-pi") for method in profile["methods"]):
        checks.extend(_pi_agent_checks(profile))
    if "openevolve" in profile["methods"]:
        checks.extend(_openevolve_agent_checks(profile))
        checks.append(_openevolve_config_check(profile))
    managed_checkouts = [("frontier_engineering", UPSTREAM_ROOT, "main")]
    if any(method.startswith("goal-plus-") for method in profile["methods"]):
        managed_checkouts.append(
            (
                "goal_plus",
                GOAL_PLUS_ROOT,
                registered_upstream_branch("goal_plus", repository_root=ROOT),
            )
        )
    for name, root, expected_branch in managed_checkouts:
        branch = git_value(root, "symbolic-ref", "--short", "HEAD")
        dirty = git_value(root, "status", "--porcelain")
        checks.append(
            {
                "kind": "managed-checkout",
                "name": name,
                "path": str(root),
                "branch": branch,
                "commit": git_value(root, "rev-parse", "HEAD"),
                "dirty": bool(dirty),
                "passed": branch == expected_branch and not dirty,
            }
        )
    required_envs = {
        V1_LITE_TASKS[task_id].runtime_env for task_id in profile["task_ids"]
    }
    required_envs.update(
        task.runtime_python_env
        for task_id in profile["task_ids"]
        for task in [V1_LITE_TASKS[task_id]]
        if task.runtime_python_env
    )
    required_envs.add("frontier-eval-driver")
    if "frontier-eval-driver" in required_envs:
        modules = ["hydra", "omegaconf", "yaml"]
        if "openevolve" in profile["methods"]:
            modules.extend(["openevolve", "openai"])
        checks.append(_runtime_probe("frontier-eval-driver", modules))
    if "frontier-v1-main" in required_envs:
        checks.append(
            _runtime_probe(
                "frontier-v1-main", ["numpy", "scipy", "qiskit", "job_shop_lib", "pybullet"]
            )
        )
    if "frontier-v1-summit" in required_envs:
        checks.append(_runtime_probe("frontier-v1-summit", ["summit"]))
    if profile["doctor_seed_evaluation"] and all(item["passed"] for item in checks):
        checks.extend(_seed_probe(task_id) for task_id in profile["task_ids"])
    payload = {
        "schema_version": 1,
        "benchmark": "frontier-engineering",
        "profile": profile["id"],
        "methods": profile["methods"],
        "model": profile["model"],
        "checks": checks,
        "passed": all(item["passed"] for item in checks),
    }
    if output is not None:
        write_json(output.expanduser().absolute(), payload)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if payload["passed"] else 2
