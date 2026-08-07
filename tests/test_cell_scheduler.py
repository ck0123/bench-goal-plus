from __future__ import annotations

import unittest
from unittest import mock

from bench_goal_plus.cell_scheduler import execute_cell_queue


class CellSchedulerTest(unittest.TestCase):
    def test_bounds_active_cells_and_continues_after_failure(self) -> None:
        active = 0
        max_active = 0
        handles: list[dict[str, object]] = []

        def start(item: str) -> dict[str, object]:
            nonlocal active, max_active
            handle: dict[str, object] = {"item": item, "done": False}
            handles.append(handle)
            active += 1
            max_active = max(max_active, active)
            return handle

        def finish(handle: dict[str, object], stopping: bool) -> int:
            nonlocal active
            self.assertFalse(stopping)
            active -= 1
            return 1 if handle["item"] == "b" else 0

        def advance(_seconds: float) -> None:
            unfinished = [handle for handle in handles if not handle["done"]]
            unfinished[0]["done"] = True

        with mock.patch("bench_goal_plus.cell_scheduler.time.sleep", side_effect=advance):
            returncode = execute_cell_queue(
                ["a", "b", "c"],
                concurrency=2,
                item_id=str,
                start=start,
                poll=lambda handle: bool(handle["done"]),
                finish=finish,
                fail=lambda _item, _error: 1,
                record_active=lambda _active: None,
                stop_requested=lambda: False,
            )

        self.assertEqual(returncode, 1)
        self.assertEqual(max_active, 2)
        self.assertEqual([handle["item"] for handle in handles], ["a", "b", "c"])

    def test_forwards_stop_once_and_does_not_start_pending_cells(self) -> None:
        requested = False
        stopped: list[str] = []
        handles: list[dict[str, object]] = []

        def start(item: str) -> dict[str, object]:
            handle: dict[str, object] = {"item": item, "done": False}
            handles.append(handle)
            return handle

        def advance(_seconds: float) -> None:
            nonlocal requested
            requested = True

        def stop(handle: dict[str, object]) -> None:
            stopped.append(str(handle["item"]))
            handle["done"] = True

        with mock.patch("bench_goal_plus.cell_scheduler.time.sleep", side_effect=advance):
            returncode = execute_cell_queue(
                ["a", "b", "c"],
                concurrency=2,
                item_id=str,
                start=start,
                poll=lambda handle: bool(handle["done"]),
                finish=lambda _handle, stopping: 130 if stopping else 0,
                fail=lambda _item, _error: 1,
                record_active=lambda _active: None,
                stop_requested=lambda: requested,
                stop=stop,
            )

        self.assertEqual(returncode, 130)
        self.assertEqual([handle["item"] for handle in handles], ["a", "b"])
        self.assertEqual(stopped, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
