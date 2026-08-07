"""Finalize SWE-bench evidence without re-running the official evaluator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import SweBenchContractError, read_json, utc_now, write_json
from .goal_plus_evidence import collect_goal_plus_state, record_completion_check
from .runtime import MANIFEST, TERMINAL_STATES


def _container_isolated(agent: dict[str, Any]) -> bool:
    cleanup = (agent.get("container") or {}).get("cleanup") or {}
    return cleanup.get("removed") is True or (
        cleanup.get("retained") is True and cleanup.get("stopped") is True
    )


def _revalidate_goal_plus_cell(
    campaign: Path,
    manifest: dict[str, Any],
    cell: dict[str, Any],
) -> bool:
    if cell.get("method") != "goal-plus-pi" or cell.get("state") not in {
        "completed",
        "partial",
    }:
        return False
    profile = manifest.get("profile_snapshot") or {}
    goal_plus_profile = profile.get("goal_plus") or {}
    task_file = cell.get("task_file")
    if not isinstance(task_file, str):
        return False
    campaign_root = campaign.resolve()
    state_root = (campaign / Path(task_file).parent / "goal-plus-state").resolve()
    if not state_root.is_relative_to(campaign_root) or not state_root.is_dir():
        return False
    try:
        refreshed = collect_goal_plus_state(
            state_root,
            expected_k=int(profile["concurrency"]),
            expected_worker_runtime_seconds=int(
                goal_plus_profile["worker_runtime_seconds"]
            ),
            expected_closeout_reserve_seconds=int(
                goal_plus_profile["closeout_reserve_seconds"]
            ),
            expected_visible_verifier_timeout_seconds=int(
                goal_plus_profile["visible_verifier_timeout_seconds"]
            ),
        )
    except (KeyError, TypeError, ValueError):
        return False

    agent = cell.get("agent") or {}
    previous_goal_plus = agent.get("goal_plus") or {}
    previous_completion = previous_goal_plus.get("completion") or {}
    export = previous_goal_plus.get("export") or {}
    closeout = agent.get("goal_plus_closeout") or {}
    annotator = (agent.get("runtime") or {}).get("evidence_annotator")
    record_completion_check(
        refreshed,
        "state_export",
        expected=True,
        actual=export.get("completed"),
        passed=export.get("completed") is True,
    )
    record_completion_check(
        refreshed,
        "controller_closeout",
        expected=True,
        actual=closeout.get("completed"),
        passed=closeout.get("completed") is True,
    )
    record_completion_check(
        refreshed,
        "evidence_annotator_disabled",
        expected="disabled",
        actual=annotator,
        passed=annotator == "disabled",
    )
    refreshed["export"] = export
    agent["goal_plus"] = refreshed
    cell["agent"] = agent

    evaluation = cell.get("evaluation") or {}
    patch_file = cell.get("patch_file")
    patch_exists = False
    if isinstance(patch_file, str):
        patch_path = (campaign / patch_file).resolve()
        patch_exists = (
            patch_path.is_relative_to(campaign_root)
            and patch_path.is_file()
            and bool(patch_path.read_text(encoding="utf-8").strip())
        )
    prior_state = cell.get("state")
    prior_reason = cell.get("incomplete_reason")
    failures = []
    if cell.get("error"):
        failures.append(str(cell["error"]))
    if not patch_exists:
        failures.append("Agent did not produce a non-empty patch")
    if not _container_isolated(agent):
        failures.append("Agent container isolation is incomplete")
    if evaluation.get("state") != "completed":
        failures.append("official evaluator did not produce a valid report")
    if evaluation.get("calls") != 1:
        failures.append("official evaluator call count is not exactly one")
    if not isinstance(evaluation.get("resolved"), bool):
        failures.append("official evaluator resolved metric is not boolean")
    if not isinstance(evaluation.get("patch_applied"), bool):
        failures.append("official evaluator patch apply metric is not boolean")
    if refreshed["completion"]["passed"] is not True:
        failures.append(
            str(
                refreshed["completion"].get("reason")
                or "Goal Plus completion evidence is incomplete"
            )
        )

    if not failures:
        agent["state"] = "completed"
        cell["state"] = "completed"
        cell.pop("incomplete_reason", None)
    else:
        agent["state"] = "partial"
        cell["state"] = "partial"
        cell["incomplete_reason"] = "; ".join(failures)
    changed = (
        previous_completion != refreshed["completion"]
        or prior_state != cell.get("state")
        or prior_reason != cell.get("incomplete_reason")
    )
    if not changed:
        return False
    cell["evidence_revalidation"] = {
        "at": utc_now(),
        "source": str(state_root.relative_to(campaign_root)),
        "prior_state": prior_state,
        "prior_incomplete_reason": prior_reason,
        "completion_passed": refreshed["completion"]["passed"],
        "result_state": cell["state"],
    }
    return True


def _record(campaign: Path, manifest: dict[str, Any], cell: dict[str, Any]) -> dict[str, Any]:
    evaluation = cell.get("evaluation") or {}
    agent = cell.get("agent") or {}
    goal_plus = agent.get("goal_plus") or {}
    goal_plus_completion = goal_plus.get("completion") or {}
    resolved = evaluation.get("resolved")
    final_metric = int(resolved) if isinstance(resolved, bool) else None
    return {
        "benchmark_id": "swe-bench-verified",
        "task_id": cell["task_id"],
        "cell_id": cell["cell_id"],
        "method": cell["method"],
        "model": cell["model"],
        "reasoning_effort": cell["reasoning_effort"],
        "seed": 1,
        "status": "succeeded" if cell["state"] == "completed" else cell["state"],
        "incomplete_reason": cell.get("incomplete_reason") or cell.get("error"),
        "budget": {
            "wall_time_seconds": manifest["budget"]["wall_time_seconds"],
            "live_search_concurrency": manifest["budget"][
                "live_search_concurrency"
            ],
            "cell_concurrency": manifest["budget"]["cell_concurrency"],
            "attempts": manifest["budget"]["attempts"],
        },
        "protocol": {
            "metric_name": "resolved",
            "direction": "maximize",
            "dataset": manifest["dataset"]["name"],
            "dataset_revision": manifest["dataset"]["revision"],
            "swebench_commit": manifest["source"]["swebench_commit"],
            "image": cell["image"],
            "base_commit": cell["base_commit"],
            "agent_provider": cell.get("agent_provider"),
            "official_evaluator": True,
            "official_evaluator_once": evaluation.get("calls") == 1,
            "goal_plus": {
                "required": cell["method"] == "goal-plus-pi",
                "completion": goal_plus_completion or None,
                "actual_subagent_count": goal_plus.get("actual_subagent_count"),
                "runs": goal_plus.get("runs") or [],
                "active_pi_pool_jobs": goal_plus.get("active_pi_pool_jobs") or [],
                "evidence_annotator": (
                    (agent.get("runtime") or {}).get("evidence_annotator")
                ),
            },
        },
        "score": {
            "final": final_metric,
            "raw_metrics": {
                "resolved": resolved,
                "patch_applied": evaluation.get("patch_applied"),
            },
            "valid": evaluation.get("state") == "completed",
        },
        "execution": {
            "agent_runtime_seconds": agent.get("runtime_seconds"),
            "agent_total_runtime_seconds": agent.get("total_runtime_seconds"),
            "agent_setup_runtime_seconds": agent.get("setup_runtime_seconds"),
            "finalization_grace_seconds": agent.get("finalization_grace_seconds"),
            "evaluator_runtime_seconds": evaluation.get("runtime_seconds"),
            "evaluator_calls": {
                "total_claimed": evaluation.get("calls"),
                "coverage": (
                    "complete" if isinstance(evaluation.get("calls"), int) else "missing"
                ),
            },
            "usage": {
                "outer_agent": agent.get("usage")
                or {"coverage": "unavailable"},
                "goal_plus_workers": goal_plus.get("worker_usage")
                or {"coverage": "unavailable"},
            },
            "goal_plus_closeout": agent.get("goal_plus_closeout"),
            "agent_container": agent.get("container"),
        },
        "patch": {
            "exists": agent.get("patch_exists"),
            "path": cell["patch_file"],
            "apply_status": evaluation.get("patch_applied"),
        },
        "run_dir": str(campaign),
        "evidence": {
            "agent_stdout": agent.get("stdout_file"),
            "agent_stderr": agent.get("stderr_file"),
            "official_report": evaluation.get("report_file"),
            "official_stdout": evaluation.get("stdout_file"),
            "official_stderr": evaluation.get("stderr_file"),
            "goal_plus_state": (
                (goal_plus.get("export") or {}).get("destination")
                if goal_plus
                else None
            ),
            "goal_plus_export": goal_plus.get("export") if goal_plus else None,
            "evidence_revalidation": cell.get("evidence_revalidation"),
        },
    }


def _markdown(summary: dict[str, Any]) -> str:
    records = summary["records"]
    lines = [
        f"# SWE-bench Verified report: {summary['campaign_id']}",
        "",
        f"Execution state: `{summary['state']}`.",
        "",
        "| Task | Method | Model | Resolved | Patch applied | Subagents | Evaluator calls |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for record in records:
        raw = record["score"]["raw_metrics"]
        lines.append(
            f"| {record['task_id']} | {record['method']} | {record['model']} | "
            f"{raw['resolved'] if raw['resolved'] is not None else ''} | "
            f"{raw['patch_applied'] if raw['patch_applied'] is not None else ''} | "
            f"{record['protocol']['goal_plus']['actual_subagent_count'] if record['protocol']['goal_plus']['required'] else ''} | "
            f"{record['execution']['evaluator_calls']['total_claimed']} |"
        )
    if records:
        lines.extend(
            [
                "",
                f"Dataset revision: `{records[0]['protocol']['dataset_revision']}`.",
                "",
                f"Official SWE-bench harness commit: `{records[0]['protocol']['swebench_commit']}`.",
                "",
            ]
        )
    return "\n".join(lines)


def finalize_campaign(campaign: Path) -> dict[str, Any]:
    manifest = read_json(campaign / MANIFEST)
    if manifest.get("state") not in TERMINAL_STATES:
        raise SweBenchContractError(
            f"campaign is not terminal: {manifest.get('state')!r}"
        )
    revalidated = False
    for cell in manifest["cells"]:
        revalidated = (
            _revalidate_goal_plus_cell(campaign, manifest, cell) or revalidated
        )
    if revalidated:
        if all(cell.get("state") == "completed" for cell in manifest["cells"]):
            manifest["state"] = "completed"
        elif any(
            cell.get("state") in {"completed", "partial"}
            for cell in manifest["cells"]
        ):
            manifest["state"] = "partial"
        else:
            manifest["state"] = "failed"
        manifest["evidence_revalidated_at"] = utc_now()
        write_json(campaign / MANIFEST, manifest)
    records = [_record(campaign, manifest, cell) for cell in manifest["cells"]]
    evaluated = [
        record
        for record in records
        if isinstance(record["score"]["raw_metrics"]["resolved"], bool)
    ]
    applied = [
        record
        for record in records
        if isinstance(record["score"]["raw_metrics"]["patch_applied"], bool)
    ]
    summary = {
        "schema_version": 1,
        "report_kind": "swe-bench-verified",
        "campaign_id": manifest["campaign_id"],
        "benchmark_id": "swe-bench-verified",
        "state": manifest["state"],
        "generated_at": utc_now(),
        "budget": manifest["budget"],
        "dataset": manifest["dataset"],
        "source": manifest["source"],
        "aggregates": {
            "task_count": len(records),
            "evaluated_count": len(evaluated),
            "resolved_count": sum(
                record["score"]["raw_metrics"]["resolved"] for record in evaluated
            ),
            "resolved_rate": (
                sum(
                    record["score"]["raw_metrics"]["resolved"]
                    for record in evaluated
                )
                / len(evaluated)
                if evaluated
                else None
            ),
            "patch_apply_rate": (
                sum(
                    record["score"]["raw_metrics"]["patch_applied"]
                    for record in applied
                )
                / len(applied)
                if applied
                else None
            ),
            "official_evaluator_calls": sum(
                record["execution"]["evaluator_calls"]["total_claimed"]
                for record in records
                if isinstance(
                    record["execution"]["evaluator_calls"]["total_claimed"], int
                )
            ),
        },
        "records": records,
    }
    write_json(campaign / "campaign-summary.json", summary)
    (campaign / "campaign-summary.md").write_text(
        _markdown(summary), encoding="utf-8"
    )
    return summary
