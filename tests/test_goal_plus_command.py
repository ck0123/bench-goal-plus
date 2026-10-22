from __future__ import annotations

import unittest

from bench_goal_plus.goal_plus_command import (
    goal_plus_command_config,
    goal_plus_entrypoint,
    render_goal_plus_command,
)


class GoalPlusCommandTests(unittest.TestCase):
    def test_host_entrypoints_are_exact(self) -> None:
        self.assertEqual(goal_plus_entrypoint("codex"), "$goal-plus")
        self.assertEqual(goal_plus_entrypoint("pi"), "/goal-plus")
        for legacy_identity in ("pi-rpc", "thinkthread", "direct"):
            with self.subTest(identity=legacy_identity), self.assertRaises(ValueError):
                goal_plus_entrypoint(legacy_identity)

    def test_render_uses_only_leading_typed_config(self) -> None:
        command = render_goal_plus_command(
            "pi",
            max_parallel=2,
            strategy="agent_guided",
            worker_model="bench-openai/gpt-5.6-luna",
            annotator_model="bench-openai/gpt-5.6-terra",
            workspace_provider="git_worktree",
            promotion_mode="apply",
        )

        self.assertEqual(
            command,
            "/goal-plus mode=autonomous max_parallel=2 "
            "workspace_provider=git_worktree promotion_mode=apply "
            "strategy=agent_guided spawn_mode=fresh workers=bench-openai/gpt-5.6-luna*2 "
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
                "workspace_provider": "git_worktree",
                "promotion_mode": "artifact_only",
                "strategy": "random",
                "spawn_mode": "fresh",
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
                "pi",
                max_parallel=1,
                strategy="random",
                worker_model="$(unsafe)",
            )


if __name__ == "__main__":
    unittest.main()
