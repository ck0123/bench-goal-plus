from __future__ import annotations

import base64
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from bench_goal_plus.application import BenchmarkAgent
from bench_goal_plus.catalog import Catalog
from bench_goal_plus.cli import build_parser
from bench_goal_plus.errors import ContractError
from bench_goal_plus.runners.factory import create_runner
from bench_goal_plus.search_scheduler import (
    GoalPlusSearchScheduler,
    render_search_scheduler_instructions,
    search_scheduler_from_json,
    summarize_worker_concurrency,
)
from experiments.edgebench.controller.runtime import cell_environment
from experiments.edgebench.controller.evidence import (
    goal_plus_completion_evidence,
    goal_plus_stats,
)
from experiments.openevolve_compare.experiment import goal_plus_incomplete_reason


SCHEDULER_ARGUMENTS = {
    "search_scheduler_host": "pi-rpc",
    "search_scheduler_model": "bench-openai/gpt-5.6-sol",
    "search_scheduler_reasoning_effort": "low",
    "search_scheduler_timeout_seconds": 180,
    "search_scheduler_reward": (
        "evidence_llm_value/v1 @prompt rubric要关注修改代码量"
    ),
    "search_scheduler_allocation": "value_guided_replace/v1",
}


class SearchSchedulerContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = BenchmarkAgent(catalog=Catalog())

    def test_cli_parses_unbounded_candidate_limit(self) -> None:
        args = build_parser().parse_args(
            [
                "plan",
                "--benchmark",
                "local-vliw",
                "--method",
                "goal-plus-pi",
                "--model",
                "test-model",
                "--reasoning-effort",
                "low",
                "--wall-time-seconds",
                "180",
                "--live-search-concurrency",
                "2",
                "--max-candidates",
                "null",
                "--search-scheduler-host",
                "pi-rpc",
                "--search-scheduler-model",
                "bench-openai/gpt-5.6-sol",
                "--search-scheduler-reasoning-effort",
                "low",
                "--search-scheduler-timeout-seconds",
                "180",
                "--search-scheduler-reward",
                "evidence_llm_value/v3",
                "--search-scheduler-allocation",
                "value_guided_replace/v2",
            ]
        )

        self.assertIsNone(args.max_candidates)
        self.assertEqual(args.search_scheduler_host, "pi-rpc")

    def test_unbounded_config_reaches_common_runner_without_mapping_to_k(self) -> None:
        spec = self.agent.resolve_spec(
            target_ids=("local-vliw",),
            campaign_id="scheduler-unbounded",
            methods=("goal-plus-pi",),
            model="test-model",
            reasoning_effort="low",
            wall_time_seconds=180,
            live_search_concurrency=2,
            max_candidates=None,
            **SCHEDULER_ARGUMENTS,
        )

        self.assertIsNotNone(spec.search_scheduler)
        assert spec.search_scheduler is not None
        self.assertIsNone(spec.search_scheduler.max_candidates)
        self.assertEqual(
            spec.as_dict()["search_scheduler"], spec.search_scheduler.as_dict()
        )
        command, _ = create_runner(spec.runner).prepare_commands(spec)
        config_index = command[0].index("--search-scheduler-config-json") + 1
        frozen = json.loads(command[0][config_index])
        self.assertIsNone(frozen["max_candidates"])
        self.assertEqual(frozen["search_scheduler"]["host"], "pi-rpc")
        self.assertEqual(
            frozen["search_scheduler"]["reward"],
            "evidence_llm_value/v1 @prompt rubric要关注修改代码量",
        )
        self.assertEqual(
            frozen["search_scheduler"]["allocation"],
            "value_guided_replace/v1",
        )

    def test_disabled_scheduler_does_not_change_plan_shape(self) -> None:
        spec = self.agent.resolve_spec(
            target_ids=("local-vliw",),
            campaign_id="scheduler-disabled",
            methods=("goal-plus-pi",),
            model="test-model",
            reasoning_effort="low",
            wall_time_seconds=180,
            live_search_concurrency=1,
        )

        self.assertNotIn("search_scheduler", spec.as_dict())

    def test_candidate_limit_is_independent_but_must_cover_live_k(self) -> None:
        with self.assertRaisesRegex(ContractError, "at least K"):
            self.agent.resolve_spec(
                target_ids=("local-vliw",),
                methods=("goal-plus-pi",),
                model="test-model",
                reasoning_effort="low",
                wall_time_seconds=180,
                live_search_concurrency=3,
                max_candidates=2,
                **SCHEDULER_ARGUMENTS,
            )

    def test_scheduler_timeout_is_left_to_goal_plus(self) -> None:
        arguments = {**SCHEDULER_ARGUMENTS, "search_scheduler_timeout_seconds": 1801}
        spec = self.agent.resolve_spec(
            target_ids=("local-vliw",),
            methods=("goal-plus-pi",),
            model="test-model",
            reasoning_effort="low",
            wall_time_seconds=180,
            live_search_concurrency=2,
            max_candidates=None,
            **arguments,
        )

        assert spec.search_scheduler is not None
        self.assertEqual(spec.search_scheduler.timeout_seconds, 1801)

    def test_custom_scheduler_policy_strings_are_forwarded(self) -> None:
        config = GoalPlusSearchScheduler(
            host="custom-host",
            model="custom/model",
            reasoning_effort="custom-effort",
            timeout_seconds=180,
            reward="my_reward/v9 @prompt prefer smaller diffs",
            allocation="my_allocator/v7 @prompt use domain policy",
        )

        self.assertEqual(
            config.scheduler_spec["reward"],
            "my_reward/v9 @prompt prefer smaller diffs",
        )
        self.assertEqual(
            config.scheduler_spec["allocation"],
            "my_allocator/v7 @prompt use domain policy",
        )

    def test_worker_concurrency_counts_replacements_without_false_overlap(self) -> None:
        summary = summarize_worker_concurrency(
            [
                {
                    "candidate_id": "c001",
                    "started_at": "2026-09-03T00:00:00Z",
                    "ended_at": "2026-09-03T00:01:00Z",
                },
                {
                    "candidate_id": "c002",
                    "started_at": "2026-09-03T00:00:00Z",
                    "ended_at": "2026-09-03T00:02:00Z",
                },
                {
                    "candidate_id": "c003",
                    "started_at": "2026-09-03T00:01:00Z",
                    "ended_at": "2026-09-03T00:03:00Z",
                },
            ]
        )

        self.assertEqual(summary["max_live_workers"], 2)
        self.assertEqual(summary["candidate_ids"], ["c001", "c002", "c003"])

    def test_scheduler_rejects_non_goal_plus_method(self) -> None:
        with self.assertRaisesRegex(ContractError, "explicit Goal Plus methods"):
            self.agent.resolve_spec(
                target_ids=("local-vliw",),
                methods=("plain-codex",),
                model="test-model",
                reasoning_effort="low",
                wall_time_seconds=180,
                live_search_concurrency=1,
                max_candidates=None,
                **SCHEDULER_ARGUMENTS,
            )

    def test_prompt_freezes_exact_scheduler_and_unbounded_limit(self) -> None:
        config = GoalPlusSearchScheduler(
            host="pi-rpc",
            model="bench-openai/gpt-5.6-sol",
            reasoning_effort="low",
            timeout_seconds=180,
            reward="evidence_llm_value/v3 @prompt rubric要关注修改代码量",
            allocation="value_guided_replace/v2",
            max_candidates=None,
        )

        text = render_search_scheduler_instructions(config)

        self.assertIn("budget.max_candidates=null", text)
        self.assertIn('strategy.orchestration_mode="adaptive_search"', text)
        self.assertIn('workspace.backend="git_worktree"', text)
        self.assertIn('"host":"pi-rpc"', text)
        self.assertIn("independent live-worker limit", text)
        self.assertEqual(search_scheduler_from_json(config.to_json()), config)

    def test_edgebench_bridge_keeps_structured_manifest_and_safe_prompt_value(self) -> None:
        config = GoalPlusSearchScheduler(
            host="pi-rpc",
            model="bench-openai/gpt-5.6-sol",
            reasoning_effort="low",
            timeout_seconds=180,
            reward="evidence_llm_value/v3 @prompt prefer smaller diffs",
            allocation="value_guided_replace/v2",
            max_candidates=None,
        )
        cell = {
            "method": "goal-plus-pi",
            "sforge_agent": "pi-goal-plus",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "low",
            "inner_search_concurrency": 2,
            "worker_runtime_seconds": 120,
            "internet": False,
            "goal_plus_config": {"search_scheduler": config.as_dict()},
            "goal_plus_source": {"source_dir": "third_party/muyuan/plugins/goal-plus"},
        }

        environment = cell_environment(
            cell,
            api_base_url="https://api.example.invalid/v1",
        )
        entries = dict(
            item.split("=", 1)
            for item in environment["SFORGE_AGENT_EXTRA_ENV"].split(",")
        )
        encoded = entries[
            "SFORGE_GOAL_PLUS_SEARCH_SCHEDULER_INSTRUCTIONS_B64"
        ]
        instructions = base64.urlsafe_b64decode(encoded).decode("utf-8")

        self.assertIn("budget.max_candidates=null", instructions)
        self.assertIn('"allocation":"value_guided_replace/v2"', instructions)
        self.assertIn('strategy.orchestration_mode="adaptive_search"', instructions)

    def test_common_completion_separates_initial_k_from_cumulative_candidates(self) -> None:
        state = {
            "goals": [
                {
                    "goal_plus_id": "gp_0001",
                    "status": "complete",
                    "linked_run_id": "run_0001",
                }
            ],
            "runs": [
                {
                    "run_id": "run_0001",
                    "search_scheduler_enabled": True,
                    "search_scheduler": {
                        "host": "pi-rpc",
                        "model": "bench-openai/gpt-5.6-sol",
                        "reasoning_effort": "low",
                        "timeout_seconds": 180,
                        "reward": "evidence_llm_value/v3",
                        "allocation": "value_guided_replace/v2",
                    },
                    "orchestration_mode": "adaptive_search",
                    "max_parallel": 2,
                    "max_candidates": None,
                    "candidate_count": 4,
                    "initial_candidate_count": 2,
                    "initial_candidate_ids": ["c001", "c002"],
                    "bound_session_counts_by_candidate": {
                        "c001": 1,
                        "c002": 1,
                        "c003": 1,
                        "c004": 1,
                    },
                    "worker_verified_candidate_count": 2,
                    "worker_budget": {},
                    "worker_concurrency": {
                        "invalid_interval_count": 0,
                        "candidate_ids": ["c001", "c002", "c003", "c004"],
                        "max_live_workers": 2,
                    },
                    "initial_worker_concurrency": {
                        "invalid_interval_count": 0,
                        "candidate_ids": ["c001", "c002"],
                        "max_live_workers": 2,
                    },
                }
            ],
        }

        self.assertIsNone(
            goal_plus_incomplete_reason(state, expected_concurrency=2)
        )
        state["runs"][0]["max_candidates"] = 3
        self.assertIn(
            "outside the scheduler contract",
            goal_plus_incomplete_reason(state, expected_concurrency=2) or "",
        )

    def test_edgebench_completion_allows_scheduler_replacements_within_limit(self) -> None:
        cell = {
            "method": "goal-plus-pi",
            "inner_search_concurrency": 2,
            "outer_replicas": 1,
            "goal_plus_config": {
                "search_scheduler": {
                    "max_candidates": None,
                    "search_scheduler": {
                        "host": "pi-rpc",
                        "model": "bench-openai/gpt-5.6-sol",
                        "reasoning_effort": "low",
                        "timeout_seconds": 180,
                        "reward": "evidence_llm_value/v3",
                        "allocation": "value_guided_replace/v2",
                    },
                }
            },
        }
        observations = [
            {
                "goal_plus": {
                    "candidates": 4,
                    "initial_candidates": 2,
                    "agent_sessions": 4,
                    "initial_agent_sessions": 2,
                    "worker_verifier_runs": 4,
                    "verifier_candidate_ids": ["c001", "c002", "c003", "c004"],
                    "selected_candidate_ids": ["c004"],
                    "candidate_ids": ["c001", "c002", "c003", "c004"],
                    "initial_candidate_ids": ["c001", "c002"],
                    "search_run_contracts": [
                        {
                            "frozen_spec_present": True,
                            "max_parallel": 2,
                            "max_candidates": None,
                            "orchestration_mode": "adaptive_search",
                            "search_scheduler": {
                                "host": "pi-rpc",
                                "model": "bench-openai/gpt-5.6-sol",
                                "reasoning_effort": "low",
                                "timeout_seconds": 180,
                                "reward": "evidence_llm_value/v3",
                                "allocation": "value_guided_replace/v2",
                            },
                        }
                    ],
                    "worker_concurrency": {
                        "invalid_interval_count": 0,
                        "candidate_ids": ["c001", "c002", "c003", "c004"],
                        "max_live_workers": 2,
                    },
                    "initial_worker_concurrency": {
                        "invalid_interval_count": 0,
                        "candidate_ids": ["c001", "c002"],
                        "max_live_workers": 2,
                    },
                },
                "agent_events": {"goal_plus": {}},
            }
        ]

        evidence = goal_plus_completion_evidence(
            cell, observations, valid_trajectories=1
        )

        self.assertTrue(evidence["passed"])
        self.assertEqual(evidence["cumulative_candidate_count"], 4)
        self.assertEqual(evidence["cumulative_agent_session_count"], 4)
        self.assertEqual(evidence["actual_subagent_count"], 2)

        observations[0]["goal_plus"]["search_run_contracts"][0][
            "orchestration_mode"
        ] = "parallel_loops"
        self.assertFalse(
            goal_plus_completion_evidence(
                cell, observations, valid_trajectories=1
            )["passed"]
        )
        observations[0]["goal_plus"]["search_run_contracts"][0][
            "orchestration_mode"
        ] = "adaptive_search"
        observations[0]["goal_plus"]["worker_concurrency"][
            "max_live_workers"
        ] = 3
        self.assertFalse(
            goal_plus_completion_evidence(
                cell, observations, valid_trajectories=1
            )["passed"]
        )
        observations[0]["goal_plus"]["worker_concurrency"][
            "max_live_workers"
        ] = 2

        observations[0]["goal_plus"]["initial_agent_sessions"] = 1
        self.assertFalse(
            goal_plus_completion_evidence(
                cell, observations, valid_trajectories=1
            )["passed"]
        )

    def test_edgebench_archive_reads_frozen_scheduler_and_worker_peak(self) -> None:
        Path(".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=".tmp") as temporary:
            task_run = Path(temporary) / "task-run"
            state_root = Path(temporary) / "state" / ".gp"
            run_dir = state_root / "runs" / "run_0001"
            spec_dir = state_root / "specs" / "spec_0001"
            goal_dir = state_root / "goal-plus" / "gp_0001"
            job_dir = (
                state_root
                / "host-pools"
                / "pi"
                / "pool_0001"
                / "jobs"
                / "job_0001"
            )
            for directory in (run_dir, spec_dir, goal_dir, job_dir, task_run):
                directory.mkdir(parents=True, exist_ok=True)
            (run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "run_id": "run_0001",
                        "frozen_spec_id": "spec_0001",
                        "state": "promoted",
                    }
                ),
                encoding="utf-8",
            )
            (spec_dir / "frozen_spec.json").write_text(
                json.dumps(
                    {
                        "spec": {
                            "budget": {"max_parallel": 1, "max_candidates": None},
                            "strategy": {
                                "orchestration_mode": "adaptive_search",
                                "search_scheduler": {
                                    "host": "pi-rpc",
                                    "model": "bench-openai/gpt-5.6-sol",
                                    "reasoning_effort": "low",
                                    "timeout_seconds": 180,
                                    "reward": "evidence_llm_value/v3",
                                    "allocation": "value_guided_replace/v2",
                                },
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            (goal_dir / "goal.json").write_text(
                json.dumps(
                    {
                        "goal_plus_id": "gp_0001",
                        "status": "complete",
                        "linked_search": {"run_id": "run_0001"},
                    }
                ),
                encoding="utf-8",
            )
            candidate_dir = run_dir / "candidates" / "c001"
            candidate_dir.mkdir(parents=True)
            (candidate_dir / "candidate.json").write_text(
                json.dumps(
                    {
                        "candidate_id": "c001",
                        "task": {"allocation_depth": 0},
                    }
                ),
                encoding="utf-8",
            )
            (job_dir / "job.json").write_text(
                json.dumps(
                    {
                        "run_id": "run_0001",
                        "candidate_id": "c001",
                        "started_at": "2026-09-03T00:00:00Z",
                        "finished_at": "2026-09-03T00:01:00Z",
                    }
                ),
                encoding="utf-8",
            )
            with tarfile.open(task_run / "goal-plus-state.tar", "w") as archive:
                archive.add(state_root, arcname=".gp")

            stats = goal_plus_stats(task_run)

        assert stats is not None
        self.assertEqual(stats["search_run_contracts"][0]["max_parallel"], 1)
        self.assertEqual(
            stats["search_run_contracts"][0]["orchestration_mode"],
            "adaptive_search",
        )
        self.assertEqual(stats["worker_concurrency"]["max_live_workers"], 1)


if __name__ == "__main__":
    unittest.main()
