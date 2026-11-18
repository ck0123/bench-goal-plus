"""Run-local OpenCode (v2.2.7) host plumbing for Goal Plus methods.

OpenCode runs Goal Plus through a native plugin: the Main session is a single
headless ``opencode run --format json`` process, and Goal Plus itself spawns
every candidate worker as a private ``opencode serve --stdio`` service inside
its own outer sandbox. The bench therefore only has to prepare, per run:

* an isolated OpenCode config home (config + XDG data/state/cache) holding
  ``opencode.json`` with one OpenAI-compatible provider, the Goal Plus plugin
  registered by ``install.sh --opencode``, and the ``gp-bind`` adapter plugin
  that replaces the TUI-only ``/goal-plus`` command for headless sessions;
* a ``goal-plus.opencode-profile.v1`` JSON pinning the exact native binary,
  provider catalog and environment-only credential reference for candidates;
* the Main ``opencode run`` command plus its environment.

Token accounting reads the same ``--format json`` event stream the controller
already captures as ``events.jsonl``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

OPENCODE_CONFIG_FILE = "opencode.json"
OPENCODE_PROFILE_SCHEMA = "goal-plus.opencode-profile.v1"
OPENCODE_PROVIDER_PACKAGE = "@opencode/ai/providers/openai-compatible"
OPENCODE_MAIN_PROVIDER_PACKAGE = "@ai-sdk/openai-compatible"
OPENCODE_PROFILE_ENV = "GOAL_PLUS_OPENCODE_PROFILE"
OPENCODE_ROLE_ENV = "GOAL_PLUS_OPENCODE_ROLE"
OPENCODE_API_KEY_ENV = "GOAL_PLUS_OPENCODE_API_KEY"
_OPENCODE_VERSION_PATTERN = re.compile(r"v?(\d+\.\d+\.\d+)")


def opencode_home(run_dir: Path) -> Path:
    """Isolated OpenCode config directory for one run (never the user's)."""

    return run_dir / "opencode-home"


def opencode_environment(
    config_dir: Path,
    *,
    workdir: Path,
    role: str = "main",
    profile_path: Path | None = None,
) -> dict[str, str]:
    """Environment that keeps every OpenCode process inside the run home."""

    environment = {
        "OPENCODE_CONFIG_DIR": str(config_dir),
        "XDG_CONFIG_HOME": str(config_dir / "xdg-config"),
        "XDG_DATA_HOME": str(config_dir / "xdg-data"),
        "XDG_STATE_HOME": str(config_dir / "xdg-state"),
        "XDG_CACHE_HOME": str(config_dir / "xdg-cache"),
        OPENCODE_ROLE_ENV: role,
        "OPENCODE_CONFIG_PROJECT_DISABLE": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        # Do not let an enclosing bench checkout become the OpenCode project.
        # A task-local .git remains valid; discovery may not escape the task.
        "GIT_CEILING_DIRECTORIES": str(workdir.parent),
        # opencode >= 2.2 renders ANSI in --version output that breaks the
        # Goal Plus version probe unless colors are disabled.
        "NO_COLOR": "1",
    }
    if profile_path is not None:
        environment[OPENCODE_PROFILE_ENV] = str(profile_path)
    return environment


def supported_opencode_versions(source: Path) -> tuple[str, ...]:
    """Read the Goal Plus OpenCode version allowlist from its source tree."""

    versions_file = source / "assets" / "opencode" / "versions.json"
    versions = tuple(json.loads(versions_file.read_text()))
    if not versions or any(not isinstance(version, str) or not version for version in versions):
        raise ValueError(f"Invalid OpenCode version allowlist: {versions_file}")
    return versions


def check_opencode_version(binary: str, supported: tuple[str, ...]) -> str:
    """Return the normalized ``X.Y.Z`` version or raise for unsupported bins."""

    result = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"OpenCode version probe failed ({binary} --version): "
            f"{(result.stderr or result.stdout).strip()[:200]}"
        )
    match = _OPENCODE_VERSION_PATTERN.search(result.stdout or "")
    if match is None:
        raise RuntimeError(
            f"OpenCode version output is unparseable: {(result.stdout or '').strip()[:120]}"
        )
    version = match.group(1)
    if version not in supported:
        raise RuntimeError(
            f"OpenCode {version} is not supported by the installed Goal Plus "
            f"(supported: {', '.join(supported)})"
        )
    return version


