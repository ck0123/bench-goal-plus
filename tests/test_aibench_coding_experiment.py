from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bench_goal_plus.catalog import Catalog
from bench_runtime_paths import ensure_temp_root
from experiments.aibench_coding import bridge, reporting, runtime, sandbox, task_adapter
from experiments.aibench_coding.cli import build_parser
from experiments.aibench_coding.config import (
    AIBenchContractError,
    load_profile,
    pi_api,
    resolve_profile,
    split_model,
)
from experiments.benchmark_compare import experiment as benchmark_compare
from experiments.benchmark_compare import pi_worker_launcher


ROOT = Path(__file__).resolve().parents[1]


class AIBenchCodingContractTest(unittest.TestCase):
    def test_pi_flash_profile_preserves_provider_and_controller_source(self) -> None:
        _path, profile = load_profile("goal-plus-pi-glm53flash-smoke")
        self.assertEqual(split_model(profile), ("zai", "glm-5.3-flash"))
        self.assertEqual(profile["agent_provider"]["wire_api"], "completions")
        with self.assertRaisesRegex(AIBenchContractError, "Responses"):
            resolve_profile(profile, methods=["goal-plus-codex"])
        with mock.patch.dict(os.environ, {
            "BENCH_GOAL_PLUS_SOURCE_DIR": "/trusted/goal-plus",
            "BENCH_GOAL_PLUS_EXPECTED_REF": "current",
            "PI_CODING_AGENT_DIR": "/trusted/pi",
            "UNRELATED_SECRET": "not-for-agent",
        }), mock.patch.object(
            runtime, "_worker_proxy_base", return_value=self.root
        ) as proxy_base:
            environment = runtime._agent_environment(Path("/cell"), profile, "goal-plus-pi")
        proxy_base.assert_called_once()
        self.assertEqual(
            Path(environment["AIBENCH_PROXY_RUNTIME_DIR"]).parent,
            self.root,
        )
        self.assertEqual(environment["BENCH_GOAL_PLUS_SOURCE_DIR"], "/trusted/goal-plus")
        self.assertEqual(environment["BENCH_GOAL_PLUS_EXPECTED_REF"], "current")
        self.assertEqual(environment["PI_CODING_AGENT_DIR"], "/trusted/pi")
        self.assertNotIn("UNRELATED_SECRET", environment)

    def test_visible_process_feedback_keeps_hidden_grading_in_controller(self) -> None:
        from experiments.openevolve_compare.experiment import render_goal
        from adapters.registry import load_adapter_module

        loaded = load_adapter_module(runtime.ADAPTER_ID, runtime.ADAPTER_MODULE)
        self.assertTrue(loaded.module.CONTROLLER_ONLY_OFFICIAL_EVALUATION)

        prompt = render_goal(
            task_text="Repair submission using public tests", artifact_name="submission",
            artifact_is_directory=True, metric_name=task_adapter.GOAL_PLUS_PROCESS_METRIC,
            metric_direction="maximize", wall_seconds=900, closeout_seconds=120,
            concurrency=1, agent_harness="pi", worker_model="zai/glm-5.3-flash",
            controller_only_official_evaluation=True, evaluation_mode=task_adapter.EVALUATION_MODE,
        )
        self.assertIn("promotion_mode=apply", prompt)
        self.assertIn("`ranking_signal`", prompt)
        self.assertIn("visible_test_score", prompt)
        self.assertNotIn("format_valid", prompt)
        self.assertEqual(task_adapter.PI_WORKER_SANDBOX["evaluation_mode"], "visible")

    def test_visible_selection_closeout_does_not_require_blind_selection_rule(self) -> None:
        closeout = {"completed": True, "runs": [{
            "selection": {"selected_candidate_id": "c001"},
            "promotion": {"artifact_path": "/artifact"},
            "final_state": "promoted", "goal_statuses": {"gp_0001": "complete"},
        }]}
        reason = benchmark_compare._controller_only_closeout_incomplete_reason
        self.assertIsNone(reason(closeout, deterministic_public_gate=False))
        self.assertIsNotNone(reason(closeout, deterministic_public_gate=True))
        closeout["runs"][0]["goal_statuses"]["gp_0001"] = "active"
        self.assertIsNotNone(reason(closeout, deterministic_public_gate=False))

    def test_failed_closeout_keeps_official_score_through_repair(self) -> None:
        self.addCleanup(benchmark_compare.configure_adapter, "heurigym")
        benchmark_compare.configure_adapter(
            "aibench-coding-native",
            module_name="experiments.aibench_coding.task_adapter",
        )
        run_dir = self.root / "goal-plus-closeout-failure"
        workspace = run_dir / "workspace"
        (workspace / "submission").mkdir(parents=True)
        (workspace / "submission" / "solution.py").write_text(
            "RESULT = 1\n", encoding="utf-8"
        )
        (workspace / "TASK.md").write_text("fix it\n", encoding="utf-8")
        manifest = {
            "method": "goal-plus-codex",
            "workspace": str(workspace),
            "reasoning_effort": "medium",
            "environment": {"runtime_bin": str(self.root / "bin")},
            "task": {
                "controller_only_official_evaluation": True,
                "goal_plus_early_stop": None,
                "goal_plus_posthoc_selection": None,
            },
            "goal_plus_config": {
                "early_stop": None,
                "posthoc_selection": None,
                "shared_dir_enabled": False,
            },
            "budget": {
                "wall_time_seconds": 300,
                "soft_closeout_seconds": 60,
                "hard_kill_grace_seconds": 5,
                "concurrency": 1,
                "worker_runtime_seconds": 200,
                "worker_min_runtime_seconds": None,
            },
        }
        args = SimpleNamespace(
            model="gpt-test",
            api_base=None,
            codex_bin="codex-test",
            pi_provider_id="openai",
            pi_api="openai-responses",
            pi_api_key_env="OPENAI_API_KEY",
        )
        seed = {"valid": True, "budget": {"total_claimed": 1}}
        final = {
            "valid": True,
            "mode": "final",
            "primary_metric": {"name": "task_success", "value": True},
            "budget": {"total_claimed": 1},
        }

        with (
            mock.patch.object(
                benchmark_compare,
                "evaluate_with_controller_runtime",
                side_effect=[seed, final],
            ) as evaluate,
            mock.patch.object(benchmark_compare, "configure_isolated_codex_home"),
            mock.patch.object(
                benchmark_compare, "configure_evidence_annotator_environment"
            ),
            mock.patch.object(benchmark_compare, "bind_goal_plus_environment"),
            mock.patch.object(benchmark_compare, "render_goal", return_value="prompt"),
            mock.patch.object(
                benchmark_compare, "codex_command", return_value=["codex-test"]
            ),
            mock.patch.object(
                benchmark_compare,
                "run_controlled",
                return_value={
                    "returncode": 0,
                    "deadline_reached": False,
                    "hard_killed": False,
                    "controller_interrupted": False,
                },
            ),
            mock.patch.object(
                benchmark_compare, "close_candidate_sessions", return_value=[]
            ),
            mock.patch.object(
                benchmark_compare,
                "finalize_goal_plus_search",
                return_value={
                    "completed": False,
                    "runs": [],
                    "error": "recovery pending",
                },
            ),
            mock.patch.object(
                benchmark_compare,
                "parse_codex_events",
                return_value={"top_level_usage": {}},
            ),
            mock.patch.object(
                benchmark_compare,
                "collect_goal_plus_state",
                return_value={"runs": [], "goals": []},
            ),
            mock.patch.object(
                benchmark_compare,
                "collect_evidence_annotator_usage",
                return_value={},
            ),
            mock.patch.object(
                benchmark_compare, "goal_plus_incomplete_reason", return_value=None
            ),
        ):
            control = benchmark_compare.execute_goal_plus(
                manifest, run_dir, args, {}
            )

        self.assertEqual(evaluate.call_count, 2)
        self.assertTrue((run_dir / "final-eval.json").is_file())
        self.assertEqual(control["evaluator_calls"]["controller_final_claimed"], 1)
        self.assertNotIn("official_evaluation_withheld", control)
        self.assertIn("recovery pending", control["result_incomplete_reason"])

        control["official_evaluation_withheld"] = True
        manifest.update(
            {
                "benchmark_adapter": "aibench-coding-native",
                "benchmark_adapter_module": (
                    "experiments.aibench_coding.task_adapter"
                ),
                "execution": control,
                "status": "incomplete",
            }
        )
        (run_dir / "experiment.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        with (
            mock.patch.object(
                benchmark_compare, "close_candidate_sessions", return_value=[]
            ),
            mock.patch.object(
                benchmark_compare,
                "finalize_goal_plus_search",
                return_value={
                    "completed": False,
                    "runs": [],
                    "error": "recovery pending",
                },
            ),
            mock.patch.object(
                benchmark_compare, "evaluate_with_controller_runtime"
            ) as repair_evaluate,
            mock.patch.object(
                benchmark_compare,
                "collect_goal_plus_state",
                return_value={"runs": [], "goals": []},
            ),
            mock.patch.object(
                benchmark_compare,
                "collect_evidence_annotator_usage",
                return_value={},
            ),
        ):
            result = benchmark_compare.repair_closeout(
                SimpleNamespace(run_dir=run_dir)
            )

        repaired = json.loads((run_dir / "experiment.json").read_text())
        self.assertEqual(result, 2)
        repair_evaluate.assert_not_called()
        self.assertNotIn("official_evaluation_withheld", repaired["execution"])
        self.assertEqual(
            repaired["execution"]["evaluator_calls"]["controller_final_claimed"],
            1,
        )

    def test_bwrap_option_detection_supports_old_and_new_versions(self) -> None:
        for help_output, expected in (
            ("--unshare-user --disable-userns --cap-drop", True),
            ("--unshare-user --cap-drop", False),
        ):
            with self.subTest(help_output=help_output), mock.patch.object(
                pi_worker_launcher.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["/usr/bin/bwrap", "--help"],
                    0,
                    stdout=help_output,
                    stderr="",
                ),
            ):
                self.assertEqual(
                    pi_worker_launcher._bwrap_supports_option(
                        "/usr/bin/bwrap", "--disable-userns", {}
                    ),
                    expected,
                )

    def test_bwrap_option_detection_fails_closed(self) -> None:
        with mock.patch.object(
            pi_worker_launcher.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["/usr/bin/bwrap", "--help"],
                1,
                stdout="",
                stderr="broken",
            ),
        ), self.assertRaisesRegex(
            RuntimeError, "failed to inspect Bubblewrap options"
        ):
            pi_worker_launcher._bwrap_supports_option(
                "/usr/bin/bwrap", "--disable-userns", {}
            )

    def test_pi_runtime_root_preserves_npm_bin_symlink(self) -> None:
        runtime_root = self.root / "pi-runtime"
        executable = runtime_root / "node_modules" / ".bin" / "pi"
        target = (
            runtime_root
            / "node_modules"
            / "@earendil-works"
            / "pi-coding-agent"
            / "dist"
            / "cli.js"
        )
        target.parent.mkdir(parents=True)
        executable.parent.mkdir(parents=True)
        target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        executable.symlink_to(
            Path("..") / "@earendil-works" / "pi-coding-agent" / "dist" / "cli.js"
        )

        self.assertEqual(
            pi_worker_launcher._executable_runtime_root(executable),
            runtime_root.resolve(),
        )
        self.assertEqual(
            pi_worker_launcher._executable_entrypoint(executable),
            target.resolve(strict=True),
        )
        self.assertEqual(
            pi_worker_launcher._executable_runtime_root(target),
            runtime_root.resolve(),
        )

    def test_pi_host_tool_preserves_a_short_rejection_detail(self) -> None:
        completed = subprocess.CompletedProcess(
            [sys.executable, "-m", "goal_plus.pi_tool"],
            1,
            stdout="",
            stderr="toolization_decision requires shared_dir.enabled=true\n",
        )
        with mock.patch.object(
            pi_worker_launcher.subprocess, "run", return_value=completed
        ), self.assertRaisesRegex(RuntimeError, "shared_dir.enabled=true"):
            pi_worker_launcher._run_host_tool(
                self.root / ".gp",
                "goal_plus_search_run_verifier",
                {},
                {"GOAL_PLUS_PYTHON": sys.executable},
            )

    def test_hidden_grading_only_blocks_native_closeout_for_blind_search(self) -> None:
        manifest = {
            "workspace": str(self.root), "budget": {}, "method": "goal-plus-pi",
            "task": {"controller_only_official_evaluation": True},
        }
        for mode in ("visible", "blind"):
            with (
                self.subTest(mode=mode),
                mock.patch.object(benchmark_compare, "EVALUATION_MODE", mode),
                mock.patch.object(benchmark_compare, "CONTROLLER_ONLY_OFFICIAL_EVALUATION", True),
                mock.patch.object(benchmark_compare, "GOAL_PLUS_EARLY_STOP_CONTRACT", None),
                mock.patch.object(benchmark_compare, "GOAL_PLUS_POSTHOC_SELECTION_CONTRACT", None),
                mock.patch.object(benchmark_compare, "evaluate_with_controller_runtime",
                                  return_value={"valid": False, "budget": {"total_claimed": 0}}),
            ):
                environment = {benchmark_compare.CONTROLLER_ONLY_CLOSEOUT_ENV: "1"}
                result = benchmark_compare.execute_goal_plus(
                    manifest, self.root, SimpleNamespace(), environment
                )
            self.assertTrue(result["preflight_failed"])
            self.assertEqual(benchmark_compare.CONTROLLER_ONLY_CLOSEOUT_ENV in environment, mode == "blind")

    def test_candidate_judge_reserves_pi_closeout_in_visible_mode(self) -> None:
        manifest = {
            "workspace": str(self.root),
            "budget": {},
            "method": "goal-plus-pi",
            "candidate_judge": {
                "mode": "jev",
                "endpoint": "https://openrouter.ai/api/alpha/decisions",
                "model": "typesafe/jev-1.13",
            },
            "task": {"controller_only_official_evaluation": True},
        }
        environment = {
            "GOAL_PLUS_JUDGE": "jev",
            "GOAL_PLUS_JEV_MODEL": "typesafe/jev-1.13",
            "OPENROUTER_API_KEY": "judge-secret",
        }
        with (
            mock.patch.object(benchmark_compare, "EVALUATION_MODE", "visible"),
            mock.patch.object(
                benchmark_compare, "CONTROLLER_ONLY_OFFICIAL_EVALUATION", True
            ),
            mock.patch.object(benchmark_compare, "GOAL_PLUS_EARLY_STOP_CONTRACT", None),
            mock.patch.object(
                benchmark_compare, "GOAL_PLUS_POSTHOC_SELECTION_CONTRACT", None
            ),
            mock.patch.object(
                benchmark_compare,
                "evaluate_with_controller_runtime",
                return_value={"valid": False, "budget": {"total_claimed": 0}},
            ),
        ):
            result = benchmark_compare.execute_goal_plus(
                manifest, self.root, SimpleNamespace(), environment
            )
        self.assertTrue(result["preflight_failed"])
        self.assertEqual(
            environment[benchmark_compare.CONTROLLER_ONLY_CLOSEOUT_ENV], "1"
        )

    def test_controller_runtime_capability_gate_matches_closeout_paths(self) -> None:
        required = benchmark_compare._requires_controller_runtime_capabilities
        with mock.patch.object(benchmark_compare, "EVALUATION_MODE", "visible"):
            self.assertTrue(required("goal-plus-codex", "jev", True))
            self.assertFalse(required("goal-plus-pi", "off", True))
            self.assertFalse(required("plain-pi", "jev", True))
        with mock.patch.object(benchmark_compare, "EVALUATION_MODE", "blind"):
            self.assertTrue(required("goal-plus-pi", "off", True))

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="aibench-coding-test-", dir=ensure_temp_root("tests")
        )
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _zai_profile(self, methods: list[str]) -> dict[str, object]:
        _path, profile = load_profile("smoke")
        profile["methods"] = methods
        profile["model"] = "zai/glm-5.2"
        profile["agent_provider"] = {
            "id": "zai",
            "name": "Z.AI Anthropic-compatible API",
            "auth_mode": "anthropic-compatible",
            "base_url_env": "ZAI_BASE_URL",
            "api_key_env": "ZAI_API_KEY",
            "wire_api": "anthropic-messages",
        }
        return profile

    def test_catalog_exposes_four_methods_and_native_capabilities(self) -> None:
        catalog = Catalog()
        runner = catalog.runners["aibench-coding-native"]
        self.assertEqual(
            set(runner.supported_methods),
            {
                "plain-codex",
                "plain-pi",
                "goal-plus-codex",
                "goal-plus-pi",
            },
        )
        self.assertTrue(runner.capabilities.cell_concurrency)
        self.assertTrue(runner.capabilities.official_evaluator)
        self.assertFalse(runner.capabilities.detach)
        target = catalog.targets["aibench-coding"]
        self.assertTrue(target.local_asset_inventory)
        self.assertEqual(target.docker.requirement, "not_required")

    def test_registry_promotes_only_evidenced_goal_plus_methods(self) -> None:
        registry = json.loads(
            (ROOT / "benchmarks" / "registry.json").read_text(encoding="utf-8")
        )
        item = next(
            entry for entry in registry["items"] if entry["id"] == "aibench-coding"
        )
        evidence_path = item["stage_evidence"]["goal_plus_codex"][0]
        summary = json.loads((ROOT / evidence_path).read_text(encoding="utf-8"))

        self.assertEqual(item["stages"]["goal_plus_codex"], "pass")
        self.assertEqual(item["stages"]["plain_codex"], "partial")
        self.assertEqual(item["stages"]["plain_pi"], "partial")
        self.assertEqual(item["stages"]["goal_plus_pi"], "pass")
        self.assertTrue(all((ROOT / path).is_file() for path in item["stage_evidence"]["goal_plus_pi"]))
        self.assertEqual(item["stages"]["campaign_ready"], "partial")
        self.assertEqual(summary["method"]["id"], "goal-plus-codex")
        self.assertEqual(summary["status"], "completed")
        self.assertTrue(summary["result"]["score_valid"])
        self.assertTrue(summary["execution"]["topology"]["matches_k"])

    def test_profile_and_provider_model_route_are_frozen(self) -> None:
        _path, profile = load_profile("smoke")
        self.assertEqual(profile["expected_case_set_fingerprint"], "9149d02169845dc5")
        self.assertEqual(profile["agent_provider"]["auth_mode"], "openai-compatible")
        self.assertEqual(
            split_model(profile),
            ("bench-openai", "gpt-5.6-sol"),
        )
        with self.assertRaises(AIBenchContractError):
            resolve_profile(profile, methods=["plain-pi"], model="gpt-5.6-sol")
        oauth = json.loads(json.dumps(profile))
        oauth["methods"] = ["goal-plus-codex"]
        oauth["model"] = "gpt-5.6-sol"
        oauth["agent_provider"] = {
            "id": "openai-codex",
            "name": "Codex ChatGPT OAuth",
            "auth_mode": "codex-oauth",
            "base_url_env": None,
            "api_key_env": None,
            "wire_api": "codex-chatgpt",
        }
        with self.assertRaisesRegex(AIBenchContractError, "openai-compatible"):
            resolve_profile(oauth)

    def test_pi_provider_protocol_is_preserved(self) -> None:
        profile = self._zai_profile(["goal-plus-pi"])
        resolved = resolve_profile(profile)
        self.assertEqual(pi_api(resolved), "anthropic-messages")

    def test_codex_rejects_anthropic_provider(self) -> None:
        profile = self._zai_profile(["goal-plus-codex"])
        with self.assertRaisesRegex(
            AIBenchContractError,
            "Codex methods require openai-compatible Responses",
        ):
            resolve_profile(profile)

    def test_cli_accepts_native_runner_override_contract(self) -> None:
        args = build_parser().parse_args(
            [
                "doctor",
                "--profile",
                "smoke",
                "--method",
                "goal-plus-pi",
                "--model",
                "bench-openai/gpt-5.6-sol",
                "--reasoning-effort",
                "high",
            ]
        )
        self.assertEqual(args.method, ["goal-plus-pi"])
        self.assertEqual(args.reasoning_effort, "high")

    def test_all_four_methods_use_controller_only_hidden_evaluation(self) -> None:
        self.assertTrue(
            {
                "plain-codex",
                "plain-pi",
                "goal-plus-codex",
                "goal-plus-pi",
            }.issubset(benchmark_compare.CONTROLLER_ONLY_METHODS)
        )
        self.assertEqual(
            task_adapter.PI_WORKER_SANDBOX["writable_workspace_paths"],
            ["submission"],
        )

    def test_goal_plus_pi_worker_uses_unwrapped_binary_inside_worker_sandbox(
        self,
    ) -> None:
        policy = task_adapter.PI_WORKER_SANDBOX
        expected = {
            **policy,
            "pass_env": [
                *policy["pass_env"],
                "OPENAI_API_KEY",
                "OPENAI_BASE_URL",
                "OPENAI_API_BASE_URL",
            ],
        }
        environment = {
            "PATH": "/usr/bin",
            benchmark_compare.REAL_PI_BIN_ENV: "/host/bin/pi",
        }
        with (
            mock.patch.object(benchmark_compare, "PI_WORKER_SANDBOX", policy),
            mock.patch.object(
                benchmark_compare,
                "_resolve_real_pi_binary",
                return_value=Path("/host/bin/pi"),
            ) as resolve,
            mock.patch.object(
                benchmark_compare.shutil, "which", return_value="/usr/bin/bwrap"
            ),
        ):
            benchmark_compare._configure_pi_worker_sandbox_environment(
                {
                    "method": "goal-plus-pi",
                    "goal_plus_config": {"worker_sandbox": expected},
                },
                environment,
                "OPENAI_API_KEY",
                "/cell/pi-sandbox",
            )
        self.assertEqual(resolve.call_args.args[0], "/host/bin/pi")
        self.assertEqual(
            environment[benchmark_compare.REAL_PI_BIN_ENV], "/host/bin/pi"
        )

    def test_candidate_judge_env_is_controller_only_for_goal_plus(self) -> None:
        profile = self._zai_profile(["goal-plus-codex"])
        judge_values = {
            "PATH": "/usr/bin",
            "ZAI_BASE_URL": "https://agent.example/v1",
            "ZAI_API_KEY": "agent-key",
            "GOAL_PLUS_JUDGE": "jev",
            "OPENROUTER_API_KEY": "judge-key",
            "GOAL_PLUS_JEV_API_KEY_ENV": "DECISIONS_API_KEY",
            "DECISIONS_API_KEY": "provider-judge-key",
            "GOAL_PLUS_JEV_ENDPOINT": "https://judge.example/decisions",
            "GOAL_PLUS_JEV_MODEL": "typesafe/jev-1.13",
            "GOAL_PLUS_JUDGE_TIMEOUT_SECONDS": "15",
            "GOAL_PLUS_LLM_VERIFIER_MODEL": "judge-model",
            "GOAL_PLUS_LLM_VERIFIER_API_KEY": "llm-judge-key",
            "GOAL_PLUS_LLM_VERIFIER_BASE_URL": "https://llm-judge.example/v1",
            "GOAL_PLUS_LLM_VERIFIER_EVALUATIONS": "2",
            "GOAL_PLUS_LLM_VERIFIER_PIVOTS": "1",
            "OPENAI_API_KEY": "openai-judge-key",
            "OPENAI_BASE_URL": "https://openai-judge.example/v1",
            "DEEPSEEK_API_KEY": "deepseek-agent-key",
            "VERTEX_API_KEY": "vertex-agent-key",
        }
        with mock.patch.dict(os.environ, judge_values, clear=True):
            goal_environment = runtime._agent_environment(
                self.root / "goal-plus", profile, "goal-plus-codex"
            )
            plain_environment = runtime._agent_environment(
                self.root / "plain", profile, "plain-codex"
            )

        for name, value in judge_values.items():
            if name in runtime._CANDIDATE_JUDGE_CONTROLLER_ENV:
                self.assertEqual(goal_environment.get(name), value)
                self.assertNotIn(name, plain_environment)

        self.assertEqual(goal_environment.get("GOAL_PLUS_JEV_API_KEY_ENV"), "DECISIONS_API_KEY")
        self.assertEqual(goal_environment.get("DECISIONS_API_KEY"), "provider-judge-key")
        self.assertNotIn("GOAL_PLUS_JEV_API_KEY_ENV", plain_environment)
        self.assertNotIn("DECISIONS_API_KEY", plain_environment)

        worker_environment = dict(goal_environment)
        worker_environment.update(
            {
                "GOAL_PLUS_EVIDENCE_ANNOTATOR_MODEL": "old-annotator",
                "GOAL_PLUS_EVIDENCE_ANNOTATOR_API_KEY_ENV": "OPENAI_API_KEY",
            }
        )
        benchmark_compare._hide_candidate_judge_from_workers(worker_environment)
        for name in runtime._CANDIDATE_JUDGE_CONTROLLER_ENV:
            self.assertNotIn(name, worker_environment)
        self.assertNotIn("GOAL_PLUS_JEV_API_KEY_ENV", worker_environment)
        self.assertNotIn("DECISIONS_API_KEY", worker_environment)
        self.assertNotIn(
            "GOAL_PLUS_EVIDENCE_ANNOTATOR_MODEL", worker_environment
        )

    def _metadata(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "case_id": "rev-09f7740f614d3ea9",
            "case_set": "_clean2026",
            "case_set_fingerprint": "9149d02169845dc5",
            "case_fingerprint": "v3:test",
            "task_type": "bugfix",
            "language": "python",
            "prompt": "Repair build.py.",
            "grader_command": "true",
            "validity_ok": True,
        }

    def test_model_free_materialize_and_public_evaluation(self) -> None:
        source = self.root / "checkout" / "benchmarks" / "coding"
        source.mkdir(parents=True)
        workspace = self.root / "workspace"

        def fake_bridge(_command: list[str], _source: Path, timeout: int = 300) -> dict:
            del timeout
            submission = workspace / "submission"
            submission.mkdir()
            (submission / "build.py").write_text("value = 1\n", encoding="utf-8")
            return self._metadata()

        with (
            mock.patch.object(task_adapter, "_bridge", side_effect=fake_bridge),
            mock.patch.object(task_adapter, "git_commit", return_value="a" * 40),
        ):
            prepared = task_adapter.materialize_workspace(source, workspace)
            report = task_adapter.evaluate_workspace(workspace, source, "public")
        self.assertEqual(prepared["source_revision"], "a" * 40)
        self.assertTrue(report["valid"])
        self.assertEqual(report["primary_metric"]["value"], 1.0)
        self.assertEqual(
            (workspace / "public_check.py").read_text(encoding="utf-8"),
            (workspace / ".goal-plus-verifiers" / "primary_metric.py").read_text(
                encoding="utf-8"
            ),
        )
        self.assertFalse((workspace / ".gp").exists())
        task_prompt = (workspace / "TASK.md").read_text(encoding="utf-8")
        agent_rules = (workspace / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("goal_plus_search_run_verifier", task_prompt)
        self.assertIn("goal_plus_search_run_verifier", agent_rules)
        self.assertNotIn("`search_run_verifier`", task_prompt)
        self.assertNotIn("`search_run_verifier`", agent_rules)

    def test_bridge_environment_prefers_locked_runtime(self) -> None:
        source = self.root / "source"
        environment = task_adapter._bridge_environment(source)
        self.assertEqual(
            environment["PATH"].split(os.pathsep)[0],
            str(task_adapter.RUNTIME_PYTHON.parent),
        )
        self.assertEqual(environment["PYTHONPATH"], str(source / "src"))

    def test_upstream_python_version_is_an_exact_minor(self) -> None:
        source = self.root / "source"
        source.mkdir()
        (source / ".python-version").write_text("3.13\n", encoding="utf-8")
        self.assertEqual(runtime._pinned_python_version(source), "3.13")
        (source / ".python-version").write_text(">=3.11\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "MAJOR.MINOR"):
            runtime._pinned_python_version(source)

    def test_public_evaluation_rejects_controller_file_changes(self) -> None:
        source = self.root / "checkout" / "benchmarks" / "coding"
        source.mkdir(parents=True)
        workspace = self.root / "workspace"

        def fake_bridge(_command: list[str], _source: Path, timeout: int = 300) -> dict:
            del timeout
            submission = workspace / "submission"
            submission.mkdir()
            (submission / "build.py").write_text("value = 1\n", encoding="utf-8")
            return self._metadata()

        with (
            mock.patch.object(task_adapter, "_bridge", side_effect=fake_bridge),
            mock.patch.object(task_adapter, "git_commit", return_value="b" * 40),
        ):
            task_adapter.materialize_workspace(source, workspace)
        (workspace / "TASK.md").write_text("tampered\n", encoding="utf-8")
        report = task_adapter.evaluate_workspace(workspace, source, "public")
        self.assertFalse(report["valid"])
        self.assertIn("TASK.md", report["unauthorized_changes"])

    def test_official_collection_error_is_not_a_valid_failure_score(self) -> None:
        workspace = self.root / "workspace"
        source = self.root / "source"
        workspace.mkdir()
        (workspace / "submission").mkdir()
        metadata = self._metadata()
        with mock.patch.object(
            task_adapter,
            "_bridge",
            return_value={
                "grade": {
                    "passed": False,
                    "infra_error": False,
                    "collection_error": True,
                    "detail": "pytest could not collect",
                }
            },
        ):
            report = task_adapter._official_evaluation(workspace, source, metadata)
        self.assertFalse(report["valid"])
        self.assertIsNone(report["value"])

    def test_official_bridge_rejects_symlinked_submission_root(self) -> None:
        hidden = self.root / "hidden"
        hidden.mkdir()
        submission = self.root / "submission"
        submission.symlink_to(hidden, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "real directory"):
            bridge._submission_root(submission)

    def test_bubblewrap_masks_hidden_checkout_and_other_cells(self) -> None:
        campaign = self.root / "campaign"
        cell = campaign / "cells" / "cell-1"
        workspace = cell / "workspaces" / "lane-00"
        hidden = self.root / "aibench-checkout"
        binary = self.root / "codex"
        workspace.mkdir(parents=True)
        hidden.mkdir()
        binary.write_text("", encoding="utf-8")
        environment = {
            "AIBENCH_AGENT_ROLE": "codex",
            "AIBENCH_METHOD": "plain-codex",
            "AIBENCH_REAL_CODEX_BIN": str(binary),
            "AIBENCH_HIDDEN_CHECKOUT": str(hidden),
            "AIBENCH_CELL_ROOT": str(cell),
        }
        previous = Path.cwd()
        try:
            os.chdir(workspace)
            with (
                mock.patch.dict(os.environ, environment, clear=False),
                mock.patch.object(
                    sandbox.shutil, "which", return_value="/usr/bin/bwrap"
                ),
            ):
                command = sandbox.build_command(["exec", "--json"])
        finally:
            os.chdir(previous)
        pairs = list(zip(command, command[1:]))
        self.assertIn(("--tmpfs", str(hidden)), pairs)
        self.assertIn(("--tmpfs", str(cell.parent)), pairs)
        self.assertIn(("--bind", str(workspace)), pairs)
        self.assertEqual(command[-3:], [str(binary), "exec", "--json"])

    def test_bubblewrap_masks_symlinked_hidden_checkout_target(self) -> None:
        cell = self.root / "campaign" / "cells" / "cell-1"
        workspace = cell / "workspaces" / "lane-00"
        hidden = self.root / "hidden-real"
        hidden_link = self.root / "hidden-link"
        binary = self.root / "codex"
        workspace.mkdir(parents=True)
        hidden.mkdir()
        hidden_link.symlink_to(hidden, target_is_directory=True)
        binary.write_text("", encoding="utf-8")
        environment = {
            "AIBENCH_AGENT_ROLE": "codex",
            "AIBENCH_METHOD": "plain-codex",
            "AIBENCH_REAL_CODEX_BIN": str(binary),
            "AIBENCH_HIDDEN_CHECKOUT": str(hidden_link),
            "AIBENCH_CELL_ROOT": str(cell),
        }
        previous = Path.cwd()
        try:
            os.chdir(workspace)
            with (
                mock.patch.dict(os.environ, environment, clear=False),
                mock.patch.object(
                    sandbox.shutil, "which", return_value="/usr/bin/bwrap"
                ),
            ):
                command = sandbox.build_command([])
        finally:
            os.chdir(previous)

        tmpfs_targets = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "--tmpfs"
        ]
        self.assertIn(str(hidden.resolve()), tmpfs_targets)
        self.assertNotIn(str(hidden_link.absolute()), tmpfs_targets)

    @unittest.skipUnless(shutil.which("bwrap"), "requires Bubblewrap")
    def test_goal_plus_pi_outer_sandbox_can_create_worker_socket(self) -> None:
        cell = self.root / "cells/one"
        workspace = cell / "workspace"
        workspace.mkdir(parents=True)
        hidden = self.root / "hidden"
        hidden.mkdir()
        (hidden / "gold.txt").write_text("not public")
        with tempfile.TemporaryDirectory(
            prefix="ab-",
            dir=pi_worker_launcher._worker_proxy_base(os.environ),
        ) as scratch:
            environment = {
                "AIBENCH_AGENT_ROLE": "pi", "AIBENCH_METHOD": "goal-plus-pi",
                "AIBENCH_REAL_PI_BIN": sys.executable,
                "AIBENCH_HIDDEN_CHECKOUT": str(hidden), "AIBENCH_CELL_ROOT": str(cell),
                "AIBENCH_PROXY_RUNTIME_DIR": scratch,
            }
            script = (
                "import os,socket,sys; from pathlib import Path; "
                f"sys.path.insert(0,{str(ROOT)!r}); "
                "from experiments.benchmark_compare.pi_worker_launcher import _worker_proxy_base; "
                "p=_worker_proxy_base(os.environ)/'bgp-pi-0000000000000000'; p.mkdir(); "
                "s=socket.socket(socket.AF_UNIX); s.bind(str(p/'tool.sock')); "
                f"assert not (Path({str(hidden)!r})/'gold.txt').exists()"
            )
            with mock.patch.dict(os.environ, environment), mock.patch.object(Path, "cwd", return_value=workspace):
                command = sandbox.build_command(["-c", script])
            completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def _write_cell(
        self,
        campaign: Path,
        method: str,
        *,
        success: bool,
        actual: int = 1,
    ) -> dict[str, object]:
        run_dir = campaign / "cells" / method
        run_dir.mkdir(parents=True)
        (run_dir / "submission").mkdir()
        if method.startswith("plain-"):
            agent = "pi" if method == "plain-pi" else "codex"
            execution = {
                agent: {
                    "lanes": [
                        {"lane": f"lane-{index:02d}"} for index in range(actual)
                    ]
                }
            }
        elif method == "goal-plus-codex":
            execution = {"codex": {"spawned_agent_thread_count": actual}}
        else:
            execution = {
                "goal_plus": {"runs": [{"bound_candidate_count": actual}]},
                "pi": {},
            }
        execution["evaluator_calls"] = {
            "total_claimed": 3,
            "controller_final_claimed": 1,
            "coverage": "complete",
        }
        manifest = {"status": "finished", "execution": execution}
        if method == "goal-plus-pi":
            manifest["pi_worker_sandbox"] = {
                "engine": "bubblewrap",
                "launch_interception": "bench-owned-pi-path-shim",
            }
        (run_dir / "experiment.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (run_dir / "final-eval.json").write_text(
            json.dumps(
                {
                    "valid": True,
                    "primary_metric": {
                        "name": "task_success",
                        "value": success,
                        "direction": "maximize",
                    },
                    "grade": {
                        "passed": success,
                        "test_pass_ratio": 1.0 if success else 0.5,
                        "infra_error": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        return {
            "cell_id": method,
            "task_id": "rev-09f7740f614d3ea9",
            "method": method,
            "seed": 1,
            "run_dir": str(run_dir),
            "state": "completed",
            "sandbox": {
                "kind": "bubblewrap",
                "hidden_checkout_masked": True,
            },
        }

    def _campaign(self, destination: Path, cells: list[dict], k: int) -> None:
        payload = {
            "schema_version": 1,
            "campaign_id": destination.name,
            "benchmark": "aibench-coding",
            "state": "completed",
            "model": "bench-openai/gpt-5.6-sol",
            "reasoning_effort": "medium",
            "budget": {
                "wall_time_seconds": 300,
                "live_search_concurrency": k,
                "cell_concurrency": 1,
                "repeats": 1,
            },
            "source": {
                "case_set": "_clean2026",
                "case_set_fingerprint": "9149d02169845dc5",
                "commit": "c" * 40,
                "goal_plus_commit": "d" * 40,
            },
            "cells": cells,
        }
        (destination / "campaign.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_finalize_preserves_false_score_and_topology_evidence(self) -> None:
        campaign = self.root / "campaign"
        campaign.mkdir()
        methods = ["plain-codex", "plain-pi", "goal-plus-codex", "goal-plus-pi"]
        cells = [
            self._write_cell(campaign, method, success=method != "plain-pi")
            for method in methods
        ]
        self._campaign(campaign, cells, 1)
        summary = reporting.finalize_campaign(campaign)
        self.assertEqual(summary["state"], "completed")
        plain_pi = next(
            item for item in summary["records"] if item["method"] == "plain-pi"
        )
        self.assertTrue(plain_pi["score"]["valid"])
        self.assertEqual(plain_pi["score"]["final"], 0)
        self.assertTrue(
            all(
                item["protocol"]["topology"]["matches_k"]
                for item in summary["records"]
            )
        )
        self.assertEqual(summary["aggregates"]["official_evaluator_calls"], 4)

    def test_finalize_marks_k_mismatch_partial_without_dropping_score(self) -> None:
        campaign = self.root / "campaign"
        campaign.mkdir()
        cell = self._write_cell(campaign, "goal-plus-pi", success=True, actual=1)
        self._campaign(campaign, [cell], 2)
        summary = reporting.finalize_campaign(campaign)
        self.assertEqual(summary["state"], "partial")
        record = summary["records"][0]
        self.assertEqual(record["score"]["final"], 1)
        self.assertFalse(record["protocol"]["matched_comparison_eligible"])
        self.assertIsNone(
            summary["aggregates"]["by_method"]["goal-plus-pi"]["pass_at_k"]
        )

    def test_finalize_requires_goal_plus_pi_worker_sandbox_evidence(self) -> None:
        campaign = self.root / "campaign"
        campaign.mkdir()
        cell = self._write_cell(campaign, "goal-plus-pi", success=True)
        manifest_path = Path(cell["run_dir"]) / "experiment.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("pi_worker_sandbox")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self._campaign(campaign, [cell], 1)
        summary = reporting.finalize_campaign(campaign)
        self.assertEqual(summary["state"], "partial")
        self.assertIn(
            "worker Bubblewrap isolation",
            summary["records"][0]["incomplete_reason"],
        )


if __name__ == "__main__":
    unittest.main()
