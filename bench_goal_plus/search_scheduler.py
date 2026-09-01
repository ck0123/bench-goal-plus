"""Pass optional Goal Plus Adaptive Search configuration through benchmark runners."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from .errors import ContractError


SEARCH_SCHEDULER_FIELDS = (
    "host",
    "model",
    "reasoning_effort",
    "timeout_seconds",
    "reward",
    "allocation",
)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def summarize_worker_concurrency(
    intervals: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarize persisted worker intervals for campaign evidence."""

    normalized: list[dict[str, str]] = []
    invalid_interval_count = 0
    events: list[tuple[datetime, int]] = []
    candidate_ids: set[str] = set()
    for interval in intervals:
        candidate_id = interval.get("candidate_id")
        started_at = interval.get("started_at")
        ended_at = interval.get("ended_at")
        started = _parse_timestamp(started_at)
        ended = _parse_timestamp(ended_at)
        if (
            not isinstance(candidate_id, str)
            or not candidate_id
            or started is None
            or ended is None
            or ended <= started
        ):
            invalid_interval_count += 1
            continue
        normalized.append(
            {
                "candidate_id": candidate_id,
                "started_at": str(started_at),
                "ended_at": str(ended_at),
            }
        )
        candidate_ids.add(candidate_id)
        events.extend(((started, 1), (ended, -1)))

    live_workers = 0
    max_live_workers = 0
    for _, delta in sorted(events, key=lambda event: (event[0], event[1])):
        live_workers += delta
        max_live_workers = max(max_live_workers, live_workers)
    return {
        "interval_count": len(normalized),
        "invalid_interval_count": invalid_interval_count,
        "candidate_ids": sorted(candidate_ids),
        "max_live_workers": max_live_workers if normalized else None,
        "intervals": normalized,
    }


def parse_max_candidates(value: str) -> int | None:
    normalized = value.strip().lower()
    if normalized in {"null", "none", "unbounded"}:
        return None
    try:
        parsed = int(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "max candidates must be a positive integer or null"
        ) from error
    if parsed < 1:
        raise argparse.ArgumentTypeError(
            "max candidates must be a positive integer or null"
        )
    return parsed


@dataclass(frozen=True)
class GoalPlusSearchScheduler:
    """Scheduler payload plus its independent cumulative candidate limit."""

    host: str
    model: str
    reasoning_effort: str
    timeout_seconds: int
    reward: str
    allocation: str
    max_candidates: int | None = None

    def __post_init__(self) -> None:
        for field in ("host", "model", "reasoning_effort", "reward", "allocation"):
            if not isinstance(getattr(self, field), str):
                raise ContractError(f"Search Scheduler {field} must be a string")
        if not isinstance(self.timeout_seconds, int) or isinstance(
            self.timeout_seconds, bool
        ):
            raise ContractError("Search Scheduler timeout_seconds must be an integer")
        if self.max_candidates is not None and (
            not isinstance(self.max_candidates, int)
            or isinstance(self.max_candidates, bool)
            or self.max_candidates < 1
        ):
            raise ContractError("max_candidates must be positive or null")

    def validate_max_candidates(self, max_parallel: int | None) -> None:
        if (
            self.max_candidates is not None
            and max_parallel is not None
            and self.max_candidates < max_parallel
        ):
            raise ContractError(
                "max_candidates must be null or at least K/max_parallel"
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GoalPlusSearchScheduler":
        missing = [field for field in SEARCH_SCHEDULER_FIELDS if field not in value]
        if missing:
            raise ContractError(
                "Search Scheduler configuration is incomplete; missing "
                + ", ".join(missing)
            )
        return cls(
            host=value["host"],
            model=value["model"],
            reasoning_effort=value["reasoning_effort"],
            timeout_seconds=value["timeout_seconds"],
            reward=value["reward"],
            allocation=value["allocation"],
            max_candidates=value.get("max_candidates"),
        )

    @property
    def scheduler_spec(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("max_candidates")
        return payload

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_candidates": self.max_candidates,
            "search_scheduler": self.scheduler_spec,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )


def add_search_scheduler_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-candidates",
        type=parse_max_candidates,
        help=(
            "cumulative unique candidate limit for Adaptive Search; use null for "
            "no cumulative limit"
        ),
    )
    parser.add_argument("--search-scheduler-host")
    parser.add_argument("--search-scheduler-model")
    parser.add_argument("--search-scheduler-reasoning-effort")
    parser.add_argument("--search-scheduler-timeout-seconds", type=int)
    parser.add_argument("--search-scheduler-reward")
    parser.add_argument("--search-scheduler-allocation")


