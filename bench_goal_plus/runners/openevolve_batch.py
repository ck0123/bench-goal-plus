"""Runner adapter for the existing OpenEvolve comparison batch controller."""

from __future__ import annotations

import json
from pathlib import Path

from ..errors import ContractError, UnsupportedOperation
from ..models import CampaignRef, CampaignSpec, StatusSnapshot
from ..paths import ROOT, RUNS_ROOT, managed_python
from ..search_scheduler import internal_search_scheduler_args
from ..state import ensure_under
from .base import BenchmarkRunner


DEFAULT_METHODS = (
    "openevolve",
    "plain-codex",
    "goal-plus-codex",
    "goal-plus-pi",
)


class OpenEvolveBatchRunner(BenchmarkRunner):
    def provision_commands(
        self, spec: CampaignSpec, *, skip_provision: bool
    ) -> list[list[str]]:
        return []

    def prepare_commands(self, spec: CampaignSpec) -> tuple[list[list[str]], CampaignRef]:
        if len(spec.targets) != 1:
            raise ContractError("openevolve-batch accepts one task-set target")
        if not all(
            value is not None
            for value in (
                spec.model,
                spec.reasoning_effort,
                spec.wall_time_seconds,
                spec.live_search_concurrency,
            )
        ):
            raise ContractError("openevolve-batch requires model, reasoning, T, and K")
        if len(spec.seeds) != 1:
            raise ContractError("openevolve-batch currently supports one seed per campaign")
        if spec.cell_concurrency not in (None, 1):
            raise ContractError("openevolve-batch has not proven cross-cell concurrency; use C=1")
        destination = ensure_under(
            spec.campaign_dir
            or RUNS_ROOT / "openevolve-campaigns" / spec.campaign_id,
            RUNS_ROOT,
            label="campaign directory",
        )
        methods = spec.methods or DEFAULT_METHODS
        command = [
            str(managed_python()),
            str(self.definition.controller.relative_to(ROOT)),
            "prepare-batch",
            "--task-set",
            spec.profile or "cpu_portable",
            "--methods",
            *methods,
            "--seed",
            str(spec.seeds[0]),
            "--model",
            str(spec.model),
            "--reasoning-effort",
            str(spec.reasoning_effort),
            "--wall-time-seconds",
            str(spec.wall_time_seconds),
            "--concurrency",
            str(spec.live_search_concurrency),
            "--run-root",
            str(destination),
        ]
        command.extend(internal_search_scheduler_args(spec.search_scheduler))
        return [command], CampaignRef(
            campaign_id=spec.campaign_id,
            path=destination,
            target_id=spec.targets[0].target_id,
            runner_id=self.definition.runner_id,
        )

    def start_command(
        self, spec: CampaignSpec, campaign: CampaignRef, *, detach: bool
    ) -> list[str]:
        if detach:
            raise UnsupportedOperation("openevolve-batch runs in the foreground")
        command = [
            str(managed_python()),
            str(self.definition.controller.relative_to(ROOT)),
            "run-batch",
            "--campaign",
            str(campaign.path),
            "--model",
            str(spec.model),
        ]
        if spec.methods:
            command.extend(["--methods", *spec.methods])
        return command

    def resume_command(self, state: dict, campaign: CampaignRef) -> list[str]:
        spec = state.get("resolved_spec") or {}
        model = spec.get("model")
        if not model:
            raise ContractError("agent state does not record the campaign model")
        command = [
            str(managed_python()),
            str(self.definition.controller.relative_to(ROOT)),
            "run-batch",
            "--campaign",
            str(campaign.path),
            "--model",
            str(model),
        ]
        methods = spec.get("methods") or []
        if methods:
            command.extend(["--methods", *(str(item) for item in methods)])
        return command

    def status(self, campaign: CampaignRef) -> StatusSnapshot:
        manifest_path = campaign.path / "campaign.json"
        if not manifest_path.is_file():
            raise ContractError(f"campaign manifest does not exist: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = [item for item in manifest.get("entries", []) if isinstance(item, dict)]
        results_path = campaign.path / "campaign-results.json"
        results = []
        if results_path.is_file():
            results = json.loads(results_path.read_text(encoding="utf-8")).get("results", [])
        counts: dict[str, int] = {}
        for item in results:
            raw = str(item.get("status") or "unknown")
            counts[raw] = counts.get(raw, 0) + 1
        remaining = max(0, len(entries) - len(results))
        if remaining:
            counts["remaining"] = remaining
        if not results:
            raw_state, normalized, terminal = "prepared", "pending", False
        elif remaining:
            raw_state, normalized, terminal = "interrupted", "interrupted", False
        elif all(item.get("status") == "finished" for item in results):
            raw_state, normalized, terminal = "finished", "succeeded", True
        else:
            raw_state, normalized, terminal = "partial", "partial", True
        return StatusSnapshot(
            state=normalized,
            raw_state=raw_state,
            terminal=terminal,
            counts=counts,
            can_resume=not terminal,
            can_stop=False,
            can_finalize=terminal,
        )

    def stop_command(self, campaign: CampaignRef) -> list[str]:
        raise UnsupportedOperation(
            "openevolve-batch has no detached controller; interrupt its foreground process"
        )

    def finalize_command(self, campaign: CampaignRef) -> list[str]:
        return [
            str(managed_python()),
            "-m",
            "bench_goal_plus.finalize_hooks",
            "openevolve-batch",
            "--campaign",
            str(campaign.path),
        ]

    def evidence_source(self, campaign: CampaignRef) -> Path:
        return campaign.path / "campaign-summary.json"
