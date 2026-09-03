"""Host, authentication, Docker, provisioning, and doctor checks."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from bench_runtime_paths import (
    configure_temp_environment,
    ensure_temp_root,
    temporary_directory,
)
from bench_goal_plus.loopback_bridge import (
    bridged_url as _bridged_url,
    default_route_ipv4 as _default_route_ipv4,
    loopback_target as _loopback_target,
    start_socket_bridge as _start_socket_bridge,
)

from . import io
from .asset_issues import asset_issue_matches_revision, known_asset_issues
from .context import current_paths
from .profiles import (
    GOAL_PLUS_METHODS,
    METHODS,
    api_protocol_for_methods,
    methods_require_codex,
    load_official_codex_protocol,
    pi_provider_role_model_refs,
    profile_task_protocol,
)


PI_BUILTIN_PROVIDER_API_KEYS: dict[str, tuple[str, ...]] = {
    "github-copilot": ("COPILOT_GITHUB_TOKEN",),
    "anthropic": ("ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
    "ant-ling": ("ANT_LING_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "azure-openai-responses": ("AZURE_OPENAI_API_KEY",),
    "nvidia": ("NVIDIA_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "google": ("GEMINI_API_KEY",),
    "google-vertex": ("GOOGLE_CLOUD_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "cerebras": ("CEREBRAS_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "vercel-ai-gateway": ("AI_GATEWAY_API_KEY",),
    "zai": ("ZAI_API_KEY",),
    "zai-coding-cn": ("ZAI_CODING_CN_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "minimax-cn": ("MINIMAX_CN_API_KEY",),
    "moonshotai": ("MOONSHOT_API_KEY",),
    "moonshotai-cn": ("MOONSHOT_API_KEY",),
    "huggingface": ("HF_TOKEN",),
    "fireworks": ("FIREWORKS_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "opencode": ("OPENCODE_API_KEY",),
    "opencode-go": ("OPENCODE_API_KEY",),
    "kimi-coding": ("KIMI_API_KEY",),
    "cloudflare-workers-ai": ("CLOUDFLARE_API_KEY",),
    "cloudflare-ai-gateway": ("CLOUDFLARE_API_KEY",),
    "xiaomi": ("XIAOMI_API_KEY",),
    "xiaomi-token-plan-cn": ("XIAOMI_TOKEN_PLAN_CN_API_KEY",),
    "xiaomi-token-plan-ams": ("XIAOMI_TOKEN_PLAN_AMS_API_KEY",),
    "xiaomi-token-plan-sgp": ("XIAOMI_TOKEN_PLAN_SGP_API_KEY",),
}

PI_BUILTIN_PROVIDER_API_BASE_URLS: dict[str, str] = {
    "github-copilot": "https://api.individual.githubcopilot.com",
    "anthropic": "https://api.anthropic.com",
    "ant-ling": "https://api.ant-ling.com/v1",
    "openai": "https://api.openai.com/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "deepseek": "https://api.deepseek.com",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "groq": "https://api.groq.com/openai/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "xai": "https://api.x.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "vercel-ai-gateway": "https://ai-gateway.vercel.sh",
    "zai": "https://api.z.ai/api/coding/paas/v4",
    "zai-coding-cn": "https://open.bigmodel.cn/api/coding/paas/v4",
    "mistral": "https://api.mistral.ai",
    "minimax": "https://api.minimax.io/anthropic",
    "minimax-cn": "https://api.minimaxi.com/anthropic",
    "moonshotai": "https://api.moonshot.ai/v1",
    "moonshotai-cn": "https://api.moonshot.cn/v1",
    "huggingface": "https://router.huggingface.co/v1",
    "fireworks": "https://api.fireworks.ai/inference",
    "together": "https://api.together.ai/v1",
    "opencode": "https://opencode.ai/zen",
    "opencode-go": "https://opencode.ai/zen/go",
    "kimi-coding": "https://api.kimi.com/coding",
    "xiaomi": "https://api.xiaomimimo.com/v1",
    "xiaomi-token-plan-cn": "https://token-plan-cn.xiaomimimo.com/v1",
    "xiaomi-token-plan-ams": "https://token-plan-ams.xiaomimimo.com/v1",
    "xiaomi-token-plan-sgp": "https://token-plan-sgp.xiaomimimo.com/v1",
}

SFORGE_AGENT_DEFAULT_API_BASE_URLS: dict[str, str] = {
    "codex": "https://api.openai.com",
    "codex-goal-plus": "https://api.openai.com",
    "pi": "https://chatgpt.com/backend-api",
    "pi-goal-plus": "https://chatgpt.com/backend-api",
    "claude-code": "https://api.anthropic.com",
}

GOAL_PLUS_BASE_REQUIRED_ASSETS = (
    "pyproject.toml",
    "src/goal_plus/__init__.py",
)

GOAL_PLUS_CODEX_REQUIRED_ASSETS = (
    "src/goal_plus/server.py",
    "src/goal_plus/tools.py",
    "src/goal_plus/goal_plus_stop_hook.py",
    ".codex/config.example.toml",
    ".codex/skills/goal-plus/SKILL.md",
    ".codex/skills/goal-plus/agents/openai.yaml",
    ".codex/skills/search/SKILL.md",
    ".codex/agents/search_candidate_agent.toml",
    ".codex/agents/goal_plus_final_checker.toml",
)

GOAL_PLUS_CODEX_HOOK_ASSETS = (
    "hooks/hooks.json",
    ".codex/hooks.example.json",
    ".codex/hooks.json",
)

GOAL_PLUS_PI_REQUIRED_ASSETS = (
    ".pi/extensions/goal-plus.ts",
    ".pi/prompts/goal-plus.md",
    ".pi/skills/goal-plus/SKILL.md",
)

GOAL_PLUS_REQUIRED_ASSETS = tuple(
    dict.fromkeys(
        GOAL_PLUS_BASE_REQUIRED_ASSETS
        + GOAL_PLUS_CODEX_REQUIRED_ASSETS
        + GOAL_PLUS_PI_REQUIRED_ASSETS
    )
)


def goal_plus_runtime_asset_contract(
    methods: list[str] | tuple[str, ...] | set[str] | None = None,
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Return exact Goal Plus assets needed by the selected runtime hosts."""
    selected = set(GOAL_PLUS_METHODS if methods is None else methods)
    required = list(GOAL_PLUS_BASE_REQUIRED_ASSETS)
    alternatives: list[tuple[str, ...]] = []
    if "goal-plus-codex" in selected:
        required.extend(GOAL_PLUS_CODEX_REQUIRED_ASSETS)
        alternatives.append(GOAL_PLUS_CODEX_HOOK_ASSETS)
    if selected & {"goal-plus-pi", "goal-plus-pi-provider"}:
        required.extend(GOAL_PLUS_PI_REQUIRED_ASSETS)
    return tuple(dict.fromkeys(required)), tuple(alternatives)


def active_sforge_codex_runtime_contract() -> dict[str, Any]:
    """Inspect the host-command authorization contract in active SForge."""
    try:
        from sforge.harness.agent.codex_goal_plus import (
            CODEX_GOAL_PLUS_MCP_OVERRIDES,
            CodexGoalPlusAgent,
        )

        commands = "\n".join(CodexGoalPlusAgent.install_cmds)
        overrides = set(CODEX_GOAL_PLUS_MCP_OVERRIDES)
        required_overrides = {
            'mcp_servers.goal-plus.command="goal-plus"',
            'mcp_servers.goal-plus.startup_timeout_sec=30',
            'mcp_servers.goal-plus.enabled=true',
        }
        explicit_mcp = required_overrides.issubset(overrides) and any(
            value.startswith('mcp_servers.goal-plus.args=') for value in overrides
        )
        plugins_disabled = all(
            "--disable plugins" in template
            for template in (CodexGoalPlusAgent.run_cmd, CodexGoalPlusAgent.resume_cmd)
        )
        plugin_install = "install_codex_plugin.py" in commands
        hook_asset_install = all(
            path in commands for path in GOAL_PLUS_CODEX_HOOK_ASSETS
        )
        project_hooks_enabled = bool(
            CodexGoalPlusAgent.stop_hook == "codex-native-goal-plus"
            and hook_asset_install
        )
        goal_command_prefix = CodexGoalPlusAgent.run_cmd.partition(
            '"\\$goal-plus'
        )[2].partition("$(cat")[0]
        typed_command_config = all(
            marker in goal_command_prefix
            for marker in (
                "mode=autonomous",
                "max_parallel=__GOAL_PLUS_PARALLEL_NUM__",
                "workspace_backend=git_worktree",
                "promotion_mode=artifact_only",
                "strategy=agent_guided",
                "__GOAL_PLUS_ROLE_COMMAND_CONFIG__",
            )
        ) and " -- " not in goal_command_prefix
        exact_start = bool(
            goal_command_prefix.startswith(
                " mode=autonomous max_parallel=__GOAL_PLUS_PARALLEL_NUM__ "
            )
            and typed_command_config
        )
        native_resume = (
            "exec codex exec " in CodexGoalPlusAgent.resume_cmd
            and "resume --last" in CodexGoalPlusAgent.resume_cmd
            and "$goal-plus" not in CodexGoalPlusAgent.resume_cmd
            and "Continue the active Goal Plus task"
            in CodexGoalPlusAgent.resume_cmd
        )
        valid = bool(
            explicit_mcp
            and plugins_disabled
            and not plugin_install
            and project_hooks_enabled
            and exact_start
            and typed_command_config
            and native_resume
        )
        return {
            "valid": valid,
            "mode": "host-command-hooks-explicit-mcp",
            "explicit_mcp": explicit_mcp,
            "plugins_disabled": plugins_disabled,
            "plugin_install": plugin_install,
            "project_hooks_enabled": project_hooks_enabled,
            "hook_asset_install": hook_asset_install,
            "exact_start": exact_start,
            "typed_command_config": typed_command_config,
            "native_resume": native_resume,
            "startup_timeout_seconds": 30 if explicit_mcp else None,
            "error": (
                None
                if valid
                else (
                    "SForge Goal Plus Codex adapter does not require exact host "
                    "start commands with typed config through project hooks "
                    "and native session resume with an ordinary continuation prompt"
                )
            ),
        }
    except (ImportError, AttributeError, TypeError) as exc:
        return {
            "valid": False,
            "error": f"cannot inspect the active SForge Codex adapter: {exc}",
        }


