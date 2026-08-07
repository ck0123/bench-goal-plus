from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("status", ROOT / "scripts/status.py")
assert SPEC and SPEC.loader
STATUS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STATUS)


class RegistryTest(unittest.TestCase):
    def test_registry_is_valid(self) -> None:
        self.assertEqual(STATUS.validate(STATUS.load_registry()), [])

    def test_dataset_catalog_is_valid(self) -> None:
        self.assertEqual(STATUS.validate_datasets(), [])

    def test_every_item_has_explicit_docker_requirement(self) -> None:
        data = STATUS.load_registry()
        requirements = {
            item["id"]: item["docker_requirement"] for item in data["items"]
        }
        self.assertEqual(requirements["ale-bench-lite"], "required")
        self.assertEqual(requirements["heurigym"], "not_required")
        self.assertEqual(requirements["autolab-cpu"], "mixed")
        self.assertEqual(requirements["edgebench"], "required")
        self.assertEqual(requirements["swe-bench-verified"], "required")
        self.assertEqual(requirements["openevolve"], "not_required")

    def test_swe_bench_readiness_keeps_methods_separate(self) -> None:
        data = STATUS.load_registry()
        item = next(
            item for item in data["items"] if item["id"] == "swe-bench-verified"
        )

        self.assertEqual(item["gate_set"], "benchmark_methods")
        self.assertEqual(
            set(item["stages"]), set(data["gate_sets"]["benchmark_methods"])
        )
        self.assertEqual(item["stages"]["plain_codex"], "pass")
        self.assertEqual(item["stages"]["plain_pi"], "pass")
        self.assertEqual(item["stages"]["goal_plus_codex"], "n/a")
        self.assertEqual(item["stages"]["goal_plus_pi"], "pass")
        self.assertEqual(item["stages"]["official_verifier"], "pass")
        self.assertEqual(item["stages"]["campaign_ready"], "partial")
        self.assertEqual(
            set(item["stage_evidence"]),
            {"official_verifier", "plain_codex", "plain_pi", "goal_plus_pi"},
        )
        self.assertTrue(item["evidence"])
        for relative_path in item["evidence"]:
            self.assertTrue((ROOT / relative_path).is_file(), relative_path)

        for stage, method in {
            "plain_codex": "plain-codex",
            "plain_pi": "plain-pi",
            "goal_plus_pi": "goal-plus-pi",
        }.items():
            paths = item["stage_evidence"][stage]
            summary_path = next(path for path in paths if path.endswith("summary.json"))
            summary = json.loads((ROOT / summary_path).read_text())
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["method"]["id"], method)
            self.assertEqual(
                {key: summary["budget"][key] for key in ("T", "K", "C", "R")},
                {"T": 1800, "K": 1, "C": 1, "R": 1},
            )
            self.assertEqual(summary["execution"]["official_evaluator_calls"], 1)
            self.assertTrue(summary["execution"]["agent_container_removed"])
            self.assertTrue(summary["result"]["score_valid"])
            self.assertTrue(summary["result"]["patch_non_empty"])
            self.assertIs(summary["result"]["resolved"], True)
            self.assertIs(summary["result"]["patch_successfully_applied"], True)

        for relative_path in item["stage_evidence"]["official_verifier"]:
            report = json.loads((ROOT / relative_path).read_text())
            result = report["sympy__sympy-16886"]
            self.assertIs(result["resolved"], True)
            self.assertIs(result["patch_successfully_applied"], True)

    def test_benchmark_method_pass_requires_stage_evidence(self) -> None:
        data = STATUS.load_registry()
        item = next(
            item for item in data["items"] if item["id"] == "swe-bench-verified"
        )
        del item["stage_evidence"]["plain_pi"]

        self.assertIn(
            "swe-bench-verified.plain_pi: pass requires method-specific stage_evidence",
            STATUS.validate(data),
        )

    def test_swe_bench_plain_codex_c2_evidence_is_explicit(self) -> None:
        data = STATUS.load_registry()
        item = next(
            item for item in data["items"] if item["id"] == "swe-bench-verified"
        )
        summary_path = next(
            path
            for path in item["stage_evidence"]["plain_codex"]
            if "plain-codex-terra-c2/summary.json" in path
        )
        summary = json.loads((ROOT / summary_path).read_text())

        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["method"]["id"], "plain-codex")
        self.assertEqual(
            {key: summary["budget"][key] for key in ("T", "K", "C", "R")},
            {"T": 1800, "K": 1, "C": 2, "R": 1},
        )
        self.assertEqual(summary["scheduler"]["max_active_observed"], 2)
        self.assertEqual(len(summary["scheduler"]["simultaneous_cell_ids"]), 2)
        self.assertEqual(summary["execution"]["official_evaluator_calls"], 2)
        self.assertEqual(summary["result"]["evaluated_count"], 2)
        self.assertEqual(summary["result"]["resolved_count"], 2)
        self.assertTrue(summary["result"]["score_valid"])
        self.assertTrue(
            all(cell["patch_successfully_applied"] for cell in summary["result"]["cells"])
        )

    def test_stage_evidence_must_be_in_flat_evidence_list(self) -> None:
        data = STATUS.load_registry()
        item = next(
            item for item in data["items"] if item["id"] == "swe-bench-verified"
        )
        path = item["stage_evidence"]["plain_codex"][0]
        item["evidence"].remove(path)

        self.assertIn(
            "swe-bench-verified.plain_codex: stage evidence is absent from the "
            f"item evidence list: {path}",
            STATUS.validate(data),
        )


if __name__ == "__main__":
    unittest.main()