def write_opencode_config(
    config_dir: Path,
    *,
    provider_id: str,
    api_base: str,
    api_key_env: str,
    model: str,
    goal_plus_assets: Path,
    context_limit: int = 128_000,
    output_limit: int = 32_768,
) -> Path:
    """Write ``opencode.json`` with one OpenAI-compatible provider and plugins."""

    config = {
        "$schema": "https://opencode.ai/config.json",
        "snapshot": False,
        "provider": {
            provider_id: {
                "npm": OPENCODE_MAIN_PROVIDER_PACKAGE,
                "name": f"Bench {provider_id}",
                "options": {
                    "baseURL": api_base,
                    "apiKey": "{env:" + api_key_env + "}",
                },
                "models": {
                    model: {
                        "name": model,
                        "limit": {"context": context_limit, "output": output_limit},
                    }
                },
            }
        },
        "model": f"{provider_id}/{model}",
        "permission": {
            "external_directory": "deny",
            "task": "deny",
            "webfetch": "deny",
            "websearch": "deny",
        },
        "plugins": [
            str(goal_plus_assets),
            str(config_dir / "plugins" / "gp-bind"),
        ],
    }
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / OPENCODE_CONFIG_FILE
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path


def write_opencode_profile(
    profile_path: Path,
    *,
    version: str,
    executable: Path,
    provider_id: str,
    model: str,
    api_base: str,
    api_key_env: str,
    variant: str | None = None,
) -> Path:
    """Write an env-only Goal Plus profile; never persist the provider key."""

    model_ref = f"{provider_id}/{model}" + (f"#{variant}" if variant else "")
    profile = {
        "schema": OPENCODE_PROFILE_SCHEMA,
        "version": version,
        "model": model_ref,
        "executable": str(executable),
        "providers": [
            {
                "id": provider_id,
                "package": OPENCODE_PROVIDER_PACKAGE,
                "settings": {
                    "baseURL": api_base,
                    "apiKey": "{env:" + api_key_env + "}",
                },
            }
        ],
        "models": [{"id": model, "providerID": provider_id}],
        "credentials": {},
    }
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(json.dumps(profile, indent=2) + "\n")
    os.chmod(profile_path, 0o600)
    return profile_path


def opencode_main_command(
    binary: str,
    *,
    qualified_model: str,
    title: str,
    variant: str | None = None,
) -> list[str]:
    """Headless Main command for OpenCode 2.2.x; the prompt is appended by the caller."""

    model = qualified_model + (f"#{variant}" if variant else "")
    return [
        binary,
        "run",
        "--format",
        "json",
        "--model",
        model,
        "--title",
        title,
        "--auto",
    ]


def opencode_bind_preamble() -> str:
    """Headless binding instructions replacing the TUI ``/goal-plus`` command.

    The Goal Plus plugin hides ``goal_plus_*`` tools from Main sessions until
    the session metadata carries ``goal_plus.goal_id``. The bundled ``gp-bind``
    plugin exposes that binding as a tool call, so the rendered goal prompt
    must instruct the model to bind on its first turn.
    """

    return (
        "The goal_plus_* tools are hidden until this session is bound to a goal. "
        "FIRST call the tool gp_bind with goal_text set to the raw goal below "
        "(everything after the /goal-plus configuration prefix — do not include "
        "the prefix itself). After gp_bind succeeds, read goal_plus_status and "
        "execute the goal with the Search configuration given in the prefix.\n\n"
    )


