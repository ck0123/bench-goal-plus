from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from bench_runtime_paths import ensure_temp_root

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.models import GoalPlusHostCommandInvocation, SearchSpec
from goal_plus.runtime import FileSearchRuntime

from bench_goal_plus.pi_fork import preparation_ticket


class PiForkAdmissionTest(unittest.TestCase):
    def test_ticket_requires_current_preparation_and_matching_native_checkpoint(self):
        # Exercise persisted Goal/Search state, not a replacement authorization API.
        with tempfile.TemporaryDirectory(dir=ensure_temp_root("pi-fork-tests")) as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            (project / "solution.py").write_text("VALUE = 0\n")
            (project / "evaluate.py").write_text('print(\'{"score": 0}\')\n')
            root = base / ".gp"
            goals = FileGoalPlusRuntime(root)
            raw = "/goal-plus spawn_mode=fork Improve score"
            goal = goals.start_from_host_command(
                "spawn_mode=fork Improve score", source_path=str(project),
                invocation=GoalPlusHostCommandInvocation(
                    agent_harness="pi", transport="pi_extension_command", command="/goal-plus",
                    action="start", session_id="main", native_entry_id="turn",
                    invocation_id="pi:test:start", raw_input_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                    invoked_at="2026-10-26T00:00:00Z",
                ),
            )
            goals.activate_session(goal.goal_plus_id, {
                "agent_harness": "pi", "session_id": "main",
                "attached_at": goal.created_at, "last_seen_at": goal.updated_at,
            })
            search = FileSearchRuntime(root)
            spec = SearchSpec.model_validate({
                "objective": "Improve score", "metric_name": "score", "metric_direction": "maximize",
                "source_path": str(project), "edit_surface": {"allow": ["solution.py"], "deny": ["evaluate.py"]},
                "budget": {"max_parallel": 2}, "workspace": {"provider": "copy"},
                "strategy": {"name": "random", "initial_context": "fork", "llm_validation": "off"},
                "process_verifiers": [{"name": "score", "role": "ranking_signal",
                                       "command": ["python3", "evaluate.py"], "timeout_seconds": 10}],
            })
            frozen = search.freeze_spec(spec, [project / "evaluate.py"], agent_harness="pi",
                                        goal_plus_id=goal.goal_plus_id)
            run_id = search.create_run(frozen.frozen_spec_id)
            goals.link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
            search.start_batch(run_id, search.plan_next(run_id, requested_k=2).plan_id)
            # Wire timestamps have second precision; preparation must follow batch.
            time.sleep(1.05)
            goals.set_status(goal.goal_plus_id, "blocked", reason="fork_checkpoint_ready", evidence=[{"run_id": run_id}])
            stopped = goals.status(goal.goal_plus_id)
            sessions = base / "sessions"
            sessions.mkdir()
            native = sessions / "stamp_main.jsonl"
            entries = [
                {"type": "session", "id": "main"},
                {"type": "message", "id": "done", "parentId": None,
                 "message": {"role": "assistant", "stopReason": "stop", "content": []}},
                {"type": "custom", "id": "marker", "parentId": "done",
                 "customType": "goal-plus-fork-checkpoint", "data": {
                     "run_id": run_id, "goal_plus_id": goal.goal_plus_id,
                     "goal_revision": stopped.goal_revision, "control_version": stopped.control.version}},
            ]
            def save():
                native.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
            save()
            ticket = preparation_ticket(root, sessions)
            self.assertEqual(ticket["session_id"], "main")
            self.assertEqual(ticket["control_version"], stopped.control.version)
            self.assertEqual(ticket["fork_preparation_event_id"], goals.state.read_events(goal.goal_plus_id)[-1]["event_id"])
            entries[-1]["data"]["control_version"] += 1
            save()
            with self.assertRaisesRegex(RuntimeError, "current preparation marker"):
                preparation_ticket(root, sessions)
            entries[-1]["data"]["control_version"] -= 1
            save()
            goals.set_status(goal.goal_plus_id, "blocked", reason="dependency unavailable", evidence=[{"run_id": run_id}])
            with self.assertRaisesRegex(RuntimeError, "ordinary stop"):
                preparation_ticket(root, sessions)
