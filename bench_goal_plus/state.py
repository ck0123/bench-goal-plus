"""Durable Agent lifecycle state without duplicating runner-owned execution state."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ContractError
from .models import CampaignRef, CampaignSpec, EvidenceBundle, StatusSnapshot
from .paths import RUNS_ROOT


STATE_FILE = "agent-run.json"
PHASES = {"prepared", "running", "terminal", "finalized", "reported"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot read campaign state {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ContractError(f"campaign state must be an object: {path}")
    return payload


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.new")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def ensure_under(path: Path, root: Path, *, label: str) -> Path:
    # Normalize (collapse "..") and make absolute WITHOUT resolving symlinks, so a
    # campaign directory that is a symlink onto a roomier disk (e.g. /data2) is not
    # rejected for physically living outside runs/. The ".." collapse keeps the
    # escape guard intact for genuinely-outside paths.
    resolved = Path(os.path.normpath(path.expanduser().absolute()))
    root_normalized = Path(os.path.normpath(root.expanduser().absolute()))
    try:
        resolved.relative_to(root_normalized)
    except ValueError as error:
        raise ContractError(f"{label} must stay under {root}: {resolved}") from error
    return resolved


def resolve_campaign_path(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or len(candidate.parts) > 1:
        resolved = candidate.resolve() if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
        return ensure_under(resolved, RUNS_ROOT, label="campaign")
    matches = [path.parent for path in RUNS_ROOT.glob(f"*/{candidate.name}/campaign.json")]
    if not matches:
        raise ContractError(
            f"campaign {candidate.name!r} was not found; pass a path under {RUNS_ROOT}"
        )
    if len(matches) > 1:
        raise ContractError(
            f"campaign id {candidate.name!r} is ambiguous; pass its full path"
        )
    return matches[0].resolve()


def create_agent_state(
    spec: CampaignSpec,
    campaign: CampaignRef,
    *,
    commands: list[str],
    follow_up: dict[str, str],
    resolved_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "campaign_id": campaign.campaign_id,
        "campaign_path": str(campaign.path),
        "target_id": campaign.target_id,
        "runner_id": campaign.runner_id,
        "agent_phase": "prepared",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "resolved_spec": resolved_spec or spec.as_dict(),
        "runner_manifest": "campaign.json",
        "commands": commands,
        "follow_up": follow_up,
        "last_observed": None,
        "artifacts": {},
        "secret_policy": "credentials are inherited only and are never serialized",
    }
    write_json_atomic(campaign.path / STATE_FILE, payload)
    return payload


def load_agent_state(campaign: Path) -> dict[str, Any]:
    path = campaign / STATE_FILE
    if not path.is_file():
        raise ContractError(
            f"{STATE_FILE} is missing in {campaign}; use --benchmark once to adopt a legacy campaign"
        )
    payload = read_json(path)
    if payload.get("schema_version") != 1:
        raise ContractError("agent-run.json schema_version must be 1")
    if payload.get("agent_phase") not in PHASES:
        raise ContractError(f"invalid agent phase: {payload.get('agent_phase')!r}")
    return payload


def update_observation(
    campaign: Path, state: dict[str, Any], snapshot: StatusSnapshot
) -> dict[str, Any]:
    state = dict(state)
    state["last_observed"] = {"at": utc_now(), **snapshot.as_dict()}
    if snapshot.terminal and state.get("agent_phase") in {"prepared", "running"}:
        state["agent_phase"] = "terminal"
    elif not snapshot.terminal and snapshot.state == "running":
        state["agent_phase"] = "running"
    state["updated_at"] = utc_now()
    write_json_atomic(campaign / STATE_FILE, state)
    return state


def update_phase(
    campaign: Path,
    state: dict[str, Any],
    phase: str,
    *,
    evidence: EvidenceBundle | None = None,
) -> dict[str, Any]:
    if phase not in PHASES:
        raise ContractError(f"invalid agent phase: {phase}")
    state = dict(state)
    state["agent_phase"] = phase
    state["updated_at"] = utc_now()
    if evidence is not None:
        state["artifacts"] = evidence.as_dict()
    write_json_atomic(campaign / STATE_FILE, state)
    return state


def campaign_ref_from_state(campaign: Path, state: dict[str, Any]) -> CampaignRef:
    return CampaignRef(
        campaign_id=str(state["campaign_id"]),
        path=campaign,
        target_id=str(state["target_id"]),
        runner_id=str(state["runner_id"]),
    )
