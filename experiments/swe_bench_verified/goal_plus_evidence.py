"""Normalize persisted Goal Plus + Pi state for SWE-bench completion gates."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ACTIVE_POOL_STATES = {"starting", "running"}
VISIBLE_VERIFIER_SUFFIX = ".goal-plus-verifiers/visible_test_verifier.py"
VISIBLE_VERIFIER_PATH = (
    Path(__file__).resolve().parent / "verifiers" / "visible_test_verifier.py"
)


def expected_visible_verifier_sha256() -> str:
    return hashlib.sha256(VISIBLE_VERIFIER_PATH.read_bytes()).hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _linked_run_id(goal: dict[str, Any]) -> str | None:
    linked = goal.get("linked_search") or {}
    if isinstance(linked, dict) and isinstance(linked.get("run_id"), str):
        return str(linked["run_id"])
    tasks = goal.get("search_tasks")
    if isinstance(tasks, list):
        for task in reversed(tasks):
            if isinstance(task, dict) and isinstance(task.get("run_id"), str):
                return str(task["run_id"])
    current = goal.get("current_search_run_id")
    return str(current) if isinstance(current, str) and current else None


def _check(expected: Any, actual: Any, passed: bool) -> dict[str, Any]:
    return {"expected": expected, "actual": actual, "passed": bool(passed)}


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _timestamp_not_after(left: Any, right: Any) -> bool:
    left_timestamp = _timestamp(left)
    right_timestamp = _timestamp(right)
    if left_timestamp is None or right_timestamp is None:
        return False
    try:
        return left_timestamp <= right_timestamp
    except TypeError:
        return False


def _worker_overlap(leases: list[dict[str, Any]], expected_k: int) -> dict[str, Any]:
    intervals = []
    parsed = []
    for lease in leases:
        interval = lease.get("observed_interval")
        if not isinstance(interval, dict):
            continue
        started_at = _timestamp(interval.get("started_at"))
        ended_at = _timestamp(interval.get("ended_at"))
        candidate_id = lease.get("candidate_id")
        if (
            started_at is None
            or ended_at is None
            or not isinstance(candidate_id, str)
            or not candidate_id
        ):
            continue
        try:
            valid_order = started_at < ended_at
        except TypeError:
            valid_order = False
        if not valid_order:
            continue
        intervals.append(
            {
                "candidate_id": candidate_id,
                "agent_session_id": lease.get("agent_session_id"),
                **interval,
            }
        )
        parsed.append((candidate_id, started_at, ended_at))

    overlap_seconds = 0.0
    if len(parsed) == expected_k:
        try:
            overlap_seconds = max(
                0.0,
                (
                    min(item[2] for item in parsed)
                    - max(item[1] for item in parsed)
                ).total_seconds(),
            )
        except TypeError:
            overlap_seconds = 0.0
    candidate_ids = {item[0] for item in parsed}
    return {
        "expected_k": expected_k,
        "intervals": intervals,
        "overlap_seconds": round(overlap_seconds, 3),
        "passed": bool(
            len(parsed) == expected_k
            and len(candidate_ids) == expected_k
            and overlap_seconds > 0
        ),
    }


def _observed_autoresearch_lease(
    lease: dict[str, Any], session: dict[str, Any], *, run_state: Any
) -> dict[str, Any]:
    minimum_runtime = int(lease.get("min_runtime_seconds") or 0)
    minimum_verifiers = int(lease.get("min_verifier_runs") or 0)
    session_verifiers = int((session.get("counters") or {}).get("verifier_runs") or 0)
    observed_verifiers = max(
        int(lease.get("verifier_runs") or 0), session_verifiers
    )
    observed_elapsed = float(lease.get("elapsed_seconds") or 0)
    started_at = _timestamp(lease.get("started_at"))
    released_at = _timestamp(lease.get("released_at"))
    session_updated_at = _timestamp(session.get("updated_at"))
    if started_at is not None and session_updated_at is not None:
        observed_elapsed = max(
            observed_elapsed,
            max(0.0, (session_updated_at - started_at).total_seconds()),
        )

    released = (
        lease.get("status") == "released"
        and lease.get("release_reason") == "lease_satisfied"
    )
    terminal_session = (
        run_state == "promoted"
        and observed_elapsed >= minimum_runtime
        and observed_verifiers >= minimum_verifiers
    )
    basis = (
        "released_lease"
        if released
        else "terminal_session_timestamps"
        if terminal_session
        else "insufficient"
    )
    interval_end = released_at or session_updated_at
    observed_interval = (
        {
            "started_at": lease.get("started_at"),
            "ended_at": (
                lease.get("released_at")
                if released_at is not None
                else session.get("updated_at")
            ),
            "end_basis": (
                "released_at" if released_at is not None else "session_updated_at"
            ),
        }
        if started_at is not None and interval_end is not None
        else None
    )
    return {
        **lease,
        "observed_interval": observed_interval,
        "minimum_observation": {
            "passed": released or terminal_session,
            "basis": basis,
            "run_state": run_state,
            "observed_elapsed_seconds": round(observed_elapsed, 3),
            "observed_verifier_runs": observed_verifiers,
        },
    }


def record_completion_check(
    state: dict[str, Any],
    name: str,
    *,
    expected: Any,
    actual: Any,
    passed: bool,
) -> None:
    completion = state.setdefault("completion", {})
    checks = completion.setdefault("checks", {})
    checks[name] = _check(expected, actual, passed)
    failed = [key for key, check in checks.items() if not check.get("passed")]
    completion["passed"] = not failed
    completion["reason"] = (
        None
        if not failed
        else "Goal Plus completion evidence failed: " + ", ".join(failed)
    )


def _usage_from_sessions(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, int | float] = {}
    covered = 0
    for session in sessions:
        handle = session.get("host_handle") or {}
        metadata = handle.get("metadata") if isinstance(handle, dict) else {}
        metrics = metadata.get("pi_metrics") if isinstance(metadata, dict) else {}
        usage = metrics.get("usage_total") if isinstance(metrics, dict) else {}
        if not isinstance(usage, dict):
            continue
        covered += 1
        for name, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[name] = totals.get(name, 0) + value
    return {
        **dict(sorted(totals.items())),
        "sessions_covered": covered,
        "coverage": "persisted_pi_worker_usage" if covered else "unavailable",
    }


def _evidence_annotations(run_dir: Path, expected_iterations: int) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    states: dict[str, int] = {}
    usage: dict[str, int | float] = {}
    for path in sorted(
        run_dir.glob("candidates/*/evidence-annotations/iteration-*.json")
    ):
        task = _read_object(path)
        state = str(task.get("state") or "unknown")
        states[state] = states.get(state, 0) + 1
        task_usage = (
            task.get("usage") if isinstance(task.get("usage"), dict) else {}
        )
        for name, value in task_usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[name] = usage.get(name, 0) + value
        view = task.get("view") if isinstance(task.get("view"), dict) else None
        profile = task.get("profile") if isinstance(task.get("profile"), dict) else {}
        entries.append(
            {
                "candidate_id": task.get("candidate_id"),
                "iteration": task.get("iteration"),
                "state": state,
                "annotator_host": profile.get("host"),
                "task_context_source": task.get("task_context_source"),
                "task_context_ref": task.get("task_context_ref"),
                "task_context_sha256": task.get("task_context_sha256"),
                "supplemental_evaluation_enabled": task.get(
                    "supplemental_evaluation_enabled"
                ),
                "comparison_basis": (
                    task.get("comparison_basis")
                    if isinstance(task.get("comparison_basis"), list)
                    else None
                ),
                "view": view,
                "last_error": task.get("last_error"),
            }
        )
    completed_views = [
        item
        for item in entries
        if item["state"] == "completed"
        and isinstance(item.get("view"), dict)
        and bool(item["view"].get("description"))
    ]
    return {
        "expected_iterations": expected_iterations,
        "task_count": len(entries),
        "states": dict(sorted(states.items())),
        "entries": entries,
        "usage": {
            **dict(sorted(usage.items())),
            "tasks": len(entries),
            "coverage": "persisted Codex Evidence annotator usage",
        },
        "all_completed": bool(
            expected_iterations > 0
            and len(entries) == expected_iterations
            and len(completed_views) == expected_iterations
        ),
    }


def _global_evidence_read_receipts(
    sessions: list[dict[str, Any]],
    candidate_records: list[dict[str, Any]],
) -> dict[str, Any]:
    verifier_iterations: list[dict[str, Any]] = []
    for candidate in candidate_records:
        candidate_id = candidate.get("candidate_id")
        for iteration in candidate.get("iterations") or []:
            if not isinstance(iteration, dict):
                continue
            verifier_iterations.append(
                {
                    "candidate_id": candidate_id,
                    "iteration": iteration.get("iteration"),
                    "agent_session_id": iteration.get("agent_session_id"),
                    "created_at": iteration.get("created_at"),
                }
            )

    normalized_reads: list[dict[str, Any]] = []
    sessions_with_reads = 0
    schema_valid = True
    for session in sessions:
        session_id = session.get("agent_session_id")
        session_candidate_id = session.get("candidate_id")
        reads = session.get("global_evidence_reads")
        if not isinstance(reads, list):
            schema_valid = False
            continue
        if reads:
            sessions_with_reads += 1
        for read in reads:
            if not isinstance(read, dict):
                schema_valid = False
                continue
            completed_views = read.get("completed_views")
            read_timestamp = _timestamp(read.get("read_at"))
            valid = (
                isinstance(read.get("read_at"), str)
                and bool(read["read_at"])
                and read_timestamp is not None
                and isinstance(read.get("evidence_count"), int)
                and not isinstance(read.get("evidence_count"), bool)
                and read["evidence_count"] >= 0
                and isinstance(read.get("completed_view_count"), int)
                and not isinstance(read.get("completed_view_count"), bool)
                and read["completed_view_count"] >= 0
                and isinstance(
                    read.get("completed_supplemental_evaluation_count"), int
                )
                and not isinstance(
                    read.get("completed_supplemental_evaluation_count"), bool
                )
                and read["completed_supplemental_evaluation_count"] >= 0
                and isinstance(completed_views, list)
            )
            references = []
            if isinstance(completed_views, list):
                for reference in completed_views:
                    reference_valid = (
                        isinstance(reference, dict)
                        and isinstance(reference.get("candidate_id"), str)
                        and bool(reference["candidate_id"])
                        and isinstance(reference.get("iteration"), int)
                        and not isinstance(reference.get("iteration"), bool)
                        and reference["iteration"] >= 1
                        and isinstance(reference.get("commit"), str)
                        and bool(reference["commit"])
                        and isinstance(reference.get("view_created_at"), str)
                        and bool(reference["view_created_at"])
                        and isinstance(
                            reference.get("supplemental_evaluation_present"), bool
                        )
                        and _timestamp_not_after(
                            reference.get("view_created_at"), read.get("read_at")
                        )
                    )
                    valid = valid and reference_valid
                    if reference_valid:
                        references.append(reference)
            supplemental_count = sum(
                bool(item.get("supplemental_evaluation_present"))
                for item in references
            )
            valid = bool(
                valid
                and read["completed_view_count"] == len(references)
                and read["completed_supplemental_evaluation_count"]
                == supplemental_count
                and read["completed_view_count"] <= read["evidence_count"]
            )
            schema_valid = schema_valid and valid
            normalized_reads.append(
                {
                    "agent_session_id": session_id,
                    "candidate_id": session_candidate_id,
                    "read_at": read.get("read_at"),
                    "evidence_count": read.get("evidence_count"),
                    "completed_view_count": read.get("completed_view_count"),
                    "completed_supplemental_evaluation_count": read.get(
                        "completed_supplemental_evaluation_count"
                    ),
                    "completed_views": references,
                    "valid": valid,
                }
            )

    influence_windows = []
    peer_influence_windows = []
    for read in normalized_reads:
        if not read["valid"] or not any(
            item["supplemental_evaluation_present"]
            for item in read["completed_views"]
        ):
            continue
        subsequent = sorted(
            (
                iteration
                for iteration in verifier_iterations
                if iteration["agent_session_id"] == read["agent_session_id"]
                and isinstance(iteration["created_at"], str)
                and iteration["created_at"] > read["read_at"]
            ),
            key=lambda item: item["created_at"],
        )
        if subsequent:
            window = {
                "agent_session_id": read["agent_session_id"],
                "candidate_id": read["candidate_id"],
                "read_at": read["read_at"],
                "completed_views": read["completed_views"],
                "next_verifier": subsequent[0],
            }
            influence_windows.append(window)
            if any(
                reference["candidate_id"] != read["candidate_id"]
                for reference in read["completed_views"]
            ):
                peer_influence_windows.append(window)

    return {
        "session_count": len(sessions),
        "sessions_with_reads": sessions_with_reads,
        "read_count": len(normalized_reads),
        "valid_read_count": sum(item["valid"] for item in normalized_reads),
        "reads_with_completed_views": sum(
            bool(item["completed_views"]) for item in normalized_reads
        ),
        "reads_with_completed_supplemental_evaluations": sum(
            any(
                reference["supplemental_evaluation_present"]
                for reference in item["completed_views"]
            )
            for item in normalized_reads
        ),
        "reads_before_subsequent_verifier": len(influence_windows),
        "influence_windows": influence_windows,
        "peer_reads_before_subsequent_verifier": len(peer_influence_windows),
        "peer_influence_windows": peer_influence_windows,
        "schema_valid": schema_valid,
    }


def _visible_verifier_contract(
    verifiers: Any,
    *,
    expected_role: str,
    expected_timeout_seconds: int,
) -> dict[str, Any]:
    records = verifiers if isinstance(verifiers, list) else []
    normalized = []
    for verifier in records:
        if not isinstance(verifier, dict):
            continue
        command = verifier.get("command")
        arguments = [str(item) for item in command] if isinstance(command, list) else []
        wrapper_present = any(
            argument.endswith(VISIBLE_VERIFIER_SUFFIX) for argument in arguments
        )
        direct_wrapper = bool(
            len(arguments) >= 2
            and Path(arguments[0]).name in {"python", "python3"}
            and arguments[1].endswith(VISIBLE_VERIFIER_SUFFIX)
        )
        ranking_signal = "--ranking-signal" in arguments
        timeout_value = None
        if "--timeout-seconds" in arguments:
            index = arguments.index("--timeout-seconds")
            if index + 1 < len(arguments):
                try:
                    timeout_value = int(arguments[index + 1])
                except ValueError:
                    timeout_value = None
        normalized.append(
            {
                "name": verifier.get("name"),
                "role": verifier.get("role"),
                "wrapper_present": wrapper_present,
                "direct_wrapper": direct_wrapper,
                "ranking_signal": ranking_signal,
                "wrapper_timeout_seconds": timeout_value,
                "command": arguments,
            }
        )
    passed = any(
        item["role"] == expected_role
        and item["wrapper_present"]
        and item["direct_wrapper"]
        and (
            item["ranking_signal"]
            if expected_role == "ranking_signal"
            else not item["ranking_signal"]
        )
        and item["wrapper_timeout_seconds"] == expected_timeout_seconds
        for item in normalized
    )
    return {"passed": passed, "verifiers": normalized}


def _visible_verifier_integrity(frozen: dict[str, Any]) -> dict[str, Any]:
    verifier_hashes = (
        frozen.get("verifier_hashes")
        if isinstance(frozen.get("verifier_hashes"), dict)
        else {}
    )
    matching = {
        str(path): str(digest)
        for path, digest in verifier_hashes.items()
        if str(path).endswith(VISIBLE_VERIFIER_SUFFIX)
    }
    expected = expected_visible_verifier_sha256()
    return {
        "expected_sha256": expected,
        "frozen_hashes": matching,
        "passed": len(matching) == 1 and next(iter(matching.values())) == expected,
    }


def _promotion_visible_test(
    candidate_records: list[dict[str, Any]], selected_candidate_id: Any
) -> dict[str, Any]:
    candidate = next(
        (
            item
            for item in candidate_records
            if item.get("candidate_id") == selected_candidate_id
        ),
        {},
    )
    report = (
        candidate.get("promotion_report")
        if isinstance(candidate.get("promotion_report"), dict)
        else {}
    )
    visible_scores: list[float] = []
    for result in report.get("verifier_results") or []:
        if not isinstance(result, dict):
            continue
        metrics = (
            result.get("metrics")
            if isinstance(result.get("metrics"), dict)
            else {}
        )
        score = metrics.get("visible_test_score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            visible_scores.append(float(score))
    passed = report.get("promotion_passed") is True and visible_scores == [1.0]
    return {
        "promotion_passed": report.get("promotion_passed"),
        "aggregate_score": report.get("aggregate_score"),
        "visible_test_scores": visible_scores,
        "passed": passed,
    }


def collect_goal_plus_state(
    root: Path,
    *,
    expected_k: int,
    expected_worker_runtime_seconds: int,
    expected_closeout_reserve_seconds: int,
    expected_visible_verifier_timeout_seconds: int,
    expected_worker_min_runtime_seconds: int | None = None,
    expected_worker_min_verifier_runs: int | None = None,
    expected_supplemental_evaluation_enabled: bool = False,
    expected_evidence_annotator_enabled: bool = False,
    expected_worker_host: str = "pi-rpc",
) -> dict[str, Any]:
    goal_records = []
    for path in sorted((root / "goal-plus").glob("gp_*/goal.json")):
        payload = _read_object(path)
        goal_records.append(
            {
                "goal_plus_id": payload.get("goal_plus_id"),
                "status": payload.get("status"),
                "phase": payload.get("phase"),
                "linked_run_id": _linked_run_id(payload),
            }
        )

    complete_goals = [item for item in goal_records if item.get("status") == "complete"]
    linked_run_ids = {
        str(item["linked_run_id"])
        for item in complete_goals
        if isinstance(item.get("linked_run_id"), str) and item.get("linked_run_id")
    }
    runs: list[dict[str, Any]] = []
    all_bound_sessions: list[dict[str, Any]] = []
    candidate_records_by_run: dict[str, list[dict[str, Any]]] = {}
    for path in sorted((root / "runs").glob("run_*/run.json")):
        payload = _read_object(path)
        run_id = str(payload.get("run_id") or path.parent.name)
        if run_id not in linked_run_ids:
            continue
        frozen_spec_id = payload.get("frozen_spec_id")
        frozen = (
            _read_object(root / "specs" / str(frozen_spec_id) / "frozen_spec.json")
            if isinstance(frozen_spec_id, str)
            else {}
        )
        spec = frozen.get("spec") if isinstance(frozen.get("spec"), dict) else {}
        strategy = spec.get("strategy") if isinstance(spec.get("strategy"), dict) else {}
        budget = spec.get("budget") if isinstance(spec.get("budget"), dict) else {}
        worker_budget = (
            strategy.get("worker_budget")
            if isinstance(strategy.get("worker_budget"), dict)
            else {}
        )
        strategy_config = (
            strategy.get("config") if isinstance(strategy.get("config"), dict) else {}
        )
        process_verifiers = _visible_verifier_contract(
            spec.get("process_verifiers"),
            expected_role="ranking_signal",
            expected_timeout_seconds=expected_visible_verifier_timeout_seconds,
        )
        promotion_verifiers = _visible_verifier_contract(
            spec.get("promotion_verifiers"),
            expected_role="promotion_gate",
            expected_timeout_seconds=expected_visible_verifier_timeout_seconds,
        )
        candidates = sorted(path.parent.glob("candidates/*/candidate.json"))
        candidate_records = [_read_object(candidate) for candidate in candidates]
        candidate_records_by_run[run_id] = candidate_records
        expected_annotation_iterations = sum(
            len(candidate.get("iterations") or [])
            for candidate in candidate_records
            if isinstance(candidate.get("iterations"), list)
        )
        legacy_acceptance_contract = (
            spec.get("acceptance_view")
            if isinstance(spec.get("acceptance_view"), dict)
            else None
        )
        evidence_annotator_spec = (
            strategy.get("evidence_annotator")
            if isinstance(strategy.get("evidence_annotator"), dict)
            else None
        )
        annotations = _evidence_annotations(
            path.parent, expected_annotation_iterations
        )
        sessions = [
            _read_object(session_path)
            for session_path in sorted(path.parent.glob("agent_sessions/agent_*.json"))
        ]
        bound_sessions: list[dict[str, Any]] = []
        autoresearch_leases: list[dict[str, Any]] = []
        bound_counts: dict[str, int] = {}
        verifier_candidate_ids: set[str] = set()
        for session in sessions:
            candidate_id = session.get("candidate_id")
            handle = session.get("host_handle") or {}
            bound_id = (
                handle.get("external_id") or handle.get("task_name")
                if isinstance(handle, dict)
                else None
            )
            if (
                session.get("host") == expected_worker_host
                and isinstance(candidate_id, str)
                and candidate_id
                and isinstance(bound_id, str)
                and bound_id
            ):
                bound_sessions.append(session)
                bound_counts[candidate_id] = bound_counts.get(candidate_id, 0) + 1
                counters = session.get("counters") or {}
                if isinstance(counters, dict) and int(counters.get("verifier_runs") or 0) > 0:
                    verifier_candidate_ids.add(candidate_id)
                lease = _read_object(
                    root
                    / "host-logs"
                    / "codex-autoresearch-leases"
                    / f"{session.get('agent_session_id')}.json"
                )
                if lease:
                    autoresearch_leases.append(
                        _observed_autoresearch_lease(
                            lease, session, run_state=payload.get("state")
                        )
                    )
        global_evidence_reads = _global_evidence_read_receipts(
            bound_sessions,
            candidate_records,
        )
        all_bound_sessions.extend(bound_sessions)

        selected_candidate_id = payload.get("selected_candidate_id")
        verifier_integrity = _visible_verifier_integrity(frozen)
        promotion_visible_test = _promotion_visible_test(
            candidate_records, selected_candidate_id
        )
        promotion = (
            path.parent / "promotion" / f"{selected_candidate_id}.patch"
            if isinstance(selected_candidate_id, str) and selected_candidate_id
            else None
        )
        runs.append(
            {
                "run_id": run_id,
                "state": payload.get("state", payload.get("status")),
                "frozen_spec_id": frozen_spec_id,
                "frozen_spec_present": bool(frozen),
                "max_parallel": budget.get("max_parallel"),
                "worker_host": strategy.get("worker_host"),
                "orchestration_mode": strategy.get("orchestration_mode"),
                "worker_budget": worker_budget,
                "strategy_config": strategy_config,
                "legacy_acceptance_view_contract": legacy_acceptance_contract,
                "evidence_annotator_spec": evidence_annotator_spec,
                "evidence_annotations": annotations,
                "global_evidence_read_receipts": global_evidence_reads,
                "process_visible_verifiers": process_verifiers,
                "promotion_visible_verifiers": promotion_verifiers,
                "visible_verifier_integrity": verifier_integrity,
                "promotion_visible_test": promotion_visible_test,
                "candidate_count": len(candidates),
                "agent_session_count": len(sessions),
                "bound_session_count": len(bound_sessions),
                "bound_candidate_ids": sorted(bound_counts),
                "bound_session_counts_by_candidate": dict(sorted(bound_counts.items())),
                "bound_session_verifier_runs": [
                    int((session.get("counters") or {}).get("verifier_runs") or 0)
                    for session in bound_sessions
                ],
                "autoresearch_leases": autoresearch_leases,
                "verifier_candidate_ids": sorted(verifier_candidate_ids),
                "selected_candidate_id": selected_candidate_id,
                "promotion_artifact": (
                    str(promotion.relative_to(root))
                    if promotion is not None and promotion.is_file()
                    else None
                ),
            }
        )

    active_pool_jobs = []
    for path in sorted((root / "host-pools" / "pi").glob("pool_*/jobs/job_*/job.json")):
        job = _read_object(path)
        if job.get("status") in ACTIVE_POOL_STATES:
            active_pool_jobs.append(
                {
                    "pool_id": job.get("pool_id"),
                    "job_id": job.get("job_id"),
                    "candidate_id": job.get("candidate_id"),
                    "status": job.get("status"),
                }
            )

    selected_run = runs[0] if len(runs) == 1 else None
    counts = (
        selected_run.get("bound_session_counts_by_candidate", {})
        if selected_run is not None
        else {}
    )
    exact_one_session_per_candidate = bool(
        len(counts) == expected_k and all(value == 1 for value in counts.values())
    )
    legacy_acceptance_contract = (
        selected_run.get("legacy_acceptance_view_contract")
        if selected_run
        else None
    )
    annotations = (
        selected_run.get("evidence_annotations", {}) if selected_run else {}
    )
    annotation_entries = (
        annotations.get("entries", []) if isinstance(annotations, dict) else []
    )
    selected_candidate_records = (
        candidate_records_by_run.get(str(selected_run.get("run_id")), [])
        if selected_run
        else []
    )
    candidate_ids = {
        candidate.get("candidate_id")
        for candidate in selected_candidate_records
        if isinstance(candidate.get("candidate_id"), str)
        and candidate.get("candidate_id")
    }
    settled_refs = {
        (
            candidate.get("candidate_id"),
            iteration.get("iteration"),
            iteration.get("git_head"),
        )
        for candidate in selected_candidate_records
        for iteration in candidate.get("iterations") or []
        if isinstance(iteration, dict)
        and isinstance(iteration.get("git_head"), str)
        and iteration.get("git_head")
    }

    allowed_relations = {
        "similar",
        "different",
        "tradeoff",
        "complementary",
        "unknown",
    }

    def reference_tuple(value: Any) -> tuple[str, int, str] | None:
        if not isinstance(value, dict):
            return None
        candidate_id = value.get("candidate_id")
        iteration = value.get("iteration")
        commit = value.get("commit")
        if (
            not isinstance(candidate_id, str)
            or not candidate_id
            or not isinstance(iteration, int)
            or isinstance(iteration, bool)
            or iteration < 1
            or not isinstance(commit, str)
            or not commit
        ):
            return None
        return candidate_id, iteration, commit

    def supplemental_entry_valid(entry: dict[str, Any]) -> bool:
        def evidence_list_valid(value: Any) -> bool:
            return (
                isinstance(value, list)
                and len(value) <= 8
                and all(isinstance(item, str) and item.strip() for item in value)
            )

        view = entry.get("view") if isinstance(entry.get("view"), dict) else {}
        basis = entry.get("comparison_basis")
        if not isinstance(basis, list) or len(basis) > 8:
            return False
        basis_refs = [reference_tuple(item) for item in basis]
        if any(item is None for item in basis_refs):
            return False
        if any(item not in settled_refs for item in basis_refs):
            return False
        candidate_ids = [item[0] for item in basis_refs if item is not None]
        if (
            len(candidate_ids) != len(set(candidate_ids))
            or entry.get("candidate_id") in candidate_ids
            or view.get("acceptance_view") is not None
            or view.get("comparison_basis") != basis
        ):
            return False
        evaluation = view.get("supplemental_evaluation")
        if not expected_supplemental_evaluation_enabled:
            return (
                entry.get("supplemental_evaluation_enabled") is False
                and basis == []
                and evaluation is None
            )
        if (
            entry.get("supplemental_evaluation_enabled") is not True
            or not isinstance(evaluation, dict)
            or set(evaluation)
            != {"summary", "dimensions", "comparisons", "limitations"}
            or not isinstance(evaluation.get("summary"), str)
            or not evaluation["summary"].strip()
        ):
            return False
        dimensions = evaluation.get("dimensions")
        if (
            not isinstance(dimensions, list)
            or not 1 <= len(dimensions) <= 8
            or any(
                not isinstance(item, dict)
                or set(item) != {"name", "finding", "confidence", "evidence"}
                or not isinstance(item.get("name"), str)
                or not item["name"].strip()
                or not isinstance(item.get("finding"), str)
                or not item["finding"].strip()
                or item.get("confidence") not in {"high", "medium", "low"}
                or not evidence_list_valid(item.get("evidence"))
                for item in dimensions
            )
        ):
            return False
        comparisons = evaluation.get("comparisons")
        if (
            not isinstance(comparisons, list)
            or [reference_tuple(item) for item in comparisons] != basis_refs
            or any(
                not isinstance(item, dict)
                or set(item)
                != {
                    "candidate_id",
                    "iteration",
                    "commit",
                    "relation",
                    "rationale",
                    "evidence",
                }
                or item.get("relation") not in allowed_relations
                or not isinstance(item.get("rationale"), str)
                or not item["rationale"].strip()
                or not evidence_list_valid(item.get("evidence"))
                for item in comparisons
            )
        ):
            return False
        limitations = evaluation.get("limitations")
        return isinstance(limitations, list) and all(
            isinstance(item, str) and item.strip() for item in limitations
        )

    annotations_passed = bool(
        not expected_evidence_annotator_enabled
        or (
            annotations.get("all_completed") is True
            and annotation_entries
            and all(
                entry.get("annotator_host") == "codex"
                for entry in annotation_entries
            )
            and all(
                entry.get("task_context_source") == "goal_plus_raw_goal"
                and isinstance(entry.get("task_context_ref"), str)
                and entry["task_context_ref"].startswith("goal_plus:")
                and isinstance(entry.get("task_context_sha256"), str)
                and len(entry["task_context_sha256"]) == 64
                and all(
                    character in "0123456789abcdef"
                    for character in entry["task_context_sha256"]
                )
                for entry in annotation_entries
            )
            and all(
                supplemental_entry_valid(entry)
                for entry in annotation_entries
            )
        )
    )
    dynamic_peer_required = bool(
        expected_k > 1 and expected_supplemental_evaluation_enabled
    )
    peer_comparison_entries = []
    for entry in annotation_entries:
        if entry.get("state") != "completed" or not supplemental_entry_valid(entry):
            continue
        basis_refs = [
            reference_tuple(item) for item in entry.get("comparison_basis") or []
        ]
        expected_peer_ids = candidate_ids - {entry.get("candidate_id")}
        actual_peer_ids = {
            item[0] for item in basis_refs if item is not None
        }
        if expected_peer_ids and actual_peer_ids == expected_peer_ids:
            peer_comparison_entries.append(
                {
                    "candidate_id": entry.get("candidate_id"),
                    "iteration": entry.get("iteration"),
                    "comparison_basis": entry.get("comparison_basis"),
                }
            )
    read_receipts = (
        selected_run.get("global_evidence_read_receipts", {})
        if selected_run
        else {}
    )
    completed_supplemental_view_refs = {
        settled_ref
        for entry in annotation_entries
        if entry.get("state") == "completed"
        and supplemental_entry_valid(entry)
        and entry.get("supplemental_evaluation_enabled") is True
        for settled_ref in settled_refs
        if settled_ref[0] == entry.get("candidate_id")
        and settled_ref[1] == entry.get("iteration")
    }
    valid_peer_influence_windows = [
        window
        for window in read_receipts.get("peer_influence_windows", [])
        if any(
            reference_tuple(reference) in completed_supplemental_view_refs
            and reference.get("candidate_id") != window.get("candidate_id")
            for reference in window.get("completed_views", [])
        )
    ]
    worker_overlap = _worker_overlap(
        selected_run.get("autoresearch_leases", []) if selected_run else [],
        expected_k,
    )
    checks = {
        "durable_state": _check(True, root.is_dir(), root.is_dir()),
        "terminal_goal": _check(
            "exactly one complete Goal Plus record",
            [item.get("status") for item in goal_records],
            len(goal_records) == 1 and len(complete_goals) == 1,
        ),
        "linked_search_run": _check(1, len(runs), len(runs) == 1),
        "frozen_spec": _check(
            True,
            selected_run.get("frozen_spec_present") if selected_run else False,
            bool(selected_run and selected_run.get("frozen_spec_present")),
        ),
        "max_parallel": _check(
            expected_k,
            selected_run.get("max_parallel") if selected_run else None,
            bool(selected_run and selected_run.get("max_parallel") == expected_k),
        ),
        "worker_topology": _check(
            f"{expected_worker_host}/parallel_loops",
            (
                f"{selected_run.get('worker_host')}/"
                f"{selected_run.get('orchestration_mode')}"
                if selected_run
                else None
            ),
            bool(
                selected_run
                and selected_run.get("worker_host") == expected_worker_host
                and selected_run.get("orchestration_mode") == "parallel_loops"
            ),
        ),
        "worker_runtime": _check(
            expected_worker_runtime_seconds,
            (
                selected_run.get("worker_budget", {}).get("max_runtime_seconds")
                if selected_run
                else None
            ),
            bool(
                selected_run
                and selected_run.get("worker_budget", {}).get(
                    "max_runtime_seconds"
                )
                == expected_worker_runtime_seconds
            ),
        ),
        "worker_minimum_budget": _check(
            {
                "min_runtime_seconds": expected_worker_min_runtime_seconds,
                "min_verifier_runs": expected_worker_min_verifier_runs,
            },
            (
                {
                    "min_runtime_seconds": selected_run.get(
                        "worker_budget", {}
                    ).get("min_runtime_seconds"),
                    "min_verifier_runs": selected_run.get(
                        "worker_budget", {}
                    ).get("min_verifier_runs"),
                }
                if selected_run
                else None
            ),
            bool(
                selected_run
                and selected_run.get("worker_budget", {}).get(
                    "min_runtime_seconds"
                )
                == expected_worker_min_runtime_seconds
                and selected_run.get("worker_budget", {}).get(
                    "min_verifier_runs"
                )
                == expected_worker_min_verifier_runs
            ),
        ),
        "worker_minimum_observed": _check(
            (
                {
                    "min_runtime_seconds": expected_worker_min_runtime_seconds,
                    "min_verifier_runs": expected_worker_min_verifier_runs,
                    "evidence": (
                        "released lease or promoted-run terminal session timestamps"
                    ),
                }
                if expected_worker_min_runtime_seconds is not None
                else "not configured"
            ),
            (
                {
                    "verifier_runs": selected_run.get(
                        "bound_session_verifier_runs", []
                    ),
                    "leases": selected_run.get("autoresearch_leases", []),
                }
                if selected_run and expected_worker_min_runtime_seconds is not None
                else "not configured"
            ),
            bool(
                expected_worker_min_runtime_seconds is None
                or (
                    selected_run
                    and len(selected_run.get("autoresearch_leases", []))
                    == expected_k
                    and all(
                        int(value) >= int(expected_worker_min_verifier_runs or 0)
                        for value in selected_run.get(
                            "bound_session_verifier_runs", []
                        )
                    )
                    and all(
                        (lease.get("minimum_observation") or {}).get("passed")
                        is True
                        for lease in selected_run.get("autoresearch_leases", [])
                    )
                )
            ),
        ),
        "closeout_reserve": _check(
            expected_closeout_reserve_seconds,
            (
                selected_run.get("strategy_config", {}).get(
                    "closeout_reserve_seconds"
                )
                if selected_run
                else None
            ),
            bool(
                selected_run
                and selected_run.get("strategy_config", {}).get(
                    "closeout_reserve_seconds"
                )
                == expected_closeout_reserve_seconds
            ),
        ),
        "visible_verifiers": _check(
            "ranking process and promotion wrappers",
            (
                {
                    "process": selected_run.get("process_visible_verifiers"),
                    "promotion": selected_run.get("promotion_visible_verifiers"),
                }
                if selected_run
                else None
            ),
            bool(
                selected_run
                and selected_run.get("process_visible_verifiers", {}).get("passed")
                and selected_run.get("promotion_visible_verifiers", {}).get("passed")
            ),
        ),
        "visible_verifier_integrity": _check(
            expected_visible_verifier_sha256(),
            (
                selected_run.get("visible_verifier_integrity")
                if selected_run
                else None
            ),
            bool(
                selected_run
                and selected_run.get("visible_verifier_integrity", {}).get("passed")
            ),
        ),
        "promotion_visible_test": _check(
            "promotion gate visible_test_score=1.0",
            selected_run.get("promotion_visible_test") if selected_run else None,
            bool(
                selected_run
                and selected_run.get("promotion_visible_test", {}).get("passed")
            ),
        ),
        "frozen_soft_rubric_absent": _check(
            None,
            legacy_acceptance_contract,
            legacy_acceptance_contract is None,
        ),
        "supplemental_evaluation_contract": _check(
            (
                "open post-settlement evaluation"
                if expected_supplemental_evaluation_enabled
                else "disabled"
            ),
            annotations,
            annotations_passed,
        ),
        "global_evidence_view": _check(
            (
                "completed descriptions plus open supplemental evaluation"
                if expected_supplemental_evaluation_enabled
                else (
                    "completed descriptions"
                    if expected_evidence_annotator_enabled
                    else "disabled"
                )
            ),
            annotations,
            annotations_passed,
        ),
        "global_evidence_read_receipts": _check(
            (
                "at least one valid persisted read per bound worker session"
                if expected_evidence_annotator_enabled
                else "not required"
            ),
            (
                selected_run.get("global_evidence_read_receipts")
                if selected_run
                else None
            ),
            bool(
                not expected_evidence_annotator_enabled
                or (
                    selected_run
                    and selected_run.get("bound_session_count") == expected_k
                    and selected_run.get(
                        "global_evidence_read_receipts", {}
                    ).get("schema_valid")
                    and selected_run.get(
                        "global_evidence_read_receipts", {}
                    ).get("sessions_with_reads")
                    == selected_run.get("bound_session_count")
                    and selected_run.get(
                        "global_evidence_read_receipts", {}
                    ).get("read_count", 0)
                    >= selected_run.get("bound_session_count", 0)
                )
            ),
        ),
        "dynamic_peer_comparison": _check(
            (
                "at least one complete comparison against every peer incumbent"
                if dynamic_peer_required
                else "not required"
            ),
            peer_comparison_entries,
            bool(not dynamic_peer_required or peer_comparison_entries),
        ),
        "peer_view_influence": _check(
            (
                "a worker reads a completed peer View before its next verifier"
                if dynamic_peer_required
                else "not required"
            ),
            {
                **read_receipts,
                "valid_peer_influence_windows": valid_peer_influence_windows,
            },
            bool(not dynamic_peer_required or valid_peer_influence_windows),
        ),
        "live_worker_overlap": _check(
            (
                "all candidate-bound worker lease intervals overlap"
                if expected_k > 1
                else "not required"
            ),
            worker_overlap,
            bool(expected_k == 1 or worker_overlap["passed"]),
        ),
        "view_agent_contract": _check(
            (
                "independent codex host"
                if expected_evidence_annotator_enabled
                else "disabled"
            ),
            selected_run.get("evidence_annotator_spec") if selected_run else None,
            bool(
                not expected_evidence_annotator_enabled
                or (
                    selected_run
                    and isinstance(
                        selected_run.get("evidence_annotator_spec"), dict
                    )
                    and selected_run["evidence_annotator_spec"].get("host")
                    == "codex"
                )
            ),
        ),
        "candidates": _check(
            expected_k,
            selected_run.get("candidate_count") if selected_run else 0,
            bool(selected_run and selected_run.get("candidate_count") == expected_k),
        ),
        "bound_pi_worker_sessions": _check(
            expected_k,
            selected_run.get("bound_session_count") if selected_run else 0,
            bool(
                selected_run
                and selected_run.get("bound_session_count") == expected_k
                and exact_one_session_per_candidate
            ),
        ),
        "worker_verifier_candidates": _check(
            expected_k,
            len(selected_run.get("verifier_candidate_ids", [])) if selected_run else 0,
            bool(
                selected_run
                and len(selected_run.get("verifier_candidate_ids", [])) == expected_k
            ),
        ),
        "promotion": _check(
            "promoted artifact",
            (
                {
                    "state": selected_run.get("state"),
                    "candidate_id": selected_run.get("selected_candidate_id"),
                    "artifact": selected_run.get("promotion_artifact"),
                }
                if selected_run
                else None
            ),
            bool(
                selected_run
                and selected_run.get("state") == "promoted"
                and selected_run.get("selected_candidate_id")
                and selected_run.get("promotion_artifact")
            ),
        ),
        "active_pi_pool_jobs": _check(0, len(active_pool_jobs), not active_pool_jobs),
    }
    failed = [name for name, check in checks.items() if not check["passed"]]
    return {
        "root": str(root),
        "exists": root.is_dir(),
        "goals": goal_records,
        "runs": runs,
        "active_pi_pool_jobs": active_pool_jobs,
        "actual_subagent_count": (
            int(selected_run.get("bound_session_count") or 0) if selected_run else 0
        ),
        "worker_usage": _usage_from_sessions(all_bound_sessions),
        "evidence_annotator_usage": (
            selected_run.get("evidence_annotations", {}).get("usage", {})
            if selected_run
            else {"coverage": "unavailable"}
        ),
        "completion": {
            "required": True,
            "passed": not failed,
            "expected_k": expected_k,
            "checks": checks,
            "reason": (
                None
                if not failed
                else "Goal Plus completion evidence failed: " + ", ".join(failed)
            ),
        },
    }
