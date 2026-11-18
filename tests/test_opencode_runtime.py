from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from bench_goal_plus.goal_plus_command import (
    goal_plus_command_config,
    goal_plus_entrypoint,
    render_goal_plus_command,
)
from bench_goal_plus.goal_plus_evidence import session_agent_harness
from experiments.benchmark_compare.experiment import _goal_plus_agent_configuration
from bench_goal_plus.opencode_runtime import (
    OPENCODE_API_KEY_ENV,
    OPENCODE_PROFILE_SCHEMA,
    check_opencode_version,
    deploy_gp_bind_plugin,
    opencode_bind_preamble,
    opencode_environment,
    opencode_main_command,
    parse_opencode_events,
    supported_opencode_versions,
    write_opencode_config,
    write_opencode_profile,
)


class OpenCodeHarnessCommandTests(unittest.TestCase):
    def test_entrypoint_matches_pi_tui_command(self) -> None:
        self.assertEqual(goal_plus_entrypoint("opencode"), "/goal-plus")

    def test_rendered_command_uses_opencode_workers(self) -> None:
        command = render_goal_plus_command(
            "opencode",
            max_parallel=2,
            strategy="agent_guided",
            worker_model="deepseek/DeepSeek-V4.1-Flash-1017",
        )
        self.assertTrue(command.startswith("/goal-plus "))
        self.assertIn("workers=deepseek/DeepSeek-V4.1-Flash-1017*2", command)
        self.assertIn("spawn_mode=fresh", command)
        config = goal_plus_command_config(
            max_parallel=2,
            strategy="agent_guided",
            worker_model="deepseek/DeepSeek-V4.1-Flash-1017",
        )
        self.assertEqual(config["spawn_mode"], "fresh")

    def test_execute_and_prepare_share_opencode_worker_configuration(self) -> None:
        self.assertEqual(
            _goal_plus_agent_configuration(
                "goal-plus-opencode", "deepseek", "DeepSeek-V4.1-Flash-1017"
            ),
            ("opencode", "deepseek/DeepSeek-V4.1-Flash-1017"),
        )
        self.assertEqual(
            _goal_plus_agent_configuration("goal-plus-pi", "deepseek", "model"),
            ("pi", "deepseek/model"),
        )
        self.assertEqual(
            _goal_plus_agent_configuration("goal-plus-codex", "unused", "model"),
            ("codex", "model"),
        )


class OpenCodeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_supported_versions_reads_allowlist(self) -> None:
        source = self.tmp / "assets" / "opencode"
        source.mkdir(parents=True)
        (source / "versions.json").write_text(json.dumps(["2.2.7"]))
        self.assertEqual(supported_opencode_versions(self.tmp), ("2.2.7",))

    def test_supported_versions_requires_goal_plus_allowlist(self) -> None:
        with self.assertRaises(FileNotFoundError):
            supported_opencode_versions(self.tmp)

    def test_check_version_rejects_unsupported(self) -> None:
        class FakeResult:
            returncode = 0
            stdout = "opencode 1.19.5\n"

        import subprocess as sp

        original = sp.run

        def fake_run(command, *args, **kwargs):  # noqa: ANN001, ANN002
            if "--version" in command:
                return FakeResult()
            return original(command, *args, **kwargs)

        sp.run = fake_run
        try:
            with self.assertRaisesRegex(RuntimeError, "not supported"):
                check_opencode_version("opencode", ("2.2.7",))
        finally:
            sp.run = original

    def test_write_opencode_config_shape(self) -> None:
        config_dir = self.tmp / "opencode-home"
        path = write_opencode_config(
            config_dir,
            provider_id="deepseek",
            api_base="http://127.0.0.1:20981/v1",
            api_key_env=OPENCODE_API_KEY_ENV,
            model="DeepSeek-V4.1-Flash-1017",
            goal_plus_assets=self.tmp / "assets" / "opencode",
        )
        config = json.loads(path.read_text())
        provider = config["provider"]["deepseek"]
        self.assertEqual(
            provider["options"]["baseURL"], "http://127.0.0.1:20981/v1"
        )
        self.assertEqual(
            provider["options"]["apiKey"], "{env:GOAL_PLUS_OPENCODE_API_KEY}"
        )
        self.assertEqual(config["model"], "deepseek/DeepSeek-V4.1-Flash-1017")
        self.assertEqual(
            provider["models"]["DeepSeek-V4.1-Flash-1017"]["limit"]["context"],
            128_000,
        )
        self.assertEqual(config["permission"]["websearch"], "deny")
        self.assertEqual(len(config["plugins"]), 2)
        self.assertTrue(str(config_dir / "plugins" / "gp-bind") in config["plugins"])

    def test_write_opencode_profile_is_private_and_pinned(self) -> None:
        profile_path = self.tmp / "controller-runtime" / "opencode-profile.json"
        write_opencode_profile(
            profile_path,
            version="2.2.7",
            executable=Path("/opt/opencode-2.2.7/bin/opencode"),
            provider_id="deepseek",
            model="DeepSeek-V4.1-Flash-1017",
            api_base="http://127.0.0.1:20981/v1",
            api_key_env=OPENCODE_API_KEY_ENV,
            variant="high",
        )
        profile = json.loads(profile_path.read_text())
        self.assertEqual(profile["schema"], OPENCODE_PROFILE_SCHEMA)
        self.assertEqual(profile["version"], "2.2.7")
        self.assertEqual(profile["model"], "deepseek/DeepSeek-V4.1-Flash-1017#high")
        self.assertEqual(
            profile["executable"], "/opt/opencode-2.2.7/bin/opencode"
        )
        self.assertEqual(
            profile["providers"][0]["settings"]["baseURL"],
            "http://127.0.0.1:20981/v1",
        )
        self.assertEqual(
            profile["providers"][0]["settings"]["apiKey"],
            "{env:GOAL_PLUS_OPENCODE_API_KEY}",
        )
        self.assertEqual(profile["credentials"], {})
        self.assertNotIn("sk-test", profile_path.read_text())
        self.assertEqual(os.stat(profile_path).st_mode & 0o777, 0o600)

    def test_main_command_is_headless_run(self) -> None:
        command = opencode_main_command(
            "opencode",
            qualified_model="deepseek/DeepSeek-V4.1-Flash-1017",
            title="bench-detect-civetweb",
            variant="high",
        )
        self.assertEqual(
            command,
            [
                "opencode", "run", "--format", "json",
                "--model", "deepseek/DeepSeek-V4.1-Flash-1017#high",
                "--title", "bench-detect-civetweb", "--auto",
            ],
        )

    def test_environment_isolates_all_opencode_state(self) -> None:
        environment = opencode_environment(
            Path("/run/opencode-home"),
            workdir=Path("/run/workspace"),
            profile_path=Path("/run/profile.json"),
        )
        self.assertEqual(
            environment["OPENCODE_CONFIG_DIR"], "/run/opencode-home"
        )
        for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                    "XDG_CACHE_HOME"):
            self.assertTrue(environment[key].startswith("/run/opencode-home/"))
        self.assertEqual(environment["GOAL_PLUS_OPENCODE_ROLE"], "main")
        self.assertEqual(environment["GOAL_PLUS_OPENCODE_PROFILE"], "/run/profile.json")
        self.assertEqual(environment["NO_COLOR"], "1")
        self.assertEqual(environment["OPENCODE_CONFIG_PROJECT_DISABLE"], "1")
        self.assertEqual(environment["OPENCODE_DISABLE_MODELS_FETCH"], "1")
        self.assertEqual(environment["GIT_CEILING_DIRECTORIES"], "/run")

    def test_bind_preamble_instructs_gp_bind(self) -> None:
        preamble = opencode_bind_preamble()
        self.assertIn("gp_bind", preamble)
        self.assertIn("goal_text", preamble)

    def test_parse_events_accumulates_usage_shapes(self) -> None:
        events = self.tmp / "events.jsonl"
        events.write_text(
            "\n".join(
                [
                    json.dumps({"type": "session.started", "sessionID": "ses_a"}),
                    json.dumps({"type": "turn.completed", "usage": {
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "cache_read_input_tokens": 900,
                        "cache_creation_input_tokens": 50,
                        "model": "deepseek/DeepSeek-V4.1-Flash-1017",
                    }}),
                    json.dumps({"type": "item.completed", "part": {
                        "type": "text", "model": "x", "cost": 0.25,
                        "tokens": {"input": 1, "output": 2,
                                   "cache": {"read": 3, "write": 4}},
                    }}),
                    json.dumps({"type": "error", "message": "boom"}),
                    "not-json",
                ]
            )
        )
        summary = parse_opencode_events(events)
        usage = summary["usage"]
        self.assertEqual(usage["input"], 101)
        self.assertEqual(usage["output"], 12)
        self.assertEqual(usage["cache_read"], 903)
        self.assertEqual(usage["cache_write"], 54)
        self.assertEqual(usage["cost_total"], 0.25)
        self.assertEqual(summary["session_ids"], ["ses_a"])
        self.assertEqual(summary["error_events"], 1)
        # The unparseable line still counts as one observed event line.
        self.assertEqual(summary["events"], 5)

    def test_parse_events_handles_missing_file(self) -> None:
        summary = parse_opencode_events(self.tmp / "absent.jsonl")
        self.assertEqual(summary["usage"]["input"], 0)
        self.assertEqual(summary["events"], 0)

    def test_deploy_gp_bind_plugin_materializes_assets(self) -> None:
        plugin_dir = deploy_gp_bind_plugin(self.tmp / "opencode-home")
        package = json.loads((plugin_dir / "package.json").read_text())
        self.assertEqual(package["name"], "gp-bind")
        server = (plugin_dir / "server.ts").read_text()
        self.assertIn("gp_bind", server)
        self.assertIn("goal_plus.opencode_host", server)


class OpenCodeEvidenceTests(unittest.TestCase):
    def test_session_agent_harness_accepts_opencode(self) -> None:
        session = {
            "agent_harness": "opencode",
            "runtime_provider": "direct",
            "session_handle": {
                "agent_harness": "opencode",
                "runtime_provider": "direct",
                "external_id": "ses_gp_e49eb9d4a6664ae6870c5dd47a12be00",
                "metadata": {"dispatches": []},
            },
        }
        self.assertEqual(session_agent_harness(session), "opencode")

    def test_session_agent_harness_rejects_unknown(self) -> None:
        session = {
            "agent_harness": "sweagent",
            "runtime_provider": "direct",
            "session_handle": {"agent_harness": "sweagent"},
        }
        self.assertIsNone(session_agent_harness(session))


if __name__ == "__main__":
    unittest.main()
