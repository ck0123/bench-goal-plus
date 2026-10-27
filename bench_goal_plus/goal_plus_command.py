"""Shared rendering for exact host-native Goal Plus commands."""

from __future__ import annotations

import re
from typing import Literal


GoalPlusAgentHarness = Literal["codex", "pi"]
GoalPlusWorkspaceProvider = Literal["git_worktree", "thinkthread"]
GoalPlusPromotionMode = Literal["apply", "artifact_only"]
GOAL_PLUS_MODEL_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*\Z")


def goal_plus_entrypoint(agent_harness: GoalPlusAgentHarness) -> str:
    if agent_harness == "codex":
        return "$goal-plus"
    if agent_harness == "pi":
        return "/goal-plus"
    raise ValueError(f"unsupported Goal Plus worker host: {agent_harness}")


def _config_token(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized or any(character.isspace() for character in normalized):
        raise ValueError(f"Goal Plus {name} must be one non-empty token")
    return normalized


def _model_token(name: str, value: str) -> str:
    normalized = _config_token(name, value)
    if not GOAL_PLUS_MODEL_TOKEN_PATTERN.fullmatch(normalized):
        raise ValueError(f"Goal Plus {name} must be one safe model token")
    return normalized


def goal_plus_command_config(
    *,
    max_parallel: int,
    strategy: str,
    worker_model: str | None,
    annotator_model: str | None = None,
    workspace_provider: GoalPlusWorkspaceProvider = "git_worktree",
    promotion_mode: GoalPlusPromotionMode = "apply",
    mode: Literal["autonomous", "probe"] = "autonomous",
    spawn_mode: Literal["fresh", "fork"] = "fresh",
) -> dict[str, str | int]:
    """Return the typed config persisted by the Goal Plus host command."""

    if max_parallel < 1:
        raise ValueError("Goal Plus max_parallel must be positive")
    config: dict[str, str | int] = {
        "mode": mode,
        "max_parallel": max_parallel,
        "workspace_provider": workspace_provider,
        "promotion_mode": promotion_mode,
        "strategy": _config_token("strategy", strategy),
        "spawn_mode": _config_token("spawn mode", spawn_mode),
    }
    if worker_model is not None:
        model = _config_token("worker model", worker_model)
        if "," in model or "*" in model:
            raise ValueError("Goal Plus worker model must be one uncounted model")
        model = _model_token("worker model", model)
        config["workers"] = f"{model}*{max_parallel}"
    if annotator_model is not None:
        annotator = _config_token("annotator model", annotator_model)
        if "," in annotator or "*" in annotator:
            raise ValueError("Goal Plus annotator model must be one model")
        annotator = _model_token("annotator model", annotator)
        config["annotator"] = annotator
    return config


def render_goal_plus_command(
    agent_harness: GoalPlusAgentHarness,
    *,
    max_parallel: int,
    strategy: str,
    worker_model: str | None,
    annotator_model: str | None = None,
    workspace_provider: GoalPlusWorkspaceProvider = "git_worktree",
    promotion_mode: GoalPlusPromotionMode = "apply",
    mode: Literal["autonomous", "probe"] = "autonomous",
    spawn_mode: Literal["fresh", "fork"] = "fresh",
) -> str:
    """Render ``$goal-plus``/``/goal-plus`` plus leading typed config tokens."""

    config = goal_plus_command_config(
        max_parallel=max_parallel,
        strategy=strategy,
        worker_model=worker_model,
        annotator_model=annotator_model,
        workspace_provider=workspace_provider,
        promotion_mode=promotion_mode,
        mode=mode,
        spawn_mode=spawn_mode,
    )
    tokens = [goal_plus_entrypoint(agent_harness)]
    tokens.extend(f"{name}={value}" for name, value in config.items())
    return " ".join(tokens)
