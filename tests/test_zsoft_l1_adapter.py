from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.zsoft_l1 import adapter


class AdapterContractTest(unittest.TestCase):
    def test_declares_raw_metric_contract(self) -> None:
        self.assertIn(adapter.DIRECTION, {"minimize", "maximize"})
        self.assertTrue(adapter.PRIMARY_METRIC)
        self.assertTrue(adapter.ARTIFACT_NAME)
        self.assertEqual(adapter.EVALUATION_MODE, "visible")
        self.assertEqual(adapter.PRIMARY_METRIC, "success")
        self.assertEqual(adapter.GOAL_PLUS_PROCESS_METRIC, "success")
        self.assertFalse(adapter.CONTROLLER_ONLY_OFFICIAL_EVALUATION)
        self.assertGreaterEqual(
            adapter.PROCESS_VERIFIER_TIMEOUT_SECONDS,
            adapter.OFFICIAL_EVALUATOR_TIMEOUT_SECONDS + 120,
        )
        self.assertEqual(
            adapter.UPSTREAM_SUBDIR,
            "benchmarks/vulnerability/zsoft-l1",
        )
        self.assertEqual(adapter.PI_WORKER_SANDBOX["engine"], "bubblewrap")
        self.assertEqual(
            adapter.PI_WORKER_SANDBOX["evaluation_mode"], "visible"
        )
        self.assertEqual(adapter.PI_WORKER_SANDBOX["workspace_access"], "read_only")
        self.assertEqual(
            adapter.PI_WORKER_SANDBOX["read_only_workspace_paths"], ["public"]
        )
        self.assertEqual(adapter.PI_WORKER_SANDBOX["writable_workspace_paths"], ["poc"])

    def test_task_catalog_is_pinned(self) -> None:
        task_ids = adapter.list_task_ids()
        self.assertIn("sample-asan-crash", task_ids)
        self.assertGreaterEqual(len(task_ids), 30)

    def test_configure_task_rejects_unknown_task(self) -> None:
        with self.assertRaises(adapter.AdapterError):
            adapter.configure_task("no-such-task")

    def test_process_timeout_follows_the_selected_official_task(self) -> None:
        self.addCleanup(adapter.configure_task, None)
        with mock.patch.object(adapter, "task_metadata", return_value={
            "evaluator": {"timeout_seconds": 180},
        }):
            adapter.configure_task(None)
        self.assertEqual(adapter.OFFICIAL_EVALUATOR_TIMEOUT_SECONDS, 180)
        self.assertEqual(adapter.PROCESS_VERIFIER_TIMEOUT_SECONDS, 360)
        self.assertEqual(adapter.VERIFIER_TIMEOUT_SECONDS, 360)

    def test_materialize_and_placeholder_is_publicly_well_formed(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        workspace = tmp / "ws"
        materialized = adapter.materialize_workspace(adapter.ZSOFT_ROOT, workspace)
        self.assertEqual(materialized["task_id"], adapter.TASK_ID)
        self.assertTrue((workspace / "poc").is_file())
        self.assertTrue((workspace / "TASK.md").is_file())
        self.assertTrue((workspace / "public_check.py").is_file())
        self.assertFalse((workspace / "evaluate.py").exists())
        self.assertTrue((workspace / ".goal-plus-verifiers/primary_metric.py").is_file())
        metadata = json.loads((workspace / "task.json").read_text())
        self.assertEqual(metadata["source_revision"], metadata["upstream_commit"])
        self.assertNotIn("upstream_root", metadata)
        self.assertEqual(metadata["primary_metric"], "success")
        with mock.patch.object(adapter, "_run_cli", return_value=subprocess.CompletedProcess(
            args=[], returncode=1,
            stdout=json.dumps({"result": {"status": "completed", "success": False}}),
            stderr="",
        )) as judge:
            report = adapter.evaluate_workspace(
                workspace, adapter.ZSOFT_ROOT, "public"
            )
        judge.assert_called_once()
        self.assertTrue(report["valid"])
        self.assertTrue(report["format_valid"])
        self.assertEqual(report[adapter.GOAL_PLUS_PROCESS_METRIC], 0)
        self.assertNotIn("zsoft_result", report)
        self.assertEqual(report["budget"]["total_claimed"], 1)

    def test_workspace_must_be_outside_benchmark_root(self) -> None:
        with self.assertRaisesRegex(adapter.AdapterError, "must be disjoint"):
            adapter.materialize_workspace(
                adapter.ZSOFT_ROOT,
                adapter.BENCHMARK_ROOT / ".forbidden-workspace",
            )

    def test_materialize_keeps_task_skills_opt_in_by_default(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.dict(os.environ):
            os.environ.pop(adapter.REA_TOOLS_ENV, None)
            adapter.materialize_workspace(adapter.ZSOFT_ROOT, tmp / "ws")
        self.assertFalse((tmp / "ws" / ".agents" / "skills").exists())
        metadata = json.loads((tmp / "ws" / "task.json").read_text())
        self.assertFalse(metadata["rea_tools"])
        self.assertEqual(metadata["task_skills"], [])

    def test_materialize_copies_rea_skill_when_tools_enabled(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.dict(
            os.environ, {adapter.REA_TOOLS_ENV: "1"}
        ):
            adapter.materialize_workspace(adapter.ZSOFT_ROOT, tmp / "ws")
        skill = tmp / "ws" / ".agents" / "skills" / "rea-analysis" / "SKILL.md"
        self.assertTrue(skill.is_file())
        self.assertFalse(skill.is_symlink())
        metadata = json.loads((tmp / "ws" / "task.json").read_text())
        self.assertTrue(metadata["rea_tools"])
        self.assertEqual(
            metadata["task_skills"], [".agents/skills/rea-analysis/SKILL.md"]
        )
        tracked = subprocess.run(
            ["git", "-C", str(tmp / "ws"), "ls-files"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        self.assertIn(".agents/skills/rea-analysis/SKILL.md", tracked)

    def test_git_commit_supports_shared_runtime_checkouts(self) -> None:
        self.assertRegex(
            adapter.git_commit(ROOT),
            r"^[0-9a-f]{40}$",
        )
        self.assertRegex(adapter.git_commit(adapter.ZSOFT_ROOT), r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
