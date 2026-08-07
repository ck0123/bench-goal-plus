"""Read-only inventory and full host/runtime doctor for SWE-bench Verified."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from bench_goal_plus.loopback_bridge import (
    bridged_url,
    default_route_ipv4,
    loopback_target,
    start_socket_bridge,
)
from bench_runtime_paths import (
    configure_temp_environment,
    ensure_temp_root,
    temporary_directory,
)

from .config import (
    GOAL_PLUS_ROOT,
    ROOT,
    SWEBENCH_ROOT,
    SweBenchContractError,
    managed_upstream_branch,
    utc_now,
    write_json,
)


CODEX_ARCHIVE = Path.home() / ".cache/sforge/codex/codex-0.144.1-linux-x64.tgz"
CODEX_RUNTIME_TMPFS = "/opt/codex:rw,exec,nosuid,nodev,size=512m"
CODEX_HOME_TMPFS = "/opt/codex-home:rw,nosuid,nodev,size=256m"
GOAL_PLUS_DEPENDENCY_LOCK = (
    ROOT / "environment" / "swe-bench-goal-plus-requirements.lock"
)
GOAL_PLUS_VISIBLE_VERIFIER = (
    ROOT
    / "experiments"
    / "swe_bench_verified"
    / "verifiers"
    / "visible_test_verifier.py"
)
GOAL_PLUS_CONTROLLER = (
    ROOT / "experiments" / "swe_bench_verified" / "goal_plus_controller.py"
)
PI_API_KEYS = {
    "zai": ("ZAI_API_KEY", "ZAI_CODING_CN_API_KEY"),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
}

PI_RESPONSES_PROVIDER_COMPAT = {
    "deepseek-responses": {
        "provider": {
            "supportsDeveloperRole": False,
            "supportsReasoningEffort": True,
            "supportsStore": False,
        },
        "context_window": 1_000_000,
        "max_tokens": 384_000,
    }
}


def run_capture(
    command: list[str],
    *,
    timeout: int = 60,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def image_inventory(profile: dict[str, Any]) -> dict[str, Any]:
    images: list[dict[str, Any]] = []
    for task in profile["tasks"]:
        reference = task["image"]
        inspected = run_capture(["docker", "image", "inspect", reference])
        entry: dict[str, Any] = {
            "task_id": task["instance_id"],
            "reference": reference,
            "present": inspected.returncode == 0,
        }
        if inspected.returncode == 0:
            try:
                values = json.loads(inspected.stdout)
                value = values[0]
                entry.update(
                    {
                        "image_id": value.get("Id"),
                        "repo_tags": value.get("RepoTags") or [],
                        "repo_digests": value.get("RepoDigests") or [],
                        "size_bytes": value.get("Size"),
                        "architecture": value.get("Architecture"),
                        "os": value.get("Os"),
                    }
                )
            except (json.JSONDecodeError, IndexError, TypeError):
                entry["inspect_error"] = "docker image inspect returned invalid JSON"
        else:
            entry["inspect_error"] = inspected.stderr.strip() or inspected.stdout.strip()
        images.append(entry)

    containers_result = run_capture(
        [
            "docker",
            "ps",
            "-a",
            "--no-trunc",
            "--format",
            "{{json .}}",
        ]
    )
    containers: list[dict[str, Any]] = []
    if containers_result.returncode == 0:
        for line in containers_result.stdout.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                containers.append(value)
    for image in images:
        reference = image["reference"]
        image_id = str(image.get("image_id") or "")
        image["associated_containers"] = [
            {
                "id": item.get("ID"),
                "name": item.get("Names"),
                "state": item.get("State"),
                "status": item.get("Status"),
            }
            for item in containers
            if item.get("Image") == reference
            or (image_id and str(item.get("Image", "")).startswith(image_id))
        ]

    return {
        "schema_version": 1,
        "benchmark_id": "swe-bench-verified",
        "profile": profile["id"],
        "dataset": {
            "name": profile["dataset"]["name"],
            "split": profile["dataset"]["split"],
            "revision": profile["dataset"]["revision"],
            "task_ids": profile["task_ids"],
            "metadata_source": "pinned profile",
        },
        "images": images,
        "docker_ps_ok": containers_result.returncode == 0,
        "read_only": True,
        "acquisition_attempted": False,
        "ok": bool(
            containers_result.returncode == 0
            and all(
                item.get("present")
                and item.get("architecture") == "amd64"
                and item.get("os") == "linux"
                for item in images
            )
        ),
        "checked_at": utc_now(),
    }


def _check(name: str, passed: bool, **details: Any) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), **details}


def _git_value_at(root: Path, *args: str) -> str | None:
    result = run_capture(["git", "-C", str(root), *args])
    return result.stdout.strip() if result.returncode == 0 else None


def _git_value(*args: str) -> str | None:
    return _git_value_at(SWEBENCH_ROOT, *args)


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def resolve_codex_runtime(profile: dict[str, Any]) -> dict[str, Any]:
    archive = Path(os.environ.get("SWEBENCH_CODEX_RUNTIME_ARCHIVE", CODEX_ARCHIVE))
    provider = profile["agent_provider"]
    base_url_env = str(provider["base_url_env"])
    api_key_env = str(provider["api_key_env"])
    return {
        "archive": archive,
        "archive_present": archive.is_file(),
        "provider_id": str(provider["id"]),
        "provider_name": str(provider["name"]),
        "auth_mode": str(provider["auth_mode"]),
        "wire_api": str(provider["wire_api"]),
        "base_url_env": base_url_env,
        "api_key_env": api_key_env,
        "api_base_url": os.environ.get(base_url_env),
        "credential_present": bool(os.environ.get(api_key_env)),
    }


def _safe_response_facts(
    status: int | None, payload: Any, *, expected_model: str
) -> dict[str, Any]:
    body = payload if isinstance(payload, dict) else {}
    result = {
        "passed": bool(
            status is not None
            and 200 <= status < 300
            and body.get("object") == "response"
            and body.get("model") == expected_model
            and body.get("status") == "completed"
        ),
        "http_status": status,
        "object": body.get("object"),
        "model": body.get("model"),
        "response_status": body.get("status"),
    }
    error = body.get("error")
    if isinstance(error, dict):
        result["error_type"] = error.get("type")
        result["error_code"] = error.get("code")
    return result


def openai_responses_probe(
    base_url: str, *, api_key_env: str, model: str, timeout: float = 45.0
) -> dict[str, Any]:
    api_key = os.environ.get(api_key_env)
    if not api_key:
        return {
            "passed": False,
            "http_status": None,
            "error": f"missing credential environment variable: {api_key_env}",
        }
    payload = json.dumps(
        {
            "model": model,
            "input": "Reply with exactly WIRE_OK.",
            "max_output_tokens": 16,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/responses",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            decoded = json.loads(response.read().decode("utf-8"))
            return _safe_response_facts(
                response.status, decoded, expected_model=model
            )
    except urllib.error.HTTPError as error:
        try:
            decoded = json.loads(error.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            decoded = None
        return _safe_response_facts(error.code, decoded, expected_model=model)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        return {"passed": False, "http_status": None, "error": str(error)}


@contextmanager
def routed_codex_runtime(
    profile: dict[str, Any],
    destination: Path,
    *,
    bridge_listen_host: str | None = None,
) -> Iterator[dict[str, Any]]:
    runtime = resolve_codex_runtime(profile)
    if not runtime["api_base_url"] or not runtime["credential_present"]:
        raise SweBenchContractError(
            "Plain Codex requires the profile-selected OpenAI-compatible base URL and key"
        )
    runtime["runtime_api_base_url"] = str(runtime["api_base_url"])
    runtime["bridge_host"] = None
    runtime["bridge"] = None
    closer = None
    try:
        target = loopback_target(str(runtime["api_base_url"]))
        if bridge_listen_host is not None and target is None:
            raise SweBenchContractError(
                "an isolated Agent network requires a loopback provider endpoint"
            )
        if target is not None:
            bridge_host = bridge_listen_host or default_route_ipv4(root=ROOT)
            _, metadata, closer = start_socket_bridge(
                destination,
                name="agent-api",
                listen_host=bridge_host,
                target_host=target[0],
                target_port=target[1],
                root=ROOT,
                display_path=lambda path: str(path.relative_to(ROOT)),
            )
            runtime["bridge_host"] = bridge_host
            runtime["bridge"] = metadata
            runtime["runtime_api_base_url"] = bridged_url(
                str(runtime["api_base_url"]),
                bridge_host,
                int(metadata["listen_port"]),
            )
        yield runtime
    finally:
        if closer is not None:
            closer()


def resolve_pi_runtime(profile: dict[str, Any]) -> dict[str, Any]:
    node_command = shutil.which("node")
    pi_command = shutil.which("pi")
    node_binary = Path(node_command).resolve() if node_command else None
    pi_cli = Path(pi_command).resolve() if pi_command else None
    node_root = node_binary.parent.parent if node_binary else None
    package_root = pi_cli.parent.parent if pi_cli else None
    model = str(profile["model"])
    provider, _, model_id = model.partition("/")
    provider_contract = profile.get("agent_provider")
    if provider_contract is not None:
        base_url_env = str(provider_contract["base_url_env"])
        api_key_env = str(provider_contract["api_key_env"])
        key_source = api_key_env if os.environ.get(api_key_env) else None
        custom_provider = True
    else:
        base_url_env = None
        api_key_env = None
        key_names = PI_API_KEYS.get(provider, ())
        key_source = next((name for name in key_names if os.environ.get(name)), None)
        custom_provider = False
    runtime = {
        "node_root": node_root,
        "node_binary": node_binary,
        "package_root": package_root,
        "pi_cli": pi_cli,
        "provider": provider,
        "model_id": model_id,
        "credential_env": key_source,
        "credential_present": key_source is not None,
        "custom_provider": custom_provider,
        "models_file": None,
    }
    if custom_provider:
        runtime.update(
            {
                "provider_name": str(provider_contract["name"]),
                "auth_mode": str(provider_contract["auth_mode"]),
                "wire_api": str(provider_contract["wire_api"]),
                "base_url_env": base_url_env,
                "api_key_env": api_key_env,
                "api_base_url": os.environ.get(str(base_url_env)),
            }
        )
    return runtime


def write_pi_models_config(
    path: Path, runtime: dict[str, Any], *, reasoning_effort: str
) -> None:
    credential_env = str(runtime["credential_env"])
    provider_compat = PI_RESPONSES_PROVIDER_COMPAT.get(
        str(runtime["provider"]), {}
    )
    context_window = int(provider_compat.get("context_window", 272_000))
    max_tokens = int(provider_compat.get("max_tokens", 32_000))
    provider_payload: dict[str, Any] = {
        "baseUrl": str(runtime["runtime_api_base_url"]),
        "api": "openai-responses",
        "apiKey": f"${credential_env}",
        "authHeader": True,
        "models": [
            {
                "id": str(runtime["model_id"]),
                "name": f"{runtime['model_id']} benchmark proxy",
                "reasoning": True,
                "thinkingLevelMap": {
                    reasoning_effort: reasoning_effort,
                },
                "input": ["text"],
                "contextWindow": context_window,
                "maxTokens": max_tokens,
                "cost": {
                    "input": 0,
                    "output": 0,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                },
                "compat": {
                    "supportsDeveloperRole": True,
                    "supportsReasoningEffort": True,
                },
            }
        ],
    }
    if "provider" in provider_compat:
        provider_payload["compat"] = dict(provider_compat["provider"])
        provider_payload["models"][0].pop("compat")
    payload = {
        "providers": {
            str(runtime["provider"]): provider_payload
        }
    }
    write_json(path, payload)


@contextmanager
def routed_pi_runtime(
    profile: dict[str, Any],
    destination: Path,
    *,
    goal_plus: bool = False,
    bridge_listen_host: str | None = None,
) -> Iterator[dict[str, Any]]:
    runtime = (
        resolve_goal_plus_runtime(profile) if goal_plus else resolve_pi_runtime(profile)
    )
    if not runtime["custom_provider"]:
        yield runtime
        return
    if not runtime.get("api_base_url") or not runtime["credential_present"]:
        raise SweBenchContractError(
            "Pi custom provider requires the profile-selected OpenAI-compatible base URL and key"
        )
    runtime["runtime_api_base_url"] = str(runtime["api_base_url"])
    runtime["bridge_host"] = None
    runtime["bridge"] = None
    closer = None
    try:
        target = loopback_target(str(runtime["api_base_url"]))
        if bridge_listen_host is not None and target is None:
            raise SweBenchContractError(
                "an isolated Agent network requires a loopback provider endpoint"
            )
        if target is not None:
            bridge_host = bridge_listen_host or default_route_ipv4(root=ROOT)
            _, metadata, closer = start_socket_bridge(
                destination,
                name="pi-agent-api",
                listen_host=bridge_host,
                target_host=target[0],
                target_port=target[1],
                root=ROOT,
                display_path=lambda path: str(path.relative_to(ROOT)),
            )
            runtime["bridge_host"] = bridge_host
            runtime["bridge"] = metadata
            runtime["runtime_api_base_url"] = bridged_url(
                str(runtime["api_base_url"]),
                bridge_host,
                int(metadata["listen_port"]),
            )
        models_file = destination / "provider-runtime" / "models.json"
        write_pi_models_config(
            models_file,
            runtime,
            reasoning_effort=str(profile["reasoning_effort"]),
        )
        runtime["models_file"] = models_file
        yield runtime
    finally:
        if closer is not None:
            closer()


def goal_plus_install_script(*, include_pi: bool = True) -> str:
    installer = (
        "import os, sys; "
        "cache = os.stat('/opt/pip-cache'); "
        "os.chown('/opt/agent-tmp', cache.st_uid, cache.st_gid); "
        "os.chown('/opt/goal-plus-runtime', cache.st_uid, cache.st_gid); "
        "os.setgroups([]); "
        "os.setgid(cache.st_gid); "
        "os.setuid(cache.st_uid); "
        "os.execv(sys.executable, [sys.executable, '-m', 'pip', 'install', "
        "'--disable-pip-version-check', '--no-input', "
        "'--target', '/opt/goal-plus-runtime', "
        "'-r', '/opt/goal-plus-runtime-requirements.lock'])"
    )
    commands = [
        "export PATH=/opt/goal-plus-bin:/opt/node/bin:$PATH",
        "mkdir -p /opt/goal-plus-runtime /opt/goal-plus-bin",
        f'python -c "{installer}"',
        "printf '#!/bin/sh\\nexec python -m goal_plus.server \"$@\"\\n' "
        "> /opt/goal-plus-bin/goal-plus",
        "chmod 0555 /opt/goal-plus-bin/goal-plus",
    ]
    if include_pi:
        commands.extend(
            [
                "mkdir -p /opt/pi-home/.pi/agent",
                "ln -sf /opt/pi/dist/cli.js /opt/goal-plus-bin/pi",
            ]
        )
    return " && ".join(commands)


def goal_plus_runtime_environment() -> dict[str, str]:
    return {
        "HOME": "/opt/agent-tmp",
        "TMPDIR": "/opt/agent-tmp",
        "TMP": "/opt/agent-tmp",
        "TEMP": "/opt/agent-tmp",
        "PIP_CACHE_DIR": "/opt/pip-cache",
        "PYTHONPATH": "/opt/goal-plus-runtime:/opt/goal-plus/src",
    }


def resolve_goal_plus_runtime(profile: dict[str, Any]) -> dict[str, Any]:
    runtime = resolve_pi_runtime(profile)
    annotator = profile["goal_plus"]["evidence_annotator"]
    codex_runtime = (
        resolve_codex_runtime(profile) if isinstance(annotator, dict) else None
    )
    runtime.update(
        {
            "goal_plus_root": GOAL_PLUS_ROOT,
            "goal_plus_dependency_lock": GOAL_PLUS_DEPENDENCY_LOCK,
            "goal_plus_visible_verifier": GOAL_PLUS_VISIBLE_VERIFIER,
            "goal_plus_controller": GOAL_PLUS_CONTROLLER,
            "goal_plus_pip_cache": ensure_temp_root(
                "swe-bench-verified/goal-plus-pip-cache"
            ),
            "goal_plus_evidence_annotator": (
                dict(annotator) if isinstance(annotator, dict) else None
            ),
            "goal_plus_codex_archive": (
                codex_runtime["archive"] if codex_runtime is not None else None
            ),
            "goal_plus_codex_archive_present": bool(
                codex_runtime is not None and codex_runtime["archive_present"]
            ),
        }
    )
    return runtime


def resolve_goal_plus_codex_runtime(profile: dict[str, Any]) -> dict[str, Any]:
    if profile.get("agent_provider") is not None:
        runtime = resolve_codex_runtime(profile)
        runtime["auth_file"] = None
    else:
        archive = Path(
            os.environ.get("SWEBENCH_CODEX_RUNTIME_ARCHIVE", CODEX_ARCHIVE)
        )
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        auth_file = codex_home / "auth.json"
        runtime = {
            "archive": archive,
            "archive_present": archive.is_file(),
            "auth_file": auth_file,
            "credential_present": auth_file.is_file(),
            "auth_mode": "chatgpt",
        }
    annotator = profile["goal_plus"]["evidence_annotator"]
    runtime.update(
        {
            "goal_plus_root": GOAL_PLUS_ROOT,
            "goal_plus_dependency_lock": GOAL_PLUS_DEPENDENCY_LOCK,
            "goal_plus_visible_verifier": GOAL_PLUS_VISIBLE_VERIFIER,
            "goal_plus_controller": GOAL_PLUS_CONTROLLER,
            "goal_plus_pip_cache": ensure_temp_root(
                "swe-bench-verified/goal-plus-pip-cache"
            ),
            "goal_plus_evidence_annotator": (
                dict(annotator) if isinstance(annotator, dict) else None
            ),
            "goal_plus_codex_archive": runtime["archive"],
            "goal_plus_codex_archive_present": runtime["archive_present"],
        }
    )
    return runtime


@contextmanager
def routed_goal_plus_codex_runtime(
    profile: dict[str, Any],
    destination: Path,
    *,
    bridge_listen_host: str | None = None,
) -> Iterator[dict[str, Any]]:
    with routed_codex_runtime(
        profile,
        destination,
        bridge_listen_host=bridge_listen_host,
    ) as routed:
        runtime = resolve_goal_plus_codex_runtime(profile)
        runtime.update(routed)
        yield runtime


def _codex_container_probe(image: str, archive: Path) -> subprocess.CompletedProcess[str]:
    return run_capture(
        [
            "docker",
            "run",
            "--pull",
            "never",
            "--rm",
            "--tmpfs",
            CODEX_RUNTIME_TMPFS,
            "--mount",
            f"type=bind,src={archive},dst=/opt/runtime/codex.tgz,readonly",
            image,
            "sh",
            "-lc",
            "mkdir -p /opt/codex && tar -xzf /opt/runtime/codex.tgz -C /opt/codex && "
            "/opt/codex/package/vendor/x86_64-unknown-linux-musl/bin/codex --version",
        ],
        timeout=120,
    )


_CONTAINER_RESPONSES_PROBE = """
import json
import os
import urllib.error
import urllib.request