def add_internal_search_scheduler_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--search-scheduler-config-json", help=argparse.SUPPRESS)


def resolve_search_scheduler(
    *,
    host: str | None,
    model: str | None,
    reasoning_effort: str | None,
    timeout_seconds: int | None,
    reward: str | None,
    allocation: str | None,
    max_candidates: int | None,
) -> GoalPlusSearchScheduler | None:
    fields = {
        "host": host,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "timeout_seconds": timeout_seconds,
        "reward": reward,
        "allocation": allocation,
    }
    configured = [name for name, value in fields.items() if value is not None]
    if not configured:
        if max_candidates is not None:
            raise ContractError(
                "--max-candidates requires a complete Search Scheduler configuration"
            )
        return None
    missing = [name for name, value in fields.items() if value is None]
    if missing:
        raise ContractError(
            "Search Scheduler configuration is incomplete; missing "
            + ", ".join(missing)
        )
    return GoalPlusSearchScheduler(
        host=host,
        model=model,
        reasoning_effort=reasoning_effort,
        timeout_seconds=timeout_seconds,
        reward=reward,
        allocation=allocation,
        max_candidates=max_candidates,
    )


def search_scheduler_from_namespace(
    args: argparse.Namespace,
) -> GoalPlusSearchScheduler | None:
    direct = getattr(args, "search_scheduler", None)
    if direct is not None:
        return search_scheduler_from_json(direct)
    internal = getattr(args, "search_scheduler_config_json", None)
    if internal is not None:
        return search_scheduler_from_json(internal)
    return resolve_search_scheduler(
        host=getattr(args, "search_scheduler_host", None),
        model=getattr(args, "search_scheduler_model", None),
        reasoning_effort=getattr(args, "search_scheduler_reasoning_effort", None),
        timeout_seconds=getattr(args, "search_scheduler_timeout_seconds", None),
        reward=getattr(args, "search_scheduler_reward", None),
        allocation=getattr(args, "search_scheduler_allocation", None),
        max_candidates=getattr(args, "max_candidates", None),
    )


def search_scheduler_from_json(
    value: str | Mapping[str, Any] | GoalPlusSearchScheduler | None,
) -> GoalPlusSearchScheduler | None:
    if value is None or isinstance(value, GoalPlusSearchScheduler):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise ContractError("invalid Search Scheduler config JSON") from error
    else:
        parsed = value
    if not isinstance(parsed, Mapping):
        raise ContractError("Search Scheduler config JSON must be an object")
    scheduler = parsed.get("search_scheduler")
    if not isinstance(scheduler, Mapping):
        raise ContractError("Search Scheduler config requires search_scheduler object")
    return GoalPlusSearchScheduler.from_mapping(
        {**scheduler, "max_candidates": parsed.get("max_candidates")}
    )


def render_search_scheduler_instructions(
    config: GoalPlusSearchScheduler | None,
) -> str:
    if config is None:
        return (
            "- Set `strategy.orchestration_mode=\"parallel_loops\"`. Leave "
            "`strategy.search_scheduler` unset and omit "
            "`budget.max_candidates`.\n"
        )
    scheduler = json.dumps(
        config.scheduler_spec,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    limit = "null" if config.max_candidates is None else str(config.max_candidates)
    return (
        "- Set `strategy.orchestration_mode=\"adaptive_search\"`.\n"
        "- Set `workspace.backend=\"git_worktree\"`.\n"
        f"- Set `budget.max_candidates={limit}`; keep "
        "`budget.max_parallel` as the independent live-worker limit.\n"
        f"- Set `strategy.search_scheduler` exactly to `{scheduler}`.\n"
    )


def internal_search_scheduler_args(
    config: GoalPlusSearchScheduler | None,
) -> list[str]:
    if config is None:
        return []
    return ["--search-scheduler-config-json", config.to_json()]
