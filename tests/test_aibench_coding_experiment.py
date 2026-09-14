from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adapters.registry import load_adapter_module
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
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="aibench-coding-test-", dir=ensure_temp_root("tests")
        )
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _sandbox_command(
        self, role: str, method: str, arguments: list[str]
    ) -> tuple[list[str], Path, Path, Path, Path]:
        cell = self.root / "campaign" / "cells" / "cell-1"
        workspace = (
            cell / "workspace"
            if method.startswith("goal-plus-")
            else cell / "workspaces" / "lane-00"
        )
        hidden = self.root / "aibench-checkout"
        hidden_runtime = self.root / "aibench-runtime"
        agent_runtime = self.root / "agent-runtime"
        grader_runtime = self.root / "grader-runtime"
        goal_plus_runtime = self.root / "goal-plus-runtime"
        binary = self.root / role
        workspace.mkdir(parents=True)
        hidden.mkdir()
        hidden_runtime.mkdir()
        agent_runtime.mkdir()
        grader_runtime.mkdir()
        goal_plus_runtime.mkdir()
        (workspace / task_adapter.PUBLIC_TESTS_RELATIVE).mkdir(parents=True)
        binary.write_text("", encoding="utf-8")
        environment = {
            "AIBENCH_AGENT_ROLE": role,
            "AIBENCH_METHOD": method,
            f"AIBENCH_REAL_{role.upper()}_BIN": str(binary),
            "AIBENCH_CONTROLLER_ROOT": str(ROOT),
            "AIBENCH_AGENT_RUNTIME": str(agent_runtime),
            "AIBENCH_GRADER_RUNTIME": str(grader_runtime),
            "AIBENCH_GOAL_PLUS_RUNTIME": str(goal_plus_runtime),
            "AIBENCH_CELL_ROOT": str(cell),
        }
        previous = Path.cwd()
        try:
            os.chdir(workspace)
            with (
                mock.patch.dict(os.environ, environment, clear=False),
                mock.patch.object(
                    sandbox.shutil,
                    "which",
                    side_effect=lambda name, **_kwargs: (
                        "/usr/bin/bwrap" if name == "bwrap" else str(binary)
                    ),
                ),
            ):
                command = sandbox.build_command(arguments)
        finally:
            os.chdir(previous)
        return command, cell, workspace, hidden, binary

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
        ):
            with self.assertRaisesRegex(
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

    def test_worker_proxy_accepts_only_goal_plus_python_transport(self) -> None:
        proxy = (
            ROOT
            / "experiments"
            / "benchmark_compare"
            / "bin"
            / "goal-plus-pi-tool"
        )
        command = [
            sys.executable,
            str(proxy),
            "-I",
            "-c",
            "from goal_plus.pi_tool import main; raise SystemExit(main())",
            "--root",
            ".gp",
            "--args-json",
            "{}",
            "search_get_agent_context",
        ]
        accepted = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(accepted.returncode, 1)
        self.assertIn(pi_worker_launcher.TOOL_SOCKET_ENV, accepted.stderr)
        self.assertNotIn("unrecognized arguments", accepted.stderr)

        host_kick = [
            *command[:-1],
            "--host-entrypoint",
            "--host-capability",
            "fixture-token",
            "goal_plus_host_kick_internal_agents",
        ]
        no_op = subprocess.run(
            host_kick,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(no_op.returncode, 0, no_op.stderr)
        self.assertEqual(
            json.loads(no_op.stdout),
            {"ok": True, "disposition": "worker_noop"},
        )

        host_kick[-1] = "goal_plus_host_control"
        rejected_host = subprocess.run(
            host_kick,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(rejected_host.returncode, 1)
        self.assertIn("worker proxy rejected host entrypoint", rejected_host.stderr)

        command[4] = "print('not a Goal Plus transport')"
        rejected = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("unsupported Goal Plus Python transport", rejected.stderr)

    def _anthropic_pi_profile(self, methods: list[str]) -> dict[str, object]:
        _path, profile = load_profile("smoke")
        profile["methods"] = methods
        profile["model"] = (
            "vendor/example-model"
            if any("pi" in method for method in methods)
            else "example-model"
        )
        profile["agent_provider"] = {
            "id": "vendor",
            "name": "Example Anthropic-compatible API",
            "auth_mode": "anthropic-compatible",
            "base_url_env": "VENDOR_BASE_URL",
            "api_key_env": "VENDOR_API_KEY",
            "wire_api": "anthropic-messages",
        }
        return profile

    def _write_public_test_bundle(
        self,
        workspace: Path,
        metadata: dict[str, object],
        contents: dict[str, str] | None = None,
    ) -> None:
        bundle = workspace / task_adapter.PUBLIC_TESTS_RELATIVE
        bundle.mkdir(parents=True, exist_ok=True)
        for relative, content in (contents or {}).items():
            path = bundle / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        boundary = {
            "schema_version": 1,
            "case_id": metadata["case_id"],
            "case_set": metadata["case_set"],
            "case_set_fingerprint": metadata["case_set_fingerprint"],
            "case_fingerprint": metadata["case_fingerprint"],
            "language": metadata["language"],
            "grader_command": metadata["grader_command"],
            "public_test_files": metadata["public_test_files"],
        }
        boundary_bytes = (json.dumps(boundary, sort_keys=True) + "\n").encode()
        (bundle / task_adapter.BOUNDARY_FILE).write_bytes(boundary_bytes)
        metadata["public_test_boundary_sha256"] = hashlib.sha256(
            boundary_bytes
        ).hexdigest()

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

    def test_registry_promotes_only_the_evidenced_goal_plus_codex_method(self) -> None:
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
        self.assertEqual(item["stages"]["goal_plus_pi"], "partial")
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

    def test_pi_accepts_anthropic_messages_provider(self) -> None:
        profile = self._anthropic_pi_profile(["goal-plus-pi"])

        resolved = resolve_profile(profile)

        self.assertEqual(split_model(resolved), ("vendor", "example-model"))
        self.assertEqual(pi_api(resolved), "anthropic-messages")

    def test_latest_pi_assets_use_the_isolated_development_runtime(self) -> None:
        goal_plus_root = self.root / "goal-plus"
        pi_assets = goal_plus_root / "assets/pi"
        (pi_assets / "extensions").mkdir(parents=True)
        (pi_assets / "skills/goal-plus").mkdir(parents=True)
        (pi_assets / "prompts").mkdir(parents=True)
        (pi_assets / "extensions/goal-plus.ts").write_text("extension\n")
        (pi_assets / "skills/goal-plus/SKILL.md").write_text("skill\n")
        environment: dict[str, str] = {}

        extension, skill = benchmark_compare.configure_goal_plus_pi_runtime(
            environment, goal_plus_root
        )

        self.assertEqual(extension, pi_assets / "extensions/goal-plus.ts")
        self.assertEqual(skill, pi_assets / "skills/goal-plus/SKILL.md")
        self.assertEqual(environment["GOAL_PLUS_PI_DEV_ROOT"], str(goal_plus_root))
        self.assertEqual(
            environment["GOAL_PLUS_PYTHON"], str(Path(sys.executable).absolute())
        )

    def test_codex_rejects_anthropic_messages_provider(self) -> None:
        profile = self._anthropic_pi_profile(["goal-plus-codex"])

        with self.assertRaisesRegex(
            AIBenchContractError, "Codex methods require openai-compatible responses"
        ):
            resolve_profile(profile)

    def test_codex_goal_plus_forwards_public_test_boundary(self) -> None:
        self.assertEqual(
            task_adapter.GOAL_PLUS_MCP_ENV_VARS,
            ("AIBENCH_PUBLIC_TESTS", "AIBENCH_PUBLIC_TESTS_SHA256"),
        )

    def test_runtime_passes_anthropic_messages_to_pi(self) -> None:
        profile = self._anthropic_pi_profile(["goal-plus-pi"])
        run_dir = self.root / "cell"
        run_dir.mkdir()
        source_workspace = run_dir / "workspace"
        metadata = self._metadata()
        self._write_public_test_bundle(source_workspace, metadata)
        (source_workspace / "task.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        captured_command: list[str] = []
        captured_environment: dict[str, str] = {}

        def fake_run(command: list[str], **kwargs: object) -> object:
            captured_command.extend(command)
            captured_environment.update(kwargs["env"])  # type: ignore[arg-type]
            (run_dir / "experiment.json").write_text(
                json.dumps({"status": "finished"}), encoding="utf-8"
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.dict(
                os.environ,
                {
                    "VENDOR_BASE_URL": "https://example.invalid",
                    "VENDOR_API_KEY": "key",
                },
                clear=False,
            ),
            mock.patch.object(
                runtime,
                "_sandbox_binaries",
                return_value=(Path("/codex"), Path("/pi")),
            ),
            mock.patch.object(runtime.subprocess, "run", side_effect=fake_run),
            mock.patch.object(
                runtime.shutil, "which", side_effect=lambda name: f"/bin/{name}"
            ),
        ):
            result = runtime._run_cell(
                profile,
                {"run_dir": str(run_dir), "method": "goal-plus-pi"},
            )

        self.assertEqual(
            captured_command[captured_command.index("--pi-api") + 1],
            "anthropic-messages",
        )
        self.assertEqual(
            captured_command[captured_command.index("--pi-provider-id") + 1],
            "vendor",
        )
        self.assertNotIn("AIBENCH_PUBLIC_TESTS", captured_environment)
        self.assertEqual(
            captured_environment["AIBENCH_PUBLIC_TESTS_SHA256"],
            metadata["public_test_boundary_sha256"],
        )
        self.assertEqual(result["state"], "completed")

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
        self.assertEqual(task_adapter.EVALUATION_MODE, "visible")
        loaded = load_adapter_module(
            "aibench-coding-native", "experiments.aibench_coding.task_adapter"
        )
        self.assertTrue(
            loaded.manifest_contract()["controller_only_official_evaluation"]
        )
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
        self.assertEqual(
            task_adapter.PI_WORKER_SANDBOX["evaluation_mode"], "visible"
        )
        self.assertEqual(
            task_adapter.PI_WORKER_SANDBOX["read_only_host_paths"],
            ["/aibench-public-tests"],
        )
        parsed = pi_worker_launcher.SandboxPolicy.from_environment(
            {
                pi_worker_launcher.SANDBOX_POLICY_ENV: json.dumps(
                    task_adapter.PI_WORKER_SANDBOX
                )
            }
        )
        self.assertEqual(parsed.read_only_host_paths, ("/aibench-public-tests",))

    def test_visible_closeout_does_not_require_blind_selection_rule(self) -> None:
        closeout = {
            "completed": True,
            "runs": [
                {
                    "selection": {"selected_candidate_id": "c001"},
                    "promotion": {"artifact_path": "promotion/c001.patch"},
                    "final_state": "promoted",
                    "goal_statuses": {"gp_0001": "complete"},
                }
            ],
        }

        self.assertIsNone(
            benchmark_compare._controller_only_closeout_incomplete_reason(
                closeout, require_deterministic_selection=False
            )
        )
        self.assertIn(
            "deterministic selection evidence",
            benchmark_compare._controller_only_closeout_incomplete_reason(
                closeout, require_deterministic_selection=True
            ),
        )

    def test_failed_closeout_keeps_official_score_but_remains_incomplete(self) -> None:
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
        (workspace / "GOAL.md").write_text("prompt", encoding="utf-8")
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
        )
        seed = {"valid": True, "budget": {"total_claimed": 1}}
        final = {
            "valid": True,
            "mode": "final",
            "primary_metric": {"name": "task_success", "value": True},
            "budget": {"total_claimed": 1},
        }

        def failed_closeout(*_args: object, **_kwargs: object) -> dict[str, object]:
            deadline = datetime.fromisoformat(
                os.environ["GOAL_PLUS_OUTER_DEADLINE_AT"]
            )
            self.assertLessEqual(deadline, datetime.now(timezone.utc))
            return {"completed": False, "runs": [], "error": "recovery pending"}

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
                benchmark_compare, "finalize_goal_plus_search", failed_closeout
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
        self.assertTrue((run_dir / "submission" / "solution.py").is_file())
        self.assertEqual(control["evaluator_calls"]["controller_final_claimed"], 1)
        self.assertNotIn("official_evaluation_withheld", control)
        self.assertIn("recovery pending", control["result_incomplete_reason"])

    def test_visible_pi_worker_proxy_surfaces_host_rejection(self) -> None:
        context = pi_worker_launcher.LaunchContext(
            run_id="run_1",
            candidate_id="c001",
            agent_session_id="agent_1",
            workspace=self.root,
        )
        proxy = pi_worker_launcher.WorkerToolProxy(
            root=self.root / ".gp",
            context=context,
            socket_dir=self.root / "proxy",
            evaluation_mode="visible",
        )
        request = {
            "tool": "search_run_verifier",
            "args": {
                "run_id": "run_1",
                "candidate_id": "c001",
                "agent_session_id": "agent_1",
            },
        }
        with (
            mock.patch.object(
                pi_worker_launcher,
                "_run_host_tool",
                side_effect=RuntimeError(
                    "toolization_decision requires shared_dir.enabled=true"
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "shared_dir.enabled=true"),
        ):
            proxy.dispatch(request)

    def test_pi_host_tool_preserves_a_short_rejection_detail(self) -> None:
        completed = subprocess.CompletedProcess(
            ["goal-plus-pi-tool"],
            1,
            stdout="",
            stderr="toolization_decision requires shared_dir.enabled=true\n",
        )
        with (
            mock.patch.object(
                pi_worker_launcher.subprocess, "run", return_value=completed
            ),
            self.assertRaisesRegex(RuntimeError, "shared_dir.enabled=true"),
        ):
            pi_worker_launcher._run_host_tool(
                self.root / ".gp", "search_run_verifier", {}, {}
            )

    def test_plain_visible_k2_selects_the_best_public_score(self) -> None:
        self.addCleanup(benchmark_compare.configure_adapter, "heurigym")
        benchmark_compare.configure_adapter(
            "aibench-coding-native",
            module_name="experiments.aibench_coding.task_adapter",
        )
        run_dir = self.root / "plain-k2"
        workspaces = []
        for lane in range(2):
            workspace = run_dir / "workspaces" / f"lane-{lane:02d}"
            (workspace / "submission").mkdir(parents=True)
            (workspace / "TASK.md").write_text("fix the task\n", encoding="utf-8")
            (workspace / "submission" / "solution.py").write_text(
                f"LANE = {lane}\n", encoding="utf-8"
            )
            workspaces.append(workspace)

        def evaluation(score: float) -> dict[str, object]:
            return {
                "valid": True,
                "primary_metric": {"value": score},
                "budget": {"total_claimed": 1},
            }

        manifest = {
            "method": "plain-codex",
            "reasoning_effort": "medium",
            "workspaces": [str(path) for path in workspaces],
            "task": {"controller_only_official_evaluation": True},
            "budget": {
                "wall_time_seconds": 300,
                "soft_closeout_seconds": 60,
                "hard_kill_grace_seconds": 30,
                "concurrency": 2,
            },
        }
        args = SimpleNamespace(
            model="gpt-test",
            pi_bin="pi-test",
            codex_bin="codex-test",
            api_base=None,
        )
        controlled = {
            "lanes": [
                {"name": f"lane-{lane:02d}", "returncode": 0, "hard_killed": False}
                for lane in range(2)
            ]
        }
        with (
            mock.patch.object(
                benchmark_compare,
                "evaluate",
                side_effect=[
                    evaluation(0.0),
                    evaluation(0.0),
                    evaluation(0.25),
                    evaluation(0.75),
                    evaluation(0.80),
                ],
            ),
            mock.patch.object(
                benchmark_compare,
                "evaluator_budget",
                return_value={"total_claimed": 1},
            ),
            mock.patch.object(
                benchmark_compare, "run_controlled_many", return_value=controlled
            ),
            mock.patch.object(
                benchmark_compare,
                "parse_codex_events",
                return_value={"coverage": "codex"},
            ),
        ):
            result = benchmark_compare.execute_plain(manifest, run_dir, args, {})

        self.assertEqual(result["selected_lane"], "lane-01")
        self.assertEqual(
            (run_dir / "submission" / "solution.py").read_text(encoding="utf-8"),
            "LANE = 1\n",
        )

    def test_visible_public_feedback_stays_distinct_from_official_ownership(
        self,
    ) -> None:
        prompt = benchmark_compare.render_goal(
            task_text="# Objective\nRepair the submission.",
            artifact_name="submission",
            artifact_is_directory=True,
            metric_name=task_adapter.GOAL_PLUS_PROCESS_METRIC,
            metric_direction=task_adapter.DIRECTION,
            wall_seconds=300,
            closeout_seconds=60,
            concurrency=1,
            worker_host="codex",
            worker_model="gpt-test",
            controller_only_official_evaluation=True,
            evaluation_mode=task_adapter.EVALUATION_MODE,
        )

        self.assertIn("Public evaluator calls are not hard-capped", prompt)
        self.assertIn("Metric: `visible_test_score` with direction `maximize`", prompt)
        self.assertIn("role `ranking_signal`", prompt)
        self.assertNotIn("it is a public format gate only", prompt)
        self.assertNotIn("Hidden evaluation is unavailable during exploration", prompt)

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
            "public_test_files": [],
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
            metadata = self._metadata()
            self._write_public_test_bundle(workspace, metadata)
            return metadata

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

    def test_materialize_separates_public_test_and_rejects_candidate_collision(
        self,
    ) -> None:
        source = self.root / "checkout" / "benchmarks" / "coding"
        source.mkdir(parents=True)
        workspace = self.root / "workspace"

        original_test = "def test_build(): pass\n"

        def fake_bridge(_command: list[str], _source: Path, timeout: int = 300) -> dict:
            del timeout
            submission = workspace / "submission"
            submission.mkdir()
            (submission / "build.py").write_text("value = 1\n", encoding="utf-8")
            metadata = self._metadata()
            metadata["public_test_files"] = [
                {
                    "path": "test_build.py",
                    "sha256": hashlib.sha256(original_test.encode()).hexdigest(),
                }
            ]
            self._write_public_test_bundle(
                workspace, metadata, {"test_build.py": original_test}
            )
            return metadata

        with (
            mock.patch.object(task_adapter, "_bridge", side_effect=fake_bridge),
            mock.patch.object(task_adapter, "git_commit", return_value="b" * 40),
        ):
            task_adapter.materialize_workspace(source, workspace)
            self.assertFalse((workspace / "submission" / "test_build.py").exists())
            tracked = subprocess.run(
                ["git", "-C", str(workspace), "ls-files"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            self.assertNotIn("submission/test_build.py", tracked)
            self.assertFalse(
                any(path.startswith(".bench-runtime/") for path in tracked)
            )
            (workspace / "submission" / "test_build.py").write_text(
                "def test_build(): assert True\n", encoding="utf-8"
            )
            with mock.patch.object(task_adapter, "_visible_ratio") as visible_ratio:
                report = task_adapter.evaluate_workspace(workspace, source, "public")

        visible_ratio.assert_not_called()
        self.assertFalse(report["valid"])
        self.assertIsNone(report["primary_metric"]["value"])
        self.assertEqual(
            report["integrity_violation"],
            "reserved_public_test_path: test_build.py",
        )

    def test_bridge_splits_and_restores_controller_owned_public_tests(self) -> None:
        complete = self.root / "complete"
        submission = self.root / "submission"
        public_tests = self.root / "public-tests"
        (complete / "nested").mkdir(parents=True)
        (complete / "build.py").write_text("value = 1\n", encoding="utf-8")
        original_test = "def test_build(): assert True\n"
        (complete / "nested" / "test_build.py").write_text(
            original_test, encoding="utf-8"
        )

        records = bridge._split_materialized_workspace(
            complete,
            submission,
            public_tests,
            ["nested/test_build.py"],
        )

        self.assertTrue((submission / "build.py").is_file())
        self.assertFalse((submission / "nested" / "test_build.py").exists())
        self.assertEqual(
            (public_tests / "nested" / "test_build.py").read_text(), original_test
        )
        self.assertEqual(records[0]["path"], "nested/test_build.py")

        evaluated = self.root / "evaluated"
        shutil.copytree(submission, evaluated)
        bridge._restore_protected_files(
            complete, evaluated, ["nested/test_build.py"]
        )
        self.assertEqual(
            (evaluated / "nested" / "test_build.py").read_text(), original_test
        )
        with self.assertRaisesRegex(RuntimeError, "controller-owned public test"):
            bridge._restore_protected_files(
                complete, evaluated, ["nested/test_build.py"]
            )

    def test_public_evaluation_reassembles_tests_with_a_clean_environment(
        self,
    ) -> None:
        trusted_workspace = self.root / "trusted-workspace"
        workspace = self.root / "candidate-workspace"
        submission = workspace / "submission"
        submission.mkdir(parents=True)
        (submission / "build.py").write_text("value = 1\n", encoding="utf-8")
        metadata = self._metadata()
        public_test = (
            "import os\n"
            "from pathlib import Path\n"
            "assert os.environ.get('TEST_OUTER_SECRET') is None\n"
            "assert Path(os.environ['HOME']).parent == Path.cwd().parent\n"
            "assert Path(os.environ['TMPDIR']).parent == Path.cwd().parent\n"
            "assert Path('build.py').read_text() == 'value = 1\\n'\n"
            "print('1 passed')\n"
        )
        metadata["grader_command"] = (
            f"{shlex.quote(sys.executable)} public_test.py"
        )
        metadata["public_test_files"] = [
            {
                "path": "public_test.py",
                "sha256": hashlib.sha256(public_test.encode()).hexdigest(),
            }
        ]
        self._write_public_test_bundle(
            trusted_workspace, metadata, {"public_test.py": public_test}
        )
        (workspace / "task.json").write_text(json.dumps(metadata), encoding="utf-8")
        task_adapter.init_git(workspace, "fixture")

        metadata.update(
            {
                "case_id": "forged-case",
                "language": "javascript",
                "grader_command": "false",
            }
        )
        (workspace / "task.json").write_text(json.dumps(metadata), encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(workspace), "add", "task.json"], check=True
        )
        subprocess.run(
            ["git", "-C", str(workspace), "commit", "-q", "-m", "candidate commit"],
            check=True,
        )

        verifier_tmpdir = self.root / "controller-verifier"
        bundle = trusted_workspace / task_adapter.PUBLIC_TESTS_RELATIVE
        with (
            mock.patch.object(task_adapter, "PUBLIC_TESTS_MOUNT", bundle),
            mock.patch.dict(
                os.environ,
                {
                    "AIBENCH_PUBLIC_TESTS": str(bundle),
                    "AIBENCH_PUBLIC_TESTS_SHA256": metadata[
                        "public_test_boundary_sha256"
                    ],
                    "GOAL_PLUS_VERIFIER_TMPDIR": str(verifier_tmpdir),
                    "TEST_OUTER_SECRET": "must-not-leak",
                },
                clear=False,
            ),
        ):
            report = task_adapter.evaluate_workspace(workspace, self.root, "public")

        self.assertTrue(report["valid"], report["diagnostics"])
        self.assertEqual(report["primary_metric"]["value"], 1.0)
        self.assertEqual(report["case_id"], "rev-09f7740f614d3ea9")
        self.assertFalse((submission / "public_test.py").exists())
        runtime_dir = verifier_tmpdir / "benchmark-runtime"
        self.assertFalse(any(runtime_dir.glob("public-evaluation-*")))

    def test_public_evaluation_ignores_an_agent_supplied_path(self) -> None:
        workspace = self.root / "workspace"
        (workspace / "submission").mkdir(parents=True)
        metadata = self._metadata()
        self._write_public_test_bundle(workspace, metadata)
        bundle = workspace / task_adapter.PUBLIC_TESTS_RELATIVE

        forged_bin = self.root / "agent-bin"
        forged_bin.mkdir()
        marker = self.root / "forged-grader-ran"
        forged_grader = forged_bin / "true"
        forged_grader.write_text(
            f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n",
            encoding="utf-8",
        )
        forged_grader.chmod(0o755)

        with (
            mock.patch.object(task_adapter, "PUBLIC_TESTS_MOUNT", bundle),
            mock.patch.dict(
                os.environ,
                {
                    "PATH": str(forged_bin),
                    "AIBENCH_PUBLIC_TESTS": str(bundle),
                    "AIBENCH_PUBLIC_TESTS_SHA256": metadata[
                        "public_test_boundary_sha256"
                    ],
                },
                clear=False,
            ),
        ):
            trusted_bundle, boundary, records = task_adapter._public_test_bundle(
                workspace, metadata
            )
            report = task_adapter._public_evaluation(
                workspace, boundary, trusted_bundle, records
            )

        self.assertTrue(report["valid"], report["diagnostics"])
        self.assertFalse(marker.exists())
        self.assertEqual(
            task_adapter.TRUSTED_EVALUATOR_PATH.split(os.pathsep)[0],
            str(task_adapter.RUNTIME_PYTHON.parent),
        )

    def test_public_evaluation_rejects_an_agent_supplied_bundle(self) -> None:
        workspace = self.root / "workspace"
        (workspace / "submission").mkdir(parents=True)
        metadata = self._metadata()
        self._write_public_test_bundle(workspace, metadata)
        (workspace / "task.json").write_text(json.dumps(metadata), encoding="utf-8")
        task_adapter.init_git(workspace, "fixture")

        forged = self.root / "forged-public-tests"
        forged.mkdir()
        forged_boundary = {
            "schema_version": 1,
            "case_id": metadata["case_id"],
            "case_set": metadata["case_set"],
            "case_set_fingerprint": metadata["case_set_fingerprint"],
            "case_fingerprint": metadata["case_fingerprint"],
            "language": "python",
            "grader_command": "true",
            "public_test_files": [],
        }
        forged_bytes = (json.dumps(forged_boundary, sort_keys=True) + "\n").encode()
        (forged / task_adapter.BOUNDARY_FILE).write_bytes(forged_bytes)

        controller_mount = self.root / "controller-mount"
        controller_mount.mkdir()
        with (
            mock.patch.object(
                task_adapter,
                "PUBLIC_TESTS_MOUNT",
                controller_mount,
            ),
            mock.patch.dict(
                os.environ,
                {
                    "AIBENCH_PUBLIC_TESTS": str(forged),
                    "AIBENCH_PUBLIC_TESTS_SHA256": hashlib.sha256(
                        forged_bytes
                    ).hexdigest(),
                },
                clear=False,
            ),
            self.assertRaisesRegex(RuntimeError, "mount path is not controller-owned"),
        ):
            task_adapter.evaluate_workspace(workspace, self.root, "public")

    def test_public_evaluation_rejects_a_tampered_test_bundle(self) -> None:
        workspace = self.root / "workspace"
        (workspace / "submission").mkdir(parents=True)
        metadata = self._metadata()
        metadata["public_test_files"] = []
        self._write_public_test_bundle(workspace, metadata)
        (workspace / "task.json").write_text(json.dumps(metadata), encoding="utf-8")
        task_adapter.init_git(workspace, "fixture")
        boundary = (
            workspace / task_adapter.PUBLIC_TESTS_RELATIVE / task_adapter.BOUNDARY_FILE
        )
        boundary.write_text(boundary.read_text() + " ", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "boundary hash"):
            task_adapter.evaluate_workspace(workspace, self.root, "public")

    def test_public_evaluation_rejects_tampered_public_test_content(self) -> None:
        workspace = self.root / "workspace"
        (workspace / "submission").mkdir(parents=True)
        metadata = self._metadata()
        original = "print('1 passed')\n"
        metadata["public_test_files"] = [
            {
                "path": "public_test.py",
                "sha256": hashlib.sha256(original.encode()).hexdigest(),
            }
        ]
        self._write_public_test_bundle(
            workspace, metadata, {"public_test.py": original}
        )
        (workspace / "task.json").write_text(json.dumps(metadata), encoding="utf-8")
        task_adapter.init_git(workspace, "fixture")
        public_test = (
            workspace
            / task_adapter.PUBLIC_TESTS_RELATIVE
            / "public_test.py"
        )
        public_test.write_text("print('forged')\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "public test changed"):
            task_adapter.evaluate_workspace(workspace, self.root, "public")

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
            metadata = self._metadata()
            self._write_public_test_bundle(workspace, metadata)
            return metadata

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

    def test_bubblewrap_allowlists_runtime_without_mounting_host_root(self) -> None:
        command, cell, workspace, hidden, binary = self._sandbox_command(
            "codex", "plain-codex", ["exec", "--json"]
        )
        pairs = list(zip(command, command[1:]))
        triples = [command[index : index + 3] for index in range(len(command) - 2)]
        public_tests = (workspace / task_adapter.PUBLIC_TESTS_RELATIVE).resolve()
        self.assertNotIn(["--ro-bind", "/", "/"], triples)
        self.assertIn("--unshare-all", command)
        self.assertIn("--share-net", command)
        self.assertIn("--unshare-user", command)
        self.assertIn(
            ["--dev", "/dev"],
            [command[index : index + 2] for index in range(len(command) - 1)],
        )
        self.assertNotIn("--dev-bind", command)
        self.assertNotIn(str(hidden), command)
        self.assertNotIn(str(self.root / "aibench-runtime"), command)
        self.assertNotIn(
            ["--ro-bind", str(cell.parent), str(cell.parent)], triples
        )
        self.assertNotIn(["--bind", str(cell.parent), str(cell.parent)], triples)
        self.assertIn(["--ro-bind", "/usr", "/usr"], triples)
        self.assertIn(
            [
                "--ro-bind",
                str((self.root / "agent-runtime").resolve()),
                str(self.root / "agent-runtime"),
            ],
            triples,
        )
        benchmark_compare = ROOT / "experiments" / "benchmark_compare"
        self.assertIn(
            ["--ro-bind", str(benchmark_compare), str(benchmark_compare)], triples
        )
        self.assertNotIn(
            ["--ro-bind", str(ROOT / "adapters"), str(ROOT / "adapters")], triples
        )
        self.assertNotIn(str(ROOT / "adapters" / "registry.py"), command)
        self.assertIn(("--bind", str(workspace)), pairs)
        self.assertIn(
            ["--ro-bind", str(public_tests), str(public_tests)], triples
        )
        self.assertIn(
            ["--ro-bind", str(public_tests), "/aibench-public-tests"], triples
        )
        self.assertIn(
            ["--setenv", "AIBENCH_PUBLIC_TESTS", "/aibench-public-tests"], triples
        )
        self.assertEqual(command[-3:], [str(binary), "exec", "--json"])

    def test_task_adapter_import_does_not_load_full_control_plane(self) -> None:
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import experiments.aibench_coding.task_adapter\n"
            "assert 'bench_goal_plus.application' not in sys.modules\n"
            "assert 'adapters.registry' not in sys.modules\n"
            "from bench_goal_plus import BenchmarkAgent, Catalog\n"
            "assert BenchmarkAgent.__name__ == 'BenchmarkAgent'\n"
            "assert Catalog.__name__ == 'Catalog'\n"
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-c", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_bubblewrap_does_not_mount_agent_home_as_runtime(self) -> None:
        fake_home = self.root / "home"
        binary = fake_home / "bin" / "codex"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        (fake_home / "package.json").write_text("{}\n", encoding="utf-8")

        with mock.patch.object(sandbox.Path, "home", return_value=fake_home):
            self.assertEqual(sandbox._runtime_mount(binary), binary.resolve())

    def test_bubblewrap_mounts_python_distribution_behind_venv_symlink(self) -> None:
        runtime = self.root / "python-runtime"
        interpreter = runtime / "bin/python3.12"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("", encoding="utf-8")
        venv_python = self.root / "venv/bin/python"
        venv_python.parent.mkdir(parents=True)
        venv_python.symlink_to(interpreter)

        self.assertEqual(sandbox._runtime_mount(venv_python), runtime.resolve())

    def test_bubblewrap_preserves_npm_pi_symlink_for_nested_worker(self) -> None:
        fake_home = self.root / "home"
        node_modules = fake_home / ".local" / "lib" / "node_modules"
        entrypoint = node_modules / "pi-package" / "dist" / "bundle" / "cli.js"
        entrypoint.parent.mkdir(parents=True)
        entrypoint.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        alias = fake_home / ".local" / "bin" / "pi"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(Path("../lib/node_modules/pi-package/dist/bundle/cli.js"))

        command: list[str] = []
        with mock.patch.object(sandbox.Path, "home", return_value=fake_home):
            sandbox._bind_runtime(
                command,
                set(),
                alias,
                (fake_home,),
            )

        triples = [command[index : index + 3] for index in range(len(command) - 2)]
        self.assertIn(
            ["--ro-bind", str(node_modules), str(node_modules)], triples
        )
        self.assertIn(["--symlink", str(entrypoint), str(alias)], triples)
        self.assertNotIn(["--ro-bind", str(entrypoint), str(alias)], triples)

    def test_bubblewrap_does_not_mount_symlinked_hidden_checkout(self) -> None:
        cell = self.root / "campaign" / "cells" / "cell-1"
        workspace = cell / "workspace"
        hidden = self.root / "hidden-real"
        hidden_runtime = self.root / "aibench-runtime"
        hidden_link = self.root / "hidden-link"
        binary = self.root / "pi"
        workspace.mkdir(parents=True)
        hidden.mkdir()
        hidden_runtime.mkdir()
        (workspace / task_adapter.PUBLIC_TESTS_RELATIVE).mkdir(parents=True)
        hidden_link.symlink_to(hidden, target_is_directory=True)
        binary.write_text("", encoding="utf-8")
        environment = {
            "AIBENCH_AGENT_ROLE": "pi",
            "AIBENCH_METHOD": "goal-plus-pi",
            "AIBENCH_REAL_PI_BIN": str(binary),
            "AIBENCH_CONTROLLER_ROOT": str(ROOT),
            "AIBENCH_AGENT_RUNTIME": str(self.root / "agent-runtime"),
            "AIBENCH_GRADER_RUNTIME": str(self.root / "grader-runtime"),
            "AIBENCH_GOAL_PLUS_RUNTIME": str(self.root / "goal-plus-runtime"),
            "AIBENCH_CELL_ROOT": str(cell),
        }
        (self.root / "agent-runtime").mkdir()
        (self.root / "grader-runtime").mkdir()
        (self.root / "goal-plus-runtime").mkdir()
        previous = Path.cwd()
        try:
            os.chdir(workspace)
            with (
                mock.patch.dict(os.environ, environment, clear=False),
                mock.patch.object(
                    sandbox.shutil,
                    "which",
                    side_effect=lambda name, **_kwargs: (
                        "/usr/bin/bwrap" if name == "bwrap" else str(binary)
                    ),
                ),
            ):
                command = sandbox.build_command([])
        finally:
            os.chdir(previous)

        self.assertNotIn(str(hidden.resolve()), command)
        self.assertNotIn(str(hidden_link.absolute()), command)

    def test_goal_plus_pi_binds_private_short_xdg_runtime(self) -> None:
        command, cell, *_ = self._sandbox_command(
            "pi", "goal-plus-pi", ["--mode", "rpc"]
        )

        goal_plus_runtime = self.root / "goal-plus-runtime"
        triples = [command[index : index + 3] for index in range(len(command) - 2)]
        self.assertIn(
            [
                "--ro-bind",
                str(goal_plus_runtime.resolve()),
                str(goal_plus_runtime),
            ],
            triples,
        )
        source = cell / "controller-runtime" / "agent-home" / "xdg-runtime"
        destination = str(sandbox.XDG_RUNTIME_DESTINATION)
        self.assertIn(["--bind", str(source), destination], triples)
        self.assertIn(["--setenv", "XDG_RUNTIME_DIR", destination], triples)
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o700)

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
                "host_filesystem_allowlisted": True,
                "hidden_checkout_visible": False,
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

    def test_finalize_requires_host_filesystem_allowlist_evidence(self) -> None:
        campaign = self.root / "campaign"
        campaign.mkdir()
        cell = self._write_cell(campaign, "plain-codex", success=True)
        cell["sandbox"] = {
            "kind": "bubblewrap",
            "hidden_checkout_masked": True,
        }
        self._campaign(campaign, [cell], 1)

        summary = reporting.finalize_campaign(campaign)

        self.assertEqual(summary["state"], "partial")
        self.assertIn(
            "host-filesystem allowlist",
            summary["records"][0]["incomplete_reason"],
        )


if __name__ == "__main__":
    unittest.main()
