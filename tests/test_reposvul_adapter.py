from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.reposvul import adapter
from adapters.reposvul.build_catalog import (
    _detail_findings,
    _function_name,
    _hunk_spans,
    _locate_span,
)


TRUTH_A = {
    "path": "lib/foo.c",
    "function": "foo",
    "start_line": 10,
    "end_line": 20,
    "cwes": ["CWE-120"],
}
TRUTH_B = {
    "path": "lib/foo.c",
    "function": None,
    "start_line": 50,
    "end_line": 60,
    "cwes": ["CWE-125"],
}


def _finding(
    path: str = "lib/foo.c",
    function: str | None = "foo",
    start: int = 10,
    end: int = 20,
    bug_type: str = "CWE-120",
) -> dict:
    return {
        "location": {
            "path": path,
            "function": function,
            "start_line": start,
            "end_line": end,
        },
        "bug_type": bug_type,
        "root_cause": {"cause": "c", "trigger": "t", "impact": "i"},
    }


class JudgeTest(unittest.TestCase):
    def test_perfect_submission_scores_one(self) -> None:
        score = adapter.score_findings(
            [_finding(), _finding(function=None, start=55, end=58, bug_type="cwe-125")],
            [TRUTH_A, TRUTH_B],
        )
        self.assertEqual((score["tp"], score["fp"], score["fn"]), (2, 0, 0))
        self.assertEqual(score["f1"], 1.0)

    def test_wrong_type_counts_as_false_positive(self) -> None:
        score = adapter.score_findings(
            [_finding(bug_type="CWE-787")], [TRUTH_A]
        )
        self.assertEqual((score["tp"], score["fp"], score["fn"]), (0, 1, 1))

    def test_duplicate_matching_is_one_to_one(self) -> None:
        score = adapter.score_findings(
            [_finding(), _finding(start=12, end=18)], [TRUTH_A]
        )
        self.assertEqual((score["tp"], score["fp"], score["fn"]), (1, 1, 0))

    def test_function_name_match_without_overlap(self) -> None:
        score = adapter.score_findings(
            [_finding(start=1, end=5)], [TRUTH_A]  # lines miss, name hits
        )
        self.assertEqual(score["tp"], 1)

    def test_wrong_file_never_matches(self) -> None:
        score = adapter.score_findings(
            [_finding(path="lib/other.c")], [TRUTH_A]
        )
        self.assertEqual((score["tp"], score["fp"]), (0, 1))

    def test_type_normalization_is_case_insensitive(self) -> None:
        score = adapter.score_findings(
            [_finding(bug_type="cwe_120")], [TRUTH_A]
        )
        self.assertEqual(score["tp"], 1)

    def test_empty_submission_is_valid_zero(self) -> None:
        score = adapter.score_findings([], [TRUTH_A, TRUTH_B])
        self.assertEqual((score["tp"], score["fp"], score["fn"]), (0, 0, 2))
        self.assertEqual(score["f1"], 0.0)


class CatalogBuilderTest(unittest.TestCase):
    def test_function_name_extraction(self) -> None:
        self.assertEqual(
            _function_name("static gboolean\nfoo_bar (MonoArray *a)\n{"),
            "foo_bar",
        )
        self.assertEqual(_function_name("int\nmain(void)\n{"), "main")

    def test_locate_span_exact_and_drifted(self) -> None:
        code = "line1\nline2\nint foo(void)\n{\n}\nline6\n"
        self.assertEqual(_locate_span("int foo(void)\n{\n}", code), (3, 5))
        drifted = _locate_span("int  foo(void)\n{\n}", code)  # spacing drift
        self.assertIsNotNone(drifted)

    def test_hunk_spans(self) -> None:
        patch = "@@ -10,5 +9,6 @@ ctx\n x\n@@ -40 +39 @@\n y"
        self.assertEqual(_hunk_spans(patch), [(10, 14), (40, 40)])

    def test_detail_findings_prefers_target_functions(self) -> None:
        code_before = "a\nint vuln(void)\n{\n  bad();\n}\nb\n"
        detail = {
            "file_name": "src/x.c",
            "code_before": code_before,
            "function_before": [
                {"function": "int ok(void)\n{\n}", "target": 0},
                {"function": "int vuln(void)\n{\n  bad();\n}", "target": 1},
            ],
        }
        findings = _detail_findings(detail)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["function"], "vuln")
        self.assertEqual((findings[0]["start_line"], findings[0]["end_line"]), (2, 5))

    def test_detail_findings_falls_back_to_hunks(self) -> None:
        detail = {
            "file_name": "src/y.c",
            "code_before": "",
            "function_before": [],
            "patch": "@@ -30,4 +30,5 @@\n x",
        }
        findings = _detail_findings(detail)
        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0]["function"])
        self.assertEqual((findings[0]["start_line"], findings[0]["end_line"]), (30, 33))