model = os.environ["SWEBENCH_API_MODEL"]
key = os.environ[os.environ["SWEBENCH_API_KEY_ENV"]]
payload = json.dumps({
    "model": model,
    "input": "Reply with exactly WIRE_OK.",
    "max_output_tokens": 16,
}).encode("utf-8")
request = urllib.request.Request(
    os.environ["SWEBENCH_API_BASE_URL"].rstrip("/") + "/responses",
    data=payload,
    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
)
result = {"passed": False, "http_status": None}
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=45) as response:
        body = json.loads(response.read().decode("utf-8"))
        result.update({
            "http_status": response.status,
            "object": body.get("object"),
            "model": body.get("model"),
            "response_status": body.get("status"),
        })
        result["passed"] = bool(
            200 <= response.status < 300
            and body.get("object") == "response"
            and body.get("model") == model
            and body.get("status") == "completed"
        )
except urllib.error.HTTPError as error:
    result["http_status"] = error.code
except Exception as error:
    result["error"] = type(error).__name__ + ": " + str(error)
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result["passed"] else 1)
""".strip()


def codex_container_responses_probe(
    target: str,
    runtime: dict[str, Any],
    *,
    model: str,
    existing_container: bool = False,
) -> dict[str, Any]:
    environment = dict(configure_temp_environment(dict(os.environ)))
    environment.update(
        {
            "SWEBENCH_API_BASE_URL": str(runtime["runtime_api_base_url"]),
            "SWEBENCH_API_KEY_ENV": str(runtime["api_key_env"]),
            "SWEBENCH_API_MODEL": model,
        }
    )
    inherited_names = [
        str(runtime["api_key_env"]),
        "SWEBENCH_API_BASE_URL",
        "SWEBENCH_API_KEY_ENV",
        "SWEBENCH_API_MODEL",
    ]
    if existing_container:
        command = ["docker", "exec"]
        for name in inherited_names:
            command.extend(["-e", name])
        command.extend([target, "python", "-c", _CONTAINER_RESPONSES_PROBE])
    else:
        command = [
            "docker",
            "run",
            "--pull",
            "never",
            "--rm",
            "--entrypoint",
            "python",
        ]
        for name in inherited_names:
            command.extend(["-e", name])
        command.extend([target, "-c", _CONTAINER_RESPONSES_PROBE])
    completed = run_capture(command, timeout=90, environment=environment)
    try:
        payload = json.loads(completed.stdout.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        payload = {"passed": False, "http_status": None}
    payload["passed"] = completed.returncode == 0 and payload.get("passed") is True
    if completed.stderr:
        payload["stderr"] = completed.stderr.strip()[-400:]
    return payload


def _image_checkout_probe(
    image: str, base_commit: str
) -> subprocess.CompletedProcess[str]:
    return run_capture(
        [
            "docker",
            "run",
            "--pull",
            "never",
            "--rm",
            "--entrypoint",
            "git",
            image,
            "-C",
            "/testbed",
            "rev-parse",
            "HEAD",
            f"{base_commit}^{{tree}}",
            "HEAD^{tree}",
        ],
        timeout=120,
    )


def _image_setup_probe(
    image: str, base_commit: str
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
    prefix = [
        "docker",
        "run",
        "--pull",
        "never",
        "--rm",
        "--entrypoint",
        "git",
        image,
        "-C",
        "/testbed",
    ]
    patch = run_capture(
        [*prefix, "diff", "--binary", f"{base_commit}..HEAD"], timeout=120
    )
    files = run_capture(
        [*prefix, "diff", "--name-only", f"{base_commit}..HEAD"], timeout=120
    )
    return patch, files


def _pi_container_probe(
    image: str, runtime: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    provider = str(runtime["provider"])
    credential_env = str(runtime["credential_env"])
    command = [
        "docker",
        "run",
        "--pull",
        "never",
        "--rm",
        "-e",
        credential_env,
        "--mount",
        f"type=bind,src={runtime['node_root']},dst=/opt/node,readonly",
        "--mount",
        f"type=bind,src={runtime['package_root']},dst=/opt/pi,readonly",
    ]
    if runtime.get("bridge_host"):
        command.extend(
            [
                "-e",
                f"NO_PROXY={runtime['bridge_host']}",
                "-e",
                f"no_proxy={runtime['bridge_host']}",
            ]
        )
    models_file = runtime.get("models_file")
    if isinstance(models_file, Path):
        command.extend(
            [
                "--tmpfs",
                "/opt/pi-home:rw,nosuid,nodev,size=128m",
                "-e",
                "HOME=/opt/pi-home",
                "-e",
                "PI_CODING_AGENT_DIR=/opt/pi-home/.pi/agent",
                "--mount",
                f"type=bind,src={models_file.parent},dst=/opt/provider,readonly",
                "--entrypoint",
                "sh",
                image,
                "-lc",
                "mkdir -p /opt/pi-home/.pi/agent && "
                "cp /opt/provider/models.json /opt/pi-home/.pi/agent/models.json && "
                "exec /opt/node/bin/node /opt/pi/dist/cli.js "
                "--offline --list-models \"$@\"",
                "swe-bench-pi-probe",
                provider,
            ]
        )
    else:
        command.extend(
            [
                image,
                "/opt/node/bin/node",
                "/opt/pi/dist/cli.js",
                "--offline",
                "--list-models",
                provider,
            ]
        )
    return run_capture(command, timeout=120)


def _goal_plus_container_probe(
    image: str, runtime: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    environment_names = [
        name for name in ("PIP_INDEX_URL",) if os.environ.get(name)
    ]
    command = [
        "docker",
        "run",
        "--pull",
        "never",
        "--rm",
        "--tmpfs",
        "/opt/agent-tmp:rw,nosuid,nodev,size=256m",
        "--tmpfs",
        "/opt/goal-plus-runtime:rw,exec,nosuid,nodev,size=512m",
    ]
    annotator_enabled = runtime.get("goal_plus_evidence_annotator") is not None
    if annotator_enabled:
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
    for name, value in goal_plus_runtime_environment().items():
        command.extend(["-e", f"{name}={value}"])
    for name in environment_names:
        command.extend(["-e", name])
    command.extend(
        [
            "--mount",
            f"type=bind,src={runtime['node_root']},dst=/opt/node,readonly",
            "--mount",
            f"type=bind,src={runtime['package_root']},dst=/opt/pi,readonly",
            "--mount",
            "type=bind,"
            f"src={runtime['goal_plus_root']},dst=/opt/goal-plus,readonly",
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
            f"type=bind,src={runtime['goal_plus_pip_cache']},dst=/opt/pip-cache",
            "--entrypoint",
            "sh",
            image,
            "-lc",
            (
                "mkdir -p /opt/codex && "
                "tar -xzf /opt/runtime/codex.tgz -C /opt/codex && "
                if annotator_enabled
                else ""
            )
            + goal_plus_install_script()
            + " && python -c \"import fastmcp, goal_plus, plotly, pydantic\""
            + " && pi --version"
            + (
                " && /opt/codex/package/vendor/x86_64-unknown-linux-musl/bin/codex --version"
                if annotator_enabled
                else ""
            )
            + " && python -m goal_plus.pi_tool --help >/dev/null"
            + " && python /opt/swebench-goal-plus-controller.py --help >/dev/null",
        ]
    )
    return run_capture(command, timeout=600)


def _goal_plus_codex_container_probe(
    image: str, runtime: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    command = [
        "docker",
        "run",
        "--pull",
        "never",
        "--rm",
        "--tmpfs",
        "/opt/agent-tmp:rw,nosuid,nodev,size=256m",
        "--tmpfs",
        "/opt/goal-plus-runtime:rw,exec,nosuid,nodev,size=512m",
        "--tmpfs",
        CODEX_RUNTIME_TMPFS,
        "--mount",
        "type=bind,"
        f"src={runtime['archive']},dst=/opt/runtime/codex.tgz,readonly",
    ]
    for name, value in goal_plus_runtime_environment().items():
        command.extend(["-e", f"{name}={value}"])
    if os.environ.get("PIP_INDEX_URL"):
        command.extend(["-e", "PIP_INDEX_URL"])
    command.extend(
        [
            "--mount",
            f"type=bind,src={runtime['goal_plus_root']},dst=/opt/goal-plus,readonly",
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
            f"type=bind,src={runtime['goal_plus_pip_cache']},dst=/opt/pip-cache",
            "--entrypoint",
            "sh",
            image,
            "-lc",
            "mkdir -p /opt/codex && "
            "tar -xzf /opt/runtime/codex.tgz -C /opt/codex && "
            + goal_plus_install_script(include_pi=False)
            + " && ln -sf "
            "/opt/codex/package/vendor/x86_64-unknown-linux-musl/bin/codex "
            "/opt/goal-plus-bin/codex"
            + " && python -c \"import fastmcp, goal_plus, plotly, pydantic\""
            + " && codex --version"
            + " && python /opt/swebench-goal-plus-controller.py --help >/dev/null",
        ]
    )
    return run_capture(command, timeout=600)


def doctor_payload(profile: dict[str, Any]) -> dict[str, Any]:
    inventory = image_inventory(profile)
    checks: list[dict[str, Any]] = [
        _check("inventory:exact-images", inventory["ok"]),
    ]
    docker_info = run_capture(["docker", "info", "--format", "{{json .}}"])
    docker_architecture = None
    if docker_info.returncode == 0:
        try:
            docker_architecture = json.loads(docker_info.stdout).get("Architecture")
        except (json.JSONDecodeError, AttributeError):
            docker_architecture = None
    checks.append(
        _check(
            "docker:linux-amd64",
            docker_info.returncode == 0 and docker_architecture == "x86_64",
            architecture=docker_architecture,
        )
    )

    branch = _git_value("branch", "--show-current")
    head = _git_value("rev-parse", "HEAD")
    upstream = _git_value("rev-parse", "--abbrev-ref", "@{upstream}")
    dirty = _git_value("status", "--porcelain")
    checks.append(
        _check(
            "checkout:swebench",
            bool(
                SWEBENCH_ROOT.is_dir()
                and branch == "main"
                and upstream == "origin/main"
                and head
                and dirty == ""
            ),
            path=str(SWEBENCH_ROOT),
            branch=branch,
            upstream=upstream,
            commit=head,
            dirty=dirty not in (None, ""),
        )
    )
    package_versions = {
        name: _package_version(name)
        for name in ("swebench", "datasets", "unidiff", "docker")
    }
    for name, version in package_versions.items():
        checks.append(
            _check(f"package:{name}", version is not None, version=version)
        )

    method = profile["methods"][0]
    task = profile["tasks"][0]
    image = task["image"]
    base_commit = task["base_commit"]
    checkout_probe = _image_checkout_probe(image, base_commit)
    checkout_values = checkout_probe.stdout.splitlines()
    checkout_valid = (
        checkout_probe.returncode == 0
        and len(checkout_values) == 3
        and checkout_values[1] == checkout_values[2]
    )
    image_setup_evidence = None
    if (
        checkout_probe.returncode == 0
        and len(checkout_values) == 3
        and not checkout_valid
        and isinstance(task.get("image_setup"), dict)
    ):
        setup_patch, setup_files = _image_setup_probe(image, base_commit)
        setup_contract = task["image_setup"]
        canonical_patch = (
            setup_patch.stdout.rstrip("\n") + "\n" if setup_patch.stdout else ""
        )
        setup_patch_sha256 = hashlib.sha256(
            canonical_patch.encode("utf-8")
        ).hexdigest()
        observed_setup_files = setup_files.stdout.splitlines()
        setup_valid = bool(
            setup_patch.returncode == 0
            and setup_files.returncode == 0
            and checkout_values[0] == setup_contract.get("head")
            and checkout_values[2] == setup_contract.get("tree")
            and setup_patch_sha256 == setup_contract.get("patch_sha256")
            and observed_setup_files == setup_contract.get("files")
        )
        checkout_valid = setup_valid
        image_setup_evidence = {
            "passed": setup_valid,
            "head": checkout_values[0],
            "tree": checkout_values[2],
            "patch_sha256": setup_patch_sha256,
            "files": observed_setup_files,
            "provenance": setup_contract.get("provenance"),
        }
    checks.append(
        _check(
            "image:dataset-base-tree",
            checkout_valid,
            base_commit=base_commit,
            observed_head=checkout_values[0] if checkout_values else None,
            base_tree=(checkout_values[1] if len(checkout_values) > 1 else None),
            observed_tree=(checkout_values[2] if len(checkout_values) > 2 else None),
            image_setup=image_setup_evidence,
            error=checkout_probe.stderr.strip()[-2000:],
        )
    )
    runtime: dict[str, Any]
    if method == "goal-plus-codex":
        runtime = resolve_goal_plus_codex_runtime(profile)
        custom_provider = profile.get("agent_provider") is not None
        checks.append(
            _check(
                "codex:runtime-archive",
                runtime["archive_present"],
                path=str(runtime["archive"]),
            )
        )
        if custom_provider:
            api_config_valid = bool(
                runtime["auth_mode"] == "openai-compatible"
                and runtime["wire_api"] == "responses"
                and runtime["api_base_url"]
                and runtime["credential_present"]
            )
            checks.append(
                _check(
                    "codex:openai-compatible-config",
                    api_config_valid,
                    provider=runtime["provider_id"],
                    auth_mode=runtime["auth_mode"],
                    wire_api=runtime["wire_api"],
                    base_url_env=runtime["base_url_env"],
                    api_key_env=runtime["api_key_env"],
                    base_url=runtime["api_base_url"],
                )
            )
            host_probe = (
                openai_responses_probe(
                    str(runtime["api_base_url"]),
                    api_key_env=str(runtime["api_key_env"]),
                    model=str(profile["model"]),
                )
                if api_config_valid
                else {
                    "passed": False,
                    "http_status": None,
                    "error": "custom provider configuration is incomplete",
                }
            )
            checks.append(
                _check(
                    "codex:host-responses",
                    bool(host_probe["passed"]),
                    **{
                        key: value
                        for key, value in host_probe.items()
                        if key != "passed"
                    },
                )
            )
            route_recorded = False
            container_recorded = False
            try:
                if not api_config_valid:
                    raise SweBenchContractError(
                        "Codex custom provider configuration is incomplete"
                    )
                with temporary_directory(
                    prefix="goal-plus-codex-api-doctor-",
                    namespace="swe-bench-verified",
                ) as destination:
                    with routed_goal_plus_codex_runtime(profile, destination) as routed:
                        route_recorded = True
                        checks.append(
                            _check(
                                "codex:container-api-route",
                                True,
                                loopback_bridge=routed["bridge"] is not None,
                                bridge=(
                                    {
                                        key: value
                                        for key, value in routed["bridge"].items()
                                        if key != "pid"
                                    }
                                    if routed["bridge"]
                                    else None
                                ),
                                runtime_base_url=routed["runtime_api_base_url"],
                            )
                        )
                        container_probe = codex_container_responses_probe(
                            image,
                            routed,
                            model=str(profile["model"]),
                        )
                        container_recorded = True
                        checks.append(
                            _check(
                                "codex:container-responses",
                                bool(container_probe["passed"]),
                                **{
                                    key: value
                                    for key, value in container_probe.items()
                                    if key != "passed"
                                },
                            )
                        )
            except Exception as error:
                detail = f"{type(error).__name__}: {error}"
                if not route_recorded:
                    checks.append(
                        _check("codex:container-api-route", False, error=detail)
                    )
                if not container_recorded:
                    checks.append(
                        _check("codex:container-responses", False, error=detail)
                    )
        else:
            checks.append(
                _check(
                    "codex:chatgpt-auth",
                    runtime["credential_present"],
                    auth_file=str(runtime["auth_file"]),
                )
            )
        if runtime["archive_present"]:
            probe = _codex_container_probe(image, runtime["archive"])
            checks.append(
                _check(
                    "codex:container-runtime",
                    probe.returncode == 0,
                    version=(probe.stdout or probe.stderr).strip(),
                )
            )
        expected_goal_plus_branch = managed_upstream_branch("goal_plus")
        goal_plus_branch = _git_value_at(
            runtime["goal_plus_root"], "branch", "--show-current"
        )
        goal_plus_head = _git_value_at(
            runtime["goal_plus_root"], "rev-parse", "HEAD"
        )
        goal_plus_upstream = _git_value_at(
            runtime["goal_plus_root"], "rev-parse", "--abbrev-ref", "@{upstream}"
        )
        goal_plus_dirty = _git_value_at(
            runtime["goal_plus_root"], "status", "--porcelain"
        )
        assets_present = all(
            isinstance(runtime.get(name), Path) and runtime[name].is_file()
            for name in (
                "goal_plus_dependency_lock",
                "goal_plus_visible_verifier",
                "goal_plus_controller",
            )
        ) and (
            runtime["goal_plus_root"] / ".codex" / "skills" / "goal-plus"
        ).is_dir()
        checks.extend(
            [
                _check(
                    "checkout:goal-plus",
                    bool(
                        runtime["goal_plus_root"].is_dir()
                        and goal_plus_branch == expected_goal_plus_branch
                        and goal_plus_upstream
                        == f"origin/{expected_goal_plus_branch}"
                        and goal_plus_head
                        and goal_plus_dirty == ""
                    ),
                    path=str(runtime["goal_plus_root"]),
                    branch=goal_plus_branch,
                    upstream=goal_plus_upstream,
                    expected_branch=expected_goal_plus_branch,
                    commit=goal_plus_head,
                    dirty=goal_plus_dirty not in (None, ""),
                ),
                _check(
                    "goal-plus:container-assets",
                    assets_present,
                    dependency_lock=str(runtime["goal_plus_dependency_lock"]),
                    visible_verifier=str(runtime["goal_plus_visible_verifier"]),
                    controller=str(runtime["goal_plus_controller"]),
                    evidence_annotator="codex",
                    codex_archive=str(runtime["archive"]),
                ),
            ]
        )
        if runtime["archive_present"] and assets_present:
            goal_plus_probe = _goal_plus_codex_container_probe(image, runtime)
            pip_cache_disabled = "cache has been disabled" in (
                goal_plus_probe.stderr.lower()
            )
            checks.append(
                _check(
                    "goal-plus:container-runtime",
                    goal_plus_probe.returncode == 0 and not pip_cache_disabled,
                    version_output=goal_plus_probe.stdout.strip()[-2000:],
                    error=goal_plus_probe.stderr.strip()[-2000:],
                    pip_cache_enabled=not pip_cache_disabled,
                    pip_index_env=(
                        "PIP_INDEX_URL" if os.environ.get("PIP_INDEX_URL") else None
                    ),
                )
            )
    elif method == "plain-codex":
        runtime = resolve_codex_runtime(profile)
        api_config_valid = bool(
            runtime["auth_mode"] == "openai-compatible"
            and runtime["wire_api"] == "responses"
            and runtime["api_base_url"]
            and runtime["credential_present"]
        )
        checks.extend(
            [
                _check(
                    "codex:runtime-archive",
                    runtime["archive_present"],
                    path=str(runtime["archive"]),
                ),
                _check(
                    "codex:openai-compatible-config",
                    api_config_valid,
                    provider=runtime["provider_id"],
                    auth_mode=runtime["auth_mode"],
                    wire_api=runtime["wire_api"],
                    base_url_env=runtime["base_url_env"],
                    api_key_env=runtime["api_key_env"],
                    base_url=runtime["api_base_url"],
                ),
            ]
        )
        if runtime["archive_present"]:
            probe = _codex_container_probe(image, runtime["archive"])
            checks.append(
                _check(
                    "codex:container-runtime",
                    probe.returncode == 0,
                    version=(probe.stdout or probe.stderr).strip(),
                )
            )
        if api_config_valid:
            host_probe = openai_responses_probe(
                str(runtime["api_base_url"]),
                api_key_env=str(runtime["api_key_env"]),
                model=str(profile["model"]),
            )
            checks.append(
                _check(
                    "codex:host-responses",
                    bool(host_probe["passed"]),
                    **{key: value for key, value in host_probe.items() if key != "passed"},
                )
            )
            route_recorded = False
            container_recorded = False
            try:
                with temporary_directory(
                    prefix="codex-api-doctor-",
                    namespace="swe-bench-verified",
                ) as destination:
                    with routed_codex_runtime(profile, destination) as routed:
                        route_recorded = True
                        checks.append(
                            _check(
                                "codex:container-api-route",
                                True,
                                loopback_bridge=routed["bridge"] is not None,
                                bridge=(
                                    {
                                        key: value
                                        for key, value in routed["bridge"].items()
                                        if key != "pid"
                                    }
                                    if routed["bridge"]
                                    else None
                                ),
                                runtime_base_url=routed["runtime_api_base_url"],
                            )
                        )
                        container_probe = codex_container_responses_probe(
                            image,
                            routed,
                            model=str(profile["model"]),
                        )
                        container_recorded = True
                        checks.append(
                            _check(
                                "codex:container-responses",
                                bool(container_probe["passed"]),
                                **{
                                    key: value
                                    for key, value in container_probe.items()
                                    if key != "passed"
                                },
                            )
                        )
            except Exception as error:
                if not route_recorded:
                    checks.append(
                        _check(
                            "codex:container-api-route",
                            False,
                            error=f"{type(error).__name__}: {error}",
                        )
                    )
                if not container_recorded:
                    checks.append(
                        _check(
                            "codex:container-responses",
                            False,
                            error=f"{type(error).__name__}: {error}",
                        )
                    )
    else:
        runtime = (
            resolve_goal_plus_runtime(profile)
            if method == "goal-plus-pi"
            else resolve_pi_runtime(profile)
        )
        paths_present = all(
            isinstance(runtime.get(name), Path) and runtime[name].exists()
            for name in ("node_root", "package_root", "pi_cli")
        )
        checks.extend(
            [
                _check(
                    "pi:host-runtime",
                    paths_present,
                    node_root=str(runtime.get("node_root") or ""),
                    package_root=str(runtime.get("package_root") or ""),
                ),
                _check(
                    "pi:credential",
                    runtime["credential_present"],
                    provider=runtime["provider"],
                    credential_env=runtime["credential_env"],
                ),
            ]
        )
        pi_model_ready = False
        if runtime["custom_provider"]:
            api_config_valid = bool(
                runtime.get("auth_mode") == "openai-compatible"
                and runtime.get("wire_api") == "responses"
                and runtime.get("api_base_url")
                and runtime["credential_present"]
            )
            checks.append(
                _check(
                    "pi:openai-compatible-config",
                    api_config_valid,
                    provider=runtime["provider"],
                    auth_mode=runtime.get("auth_mode"),
                    wire_api=runtime.get("wire_api"),
                    base_url_env=runtime.get("base_url_env"),
                    api_key_env=runtime.get("api_key_env"),
                    base_url=runtime.get("api_base_url"),
                )
            )
            host_probe = (
                openai_responses_probe(
                    str(runtime["api_base_url"]),
                    api_key_env=str(runtime["api_key_env"]),
                    model=str(runtime["model_id"]),
                )
                if api_config_valid
                else {
                    "passed": False,
                    "http_status": None,
                    "error": "custom provider configuration is incomplete",
                }
            )
            checks.append(
                _check(
                    "pi:host-responses",
                    bool(host_probe["passed"]),
                    **{
                        key: value
                        for key, value in host_probe.items()
                        if key != "passed"
                    },
                )
            )
            route_recorded = False
            container_recorded = False
            model_recorded = False
            try:
                if not paths_present or not api_config_valid:
                    raise SweBenchContractError(
                        "Pi runtime or custom provider configuration is incomplete"
                    )
                with temporary_directory(
                    prefix="pi-api-doctor-",
                    namespace="swe-bench-verified",
                ) as destination:
                    with routed_pi_runtime(
                        profile,
                        destination,
                        goal_plus=method == "goal-plus-pi",
                    ) as routed:
                        route_recorded = True
                        checks.append(
                            _check(
                                "pi:container-api-route",
                                True,
                                loopback_bridge=routed["bridge"] is not None,
                                bridge=(
                                    {
                                        key: value
                                        for key, value in routed["bridge"].items()
                                        if key != "pid"
                                    }
                                    if routed["bridge"]
                                    else None
                                ),
                                runtime_base_url=routed["runtime_api_base_url"],
                            )
                        )
                        container_probe = codex_container_responses_probe(
                            image,
                            routed,
                            model=str(routed["model_id"]),
                        )
                        container_recorded = True
                        checks.append(
                            _check(
                                "pi:container-responses",
                                bool(container_probe["passed"]),
                                **{
                                    key: value
                                    for key, value in container_probe.items()
                                    if key != "passed"
                                },
                            )
                        )
                        model_probe = _pi_container_probe(image, routed)
                        model_recorded = True
                        model_passed = bool(
                            model_probe.returncode == 0
                            and str(routed["model_id"]) in model_probe.stdout
                        )
                        checks.append(
                            _check(
                                "pi:container-model",
                                model_passed,
                                provider=routed["provider"],
                                model=routed["model_id"],
                                output=model_probe.stdout.strip()[-2000:],
                                error=model_probe.stderr.strip()[-2000:],
                            )
                        )
                        pi_model_ready = bool(
                            host_probe["passed"]
                            and container_probe["passed"]
                            and model_passed
                        )
            except Exception as error:
                detail = f"{type(error).__name__}: {error}"
                if not route_recorded:
                    checks.append(
                        _check("pi:container-api-route", False, error=detail)
                    )
                if not container_recorded:
                    checks.append(
                        _check("pi:container-responses", False, error=detail)
                    )
                if not model_recorded:
                    checks.append(_check("pi:container-model", False, error=detail))
        else:
            if paths_present and runtime["credential_present"]:
                probe = _pi_container_probe(image, runtime)
                pi_model_ready = bool(
                    probe.returncode == 0
                    and str(runtime["model_id"]) in probe.stdout
                )
                checks.append(
                    _check(
                        "pi:container-model",
                        pi_model_ready,
                        provider=runtime["provider"],
                        model=runtime["model_id"],
                        output=probe.stdout.strip()[-2000:],
                        error=probe.stderr.strip()[-2000:],
                    )
                )
            else:
                checks.append(
                    _check(
                        "pi:container-model",
                        False,
                        provider=runtime["provider"],
                        model=runtime["model_id"],
                        error="Pi runtime or credential is missing",
                    )
                )
        if method == "goal-plus-pi":
            expected_goal_plus_branch = managed_upstream_branch("goal_plus")
            goal_plus_branch = _git_value_at(
                runtime["goal_plus_root"], "branch", "--show-current"
            )
            goal_plus_head = _git_value_at(
                runtime["goal_plus_root"], "rev-parse", "HEAD"
            )
            goal_plus_upstream = _git_value_at(
                runtime["goal_plus_root"], "rev-parse", "--abbrev-ref", "@{upstream}"
            )
            goal_plus_dirty = _git_value_at(
                runtime["goal_plus_root"], "status", "--porcelain"
            )
            goal_plus_assets_present = all(
                isinstance(runtime.get(name), Path) and runtime[name].is_file()
                for name in (
                    "goal_plus_dependency_lock",
                    "goal_plus_visible_verifier",
                    "goal_plus_controller",
                )
            ) and (
                runtime["goal_plus_root"] / ".pi" / "extensions" / "goal-plus.ts"
            ).is_file()
            annotator_assets_present = bool(
                runtime.get("goal_plus_evidence_annotator") is None
                or runtime.get("goal_plus_codex_archive_present")
            )
            checkout_valid = bool(
                runtime["goal_plus_root"].is_dir()
                and goal_plus_branch == expected_goal_plus_branch
                and goal_plus_upstream == f"origin/{expected_goal_plus_branch}"
                and goal_plus_head
                and goal_plus_dirty == ""
            )
            checks.extend(
                [
                    _check(
                        "checkout:goal-plus",
                        checkout_valid,
                        path=str(runtime["goal_plus_root"]),
                        branch=goal_plus_branch,
                        upstream=goal_plus_upstream,
                        expected_branch=expected_goal_plus_branch,
                        commit=goal_plus_head,
                        dirty=goal_plus_dirty not in (None, ""),
                    ),
                    _check(
                        "goal-plus:container-assets",
                        goal_plus_assets_present and annotator_assets_present,
                        dependency_lock=str(runtime["goal_plus_dependency_lock"]),
                        visible_verifier=str(runtime["goal_plus_visible_verifier"]),
                        controller=str(runtime["goal_plus_controller"]),
                        evidence_annotator=(
                            "codex"
                            if runtime.get("goal_plus_evidence_annotator")
                            else "disabled"
                        ),
                        codex_archive=(
                            str(runtime.get("goal_plus_codex_archive") or "")
                            if runtime.get("goal_plus_evidence_annotator")
                            else None
                        ),
                    ),
                ]
            )
            if (
                pi_model_ready
                and checkout_valid
                and goal_plus_assets_present
                and annotator_assets_present
            ):
                goal_plus_probe = _goal_plus_container_probe(image, runtime)
                pip_cache_disabled = "cache has been disabled" in (
                    goal_plus_probe.stderr.lower()
                )
                checks.append(
                    _check(
                        "goal-plus:container-runtime",
                        goal_plus_probe.returncode == 0 and not pip_cache_disabled,
                        version_output=goal_plus_probe.stdout.strip()[-2000:],
                        error=goal_plus_probe.stderr.strip()[-2000:],
                        pip_cache_enabled=not pip_cache_disabled,
                        pip_index_env=(
                            "PIP_INDEX_URL" if os.environ.get("PIP_INDEX_URL") else None
                        ),
                    )
                )

    return {
        "schema_version": 1,
        "benchmark_id": "swe-bench-verified",
        "profile": profile["id"],
        "method": method,
        "model": profile["model"],
        "ok": all(item["passed"] for item in checks),
        "checks": checks,
        "inventory": inventory,
        "packages": package_versions,
        "swebench_commit": head,
        "checked_at": utc_now(),
    }


def doctor(
    profile: dict[str, Any],
    *,
    output: Path | None,
    local_assets_only: bool,
    allow_missing_local_assets: bool,
) -> int:
    payload = (
        image_inventory(profile)
        if local_assets_only
        else doctor_payload(profile)
    )
    if output is not None:
        write_json(output, payload)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if payload["ok"] or (local_assets_only and allow_missing_local_assets):
        return 0
    return 1
