"""Resolve Pi model pricing without duplicating the Pi catalog."""

from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path
from typing import Any, Mapping


_COST_FIELDS = ("input", "output", "cacheRead", "cacheWrite")
_BUILTIN_PROVIDER_ALIASES = {"bench-openai": "openai"}


def _catalog_paths(
    catalog_path: Path | None,
    environment: Mapping[str, str],
) -> tuple[Path, ...]:
    if catalog_path is not None:
        return (catalog_path.expanduser().resolve(),)

    paths: list[Path] = []
    explicit = environment.get("SFORGE_PI_MODELS_FILE")
    if explicit:
        paths.append(Path(explicit).expanduser().resolve())
    agent_dir = Path(
        environment.get("PI_CODING_AGENT_DIR", Path.home() / ".pi" / "agent")
    ).expanduser().resolve()
    paths.extend((agent_dir / "models.json", agent_dir / "models-store.json"))
    return tuple(dict.fromkeys(paths))


def _builtin_catalog_path(
    pi_bin: str | Path,
    provider_id: str,
    environment: Mapping[str, str],
) -> Path | None:
    package_dir = environment.get("PI_PACKAGE_DIR")
    if package_dir:
        root = Path(package_dir).expanduser().resolve()
    else:
        executable = shutil.which(
            str(pi_bin), path=environment.get("PATH")
        )
        if executable is None:
            candidate = Path(pi_bin).expanduser()
            executable = str(candidate) if candidate.is_file() else None
        if executable is None:
            return None
        root = Path(executable).resolve().parent.parent
    path = (
        root
        / "node_modules/@earendil-works/pi-ai/dist/providers/data"
        / f"{provider_id}.json"
    )
    return path if path.is_file() else None


def _providers(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    configured = payload.get("providers")
    return configured if isinstance(configured, dict) else payload


def _valid_cost(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(value.get(field), int | float)
        and not isinstance(value.get(field), bool)
        and value[field] >= 0
        for field in _COST_FIELDS
    ) and any(value[field] > 0 for field in _COST_FIELDS)


def _matching_costs(
    payload: Any,
    *,
    provider_id: str,
    model_id: str,
    api: str,
) -> list[dict[str, Any]]:
    exact: list[dict[str, Any]] = []
    compatible: list[dict[str, Any]] = []
    for candidate_provider, provider in _providers(payload).items():
        if not isinstance(provider, dict):
            continue
        provider_api = provider.get("api")
        for model in provider.get("models", []):
            if not isinstance(model, dict) or model.get("id") != model_id:
                continue
            model_api = model.get("api", provider_api)
            cost = model.get("cost")
            if model_api != api or not _valid_cost(cost):
                continue
            target = exact if candidate_provider == provider_id else compatible
            target.append(copy.deepcopy(cost))
    packaged_models = payload.get(api) if isinstance(payload, dict) else None
    if isinstance(packaged_models, dict):
        for model in packaged_models.values():
            if not isinstance(model, dict) or model.get("id") != model_id:
                continue
            cost = model.get("cost")
            if not _valid_cost(cost):
                continue
            target = exact if model.get("provider") == provider_id else compatible
            target.append(copy.deepcopy(cost))
    return exact or compatible


def _catalog_cost(
    path: Path,
    *,
    provider_id: str,
    model_id: str,
    api: str,
) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Pi model catalog {path}: {exc}") from exc
    costs = _matching_costs(
        payload,
        provider_id=provider_id,
        model_id=model_id,
        api=api,
    )
    distinct = {json.dumps(cost, sort_keys=True) for cost in costs}
    if len(distinct) > 1:
        raise ValueError(
            f"ambiguous Pi pricing for {provider_id}/{model_id} in {path}"
        )
    return costs[0] if costs else None


def resolve_pi_model_cost(
    *,
    provider_id: str,
    model_id: str,
    api: str,
    catalog_path: Path | None = None,
    pi_bin: str | Path = "pi",
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Return per-million-token rates, or None when pricing is unavailable."""
    source = os.environ if environment is None else environment
    for path in _catalog_paths(catalog_path, source):
        if not path.is_file():
            continue
        cost = _catalog_cost(
            path,
            provider_id=provider_id,
            model_id=model_id,
            api=api,
        )
        if cost is not None:
            return cost

    if catalog_path is None:
        builtin_provider = _BUILTIN_PROVIDER_ALIASES.get(provider_id, provider_id)
        builtin = _builtin_catalog_path(pi_bin, builtin_provider, source)
        if builtin is not None:
            cost = _catalog_cost(
                builtin,
                provider_id=builtin_provider,
                model_id=model_id,
                api=api,
            )
            if cost is not None:
                return cost

    return None