class AdapterContractTest(unittest.TestCase):
    def test_declares_metric_and_mode_contracts(self) -> None:
        self.assertEqual(adapter.BENCHMARK_NAME, "reposvul")
        self.assertEqual(adapter.PRIMARY_METRIC, "f1")
        self.assertEqual(adapter.DIRECTION, "maximize")
        self.assertEqual(adapter.EVALUATION_MODE, "blind")
        self.assertTrue(adapter.CONTROLLER_ONLY_OFFICIAL_EVALUATION)
        self.assertEqual(adapter.ARTIFACT_NAME, "submission")
        self.assertEqual(
            adapter.PI_WORKER_SANDBOX["read_only_workspace_paths"],
            ["source", "schemas"],
        )

    def test_catalog_exists_and_default_task_resolves(self) -> None:
        catalog = adapter.load_catalog()
        self.assertGreater(catalog["task_count"], 1000)
        adapter.configure_task(None)
        task = adapter.active_task()
        self.assertIn("commit_before", task)
        self.assertGreaterEqual(task["finding_count"], 1)

    def test_configure_task_rejects_unknown_id(self) -> None:
        with self.assertRaises(adapter.AdapterError):
            adapter.configure_task("no-such-task")

    def test_registry_loads_and_validates_the_module(self) -> None:
        from adapters.registry import load_definitions, load_adapter_module

        definitions = load_definitions()
        self.assertIn("reposvul", definitions)
        loaded = load_adapter_module(
            "reposvul", definitions["reposvul"].module_name
        )
        contract = loaded.manifest_contract()
        self.assertEqual(contract["benchmark_name"], "reposvul")
        self.assertEqual(contract["evaluation_mode"], "blind")
        self.assertEqual(contract["primary_metric"], "f1")