def parse_opencode_events(path: Path) -> dict[str, Any]:
    """Summarize ``opencode run --format json`` output like ``parse_pi_events``.

    Usage is accumulated from every event that carries OpenAI-compatible
    ``usage`` counters (``turn.completed`` style events) or ``part.tokens``
    payloads, matching the two shapes OpenCode 2.2.x emits.
    """

    usage = {
        "assistant_messages": 0,
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "cost_total": 0.0,
    }
    event_count = 0
    error_events = 0
    models: set[str] = set()
    providers: set[str] = set()
    session_ids: set[str] = set()

    def _int(value: Any) -> int:
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return 0

    def _float(value: Any) -> float:
        try:
            return max(float(value), 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _count_usage(usage_payload: Mapping[str, Any]) -> bool:
        if not any(
            key in usage_payload
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cached_input_tokens",
                "cache_creation_input_tokens",
            )
        ):
            return False
        usage["input"] += _int(usage_payload.get("input_tokens"))
        usage["output"] += _int(usage_payload.get("output_tokens"))
        usage["cache_read"] += _int(
            usage_payload.get(
                "cache_read_input_tokens", usage_payload.get("cached_input_tokens")
            )
        )
        usage["cache_write"] += _int(usage_payload.get("cache_creation_input_tokens"))
        model = usage_payload.get("model")
        if isinstance(model, str) and model:
            models.add(model)
        return True

    lines = path.read_text().splitlines() if path.is_file() else []
    for line in lines:
        if not line.strip():
            continue
        event_count += 1
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type", "")).lower()
        if event_type in {"error", "turn.failed"}:
            error_events += 1
        session_id = event.get("sessionID") or event.get("session_id")
        if isinstance(session_id, str) and session_id:
            session_ids.add(session_id)
        counted = False
        usage_payload = event.get("usage")
        if isinstance(usage_payload, dict):
            counted = _count_usage(usage_payload)
        if event_type in {"assistant", "agent_message", "message_end"}:
            usage["assistant_messages"] += 1
        part = event.get("part") if isinstance(event.get("part"), dict) else None
        if isinstance(part, Mapping):
            model = part.get("model") or event.get("model")
            if isinstance(model, str) and model:
                models.add(model)
            provider = part.get("provider") or event.get("provider")
            if isinstance(provider, str) and provider:
                providers.add(provider)
            tokens = part.get("tokens")
            if isinstance(tokens, Mapping) and not counted:
                usage["input"] += _int(tokens.get("input"))
                usage["output"] += _int(tokens.get("output"))
                cache = tokens.get("cache")
                if isinstance(cache, Mapping):
                    usage["cache_read"] += _int(cache.get("read"))
                    usage["cache_write"] += _int(cache.get("write"))
            usage["cost_total"] += _float(part.get("cost"))
    return {
        "usage": usage,
        "models": sorted(models),
        "providers": sorted(providers),
        "session_ids": sorted(session_ids),
        "events": event_count,
        "error_events": error_events,
    }


GP_BIND_PACKAGE_JSON = json.dumps(
    {"name": "gp-bind", "private": True, "type": "module",
     "exports": {"./server": "./server.ts"}},
    indent=2,
) + "\n"


