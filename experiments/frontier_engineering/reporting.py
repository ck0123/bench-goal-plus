"""Finalize Frontier-Engineering campaigns into the generic report contract."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from bench_artifacts import utc_now
from experiments.benchmark_campaign.experiment import coordination_metrics, trajectory_metrics
from experiments.openevolve_compare.reporting import collect_run, render_markdown

from .config import write_json


TERMINAL = {"completed", "partial", "failed", "interrupted"}


def _reaudit_parallel_evidence(destination: Path, campaign: dict[str, Any]) -> None:
    """Reclassify old receipt-only audits through the existing closeout gates.

    No worker is resumed and the saved final evaluation is reused. Preserve the
    previous classification and reports before repairing derived evidence.
    """
    from experiments.benchmark_compare import experiment as standalone

    repairs = []
    for cell in campaign["cells"]:
        run_dir = Path(cell["run_dir"])
        manifest_path = run_dir / "experiment.json"
        if cell["state"] != "partial" or not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text())
        execution = manifest.get("execution") or {}
        if (manifest.get("method") != "goal-plus-pi"
                or not str(execution.get("result_incomplete_reason", "")).startswith(
                    "continuous parallel execution: ")
                or (execution.get("parallel_execution") or {}).get("schema_version") != 1
                or not (run_dir / "final-eval.json").is_file()):
            continue
        snapshots = []
        for path in [destination / name for name in (
            "campaign.json", "agent-run.json", "campaign-summary.json", "campaign-summary.md",
            "report.md", f"{destination.name}.xlsx",
        )] + [manifest_path, run_dir / "parallel-execution.json"]:
            if path.is_file():
                backup = path.with_name(path.name + "_bak")
                index = 2
                while backup.exists():
                    backup = path.with_name(path.name + f"_bak{index}")
                    index += 1
                shutil.copyfile(path, backup)
                snapshots.append(str(backup))
        previous = {key: cell.get(key) for key in ("state", "returncode", "error")}
        returncode = standalone.repair_closeout(argparse.Namespace(run_dir=run_dir))
        repaired = json.loads(manifest_path.read_text())
        final = json.loads((run_dir / "final-eval.json").read_text())
        if returncode == 0 and repaired.get("status") == "finished" and final.get("valid") is True:
            cell.update(state="completed", returncode=0, error=None)
        else:
            cell["error"] = (repaired.get("execution") or {}).get("result_incomplete_reason") or cell.get("error")
        repairs.append({"cell_id": cell["cell_id"], "previous": previous,
                        "state": cell["state"], "backups": snapshots, "reaudited_at": utc_now()})
    if repairs:
        campaign.setdefault("evidence_reaudits", []).extend(repairs)
        if campaign["state"] == "partial" and all(cell["state"] == "completed" for cell in campaign["cells"]):
            campaign["state"] = "completed"
        write_json(destination / "campaign.json", campaign)


def finalize_campaign(destination: Path) -> dict[str, Any]:
    campaign = json.loads((destination / "campaign.json").read_text(encoding="utf-8"))
    if campaign["state"] not in TERMINAL:
        raise RuntimeError(f"cannot finalize non-terminal campaign: {campaign['state']}")
    _reaudit_parallel_evidence(destination, campaign)
    records = []
    for cell in campaign["cells"]:
        run_dir = Path(cell["run_dir"])
        record = collect_run(
            run_dir,
            campaign_id=campaign["campaign_id"],
            campaign=campaign,
            entry=cell,
            ledger=cell,
        )
        manifest_path = run_dir / "experiment.json"
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.is_file()
            else {}
        )
        score = record.get("score") or {}
        protocol = record.get("protocol") or {}
        task_metadata = manifest.get("task") or {}
        final_path = run_dir / "final-eval.json"
        if not task_metadata.get("evaluation_protocol") and final_path.is_file():
            final_metadata = json.loads(final_path.read_text(encoding="utf-8"))
            task_metadata = {**task_metadata, **{
                key: final_metadata[key]
                for key in ("evaluation_protocol", "evaluator_sha256") if key in final_metadata
            }}
        protocol["evaluation_protocol"] = task_metadata.get("evaluation_protocol", "legacy-frozen")
        protocol["evaluator_sha256"] = task_metadata.get("evaluator_sha256")
        record["protocol"] = protocol
        execution = (
            manifest.get("execution")
            if isinstance(manifest.get("execution"), dict)
            else {}
        )
        if manifest.get("method") == "openevolve":
            protocol["iterations"] = (manifest.get("budget") or {}).get(
                "iterations_ceiling"
            )
            record["protocol"] = protocol
            record["execution"]["iterations"] = execution.get("iterations")
            record["execution"]["total_duration_seconds"] = execution.get(
                "total_duration_seconds"
            )
        record.update(
            {
                "benchmark_id": "frontier-engineering",
                "cell_id": cell["cell_id"],
                "task_id": cell["task_id"],
                "effective_concurrency": campaign["budget"]["live_search_concurrency"],
                "trajectory": trajectory_metrics(
                    run_dir,
                    manifest,
                    seed_score=score.get("seed_best"),
                    direction=protocol.get("direction"),
                    threshold=None,
                ),
                "coordination": coordination_metrics(manifest),
            }
        )
        records.append(record)
    accepted = all(
        record.get("status") == "finished"
        and (record.get("score") or {}).get("valid") is True
        for record in records
    )
    final_state = (
        "completed"
        if campaign["state"] == "completed" and accepted
        else campaign["state"]
        if campaign["state"] != "completed"
        else "partial"
    )
    summary = {
        "schema_version": 1,
        "report_kind": "campaign",
        "campaign_id": campaign["campaign_id"],
        "benchmark": "frontier-engineering",
        "suite": "v1-lite",
        "state": final_state,
        "updated_at": utc_now(),
        "record_count": len(records),
        "budget": campaign["budget"],
        "wall_time_seconds": campaign["budget"]["wall_time_seconds"],
        "live_search_concurrency": campaign["budget"]["live_search_concurrency"],
        "cell_concurrency": campaign["budget"]["cell_concurrency"],
        "attempts": campaign["budget"]["attempts"],
        "records": records,
        "coverage": {
            "final_score": "upstream Frontier-Engineering UnifiedTask evaluator",
            "trajectory": "controller evaluator histories",
            "coordination": "Goal Plus Search Space evidence when applicable",
        },
    }
    summary_path = destination / "campaign-summary.json"
    markdown_path = destination / "campaign-summary.md"
    write_json(summary_path, summary)
    markdown_path.write_text(
        render_markdown(
            summary,
            title=f"Frontier-Engineering v1-lite: {campaign['campaign_id']}",
            output_path=markdown_path,
        ),
        encoding="utf-8",
    )
    return summary
