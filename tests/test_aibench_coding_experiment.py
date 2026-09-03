from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bench_goal_plus.catalog import Catalog
from bench_runtime_paths import ensure_temp_root
from experiments.aibench_coding import bridge, reporting, runtime, sandbox, task_adapter
from experiments.aibench_coding.cli import build_parser
from experiments.aibench_coding.config import (
    AIBenchContractError,
    load_profile,
    resolve_profile,
    split_model,
)
from experiments.benchmark_compare import experiment as benchmark_compare


ROOT = Path(__file__).resolve().parents[1]


class AIBenchCodingContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="aibench-coding-test-", dir=ensure_temp_root("tests")
        )
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

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
