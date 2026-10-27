from __future__ import annotations

import json
import unittest
from pathlib import Path

from adapters.frontier_cs2 import adapter


ROOT = Path(__file__).resolve().parents[1]

EVAL_RESULT = {
    "metrics": {"score_unbounded": 51.9608723673069},
    "private_metrics": {
        "aggregation": {"kind": "conjunction"},
        "beat_spec_progress": {
            "build_ns": {"failed": 4, "passed": 140, "total": 144},
            "effective_fpr": {"failed": 0, "passed": 144, "total": 144},
            "lookup_ns": {"failed": 85, "passed": 59, "total": 144},
            "resident_bytes": {"failed": 0, "passed": 144, "total": 144},
            "space_overhead_pct": {"failed": 0, "passed": 144, "total": 144},
        },
        "beats_reference": 0,
        "margin": 1.960872367306898,
        "reference_score": 50.0,
    },
    "raw_score": 51.9608723673069,
    "status": "passed",
    "summary": "valid; strict FAIL",
}

TRAIN_RESULT = {
    "metrics": {
        "beat_spec_progress": {
            "build_ns": {"failed": 0, "passed": 72, "total": 72},
            "effective_fpr": {"failed": 0, "passed": 72, "total": 72},
            "lookup_ns": {"failed": 39, "passed": 33, "total": 72},
            "resident_bytes": {"failed": 0, "passed": 72, "total": 72},
            "space_overhead_pct": {"failed": 0, "passed": 72, "total": 72},
        },
        "score_unbounded": 52.6119547564908,
    },
    "raw_score": 52.6119547564908,
    "status": "passed",
    "summary": "valid; strict FAIL",
}


class FrontierCS2RegistryContractTest(unittest.TestCase):
    def test_registry_entry_matches_contract(self) -> None:
        data = json.loads((ROOT / "benchmarks/registry.json").read_text())
        entry = next(it for it in data["items"] if it["id"] == "frontier-cs-2")
        self.assertEqual(entry["kind"], "benchmark")
        self.assertEqual(entry["gate_set"], "benchmark")
        self.assertEqual(entry["docker_requirement"], "required")
        self.assertEqual(
            sorted(entry["stages"]), sorted(data["gate_sets"]["benchmark"])
        )
        for stage, status in entry["stages"].items():
            if status != "todo":
                evidence = entry.get("stage_evidence", {}).get(stage, [])
                self.assertTrue(
                    evidence, f"stage {stage} claims {status} without evidence"
                )
                for relative in evidence:
                    self.assertTrue((ROOT / relative).is_file(), relative)

    def test_upstreams_entry_matches_registry(self) -> None:
        registry = json.loads((ROOT / "benchmarks/registry.json").read_text())
        entry = next(it for it in registry["items"] if it["id"] == "frontier-cs-2")
        upstreams = json.loads(
            (ROOT / "environment/upstreams.json").read_text()
        )["upstreams"]
        upstream = upstreams[adapter.UPSTREAM_KEY]
        self.assertEqual(upstream["checkout_dir"], Path(entry["local_path"]).name)
        self.assertEqual(
            upstream["tracking_branch"], entry["repositories"][0]["tracking_branch"]
        )
        self.assertIn("FrontierCS-2", entry["repositories"][0]["upstream_url"])
        self.assertIn("FrontierCS-2", upstream["repository"])


class FrontierCS2AdapterSurfaceTest(unittest.TestCase):
    def test_surface(self) -> None:
        for name in (
            "discover_tasks",
            "resolve_task_dir",
            "build_verifier_image",
            "prepare_workspace",
            "make_submission",
            "run_verify",
            "parse_result",
        ):
            self.assertTrue(callable(getattr(adapter, name, None)), name)
        self.assertEqual(adapter.PRIMARY_METRIC, "score_unbounded")
        self.assertEqual(adapter.DIRECTION, "maximize")

    def test_default_task_layout_if_checkout_present(self) -> None:
        checkout = adapter.checkout_root()
        if not checkout.is_dir():
            self.skipTest("third_party/frontier-cs-2 not provisioned")
        task = adapter.resolve_task_dir(checkout, adapter.DEFAULT_TASK)
        for relative in (
            "policy.json",
            "judge/evaluator.py",
            "judge/reference.patch",
            "judge/beat_spec.json",
            "verifier/Dockerfile",
            "verifier/verify.py",
            "agent-base/Dockerfile",
            "datasets/train.json",
            "datasets/eval.json",
        ):
            self.assertTrue((task / relative).is_file(), relative)


class FrontierCS2ResultParsingTest(unittest.TestCase):
    def test_parse_eval_result(self) -> None:
        parsed = adapter.parse_result(EVAL_RESULT)
        self.assertEqual(parsed["status"], "passed")
        self.assertIn("strict FAIL", parsed["summary"])
        self.assertAlmostEqual(parsed["score_unbounded"], 51.9608723673069)
        self.assertEqual(parsed["beats_reference"], 0)
        self.assertAlmostEqual(parsed["margin"], 1.960872367306898)
        self.assertEqual(parsed["beat_spec_progress"]["lookup_ns"]["failed"], 85)

    def test_parse_train_result(self) -> None:
        parsed = adapter.parse_result(TRAIN_RESULT)
        self.assertIn("beat_spec_progress", parsed)
        self.assertEqual(parsed["beat_spec_progress"]["build_ns"]["passed"], 72)
        self.assertAlmostEqual(parsed["score_unbounded"], 52.6119547564908)


if __name__ == "__main__":
    unittest.main()
