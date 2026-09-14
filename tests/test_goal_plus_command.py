from __future__ import annotations

import unittest

from bench_goal_plus.goal_plus_command import (
    GOAL_PLUS_CONTROLLER_OWNED_CLOSEOUT_TOOLS,
    goal_plus_command_config,
    goal_plus_entrypoint,
    goal_plus_worker_host,
    render_goal_plus_command,
)


class GoalPlusCommandTests(unittest.TestCase):
    def test_controller_owned_closeout_tools_exclude_worker_operations(self) -> None:
        self.assertEqual(
            GOAL_PLUS_CONTROLLER_OWNED_CLOSEOUT_TOOLS,
            (
                "goal_plus_record_search_result",
                "goal_plus_set_status",
                "search_promote",
                "search_report",
                "search_select",
            ),
        )
        self.assertNotIn(
            "search_run_verifier", GOAL_PLUS_CONTROLLER_OWNED_CLOSEOUT_TOOLS
        )

    def test_host_entrypoints_are_exact(self) -> None:
        self.assertEqual(goal_plus_entrypoint("codex"), "$goal-plus")
        self.assertEqual(goal_plus_entrypoint("pi-rpc"), "/goal-plus")

    def test_worker_host_is_derived_from_frozen_native_host(self) -> None:
        self.assertEqual(goal_plus_worker_host("codex", "git_worktree"), "codex")
        self.assertEqual(goal_plus_worker_host("pi", "git_worktree"), "pi-rpc")
        self.assertEqual(goal_plus_worker_host("pi", "thinkthread"), "thinkthread")
        with self.assertRaisesRegex(ValueError, "Codex does not support"):
            goal_plus_worker_host("codex", "thinkthread")

    def test_render_uses_only_leading_typed_config(self) -> None:
        command = render_goal_plus_command(
            "pi-rpc",
            max_parallel=2,
            strategy="agent_guided",
            worker_model="bench-openai/gpt-5.6-luna",
            annotator_model="bench-openai/gpt-5.6-terra",
            workspace_backend="git_worktree",
            promotion_mode="apply",
        )

        self.assertEqual(
            command,
            "/goal-plus mode=autonomous max_parallel=2 "
            "workspace_backend=git_worktree promotion_mode=apply "
            "strategy=agent_guided workers=bench-openai/gpt-5.6-luna*2 "
            "annotator=bench-openai/gpt-5.6-terra",
        )
        self.assertNotIn(" -- ", command)

    def test_config_matches_manifest_shape(self) -> None:
        self.assertEqual(
            goal_plus_command_config(
                max_parallel=1,
                strategy="random",
                worker_model="gpt-5.6-sol",
                annotator_model="gpt-5.5",
                promotion_mode="artifact_only",
            ),
            {
                "mode": "autonomous",
                "max_parallel": 1,
                "workspace_backend": "git_worktree",
                "promotion_mode": "artifact_only",
                "strategy": "random",
                "workers": "gpt-5.6-sol*1",
                "annotator": "gpt-5.5",
            },
        )

    def test_invalid_values_fail_before_launch(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_parallel must be positive"):
            render_goal_plus_command(
                "codex",
                max_parallel=0,
                strategy="agent_guided",
                worker_model="gpt-5.6-sol",
            )
        with self.assertRaisesRegex(ValueError, "uncounted model"):
            render_goal_plus_command(
                "codex",
                max_parallel=2,
                strategy="agent_guided",
                worker_model="gpt-5.6-sol*2",
            )
        with self.assertRaisesRegex(ValueError, "safe model token"):
            render_goal_plus_command(
                "pi-rpc",
                max_parallel=1,
                strategy="random",
                worker_model="$(unsafe)",
            )


if __name__ == "__main__":
    unittest.main()
