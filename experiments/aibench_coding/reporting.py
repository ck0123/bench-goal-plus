"""Normalize terminal aibench coding evidence without re-running its grader."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from bench_artifacts import utc_now
from experiments.openevolve_compare.reporting import collect_usage

from .config import GOAL_PLUS_METHODS, PI_METHODS, write_json


TERMINAL = {"completed", "partial", "failed", "interrupted"}


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _goal_plus_count(method: str, execution: dict[str, Any]) -> tuple[int, str]:
    if method == "goal-plus-codex":
        codex = execution.get("codex") if isinstance(execution.get("codex"), dict) else {}
        direct = codex.get("spawned_agent_thread_count")
        handles = (codex.get("goal_plus") or {}).get("bound_worker_handle_count")
        values = [value for value in (direct, handles) if isinstance(value, int)]
        return (max(values, default=0), "distinct Codex worker threads/host handles")
    runs = (execution.get("goal_plus") or {}).get("runs") or []
    values = [
        run.get("bound_candidate_count")
        for run in runs
        if isinstance(run, dict) and isinstance(run.get("bound_candidate_count"), int)
    ]
    return (max(values, default=0), "distinct candidates with bound Pi worker sessions")


def _topology(method: str, execution: dict[str, Any], expected_k: int) -> dict[str, Any]:
    if method in GOAL_PLUS_METHODS:
        actual, source = _goal_plus_count(method, execution)
        outer = 1 if execution else 0
        return {
            "expected_k": expected_k,
            "expected_outer_trajectories": 1,
            "actual_outer_trajectories": outer,
            "actual_subagent_count": actual,
            "subagent_count_source": source,
            "matches_k": outer == 1 and actual == expected_k,
        }
    agent_key = "pi" if method in PI_METHODS else "codex"
    agent = execution.get(agent_key) if isinstance(execution.get(agent_key), dict) else {}
    lanes = agent.get("lanes") if isinstance(agent.get("lanes"), list) else []
    actual = len(lanes)
    return {
        "expected_k": expected_k,
        "expected_outer_trajectories": expected_k,
        "actual_outer_trajectories": actual,
        "actual_subagent_count": 0,
        "subagent_count_source": "not applicable to Plain methods",
        "matches_k": actual == expected_k,
    }


def _record(campaign: dict[str, Any], cell: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(cell["run_dir"])
    manifest = _read(run_dir / "experiment.json")
    final = _read(run_dir / "final-eval.json")
    execution = manifest.get("execution") if isinstance(manifest.get("execution"), dict) else {}
    final_metric = final.get("primary_metric") if isinstance(final.get("primary_metric"), dict) else {}
    raw_value = final_metric.get("value")
    score_valid = final.get("valid") is True and isinstance(raw_value, bool)
    expected_k = int(campaign["budget"]["live_search_concurrency"])
    topology = _topology(cell["method"], execution, expected_k)
    sandbox = cell.get("sandbox") if isinstance(cell.get("sandbox"), dict) else {}
    outer_isolation_valid = (
        sandbox.get("kind") == "bubblewrap"
        and sandbox.get("hidden_checkout_masked") is True
    )
    worker_sandbox = (
        manifest.get("pi_worker_sandbox")
        if isinstance(manifest.get("pi_worker_sandbox"), dict)
        else {}
    )
    worker_isolation_valid = cell["method"] != "goal-plus-pi" or (
        worker_sandbox.get("engine") == "bubblewrap"
        and worker_sandbox.get("launch_interception")
        == "bench-owned-pi-path-shim"
    )
    isolation_valid = outer_isolation_valid and worker_isolation_valid
    succeeded = cell.get("state") == "completed" and manifest.get("status") == "finished"
    eligible = succeeded and score_valid and topology["matches_k"] and isolation_valid
    reasons = [
        reason
        for reason in (
            cell.get("error"),
            execution.get("result_incomplete_reason"),
            None if score_valid else "official hidden grade is missing or invalid",
            None if topology["matches_k"] else "observed agent topology does not match K",
            None
            if outer_isolation_valid
            else "Bubblewrap hidden-checkout isolation is unproven",
            None
            if worker_isolation_valid
            else "Goal Plus Pi worker Bubblewrap isolation is unproven",
        )
        if reason
    ]
    grade = final.get("grade") if isinstance(final.get("grade"), dict) else {}
    return {
        "benchmark_id": "aibench-coding",
        "task_id": cell["task_id"],
        "cell_id": cell["cell_id"],
        "method": cell["method"],
        "model": campaign["model"],
        "reasoning_effort": campaign["reasoning_effort"],
        "seed": cell["seed"],
        "status": "succeeded" if succeeded else cell.get("state"),
        "incomplete_reason": "; ".join(dict.fromkeys(str(item) for item in reasons)) or None,
        "budget": campaign["budget"],
        "protocol": {
            "case_set": campaign["source"]["case_set"],
            "case_set_fingerprint": campaign["source"]["case_set_fingerprint"],
            "source_commit": campaign["source"]["commit"],
            "goal_plus_commit": campaign["source"].get("goal_plus_commit"),
            "metric_name": "task_success",
            "direction": "maximize",
            "official_evaluator": True,
            "official_evaluator_calls": 1 if final else 0,
            "selection_uses_hidden_grade": False,
            "sandbox": {
                "outer": sandbox or None,
                "goal_plus_pi_worker": worker_sandbox or None,
            },
            "topology": topology,
            "matched_comparison_eligible": eligible,
        },
        "score": {
            "valid": score_valid,
            "final": int(raw_value) if score_valid else None,
            "raw_metrics": {
                "task_success": raw_value if isinstance(raw_value, bool) else None,
                "test_pass_ratio": grade.get("test_pass_ratio"),
                "passed": grade.get("passed"),
                "infra_error": grade.get("infra_error"),
                "detail": grade.get("detail"),
            },
        },
        "execution": {
            "duration_seconds": execution.get("duration_seconds"),
            "deadline_reached": execution.get("deadline_reached"),
            "hard_killed": execution.get("hard_killed"),
            "selected_lane": execution.get("selected_lane"),
            "evaluator_calls": execution.get("evaluator_calls") or {
                "coverage": "missing: controller ledger unavailable"
            },
            "usage": collect_usage(execution),
        },
        "evidence": {
            "manifest": str(run_dir / "experiment.json"),
            "official_report": str(run_dir / "final-eval.json"),
            "candidate": str(run_dir / "submission"),
            "events": str(run_dir / "events.jsonl"),
            "goal_plus_state": str(run_dir / "workspace" / ".gp"),
        },
    }


def _method_aggregates(records: list[dict[str, Any]], expected_k: int) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["method"]].append(record)
    aggregates: dict[str, Any] = {}
    for method, items in sorted(grouped.items()):
        valid = [item for item in items if item["score"]["valid"]]
        successes = sum(item["score"]["final"] for item in valid)
        rate = successes / len(valid) if valid else None
        aggregates[method] = {
            "trial_count": len(items),
            "evaluated_count": len(valid),
            "success_count": successes,
            "task_success_rate": rate,
            "pass_at_1": rate if expected_k == 1 else None,
            "pass_at_k": rate if expected_k == 1 else None,
            "pass_power_k": rate if expected_k == 1 else None,
            "k_metric_coverage": (
                "K=1 identity"
                if expected_k == 1
                else "not computed: hidden grading is applied only to the selected result"
            ),
        }
    return aggregates


def _markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# aibench coding report: {summary['campaign_id']}",
        "",
        f"State: `{summary['state']}`. Primary metric: `task_success_rate` (maximize).",
        "",
        "| Task | Method | Seed | State | Success | Agents | K match | Comparable |",
        "| --- | --- | ---: | --- | ---: | ---: | --- | --- |",
    ]
    for record in summary["records"]:
        topology = record["protocol"]["topology"]
        agents = (
            topology["actual_subagent_count"]
            if record["method"] in GOAL_PLUS_METHODS
            else topology["actual_outer_trajectories"]
        )
        lines.append(
            "| "
            + " | ".join(
                str(value)
                for value in (
                    record["task_id"],
                    record["method"],
                    record["seed"],
                    record["status"],
                    "" if record["score"]["final"] is None else record["score"]["final"],
                    agents,
                    topology["matches_k"],
                    record["protocol"]["matched_comparison_eligible"],
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def finalize_campaign(destination: Path) -> dict[str, Any]:
    campaign_path = destination / "campaign.json"
    campaign = _read(campaign_path)
    if campaign.get("state") not in TERMINAL:
        raise RuntimeError(f"cannot finalize non-terminal campaign: {campaign.get('state')}")
    records = [_record(campaign, cell) for cell in campaign["cells"]]
    accepted = all(
        record["protocol"]["matched_comparison_eligible"] for record in records
    )
    state = campaign["state"]
    if state == "completed" and not accepted:
        state = "partial"
        campaign["state"] = state
        for cell, record in zip(campaign["cells"], records):
            if not record["protocol"]["matched_comparison_eligible"]:
                cell["state"] = "partial"
                cell["error"] = record["incomplete_reason"]
        write_json(campaign_path, campaign)
    expected_k = int(campaign["budget"]["live_search_concurrency"])
    summary = {
        "schema_version": 1,
        "report_kind": "campaign",
        "campaign_id": campaign["campaign_id"],
        "benchmark": "aibench-coding",
        "runner": "aibench-coding-native",
        "state": state,
        "updated_at": utc_now(),
        "record_count": len(records),
        "budget": campaign["budget"],
        "source": campaign["source"],
        "aggregates": {
            "evaluated_count": sum(record["score"]["valid"] for record in records),
            "success_count": sum(
                record["score"]["final"]
                for record in records
                if record["score"]["valid"]
            ),
            "matched_comparison_count": sum(
                record["protocol"]["matched_comparison_eligible"] for record in records
            ),
            "official_evaluator_calls": sum(
                record["protocol"]["official_evaluator_calls"] for record in records
            ),
            "by_method": _method_aggregates(records, expected_k),
        },
        "records": records,
        "coverage": {
            "official_score": "upstream aibench hidden grade_case result",
            "selection": "public visible-test gate; hidden grade never selects a candidate",
            "sandbox": "Linux Bubblewrap masks the managed aibench checkout from agents",
            "k_metrics": (
                "complete for K=1; selected-result task success only for K>1"
            ),
        },
    }
    write_json(destination / "campaign-summary.json", summary)
    (destination / "campaign-summary.md").write_text(
        _markdown(summary), encoding="utf-8"
    )
    return summary
