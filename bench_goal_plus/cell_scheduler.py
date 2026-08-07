"""Benchmark-neutral bounded scheduling for independent task cells."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import TypeVar


ItemT = TypeVar("ItemT")
HandleT = TypeVar("HandleT")


def execute_cell_queue(
    items: Iterable[ItemT],
    *,
    concurrency: int,
    item_id: Callable[[ItemT], str],
    start: Callable[[ItemT], HandleT | None],
    poll: Callable[[HandleT], bool],
    finish: Callable[[HandleT, bool], int],
    fail: Callable[[ItemT, Exception], int],
    record_active: Callable[[Mapping[str, HandleT]], None],
    stop_requested: Callable[[], bool],
    stop: Callable[[HandleT], None] | None = None,
    poll_interval_seconds: float = 0.25,
) -> int:
    """Run cells with a bounded active set and aggregate nonzero return codes."""

    if concurrency < 1:
        raise ValueError("cell concurrency must be positive")

    queued = list(items)
    queued_ids = [item_id(item) for item in queued]
    if len(queued_ids) != len(set(queued_ids)):
        raise ValueError("cell ids must be unique")
    pending = deque(queued)
    active: dict[str, HandleT] = {}
    overall_returncode = 0
    stop_forwarded = False
    record_active(active)

    while pending or active:
        while pending and len(active) < concurrency and not stop_requested():
            item = pending.popleft()
            key = item_id(item)
            try:
                handle = start(item)
            except Exception as error:
                returncode = fail(item, error)
                if returncode != 0:
                    overall_returncode = overall_returncode or returncode
                continue
            if handle is not None:
                active[key] = handle
                record_active(active)

        if stop_requested() and not stop_forwarded:
            if stop is not None:
                for handle in active.values():
                    stop(handle)
            stop_forwarded = True

        completed = [key for key, handle in active.items() if poll(handle)]
        if not completed:
            if not active:
                break
            time.sleep(poll_interval_seconds)
            continue

        stopping = bool(stop_requested())
        for key in completed:
            handle = active.pop(key)
            returncode = finish(handle, stopping)
            if returncode != 0:
                overall_returncode = overall_returncode or returncode
        record_active(active)

    return overall_returncode