class WorkspaceFlowTest(unittest.TestCase):
    """Offline judge flow against a materialization-free synthetic workspace."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.workspace = self.tmp / "ws"
        self.workspace.mkdir()
        task = None
        for candidate in adapter.load_catalog()["tasks"]:
            if candidate["finding_count"] >= 3:
                task = candidate
                break
        assert task is not None
        self.task = task
        (self.workspace / "task.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "adapter": "reposvul",
                    "task_id": task["task_id"],
                    "artifact_name": "submission",
                },
                indent=2,
            )
            + "\n"
        )
        (self.workspace / "submission").mkdir()
        findings = [
            {
                "location": {
                    "path": item["path"],
                    "function": item["function"],
                    "start_line": item["start_line"],
                    "end_line": item["end_line"],
                },
                "bug_type": item["cwes"][0],
                "root_cause": {"cause": "c", "trigger": "t", "impact": "i"},
            }
            for item in task["findings"]
        ]
        for index, payload in enumerate(findings):
            (self.workspace / "submission" / f"gt-{index:02d}.json").write_text(
                json.dumps(payload, indent=1)
            )

    def test_public_then_final_scores_perfect_submission(self) -> None:
        public = adapter.evaluate_workspace(
            self.workspace, Path(ROOT), "public"
        )
        self.assertTrue(public["valid"])
        # 盲契约:搜索期(public)不泄露 F1/TP/FP/FN
        for leak in ("f1", "reposvul_score", "tp", "fp", "fn"):
            self.assertNotIn(leak, public)
        final = adapter.evaluate_workspace(
            self.workspace, Path(ROOT), "final"
        )
        self.assertTrue(final["valid"])
        self.assertEqual(final["reposvul_score"]["fp"], 0)
        self.assertEqual(final["reposvul_score"]["fn"], 0)
        self.assertEqual(final["f1"], 1.0)

    def test_final_report_passes_posthoc_metric_validation(self) -> None:
        """事后结算侧 _posthoc_metric_value 接受本适配器的 *_score 载荷。"""
        from experiments.benchmark_compare.experiment import _posthoc_metric_value

        contract = {
            "metric_name": "f1",
            "metric_direction": "maximize",
        }
        final = adapter.evaluate_workspace(
            self.workspace, Path(ROOT), "final"
        )
        value = _posthoc_metric_value(final, contract)
        self.assertEqual(value, 1.0)


if __name__ == "__main__":
    unittest.main()


class MaterializationLeakTest(unittest.TestCase):
    """Worker-visible workspace files must not contain ground-truth keys.

    The fix commit, CVE ids and CWE ids are all one network query away from
    the full patch; none of them may appear in any file the worker can read
    (TASK.md, AGENTS.md, task.json, public_check.py, schemas/).
    """

    def test_workspace_hides_ground_truth_lookup_keys(self) -> None:
        import os

        os.environ.setdefault("REPOSVUL_CHECKOUT_CACHE", "/tmp/reposvul-leak-test-cache")
        task = None
        for candidate in adapter.load_catalog()["tasks"]:
            if candidate["project"] == "jpirko/libndp" and candidate["finding_count"] == 4:
                task = candidate
                break
        if task is None:  # catalog rebuilt without the sample task
            self.skipTest("libndp sample task unavailable")
        adapter.configure_task(task["task_id"])
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        workspace = tmp / "ws"
        adapter.materialize_workspace(Path(ROOT), workspace)
        forbidden = {task["fix_commit"], task["commit_before"]}
        forbidden.update(task["cve_ids"])
        forbidden.update(task["cwes"])
        leaks: list[str] = []
        for path in sorted(workspace.rglob("*")):
            if path.is_file() and ".git/" not in path.as_posix():
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for key in forbidden:
                    if key and key in text:
                        leaks.append(f"{path.relative_to(workspace)}: {key}")
        self.assertEqual(leaks, [])


class TaskSkillsTest(unittest.TestCase):
    """path-validation / rea-analysis skills 注入开关(默认关)。"""

    def _materialized_tracked_files(self) -> tuple[list[str], Path]:
        import tempfile

        adapter.configure_task(None)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        workspace = tmp / "ws"
        adapter.materialize_workspace(Path(ROOT), workspace)
        import subprocess

        tracked = subprocess.run(
            ["git", "-C", str(workspace), "ls-files"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        return tracked, workspace

    def test_skills_are_opt_in_and_default_off(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop("BENCH_GOAL_PLUS_REPOSVUL_TASK_SKILLS", None)
            os.environ.pop("BENCH_GOAL_PLUS_REPOSVUL_REA_TOOLS", None)
            tracked, workspace = self._materialized_tracked_files()
        skills = [name for name in tracked if name.startswith(".agents/skills/")]
        self.assertEqual(skills, [])
        metadata = json.loads((workspace / "task.json").read_text())
        self.assertEqual(metadata.get("task_skills"), [])

    def test_both_skills_copy_when_enabled(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "BENCH_GOAL_PLUS_REPOSVUL_TASK_SKILLS": "1",
                "BENCH_GOAL_PLUS_REPOSVUL_REA_TOOLS": "1",
                "BENCH_GOAL_PLUS_REA_ROOT": "/home/tangzhiyun/workspace/rea",
            },
        ):
            tracked, workspace = self._materialized_tracked_files()
        skills = {name for name in tracked if name.startswith(".agents/skills/")}
        self.assertIn(".agents/skills/path-validation/SKILL.md", skills)
        self.assertIn(".agents/skills/rea-analysis/SKILL.md", skills)

    def test_knowledge_template_routes_reposvul_to_audit_template(self) -> None:
        from experiments.benchmark_compare.experiment import (
            SHARED_KNOWLEDGE_SPEC_TEMPLATE,
            shared_knowledge_template,
        )

        self.assertIs(
            shared_knowledge_template({"benchmark_adapter": "reposvul"}),
            SHARED_KNOWLEDGE_SPEC_TEMPLATE,
        )
