"""Runner adapter for benchmark-owned profile lifecycles such as EdgeBench."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from ..errors import ContractError, UnsupportedOperation
from ..models import CampaignRef, CampaignSpec, StatusSnapshot
from ..paths import ROOT, managed_python
from .base import BenchmarkRunner


TERMINAL = {"completed", "partial", "failed", "interrupted"}


def process_alive(pid: int | None) -> bool | None:
    if not isinstance(pid, int) or pid < 1:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class NativeProfileRunner(BenchmarkRunner):
    def local_asset_check_commands(
        self, profile: str, *, allow_missing: bool = False
    ) -> list[list[str]]:
        if not profile:
            raise ContractError("native-profile local-asset check requires a profile")
        command = [
            str(managed_python()),
            str(self.definition.controller.relative_to(ROOT)),
            "doctor",
            "--profile",
            profile,
            "--local-assets-only",
        ]
        if allow_missing:
            command.append("--allow-missing-local-assets")
        return [command]

    def provision_commands(
        self, spec: CampaignSpec, *, skip_provision: bool
    ) -> list[list[str]]:
        if not spec.profile:
            raise ContractError("native-profile runner requires a profile")
        python = str(managed_python())
        controller = str(self.definition.controller.relative_to(ROOT))
        commands: list[list[str]] = []
        if self.definition.capabilities.provision and not skip_provision:
            commands.append([python, controller, "provision", "--profile", spec.profile])
        doctor_command = [python, controller, "doctor", "--profile", spec.profile]
        if spec.model is not None:
            doctor_command.extend(["--model", spec.model])
        for method in spec.methods:
            doctor_command.extend(["--method", method])
        commands.append(doctor_command)
        return commands

    def prepare_commands(self, spec: CampaignSpec) -> tuple[list[list[str]], CampaignRef]:
        if len(spec.targets) != 1 or not spec.profile:
            raise ContractError("native-profile campaigns require one target and one profile")
        target = spec.targets[0]
        python = str(managed_python())
        controller = str(self.definition.controller.relative_to(ROOT))
        command = [
            python,
            controller,
            "prepare",
            "--profile",
            spec.profile,
            "--campaign-id",
            spec.campaign_id,
        ]
        for flag, value in (
            ("--model", spec.model),
            ("--reasoning-effort", spec.reasoning_effort),
            ("--wall-time-seconds", spec.wall_time_seconds),
            ("--concurrency", spec.live_search_concurrency),
            ("--cell-concurrency", spec.cell_concurrency),
        ):
            if value is not None:
                command.extend([flag, str(value)])
        if self.definition.capabilities.attempt_seed:
            command.extend(["--seed", str(spec.seeds[0])])
        for method in spec.methods:
            command.extend(["--method", method])
        if spec.retain_containers:
            command.append("--retain-containers")
        campaign = CampaignRef(
            campaign_id=spec.campaign_id,
            path=ROOT / "runs" / target.target_id / spec.campaign_id,
            target_id=target.target_id,
            runner_id=self.definition.runner_id,
        )
        return [command], campaign

    def start_command(
        self, spec: CampaignSpec, campaign: CampaignRef, *, detach: bool
    ) -> list[str]:
        command = self._campaign_command("run", campaign)
        if detach:
            command.append("--detach")
        return command

    def resume_command(self, state: dict, campaign: CampaignRef) -> list[str]:
        if not self.definition.capabilities.resume:
            raise UnsupportedOperation(
                "this native runner cannot resume the same trajectory; start a new attempt"
            )
        return self._campaign_command("run", campaign)

    def status(self, campaign: CampaignRef) -> StatusSnapshot:
        manifest_path = campaign.path / "campaign.json"
        if not manifest_path.is_file():
            raise ContractError(f"campaign manifest does not exist: {manifest_path}")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw = str(payload.get("state") or "unknown")
        controller_path = campaign.path / "controller.json"
        controller = (
            json.loads(controller_path.read_text(encoding="utf-8"))
            if controller_path.is_file()
            else {}
        )
        counts: dict[str, int] = {}
        for cell in payload.get("cells", []):
            cell_state = str(cell.get("state") or "unknown")
            counts[cell_state] = counts.get(cell_state, 0) + 1
        terminal = raw in TERMINAL
        normalized = {
            "prepared": "pending",
            "running": "running",
            "completed": "succeeded",
            "partial": "partial",
            "failed": "failed",
            "interrupted": "interrupted",
        }.get(raw, "unknown")
        pid = controller.get("pid") if isinstance(controller.get("pid"), int) else None
        native_details: dict[str, object] = {}
        command = [
            str(managed_python()),
            str(self.definition.controller.relative_to(ROOT)),
            "status",
            "--campaign",
            campaign.campaign_id,
            "--json",
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            try:
                parsed = json.loads(completed.stdout)
                if isinstance(parsed, dict):
                    native_details = parsed
            except json.JSONDecodeError:
                native_details = {"status_error": "native status returned invalid JSON"}
        else:
            native_details = {
                "status_error": completed.stderr.strip() or completed.stdout.strip()
            }
        return StatusSnapshot(
            state=normalized,
            raw_state=raw,
            terminal=terminal,
            controller_pid=pid,
            controller_alive=process_alive(pid),
            counts=counts,
            can_resume=raw == "interrupted" and self.definition.capabilities.resume,
            can_stop=raw == "running" and self.definition.capabilities.stop,
            can_finalize=terminal,
            details=native_details,
        )

    def stop_command(self, campaign: CampaignRef) -> list[str]:
        if not self.definition.capabilities.stop:
            raise UnsupportedOperation("runner does not support recoverable stop")
        return self._campaign_command("stop", campaign)

    def finalize_command(self, campaign: CampaignRef) -> list[str]:
        return self._campaign_command("finalize", campaign)

    def evidence_source(self, campaign: CampaignRef) -> Path:
        return campaign.path / self.definition.evidence_filename

    def _campaign_command(self, action: str, campaign: CampaignRef) -> list[str]:
        return [
            str(managed_python()),
            str(self.definition.controller.relative_to(ROOT)),
            action,
            "--campaign",
            campaign.campaign_id,
        ]