def active_sforge_pi_runtime_contract() -> dict[str, Any]:
    """Inspect the Pi host-command start and cross-process resume contract."""
    try:
        from sforge.harness.agent.pi_goal_plus import PiGoalPlusAgent

        start_prefix = PiGoalPlusAgent.run_cmd.partition('"/goal-plus')[2].partition(
            "$(cat"
        )[0]
        typed_command_config = all(
            marker in start_prefix
            for marker in (
                "mode=autonomous",
                "max_parallel=__GOAL_PLUS_PARALLEL_NUM__",
                "workspace_backend=git_worktree",
                "promotion_mode=artifact_only",
                "strategy=agent_guided",
                "__GOAL_PLUS_ROLE_COMMAND_CONFIG__",
            )
        ) and " -- " not in start_prefix
        exact_start = bool(
            start_prefix.startswith(
                " mode=autonomous max_parallel=__GOAL_PLUS_PARALLEL_NUM__ "
            )
            and typed_command_config
        )
        extension_loaded = all(
            "-e /opt/goal-plus/.pi/extensions/goal-plus.ts" in template
            for template in (PiGoalPlusAgent.run_cmd, PiGoalPlusAgent.resume_cmd)
        )
        reasoning_explicit = all(
            '--thinking "$SFORGE_PI_REASONING_EFFORT"' in template
            for template in (PiGoalPlusAgent.run_cmd, PiGoalPlusAgent.resume_cmd)
        )
        promotion_sync_persisted = all(
            marker in PiGoalPlusAgent.resume_cmd
            for marker in (
                "sforge-goal-plus-submit --details --if-new",
                "edgebench-resume-sync.log",
            )
        )
        native_resume = bool(
            '--session-id "$SFORGE_PI_GOAL_PLUS_SESSION_ID"' in PiGoalPlusAgent.run_cmd
            and '--session "$SFORGE_PI_GOAL_PLUS_SESSION_ID"' in PiGoalPlusAgent.resume_cmd
            and all(
                "--session-dir /home/agent/.goal-plus/pi-sessions" in template
                for template in (PiGoalPlusAgent.run_cmd, PiGoalPlusAgent.resume_cmd)
            )
            and "/goal-plus resume" not in PiGoalPlusAgent.resume_cmd
            and "--goal-plus-headless-continue" not in PiGoalPlusAgent.resume_cmd
            and "Continue the active Goal Plus task" in PiGoalPlusAgent.resume_cmd
        )
        valid = bool(
            exact_start
            and typed_command_config
            and extension_loaded
            and reasoning_explicit
            and promotion_sync_persisted
            and native_resume
        )
        return {
            "valid": valid,
            "mode": "pi-extension-exact-host-command",
            "exact_start": exact_start,
            "typed_command_config": typed_command_config,
            "extension_loaded": extension_loaded,
            "reasoning_explicit": reasoning_explicit,
            "promotion_sync_persisted": promotion_sync_persisted,
            "native_resume": native_resume,
            "error": (
                None
                if valid
                else (
                    "SForge Goal Plus Pi adapter does not require exact host "
                    "start commands and stable native session resume with "
                    "an ordinary continuation prompt"
                )
            ),
        }
    except (ImportError, AttributeError, TypeError) as exc:
        return {
            "valid": False,
            "error": f"cannot inspect the active SForge Pi adapter: {exc}",
        }


