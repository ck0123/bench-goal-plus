"""Resolve managed upstream checkouts and source subdirectories."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


def external_goal_plus_source() -> dict[str, str] | None:
    return _external_source("GOAL_PLUS", ("install.sh", "src/goal_plus"))


def external_swebench_source() -> dict[str, str] | None:
    return _external_source("SWEBENCH", ("pyproject.toml", "swebench/harness"))


def _external_source(name: str, required: tuple[str, ...]) -> dict[str, str] | None:
    """Resolve an externally supplied source checkout.

    ``BENCH_<name>_SOURCE_DIR`` selects the checkout.  ``BENCH_<name>_EXPECTED_REF``
    is optional: when set, HEAD must match it exactly (reproducible experiments);
    when unset, whatever commit the checkout currently sits on is used as-is, so a
    freshly downloaded repository works without pinning hashes.  The checkout must
    still be clean and contain the required assets, and the recorded identity
    (branch/commit) keeps prepare-vs-run consistency enforced either way.
    """
    source = os.environ.get(f"BENCH_{name}_SOURCE_DIR")
    expected = os.environ.get(f"BENCH_{name}_EXPECTED_REF")
    if not source:
        if expected:
            raise ValueError(
                f"external {name} requires BENCH_{name}_SOURCE_DIR when "
                f"BENCH_{name}_EXPECTED_REF is set"
            )
        return None
    path = Path(source).expanduser().resolve(strict=True)

    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()

    head = git("rev-parse", "HEAD")
    if expected:
        if head != git("rev-parse", "--verify", "--end-of-options", f"{expected}^{{commit}}"):
            raise ValueError(f"external {name} HEAD differs from expected ref")
    if git("status", "--porcelain"):
        raise ValueError(f"external {name} checkout has local changes")
    if not all((path / item).exists() for item in required):
        raise ValueError(f"external {name} source is missing required assets")
    identity: dict[str, str] = {"source_kind": "external", "source_dir": str(path)}
    if expected:
        identity["expected_ref"] = expected
    identity["branch"] = git("branch", "--show-current")
    identity["commit"] = head
    return identity


def require_prepared_goal_plus_source(identity: Mapping[str, Any] | None) -> None:
    if identity is None:
        return
    if external_goal_plus_source() != identity:
        raise ValueError("external Goal Plus source differs from the prepared campaign")
    import goal_plus

    installed = Path(goal_plus.__file__).resolve().parents[2]
    if installed != Path(identity["source_dir"]).resolve():
        raise ValueError("installed Goal Plus runtime differs from the prepared source")


def _relative_path(value: Any, *, field: str, upstream_key: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{upstream_key}: {field} must be a non-empty string")
    if "\\" in value:
        raise ValueError(f"{upstream_key}: {field} must use POSIX separators")
    relative = PurePosixPath(value)
    if (
        not relative.parts
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"{upstream_key}: unsafe {field} {value!r}")
    return Path(*relative.parts)


def upstream_checkout_path(
    checkout_root: Path,
    entry: Mapping[str, Any],
    *,
    upstream_key: str,
) -> Path:
    """Return the Git worktree root for a managed upstream."""

    return checkout_root / _relative_path(
        entry.get("checkout_dir"),
        field="checkout_dir",
        upstream_key=upstream_key,
    )


def upstream_source_path(
    checkout_root: Path,
    entry: Mapping[str, Any],
    *,
    upstream_key: str,
) -> Path:
    """Return the consumable source root within a managed upstream checkout."""

    if upstream_key == "goal_plus" and (external := external_goal_plus_source()):
        return Path(external["source_dir"])
    if upstream_key == "swebench" and (external := external_swebench_source()):
        return Path(external["source_dir"])

    checkout = upstream_checkout_path(
        checkout_root,
        entry,
        upstream_key=upstream_key,
    )
    source_subdir = entry.get("source_subdir")
    if source_subdir is None:
        return checkout
    return checkout / _relative_path(
        source_subdir,
        field="source_subdir",
        upstream_key=upstream_key,
    )


def registered_upstream_source_path(
    upstream_key: str,
    *,
    repository_root: Path,
) -> Path:
    """Resolve a source root from a repository's environment registry."""

    manifest_path = repository_root / "environment" / "upstreams.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = (manifest.get("upstreams") or {}).get(upstream_key)
    if not isinstance(entry, dict):
        raise ValueError(f"unknown managed upstream: {upstream_key}")
    return upstream_source_path(
        repository_root / "third_party",
        entry,
        upstream_key=upstream_key,
    )


def registered_upstream_branch(
    upstream_key: str,
    *,
    repository_root: Path,
) -> str:
    """Return the tracked branch declared for a managed upstream."""

    manifest_path = repository_root / "environment" / "upstreams.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = (manifest.get("upstreams") or {}).get(upstream_key)
    branch = entry.get("tracking_branch") if isinstance(entry, dict) else None
    if not isinstance(branch, str) or not branch:
        raise ValueError(f"managed upstream {upstream_key!r} has no tracking_branch")
    return branch