GP_BIND_SERVER_TS = """// gp-bind: headless binding adapter for goal-plus on OpenCode.
// The goal-plus plugin hides goal_plus_* tools from Main sessions until the
// session metadata carries goal_plus.goal_id, which is normally set by the
// TUI-only /goal-plus command. This adapter exposes a gp_bind tool that
// creates the goal through the same managed host and writes that metadata,
// so `opencode run` (headless) sessions can bind on their first turn.
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { readFileSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";

const here = dirname(fileURLToPath(import.meta.url));
const goalPlusPlugin = realpathSync(resolve(here, "..", "goal-plus"));
const packageRoot = resolve(goalPlusPlugin, "..", "..");
const receipt = JSON.parse(readFileSync(resolve(packageRoot, ".goal-plus-runtime.json"), "utf8"));
if (receipt.schema !== "goal-plus.managed-runtime.v1" || !isAbsolute(receipt.python) || !receipt.build)
  throw new Error("gp-bind: managed goal-plus runtime receipt missing (run install.sh --opencode)");

const ROLE = process.env.GOAL_PLUS_OPENCODE_ROLE || "main";
const PROFILE = process.env.GOAL_PLUS_OPENCODE_PROFILE;
const textResult = (value: unknown) => ({ content: [{ type: "text", text: JSON.stringify(value) }] });

class Host {
  private child: ReturnType<typeof spawn>;
  private pending = new Map<string, { resolve: (v: any) => void; reject: (e: Error) => void }>();
  constructor(readonly sessionID: string, readonly cwd: string) {
    const root = resolve(cwd, process.env.GOAL_PLUS_ROOT || ".gp");
    const env: Record<string, string> = { ...process.env as Record<string, string>,
      GOAL_PLUS_INTERNAL_AGENT_HARNESS: "opencode", GOAL_PLUS_OPENCODE_ROLE: ROLE };
    if (PROFILE) env.GOAL_PLUS_OPENCODE_PROFILE = PROFILE;
    this.child = spawn(receipt.python, ["-I", "-m", "goal_plus.installation", "--expected-build", receipt.build,
      "--module", "goal_plus.opencode_host", "--root", root, "--session", sessionID],
      { cwd, stdio: ["pipe", "pipe", "pipe"], env });
    this.child.stderr!.on("data", (d: any) => process.stderr.write(d));
    const fail = () => { for (const p of this.pending.values()) p.reject(new Error("gp-bind host closed")); this.pending.clear(); };
    this.child.on("error", fail); this.child.on("exit", fail);
    createInterface({ input: this.child.stdout! }).on("line", (line: string) => {
      let m: any; try { m = JSON.parse(line); } catch { fail(); return; }
      const p = this.pending.get(m.id); if (!p) return;
      this.pending.delete(m.id);
      m.error ? p.reject(new Error(m.error)) : p.resolve(m.result);
    });
  }
  call(operation: string, args: any = {}): Promise<any> {
    const id = randomUUID();
    return new Promise((res, rej) => {
      this.pending.set(id, { resolve: res, reject: rej });
      this.child.stdin!.write(JSON.stringify({ id, operation, arguments: args }) + "\\n", (e: any) => {
        if (e) { this.pending.delete(id); rej(e); }
      });
    });
  }
  async close() { await new Promise<void>((r) => { const t = setTimeout(() => this.child.kill("SIGTERM"), 10000);
    this.child.once("exit", () => { clearTimeout(t); r(); }); this.child.stdin!.end(); }); }
}

export default {
  id: "gp-bind",
  async setup(ctx: any) {
    if (ROLE !== "main") return; // candidates/annotation never bind
    const binders = new Map<string, Host>();
    await ctx.tool.transform((tools: any) => {
      tools.add({
        name: "gp_bind",
        description: "Bind this session to a new Goal Plus task (headless replacement for /goal-plus). "
          + "Call this FIRST with the raw goal text when instructed, then follow goal_plus_status.",
        options: { codemode: false },
        input: { type: "object", properties: { goal_text: { type: "string", description: "The raw goal prompt (without any /goal-plus prefix)" } },
          required: ["goal_text"], additionalProperties: false },
        execute: async (input: any, call: any) => {
          const sid = call.sessionID;
          let host = binders.get(sid);
          if (!host) { host = new Host(sid, ctx.location.directory); binders.set(sid, host); }
          const result = await host.call("command", { text: input.goal_text });
          const goalId = result?.goal?.goal_plus_id;
          const info = await ctx.session.get({ sessionID: sid });
          await ctx.session.update({ sessionID: sid,
            metadata: { ...info.metadata, goal_plus: { goal_id: goalId } } });
          return textResult({ bound: true, goal_plus_id: goalId,
            next: "goal_plus_* tools are now available; read goal_plus_status and execute the goal." });
        },
      });
    });
  },
};
"""


def deploy_gp_bind_plugin(config_dir: Path) -> Path:
    """Materialize the gp-bind adapter plugin inside the isolated config dir."""

    plugin_dir = config_dir / "plugins" / "gp-bind"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "package.json").write_text(GP_BIND_PACKAGE_JSON)
    (plugin_dir / "server.ts").write_text(GP_BIND_SERVER_TS)
    return plugin_dir
