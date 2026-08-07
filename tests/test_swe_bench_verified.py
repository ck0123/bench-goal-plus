from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bench_goal_plus.application import BenchmarkAgent
from bench_goal_plus.catalog import Catalog
from bench_goal_plus.errors import ContractError
from bench_goal_plus.loopback_bridge import bridged_url, loopback_target
from bench_goal_plus.runners.factory import create_runner
from bench_runtime_paths import ensure_temp_root
from experiments.swe_bench_verified import (
    environment,
    goal_plus_evidence,
    reporting,
    runtime,
)
from experiments.swe_bench_verified.config import (
    SweBenchContractError,
    load_profile,
    managed_upstream_branch,
    read_json,
    resolve_profile,
    validate_profile,
    write_json,
)
from scripts import benchmark_report


class SweBenchVerifiedContractTest(unittest.TestCase):
    def temporary_directory(self) -> tempfile.TemporaryDirectory[str]:
        return tempfile.TemporaryDirectory(dir=ensure_temp_root("test-swe-bench-verified"))

    def profile(self, profile_id: str) -> dict:
        return load_profile(profile_id)[1]

    def test_visible_verifier_propagates_test_failure_with_diagnostics(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(environment.GOAL_PLUS_VISIBLE_VERIFIER),
                "--",
                sys.executable,
                "-c",
                "import sys; print('public failure'); sys.exit(7)",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        payload = json.loads(completed.stdout)

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(payload["visible_test_score"], 0.0)
        self.assertEqual(payload["test_returncode"], 7)
        self.assertEqual(payload["failure_kind"], "test_command_failed")
        self.assertIn("public failure", payload["stdout_tail"])

        ranking = subprocess.run(
            [
                sys.executable,
                str(environment.GOAL_PLUS_VISIBLE_VERIFIER),
                "--ranking-signal",
                "--",
                sys.executable,
                "-c",
                "import sys; sys.exit(7)",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(ranking.returncode, 0)
        self.assertEqual(json.loads(ranking.stdout)["visible_test_score"], 0.0)

        passing = subprocess.run(
            [
                sys.executable,
                str(environment.GOAL_PLUS_VISIBLE_VERIFIER),
                "--",
                sys.executable,
                "-c",
                "print('public pass')",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(passing.returncode, 0)
        self.assertEqual(json.loads(passing.stdout)["visible_test_score"], 1.0)

    def test_goal_plus_installer_uses_the_bind_cache_owner(self) -> None:
        script = environment.goal_plus_install_script()

        self.assertIn("os.stat('/opt/pip-cache')", script)
        self.assertIn("os.setgid(cache.st_gid)", script)
        self.assertIn("os.setuid(cache.st_uid)", script)
        self.assertIn("'/opt/goal-plus-runtime-requirements.lock'", script)
        self.assertIn("PATH=/opt/goal-plus-bin:/opt/node/bin:$PATH", script)
        self.assertNotIn("PATH", environment.goal_plus_runtime_environment())
        self.assertEqual(
            environment.goal_plus_runtime_environment()["HOME"],
            "/opt/agent-tmp",
        )

        codex_script = environment.goal_plus_install_script(include_pi=False)
        self.assertIn("goal_plus.server", codex_script)
        self.assertNotIn("/opt/pi/dist", codex_script)
        self.assertNotIn("/opt/goal-plus-bin/pi", codex_script)

    def test_goal_plus_codex_profile_uses_native_auth_and_codex_workers(self) -> None:
        profile = self.profile("sympy-16886-goal-plus-codex-acceptance-smoke")
        self.assertEqual(profile["methods"], ["goal-plus-codex"])
        self.assertNotIn("agent_provider", profile)

        prompt = runtime.build_goal_plus_prompt(
            {"problem_statement": "Public issue text"}, profile
        )
        command = runtime._agent_command(
            "container-id",
            profile,
            {"outer_deadline_at": "2026-08-04T12:00:00+00:00"},
        )
        joined = " ".join(command)
        self.assertIn("strategy.worker_host=codex", prompt)
        self.assertIn("strategy.config.seed=1", prompt)
        self.assertIn(
            "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED=1", command
        )
        self.assertIn("gpt-5.6-sol", command)
        self.assertNotIn("OPENAI_API_KEY", joined)
        self.assertNotIn("agents.enabled", joined)
        self.assertEqual(command[-1], "-")

    def test_goal_plus_codex_ablation_uses_the_frozen_responses_provider(
        self,
    ) -> None:
        enabled = self.profile(
            "sympy-16886-goal-plus-codex-luna-high-acceptance-on-smoke"
        )
        disabled = self.profile(
            "sympy-16886-goal-plus-codex-luna-high-acceptance-off-smoke"
        )
        self.assertTrue(enabled["goal_plus"]["supplemental_evaluation_enabled"])
        self.assertFalse(disabled["goal_plus"]["supplemental_evaluation_enabled"])

        enabled_without_ablation = json.loads(json.dumps(enabled))
        disabled_without_ablation = json.loads(json.dumps(disabled))
        enabled_without_ablation["id"] = "ablation"
        disabled_without_ablation["id"] = "ablation"
        enabled_without_ablation["goal_plus"].pop("supplemental_evaluation_enabled")
        disabled_without_ablation["goal_plus"].pop("supplemental_evaluation_enabled")
        self.assertEqual(enabled_without_ablation, disabled_without_ablation)
        self.assertEqual(
            runtime.build_goal_plus_prompt(
                {"problem_statement": "Public issue text"}, enabled
            ),
            runtime.build_goal_plus_prompt(
                {"problem_statement": "Public issue text"}, disabled
            ),
        )

        with mock.patch.dict(
            os.environ,
            {
                "OPENAI_BASE_URL": "http://127.0.0.1:3788/v1",
                "OPENAI_API_KEY": "openai-secret",
            },
            clear=False,
        ):
            resolved = environment.resolve_goal_plus_codex_runtime(enabled)
        resolved["runtime_api_base_url"] = "http://192.0.2.10:45678/v1"
        resolved["bridge_host"] = "192.0.2.10"
        resolved["outer_deadline_at"] = "2026-08-04T12:00:00+00:00"
        command = runtime._agent_command("container-id", enabled, resolved)
        joined = " ".join(command)
        self.assertIn("OPENAI_API_KEY", command)
        self.assertIn('model_provider="bench_proxy"', command)
        self.assertIn("http://192.0.2.10:45678/v1", joined)
        self.assertNotIn("openai-secret", joined)
        self.assertIsNone(resolved["auth_file"])

    def test_goal_plus_controller_drains_views_before_search_selection(self) -> None:
        controller = environment.GOAL_PLUS_CONTROLLER.read_text(encoding="utf-8")
        drain = controller.index(
            "annotated_in_closeout = drain_evidence_annotations("
        )
        selection = controller.index("selection = tools.search_select(run_id)")
        existing_promotion = controller.index("if existing_promotion is not None:")

        self.assertLess(drain, selection)
        self.assertLess(drain, existing_promotion)
        self.assertIn("wait_for_retries=True", controller[drain:selection])

    def write_goal_plus_state(
        self,
        root: Path,
        *,
        max_parallel: int = 1,
        session_count: int = 1,
        verifier_runs: int = 1,
        supplemental_evaluation: bool = False,
        evidence_annotations: bool = False,
        worker_host: str = "pi-rpc",
        worker_min_runtime_seconds: int | None = None,
        worker_min_verifier_runs: int | None = None,
        candidate_count: int = 1,
        peer_comparison: bool = False,
    ) -> None:
        run_id = "run_test"
        candidate_ids = [f"c{index + 1:03d}" for index in range(candidate_count)]
        candidate_id = candidate_ids[0]
        candidate_commits = {
            candidate: chr(ord("a") + index) * 40
            for index, candidate in enumerate(candidate_ids)
        }
        write_json(
            root / "goal-plus/gp_test/goal.json",
            {
                "goal_plus_id": "gp_test",
                "status": "complete",
                "phase": "done",
                "linked_search": {"run_id": run_id},
            },
        )
        write_json(
            root / f"runs/{run_id}/run.json",
            {
                "run_id": run_id,
                "state": "promoted",
                "frozen_spec_id": "spec_test",
                "selected_candidate_id": candidate_id,
            },
        )
        spec = {
            "budget": {"max_parallel": max_parallel},
            "strategy": {
                "worker_host": worker_host,
                "orchestration_mode": "parallel_loops",
                "worker_budget": {
                    "max_runtime_seconds": 1500,
                    **(
                        {
                            "min_runtime_seconds": worker_min_runtime_seconds,
                            "min_verifier_runs": worker_min_verifier_runs,
                        }
                        if worker_min_runtime_seconds is not None
                        else {}
                    ),
                },
                "config": {"closeout_reserve_seconds": 300},
                **(
                    {
                        "evidence_annotator": {
                            "host": "codex",
                            "model": None,
                            "reasoning_effort": None,
                            "timeout_seconds": 300,
                            "provider": None,
                            "pi_provider": None,
                        }
                    }
                    if evidence_annotations
                    else {}
                ),
            },
            "process_verifiers": [
                {
                    "name": "visible",
                    "role": "ranking_signal",
                    "command": [
                        "python",
                        ".goal-plus-verifiers/visible_test_verifier.py",
                        "--ranking-signal",
                        "--timeout-seconds",
                        "300",
                        "--",
                        "python",
                        "-m",
                        "pytest",
                    ],
                }
            ],
            "promotion_verifiers": [
                {
                    "name": "visible-promotion",
                    "role": "promotion_gate",
                    "command": [
                        "python",
                        ".goal-plus-verifiers/visible_test_verifier.py",
                        "--timeout-seconds",
                        "300",
                        "--",
                        "python",
                        "-m",
                        "pytest",
                    ],
                }
            ],
        }
        write_json(
            root / "specs/spec_test/frozen_spec.json",
            {
                "spec": spec,
                "verifier_hashes": {
                    ".goal-plus-verifiers/visible_test_verifier.py": (
                        goal_plus_evidence.expected_visible_verifier_sha256()
                    )
                },
            },
        )
        candidate_iterations: dict[str, list[dict]] = {}
        for index, current_candidate_id in enumerate(candidate_ids):
            iterations = [
                {
                    "iteration": 1,
                    "score": 1.0,
                    "agent_session_id": f"agent_{index}",
                    "created_at": "2026-08-06T12:00:00Z",
                    "git_head": candidate_commits[current_candidate_id],
                }
            ]
            if peer_comparison and index == 0:
                iterations.append(
                    {
                        "iteration": 2,
                        "score": 1.0,
                        "agent_session_id": "agent_0",
                        "created_at": "2026-08-06T12:02:00Z",
                        "git_head": "f" * 40,
                    }
                )
            candidate_iterations[current_candidate_id] = iterations
            payload = {
                "candidate_id": current_candidate_id,
                "iterations": iterations,
            }
            if current_candidate_id == candidate_id:
                payload["promotion_report"] = {
                    "promotion_passed": True,
                    "aggregate_score": 1.0,
                    "verifier_results": [
                        {"metrics": {"visible_test_score": 1.0}}
                    ],
                }
            write_json(
                root
                / f"runs/{run_id}/candidates/{current_candidate_id}/candidate.json",
                payload,
            )

        if evidence_annotations:
            for current_candidate_id, iterations in candidate_iterations.items():
                peer_ids = [
                    peer for peer in candidate_ids if peer != current_candidate_id
                ]
                comparison_basis = (
                    [
                        {
                            "candidate_id": peer,
                            "iteration": 1,
                            "commit": candidate_commits[peer],
                        }
                        for peer in peer_ids
                    ]
                    if peer_comparison
                    else []
                )
                comparisons = [
                    {
                        **reference,
                        "relation": "different",
                        "rationale": "The candidates implement distinct public hypotheses.",
                        "evidence": ["The cumulative diffs use different branches."],
                    }
                    for reference in comparison_basis
                ]
                evaluation = (
                    {
                        "summary": "The implementation changes the requested behavior.",
                        "dimensions": [
                            {
                                "name": "Behavioral implementation",
                                "finding": "The cumulative diff changes the target behavior.",
                                "confidence": "high",
                                "evidence": ["Persisted test evidence."],
                            }
                        ],
                        "comparisons": comparisons,
                        "limitations": ["No hidden test evidence is available."],
                    }
                    if supplemental_evaluation
                    else None
                )
                for iteration in iterations:
                    write_json(
                        root
                        / (
                            f"runs/{run_id}/candidates/{current_candidate_id}/"
                            "evidence-annotations/"
                            f"iteration-{iteration['iteration']:04d}.json"
                        ),
                        {
                            "candidate_id": current_candidate_id,
                            "iteration": iteration["iteration"],
                            "state": "completed",
                            "profile": {"host": "codex"},
                            "task_context_source": "goal_plus_raw_goal",
                            "task_context_ref": "goal_plus:gp_test:revision:1",
                            "task_context_sha256": hashlib.sha256(
                                b"Public issue text"
                            ).hexdigest(),
                            "supplemental_evaluation_enabled": (
                                supplemental_evaluation
                            ),
                            "comparison_basis": comparison_basis,
                            "usage": {"input_tokens": 40, "output_tokens": 10},
                            "view": {
                                "description": (
                                    "Independent description of candidate evidence."
                                ),
                                "supplemental_evaluation": evaluation,
                                "comparison_basis": comparison_basis,
                                "acceptance_view": None,
                            },
                        },
                    )
        for index in range(session_count):
            agent_session_id = f"agent_{index}"
            session_candidate_id = candidate_ids[index % len(candidate_ids)]
            completed_views = [
                {
                    "candidate_id": current_candidate_id,
                    "iteration": 1,
                    "commit": candidate_commits[current_candidate_id],
                    "view_created_at": "2026-08-06T12:00:30Z",
                    "supplemental_evaluation_present": supplemental_evaluation,
                }
                for current_candidate_id in (
                    candidate_ids if peer_comparison else [session_candidate_id]
                )
            ]
            write_json(
                root / f"runs/{run_id}/agent_sessions/{agent_session_id}.json",
                {
                    "agent_session_id": agent_session_id,
                    "created_at": "2026-08-06T12:00:00Z",
                    "updated_at": "2026-08-06T12:10:00Z",
                    "host": worker_host,
                    "candidate_id": session_candidate_id,
                    "host_handle": {
                        "external_id": f"agent_{index}",
                        "metadata": {
                            "pi_metrics": {
                                "usage_total": {"input_tokens": 10 + index}
                            }
                        },
                    },
                    "counters": {"verifier_runs": verifier_runs},
                    "global_evidence_reads": (
                        [
                            {
                                "read_at": "2026-08-06T12:01:00Z",
                                "evidence_count": len(completed_views),
                                "completed_view_count": len(completed_views),
                                "completed_supplemental_evaluation_count": (
                                    len(completed_views)
                                    if supplemental_evaluation
                                    else 0
                                ),
                                "completed_views": completed_views,
                            }
                        ]
                        if evidence_annotations
                        else []
                    ),
                },
            )
            if worker_min_runtime_seconds is not None:
                write_json(
                    root
                    / "host-logs"
                    / "codex-autoresearch-leases"
                    / f"{agent_session_id}.json",
                    {
                        "status": "released",
                        "release_reason": "lease_satisfied",
                        "agent_session_id": agent_session_id,
                        "candidate_id": session_candidate_id,
                        "started_at": "2026-08-06T12:00:00Z",
                        "released_at": "2026-08-06T12:10:00Z",
                        "elapsed_seconds": worker_min_runtime_seconds,
                        "min_runtime_seconds": worker_min_runtime_seconds,
                        "min_verifier_runs": worker_min_verifier_runs,
                        "verifier_runs": verifier_runs,
                    },
                )
        promotion = root / f"runs/{run_id}/promotion/{candidate_id}.patch"
        promotion.parent.mkdir(parents=True, exist_ok=True)
        promotion.write_text("diff --git a/a b/a\n", encoding="utf-8")

    def test_goal_plus_codex_completion_accepts_codex_worker_sessions(self) -> None:
        with self.temporary_directory() as temporary:
            root = Path(temporary)
            self.write_goal_plus_state(root, worker_host="codex")
            state = goal_plus_evidence.collect_goal_plus_state(
                root,
                expected_k=1,
                expected_worker_runtime_seconds=1500,
                expected_closeout_reserve_seconds=300,
                expected_visible_verifier_timeout_seconds=300,
                expected_worker_host="codex",
            )

        self.assertTrue(state["completion"]["passed"])
        self.assertEqual(
            state["completion"]["checks"]["worker_topology"]["actual"],
            "codex/parallel_loops",
        )

    def test_goal_plus_k2_requires_peer_comparison_and_search_influence(self) -> None:
        with self.temporary_directory() as temporary:
            root = Path(temporary) / "passing"
            self.write_goal_plus_state(
                root,
                max_parallel=2,
                session_count=2,
                verifier_runs=2,
                supplemental_evaluation=True,
                evidence_annotations=True,
                worker_host="codex",
                worker_min_runtime_seconds=600,
                worker_min_verifier_runs=2,
                candidate_count=2,
                peer_comparison=True,
            )
            state = goal_plus_evidence.collect_goal_plus_state(
                root,
                expected_k=2,
                expected_worker_runtime_seconds=1500,
                expected_closeout_reserve_seconds=300,
                expected_visible_verifier_timeout_seconds=300,
                expected_worker_min_runtime_seconds=600,
                expected_worker_min_verifier_runs=2,
                expected_supplemental_evaluation_enabled=True,
                expected_evidence_annotator_enabled=True,
                expected_worker_host="codex",
            )

            self.assertTrue(state["completion"]["passed"])
            self.assertEqual(state["actual_subagent_count"], 2)
            self.assertTrue(
                state["completion"]["checks"]["dynamic_peer_comparison"][
                    "passed"
                ]
            )
            influence = state["completion"]["checks"]["peer_view_influence"]
            self.assertTrue(influence["passed"])
            self.assertEqual(
                influence["actual"]["peer_reads_before_subsequent_verifier"],
                1,
            )
            overlap = state["completion"]["checks"]["live_worker_overlap"]
            self.assertTrue(overlap["passed"])
            self.assertEqual(overlap["actual"]["overlap_seconds"], 600.0)

            second_lease_path = (
                root
                / "host-logs/codex-autoresearch-leases/agent_1.json"
            )
            second_lease = read_json(second_lease_path)
            second_lease["started_at"] = "2026-08-06T12:11:00Z"
            second_lease["released_at"] = "2026-08-06T12:21:00Z"
            write_json(second_lease_path, second_lease)
            serialized_state = goal_plus_evidence.collect_goal_plus_state(
                root,
                expected_k=2,
                expected_worker_runtime_seconds=1500,
                expected_closeout_reserve_seconds=300,
                expected_visible_verifier_timeout_seconds=300,
                expected_worker_min_runtime_seconds=600,
                expected_worker_min_verifier_runs=2,
                expected_supplemental_evaluation_enabled=True,
                expected_evidence_annotator_enabled=True,
                expected_worker_host="codex",
            )
            self.assertFalse(
                serialized_state["completion"]["checks"]["live_worker_overlap"][
                    "passed"
                ]
            )
            second_lease["started_at"] = "2026-08-06T12:00:00Z"
            second_lease["released_at"] = "2026-08-06T12:10:00Z"
            write_json(second_lease_path, second_lease)
            self.assertEqual(
                len(influence["actual"]["valid_peer_influence_windows"]),
                1,
            )

            session_path = root / "runs/run_test/agent_sessions/agent_0.json"
            tampered_session = read_json(session_path)
            tampered_session["global_evidence_reads"][0]["completed_views"][1][
                "commit"
            ] = "0" * 40
            write_json(session_path, tampered_session)
            tampered_state = goal_plus_evidence.collect_goal_plus_state(
                root,
                expected_k=2,
                expected_worker_runtime_seconds=1500,
                expected_closeout_reserve_seconds=300,
                expected_visible_verifier_timeout_seconds=300,
                expected_worker_min_runtime_seconds=600,
                expected_worker_min_verifier_runs=2,
                expected_supplemental_evaluation_enabled=True,
                expected_evidence_annotator_enabled=True,
                expected_worker_host="codex",
            )
            self.assertTrue(
                tampered_state["completion"]["checks"][
                    "dynamic_peer_comparison"
                ]["passed"]
            )
            self.assertFalse(
                tampered_state["completion"]["checks"]["peer_view_influence"][
                    "passed"
                ]
            )

            missing = Path(temporary) / "missing-peer"
            self.write_goal_plus_state(
                missing,
                max_parallel=2,
                session_count=2,
                verifier_runs=2,
                supplemental_evaluation=True,
                evidence_annotations=True,
                worker_host="codex",
                worker_min_runtime_seconds=600,
                worker_min_verifier_runs=2,
                candidate_count=2,
            )
            missing_state = goal_plus_evidence.collect_goal_plus_state(
                missing,
                expected_k=2,
                expected_worker_runtime_seconds=1500,
                expected_closeout_reserve_seconds=300,
                expected_visible_verifier_timeout_seconds=300,
                expected_worker_min_runtime_seconds=600,
                expected_worker_min_verifier_runs=2,
                expected_supplemental_evaluation_enabled=True,
                expected_evidence_annotator_enabled=True,
                expected_worker_host="codex",
            )
            self.assertFalse(missing_state["completion"]["passed"])
            self.assertIn(
                "dynamic_peer_comparison", missing_state["completion"]["reason"]
            )
            self.assertIn(
                "peer_view_influence", missing_state["completion"]["reason"]
            )

    def test_goal_plus_codex_completion_enforces_worker_minimums(self) -> None:
        with self.temporary_directory() as temporary:
            root = Path(temporary)
            self.write_goal_plus_state(
                root,
                worker_host="codex",
                verifier_runs=2,
                worker_min_runtime_seconds=600,
                worker_min_verifier_runs=2,
            )
            state = goal_plus_evidence.collect_goal_plus_state(
                root,
                expected_k=1,
                expected_worker_runtime_seconds=1500,
                expected_closeout_reserve_seconds=300,
                expected_visible_verifier_timeout_seconds=300,
                expected_worker_min_runtime_seconds=600,
                expected_worker_min_verifier_runs=2,
                expected_worker_host="codex",
            )

            self.assertTrue(state["completion"]["passed"])
            self.assertTrue(
                state["completion"]["checks"]["worker_minimum_observed"][
                    "passed"
                ]
            )

            session_path = (
                root
                / "runs/run_test/agent_sessions/agent_0.json"
            )
            session = read_json(session_path)
            session["updated_at"] = "2026-08-06T12:10:01Z"
            write_json(session_path, session)

            lease_path = (
                root
                / "host-logs"
                / "codex-autoresearch-leases"
                / "agent_0.json"
            )
            lease = read_json(lease_path)
            lease.update(
                {
                    "status": "active",
                    "started_at": "2026-08-06T12:00:00Z",
                    "elapsed_seconds": 544,
                    "verifier_runs": 2,
                }
            )
            lease.pop("release_reason")
            write_json(lease_path, lease)
            recovered = goal_plus_evidence.collect_goal_plus_state(
                root,
                expected_k=1,
                expected_worker_runtime_seconds=1500,
                expected_closeout_reserve_seconds=300,
                expected_visible_verifier_timeout_seconds=300,
                expected_worker_min_runtime_seconds=600,
                expected_worker_min_verifier_runs=2,
                expected_worker_host="codex",
            )
            observation = recovered["completion"]["checks"][
                "worker_minimum_observed"
            ]
            self.assertTrue(observation["passed"])
            self.assertEqual(
                observation["actual"]["leases"][0]["minimum_observation"][
                    "basis"
                ],
                "terminal_session_timestamps",
            )

            lease["verifier_runs"] = 1
            write_json(lease_path, lease)
            session["counters"]["verifier_runs"] = 1
            write_json(session_path, session)
            failed = goal_plus_evidence.collect_goal_plus_state(
                root,
                expected_k=1,
                expected_worker_runtime_seconds=1500,
                expected_closeout_reserve_seconds=300,
                expected_visible_verifier_timeout_seconds=300,
                expected_worker_min_runtime_seconds=600,
                expected_worker_min_verifier_runs=2,
                expected_worker_host="codex",
            )
            self.assertFalse(failed["completion"]["passed"])
            self.assertIn("worker_minimum_observed", failed["completion"]["reason"])

    def test_finalize_revalidates_goal_plus_codex_with_codex_workers(self) -> None:
        profile = self.profile(
            "sympy-16886-goal-plus-codex-luna-high-acceptance-on-smoke"
        )
        with self.temporary_directory() as temporary:
            campaign = Path(temporary)
            state_root = campaign / "cells/goal-plus-codex/goal-plus-state"
            self.write_goal_plus_state(
                state_root,
                supplemental_evaluation=True,
                evidence_annotations=True,
                worker_host="codex",
            )
            patch_file = campaign / "cells/goal-plus-codex/model.patch"
            patch_file.write_text("diff --git a/a b/a\n", encoding="utf-8")
            manifest = {"profile_snapshot": profile}
            cell = {
                "method": "goal-plus-codex",
                "state": "partial",
                "task_file": "cells/goal-plus-codex/task.json",
                "patch_file": "cells/goal-plus-codex/model.patch",
                "agent": {
                    "runtime": {
                        "evidence_annotator": (
                            runtime._goal_plus_evidence_annotator_public(profile)
                        )
                    },
                    "goal_plus_closeout": {"completed": True},
                    "goal_plus": {
                        "completion": {"passed": False},
                        "export": {"completed": True},
                    },
                    "container": {"cleanup": {"removed": True}},
                },
                "evaluation": {
                    "state": "completed",
                    "calls": 1,
                    "resolved": True,
                    "patch_applied": True,
                },
            }

            changed = reporting._revalidate_goal_plus_cell(campaign, manifest, cell)

        self.assertTrue(changed)
        goal_plus = cell["agent"]["goal_plus"]
        self.assertTrue(goal_plus["completion"]["passed"])
        self.assertEqual(goal_plus["actual_subagent_count"], 1)
        self.assertEqual(
            goal_plus["completion"]["checks"]["worker_topology"]["actual"],
            "codex/parallel_loops",
        )

    def test_catalog_presets_freeze_methods_and_tkcr(self) -> None:
        agent = BenchmarkAgent(catalog=Catalog())
        codex = agent.resolve_spec(
            preset_id="swe-bench-verified-sympy-16886-codex-smoke"
        )
        pi = agent.resolve_spec(
            preset_id="swe-bench-verified-sympy-16886-pi-smoke"
        )
        goal_plus_pi = agent.resolve_spec(
            preset_id="swe-bench-verified-sympy-16886-goal-plus-pi-smoke"
        )
        luna_goal_plus_pi = agent.resolve_spec(
            preset_id=(
                "swe-bench-verified-sympy-16886-goal-plus-pi-luna-high-smoke"
            )
        )
        acceptance_off = agent.resolve_spec(
            preset_id="swe-bench-verified-sympy-16886-acceptance-view-off-smoke"
        )
        acceptance_on = agent.resolve_spec(
            preset_id="swe-bench-verified-sympy-16886-acceptance-view-on-smoke"
        )
        django_deepseek = agent.resolve_spec(
            preset_id=(
                "swe-bench-verified-django-13406-goal-plus-pi-"
                "deepseek-v4-flash-on"
            )
        )
        astropy_deepseek = agent.resolve_spec(
            preset_id=(
                "swe-bench-verified-astropy-13033-goal-plus-pi-"
                "deepseek-v4-flash-on"
            )
        )
        astropy_luna_k2_peer = agent.resolve_spec(
            preset_id=(
                "swe-bench-verified-astropy-13033-goal-plus-codex-"
                "luna-high-k2-peer-smoke"
            )
        )
        astropy_sol_k2_peer = agent.resolve_spec(
            preset_id=(
                "swe-bench-verified-astropy-13033-goal-plus-codex-"
                "sol-medium-k2-peer-smoke"
            )
        )

        self.assertEqual(codex.runner.runner_id, "swe-bench-native")
        self.assertEqual(codex.methods, ("plain-codex",))
        self.assertEqual(codex.concurrency(), {"T": 1800, "K": 1, "C": 1, "R": 1})
        self.assertEqual(pi.methods, ("plain-pi",))
        self.assertEqual(pi.model, "zai/glm-5.2")
        self.assertEqual(pi.concurrency(), {"T": 1800, "K": 1, "C": 1, "R": 1})
        self.assertEqual(goal_plus_pi.methods, ("goal-plus-pi",))
        self.assertEqual(goal_plus_pi.model, "zai/glm-5.2")
        self.assertEqual(
            goal_plus_pi.concurrency(), {"T": 1800, "K": 1, "C": 1, "R": 1}
        )
        self.assertEqual(luna_goal_plus_pi.methods, ("goal-plus-pi",))
        self.assertEqual(luna_goal_plus_pi.model, "bench-openai/gpt-5.6-luna")
        self.assertEqual(luna_goal_plus_pi.reasoning_effort, "high")
        self.assertEqual(
            luna_goal_plus_pi.concurrency(),
            {"T": 1800, "K": 1, "C": 1, "R": 1},
        )
        self.assertEqual(acceptance_off.methods, acceptance_on.methods)
        self.assertEqual(acceptance_off.model, acceptance_on.model)
        self.assertEqual(acceptance_off.reasoning_effort, acceptance_on.reasoning_effort)
        self.assertEqual(
            acceptance_off.concurrency(),
            acceptance_on.concurrency(),
        )
        for deepseek in (django_deepseek, astropy_deepseek):
            self.assertEqual(deepseek.methods, ("goal-plus-pi",))
            self.assertEqual(
                deepseek.model, "deepseek-responses/deepseek-v4-flash"
            )
            self.assertEqual(deepseek.reasoning_effort, "medium")
            self.assertEqual(
                deepseek.concurrency(), {"T": 1800, "K": 1, "C": 1, "R": 1}
            )
        for astropy_k2_peer, model, reasoning in (
            (astropy_luna_k2_peer, "gpt-5.6-luna", "high"),
            (astropy_sol_k2_peer, "gpt-5.6-sol", "medium"),
        ):
            self.assertEqual(astropy_k2_peer.methods, ("goal-plus-codex",))
            self.assertEqual(astropy_k2_peer.model, model)
            self.assertEqual(astropy_k2_peer.reasoning_effort, reasoning)
            self.assertEqual(
                astropy_k2_peer.concurrency(),
                {"T": 1800, "K": 2, "C": 1, "R": 1},
            )
        with self.assertRaisesRegex(ContractError, "preset.*is frozen"):
            agent.resolve_spec(
                preset_id=(
                    "swe-bench-verified-astropy-13033-goal-plus-codex-"
                    "luna-high-k2-peer-smoke"
                ),
                live_search_concurrency=1,
            )
        self.assertTrue(codex.runner.capabilities.official_evaluator)
        self.assertTrue(codex.runner.capabilities.retain_containers)
        self.assertFalse(codex.runner.capabilities.detach)
        self.assertEqual(codex.runner.evidence_filename, "campaign-summary.json")

    def test_profiles_pin_the_official_base_commit(self) -> None:
        expected = "c50643a49811e9fe2f4851adff4313ad46f7325e"

        self.assertEqual(
            self.profile("sympy-16886-codex-smoke")["tasks"][0]["base_commit"],
            expected,
        )
        self.assertEqual(
            self.profile("sympy-16886-pi-smoke")["tasks"][0]["base_commit"],
            expected,
        )
        self.assertEqual(
            self.profile("sympy-16886-goal-plus-pi-smoke")["tasks"][0][
                "base_commit"
            ],
            expected,
        )
        self.assertEqual(
            self.profile("sympy-16886-goal-plus-pi-luna-high-smoke")["tasks"][0][
                "base_commit"
            ],
            expected,
        )

    def test_goal_plus_profile_freezes_worker_and_closeout_budget(self) -> None:
        profile = self.profile("sympy-16886-goal-plus-pi-smoke")
        self.assertEqual(
            profile["goal_plus"],
            {
                "worker_runtime_seconds": 1500,
                "closeout_reserve_seconds": 300,
                "visible_verifier_timeout_seconds": 300,
                "evidence_annotator": "disabled",
                "supplemental_evaluation_enabled": False,
            },
        )

        invalid = json.loads(json.dumps(profile))
        invalid["goal_plus"]["worker_runtime_seconds"] = 1700
        with self.assertRaisesRegex(SweBenchContractError, "must fit T"):
            validate_profile(str(invalid["id"]), invalid)

        invalid = json.loads(json.dumps(profile))
        invalid["goal_plus"]["evidence_annotator"] = "enabled"
        with self.assertRaisesRegex(SweBenchContractError, "contract is invalid"):
            validate_profile(str(invalid["id"]), invalid)

    def test_codex_profile_freezes_custom_responses_auth(self) -> None:
        profile = self.profile("sympy-16886-codex-smoke")
        self.assertEqual(
            profile["agent_provider"],
            {
                "id": "bench_proxy",
                "name": "Benchmark OpenAI-compatible proxy",
                "auth_mode": "openai-compatible",
                "base_url_env": "OPENAI_BASE_URL",
                "api_key_env": "OPENAI_API_KEY",
                "wire_api": "responses",
            },
        )

        invalid = json.loads(json.dumps(profile))
        invalid["agent_provider"]["auth_mode"] = "oauth"
        with self.assertRaisesRegex(SweBenchContractError, "openai-compatible"):
            validate_profile(str(invalid["id"]), invalid)

    def test_codex_runtime_ignores_sforge_anthropic_environment(self) -> None:
        profile = self.profile("sympy-16886-codex-smoke")
        values = {
            "OPENAI_BASE_URL": "http://127.0.0.1:3788/v1",
            "OPENAI_API_KEY": "openai-secret",
            "SFORGE_AGENT_API_BASE_URL": "https://api.z.ai/api/anthropic",
            "SFORGE_AGENT_API_KEY": "anthropic-secret",
        }
        with mock.patch.dict(os.environ, values, clear=False):
            resolved = environment.resolve_codex_runtime(profile)

        self.assertEqual(resolved["base_url_env"], "OPENAI_BASE_URL")
        self.assertEqual(resolved["api_key_env"], "OPENAI_API_KEY")
        self.assertEqual(resolved["api_base_url"], values["OPENAI_BASE_URL"])
        self.assertNotIn("api_key", resolved)

    def test_luna_pi_profile_freezes_custom_responses_provider(self) -> None:
        profile = self.profile("sympy-16886-goal-plus-pi-luna-high-smoke")
        self.assertEqual(profile["model"], "bench-openai/gpt-5.6-luna")
        self.assertEqual(profile["reasoning_effort"], "high")
        self.assertEqual(
            profile["agent_provider"],
            {
                "id": "bench-openai",
                "name": "Benchmark OpenAI-compatible proxy",
                "auth_mode": "openai-compatible",
                "base_url_env": "OPENAI_BASE_URL",
                "api_key_env": "OPENAI_API_KEY",
                "wire_api": "responses",
            },
        )
        self.assertEqual(
            profile["goal_plus"]["evidence_annotator"],
            {
                "kind": "codex",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "high",
                "timeout_seconds": 300,
            },
        )

        invalid = json.loads(json.dumps(profile))
        invalid["agent_provider"]["id"] = "other-provider"
        with self.assertRaisesRegex(SweBenchContractError, "must match"):
            validate_profile(str(invalid["id"]), invalid)

    def test_supplemental_evaluation_profiles_only_switch_the_boolean(self) -> None:
        enabled = self.profile("sympy-16886-goal-plus-pi-luna-high-smoke")
        disabled = self.profile(
            "sympy-16886-goal-plus-pi-luna-high-acceptance-off-smoke"
        )

        self.assertTrue(enabled["goal_plus"]["supplemental_evaluation_enabled"])
        self.assertFalse(disabled["goal_plus"]["supplemental_evaluation_enabled"])
        enabled_without_ablation = json.loads(json.dumps(enabled))
        disabled_without_ablation = json.loads(json.dumps(disabled))
        enabled_without_ablation["id"] = "ablation"
        disabled_without_ablation["id"] = "ablation"
        enabled_without_ablation["goal_plus"].pop("supplemental_evaluation_enabled")
        disabled_without_ablation["goal_plus"].pop("supplemental_evaluation_enabled")
        self.assertEqual(enabled_without_ablation, disabled_without_ablation)
        task = {"problem_statement": "Public issue text"}
        self.assertEqual(
            runtime.build_goal_plus_prompt(task, enabled),
            runtime.build_goal_plus_prompt(task, disabled),
        )

        self.assertEqual(
            runtime._goal_plus_supplemental_evaluation_environment(enabled),
            {
                "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED": "1",
                "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED": "1",
            },
        )
        self.assertEqual(
            runtime._goal_plus_supplemental_evaluation_environment(disabled),
            {
                "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED": "0",
                "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED": "0",
            },
        )

        invalid = json.loads(json.dumps(enabled))
        invalid["goal_plus"]["supplemental_evaluation_enabled"] = "yes"
        with self.assertRaisesRegex(SweBenchContractError, "must be boolean"):
            validate_profile(str(invalid["id"]), invalid)

    def test_django_profiles_match_plain_models_and_use_medium_view_agent(self) -> None:
        pairs = (
            (
                "django-13406-goal-plus-codex-luna-high-acceptance-on-smoke",
                "django-13406-goal-plus-codex-luna-high-acceptance-off-smoke",
                "gpt-5.6-luna",
                "high",
            ),
            (
                "django-13406-goal-plus-codex-sol-medium-acceptance-on-smoke",
                "django-13406-goal-plus-codex-sol-medium-acceptance-off-smoke",
                "gpt-5.6-sol",
                "medium",
            ),
        )
        for enabled_id, disabled_id, model, reasoning in pairs:
            enabled = self.profile(enabled_id)
            disabled = self.profile(disabled_id)
            self.assertEqual(enabled["task_ids"], ["django__django-13406"])
            self.assertEqual(enabled["model"], model)
            self.assertEqual(enabled["reasoning_effort"], reasoning)
            self.assertEqual(
                enabled["goal_plus"]["evidence_annotator"]["reasoning_effort"],
                "medium",
            )
            self.assertTrue(enabled["goal_plus"]["supplemental_evaluation_enabled"])
            self.assertFalse(disabled["goal_plus"]["supplemental_evaluation_enabled"])
            enabled_without_ablation = json.loads(json.dumps(enabled))
            disabled_without_ablation = json.loads(json.dumps(disabled))
            enabled_without_ablation["id"] = "paired"
            disabled_without_ablation["id"] = "paired"
            enabled_without_ablation["goal_plus"].pop("supplemental_evaluation_enabled")
            disabled_without_ablation["goal_plus"].pop("supplemental_evaluation_enabled")
            self.assertEqual(enabled_without_ablation, disabled_without_ablation)

    def test_astropy_profiles_freeze_worker_minimums_and_matched_ablations(
        self,
    ) -> None:
        pairs = (
            (
                "astropy-13033-goal-plus-codex-luna-high-acceptance-on-smoke",
                "astropy-13033-goal-plus-codex-luna-high-acceptance-off-smoke",
                "gpt-5.6-luna",
                "high",
            ),
            (
                "astropy-13033-goal-plus-codex-sol-medium-acceptance-on-smoke",
                "astropy-13033-goal-plus-codex-sol-medium-acceptance-off-smoke",
                "gpt-5.6-sol",
                "medium",
            ),
        )
        for enabled_id, disabled_id, model, reasoning in pairs:
            enabled = self.profile(enabled_id)
            disabled = self.profile(disabled_id)
            self.assertEqual(enabled["task_ids"], ["astropy__astropy-13033"])
            self.assertEqual(enabled["model"], model)
            self.assertEqual(enabled["reasoning_effort"], reasoning)
            self.assertEqual(enabled["wall_time_seconds"], 1800)
            self.assertEqual(enabled["concurrency"], 1)
            self.assertEqual(enabled["cell_concurrency"], 1)
            self.assertEqual(
                enabled["agent_network_policy"], "public-egress-blocked"
            )
            self.assertEqual(
                disabled["agent_network_policy"], "public-egress-blocked"
            )
            self.assertEqual(enabled["goal_plus"]["worker_runtime_seconds"], 1500)
            self.assertEqual(
                enabled["goal_plus"]["worker_min_runtime_seconds"], 600
            )
            self.assertEqual(enabled["goal_plus"]["worker_min_verifier_runs"], 2)
            self.assertEqual(
                enabled["goal_plus"]["evidence_annotator"]["reasoning_effort"],
                "medium",
            )
            self.assertTrue(enabled["goal_plus"]["supplemental_evaluation_enabled"])
            self.assertFalse(disabled["goal_plus"]["supplemental_evaluation_enabled"])
            enabled_without_ablation = json.loads(json.dumps(enabled))
            disabled_without_ablation = json.loads(json.dumps(disabled))
            enabled_without_ablation["id"] = "paired"
            disabled_without_ablation["id"] = "paired"
            enabled_without_ablation["goal_plus"].pop("supplemental_evaluation_enabled")
            disabled_without_ablation["goal_plus"].pop("supplemental_evaluation_enabled")
            self.assertEqual(enabled_without_ablation, disabled_without_ablation)

            prompt = runtime.build_goal_plus_prompt(
                {"problem_statement": "Public issue text"}, enabled
            )
            self.assertIn("strategy.worker_budget.min_runtime_seconds=600", prompt)
            self.assertIn("strategy.worker_budget.min_verifier_runs=2", prompt)

        invalid = json.loads(json.dumps(enabled))
        invalid["goal_plus"].pop("worker_min_verifier_runs")
        with self.assertRaisesRegex(SweBenchContractError, "configured together"):
            validate_profile(str(invalid["id"]), invalid)

        invalid = json.loads(json.dumps(enabled))
        invalid["goal_plus"]["worker_min_runtime_seconds"] = 1500
        with self.assertRaisesRegex(SweBenchContractError, "less than"):
            validate_profile(str(invalid["id"]), invalid)

        invalid = json.loads(json.dumps(enabled))
        invalid["agent_network_policy"] = "unknown"
        with self.assertRaisesRegex(SweBenchContractError, "network_policy"):
            validate_profile(str(invalid["id"]), invalid)

        invalid = json.loads(json.dumps(enabled))
        invalid.pop("agent_provider")
        with self.assertRaisesRegex(
            SweBenchContractError, "requires an agent_provider"
        ):
            validate_profile(str(invalid["id"]), invalid)

        k2 = self.profile(
            "astropy-13033-goal-plus-codex-luna-high-acceptance-on-k2-peer-smoke"
        )
        self.assertEqual(k2["concurrency"], 2)
        self.assertEqual(k2["cell_concurrency"], 1)
        self.assertTrue(k2["goal_plus"]["supplemental_evaluation_enabled"])
        k2_prompt = runtime.build_goal_plus_prompt(
            {"problem_statement": "Public issue text"}, k2
        )
        self.assertIn("budget.max_parallel=2", k2_prompt)
        self.assertIn("Use exactly 2 fixed initial candidates", k2_prompt)
        self.assertIn("each candidate on its existing bound Codex worker", k2_prompt)
        self.assertIn(
            "reads a completed peer supplemental View before its next verifier",
            k2_prompt,
        )

        k1 = self.profile(
            "astropy-13033-goal-plus-codex-luna-high-acceptance-on-smoke"
        )
        with self.assertRaisesRegex(SweBenchContractError, "profile-frozen"):
            resolve_profile(k1, concurrency=2)

        invalid = json.loads(json.dumps(k2))
        invalid["goal_plus"]["supplemental_evaluation_enabled"] = False
        with self.assertRaisesRegex(SweBenchContractError, "K=2 is restricted"):
            validate_profile(str(invalid["id"]), invalid)

    def test_goal_plus_checkout_branch_comes_from_managed_upstream(self) -> None:
        self.assertEqual(
            managed_upstream_branch("goal_plus"),
            "codex/acceptance-view",
        )

    def test_luna_pi_runtime_writes_environment_reference_not_secret(self) -> None:
        profile = self.profile("sympy-16886-goal-plus-pi-luna-high-smoke")
        secret = "not-for-provider-config"
        values = {
            "OPENAI_BASE_URL": "http://127.0.0.1:3788/v1",
            "OPENAI_API_KEY": secret,
        }
        with mock.patch.dict(os.environ, values, clear=False):
            runtime_info = environment.resolve_pi_runtime(profile)

        self.assertTrue(runtime_info["custom_provider"])
        self.assertEqual(runtime_info["provider"], "bench-openai")
        self.assertEqual(runtime_info["model_id"], "gpt-5.6-luna")
        self.assertEqual(runtime_info["credential_env"], "OPENAI_API_KEY")
        self.assertNotIn("api_key", runtime_info)

        runtime_info["runtime_api_base_url"] = "http://192.0.2.10:45678/v1"
        annotator_environment = runtime._goal_plus_evidence_annotator_environment(
            profile, runtime_info
        )
        self.assertEqual(
            annotator_environment["GOAL_PLUS_EVIDENCE_ANNOTATOR_MODEL"],
            "gpt-5.6-luna",
        )
        self.assertEqual(
            annotator_environment["GOAL_PLUS_EVIDENCE_ANNOTATOR_REASONING_EFFORT"],
            "high",
        )
        self.assertEqual(
            annotator_environment["GOAL_PLUS_EVIDENCE_ANNOTATOR_BASE_URL"],
            "http://192.0.2.10:45678/v1",
        )
        self.assertEqual(
            annotator_environment["GOAL_PLUS_EVIDENCE_ANNOTATOR_DISABLED"], "0"
        )
        self.assertNotIn(secret, annotator_environment.values())
        with self.temporary_directory() as temporary:
            models_file = Path(temporary) / "provider-runtime/models.json"
            environment.write_pi_models_config(
                models_file,
                runtime_info,
                reasoning_effort=profile["reasoning_effort"],
            )
            raw = models_file.read_text(encoding="utf-8")
            payload = json.loads(raw)

        provider = payload["providers"]["bench-openai"]
        self.assertEqual(provider["baseUrl"], "http://192.0.2.10:45678/v1")
        self.assertEqual(provider["api"], "openai-responses")
        self.assertEqual(provider["apiKey"], "$OPENAI_API_KEY")
        self.assertEqual(
            provider["models"][0]["thinkingLevelMap"], {"high": "high"}
        )
        self.assertNotIn(secret, raw)

    def test_deepseek_pi_profiles_use_responses_for_worker_and_view_agent(self) -> None:
        for profile_id, task_id in (
            (
                "django-13406-goal-plus-pi-deepseek-v4-flash-acceptance-on-smoke",
                "django__django-13406",
            ),
            (
                "astropy-13033-goal-plus-pi-deepseek-v4-flash-acceptance-on-smoke",
                "astropy__astropy-13033",
            ),
        ):
            profile = self.profile(profile_id)
            self.assertEqual(profile["task_ids"], [task_id])
            self.assertEqual(profile["methods"], ["goal-plus-pi"])
            self.assertEqual(
                profile["model"], "deepseek-responses/deepseek-v4-flash"
            )
            self.assertEqual(profile["reasoning_effort"], "medium")
            self.assertEqual(
                profile["agent_provider"],
                {
                    "id": "deepseek-responses",
                    "name": "DeepSeek Responses API",
                    "auth_mode": "openai-compatible",
                    "base_url_env": "DEEPSEEK_BASE_URL",
                    "api_key_env": "DEEPSEEK_API_KEY",
                    "wire_api": "responses",
                },
            )
            self.assertTrue(profile["goal_plus"]["supplemental_evaluation_enabled"])
            self.assertEqual(
                profile["goal_plus"]["evidence_annotator"]["model"],
                "deepseek-v4-flash",
            )

    def test_deepseek_pi_registry_uses_provider_compatibility(self) -> None:
        profile = self.profile(
            "django-13406-goal-plus-pi-deepseek-v4-flash-acceptance-on-smoke"
        )
        secret = "not-for-provider-config"
        values = {
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
            "DEEPSEEK_API_KEY": secret,
        }
        with mock.patch.dict(os.environ, values, clear=False):
            runtime_info = environment.resolve_pi_runtime(profile)

        runtime_info["runtime_api_base_url"] = values["DEEPSEEK_BASE_URL"]
        with self.temporary_directory() as temporary:
            models_file = Path(temporary) / "provider-runtime/models.json"
            environment.write_pi_models_config(
                models_file,
                runtime_info,
                reasoning_effort=profile["reasoning_effort"],
            )
            raw = models_file.read_text(encoding="utf-8")
            provider = json.loads(raw)["providers"]["deepseek-responses"]

        self.assertEqual(provider["api"], "openai-responses")
        self.assertEqual(provider["apiKey"], "$DEEPSEEK_API_KEY")
        self.assertEqual(
            provider["compat"],
            {
                "supportsDeveloperRole": False,
                "supportsReasoningEffort": True,
                "supportsStore": False,
            },
        )
        self.assertEqual(provider["models"][0]["contextWindow"], 1_000_000)
        self.assertEqual(provider["models"][0]["maxTokens"], 384_000)
        self.assertNotIn("compat", provider["models"][0])
        self.assertNotIn(secret, raw)

    def test_loopback_bridge_preserves_the_responses_base_path(self) -> None:
        self.assertEqual(
            loopback_target("http://127.0.0.1:3788/v1"),
            ("127.0.0.1", 3788),
        )
        self.assertEqual(
            bridged_url("http://127.0.0.1:3788/v1", "192.0.2.10", 45678),
            "http://192.0.2.10:45678/v1",
        )

    def test_default_huggingface_cache_is_repository_local(self) -> None:
        expected = ensure_temp_root("huggingface")
        with mock.patch.dict(
            os.environ,
            {"XDG_CACHE_HOME": "/outside/cache"},
            clear=False,
        ):
            os.environ.pop("HF_HOME", None)
            observed = runtime._configure_huggingface_cache()

        self.assertEqual(observed, expected)

    def test_image_synthetic_head_must_match_the_official_base_tree(self) -> None:
        profile = self.profile("sympy-16886-codex-smoke")
        base_commit = profile["tasks"][0]["base_commit"]
        observed_head = "5" * 40
        expected_tree = "6" * 40

        def docker_checked(command: list[str], *, timeout: int = 120) -> str:
            del timeout
            if command[-2:] == ["rev-parse", "HEAD"]:
                return observed_head
            if command[-2:] == ["rev-parse", f"{base_commit}^{{tree}}"]:
                return expected_tree
            if command[-2:] == ["rev-parse", "HEAD^{tree}"]:
                return expected_tree
            return ""

        with mock.patch.object(runtime, "_docker_checked", side_effect=docker_checked):
            checkout = runtime._initialize_agent_container(
                "container-id", profile, {}
            )

        self.assertTrue(checkout["synthetic_head"])
        self.assertEqual(checkout["observed_head"], observed_head)
        self.assertEqual(checkout["base_commit"], base_commit)

        def mismatched_tree(command: list[str], *, timeout: int = 120) -> str:
            value = docker_checked(command, timeout=timeout)
            return "7" * 40 if command[-2:] == ["rev-parse", "HEAD^{tree}"] else value

        with (
            mock.patch.object(runtime, "_docker_checked", side_effect=mismatched_tree),
            self.assertRaisesRegex(SweBenchContractError, "checkout tree"),
        ):
            runtime._initialize_agent_container("container-id", profile, {})

    def test_astropy_official_image_setup_patch_must_match_profile(self) -> None:
        profile = self.profile(
            "astropy-13033-goal-plus-codex-luna-high-acceptance-off-smoke"
        )
        profile["methods"] = ["plain-codex"]
        task = profile["tasks"][0]
        setup = task["image_setup"]
        patch = (
            "diff --git a/pyproject.toml b/pyproject.toml\n"
            "--- a/pyproject.toml\n"
            "+++ b/pyproject.toml\n"
            "@@ -1 +1 @@\n"
            "-setuptools\n"
            "+setuptools==68.0.0"
        )
        setup["patch_sha256"] = hashlib.sha256(
            (patch + "\n").encode("utf-8")
        ).hexdigest()

        def docker_checked(command: list[str], *, timeout: int = 120) -> str:
            del timeout
            if command[-2:] == ["rev-parse", "HEAD"]:
                return setup["head"]
            if command[-2:] == ["rev-parse", f"{task['base_commit']}^{{tree}}"]:
                return "6" * 40
            if command[-2:] == ["rev-parse", "HEAD^{tree}"]:
                return setup["tree"]
            if "--binary" in command:
                return patch
            if "--name-only" in command:
                return "pyproject.toml"
            return ""

        with mock.patch.object(runtime, "_docker_checked", side_effect=docker_checked):
            checkout = runtime._initialize_agent_container(
                "container-id", profile, {}
            )
        self.assertTrue(checkout["image_setup"]["passed"])
        self.assertEqual(checkout["image_setup"]["files"], ["pyproject.toml"])

        def tampered_patch(command: list[str], *, timeout: int = 120) -> str:
            value = docker_checked(command, timeout=timeout)
            return value + "\nextra" if "--binary" in command else value

        with (
            mock.patch.object(
                runtime, "_docker_checked", side_effect=tampered_patch
            ),
            self.assertRaisesRegex(SweBenchContractError, "official image setup"),
        ):
            runtime._initialize_agent_container("container-id", profile, {})

    def test_goal_plus_initialization_preserves_read_only_verifier_mount(self) -> None:
        profile = self.profile(
            "django-13406-goal-plus-codex-sol-medium-acceptance-on-smoke"
        )
        base_commit = profile["tasks"][0]["base_commit"]
        docker_commands: list[list[str]] = []

        def docker_checked(command: list[str], *, timeout: int = 120) -> str:
            del timeout
            docker_commands.append(command)
            if command[-2:] == ["rev-parse", "HEAD"]:
                return base_commit
            if command[-2:] in (
                ["rev-parse", f"{base_commit}^{{tree}}"],
                ["rev-parse", "HEAD^{tree}"],
            ):
                return "1" * 40
            if "sha256sum" in command:
                return (
                    goal_plus_evidence.expected_visible_verifier_sha256()
                    + "  /testbed/.goal-plus-verifiers/visible_test_verifier.py"
                )
            return ""

        with mock.patch.object(runtime, "_docker_checked", side_effect=docker_checked):
            runtime._initialize_agent_container(
                "container-id",
                profile,
                {
                    "goal_plus_visible_verifier": (
                        environment.GOAL_PLUS_VISIBLE_VERIFIER
                    )
                },
            )

        clean = next(command for command in docker_commands if "clean" in command)
        self.assertEqual(clean[-2:], ["-e", ".goal-plus-verifiers/"])
        joined = " ".join(
            argument for command in docker_commands for argument in command
        )
        self.assertIn("sha256sum", joined)
        self.assertNotIn("cp /opt/swebench-visible-test-verifier.py", joined)
        self.assertNotIn(
            "chmod 0555 /testbed/.goal-plus-verifiers/visible_test_verifier.py",
            joined,
        )

    def test_goal_plus_codex_resolves_and_unqualified_pi_model_fails(self) -> None:
        agent = BenchmarkAgent(catalog=Catalog())
        goal_plus_codex = agent.resolve_spec(
            preset_id=(
                "swe-bench-verified-sympy-16886-"
                "goal-plus-codex-acceptance-smoke"
            )
        )
        self.assertEqual(goal_plus_codex.methods, ("goal-plus-codex",))
        with self.assertRaisesRegex(ContractError, "PROVIDER/MODEL"):
            agent.resolve_spec(
                target_ids=("swe-bench-verified",),
                profile="sympy-16886-pi-smoke",
                methods=("plain-pi",),
                model="glm-5.2",
            )
        with self.assertRaisesRegex(ContractError, "use C=1"):
            agent.resolve_spec(
                target_ids=("swe-bench-verified",),
                profile="sympy-16886-codex-smoke",
                methods=("plain-codex",),
                model="gpt-5.6-sol",
                cell_concurrency=2,
            )

    def test_native_plan_uses_doctor_prepare_and_foreground_run(self) -> None:
        agent = BenchmarkAgent(catalog=Catalog())
        spec = agent.resolve_spec(
            preset_id="swe-bench-verified-sympy-16886-codex-smoke",
            seeds=(2,),
            retain_containers=True,
        )
        runner = create_runner(spec.runner)

        doctor = runner.provision_commands(spec, skip_provision=True)
        prepare, campaign = runner.prepare_commands(spec)
        run = runner.start_command(spec, campaign, detach=False)

        self.assertEqual(len(doctor), 1)
        self.assertIn("doctor", doctor[0])
        self.assertIn("--method", doctor[0])
        self.assertIn("prepare", prepare[0])
        self.assertIn("--wall-time-seconds", prepare[0])
        self.assertIn("--retain-containers", prepare[0])
        seed_index = prepare[0].index("--seed")
        self.assertEqual(prepare[0][seed_index : seed_index + 2], ["--seed", "2"])
        self.assertTrue(spec.as_dict()["retain_containers"])
        self.assertEqual(run[-2:], ["--campaign", campaign.campaign_id])
        self.assertNotIn("--detach", run)

    def test_unproven_runner_rejects_retained_containers_during_plan(self) -> None:
        agent = BenchmarkAgent(catalog=Catalog())
        with self.assertRaisesRegex(ContractError, "does not support retained"):
            agent.resolve_spec(
                preset_id="edgebench-vliw-codex-local-smoke",
                retain_containers=True,
            )

    def test_prepare_separates_agent_task_from_hidden_evaluator_fields(self) -> None:
        profile = self.profile("sympy-16886-codex-smoke")
        profile["seed"] = 2
        instance = {
            "instance_id": "sympy__sympy-16886",
            "repo": "sympy/sympy",
            "base_commit": profile["tasks"][0]["base_commit"],
            "problem_statement": "Public issue text",
            "version": "1.5",
            "patch": "gold patch",
            "test_patch": "hidden tests",
            "FAIL_TO_PASS": ["hidden-fail"],
            "PASS_TO_PASS": ["hidden-pass"],
        }
        with self.temporary_directory() as temporary:
            campaign = Path(temporary) / "campaign"

            def git_value(_path: Path, *args: str) -> str:
                return "" if args[:2] == ("status", "--porcelain") else "a" * 40

            with (
                mock.patch.object(runtime, "campaign_dir", return_value=campaign),
                mock.patch.object(runtime, "preserve_conflict", return_value=None),
                mock.patch.object(runtime, "_load_pinned_instance", return_value=instance),
                mock.patch.object(runtime, "_validate_instance_image"),
                mock.patch.object(runtime, "_git_value", side_effect=git_value),
            ):
                runtime.prepare("test-campaign", profile)

            manifest = json.loads((campaign / "campaign.json").read_text())
            task = json.loads((campaign / manifest["cells"][0]["task_file"]).read_text())
            evaluator_path = campaign / manifest["dataset"]["evaluator_instances_file"]
            evaluator = json.loads(evaluator_path.read_text())

            self.assertTrue(runtime.HIDDEN_INSTANCE_FIELDS.isdisjoint(task))
            self.assertEqual(task["problem_statement"], "Public issue text")
            self.assertEqual(len(evaluator), 1)
            self.assertEqual(evaluator[0]["patch"], "gold patch")
            self.assertEqual(evaluator[0]["test_patch"], "hidden tests")
            self.assertEqual(stat.S_IMODE(evaluator_path.stat().st_mode), 0o600)
            self.assertFalse(manifest["container_retention"]["requested"])
            self.assertEqual(manifest["container_retention"]["scope"], "agent")
            self.assertEqual(manifest["seed"], 2)
            self.assertEqual(manifest["cells"][0]["seed"], 2)

            from swebench.harness.utils import load_swebench_dataset

            loaded = load_swebench_dataset(str(evaluator_path), "test")
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["instance_id"], instance["instance_id"])

    def test_prompt_rejects_hidden_fields(self) -> None:
        with self.assertRaisesRegex(SweBenchContractError, "hidden fields"):
            runtime.build_agent_prompt(
                {"problem_statement": "issue", "test_patch": "do not expose"}
            )

    def test_prepare_persists_k2_profile_concurrency(self) -> None:
        profile = self.profile(
            "astropy-13033-goal-plus-codex-luna-high-acceptance-on-k2-peer-smoke"
        )
        instance = {
            "instance_id": "astropy__astropy-13033",
            "repo": "astropy/astropy",
            "base_commit": profile["tasks"][0]["base_commit"],
            "problem_statement": "Public issue text",
            "version": "4.3",
            "patch": "gold patch",
            "test_patch": "hidden tests",
            "FAIL_TO_PASS": ["hidden-fail"],
            "PASS_TO_PASS": ["hidden-pass"],
        }
        with self.temporary_directory() as temporary:
            campaign = Path(temporary) / "campaign"

            def git_value(_path: Path, *args: str) -> str:
                return "" if args[:2] == ("status", "--porcelain") else "a" * 40

            with (
                mock.patch.object(runtime, "campaign_dir", return_value=campaign),
                mock.patch.object(runtime, "preserve_conflict", return_value=None),
                mock.patch.object(runtime, "_load_pinned_instance", return_value=instance),
                mock.patch.object(runtime, "_validate_instance_image"),
                mock.patch.object(runtime, "_git_value", side_effect=git_value),
            ):
                runtime.prepare("k2-test-campaign", profile)

            manifest = json.loads((campaign / "campaign.json").read_text())

        self.assertEqual(manifest["budget"]["live_search_concurrency"], 2)
        self.assertEqual(manifest["budget"]["cell_concurrency"], 1)

    def test_pi_commands_inherit_only_the_credential_variable_name(self) -> None:
        profile = self.profile("sympy-16886-pi-smoke")
        secret = "not-for-command-lines"
        runtime_info = {
            "credential_env": "ZAI_API_KEY",
            "provider": "zai",
            "model_id": "glm-5.2",
            "node_root": Path("/opt/node-host"),
            "package_root": Path("/opt/pi-host"),
        }
        with mock.patch.dict(os.environ, {"ZAI_API_KEY": secret}, clear=False):
            command = runtime._agent_command("container-id", profile, runtime_info)

        self.assertIn("ZAI_API_KEY", command)
        self.assertFalse(any(secret in argument for argument in command))
        self.assertNotIn(f"ZAI_API_KEY={secret}", command)

        completed = subprocess.CompletedProcess([], 0, "glm-5.2", "")
        with mock.patch.object(environment, "run_capture", return_value=completed) as capture:
            environment._pi_container_probe(profile["tasks"][0]["image"], runtime_info)
        probe = capture.call_args.args[0]
        self.assertIn("ZAI_API_KEY", probe)
        self.assertFalse(any(secret in argument for argument in probe))

    def test_custom_pi_container_probe_mounts_generated_provider_config(self) -> None:
        secret = "not-for-command-lines"
        with self.temporary_directory() as temporary:
            models_file = Path(temporary) / "provider-runtime/models.json"
            models_file.parent.mkdir(parents=True)
            models_file.write_text("{\"providers\": {}}\n", encoding="utf-8")
            runtime_info = {
                "credential_env": "OPENAI_API_KEY",
                "provider": "bench-openai",
                "model_id": "gpt-5.6-luna",
                "node_root": Path("/opt/node-host"),
                "package_root": Path("/opt/pi-host"),
                "models_file": models_file,
                "bridge_host": "192.0.2.10",
            }
            completed = subprocess.CompletedProcess([], 0, "gpt-5.6-luna", "")
            with (
                mock.patch.dict(
                    os.environ, {"OPENAI_API_KEY": secret}, clear=False
                ),
                mock.patch.object(
                    environment, "run_capture", return_value=completed
                ) as capture,
            ):
                environment._pi_container_probe("image:latest", runtime_info)

        command = capture.call_args.args[0]
        joined = " ".join(str(item) for item in command)
        self.assertEqual(command[:4], ["docker", "run", "--pull", "never"])
        self.assertIn("OPENAI_API_KEY", command)
        self.assertIn("dst=/opt/provider,readonly", joined)
        self.assertIn("PI_CODING_AGENT_DIR=/opt/pi-home/.pi/agent", command)
        self.assertIn("NO_PROXY=192.0.2.10", command)
        self.assertNotIn(secret, joined)

    def test_goal_plus_container_mounts_and_outer_pi_command_are_explicit(self) -> None:
        profile = self.profile("sympy-16886-goal-plus-pi-smoke")
        secret = "not-for-command-lines"
        with self.temporary_directory() as temporary:
            assets = Path(temporary)
            goal_plus_root = assets / "goal-plus"
            goal_plus_root.mkdir()
            dependency_lock = assets / "requirements.lock"
            dependency_lock.write_text("pydantic==2.13.4\n", encoding="utf-8")
            verifier = assets / "visible.py"
            verifier.write_text("print('{}')\n", encoding="utf-8")
            controller = assets / "controller.py"
            controller.write_text("print('{}')\n", encoding="utf-8")
            pip_cache = assets / "pip-cache"
            pip_cache.mkdir()
            runtime_info = {
                "credential_env": "ZAI_API_KEY",
                "credential_present": True,
                "provider": "zai",
                "model_id": "glm-5.2",
                "node_root": Path("/host/node"),
                "package_root": Path("/host/pi"),
                "goal_plus_root": goal_plus_root,
                "goal_plus_dependency_lock": dependency_lock,
                "goal_plus_visible_verifier": verifier,
                "goal_plus_controller": controller,
                "goal_plus_pip_cache": pip_cache,
            }
            docker_commands: list[list[str]] = []

            def docker_checked(command: list[str], *, timeout: int = 120) -> str:
                del timeout
                docker_commands.append(command)
                return "container-id" if command[:2] == ["docker", "create"] else ""

            with mock.patch.object(
                runtime, "_docker_checked", side_effect=docker_checked
            ):
                runtime._create_agent_container(
                    "goal-plus-campaign", profile, runtime_info
                )

            create = docker_commands[0]
            joined = " ".join(create)
            self.assertEqual(create[:4], ["docker", "create", "--pull", "never"])
            self.assertIn("dst=/opt/goal-plus,readonly", joined)
            self.assertIn("dst=/opt/pi,readonly", joined)
            self.assertIn("dst=/opt/node,readonly", joined)
            self.assertIn("dst=/opt/pip-cache", joined)
            self.assertNotIn("dst=/opt/pip-cache,readonly", joined)
            self.assertIn("dst=/testbed/.goal-plus-verifiers,readonly", joined)
            self.assertNotIn("dst=/opt/swebench-visible-test-verifier.py", joined)
            self.assertIn(
                "/opt/goal-plus-runtime:rw,exec,nosuid,nodev,size=512m", create
            )

            prompt = runtime.build_goal_plus_prompt(
                {"problem_statement": "Public issue text"}, profile
            )
            runtime_info.update(
                {
                    "outer_deadline_at": "2026-08-03T12:00:00+00:00",
                    "main_session_id": "swe-bench-main-test",
                    "goal_prompt": prompt,
                }
            )
            with mock.patch.dict(os.environ, {"ZAI_API_KEY": secret}, clear=False):
                command = runtime._agent_command(
                    "container-id", profile, runtime_info
                )

            self.assertEqual(command[-1], prompt)
            self.assertTrue(prompt.startswith("/goal-plus mode=autonomous"))
            self.assertIn("budget.max_parallel=1", prompt)
            self.assertIn("do not set the deprecated max_candidates", prompt)
            self.assertIn("Public issue text", prompt)
            self.assertIn("Do not add acceptance_view", prompt)
            self.assertIn("open-ended, task-specific observations", prompt)
            self.assertIn("do not choose a winner", prompt)
            self.assertIn("strategy.evidence_annotator.host=codex", prompt)
            self.assertIn("never change candidate settlement", prompt)
            self.assertIn("or the official binary result", prompt)
            self.assertIn("repository's native test instructions", prompt)
            self.assertIn("adjacent existing assertions", prompt)
            self.assertIn("public behavior inventory", prompt)
            self.assertIn("single substring", prompt)
            self.assertIn("every runnable item", prompt)
            self.assertIn("public issue leaves ambiguous", prompt)
            self.assertIn("relevant existing test files in edit_surface", prompt)
            self.assertIn("must not redefine or weaken the frozen verifier", prompt)
            self.assertIn("Do not edit public tests merely", prompt)
            self.assertIn("benchmark-owned and read-only", prompt)
            self.assertIn(
                ".goal-plus-verifiers/visible_test_verifier.py --ranking-signal",
                prompt,
            )
            self.assertIn(
                "promotion command must use the same candidate-relative command "
                "without --ranking-signal",
                prompt,
            )
            self.assertIn("other exit-code suppressor", prompt)
            self.assertIn("/opt/goal-plus/.pi/extensions/goal-plus.ts", command)
            self.assertIn("/opt/goal-plus/.pi/skills/goal-plus/SKILL.md", command)
            self.assertIn("GOAL_PLUS_ROOT=/testbed/.gp", command)
            self.assertIn("GOAL_PLUS_EVIDENCE_ANNOTATOR_DISABLED=1", command)
            self.assertIn(
                "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED=0", command
            )
            self.assertIn(
                'export PATH=/opt/goal-plus-bin:/opt/node/bin:$PATH; exec "$@"',
                command,
            )
            self.assertIn("ZAI_API_KEY", command)
            self.assertFalse(any(secret in argument for argument in command))

    def test_goal_plus_luna_container_mounts_codex_view_agent_runtime(self) -> None:
        profile = self.profile("sympy-16886-goal-plus-pi-luna-high-smoke")
        secret = "not-for-command-lines"
        with self.temporary_directory() as temporary:
            assets = Path(temporary)
            path_assets = {
                "goal_plus_root": assets / "goal-plus",
                "goal_plus_dependency_lock": assets / "requirements.lock",
                "goal_plus_visible_verifier": assets / "visible.py",
                "goal_plus_controller": assets / "controller.py",
                "goal_plus_pip_cache": assets / "pip-cache",
                "goal_plus_codex_archive": assets / "codex.tgz",
            }
            path_assets["goal_plus_root"].mkdir()
            path_assets["goal_plus_pip_cache"].mkdir()
            for name, path in path_assets.items():
                if name not in {"goal_plus_root", "goal_plus_pip_cache"}:
                    path.write_text("fixture\n", encoding="utf-8")
            runtime_info = {
                **path_assets,
                "credential_env": "OPENAI_API_KEY",
                "credential_present": True,
                "provider": "bench-openai",
                "provider_name": "Benchmark OpenAI-compatible proxy",
                "model_id": "gpt-5.6-luna",
                "node_root": Path("/host/node"),
                "package_root": Path("/host/pi"),
                "runtime_api_base_url": "http://192.0.2.10:45678/v1",
                "outer_deadline_at": "2026-08-03T12:00:00+00:00",
                "goal_plus_evidence_annotator": profile["goal_plus"][
                    "evidence_annotator"
                ],
            }
            docker_commands: list[list[str]] = []

            def docker_checked(command: list[str], *, timeout: int = 120) -> str:
                del timeout
                docker_commands.append(command)
                return "container-id" if command[:2] == ["docker", "create"] else ""

            with mock.patch.object(
                runtime, "_docker_checked", side_effect=docker_checked
            ):
                runtime._create_agent_container(
                    "goal-plus-view-agent", profile, runtime_info
                )

            completed = subprocess.CompletedProcess(
                ["docker", "exec"], 0, '{"completed": true}\n', ""
            )
            with (
                mock.patch.dict(
                    os.environ, {"OPENAI_API_KEY": secret}, clear=False
                ),
                mock.patch.object(runtime, "_run", return_value=completed) as capture,
            ):
                closeout = runtime._goal_plus_closeout(
                    "container-id", profile, runtime_info
                )

        create = docker_commands[0]
        joined = " ".join(create)
        self.assertIn("/opt/codex:rw,exec,nosuid,nodev,size=512m", create)
        self.assertIn("/opt/codex-home:rw,nosuid,nodev,size=256m", create)
        self.assertIn("dst=/opt/runtime/codex.tgz,readonly", joined)
        closeout_command = capture.call_args.args[0]
        self.assertTrue(closeout["completed"])
        self.assertIn("OPENAI_API_KEY", closeout_command)
        self.assertIn(
            "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED=1",
            closeout_command,
        )
        self.assertIn(
            "GOAL_PLUS_EVIDENCE_ANNOTATOR_MODEL=gpt-5.6-luna",
            closeout_command,
        )
        self.assertFalse(any(secret in argument for argument in closeout_command))
        self.assertEqual(capture.call_args.kwargs["timeout"], 1020)

    def test_goal_plus_durable_evidence_enforces_k_and_verifier_contract(self) -> None:
        with self.temporary_directory() as temporary:
            root = Path(temporary) / "passing"
            self.write_goal_plus_state(root)
            state = goal_plus_evidence.collect_goal_plus_state(
                root,
                expected_k=1,
                expected_worker_runtime_seconds=1500,
                expected_closeout_reserve_seconds=300,
                expected_visible_verifier_timeout_seconds=300,
            )

            self.assertTrue(state["completion"]["passed"])
            self.assertEqual(state["actual_subagent_count"], 1)
            self.assertEqual(state["worker_usage"]["input_tokens"], 10)
            self.assertEqual(state["worker_usage"]["sessions_covered"], 1)
            visible = state["completion"]["checks"]["visible_verifiers"]["actual"]
            self.assertTrue(visible["process"]["passed"])
            self.assertTrue(visible["promotion"]["passed"])
            self.assertEqual(
                visible["promotion"]["verifiers"][0]["role"], "promotion_gate"
            )

            mismatched = Path(temporary) / "mismatched"
            self.write_goal_plus_state(mismatched, session_count=0)
            mismatch = goal_plus_evidence.collect_goal_plus_state(
                mismatched,
                expected_k=1,
                expected_worker_runtime_seconds=1500,
                expected_closeout_reserve_seconds=300,
                expected_visible_verifier_timeout_seconds=300,
            )
            self.assertFalse(mismatch["completion"]["passed"])
            self.assertEqual(mismatch["actual_subagent_count"], 0)
            self.assertIn(
                "bound_pi_worker_sessions", mismatch["completion"]["reason"]
            )

    def test_goal_plus_durable_evidence_enforces_supplemental_evaluation(self) -> None:
        with self.temporary_directory() as temporary:
            root = Path(temporary) / "acceptance-on"
            self.write_goal_plus_state(
                root, supplemental_evaluation=True, evidence_annotations=True
            )
            state = goal_plus_evidence.collect_goal_plus_state(
                root,
                expected_k=1,
                expected_worker_runtime_seconds=1500,
                expected_closeout_reserve_seconds=300,
                expected_visible_verifier_timeout_seconds=300,
                expected_supplemental_evaluation_enabled=True,
                expected_evidence_annotator_enabled=True,
            )

            self.assertTrue(state["completion"]["passed"])
            self.assertTrue(
                state["completion"]["checks"][
                    "supplemental_evaluation_contract"
                ]["passed"]
            )
            self.assertTrue(
                state["completion"]["checks"]["global_evidence_view"]["passed"]
            )
            reads = state["completion"]["checks"][
                "global_evidence_read_receipts"
            ]
            self.assertTrue(reads["passed"])
            self.assertEqual(reads["actual"]["read_count"], 1)
            self.assertEqual(
                reads["actual"][
                    "reads_with_completed_supplemental_evaluations"
                ],
                1,
            )
            self.assertEqual(state["evidence_annotator_usage"]["input_tokens"], 40)

            missing = Path(temporary) / "acceptance-missing"
            self.write_goal_plus_state(missing)
            missing_state = goal_plus_evidence.collect_goal_plus_state(
                missing,
                expected_k=1,
                expected_worker_runtime_seconds=1500,
                expected_closeout_reserve_seconds=300,
                expected_visible_verifier_timeout_seconds=300,
                expected_supplemental_evaluation_enabled=True,
                expected_evidence_annotator_enabled=True,
            )
            self.assertFalse(missing_state["completion"]["passed"])
            self.assertIn(
                "supplemental_evaluation_contract",
                missing_state["completion"]["reason"],
            )
            self.assertIn(
                "global_evidence_view", missing_state["completion"]["reason"]
            )

            off = Path(temporary) / "acceptance-off"
            self.write_goal_plus_state(off, evidence_annotations=True)
            off_state = goal_plus_evidence.collect_goal_plus_state(
                off,
                expected_k=1,
                expected_worker_runtime_seconds=1500,
                expected_closeout_reserve_seconds=300,
                expected_visible_verifier_timeout_seconds=300,
                expected_supplemental_evaluation_enabled=False,
                expected_evidence_annotator_enabled=True,
            )
            self.assertTrue(off_state["completion"]["passed"])

    def test_global_evidence_receipt_precedes_next_verifier(self) -> None:
        receipts = goal_plus_evidence._global_evidence_read_receipts(
            [
                {
                    "agent_session_id": "agent_test",
                    "global_evidence_reads": [
                        {
                            "read_at": "2026-08-06T12:01:00Z",
                            "evidence_count": 1,
                            "completed_view_count": 1,
                            "completed_supplemental_evaluation_count": 1,
                            "completed_views": [
                                {
                                    "candidate_id": "c001",
                                    "iteration": 1,
                                    "commit": "a" * 40,
                                    "view_created_at": "2026-08-06T12:00:30Z",
                                    "supplemental_evaluation_present": True,
                                }
                            ],
                        }
                    ],
                }
            ],
            [
                {
                    "candidate_id": "c001",
                    "iterations": [
                        {
                            "iteration": 1,
                            "agent_session_id": "agent_test",
                            "created_at": "2026-08-06T12:00:00Z",
                        },
                        {
                            "iteration": 2,
                            "agent_session_id": "agent_test",
                            "created_at": "2026-08-06T12:02:00Z",
                        },
                    ],
                }
            ],
        )

        self.assertTrue(receipts["schema_valid"])
        self.assertEqual(receipts["reads_before_subsequent_verifier"], 1)
        self.assertEqual(
            receipts["influence_windows"][0]["next_verifier"]["iteration"],
            2,
        )

    def test_goal_plus_completion_rejects_tampered_or_failing_visible_verifier(
        self,
    ) -> None:
        with self.temporary_directory() as temporary:
            root = Path(temporary)
            self.write_goal_plus_state(root, worker_host="codex")
            frozen_path = root / "specs/spec_test/frozen_spec.json"
            frozen = read_json(frozen_path)
            frozen["verifier_hashes"][
                ".goal-plus-verifiers/visible_test_verifier.py"
            ] = "0" * 64
            write_json(frozen_path, frozen)

            state = goal_plus_evidence.collect_goal_plus_state(
                root,
                expected_k=1,
                expected_worker_runtime_seconds=1500,
                expected_closeout_reserve_seconds=300,
                expected_visible_verifier_timeout_seconds=300,
                expected_worker_host="codex",
            )
            self.assertFalse(
                state["completion"]["checks"]["visible_verifier_integrity"][
                    "passed"
                ]
            )

            frozen["verifier_hashes"][
                ".goal-plus-verifiers/visible_test_verifier.py"
            ] = goal_plus_evidence.expected_visible_verifier_sha256()
            write_json(frozen_path, frozen)

            frozen["spec"]["promotion_verifiers"][0]["command"] = [
                "python",
                "-c",
                "print('suppressed promotion')",
                ".goal-plus-verifiers/visible_test_verifier.py",
                "--timeout-seconds",
                "300",
            ]
            write_json(frozen_path, frozen)
            state = goal_plus_evidence.collect_goal_plus_state(
                root,
                expected_k=1,
                expected_worker_runtime_seconds=1500,
                expected_closeout_reserve_seconds=300,
                expected_visible_verifier_timeout_seconds=300,
                expected_worker_host="codex",
            )
            self.assertFalse(
                state["completion"]["checks"]["visible_verifiers"]["passed"]
            )

            frozen["spec"]["promotion_verifiers"][0]["command"] = [
                "python",
                ".goal-plus-verifiers/visible_test_verifier.py",
                "--timeout-seconds",
                "300",
                "--",
                "python",
                "-m",
                "pytest",
            ]
            write_json(frozen_path, frozen)
            candidate_path = root / "runs/run_test/candidates/c001/candidate.json"
            candidate = read_json(candidate_path)
            candidate["promotion_report"]["aggregate_score"] = 0.0
            candidate["promotion_report"]["verifier_results"][0]["metrics"][
                "visible_test_score"
            ] = 0.0
            write_json(candidate_path, candidate)

            state = goal_plus_evidence.collect_goal_plus_state(
                root,
                expected_k=1,
                expected_worker_runtime_seconds=1500,
                expected_closeout_reserve_seconds=300,
                expected_visible_verifier_timeout_seconds=300,
                expected_worker_host="codex",
            )
            self.assertFalse(
                state["completion"]["checks"]["promotion_visible_test"]["passed"]
            )

    def test_public_egress_agent_command_fails_http_clients_fast(self) -> None:
        profile = self.profile(
            "astropy-13033-goal-plus-codex-sol-medium-acceptance-on-smoke"
        )
        command = runtime._agent_command(
            "container-id",
            profile,
            {
                "outer_deadline_at": "2026-08-07T12:00:00+00:00",
                "api_key_env": "OPENAI_API_KEY",
                "runtime_api_base_url": "http://192.0.2.1:45678/v1",
                "provider_id": "bench_proxy",
                "provider_name": "Benchmark proxy",
                "bridge_host": "192.0.2.1",
            },
        )

        self.assertIn("HTTPS_PROXY=http://127.0.0.1:9", command)
        self.assertIn("https_proxy=http://127.0.0.1:9", command)
        self.assertIn("ALL_PROXY=http://127.0.0.1:9", command)
        self.assertIn("NO_PROXY=192.0.2.1", command)

    def test_public_egress_network_is_internal_verified_and_removed(self) -> None:
        profile = self.profile(
            "astropy-13033-goal-plus-codex-sol-medium-acceptance-on-smoke"
        )
        network_id = "a" * 64
        inspected_network = json.dumps(
            [
                {
                    "Internal": True,
                    "IPAM": {"Config": [{"Gateway": "192.0.2.1"}]},
                }
            ]
        )
        docker_commands: list[list[str]] = []

        def docker_checked(command: list[str], *, timeout: int = 120) -> str:
            del timeout
            docker_commands.append(command)
            if command[:3] == ["docker", "network", "create"]:
                return network_id
            return inspected_network

        cleanup = subprocess.CompletedProcess(
            ["docker", "network", "rm", network_id], 0, network_id, ""
        )
        with (
            mock.patch.object(runtime, "_docker_checked", side_effect=docker_checked),
            mock.patch.object(runtime, "_run", return_value=cleanup) as run_command,
        ):
            with runtime._agent_network("campaign", profile) as network:
                self.assertTrue(network["enforced"])
                self.assertTrue(network["docker_internal"])
                self.assertEqual(network["gateway"], "192.0.2.1")

        create = docker_commands[0]
        self.assertIn("--internal", create)
        self.assertEqual(network["cleanup"]["removed"], True)
        self.assertEqual(
            run_command.call_args.args[0],
            ["docker", "network", "rm", network_id],
        )

        inspected_container = json.dumps(
            [
                {
                    "HostConfig": {"NetworkMode": network["name"]},
                    "NetworkSettings": {"Networks": {network["name"]: {}}},
                }
            ]
        )
        blocked = subprocess.CompletedProcess(
            ["docker", "exec"], 0, "PUBLIC_EGRESS_BLOCKED TimeoutError\n", ""
        )
        with (
            mock.patch.object(
                runtime, "_docker_checked", return_value=inspected_container
            ),
            mock.patch.object(runtime, "_run", return_value=blocked),
        ):
            verification = runtime._verify_agent_network("container-id", network)

        self.assertTrue(verification["passed"])
        self.assertEqual(verification["public_egress_probe"], "blocked")
        self.assertEqual(verification["attached_networks"], [network["name"]])
        network["verification"] = verification
        agent = {
            "runtime": {"agent_network": network},
            "container": {"cleanup": {"removed": True}},
        }
        self.assertTrue(reporting._agent_network_verified(agent, profile))

        available = subprocess.CompletedProcess(
            ["docker", "exec"], 1, "", "PUBLIC_EGRESS_AVAILABLE\n"
        )
        with (
            mock.patch.object(
                runtime, "_docker_checked", return_value=inspected_container
            ),
            mock.patch.object(runtime, "_run", return_value=available),
            self.assertRaisesRegex(SweBenchContractError, "isolation probe failed"),
        ):
            runtime._verify_agent_network("container-id", network)

        leaked_setup_network = json.dumps(
            [
                {
                    "HostConfig": {"NetworkMode": network["name"]},
                    "NetworkSettings": {
                        "Networks": {network["name"]: {}, "bridge": {}}
                    },
                }
            ]
        )
        with (
            mock.patch.object(
                runtime, "_docker_checked", return_value=leaked_setup_network
            ),
            mock.patch.object(runtime, "_run", return_value=blocked),
            self.assertRaisesRegex(SweBenchContractError, "isolation probe failed"),
        ):
            runtime._verify_agent_network("container-id", network)

        network["verification"] = {"passed": False}
        self.assertFalse(reporting._agent_network_verified(agent, profile))

    def test_setup_egress_is_disconnected_before_agent_network_probe(self) -> None:
        network = {
            "policy": "public-egress-blocked",
            "name": "bgp-swe-net-test",
        }
        commands: list[list[str]] = []

        def docker_checked(command: list[str], *, timeout: int = 120) -> str:
            del timeout
            commands.append(command)
            return ""

        with mock.patch.object(
            runtime, "_docker_checked", side_effect=docker_checked
        ):
            with runtime._temporary_setup_egress("container-id", network):
                self.assertTrue(network["setup_egress"]["connected"])
                self.assertIsNone(
                    network["setup_egress"]["disconnected_before_agent"]
                )

        self.assertEqual(
            commands,
            [
                [
                    "docker",
                    "network",
                    "connect",
                    "bridge",
                    "container-id",
                ],
                [
                    "docker",
                    "network",
                    "disconnect",
                    "bridge",
                    "container-id",
                ],
            ],
        )
        self.assertTrue(
            network["setup_egress"]["disconnected_before_agent"]
        )

    def test_codex_runtime_probe_and_agent_use_the_same_bounded_tmpfs(self) -> None:
        profile = self.profile("sympy-16886-codex-smoke")
        runtime_info = {
            "archive": Path("/runtime/codex.tgz"),
            "archive_present": True,
            "provider_id": "bench_proxy",
            "provider_name": "Benchmark OpenAI-compatible proxy",
            "auth_mode": "openai-compatible",
            "wire_api": "responses",
            "base_url_env": "OPENAI_BASE_URL",
            "api_key_env": "OPENAI_API_KEY",
            "api_base_url": "http://127.0.0.1:3788/v1",
            "runtime_api_base_url": "http://192.0.2.10:45678/v1",
            "credential_present": True,
            "bridge_host": "192.0.2.10",
            "bridge": None,
        }
        completed = subprocess.CompletedProcess([], 0, "codex-cli 0.144.1", "")
        with mock.patch.object(environment, "run_capture", return_value=completed) as capture:
            environment._codex_container_probe(
                profile["tasks"][0]["image"], runtime_info["archive"]
            )
        probe = capture.call_args.args[0]

        docker_commands: list[list[str]] = []

        def docker_checked(command: list[str], *, timeout: int = 120) -> str:
            del timeout
            docker_commands.append(command)
            return "container-id"

        with (
            mock.patch.object(runtime, "resolve_codex_runtime", return_value=runtime_info),
            mock.patch.object(runtime, "_docker_checked", side_effect=docker_checked),
        ):
            runtime._create_agent_container(
                "campaign", profile, network_name="bgp-swe-net-test"
            )
        create = docker_commands[0]

        self.assertIn(environment.CODEX_RUNTIME_TMPFS, probe)
        self.assertIn(environment.CODEX_RUNTIME_TMPFS, create)
        self.assertIn(":rw,exec,nosuid,nodev,", environment.CODEX_RUNTIME_TMPFS)
        self.assertEqual(environment.CODEX_RUNTIME_TMPFS.rsplit("=", 1)[1], "512m")
        self.assertNotIn("/opt/host-auth", " ".join(create))
        self.assertIn("--network", create)
        self.assertIn("bgp-swe-net-test", create)

        secret = "not-for-command-lines"
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": secret}, clear=False):
            agent_command = runtime._agent_command(
                "container-id", profile, runtime_info
            )
        joined = " ".join(agent_command)
        self.assertIn('model_provider="bench_proxy"', joined)
        self.assertIn('wire_api="responses"', joined)
        self.assertIn("http://192.0.2.10:45678/v1", joined)
        self.assertIn("OPENAI_API_KEY", agent_command)
        self.assertNotIn(secret, joined)
        self.assertNotIn("chatgpt.com", joined)

    def test_container_responses_probe_inherits_key_by_name(self) -> None:
        secret = "not-for-command-lines"
        runtime_info = {
            "runtime_api_base_url": "http://192.0.2.10:45678/v1",
            "api_key_env": "OPENAI_API_KEY",
        }
        completed = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "passed": True,
                    "http_status": 200,
                    "object": "response",
                    "model": "gpt-5.6-sol",
                    "response_status": "completed",
                }
            ),
            "",
        )
        with (
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": secret}, clear=False),
            mock.patch.object(
                environment, "run_capture", return_value=completed
            ) as capture,
        ):
            result = environment.codex_container_responses_probe(
                "image:latest", runtime_info, model="gpt-5.6-sol"
            )

        command = capture.call_args.args[0]
        self.assertTrue(result["passed"])
        self.assertEqual(command[:4], ["docker", "run", "--pull", "never"])
        self.assertIn("OPENAI_API_KEY", command)
        self.assertFalse(any(secret in argument for argument in command))
        self.assertNotIn(f"OPENAI_API_KEY={secret}", command)

    def test_container_responses_probe_retries_transient_502(self) -> None:
        failed = {"passed": False, "http_status": 502, "error": "bad gateway"}
        passed = {"passed": True, "http_status": 200, "object": "response"}
        with (
            mock.patch.object(
                runtime,
                "codex_container_responses_probe",
                side_effect=[failed, passed],
            ) as probe,
            mock.patch.object(runtime.time, "sleep") as sleep,
        ):
            result = runtime._container_responses_probe_with_retry(
                "container-id", {}, model="gpt-5.6-luna"
            )

        self.assertTrue(result["passed"])
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(
            [item["http_status"] for item in result["attempts"]],
            [502, 200],
        )
        self.assertEqual(probe.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_container_responses_probe_does_not_retry_auth_failure(self) -> None:
        failed = {"passed": False, "http_status": 401, "error": "unauthorized"}
        with (
            mock.patch.object(
                runtime,
                "codex_container_responses_probe",
                return_value=failed,
            ) as probe,
            mock.patch.object(runtime.time, "sleep") as sleep,
        ):
            result = runtime._container_responses_probe_with_retry(
                "container-id", {}, model="gpt-5.6-luna"
            )

        self.assertFalse(result["passed"])
        self.assertEqual(result["attempt_count"], 1)
        probe.assert_called_once()
        sleep.assert_not_called()

    def test_timeout_stops_container_before_exporting_patch(self) -> None:
        profile = self.profile("sympy-16886-pi-smoke")
        runtime_info = {
            "credential_env": "ZAI_API_KEY",
            "provider": "zai",
            "node_root": Path("/node"),
            "package_root": Path("/pi"),
        }
        with self.temporary_directory() as temporary:
            campaign = Path(temporary)
            cell_dir = campaign / "cells/plain-pi"
            cell_dir.mkdir(parents=True)
            write_json(
                cell_dir / "task.json",
                {"problem_statement": "issue"},
            )
            cell = {
                "task_file": "cells/plain-pi/task.json",
                "patch_file": "cells/plain-pi/model.patch",
                "method": "plain-pi",
                "model": profile["model"],
            }
            manifest = {"campaign_id": "timeout-test", "profile_snapshot": profile}
            docker_commands: list[list[str]] = []

            def docker_checked(command: list[str], *, timeout: int = 120) -> str:
                docker_commands.append(command)
                if "diff" in command:
                    return "diff --git a/a b/a\n"
                if "status" in command:
                    return " M a"
                return "container-id"

            def run_command(command: list[str], **_kwargs):
                if command[:3] == ["docker", "rm", "-f"]:
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise subprocess.TimeoutExpired(command, 300, output="partial output")

            with (
                mock.patch.object(
                    runtime,
                    "_create_agent_container",
                    return_value=("container-id", runtime_info),
                ),
                mock.patch.object(runtime, "_initialize_agent_container"),
                mock.patch.object(
                    runtime, "_agent_command", return_value=["docker", "exec", "container-id"]
                ),
                mock.patch.object(runtime, "_docker_checked", side_effect=docker_checked),
                mock.patch.object(runtime, "_run", side_effect=run_command),
            ):
                result = runtime._run_agent(campaign, manifest, cell)

            stop_index = next(i for i, command in enumerate(docker_commands) if "stop" in command)
            diff_index = next(i for i, command in enumerate(docker_commands) if "diff" in command)
            self.assertLess(stop_index, diff_index)
            self.assertTrue(result["timed_out"])
            self.assertTrue(result["patch_exists"])
            self.assertTrue(result["container"]["cleanup"]["removed"])
            self.assertIsNotNone(result["runtime_seconds"])
            self.assertIsNotNone(result["finalization_grace_seconds"])

    def test_goal_plus_state_is_exported_before_container_disposal(self) -> None:
        profile = self.profile("sympy-16886-goal-plus-pi-smoke")
        runtime_info = {
            "credential_env": "ZAI_API_KEY",
            "provider": "zai",
            "node_root": Path("/node"),
            "package_root": Path("/pi"),
            "goal_plus_root": Path("/goal-plus"),
            "goal_plus_dependency_lock": Path("/requirements.lock"),
            "goal_plus_visible_verifier": Path("/visible.py"),
            "goal_plus_controller": Path("/controller.py"),
            "goal_plus_pip_cache": Path("/pip-cache"),
        }
        with self.temporary_directory() as temporary:
            campaign = Path(temporary)
            cell_dir = campaign / "cells/goal-plus-pi"
            cell_dir.mkdir(parents=True)
            write_json(cell_dir / "task.json", {"problem_statement": "issue"})
            cell = {
                "task_file": "cells/goal-plus-pi/task.json",
                "patch_file": "cells/goal-plus-pi/model.patch",
                "method": "goal-plus-pi",
                "model": profile["model"],
            }
            manifest = {
                "campaign_id": "goal-plus-export-test",
                "profile_snapshot": profile,
                "source": {"goal_plus_commit": "a" * 40},
            }
            sequence: list[str] = []

            def docker_checked(command: list[str], *, timeout: int = 120) -> str:
                del timeout
                if "diff" in command:
                    return "diff --git a/a b/a\n"
                if "status" in command:
                    return " M a"
                return ""

            def export_state(*_args, **_kwargs):
                sequence.append("export")
                return {
                    "actual_subagent_count": 1,
                    "completion": {"passed": True, "reason": None},
                    "worker_usage": {"coverage": "persisted_pi_worker_usage"},
                }

            def dispose(*_args, **_kwargs):
                sequence.append("dispose")
                return {
                    "attempted": True,
                    "removed": True,
                    "retained": False,
                    "stopped": None,
                }

            with (
                mock.patch.object(
                    runtime, "resolve_goal_plus_runtime", return_value=runtime_info
                ),
                mock.patch.object(
                    runtime,
                    "_create_agent_container",
                    return_value=("container-id", runtime_info),
                ),
                mock.patch.object(runtime, "_initialize_agent_container"),
                mock.patch.object(runtime, "_agent_command", return_value=["outer"]),
                mock.patch.object(
                    runtime,
                    "_run",
                    return_value=subprocess.CompletedProcess(["outer"], 0, "", ""),
                ),
                mock.patch.object(
                    runtime,
                    "_goal_plus_closeout",
                    return_value={"completed": True},
                ),
                mock.patch.object(
                    runtime, "_export_goal_plus_state", side_effect=export_state
                ),
                mock.patch.object(
                    runtime, "_dispose_agent_container", side_effect=dispose
                ),
                mock.patch.object(
                    runtime, "_docker_checked", side_effect=docker_checked
                ),
            ):
                result = runtime._run_agent(campaign, manifest, cell)

            self.assertEqual(sequence, ["export", "dispose"])
            self.assertEqual(result["state"], "completed")
            self.assertTrue(result["patch_exists"])
            self.assertEqual(result["goal_plus"]["actual_subagent_count"], 1)

    def test_unconfirmed_agent_cleanup_blocks_official_evaluator(self) -> None:
        with self.temporary_directory() as temporary:
            campaign = Path(temporary)
            manifest = {
                "schema_version": 1,
                "campaign_id": "cleanup-test",
                "benchmark_id": "swe-bench-verified",
                "state": "prepared",
                "methods": ["plain-codex"],
                "model": "gpt-5.6-sol",
                "budget": {"wall_time_seconds": 300},
                "cells": [
                    {
                        "cell_id": "cell",
                        "task_id": "task",
                        "state": "prepared",
                        "evaluation": {"state": "pending", "calls": 0},
                    }
                ],
            }
            write_json(campaign / "campaign.json", manifest)
            agent_result = {
                "patch_exists": True,
                "container": {"cleanup": {"attempted": True, "removed": False}},
            }
            with (
                mock.patch.object(runtime, "_run_agent", return_value=agent_result),
                mock.patch.object(runtime, "_official_evaluation") as official,
            ):
                exit_code = runtime.execute_campaign(campaign)

            saved = json.loads((campaign / "campaign.json").read_text())
            self.assertEqual(exit_code, 1)
            self.assertEqual(saved["state"], "failed")
            self.assertEqual(saved["cells"][0]["evaluation"]["calls"], 0)
            official.assert_not_called()

    def test_stopped_retained_agent_allows_separate_official_evaluator(self) -> None:
        with self.temporary_directory() as temporary:
            campaign = Path(temporary)
            manifest = {
                "schema_version": 1,
                "campaign_id": "retention-test",
                "benchmark_id": "swe-bench-verified",
                "state": "prepared",
                "methods": ["plain-codex"],
                "model": "gpt-5.6-sol",
                "budget": {"wall_time_seconds": 300},
                "cells": [
                    {
                        "cell_id": "cell",
                        "task_id": "task",
                        "state": "prepared",
                        "evaluation": {"state": "pending", "calls": 0},
                    }
                ],
            }
            write_json(campaign / "campaign.json", manifest)
            agent_result = {
                "state": "completed",
                "patch_exists": True,
                "container": {
                    "id": "container-id",
                    "name": "bgp-swe-agent-test",
                    "cleanup": {
                        "policy": "retain",
                        "removed": False,
                        "retained": True,
                        "stopped": True,
                    },
                },
            }
            official_result = {"state": "completed", "calls": 1, "resolved": False}
            with (
                mock.patch.object(runtime, "_run_agent", return_value=agent_result),
                mock.patch.object(
                    runtime, "_official_evaluation", return_value=official_result
                ) as official,
            ):
                exit_code = runtime.execute_campaign(campaign)

            saved = json.loads((campaign / "campaign.json").read_text())
            self.assertEqual(exit_code, 0)
            self.assertEqual(saved["state"], "completed")
            official.assert_called_once()

    def test_goal_plus_evidence_failure_preserves_official_raw_score(self) -> None:
        with self.temporary_directory() as temporary:
            campaign = Path(temporary)
            manifest = {
                "schema_version": 1,
                "campaign_id": "goal-plus-partial-test",
                "benchmark_id": "swe-bench-verified",
                "state": "prepared",
                "methods": ["goal-plus-pi"],
                "model": "zai/glm-5.2",
                "budget": {"wall_time_seconds": 1800},
                "cells": [
                    {
                        "cell_id": "cell",
                        "task_id": "task",
                        "method": "goal-plus-pi",
                        "state": "prepared",
                        "evaluation": {"state": "pending", "calls": 0},
                    }
                ],
            }
            write_json(campaign / "campaign.json", manifest)
            agent_result = {
                "state": "partial",
                "patch_exists": True,
                "goal_plus": {
                    "actual_subagent_count": 0,
                    "completion": {
                        "passed": False,
                        "reason": "Goal Plus completion evidence failed: bound_pi_worker_sessions",
                    },
                },
                "container": {
                    "cleanup": {
                        "attempted": True,
                        "removed": True,
                        "retained": False,
                    }
                },
            }
            official_result = {
                "state": "completed",
                "calls": 1,
                "resolved": True,
                "patch_applied": True,
            }
            with (
                mock.patch.object(runtime, "_run_agent", return_value=agent_result),
                mock.patch.object(
                    runtime, "_official_evaluation", return_value=official_result
                ),
            ):
                exit_code = runtime.execute_campaign(campaign)

            saved = json.loads((campaign / "campaign.json").read_text())
            self.assertEqual(exit_code, 1)
            self.assertEqual(saved["state"], "partial")
            self.assertTrue(saved["cells"][0]["evaluation"]["resolved"])
            self.assertTrue(saved["cells"][0]["evaluation"]["patch_applied"])
            self.assertIn(
                "bound_pi_worker_sessions",
                saved["cells"][0]["incomplete_reason"],
            )

    def test_debug_disposition_stops_without_removing_agent_container(self) -> None:
        def run(command: list[str], **_kwargs):
            if command[:2] == ["docker", "stop"]:
                return subprocess.CompletedProcess(command, 0, "container-id\n", "")
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"Status": "exited", "Running": False, "ExitCode": 0}),
                "",
            )

        with mock.patch.object(runtime, "_run", side_effect=run) as run_command:
            disposition = runtime._dispose_agent_container(
                "container-id", retain=True
            )

        self.assertEqual(
            run_command.call_args_list[0].args[0],
            ["docker", "stop", "--time", "10", "container-id"],
        )
        self.assertEqual(
            run_command.call_args_list[1].args[0],
            [
                "docker",
                "inspect",
                "--format",
                "{{json .State}}",
                "container-id",
            ],
        )
        self.assertTrue(disposition["retained"])
        self.assertTrue(disposition["stopped"])
        self.assertFalse(disposition["removed"])
        self.assertEqual(disposition["observed_state"]["status"], "exited")

    def test_official_harness_is_recorded_once_and_normalized(self) -> None:
        profile = self.profile("sympy-16886-codex-smoke")
        with self.temporary_directory() as temporary:
            campaign = Path(temporary)
            evaluator = campaign / "evaluator"
            evaluator.mkdir()
            (evaluator / "instances.json").write_text(
                json.dumps([{"instance_id": "task"}]) + "\n",
                encoding="utf-8",
            )
            patch_path = campaign / "model.patch"
            patch_path.write_text("diff --git a/a b/a\n")
            cell = {
                "task_id": "task",
                "method": "plain-codex",
                "model": "gpt-5.6-sol",
                "patch_file": "model.patch",
                "evaluation": {"state": "pending", "calls": 0},
            }
            manifest = {
                "schema_version": 1,
                "campaign_id": "official-once",
                "profile_snapshot": profile,
                "cells": [cell],
            }
            write_json(campaign / "campaign.json", manifest)
            report_path = (
                evaluator
                / "logs/run_evaluation/official-once"
                / "bench-goal-plus-plain-codex-gpt-5.6-sol"
                / "task/report.json"
            )
            report_path.parent.mkdir(parents=True)
            write_json(
                report_path,
                {
                    "task": {
                        "patch_successfully_applied": True,
                        "resolved": False,
                    }
                },
            )
            completed = subprocess.CompletedProcess([], 0, "official output", "")
            with mock.patch.object(runtime, "_run", return_value=completed):
                result = runtime._official_evaluation(campaign, manifest, cell)

            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["calls"], 1)
            self.assertTrue(result["patch_applied"])
            self.assertFalse(result["resolved"])
            self.assertIn("swebench.harness.run_evaluation", result["command"])
            self.assertEqual(
                result["command"][result["command"].index("--cache_level") + 1],
                "instance",
            )
            self.assertEqual(
                result["command"][result["command"].index("--clean") + 1],
                "false",
            )
            self.assertEqual(
                result["command"][result["command"].index("--force_rebuild") + 1],
                "false",
            )
            persisted = json.loads((campaign / "campaign.json").read_text())
            self.assertEqual(persisted["cells"][0]["evaluation"]["calls"], 1)
            with self.assertRaisesRegex(SweBenchContractError, "already been attempted"):
                runtime._official_evaluation(campaign, manifest, cell)

    def test_finalize_preserves_raw_metric_and_report_kind(self) -> None:
        with self.temporary_directory() as temporary:
            campaign = Path(temporary)
            manifest = {
                "schema_version": 1,
                "campaign_id": "finalize-test",
                "benchmark_id": "swe-bench-verified",
                "state": "completed",
                "budget": {
                    "wall_time_seconds": 300,
                    "live_search_concurrency": 1,
                    "cell_concurrency": 1,
                    "attempts": 1,
                },
                "dataset": {"name": "dataset", "revision": "a" * 40},
                "source": {"swebench_commit": "b" * 40},
                "cells": [
                    {
                        "task_id": "task",
                        "cell_id": "cell",
                        "method": "plain-codex",
                        "model": "gpt-5.6-sol",
                        "reasoning_effort": "medium",
                        "state": "completed",
                        "image": "image:latest",
                        "base_commit": "c" * 40,
                        "patch_file": "model.patch",
                        "agent": {
                            "patch_exists": True,
                            "runtime_seconds": 300.0,
                            "total_runtime_seconds": 312.0,
                            "setup_runtime_seconds": 5.0,
                            "finalization_grace_seconds": 7.0,
                            "usage": {"coverage": "unavailable"},
                            "container": {
                                "id": "container-id",
                                "name": "bgp-swe-agent-test",
                                "cleanup": {
                                    "policy": "retain",
                                    "removed": False,
                                    "retained": True,
                                    "stopped": True,
                                },
                            },
                        },
                        "evaluation": {
                            "state": "completed",
                            "calls": 1,
                            "resolved": False,
                            "patch_applied": True,
                            "runtime_seconds": 10.0,
                        },
                    }
                ],
            }
            write_json(campaign / "campaign.json", manifest)

            summary = reporting.finalize_campaign(campaign)
            kind, loaded, markdown, source_json = benchmark_report.load_campaign(campaign)

            self.assertEqual(summary["report_kind"], "swe-bench-verified")
            self.assertEqual(summary["records"][0]["protocol"]["direction"], "maximize")
            self.assertIs(summary["records"][0]["score"]["raw_metrics"]["resolved"], False)
            self.assertEqual(
                summary["records"][0]["execution"]["finalization_grace_seconds"], 7.0
            )
            self.assertTrue(
                summary["records"][0]["execution"]["agent_container"]["cleanup"][
                    "retained"
                ]
            )
            self.assertEqual(kind, "swe-bench-verified")
            self.assertEqual(loaded["aggregates"]["resolved_count"], 0)
            self.assertEqual(markdown, campaign / "campaign-summary.md")
            self.assertEqual(source_json, campaign / "campaign-summary.json")

    def test_finalize_recovers_evidence_parser_downgrade(self) -> None:
        profile = self.profile("sympy-16886-goal-plus-pi-luna-high-smoke")
        with self.temporary_directory() as temporary:
            campaign = Path(temporary)
            state_root = campaign / "cells/goal-plus-pi/goal-plus-state"
            self.write_goal_plus_state(
                state_root,
                supplemental_evaluation=True,
                evidence_annotations=True,
            )
            patch_file = campaign / "cells/goal-plus-pi/model.patch"
            patch_file.write_text("diff --git a/a b/a\n", encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "campaign_id": "goal-plus-revalidation-test",
                "benchmark_id": "swe-bench-verified",
                "state": "partial",
                "profile_snapshot": profile,
                "budget": {
                    "wall_time_seconds": 1800,
                    "live_search_concurrency": 1,
                    "cell_concurrency": 1,
                    "attempts": 1,
                },
                "dataset": {"name": "dataset", "revision": "a" * 40},
                "source": {"swebench_commit": "b" * 40},
                "cells": [
                    {
                        "task_id": "task",
                        "cell_id": "goal-plus-pi--task",
                        "task_file": "cells/goal-plus-pi/task.json",
                        "patch_file": "cells/goal-plus-pi/model.patch",
                        "method": "goal-plus-pi",
                        "model": profile["model"],
                        "reasoning_effort": "high",
                        "state": "partial",
                        "incomplete_reason": (
                            "Goal Plus completion evidence failed: visible_verifiers"
                        ),
                        "image": "image:latest",
                        "base_commit": "c" * 40,
                        "agent": {
                            "state": "partial",
                            "patch_exists": True,
                            "runtime": {
                                "evidence_annotator": (
                                    runtime._goal_plus_evidence_annotator_public(
                                        profile
                                    )
                                )
                            },
                            "goal_plus_closeout": {"completed": True, "returncode": 0},
                            "goal_plus": {
                                "completion": {
                                    "passed": False,
                                    "reason": (
                                        "Goal Plus completion evidence failed: "
                                        "visible_verifiers"
                                    ),
                                },
                                "export": {
                                    "completed": True,
                                    "destination": str(state_root),
                                },
                            },
                            "container": {
                                "cleanup": {
                                    "removed": True,
                                    "retained": False,
                                }
                            },
                        },
                        "evaluation": {
                            "state": "completed",
                            "calls": 1,
                            "resolved": True,
                            "patch_applied": True,
                        },
                    }
                ],
            }
            write_json(campaign / "campaign.json", manifest)

            summary = reporting.finalize_campaign(campaign)
            recovered = read_json(campaign / "campaign.json")

            self.assertEqual(summary["state"], "completed")
            self.assertEqual(summary["records"][0]["status"], "succeeded")
            self.assertTrue(
                summary["records"][0]["protocol"]["goal_plus"]["completion"][
                    "passed"
                ]
            )
            goal_plus_run = summary["records"][0]["protocol"]["goal_plus"][
                "runs"
            ][0]
            self.assertIsNone(goal_plus_run["legacy_acceptance_view_contract"])
            self.assertIsNotNone(
                goal_plus_run["evidence_annotations"]["entries"][0]["view"][
                    "supplemental_evaluation"
                ]
            )
            self.assertEqual(
                summary["records"][0]["execution"]["usage"]["view_agent"][
                    "input_tokens"
                ],
                40,
            )
            self.assertEqual(recovered["state"], "completed")
            self.assertEqual(recovered["cells"][0]["state"], "completed")
            self.assertNotIn("incomplete_reason", recovered["cells"][0])
            revalidation = recovered["cells"][0]["evidence_revalidation"]
            self.assertEqual(revalidation["prior_state"], "partial")
            self.assertIn("visible_verifiers", revalidation["prior_incomplete_reason"])
            self.assertTrue(revalidation["completion_passed"])

            recovered["state"] = "partial"
            recovered_cell = recovered["cells"][0]
            recovered_cell["state"] = "partial"
            recovered_cell["agent"]["state"] = "partial"
            recovered_cell["evaluation"]["calls"] = 2
            recovered_cell.pop("evidence_revalidation", None)
            write_json(campaign / "campaign.json", recovered)

            rejected = reporting.finalize_campaign(campaign)
            self.assertEqual(rejected["state"], "partial")
            self.assertEqual(rejected["records"][0]["status"], "partial")
            self.assertIn(
                "call count is not exactly one",
                rejected["records"][0]["incomplete_reason"],
            )


if __name__ == "__main__":
    unittest.main()