def resolve_codex_runtime_archive(
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve and validate the exact Linux Codex archive used by SForge."""
    source = os.environ if env is None else env
    try:
        from sforge.harness.agent.codex import (
            CODEX_CLI_VERSION,
            CODEX_LINUX_RUNTIME_CACHE,
        )
    except (ImportError, AttributeError) as exc:
        return {
            "passed": False,
            "archive": None,
            "source": "sforge",
            "expected_version": None,
            "actual_version": None,
            "size": None,
            "error": f"cannot inspect the active SForge Codex runtime: {exc}",
        }

    override = source.get("SFORGE_CODEX_RUNTIME_ARCHIVE")
    if override is None:
        archive = Path(CODEX_LINUX_RUNTIME_CACHE).expanduser().resolve()
        archive_source = "sforge-default"
    elif override.strip():
        archive = Path(override).expanduser().resolve()
        archive_source = "SFORGE_CODEX_RUNTIME_ARCHIVE"
    else:
        archive = None
        archive_source = "SFORGE_CODEX_RUNTIME_ARCHIVE"

    actual_version: str | None = None
    error: str | None = None
    size: int | None = None
    if archive is None:
        error = "cached Codex runtime injection is explicitly disabled"
    elif not archive.is_file():
        error = f"Codex Linux runtime archive not found: {archive}"
    else:
        size = archive.stat().st_size
        try:
            with tarfile.open(archive, "r:gz") as bundle:
                metadata = bundle.extractfile(
                    "package/vendor/x86_64-unknown-linux-musl/codex-package.json"
                )
                if metadata is None:
                    raise ValueError("archive has no Codex runtime metadata")
                payload = json.loads(metadata.read().decode("utf-8"))
            actual_version = str(payload.get("version") or "") or None
            if payload.get("target") != "x86_64-unknown-linux-musl":
                error = "Codex runtime archive is not Linux x64"
            elif actual_version != CODEX_CLI_VERSION:
                error = (
                    f"Codex runtime version {actual_version!r} does not match "
                    f"SForge pin {CODEX_CLI_VERSION!r}"
                )
        except (OSError, tarfile.TarError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            error = f"invalid Codex Linux runtime archive: {exc}"

    return {
        "passed": error is None,
        "archive": str(archive) if archive is not None else None,
        "source": archive_source,
        "expected_version": CODEX_CLI_VERSION,
        "actual_version": actual_version,
        "size": size,
        "error": error,
    }


def resolve_agent_api_config(
    env: dict[str, str] | None = None,
    *,
    protocol: str = "openai",
) -> dict[str, str | None]:
    source = os.environ if env is None else env

    def first(names: tuple[str, ...]) -> tuple[str | None, str | None]:
        for name in names:
            value = source.get(name)
            if value:
                return value, name
        return None, None

    if protocol == "anthropic":
        key_names = (
            "SFORGE_AGENT_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_API_KEY",
        )
        base_names = ("SFORGE_AGENT_API_BASE_URL", "ANTHROPIC_BASE_URL")
    elif protocol == "openai":
        key_names = (
            "SFORGE_AGENT_API_KEY",
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
        )
        base_names = ("SFORGE_AGENT_API_BASE_URL", "OPENAI_BASE_URL")
    elif protocol == "pi-provider":
        return {
            "api_key": None,
            "api_key_source": None,
            "api_base_url": None,
            "api_base_url_source": None,
        }
    else:
        raise ValueError(f"unsupported agent API protocol: {protocol!r}")
    api_key, key_source = first(key_names)
    base_url, base_source = first(base_names)
    return {
        "api_key": api_key,
        "api_key_source": key_source,
        "api_base_url": base_url,
        "api_base_url_source": base_source,
    }


def resolve_pi_auth(env: dict[str, str] | None = None) -> dict[str, Any]:
    source = os.environ if env is None else env
    override = source.get("SFORGE_PI_AUTH_FILE")
    path = (
        Path(override).expanduser()
        if override
        else Path.home() / ".pi" / "agent" / "auth.json"
    )
    valid = False
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            valid = isinstance(payload.get("openai-codex"), dict)
        except (OSError, json.JSONDecodeError, AttributeError):
            valid = False
    return {"path": path, "valid": valid}


def pi_api_key_env_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"(?:\$\{([A-Z][A-Z0-9_]*)\}|\$([A-Z][A-Z0-9_]*))",
        value,
    )
    if not match:
        return None
    return next(group for group in match.groups() if group is not None)


def resolve_pi_provider(
    model_ref: str,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    source = os.environ if env is None else env
    provider, separator, model_id = model_ref.partition("/")
    result: dict[str, Any] = {
        "provider": provider or None,
        "model": model_id or None,
        "models_path": None,
        "model_registered": False,
        "credential_mode": None,
        "credential_env": None,
        "api_base_url": None,
        "valid": False,
        "error": None,
    }
    if not separator or not provider or not model_id:
        result["error"] = "model must be PROVIDER/MODEL"
        return result
    builtin_keys = PI_BUILTIN_PROVIDER_API_KEYS.get(provider)
    if builtin_keys:
        key_name = next((name for name in builtin_keys if source.get(name)), None)
        expected = " or ".join(builtin_keys)
        result.update(
            {
                "model_registered": True,
                "credential_mode": "environment",
                "credential_env": key_name or builtin_keys[0],
                "credential_env_candidates": list(builtin_keys),
                "api_base_url": PI_BUILTIN_PROVIDER_API_BASE_URLS.get(provider),
                "valid": key_name is not None,
                "error": None if key_name else f"missing {expected}",
            }
        )
        return result

    models_path = (
        Path(
            source.get(
                "SFORGE_PI_MODELS_FILE",
                Path.home() / ".pi" / "agent" / "models.json",
            )
        )
        .expanduser()
        .resolve()
    )
    result["models_path"] = str(models_path)
    if not models_path.is_file():
        result["error"] = "Pi models file not found"
        return result
    try:
        models = json.loads(models_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        result["error"] = f"invalid Pi models file: {exc}"
        return result
    provider_config = models.get("providers", {}).get(provider)
    if not isinstance(provider_config, dict):
        result["error"] = f"provider {provider!r} is not registered"
        return result
    base_url = provider_config.get("baseUrl")
    if isinstance(base_url, str) and base_url:
        result["api_base_url"] = base_url
    registered_ids = {
        entry.get("id")
        for entry in provider_config.get("models", [])
        if isinstance(entry, dict)
    }
    result["model_registered"] = model_id in registered_ids
    if not result["model_registered"]:
        result["error"] = f"model {model_id!r} is not registered"
        return result
    api_key = provider_config.get("apiKey")
    key_name = pi_api_key_env_name(api_key)
    if key_name:
        result.update(
            {
                "credential_mode": "environment",
                "credential_env": key_name,
                "valid": bool(source.get(key_name)),
                "error": None if source.get(key_name) else f"missing {key_name}",
            }
        )
    elif api_key is not None:
        result.update(
            {
                "credential_mode": "invalid-models-file-reference",
                "valid": False,
                "error": (
                    "provider apiKey must use $NAME or ${NAME}; "
                    "literal credentials are not allowed"
                ),
            }
        )
    else:
        result.update(
            {
                "credential_mode": "missing-models-file-reference",
                "valid": False,
                "error": "custom provider requires apiKey as $NAME or ${NAME}",
            }
        )
    return result


def resolve_pi_provider_bundle(
    model_refs: list[str] | tuple[str, ...],
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    source = os.environ if env is None else env
    unique_refs = list(dict.fromkeys(str(ref) for ref in model_refs))
    statuses = [resolve_pi_provider(ref, source) for ref in unique_refs]
    errors = [
        f"{ref}: {status['error']}"
        for ref, status in zip(unique_refs, statuses, strict=True)
        if not status["valid"]
    ]
    registry: dict[str, Any] = {"providers": {}}
    credential_envs = sorted(
        {
            str(status["credential_env"])
            for status in statuses
            if status.get("credential_env")
        }
    )
    registry_cache: dict[str, Any] = {}
    for ref, status in zip(unique_refs, statuses, strict=True):
        if not status["valid"] or status["models_path"] is None:
            continue
        models_path = str(status["models_path"])
        if models_path not in registry_cache:
            try:
                registry_cache[models_path] = json.loads(
                    Path(models_path).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError, AttributeError) as exc:
                errors.append(f"{ref}: invalid Pi models file: {exc}")
                continue
        provider = str(status["provider"])
        model_id = str(status["model"])
        provider_config = registry_cache[models_path].get("providers", {}).get(provider)
        if not isinstance(provider_config, dict):
            errors.append(f"{ref}: provider config disappeared during resolution")
            continue
        selected_models = [
            copy.deepcopy(item)
            for item in provider_config.get("models", [])
            if isinstance(item, dict) and item.get("id") == model_id
        ]
        if provider not in registry["providers"]:
            selected = copy.deepcopy(provider_config)
            selected["models"] = []
            registry["providers"][provider] = selected
        existing = registry["providers"][provider]["models"]
        existing_ids = {item.get("id") for item in existing}
        existing.extend(
            item for item in selected_models if item.get("id") not in existing_ids
        )
    return {
        "valid": not errors,
        "error": "; ".join(errors) if errors else None,
        "model_refs": unique_refs,
        "models": statuses,
        "credential_envs": credential_envs,
        "registry": registry,
    }


def pi_provider_bundle_contract(bundle: dict[str, Any]) -> dict[str, Any]:
    """Return the secret-free external provider inputs frozen by a campaign."""
    source_paths = sorted(
        {
            str(status["models_path"])
            for status in bundle.get("models", [])
            if status.get("models_path")
        }
    )
    return {
        "model_refs": list(bundle.get("model_refs", [])),
        "credential_envs": list(bundle.get("credential_envs", [])),
        "selected_registry_sha256": io.sha256_text(
            json.dumps(
                bundle.get("registry", {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
        "registry_sources": [
            {
                "path": io.portable_path(Path(path)),
                "sha256": io.sha256_file(Path(path)),
            }
            for path in source_paths
        ],
    }


def resolve_goal_plus_source(
    env: dict[str, str] | None = None,
    *,
    source_dir: str | Path | None = None,
    expected_ref: str | None = None,
    expected_commit: str | None = None,
    methods: list[str] | tuple[str, ...] | set[str] | None = None,
) -> dict[str, Any]:
    """Resolve and validate the exact Goal Plus source copied into containers."""
    source = os.environ if env is None else env
    configured_dir = source_dir or source.get("SFORGE_GOAL_PLUS_SOURCE_DIR")
    external = configured_dir is not None
    selected = (
        Path(configured_dir).expanduser()
        if external
        else current_paths().goal_plus_root
    )
    selected = selected.resolve()
    configured_ref = expected_ref or source.get("SFORGE_GOAL_PLUS_EXPECTED_REF")
    if not external and configured_ref is None:
        configured_ref = str(io.upstream_entry("goal_plus")["tracking_branch"])

    errors: list[str] = []
    if external and not configured_ref:
        errors.append(
            "external Goal Plus source requires SFORGE_GOAL_PLUS_EXPECTED_REF"
        )
    if configured_ref and (
        configured_ref.startswith("-")
        or "\x00" in configured_ref
        or configured_ref.strip() != configured_ref
    ):
        errors.append("Goal Plus expected ref is invalid")
        configured_ref = None
    if not selected.is_dir():
        errors.append(f"Goal Plus source directory not found: {selected}")

    checkout_root: Path | None = None
    branch: str | None = None
    commit: str | None = None
    dirty: bool | None = None
    ref_commit: str | None = None
    if selected.is_dir():
        top = io.run_capture(
            ["git", "-C", str(selected), "rev-parse", "--show-toplevel"]
        )
        if top["returncode"] != 0:
            errors.append("Goal Plus source is not inside a Git checkout")
        else:
            checkout_root = Path(top["stdout"]).resolve()
            branch = io.git_branch(checkout_root)
            commit = io.git_head(checkout_root)
            dirty = io.git_dirty(checkout_root)
            if dirty is not False:
                errors.append("Goal Plus checkout must be clean")
            if configured_ref:
                resolved = io.run_capture(
                    [
                        "git",
                        "-C",
                        str(checkout_root),
                        "rev-parse",
                        "--verify",
                        f"{configured_ref}^{{commit}}",
                    ]
                )
                if resolved["returncode"] != 0:
                    errors.append(
                        f"Goal Plus expected ref {configured_ref!r} does not resolve locally"
                    )
                else:
                    ref_commit = resolved["stdout"]
                    if commit != ref_commit:
                        errors.append(
                            "Goal Plus HEAD does not match the expected ref commit"
                        )
            if not external and branch != configured_ref:
                errors.append(
                    f"managed Goal Plus checkout must be on {configured_ref!r}"
                )
            if expected_commit and commit != expected_commit:
                errors.append(
                    "Goal Plus HEAD changed after the campaign source was frozen"
                )

    required_assets, asset_alternatives = goal_plus_runtime_asset_contract(methods)
    missing_assets = [
        relative
        for relative in required_assets
        if not (selected / relative).is_file()
    ]
    missing_asset_alternatives = [
        list(group)
        for group in asset_alternatives
        if not any((selected / relative).is_file() for relative in group)
    ]
    codex_runtime_compatibility: dict[str, Any] | None = None
    if methods is None or "goal-plus-codex" in set(methods):
        codex_runtime_compatibility = active_sforge_codex_runtime_contract()
        if not codex_runtime_compatibility["valid"]:
            errors.append(
                "active SForge Codex adapter does not satisfy the host-command contract"
            )
    pi_runtime_compatibility: dict[str, Any] | None = None
    if methods is None or set(methods) & {"goal-plus-pi", "goal-plus-pi-provider"}:
        pi_runtime_compatibility = active_sforge_pi_runtime_contract()
        if not pi_runtime_compatibility["valid"]:
            errors.append(
                "active SForge Pi adapter does not satisfy the host-command contract"
            )
    if missing_assets or missing_asset_alternatives:
        errors.append("Goal Plus source is missing required runtime assets")
    return {
        "valid": not errors,
        "source_kind": "external" if external else "managed",
        "source_dir": str(selected),
        "source_path": io.portable_path(selected),
        "checkout_root": (
            io.portable_path(checkout_root) if checkout_root is not None else None
        ),
        "expected_ref": configured_ref,
        "expected_ref_commit": ref_commit,
        "branch": branch,
        "commit": commit,
        "dirty": dirty,
        "required_assets": list(required_assets),
        "required_asset_alternatives": [list(group) for group in asset_alternatives],
        "missing_assets": missing_assets,
        "missing_asset_alternatives": missing_asset_alternatives,
        "codex_runtime_compatibility": codex_runtime_compatibility,
        "pi_runtime_compatibility": pi_runtime_compatibility,
        "error": "; ".join(errors) if errors else None,
    }


def _pi_probe_observations(stdout: str, marker: str) -> dict[str, Any]:
    providers: set[str] = set()
    models: set[str] = set()
    wire_apis: set[str] = set()
    assistant_text: list[str] = []
    tool_call_observed = False
    tool_result_observed = False
    thinking_observed = False
    event_count = 0
    invalid_json_lines = 0

    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            invalid_json_lines += 1
            continue
        if not isinstance(event, dict):
            continue
        event_count += 1
        event_type = str(event.get("type") or "")
        if event_type == "tool_execution_end":
            tool_result_observed = True
        update = event.get("assistantMessageEvent")
        if isinstance(update, dict) and str(update.get("type") or "").startswith(
            "thinking_"
        ):
            thinking_observed = True
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        for field, destination in (
            ("provider", providers),
            ("model", models),
            ("api", wire_apis),
        ):
            value = message.get(field)
            if isinstance(value, str) and value:
                destination.add(value)
        usage = message.get("usage")
        if isinstance(usage, dict) and int(usage.get("reasoning") or 0) > 0:
            thinking_observed = True
        role = str(message.get("role") or "")
        if role == "toolResult":
            tool_result_observed = True
        content = message.get("content")
        if isinstance(content, str):
            if role == "assistant":
                assistant_text.append(content)
            continue
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type == "toolCall":
                tool_call_observed = True
            if item_type == "thinking":
                thinking_observed = True
            text = item.get("text")
            if role == "assistant" and isinstance(text, str):
                assistant_text.append(text)

    return {
        "event_count": event_count,
        "invalid_json_lines": invalid_json_lines,
        "providers": sorted(providers),
        "models": sorted(models),
        "wire_apis": sorted(wire_apis),
        "tool_roundtrip": tool_call_observed and tool_result_observed,
        "thinking_observed": thinking_observed,
        "marker_observed": marker in "".join(assistant_text),
    }


def _redact_secret_values(value: str, env_names: list[str], env: dict[str, str]) -> str:
    redacted = value
    for name in env_names:
        secret = env.get(name)
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


def codex_provider_contract(
    profile: dict[str, Any],
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve the secret-free Codex provider inputs used by SForge."""
    source = os.environ if env is None else env
    api_config = resolve_agent_api_config(source, protocol="openai")
    api_key = api_config["api_key"]
    base_url = api_config["api_base_url"]
    auth_override = source.get("SFORGE_CODEX_AUTH_FILE")
    codex_home = Path(source.get("CODEX_HOME", Path.home() / ".codex"))
    auth_path = (
        Path(auth_override).expanduser().resolve()
        if auth_override
        else (codex_home / "auth.json").expanduser().resolve()
    )

    errors: list[str] = []
    endpoint: dict[str, Any] | None = None
    if base_url and not api_key:
        errors.append("a custom Codex API base URL requires an API key")
    if base_url:
        try:
            endpoint = sanitized_api_endpoint(str(base_url))
        except ValueError as exc:
            errors.append(str(exc))
    if not api_key and not auth_path.is_file():
        errors.append("Codex API credentials or OAuth auth file are required")

    if api_key and base_url:
        auth_mode = "openai-compatible"
        provider = "sforge-proxy"
    elif api_key:
        auth_mode = "openai-api-key"
        provider = "openai"
    else:
        auth_mode = "oauth"
        provider = "openai"
    model = str(profile["model"])
    reasoning_effort = str(profile.get("reasoning_effort") or "medium")
    provider_config = {
        "provider": provider,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "api_key_source": api_config["api_key_source"],
        "api_base_url_source": api_config["api_base_url_source"],
        "base_url": str(base_url) if base_url else None,
    }
    return {
        "valid": not errors,
        "auth_mode": auth_mode,
        "provider": provider,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "api_key_source": api_config["api_key_source"],
        "api_base_url_source": api_config["api_base_url_source"],
        "api_base_url_sha256": (
            io.sha256_text(str(base_url)) if base_url else None
        ),
        "api_endpoint": endpoint,
        "auth_path": io.portable_path(auth_path) if not api_key else None,
        "provider_config_sha256": io.sha256_text(
            json.dumps(
                provider_config,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
        "error": "; ".join(errors) if errors else None,
    }


def _codex_probe_observations(stdout: str, marker: str) -> dict[str, Any]:
    event_count = 0
    invalid_json_lines = 0
    tool_calls: set[str] = set()
    tool_results: set[str] = set()
    mcp_tool_calls: set[str] = set()
    mcp_tool_results: set[str] = set()
    mcp_tools: set[str] = set()
    mcp_tool_completion_counts: dict[str, int] = {}
    assistant_text: list[str] = []
    models: set[str] = set()
    providers: set[str] = set()
    turn_completed = False

    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            invalid_json_lines += 1
            continue
        if not isinstance(event, dict):
            continue
        event_count += 1
        event_type = str(event.get("type") or "")
        if event_type == "turn.completed":
            turn_completed = True
        for field, destination in (("model", models), ("provider", providers)):
            value = event.get(field)
            if isinstance(value, str) and value:
                destination.add(value)
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or f"event-{event_count}")
        item_type = str(item.get("type") or "")
        for field, destination in (("model", models), ("provider", providers)):
            value = item.get(field)
            if isinstance(value, str) and value:
                destination.add(value)
        if item_type in {"command_execution", "mcp_tool_call"}:
            tool_calls.add(item_id)
            if event_type == "item.completed" and (
                item.get("status") in {None, "completed"}
                and item.get("exit_code") in {None, 0}
            ):
                tool_results.add(item_id)
        if item_type == "mcp_tool_call":
            server = str(item.get("server") or "")
            tool = str(item.get("tool") or "")
            if server and tool:
                mcp_tools.add(f"{server}:{tool}")
            mcp_tool_calls.add(item_id)
            if event_type == "item.completed" and (
                item.get("status") in {None, "completed"}
                and not item.get("error")
            ):
                mcp_tool_results.add(item_id)
                key = f"{server}:{tool}"
                mcp_tool_completion_counts[key] = (
                    mcp_tool_completion_counts.get(key, 0) + 1
                )
        if item_type == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                assistant_text.append(text)

    return {
        "event_count": event_count,
        "invalid_json_lines": invalid_json_lines,
        "models": sorted(models),
        "providers": sorted(providers),
        "tool_roundtrip": bool(tool_calls & tool_results),
        "mcp_tool_roundtrip": bool(mcp_tool_calls & mcp_tool_results),
        "mcp_tools": sorted(mcp_tools),
        "mcp_tool_completion_counts": mcp_tool_completion_counts,
        "turn_completed": turn_completed,
        "marker_observed": marker in "".join(assistant_text),
    }


def codex_host_provider_probe(
    profile: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
    timeout_seconds: int = 90,
) -> dict[str, Any]:
    """Run the campaign model through its real host Codex contract before Docker."""
    source = dict(os.environ if env is None else env)
    contract = codex_provider_contract(profile, source)
    if not contract["valid"]:
        return {"passed": False, "contract": contract, "error": contract["error"]}

    methods = set(profile.get("methods") or ())
    requires_goal_plus_mcp = "goal-plus-codex" in methods
    goal_plus_source: dict[str, Any] | None = None
    goal_plus_executable: str | None = None
    if requires_goal_plus_mcp:
        frozen_source = profile.get("goal_plus_source") or {}
        goal_plus_source = resolve_goal_plus_source(
            env=source,
            source_dir=frozen_source.get("source_dir"),
            expected_ref=frozen_source.get("expected_ref"),
            expected_commit=frozen_source.get("commit"),
            methods=methods,
        )
        if not goal_plus_source["valid"]:
            return {
                "passed": False,
                "contract": contract,
                "goal_plus_mcp_required": True,
                "goal_plus_source": goal_plus_source,
                "error": "Goal Plus source is invalid for the Codex MCP probe: "
                + str(goal_plus_source["error"]),
            }
        goal_plus_executable = source.get(
            "SFORGE_GOAL_PLUS_HOST_EXECUTABLE"
        ) or shutil.which("goal-plus", path=source.get("PATH"))
        if not goal_plus_executable:
            return {
                "passed": False,
                "contract": contract,
                "goal_plus_mcp_required": True,
                "goal_plus_source": goal_plus_source,
                "error": "Goal Plus executable not found on the host",
            }

    executable = source.get("SFORGE_CODEX_HOST_EXECUTABLE") or shutil.which(
        "codex", path=source.get("PATH")
    )
    if not executable:
        return {
            "passed": False,
            "contract": contract,
            "error": "Codex executable not found on the host",
        }
    configure_temp_environment(source)
    version_result = io.run_capture(
        [executable, "--version"], env=source, timeout_seconds=10
    )
    codex_version = version_result["stdout"].strip() or None
    if version_result["returncode"] != 0:
        return {
            "passed": False,
            "contract": contract,
            "codex_version": codex_version,
            "error": _redact_secret_values(
                version_result["stderr"] or "host Codex version probe failed",
                [str(contract.get("api_key_source") or "")],
                source,
            ),
        }

    try:
        from sforge.harness.agent.codex import (
            CODEX_CLI_VERSION,
            CODEX_PROVIDER_REQUEST_MAX_RETRIES,
            CODEX_PROVIDER_STREAM_IDLE_TIMEOUT_MS,
            CODEX_PROVIDER_STREAM_MAX_RETRIES,
        )
    except (ImportError, AttributeError) as exc:
        return {
            "passed": False,
            "contract": contract,
            "codex_version": codex_version,
            "error": f"cannot resolve the campaign Codex version: {exc}",
        }
    expected_codex_version = f"codex-cli {CODEX_CLI_VERSION}"
    if codex_version != expected_codex_version:
        return {
            "passed": False,
            "contract": contract,
            "codex_version": codex_version,
            "expected_codex_version": expected_codex_version,
            "error": (
                f"host Codex version {codex_version!r} does not match the "
                f"campaign version {expected_codex_version!r}"
            ),
        }

    marker = "GOAL_PLUS_MCP_HOST_OK" if requires_goal_plus_mcp else "CODEX_HOST_API_OK"
    configured_auth = source.get("SFORGE_CODEX_AUTH_FILE")
    original_codex_home = Path(
        source.get("CODEX_HOME", Path.home() / ".codex")
    ).expanduser()
    with temporary_directory(
        prefix="codex-provider-probe-", namespace="edgebench"
    ) as probe_root:
        codex_home = probe_root / "codex-home"
        codex_home.mkdir(parents=True)
        probe_file = probe_root / "probe.txt"
        probe_file.write_text(marker + "\n", encoding="utf-8")
        source["CODEX_HOME"] = str(codex_home)

        api_config = resolve_agent_api_config(source, protocol="openai")
        api_key = api_config["api_key"]
        base_url = api_config["api_base_url"]
        ignore_user_config = True
        if api_key:
            source["OPENAI_API_KEY"] = str(api_key)
        config_lines: list[str] = []
        if api_key and base_url:
            config_lines.extend(
                (
                    'model_provider = "sforge-proxy"',
                    'model_verbosity = "medium"',
                    "model_reasoning_effort = "
                    + json.dumps(contract["reasoning_effort"]),
                    "model = " + json.dumps(contract["model"]),
                    "",
                    "[model_providers.sforge-proxy]",
                    'name = "sforge-proxy"',
                    "base_url = " + json.dumps(str(base_url)),
                    'env_key = "OPENAI_API_KEY"',
                    "stream_idle_timeout_ms = "
                    + str(CODEX_PROVIDER_STREAM_IDLE_TIMEOUT_MS),
                    "stream_max_retries = "
                    + str(CODEX_PROVIDER_STREAM_MAX_RETRIES),
                    "request_max_retries = "
                    + str(CODEX_PROVIDER_REQUEST_MAX_RETRIES),
                    "",
                )
            )
        elif not api_key:
            auth_path = (
                Path(configured_auth).expanduser().resolve()
                if configured_auth
                else (original_codex_home / "auth.json").resolve()
            )
            (codex_home / "auth.json").symlink_to(auth_path)

        if requires_goal_plus_mcp:
            assert goal_plus_source is not None
            assert goal_plus_executable is not None
            source_root = Path(str(goal_plus_source["source_dir"]))
            selected_python_path = str(source_root / "src")
            inherited_python_path = source.get("PYTHONPATH")
            source["PYTHONPATH"] = (
                selected_python_path
                if not inherited_python_path
                else selected_python_path + os.pathsep + inherited_python_path
            )
            state_root = probe_root / "goal-plus-state"
            config_lines.extend(
                (
                    "[mcp_servers.goal-plus]",
                    "command = " + json.dumps(goal_plus_executable),
                    "args = "
                    + json.dumps(["--root", str(state_root)], separators=(",", ":")),
                    'env_vars = ["PYTHONPATH"]',
                    "startup_timeout_sec = 30",
                    "tool_timeout_sec = 300",
                    "enabled = true",
                    'default_tools_approval_mode = "approve"',
                    "",
                )
            )

        if config_lines:
            config = "\n".join(config_lines)
            config_path = codex_home / "config.toml"
            config_path.write_text(config, encoding="utf-8")
            config_path.chmod(0o600)
            ignore_user_config = False

        command = [
            executable,
            "exec",
            "--disable",
            "plugins",
            "--json",
            "--ephemeral",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-C",
            str(probe_root),
            "--model",
            str(contract["model"]),
            "-c",
            "model_reasoning_effort="
            + json.dumps(str(contract["reasoning_effort"])),
        ]
        if ignore_user_config:
            command.append("--ignore-user-config")
        if requires_goal_plus_mcp:
            command.append(
                "Call goal_plus_monitor_snapshot exactly twice in sequence through the "
                f"Goal Plus MCP server. Reply with exactly {marker} only after both "
                "native MCP tool calls complete successfully."
            )
        else:
            command.append(
                "Use the shell tool exactly once to read "
                f"{probe_file}. Then reply with exactly {marker}."
            )
        result = io.run_capture(command, env=source, timeout_seconds=timeout_seconds)

    observations = _codex_probe_observations(result["stdout"], marker)
    exact_model = (
        not observations["models"] or contract["model"] in observations["models"]
    )
    exact_provider = (
        not observations["providers"]
        or contract["provider"] in observations["providers"]
    )
    exact_goal_plus_mcp = (
        not requires_goal_plus_mcp
        or (
            observations["mcp_tool_roundtrip"]
            and observations["mcp_tool_completion_counts"].get(
                "goal-plus:goal_plus_monitor_snapshot", 0
            )
            >= 2
        )
    )
    passed = (
        result["returncode"] == 0
        and observations["tool_roundtrip"]
        and exact_goal_plus_mcp
        and observations["turn_completed"]
        and observations["marker_observed"]
        and exact_model
        and exact_provider
    )
    failures: list[str] = []
    secret_envs = [str(contract.get("api_key_source") or "")]
    if result["returncode"] != 0:
        failures.append(
            _redact_secret_values(
                result["stderr"] or f"Codex exited {result['returncode']}",
                secret_envs,
                source,
            )
        )
    if not observations["tool_roundtrip"]:
        failures.append("Codex did not complete a tool call/result round-trip")
    if not exact_goal_plus_mcp:
        failures.append(
            "Codex did not complete the required Goal Plus MCP "
            "goal_plus_monitor_snapshot round-trip twice in sequence"
        )
    if not observations["turn_completed"]:
        failures.append("Codex did not complete the turn")
    if not observations["marker_observed"]:
        failures.append("Codex did not produce the expected final response")
    if not exact_model:
        failures.append("Codex reported a different model")
    if not exact_provider:
        failures.append("Codex reported a different provider")
    return {
        "passed": passed,
        "contract": contract,
        "codex_version": codex_version,
        "expected_codex_version": expected_codex_version,
        "goal_plus_mcp_required": requires_goal_plus_mcp,
        "goal_plus_source": (
            {
                key: goal_plus_source.get(key)
                for key in (
                    "source_kind",
                    "source_path",
                    "expected_ref",
                    "branch",
                    "commit",
                )
            }
            if goal_plus_source is not None
            else None
        ),
        **observations,
        "error": "; ".join(failures) if failures else None,
    }


def pi_host_provider_probe(
    model_ref: str,
    *,
    reasoning_effort: str | None = None,
    expected_pi_version: str | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: int = 90,
) -> dict[str, Any]:
    """Exercise one Pi provider with the exact host registry and credentials."""
    source = dict(os.environ if env is None else env)
    bundle = resolve_pi_provider_bundle([model_ref], source)
    provider, separator, model_id = model_ref.partition("/")
    if not separator or not bundle["valid"]:
        return {
            "passed": False,
            "model_ref": model_ref,
            "error": bundle.get("error") or "model must be PROVIDER/MODEL",
        }

    executable = source.get("SFORGE_PI_HOST_EXECUTABLE") or shutil.which(
        "pi", path=source.get("PATH")
    )
    if not executable:
        return {
            "passed": False,
            "model_ref": model_ref,
            "error": "Pi executable not found on the host",
        }
    configure_temp_environment(source)
    version_result = io.run_capture(
        [executable, "--version"], env=source, timeout_seconds=10
    )
    actual_version = (
        version_result["stdout"].splitlines()[-1].strip()
        if version_result["stdout"]
        else None
    )
    version_matches = (
        version_result["returncode"] == 0
        and actual_version is not None
        and (
            not expected_pi_version
            or expected_pi_version == "latest"
            or actual_version == expected_pi_version
        )
    )
    if not version_matches:
        return {
            "passed": False,
            "model_ref": model_ref,
            "pi_version": actual_version,
            "expected_pi_version": expected_pi_version,
            "error": _redact_secret_values(
                version_result["stderr"] or "host Pi version does not match profile",
                list(bundle["credential_envs"]),
                source,
            ),
        }

    marker = "PI_HOST_API_OK"
    with temporary_directory(
        prefix="pi-provider-probe-", namespace="edgebench"
    ) as probe_root:
        agent_dir = probe_root / "agent"
        agent_dir.mkdir(parents=True)
        if bundle["registry"].get("providers"):
            models_path = agent_dir / "models.json"
            io.write_json(models_path, bundle["registry"])
            models_path.chmod(0o600)
        probe_file = probe_root / "probe.txt"
        probe_file.write_text(marker + "\n", encoding="utf-8")
        source["PI_CODING_AGENT_DIR"] = str(agent_dir)
        source["PI_OFFLINE"] = "1"
        command = [
            executable,
            "--offline",
            "--mode",
            "json",
            "--print",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-approve",
            "--tools",
            "read",
            "--provider",
            provider,
            "--model",
            model_id,
        ]
        if reasoning_effort:
            command.extend(["--thinking", reasoning_effort])
        command.append(
            "Use the read tool exactly once to read "
            f"{probe_file}. Then reply with exactly {marker}."
        )
        result = io.run_capture(command, env=source, timeout_seconds=timeout_seconds)

    observations = _pi_probe_observations(result["stdout"], marker)
    exact_model_observed = (
        provider in observations["providers"] and model_id in observations["models"]
    )
    passed = (
        result["returncode"] == 0
        and observations["tool_roundtrip"]
        and observations["marker_observed"]
        and exact_model_observed
    )
    failures: list[str] = []
    if result["returncode"] != 0:
        failures.append(
            _redact_secret_values(
                result["stderr"] or f"Pi exited {result['returncode']}",
                list(bundle["credential_envs"]),
                source,
            )
        )
    if not observations["tool_roundtrip"]:
        failures.append("Pi did not complete a tool call/result round-trip")
    if not observations["marker_observed"]:
        failures.append("Pi did not produce the expected final response")
    if not exact_model_observed:
        failures.append("Pi did not report the requested provider/model")
    return {
        "passed": passed,
        "model_ref": model_ref,
        "pi_version": actual_version,
        "expected_pi_version": expected_pi_version,
        "credential_envs": list(bundle["credential_envs"]),
        **observations,
        "error": "; ".join(failures) if failures else None,
    }


def pi_provider_host_preflight(
    profile: dict[str, Any],
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run real host Pi calls for every distinct campaign model contract."""
    source = dict(os.environ if env is None else env)
    model_refs = pi_provider_role_model_refs(profile)
    bundle = resolve_pi_provider_bundle(model_refs, source)
    if not bundle["valid"]:
        return {
            "passed": False,
            "contract": None,
            "probes": [],
            "error": bundle["error"],
        }

    role_specs = (
        ("main", str(profile["model"]), profile.get("reasoning_effort")),
        (
            "worker",
            str(profile.get("worker_model") or profile["model"]),
            profile.get("worker_reasoning_effort") or profile.get("reasoning_effort"),
        ),
        (
            "evidence_annotator",
            str(profile.get("evidence_annotator_model") or profile["model"]),
            profile.get("evidence_annotator_reasoning_effort")
            or profile.get("reasoning_effort"),
        ),
    )
    grouped_roles: dict[tuple[str, str | None], list[str]] = {}
    for role, model_ref, effort in role_specs:
        grouped_roles.setdefault(
            (model_ref, str(effort) if effort else None), []
        ).append(role)

    probes: list[dict[str, Any]] = []
    for (model_ref, effort), roles in grouped_roles.items():
        probe = pi_host_provider_probe(
            model_ref,
            reasoning_effort=effort,
            expected_pi_version=profile.get("pi_package_version"),
            env=source,
        )
        probes.append({**probe, "roles": roles, "reasoning_effort": effort})
    return {
        "passed": all(bool(probe["passed"]) for probe in probes),
        "contract": pi_provider_bundle_contract(bundle),
        "probes": probes,
        "error": None,
    }


def sanitized_api_endpoint(base_url: str) -> dict[str, Any]:
    """Return a credential-free endpoint identity suitable for evidence files."""
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(
            f"LLM API base URL must be an absolute http(s) URL: {base_url!r}"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("LLM API base URL must not contain credentials")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError(f"LLM API base URL has an invalid port: {base_url!r}") from exc
    return {"scheme": parsed.scheme, "host": parsed.hostname, "port": port}


def resolve_profile_api_endpoints(
    profile: dict[str, Any],
    env: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Resolve every model-calling role to the API endpoint it must reach."""
    source = os.environ if env is None else env
    methods = [str(method) for method in profile["methods"]]
    protocol = api_protocol_for_methods(methods)
    endpoints: list[dict[str, Any]] = []

    if protocol == "pi-provider":
        role_refs = [("main", str(profile["model"]))]
        if set(methods) & GOAL_PLUS_METHODS:
            role_refs.extend(
                [
                    (
                        "worker",
                        str(profile.get("worker_model") or profile["model"]),
                    ),
                    (
                        "evidence_annotator",
                        str(
                            profile.get("evidence_annotator_model") or profile["model"]
                        ),
                    ),
                ]
            )
        for role, model_ref in role_refs:
            status = resolve_pi_provider(model_ref, source)
            base_url = status.get("api_base_url")
            if not isinstance(base_url, str) or not base_url:
                raise ValueError(
                    f"Pi {role} model {model_ref!r} has no resolvable API base URL; "
                    "use a built-in provider with a registered endpoint or set baseUrl "
                    "in SFORGE_PI_MODELS_FILE"
                )
            endpoints.append(
                {
                    "method": methods[0],
                    "role": role,
                    "model": model_ref,
                    "base_url": base_url,
                    "source": (
                        "pi-built-in-provider"
                        if status.get("models_path") is None
                        else "pi-models-file"
                    ),
                    **sanitized_api_endpoint(base_url),
                }
            )
        return endpoints

    api_config = resolve_agent_api_config(source, protocol=protocol)
    configured_base_url = api_config.get("api_base_url")
    for method in methods:
        agent = str(METHODS[method]["agent"])
        base_url = configured_base_url or SFORGE_AGENT_DEFAULT_API_BASE_URLS.get(agent)
        if not isinstance(base_url, str) or not base_url:
            raise ValueError(
                f"EdgeBench method {method!r} has no resolvable LLM API base URL"
            )
        roles = ["main"]
        if method in GOAL_PLUS_METHODS:
            roles.extend(["worker", "evidence_annotator"])
        for role in roles:
            endpoints.append(
                {
                    "method": method,
                    "role": role,
                    "model": str(profile["model"]),
                    "base_url": str(base_url),
                    "source": (
                        str(api_config.get("api_base_url_source"))
                        if configured_base_url
                        else "sforge-agent-default"
                    ),
                    **sanitized_api_endpoint(str(base_url)),
                }
            )
    return endpoints


def require_api_only_network(
    profile: dict[str, Any],
    official_protocol: dict[str, Any] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fail closed unless every task is isolated and every API role is resolvable."""
    protocol = official_protocol or load_official_codex_protocol()
    task_contracts: list[dict[str, Any]] = []
    open_network: list[dict[str, str]] = []
    for task_id in profile["task_ids"]:
        config = task_config(str(task_id))
        effective = profile_task_protocol(profile, protocol, str(task_id), config)
        source = (
            f"profiles/{profile['id']}.protocol_overrides.internet"
            if "internet" in profile.get("protocol_overrides", {})
            else f"tasks/{task_id}.json"
        )
        task_contracts.append(
            {
                "task_id": str(task_id),
                "internet": effective["internet"],
                "source": source,
            }
        )
        if effective["internet"] is not False:
            open_network.append({"task_id": str(task_id), "source": source})
    if open_network:
        details = ", ".join(
            f"{item['task_id']} ({item['source']})" for item in open_network
        )
        raise ValueError(
            "EdgeBench Agent network policy must be API-only; internet=true is "
            f"forbidden for: {details}"
        )

    endpoints = resolve_profile_api_endpoints(profile, env)
    return {
        "policy": "api-only",
        "tasks": task_contracts,
        "judge": "per-cell SForge Judge endpoint",
        "api_endpoints": endpoints,
    }


def loopback_api_target(base_url: str) -> tuple[str, int] | None:
    return _loopback_target(base_url)


def bridged_base_url(base_url: str, host: str, port: int) -> str:
    return _bridged_url(base_url, host, port)


def default_route_ipv4() -> str:
    return _default_route_ipv4(root=current_paths().root)


def start_socket_bridge(
    destination: Path,
    *,
    name: str,
    listen_host: str,
    target_host: str,
    target_port: int,
) -> tuple[subprocess.Popen[str], dict[str, Any], Any]:
    return _start_socket_bridge(
        destination,
        name=name,
        listen_host=listen_host,
        target_host=target_host,
        target_port=target_port,
        root=current_paths().root,
        display_path=io.portable_path,
    )


def agent_api_probe_url(base_url: str, protocol: str) -> str:
    base = base_url.rstrip("/")
    if protocol == "anthropic":
        return base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    if protocol == "openai":
        return base + "/models"
    raise ValueError(f"unsupported agent API protocol: {protocol!r}")


def authenticated_api_probe(
    base_url: str,
    api_key: str,
    *,
    protocol: str = "openai",
    model: str | None = None,
    thinking: dict[str, str] | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    url = agent_api_probe_url(base_url, protocol)
    headers = {"Authorization": f"Bearer {api_key}"}
    data: bytes | None = None
    if protocol == "anthropic":
        if not model:
            raise ValueError("Anthropic API probes require a model")
        headers.update(
            {
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        )
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "Reply OK."}],
        }
        if thinking is not None:
            payload["thinking"] = thinking
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, headers=headers, data=data)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return {"passed": response.status == 200, "status": response.status}
    except urllib.error.HTTPError as exc:
        return {"passed": False, "status": exc.code, "error": str(exc)}
    except (OSError, urllib.error.URLError) as exc:
        return {"passed": False, "status": None, "error": str(exc)}


def endpoint_reachability_probe(base_url: str) -> dict[str, Any]:
    """Check a dynamic API endpoint without guessing its wire route."""
    sanitized_api_endpoint(base_url)
    request = urllib.request.Request(base_url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return {"passed": True, "status": response.status}
    except urllib.error.HTTPError as exc:
        return {"passed": True, "status": exc.code}
    except (OSError, urllib.error.URLError) as exc:
        return {"passed": False, "status": None, "error": str(exc)}


def append_no_proxy(env: dict[str, str], host: str) -> None:
    current = env.get("NO_PROXY") or env.get("no_proxy") or ""
    entries = [item.strip() for item in current.split(",") if item.strip()]
    if host not in entries:
        entries.insert(0, host)
    value = ",".join(entries)
    env["NO_PROXY"] = value
    env["no_proxy"] = value
    env["SFORGE_NO_PROXY"] = value


def judge_server_environment(
    *,
    api_key: str | None,
    api_base_url: str | None,
    bridge_host: str | None,
    model: str = "gpt-5.5",
) -> dict[str, str]:
    env = dict(os.environ)
    configure_temp_environment(env)
    entries: dict[str, str] = {}
    for item in env.get("SFORGE_JUDGE_EXTRA_ENV", "").split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            entries[key.strip()] = value.strip()
    if api_key:
        entries.setdefault("SFORGE_JUDGE_API_KEY", api_key)
    if api_base_url:
        entries.setdefault("SFORGE_JUDGE_API_BASE_URL", api_base_url)
    entries.setdefault("SFORGE_JUDGE_MODEL", model)
    if entries:
        env["SFORGE_JUDGE_EXTRA_ENV"] = ",".join(
            f"{key}={value}" for key, value in sorted(entries.items())
        )
    if bridge_host:
        append_no_proxy(env, bridge_host)
    return env


def docker_http_probe(
    image: str,
    url: str,
    *,
    api_key: str | None = None,
    protocol: str | None = None,
    model: str | None = None,
    thinking_type: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    env = dict(os.environ)
    configure_temp_environment(env)
    env["SFORGE_PROBE_URL"] = agent_api_probe_url(url, protocol) if protocol else url
    command = [
        "docker",
        "run",
        "--pull",
        "never",
        "--rm",
        "--entrypoint",
        "/bin/sh",
        "-e",
        "SFORGE_PROBE_URL",
    ]
    if api_key:
        env["SFORGE_PROBE_API_KEY"] = api_key
        command.extend(["-e", "SFORGE_PROBE_API_KEY"])
    if protocol:
        env["SFORGE_PROBE_PROTOCOL"] = protocol
        command.extend(["-e", "SFORGE_PROBE_PROTOCOL"])
    if model:
        env["SFORGE_PROBE_MODEL"] = model
        command.extend(["-e", "SFORGE_PROBE_MODEL"])
    if thinking_type:
        env["SFORGE_PROBE_THINKING_TYPE"] = thinking_type
        command.extend(["-e", "SFORGE_PROBE_THINKING_TYPE"])
    if reasoning_effort:
        env["SFORGE_PROBE_REASONING_EFFORT"] = reasoning_effort
        command.extend(["-e", "SFORGE_PROBE_REASONING_EFFORT"])
    command.extend(
        [
            image,
            "-c",
            (
                'if [ "${SFORGE_PROBE_PROTOCOL:-}" = anthropic ]; then '
                'if [ -n "${SFORGE_PROBE_REASONING_EFFORT:-}" ]; then '
                'payload=\'{"model":"\'"$SFORGE_PROBE_MODEL"\'",'
                '"max_tokens":1,"messages":[{"role":"user",'
                '"content":"Reply OK."}],"thinking":{"type":"\''
                '"$SFORGE_PROBE_THINKING_TYPE"\'"},"reasoning_effort":"\''
                '"$SFORGE_PROBE_REASONING_EFFORT"\'"}\'; '
                'else payload=\'{"model":"\'"$SFORGE_PROBE_MODEL"\'",'
                '"max_tokens":1,"messages":[{"role":"user",'
                '"content":"Reply OK."}],"thinking":{"type":"\''
                '"$SFORGE_PROBE_THINKING_TYPE"\'"}}\'; fi; '
                'auth="Authorization: Bearer $SFORGE_PROBE_API_KEY"; '
                "code=$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' "
                '--max-time 30 -X POST -H "$auth" '
                "-H 'anthropic-version: 2023-06-01' "
                "-H 'content-type: application/json' "
                '--data "$payload" "$SFORGE_PROBE_URL"); '
                'elif [ -n "${SFORGE_PROBE_API_KEY:-}" ]; then '
                'auth="Authorization: Bearer $SFORGE_PROBE_API_KEY"; '
                "code=$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' "
                '--max-time 15 -H "$auth" "$SFORGE_PROBE_URL"); '
                "else code=$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' "
                '--max-time 15 "$SFORGE_PROBE_URL"); fi; '
                'printf \'%s\\n\' "$code"; test "$code" = 200'
            ),
        ]
    )
    result = io.run_capture(command, env=env)
    return {
        "passed": result["returncode"] == 0,
        "status": result["stdout"].splitlines()[-1] if result["stdout"] else None,
        "stderr": result["stderr"][-400:] or None,
    }


def docker_endpoint_reachability_probe(image: str, base_url: str) -> dict[str, Any]:
    """Prove container connectivity without credentials or API route guesses."""
    sanitized_api_endpoint(base_url)
    env = dict(os.environ)
    configure_temp_environment(env)
    env["SFORGE_PROBE_URL"] = base_url
    command = [
        "docker",
        "run",
        "--pull",
        "never",
        "--rm",
        "--entrypoint",
        "/bin/sh",
        "-e",
        "SFORGE_PROBE_URL",
        image,
        "-c",
        (
            "code=$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' "
            '--max-time 15 "$SFORGE_PROBE_URL"); rc=$?; '
            "printf '%s\\n' \"$code\"; "
            'test "$rc" = 0 && test "$code" != 000'
        ),
    ]
    result = io.run_capture(command, env=env)
    return {
        "passed": result["returncode"] == 0,
        "status": result["stdout"].splitlines()[-1] if result["stdout"] else None,
        "stderr": result["stderr"][-400:] or None,
    }


def _docker_memory_bytes(value: str) -> int:
    match = re.fullmatch(r"([0-9]+)([kmgt]?)(?:i?b)?", value.strip().lower())
    if not match:
        raise ValueError(f"unsupported Docker memory limit: {value}")
    amount = int(match.group(1))
    exponent = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4}[match.group(2)]
    return amount * (1024**exponent)


def docker_resource_limit_probe(
    image: str, *, cpu_limit: int, mem_limit: str
) -> dict[str, Any]:
    name = f"edgebench-resource-probe-{os.getpid()}-{time.time_ns()}"
    expected_nano_cpus = int(cpu_limit * 1_000_000_000)
    expected_memory = _docker_memory_bytes(mem_limit)
    started = io.run_capture(
        [
            "docker",
            "run",
            "--pull",
            "never",
            "--detach",
            "--name",
            name,
            "--cpus",
            str(cpu_limit),
            "--memory",
            mem_limit,
            "--entrypoint",
            "/bin/sh",
            image,
            "-c",
            "while :; do sleep 60; done",
        ]
    )
    inspected: dict[str, Any] | None = None
    inspect_result: dict[str, Any] | None = None
    try:
        if started["returncode"] == 0:
            inspect_result = io.run_capture(
                ["docker", "inspect", "--format", "{{json .HostConfig}}", name]
            )
            if inspect_result["returncode"] == 0:
                try:
                    inspected = json.loads(inspect_result["stdout"])
                except json.JSONDecodeError:
                    inspected = None
    finally:
        io.run_capture(["docker", "rm", "--force", name])

    actual_nano_cpus = inspected.get("NanoCpus") if inspected else None
    actual_memory = inspected.get("Memory") if inspected else None
    return {
        "passed": (
            started["returncode"] == 0
            and inspect_result is not None
            and inspect_result["returncode"] == 0
            and actual_nano_cpus == expected_nano_cpus
            and actual_memory == expected_memory
        ),
        "image": image,
        "cpu_limit": cpu_limit,
        "mem_limit": mem_limit,
        "expected_nano_cpus": expected_nano_cpus,
        "actual_nano_cpus": actual_nano_cpus,
        "expected_memory_bytes": expected_memory,
        "actual_memory_bytes": actual_memory,
        "stderr": (
            started["stderr"][-400:]
            or ((inspect_result or {}).get("stderr") or "")[-400:]
            or None
        ),
    }


def sforge_iptables_permission_probe() -> dict[str, Any]:
    python = current_paths().venv_python
    if not python.is_file():
        return {"passed": False, "stderr": "benchmark virtualenv is missing"}
    result = io.run_capture(
        [
            str(python),
            "-c",
            (
                "from sforge.harness.network_isolation import "
                "check_iptables_permission; "
                "raise SystemExit(0 if check_iptables_permission() else 1)"
            ),
        ]
    )
    return {
        "passed": result["returncode"] == 0,
        "stderr": result["stderr"][-400:] or None,
    }


def task_config(task_id: str) -> dict[str, Any]:
    path = current_paths().tasks_dir / f"{task_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"task definition missing: {path}; run provision first")
    return io.read_json(path)


def task_images(task_id: str) -> tuple[str, str]:
    config = task_config(task_id)
    return (
        f"edgebench.work.{task_id}:{config['work']['image_tag']}",
        f"edgebench.judge.{task_id}:{config['judge']['image_tag']}",
    )


def rust_runtime_asset() -> dict[str, str]:
    path = current_paths().edge_root / "sforge" / "harness" / "runtime_assets.json"
    payload = io.read_json(path)
    asset = payload.get("rust") if payload.get("schema_version") == 1 else None
    required = {"version", "target", "archive_name", "url", "sha256"}
    if not isinstance(asset, dict) or required - set(asset):
        raise RuntimeError(f"invalid EdgeBench runtime asset manifest: {path}")
    return {key: str(asset[key]) for key in required}


def rust_runtime_archive_status() -> dict[str, Any]:
    try:
        asset = rust_runtime_asset()
        archive = Path.home() / ".cache" / "sforge" / "rust" / asset["archive_name"]
        actual_sha256 = io.sha256_file(archive) if archive.is_file() else None
        return {
            "passed": actual_sha256 == asset["sha256"],
            "path": str(archive),
            "version": asset["version"],
            "expected_sha256": asset["sha256"],
            "actual_sha256": actual_sha256,
        }
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        return {"passed": False, "error": str(exc)}


def rust_image_runtime_probe(image: str, version: str) -> dict[str, Any]:
    command = (
        "set -e; command -v cargo; command -v rustc; "
        f"cargo --version | grep -F 'cargo {version} '; "
        f"rustc --version | grep -F 'rustc {version} '"
    )
    return io.run_capture(
        [
            "docker",
            "run",
            "--pull",
            "never",
            "--rm",
            "--entrypoint",
            "/bin/bash",
            image,
            "-c",
            command,
        ]
    )


def dataset_revision(task_id: str) -> str | None:
    metadata = (
        current_paths().tasks_dir
        / ".cache"
        / "huggingface"
        / "download"
        / f"{task_id}.json.metadata"
    )
    if not metadata.is_file():
        return None
    lines = metadata.read_text(encoding="utf-8").splitlines()
    return lines[0].strip() if lines else None


def _local_containers() -> (
    tuple[dict[str, Any], list[dict[str, Any]], list[str], list[str]]
):
    command = [
        "docker",
        "ps",
        "-a",
        "--no-trunc",
        "--format",
        "{{json .}}",
    ]
    result = io.run_capture(command)
    containers: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    if result["returncode"] == 0:
        for line_number, line in enumerate(result["stdout"].splitlines(), start=1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                parse_errors.append(f"line {line_number}: {exc.msg}")
                continue
            if isinstance(item, dict):
                containers.append(item)
            else:
                parse_errors.append(f"line {line_number}: expected a JSON object")
    return result, containers, parse_errors, command


def _container_summary(container: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": container.get("ID"),
        "name": container.get("Names"),
        "image": container.get("Image"),
        "image_id": container.get("ImageID"),
        "state": container.get("State"),
        "status": container.get("Status"),
    }


def _inspect_local_image(
    role: str,
    reference: str,
    containers: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    command = ["docker", "image", "inspect", reference]
    result = io.run_capture(command)
    record: dict[str, Any] = {
        "role": role,
        "reference": reference,
        "present": False,
        "image_id": None,
        "repo_tags": [],
        "repo_digests": [],
        "size_bytes": None,
        "architecture": None,
        "os": None,
        "containers": [],
    }
    if result["returncode"] != 0:
        record["error"] = result["stderr"] or "exact image reference is missing"
        return record, command

    try:
        payload = json.loads(result["stdout"])
        details = payload[0] if isinstance(payload, list) and payload else payload
    except json.JSONDecodeError as exc:
        record["error"] = f"docker image inspect returned invalid JSON: {exc.msg}"
        return record, command
    if not isinstance(details, dict):
        record["error"] = "docker image inspect returned no image object"
        return record, command

    image_id = details.get("Id")
    repo_tags = details.get("RepoTags") or []
    repo_digests = details.get("RepoDigests") or []
    if (
        not isinstance(image_id, str)
        or not isinstance(repo_tags, list)
        or not isinstance(repo_digests, list)
    ):
        record["error"] = "docker image inspect returned an invalid image object"
        return record, command
    aliases = {
        str(value)
        for value in (reference, image_id, *repo_tags, *repo_digests)
        if value
    }
    if isinstance(image_id, str) and image_id.startswith("sha256:"):
        aliases.add(image_id.removeprefix("sha256:"))
    matching = [
        _container_summary(container)
        for container in containers
        if str(container.get("Image") or "") in aliases
        or str(container.get("ImageID") or "") in aliases
    ]
    record.update(
        {
            "present": True,
            "image_id": image_id,
            "repo_tags": list(repo_tags),
            "repo_digests": list(repo_digests),
            "size_bytes": details.get("Size"),
            "architecture": details.get("Architecture"),
            "os": details.get("Os"),
            "containers": matching,
        }
    )
    return record, command


def _known_asset_issue(
    issues: list[dict[str, Any]],
    role: str,
    reference: str,
    dataset_revision: str | None,
    image_id: str | None,
) -> dict[str, Any] | None:
    for issue in issues:
        if (
            issue.get("role") == role
            and issue.get("reference") == reference
            and asset_issue_matches_revision(issue, dataset_revision)
            and issue.get("image_id") == image_id
        ):
            return issue
    return None


def local_asset_inventory(profile: dict[str, Any]) -> dict[str, Any]:
    """Inspect exact local EdgeBench assets without acquiring or running anything."""

    (
        container_result,
        containers,
        container_parse_errors,
        container_command,
    ) = _local_containers()
    docker_commands = [container_command]
    try:
        asset_issues = known_asset_issues()
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        asset_issues = []
        asset_issue_registry_error: str | None = str(exc)
    else:
        asset_issue_registry_error = None
    expected_revision = str(profile["dataset_revision"])
    tasks: list[dict[str, Any]] = []
    for raw_task_id in profile["task_ids"]:
        task_id = str(raw_task_id)
        task_path = current_paths().tasks_dir / f"{task_id}.json"
        try:
            actual_revision = dataset_revision(task_id)
        except (OSError, UnicodeError) as exc:
            actual_revision = None
            revision_error: str | None = str(exc)
        else:
            revision_error = None
        task: dict[str, Any] = {
            "task_id": task_id,
            "task_file": str(task_path),
            "task_file_present": task_path.is_file(),
            "expected_dataset_revision": expected_revision,
            "actual_dataset_revision": actual_revision,
            "dataset_revision_matches": actual_revision == expected_revision,
            "images": [],
        }
        if revision_error:
            task["revision_error"] = revision_error
        if task_path.is_file():
            try:
                references = task_images(task_id)
            except (OSError, UnicodeError, KeyError, TypeError, ValueError) as exc:
                task["task_error"] = str(exc)
            else:
                for role, reference in zip(("Work", "Judge"), references, strict=True):
                    image, command = _inspect_local_image(role, reference, containers)
                    known_issue = _known_asset_issue(
                        asset_issues,
                        role,
                        reference,
                        actual_revision,
                        image.get("image_id"),
                    )
                    if known_issue is not None:
                        image["known_issue"] = known_issue
                        image["known_issue_image_id_matches"] = image.get(
                            "image_id"
                        ) == known_issue.get("image_id")
                    task["images"].append(image)
                    docker_commands.append(command)
        tasks.append(task)

    images = [image for task in tasks for image in task["images"]]
    matched_container_ids = {
        str(container.get("id") or container.get("name"))
        for image in images
        for container in image["containers"]
    }
    matched_container_ids.discard("None")
    expected_image_count = len(tasks) * 2
    present_image_count = sum(image["present"] for image in images)
    blocking_known_issues = [
        image["known_issue"]
        for image in images
        if image.get("present")
        and isinstance(image.get("known_issue"), dict)
        and image["known_issue"].get("severity") == "blocking"
    ]
    summary = {
        "tasks_expected": len(tasks),
        "task_files_present": sum(task["task_file_present"] for task in tasks),
        "task_revisions_matching": sum(
            task["dataset_revision_matches"] for task in tasks
        ),
        "images_expected": expected_image_count,
        "image_references_resolved": len(images),
        "images_present": present_image_count,
        "images_missing": expected_image_count - present_image_count,
        "blocking_known_asset_issues": len(blocking_known_issues),
        "matching_containers": len(matched_container_ids),
    }
    container_inventory_ok = (
        container_result["returncode"] == 0 and not container_parse_errors
    )
    ok = (
        bool(tasks)
        and summary["task_files_present"] == summary["tasks_expected"]
        and summary["task_revisions_matching"] == summary["tasks_expected"]
        and summary["image_references_resolved"] == expected_image_count
        and summary["images_present"] == expected_image_count
        and not blocking_known_issues
        and container_inventory_ok
        and asset_issue_registry_error is None
    )
    return {
        "schema_version": 1,
        "action": "local-asset-inventory",
        "checked_at": io.utc_now(),
        "profile": str(profile["id"]),
        "dataset_repository": str(profile["dataset_repository"]),
        "expected_dataset_revision": expected_revision,
        "read_only": True,
        "acquisition_attempted": False,
        "ok": ok,
        "container_inventory": {
            "ok": container_inventory_ok,
            "containers_seen": len(containers),
            "error": container_result["stderr"] or None,
            "parse_errors": container_parse_errors,
        },
        "asset_issue_registry_error": asset_issue_registry_error,
        "tasks": tasks,
        "summary": summary,
        "missing_image_references": [
            image["reference"] for image in images if not image["present"]
        ],
        "unresolved_image_task_ids": [
            task["task_id"] for task in tasks if len(task["images"]) != 2
        ],
        "blocking_known_asset_issues": blocking_known_issues,
        "docker_commands": docker_commands,
    }


def ensure_local_task_exclude() -> None:
    """Keep fetched task data out of managed-source dirty-state checks."""

    exclude = current_paths().edge_root / ".git" / "info" / "exclude"
    if not exclude.is_file():
        return
    lines = exclude.read_text(encoding="utf-8").splitlines()
    if "tasks/" not in lines:
        exclude.write_text(
            "\n".join([*lines, "tasks/"]).rstrip() + "\n", encoding="utf-8"
        )


def provision(profile: dict[str, Any]) -> int:
    paths = current_paths()
    if not paths.sforge.is_file():
        raise FileNotFoundError(
            "SForge is not installed; run repro_env.py bootstrap --only edgebench"
        )
    ensure_local_task_exclude()
    env = dict(os.environ)
    configure_temp_environment(env)
    fetch = [
        str(paths.sforge),
        "--tasks-dir",
        str(paths.tasks_dir),
        "fetch-tasks",
        "--repo",
        str(profile["dataset_repository"]),
        "--revision",
        str(profile["dataset_revision"]),
    ]
    completed = subprocess.run(fetch, cwd=paths.root, env=env, check=False)
    if completed.returncode != 0:
        return completed.returncode
    pull = [
        str(paths.sforge),
        "--tasks-dir",
        str(paths.tasks_dir),
        "pull",
        "--task",
        *[str(task) for task in profile["task_ids"]],
        "--registry",
        str(profile["registry"]),
    ]
    return subprocess.run(pull, cwd=paths.root, env=env, check=False).returncode


class DoctorReport:
    """Ordered, machine-readable collection of environment checks."""

    def __init__(self, profile_id: str) -> None:
        self.profile_id = profile_id
        self.checks: list[dict[str, Any]] = []

    def add(self, name: str, passed: bool, **details: Any) -> None:
        self.checks.append({"name": name, "passed": bool(passed), **details})

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "checked_at": io.utc_now(),
            "profile": self.profile_id,
            "ok": all(check["passed"] for check in self.checks),
            "checks": self.checks,
        }


def _check_protocol(
    report: DoctorReport, profile: dict[str, Any]
) -> dict[str, Any] | None:
    try:
        official = load_official_codex_protocol()
        report.add(
            "protocol:official-source",
            True,
            path=official["source"],
            sha256=official["source_sha256"],
            task_count=len(official["tasks"]),
        )
        missing = sorted(set(profile["task_ids"]) - set(official["tasks"]))
        report.add(
            "protocol:task-coverage",
            not missing,
            profile_task_count=len(profile["task_ids"]),
            official_task_count=len(official["tasks"]),
            missing_tasks=missing,
        )
        return official
    except (OSError, ValueError, yaml.YAMLError) as exc:
        report.add(
            "protocol:official-source",
            False,
            path=io.portable_path(current_paths().official_codex_protocol_path),
            error=str(exc),
        )
        return None


def _check_checkouts_and_runtime(report: DoctorReport, profile: dict[str, Any]) -> None:
    paths = current_paths()
    expected_edge = io.upstream_entry("edgebench")["tracking_branch"]
    edge_branch = io.git_branch(paths.edge_root)
    edge_dirty = io.git_dirty(paths.edge_root)
    report.add(
        "checkout:edgebench",
        edge_branch == expected_edge and edge_dirty is False,
        expected_branch=expected_edge,
        actual_branch=edge_branch,
        actual_commit=io.git_head(paths.edge_root),
        dirty=edge_dirty,
    )
    if set(profile["methods"]) & GOAL_PLUS_METHODS:
        goal_source = resolve_goal_plus_source(methods=profile["methods"])
        report.add(
            "checkout:goal-plus",
            bool(goal_source["valid"]),
            source_kind=goal_source["source_kind"],
            source_path=goal_source["source_path"],
            checkout_root=goal_source["checkout_root"],
            expected_ref=goal_source["expected_ref"],
            expected_ref_commit=goal_source["expected_ref_commit"],
            actual_branch=goal_source["branch"],
            actual_commit=goal_source["commit"],
            dirty=goal_source["dirty"],
            missing_assets=goal_source["missing_assets"],
            missing_asset_alternatives=goal_source[
                "missing_asset_alternatives"
            ],
            codex_runtime_compatibility=goal_source["codex_runtime_compatibility"],
            pi_runtime_compatibility=goal_source["pi_runtime_compatibility"],
            error=goal_source["error"],
        )
    report.add(
        "entrypoint:sforge",
        paths.sforge.is_file(),
        path=".bench-env/venv/bin/sforge",
    )
    imports = (
        io.run_capture([str(paths.venv_python), "-c", "import fastapi, sforge"])
        if paths.venv_python.is_file()
        else {"returncode": 127, "stderr": "venv missing"}
    )
    report.add(
        "runtime:sforge-server-dependencies",
        imports["returncode"] == 0,
        stderr=imports["stderr"][-400:] or None,
    )
    report.add(
        "runtime:repository-local-temp", ensure_temp_root().is_dir(), path=".tmp"
    )


def _check_auth(
    report: DoctorReport, profile: dict[str, Any]
) -> tuple[str, dict[str, str | None], bool]:
    api_protocol = api_protocol_for_methods(profile["methods"])
    agents = {str(METHODS[method]["agent"]) for method in profile["methods"]}
    api_config = resolve_agent_api_config(protocol=api_protocol)
    auth_override = os.environ.get("SFORGE_CODEX_AUTH_FILE")
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    auth = (
        Path(auth_override).expanduser() if auth_override else codex_home / "auth.json"
    )
    pi_auth_status = resolve_pi_auth()
    pi_provider_bundle = (
        resolve_pi_provider_bundle(pi_provider_role_model_refs(profile))
        if api_protocol == "pi-provider"
        else None
    )
    pi_provider_status = (
        pi_provider_bundle["models"][0] if pi_provider_bundle is not None else None
    )
    api_key = api_config["api_key"]
    api_base_url = api_config["api_base_url"]
    needs_codex = methods_require_codex(profile["methods"])
    needs_pi = any(agent.startswith("pi") for agent in agents)
    needs_pi_oauth = needs_pi and api_protocol != "pi-provider"
    needs_claude = "claude-code" in agents
    pi_oauth_only = needs_pi_oauth and not (needs_codex or needs_claude)
    custom_base_without_key = (
        bool(api_base_url) and not bool(api_key) and not pi_oauth_only
    )
    auth_ready = (
        (not needs_codex or bool(api_key) or auth.is_file())
        and (not needs_pi_oauth or bool(pi_auth_status["valid"]))
        and (pi_provider_bundle is None or bool(pi_provider_bundle["valid"]))
        and (not needs_claude or bool(api_key))
        and not custom_base_without_key
    )
    report.add(
        "auth:agent",
        auth_ready,
        mode=(
            "pi-provider"
            if pi_provider_status is not None
            else (
                "pi-oauth"
                if pi_oauth_only
                else "api_key" if api_key else "host_login"
            )
        ),
        protocol=api_protocol,
        api_key_source=api_config["api_key_source"],
        api_base_url_source=api_config["api_base_url_source"],
        policy=(
            "Codex accepts API credentials or Codex auth; openai-codex Pi requires "
            "a Pi auth file; pi-provider uses the explicit provider/model registry"
        ),
    )
    if needs_codex:
        report.add(
            "auth:codex",
            bool(api_key) or auth.is_file(),
            mode="api_key" if api_key else "oauth",
            path=str(auth),
        )
    host_preflight_ready = True
    if needs_pi:
        if pi_provider_status is not None:
            report.add(
                "auth:pi",
                bool(pi_provider_bundle["valid"]),
                mode="provider-api",
                provider=pi_provider_status["provider"],
                model=pi_provider_status["model"],
                models_path=pi_provider_status["models_path"],
                model_registered=pi_provider_status["model_registered"],
                credential_mode=pi_provider_status["credential_mode"],
                credential_env=pi_provider_status["credential_env"],
                role_models=[
                    {
                        "provider": status["provider"],
                        "model": status["model"],
                        "model_registered": status["model_registered"],
                        "credential_env": status["credential_env"],
                        "valid": status["valid"],
                        "error": status["error"],
                    }
                    for status in pi_provider_bundle["models"]
                ],
                error=pi_provider_bundle["error"],
            )
            preflight = pi_provider_host_preflight(profile)
            host_preflight_ready = bool(preflight["passed"])
            if preflight["probes"]:
                for probe in preflight["probes"]:
                    roles = probe["roles"]
                    report.add(
                        "auth:pi-provider-host:" + "+".join(roles),
                        bool(probe["passed"]),
                        roles=roles,
                        model_ref=probe["model_ref"],
                        reasoning_effort=probe["reasoning_effort"],
                        pi_version=probe.get("pi_version"),
                        expected_pi_version=probe.get("expected_pi_version"),
                        credential_envs=probe.get("credential_envs", []),
                        wire_apis=probe.get("wire_apis", []),
                        tool_roundtrip=probe.get("tool_roundtrip", False),
                        thinking_observed=probe.get("thinking_observed", False),
                        error=probe.get("error"),
                    )
            else:
                report.add(
                    "auth:pi-provider-host",
                    False,
                    error=preflight["error"] or "host Pi provider preflight failed",
                )
        else:
            report.add(
                "auth:pi",
                bool(pi_auth_status["valid"]),
                mode="openai-codex",
                path=str(pi_auth_status["path"]),
            )
    if needs_codex:
        codex_preflight = codex_host_provider_probe(profile)
        host_preflight_ready = host_preflight_ready and bool(
            codex_preflight["passed"]
        )
        contract = codex_preflight.get("contract") or {}
        report.add(
            "auth:codex-host",
            bool(codex_preflight["passed"]),
            auth_mode=contract.get("auth_mode"),
            provider=contract.get("provider"),
            model=contract.get("model"),
            reasoning_effort=contract.get("reasoning_effort"),
            api_key_source=contract.get("api_key_source"),
            api_base_url_source=contract.get("api_base_url_source"),
            api_endpoint=contract.get("api_endpoint"),
            provider_config_sha256=contract.get("provider_config_sha256"),
            codex_version=codex_preflight.get("codex_version"),
            expected_codex_version=codex_preflight.get("expected_codex_version"),
            tool_roundtrip=codex_preflight.get("tool_roundtrip", False),
            mcp_tool_roundtrip=codex_preflight.get(
                "mcp_tool_roundtrip", False
            ),
            mcp_tools=codex_preflight.get("mcp_tools", []),
            goal_plus_mcp_required=codex_preflight.get(
                "goal_plus_mcp_required", False
            ),
            goal_plus_source=codex_preflight.get("goal_plus_source"),
            turn_completed=codex_preflight.get("turn_completed", False),
            error=codex_preflight.get("error"),
        )
    if api_key and api_base_url and needs_claude:
        api_probe = authenticated_api_probe(
            str(api_base_url),
            str(api_key),
            protocol=api_protocol,
            model=str(profile["model"]),
            thinking=profile.get("thinking"),
            reasoning_effort=profile.get("reasoning_effort"),
        )
        report.add(
            "auth:agent-api-host",
            bool(api_probe["passed"]),
            base_url=str(api_base_url),
            status=api_probe.get("status"),
            error=api_probe.get("error"),
        )
    if api_base_url and (needs_codex or needs_claude):
        try:
            loopback = loopback_api_target(str(api_base_url)) is not None
        except ValueError as exc:
            loopback = False
            report.add("auth:agent-api-url", False, error=str(exc))
        if loopback:
            report.add(
                "runtime:rootless-loopback-bridge",
                Path("/usr/bin/systemd-socket-activate").is_file()
                and Path("/lib/systemd/systemd-socket-proxyd").is_file(),
                mechanism="systemd-socket-proxyd",
            )
    if needs_codex:
        codex_runtime = resolve_codex_runtime_archive()
        report.add(
            "runtime:codex-host-cache",
            bool(codex_runtime["passed"]),
            path=codex_runtime["archive"],
            source=codex_runtime["source"],
            expected_version=codex_runtime["expected_version"],
            actual_version=codex_runtime["actual_version"],
            size=codex_runtime["size"],
            error=codex_runtime["error"],
        )
    return api_protocol, api_config, host_preflight_ready


def _docker_details(report: DoctorReport) -> dict[str, Any]:
    docker_info = io.run_capture(["docker", "info", "--format", "{{json .}}"])
    details: dict[str, Any] = {}
    if docker_info["returncode"] == 0:
        try:
            details = json.loads(docker_info["stdout"])
        except json.JSONDecodeError:
            details = {}
    architecture = str(details.get("Architecture") or "").lower()
    report.add(
        "docker:engine",
        docker_info["returncode"] == 0 and bool(details),
        architecture=architecture or None,
        stderr=docker_info["stderr"][-400:] or None,
    )
    report.add(
        "docker:linux-amd64",
        architecture in {"amd64", "x86_64"},
        required="linux/amd64",
        actual=architecture or None,
    )
    return details


def _check_tasks_and_resources(
    report: DoctorReport,
    profile: dict[str, Any],
    official_protocol: dict[str, Any] | None,
    docker_details: dict[str, Any],
) -> None:
    paths = current_paths()
    rust_archive: dict[str, Any] | None = None
    resource_probe_image: str | None = None
    effective_protocols: list[dict[str, Any]] = []
    offline_task_ids: list[str] = []
    for task_id in profile["task_ids"]:
        task_path = paths.tasks_dir / f"{task_id}.json"
        report.add(
            f"task:{task_id}", task_path.is_file(), path=io.portable_path(task_path)
        )
        actual_revision = dataset_revision(task_id)
        report.add(
            f"dataset-revision:{task_id}",
            actual_revision == profile["dataset_revision"],
            expected=profile["dataset_revision"],
            actual=actual_revision,
        )
        if not task_path.is_file():
            continue
        config = task_config(task_id)
        if official_protocol is not None:
            try:
                effective = profile_task_protocol(
                    profile, official_protocol, task_id, config
                )
                effective_protocols.append(effective)
                if effective["internet"] is False:
                    offline_task_ids.append(task_id)
                report.add(
                    f"protocol-effective:{task_id}",
                    True,
                    internet=effective["internet"],
                    internet_source=(
                        f"profiles/{profile['id']}.protocol_overrides.internet"
                        if "internet" in profile.get("protocol_overrides", {})
                        else f"tasks/{task_id}.json"
                    ),
                    submission_cooldown=effective["submission_cooldown"],
                )
            except ValueError as exc:
                report.add(f"protocol-effective:{task_id}", False, error=str(exc))
        if config.get("base_image") == "rust" and rust_archive is None:
            rust_archive = rust_runtime_archive_status()
            report.add(
                "runtime:rust-host-cache",
                bool(rust_archive["passed"]),
                **{
                    key: value for key, value in rust_archive.items() if key != "passed"
                },
            )
        for image_index, image in enumerate(task_images(task_id)):
            inspected = io.run_capture(["docker", "image", "inspect", image])
            report.add(f"image:{image}", inspected["returncode"] == 0, image=image)
            if (
                image_index == 0
                and inspected["returncode"] == 0
                and resource_probe_image is None
            ):
                resource_probe_image = image
            if config.get("base_image") == "rust" and inspected["returncode"] == 0:
                assert rust_archive is not None
                version = str(rust_archive.get("version") or "")
                probe = (
                    rust_image_runtime_probe(image, version)
                    if version
                    else {
                        "returncode": 1,
                        "stdout": "",
                        "stderr": "Rust runtime manifest has no version",
                    }
                )
                native = probe["returncode"] == 0
                report.add(
                    f"runtime:rust:{image}",
                    native or bool(rust_archive["passed"]),
                    image=image,
                    expected_version=version,
                    native=native,
                    fallback_archive_ready=bool(rust_archive["passed"]),
                    stdout=probe["stdout"][-400:] or None,
                    stderr=probe["stderr"][-400:] or None,
                )

    if effective_protocols:
        work_cpu_limit = max(
            max(int(item["work_cpu_limit"]), int(item["judge_cpu_limit"]))
            for item in effective_protocols
        )
        work_mem_limit = max(
            (
                str(limit)
                for item in effective_protocols
                for limit in (item["work_mem_limit"], item["judge_mem_limit"])
            ),
            key=_docker_memory_bytes,
        )
        resource_probe = (
            docker_resource_limit_probe(
                resource_probe_image,
                cpu_limit=work_cpu_limit,
                mem_limit=work_mem_limit,
            )
            if resource_probe_image
            else {
                "passed": False,
                "error": "no prepared Work image is available for the resource probe",
            }
        )
        daemon_cpu_support = docker_details.get("CpuCfsQuota")
        daemon_memory_support = docker_details.get("MemoryLimit")
        report.add(
            "docker:official-resource-limits",
            daemon_cpu_support is not False
            and daemon_memory_support is not False
            and bool(resource_probe["passed"]),
            daemon_cpu_cfs_quota=daemon_cpu_support,
            daemon_memory_limit=daemon_memory_support,
            **{key: value for key, value in resource_probe.items() if key != "passed"},
        )
    if offline_task_ids:
        isolation_probe = sforge_iptables_permission_probe()
        report.add(
            "network:offline-task-isolation",
            bool(isolation_probe["passed"]),
            mechanism=(
                "SForge host iptables allowlist; macOS enters the Docker VM "
                "through a local-only privileged nsenter helper"
            ),
            offline_task_count=len(offline_task_ids),
            sample_task=offline_task_ids[0],
            stderr=isolation_probe.get("stderr"),
        )


def _check_api_only_network(
    report: DoctorReport,
    profile: dict[str, Any],
    official_protocol: dict[str, Any] | None,
) -> None:
    if official_protocol is None:
        report.add(
            "network:api-only-policy",
            False,
            policy="api-only",
            error="official protocol is unavailable",
        )
        return
    try:
        contract = require_api_only_network(profile, official_protocol)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        report.add(
            "network:api-only-policy",
            False,
            policy="api-only",
            allowed_classes=["judge", "llm-api"],
            error=str(exc),
        )
        return
    report.add(
        "network:api-only-policy",
        True,
        policy=contract["policy"],
        allowed_classes=["judge", "llm-api"],
        blocked_classes=["task-internet", "package-registry", "public-proxy"],
        tasks=contract["tasks"],
        judge=contract["judge"],
        api_endpoints=[
            {
                key: endpoint[key]
                for key in (
                    "method",
                    "role",
                    "model",
                    "source",
                    "scheme",
                    "host",
                    "port",
                )
            }
            for endpoint in contract["api_endpoints"]
        ],
    )


def doctor_payload(profile: dict[str, Any]) -> dict[str, Any]:
    report = DoctorReport(str(profile["id"]))
    official_protocol = _check_protocol(report, profile)
    _check_api_only_network(report, profile, official_protocol)
    _check_checkouts_and_runtime(report, profile)
    _, _, host_api_ready = _check_auth(report, profile)
    source_ready = all(
        check["passed"]
        for check in report.checks
        if check["name"] in {"checkout:edgebench", "checkout:goal-plus"}
    )
    if not host_api_ready or not source_ready:
        report.add(
            "docker:preflight",
            False,
            skipped=True,
            reason=(
                "host agent capability probe failed"
                if not host_api_ready
                else "source checkout validation failed"
            ),
        )
        return report.payload()
    docker_details = _docker_details(report)
    _check_tasks_and_resources(report, profile, official_protocol, docker_details)
    return report.payload()


def doctor(
    profile: dict[str, Any],
    *,
    output: Path | None = None,
    local_assets_only: bool = False,
    allow_missing_local_assets: bool = False,
) -> int:
    payload = (
        local_asset_inventory(profile) if local_assets_only else doctor_payload(profile)
    )
    if output:
        io.write_json(output, payload)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return (
        0 if payload["ok"] or (local_assets_only and allow_missing_local_assets) else 1
    )
