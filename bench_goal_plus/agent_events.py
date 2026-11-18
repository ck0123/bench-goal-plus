"""Compact, reusable parsers for host-agent JSONL event streams."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


def iter_json_events(text: str) -> Iterable[dict[str, Any]]:
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _structured_result(item: dict[str, Any]) -> Any:
    result = item.get("result")
    if not isinstance(result, dict):
        return None
    structured = result.get("structured_content")
    if structured is not None:
        return structured
    for content in result.get("content") or []:
        if not isinstance(content, dict) or content.get("type") != "text":
            continue
        text = content.get("text")
        if not isinstance(text, str):
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    return None


def _distinct_bound_handle_count(
    handles: Iterable[dict[str, Any]],
) -> int:
    identities: set[tuple[str, str, str]] = set()
    for handle in handles:
        identity = handle.get("external_id")
        if not isinstance(identity, str) or not identity:
            continue
        identities.add((str(handle.get("agent_harness") or ""), str(handle.get("runtime_provider") or ""), identity))
    return len(identities)


def parse_codex_event_text(text: str) -> dict[str, Any]:
    thread_id = None
    usage = None
    terminal_event = None
    event_count = sum(1 for line in text.splitlines() if line.strip())
    collaboration_tool_counts: dict[str, dict[str, int]] = {}
    spawned_agent_thread_ids: set[str] = set()
    targetless_wait_count = 0
    goal_tool_counts: dict[str, dict[str, int]] = {}
    run_ids: set[str] = set()
    candidate_ids: set[str] = set()
    agent_session_ids: set[str] = set()
    worker_sessions: dict[str, dict[str, Any]] = {}
    bound_worker_handles: dict[str, dict[str, Any]] = {}
    native_sessions: dict[str, tuple[str, dict[str, Any]]] = {}
    verifier_ledger: list[dict[str, Any]] = []
    selected_candidate_ids: set[str] = set()
    promoted_candidate_ids: set[str] = set()
    goal_statuses: list[dict[str, Any]] = []

    for event in iter_json_events(text):
        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
        if event_type in {"turn.completed", "turn.failed"}:
            terminal_event = event_type
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        tool = item.get("tool")
        if not isinstance(tool, str):
            continue
        status = item.get("status")
        if not isinstance(status, str):
            status = str(event_type or "").removeprefix("item.")

        if item_type == "collab_tool_call":
            counts = collaboration_tool_counts.setdefault(tool, {})
            counts[status] = counts.get(status, 0) + 1
            if tool == "spawn_agent" and status == "completed":
                receiver_ids = item.get("receiver_thread_ids")
                if isinstance(receiver_ids, list):
                    spawned_agent_thread_ids.update(
                        value
                        for value in receiver_ids
                        if isinstance(value, str) and value
                    )
            if tool in {"wait", "wait_agent"} and status == "completed":
                receiver_ids = item.get("receiver_thread_ids")
                if isinstance(receiver_ids, list) and not receiver_ids:
                    targetless_wait_count += 1
            continue

        if item_type != "mcp_tool_call" or item.get("server") != "goal-plus":
            continue
        counts = goal_tool_counts.setdefault(tool, {})
        counts[status] = counts.get(status, 0) + 1
        if status != "completed":
            continue
        arguments = item.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        structured = _structured_result(item)
        if tool.startswith("goal_plus_search_"):
            tool = tool.removeprefix("goal_plus_")

        run_id = arguments.get("run_id")
        if isinstance(run_id, str) and run_id:
            run_ids.add(run_id)
        candidate_id = arguments.get("candidate_id")
        if isinstance(candidate_id, str) and candidate_id:
            candidate_ids.add(candidate_id)

        if tool == "search_start_batch" and isinstance(structured, list):
            for candidate in structured:
                if not isinstance(candidate, dict):
                    continue
                value = candidate.get("candidate_id")
                if isinstance(value, str) and value:
                    candidate_ids.add(value)
                value = candidate.get("run_id")
                if isinstance(value, str) and value:
                    run_ids.add(value)
        elif tool == "goal_plus_session_run" and isinstance(structured, dict):
            session_id = structured.get("session_id")
            call_id = arguments.get("call_id")
            if (
                isinstance(session_id, str) and session_id
                and isinstance(call_id, str) and call_id
                and structured.get("call_id") == call_id
            ):
                agent_session_ids.add(session_id)
                worker_sessions[session_id] = {
                    "agent_session_id": session_id,
                    "run_id": run_id,
                    "candidate_id": candidate_id,
                }
                native_sessions[session_id] = (session_id, {
                    "agent_harness": "codex", "runtime_provider": "direct",
                    "native_session_id": structured.get("native_session_id"),
                    "call_id": call_id,
                })
        elif tool == "search_start_agent_session" and isinstance(structured, dict):
            session_id = structured.get("agent_session_id")
            if isinstance(session_id, str) and session_id:
                agent_session_ids.add(session_id)
                worker_sessions[session_id] = {
                    key: structured.get(key)
                    for key in ("agent_session_id", "run_id", "candidate_id", "agent_harness", "runtime_provider")
                    if structured.get(key) is not None
                }
            for key, target in (
                ("run_id", run_ids),
                ("candidate_id", candidate_ids),
            ):
                value = structured.get(key)
                if isinstance(value, str) and value:
                    target.add(value)
        elif tool == "goal_plus_session_open" and isinstance(structured, dict):
            session_id = arguments.get("agent_session_id")
            control_id = structured.get("session_id")
            if (
                isinstance(session_id, str) and isinstance(control_id, str)
                and structured.get("agent_harness") in {"codex", "pi", "opencode"}
                and structured.get("runtime_provider") == "direct"
            ):
                native_sessions[control_id] = (session_id, structured)
                agent_session_ids.add(session_id)
        elif tool == "goal_plus_session_wait" and isinstance(structured, dict):
            binding = native_sessions.get(arguments.get("session_id"))
            result = structured.get("result")
            if binding and isinstance(result, dict):
                session_id, opened = binding
                external_id = result.get("native_session_id")
                if (
                    isinstance(external_id, str) and external_id
                    and result.get("agent_harness") == opened["agent_harness"]
                    and result.get("runtime_provider") == opened["runtime_provider"]
                    and opened.get("native_session_id") in {None, external_id}
                    and result.get("invocation_id")
                    and structured.get("call_id") == arguments.get("call_id")
                    and opened.get("call_id", arguments.get("call_id")) == arguments.get("call_id")
                ):
                    bound_worker_handles[session_id] = {
                        "agent_session_id": session_id,
                        "agent_harness": result["agent_harness"],
                        "runtime_provider": result["runtime_provider"],
                        "external_id": external_id,
                    }
        elif tool == "search_run_verifier":
            payload = structured if isinstance(structured, dict) else arguments
            result_candidate = payload.get("candidate_id")
            if isinstance(result_candidate, str) and result_candidate:
                candidate_ids.add(result_candidate)
            verifier_ledger.append(
                {
                    key: payload.get(key)
                    for key in (
                        "run_id",
                        "candidate_id",
                        "validity_passed",
                        "process_passed",
                        "promotion_passed",
                        "aggregate_score",
                        "disposition",
                    )
                    if payload.get(key) is not None
                }
            )
        elif tool == "search_promote":
            value = arguments.get("candidate_id")
            if isinstance(value, str) and value:
                promoted_candidate_ids.add(value)
        elif tool == "goal_plus_record_search_result":
            value = arguments.get("selected_candidate_id")
            if isinstance(value, str) and value:
                selected_candidate_ids.add(value)
        elif tool == "goal_plus_set_status":
            goal_statuses.append(
                {
                    key: arguments.get(key)
                    for key in ("goal_plus_id", "status", "reason")
                    if arguments.get(key) is not None
                }
            )

    spawn_counts = collaboration_tool_counts.get("spawn_agent") or {}
    bound_handles = [
        bound_worker_handles[key] for key in sorted(bound_worker_handles)
    ]
    return {
        "thread_id": thread_id,
        "terminal_event": terminal_event,
        "top_level_usage": usage,
        "event_count": event_count,
        "collaboration_tool_counts": collaboration_tool_counts,
        "spawn_agent_completed_count": spawn_counts.get("completed", 0),
        "spawned_agent_thread_ids": sorted(spawned_agent_thread_ids),
        "spawned_agent_thread_count": len(spawned_agent_thread_ids),
        "targetless_wait_count": targetless_wait_count,
        "goal_plus": {
            "tool_counts": goal_tool_counts,
            "run_ids": sorted(run_ids),
            "candidate_ids": sorted(candidate_ids),
            "agent_session_ids": sorted(agent_session_ids),
            "worker_sessions": [
                worker_sessions[key] for key in sorted(worker_sessions)
            ],
            "bound_worker_handles": bound_handles,
            "bound_worker_session_count": len(bound_worker_handles),
            "bound_worker_handle_count": _distinct_bound_handle_count(
                bound_handles
            ),
            "verifier_ledger": verifier_ledger,
            "selected_candidate_ids": sorted(selected_candidate_ids),
            "promoted_candidate_ids": sorted(promoted_candidate_ids),
            "goal_statuses": goal_statuses,
        },
        "coverage": (
            "top-level Codex usage, collaboration calls, and compact Goal Plus "
            "MCP lifecycle evidence"
        ),
    }


def parse_codex_event_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    return parse_codex_event_text(text)
