#!/usr/bin/env python3
"""Create and verify the portable benchmark runtime and managed checkouts."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench_runtime_paths import (  # noqa: E402
    DEFAULT_TEMP_ROOT,
    configure_temp_environment,
    ensure_temp_root,
)


DEFAULT_MANIFEST = ROOT / "environment/upstreams.json"
DEFAULT_LOCK = ROOT / "environment/requirements.lock"
DEFAULT_VENV = ROOT / ".bench-env/venv"
DEFAULT_CHECKOUT_ROOT = ROOT / "third_party"
STATE_PATH = ROOT / ".bench-env/state.json"


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=capture,
        text=True,
        check=check,
        env=env,
        timeout=timeout,
    )


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if payload.get("schema_version") != 2:
        raise RuntimeError("unsupported environment manifest schema")
    for name, entry in payload.get("upstreams", {}).items():
        branch = entry.get("tracking_branch")
        if not isinstance(branch, str) or not branch:
            raise RuntimeError(f"{name}: tracking_branch is required")
        if branch.startswith(("-", ".", "/")) or ".." in branch:
            raise RuntimeError(f"{name}: unsafe tracking_branch {branch!r}")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) is None:
            raise RuntimeError(f"{name}: invalid tracking_branch {branch!r}")
    return payload


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def venv_bin(venv: Path) -> Path:
    return venv / ("Scripts" if sys.platform == "win32" else "bin")


def selected_upstreams(
    manifest: dict[str, Any],
    only: list[str] | None = None,
    *,
    include_always: bool = True,
) -> dict[str, dict[str, Any]]:
    upstreams = manifest["upstreams"]
    if not only:
        return dict(upstreams) if include_always else {}
    requested = set(only)
    unknown = requested - set(upstreams)
    if unknown:
        raise ValueError("unknown managed checkout(s): " + ", ".join(sorted(unknown)))
    return {
        name: entry
        for name, entry in upstreams.items()
        if (include_always and entry.get("always") is True) or name in requested
    }


def checkout_paths(
    manifest: dict[str, Any],
    checkout_root: Path,
    only: list[str] | None = None,
    *,
    include_always: bool = True,
) -> dict[str, Path]:
    return {
        name: checkout_root / entry["checkout_dir"]
        for name, entry in selected_upstreams(
            manifest, only, include_always=include_always
        ).items()
    }


def normalize_repository(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.rstrip("/").removesuffix(".git")
    ssh_match = re.fullmatch(r"(?:ssh://)?git@([^/:]+)[:/](.+)", normalized)
    if ssh_match:
        host, path = ssh_match.groups()
        return f"https://{host.lower()}/{path}"
    return normalized


def repository_transport_url(value: str) -> str:
    normalized = value.rstrip("/")
    ssh_match = re.fullmatch(r"(?:ssh://)?git@([^/:]+)[:/](.+)", normalized)
    if ssh_match:
        host, path = ssh_match.groups()
        return f"https://{host.lower()}/{path}"
    return normalized


def redact_git_text(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"((?:https?|ssh)://)[^/@\s]+@", r"\1<redacted>@", value)


def _git_network_environment(*, ignore_global_config: bool) -> dict[str, str]:
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    if ignore_global_config:
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
    return environment


def run_remote_git(
    primary: list[str],
    fallback: list[str],
    *,
    check: bool,
    timeout: int,
) -> tuple[subprocess.CompletedProcess[str], str]:
    failures: list[str] = []
    attempts = (
        (primary, False, "configured-remote"),
        (fallback, True, "manifest-repository"),
    )
    for command, ignore_global_config, transport in attempts:
        try:
            result = run(
                command,
                check=False,
                env=_git_network_environment(
                    ignore_global_config=ignore_global_config
                ),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            failures.append(f"{transport}: timed out after {timeout}s")
            continue
        if result.returncode == 0:
            return result, transport
        detail = redact_git_text((result.stderr or result.stdout).strip()) or ""
        failures.append(f"{transport}: {detail or f'exit {result.returncode}'}")
    message = "; ".join(failures)
    if check:
        raise RuntimeError(f"Git remote operation failed: {message}")
    return subprocess.CompletedProcess(primary, 1, "", message), "failed"


def git_state(path: Path, tracking_branch: str | None = None) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "is_git": False,
            "head": None,
            "branch": None,
            "upstream": None,
            "remote_head": None,
            "origin_url": None,
            "dirty": None,
        }
    head = run(["git", "-C", str(path), "rev-parse", "HEAD"], check=False)
    status = run(["git", "-C", str(path), "status", "--porcelain"], check=False)
    branch = run(
        ["git", "-C", str(path), "symbolic-ref", "--short", "-q", "HEAD"],
        check=False,
    )
    upstream = run(
        [
            "git",
            "-C",
            str(path),
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
        ],
        check=False,
    )
    origin = run(
        ["git", "-C", str(path), "remote", "get-url", "origin"],
        check=False,
    )
    remote_head = None
    if tracking_branch:
        remote = run(
            [
                "git",
                "-C",
                str(path),
                "rev-parse",
                f"refs/remotes/origin/{tracking_branch}",
            ],
            check=False,
        )
        remote_head = remote.stdout.strip() if remote.returncode == 0 else None
    return {
        "exists": True,
        "is_git": head.returncode == 0,
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "upstream": upstream.stdout.strip() if upstream.returncode == 0 else None,
        "remote_head": remote_head,
        "origin_url": origin.stdout.strip() if origin.returncode == 0 else None,
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
    }


def ensure_checkout(path: Path, entry: dict[str, Any]) -> None:
    branch = entry["tracking_branch"]
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_name(path.name + "_bootstrap_incomplete")
        if staging.exists():
            raise RuntimeError(
                f"preserved incomplete checkout exists: {staging}; rename it to *_bak "
                "after inspection before retrying"
            )
        staging.mkdir()
        run(["git", "-C", str(staging), "init", "-q"])
        run(
            [
                "git",
                "-C",
                str(staging),
                "remote",
                "add",
                "origin",
                entry["repository"],
            ]
        )
        fetch_command = ["git", "-C", str(staging), "fetch"]
        if entry.get("sparse_paths"):
            fetch_command.append("--filter=blob:none")
        fetch_command.extend(
            [
                "--depth",
                "1",
                "origin",
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            ]
        )
        fallback_fetch = list(fetch_command)
        fallback_fetch[fallback_fetch.index("origin")] = repository_transport_url(
            entry["repository"]
        )
        run_remote_git(
            fetch_command,
            fallback_fetch,
            check=True,
            timeout=300,
        )
        if entry.get("sparse_paths"):
            run(
                [
                    "git",
                    "-C",
                    str(staging),
                    "sparse-checkout",
                    "set",
                    "--no-cone",
                    *entry["sparse_paths"],
                ]
            )
        run(
            [
                "git",
                "-C",
                str(staging),
                "checkout",
                "-q",
                "-b",
                branch,
                "--track",
                f"origin/{branch}",
            ]
        )
        staging.rename(path)
        return
    state = git_state(path, branch)
    if not state["is_git"]:
        raise RuntimeError(f"existing checkout path is not a Git repository: {path}")
    if state["dirty"]:
        raise RuntimeError(f"checkout has local changes and will not be used: {path}")
    if normalize_repository(state["origin_url"]) != normalize_repository(
        entry["repository"]
    ):
        raise RuntimeError(
            f"origin mismatch for {path}: expected {entry['repository']}, "
            f"got {redact_git_text(state['origin_url'])}; use a separate checkout root"
        )
    fetch_command = ["git", "-C", str(path), "fetch", "--prune"]
    if entry.get("sparse_paths"):
        fetch_command.append("--filter=blob:none")
    fetch_command.extend(
        [
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        ]
    )
    fallback_fetch = list(fetch_command)
    fallback_fetch[fallback_fetch.index("origin")] = repository_transport_url(
        entry["repository"]
    )
    run_remote_git(
        fetch_command,
        fallback_fetch,
        check=True,
        timeout=300,
    )
    state = git_state(path, branch)
    if state["branch"] is None:
        local_branch = run(
            [
                "git",
                "-C",
                str(path),
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            check=False,
        )
        if local_branch.returncode == 0:
            run(["git", "-C", str(path), "checkout", "-q", branch])
        else:
            run(
                [
                    "git",
                    "-C",
                    str(path),
                    "checkout",
                    "-q",
                    "-b",
                    branch,
                    "--track",
                    f"origin/{branch}",
                ]
            )
    elif state["branch"] != branch:
        raise RuntimeError(
            f"checkout {path} is on branch {state['branch']!r}, expected {branch!r}; "
            "switch it explicitly or use a separate checkout root"
        )
    state = git_state(path, branch)
    expected_upstream = f"origin/{branch}"
    if state["upstream"] != expected_upstream:
        run(
            [
                "git",
                "-C",
                str(path),
                "branch",
                "--set-upstream-to",
                expected_upstream,
                branch,
            ]
        )
    merged = run(
        ["git", "-C", str(path), "merge", "--ff-only", expected_upstream],
        check=False,
    )
    if merged.returncode != 0:
        raise RuntimeError(
            f"checkout {path} cannot fast-forward to {expected_upstream}: "
            f"{merged.stderr.strip() or merged.stdout.strip()}"
        )
    state = git_state(path, branch)
    if state["head"] != state["remote_head"]:
        raise RuntimeError(
            f"checkout {path} has unpublished or divergent commits on {branch}; "
            "push them to the tracked branch or use a separate checkout root"
        )


def advertised_remote_head(
    path: Path, entry: dict[str, Any]
) -> dict[str, Any]:
    branch = entry["tracking_branch"]
    reference = f"refs/heads/{branch}"
    primary = ["git", "-C", str(path), "ls-remote", "--exit-code", "origin", reference]
    fallback = [
        "git",
        "ls-remote",
        "--exit-code",
        repository_transport_url(entry["repository"]),
        reference,
    ]
    result, transport = run_remote_git(
        primary,
        fallback,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        return {
            "ok": False,
            "head": None,
            "transport": transport,
            "error": result.stderr.strip() or "remote query failed",
        }
    matches = [line.split() for line in result.stdout.splitlines() if line.strip()]
    heads = [parts[0] for parts in matches if len(parts) == 2 and parts[1] == reference]
    if len(heads) != 1:
        return {
            "ok": False,
            "head": None,
            "transport": transport,
            "error": f"expected one advertised {reference}, got {len(heads)}",
        }
    return {
        "ok": True,
        "head": heads[0],
        "transport": transport,
        "error": None,
    }


def repository_update_check(
    name: str, path: Path, entry: dict[str, Any]
) -> dict[str, Any]:
    branch = entry["tracking_branch"]
    state = git_state(path, branch)
    blockers: list[str] = []
    if state["exists"] and not state["is_git"]:
        blockers.append("checkout path is not a Git repository")
    if state["dirty"]:
        blockers.append("checkout has local changes")
    if state["origin_url"] and normalize_repository(
        state["origin_url"]
    ) != normalize_repository(entry["repository"]):
        blockers.append("origin does not match the registered repository")
    if state["branch"] not in (None, branch):
        blockers.append(
            f"checkout is on {state['branch']!r}, expected {branch!r}"
        )
    remote = advertised_remote_head(path, entry)
    update_available = bool(
        remote["ok"] and state["head"] != remote["head"]
    )
    repair_required = bool(
        state["exists"]
        and state["is_git"]
        and not blockers
        and (
            state["branch"] != branch
            or state["upstream"] != f"origin/{branch}"
            or state["remote_head"] != state["head"]
        )
    )
    clone_required = not state["exists"]
    action_required = update_available or repair_required or clone_required
    passed = bool(remote["ok"] and not blockers and not action_required)
    return {
        "name": name,
        "path": str(path),
        "branch": branch,
        "repository": redact_git_text(entry["repository"]),
        "passed": passed,
        "current_head": state["head"],
        "advertised_head": remote["head"],
        "update_available": update_available,
        "repair_required": repair_required,
        "clone_required": clone_required,
        "action_required": action_required,
        "query_ok": remote["ok"],
        "transport": remote["transport"],
        "query_error": remote["error"],
        "blockers": blockers,
    }


def root_repository_entry(
    root: Path = ROOT,
) -> tuple[dict[str, Any] | None, str | None]:
    state = git_state(root)
    branch = state["branch"]
    upstream = state["upstream"]
    if not state["is_git"]:
        return None, "bench-goal-plus root is not a Git repository"
    if not branch:
        return None, "bench-goal-plus root is on a detached HEAD"
    if upstream != f"origin/{branch}":
        return None, (
            "bench-goal-plus root must track "
            f"origin/{branch}; current upstream is {upstream!r}"
        )
    if not state["origin_url"]:
        return None, "bench-goal-plus root has no origin URL"
    return {
        "repository": state["origin_url"],
        "tracking_branch": branch,
    }, None


def collect_repository_updates(
    manifest: dict[str, Any],
    checkout_root: Path,
    *,
    only: list[str] | None = None,
    include_root: bool = True,
) -> dict[str, Any]:
    sources: list[tuple[str, Path, dict[str, Any]]] = []
    early_checks: list[dict[str, Any]] = []
    if include_root:
        root_entry, error = root_repository_entry()
        if root_entry is None:
            early_checks.append(
                {
                    "name": "bench_goal_plus",
                    "path": str(ROOT),
                    "passed": False,
                    "action_required": False,
                    "update_available": False,
                    "repair_required": False,
                    "clone_required": False,
                    "query_ok": False,
                    "query_error": error,
                    "blockers": [error],
                }
            )
        else:
            sources.append(("bench_goal_plus", ROOT, root_entry))
    chosen = selected_upstreams(manifest, only)
    for name, entry in chosen.items():
        sources.append(
            (name, checkout_root / entry["checkout_dir"], entry)
        )
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(8, max(1, len(sources)))
    ) as executor:
        checks = list(
            executor.map(
                lambda item: repository_update_check(*item),
                sources,
            )
        )
    checks = [*early_checks, *checks]
    return {
        "schema_version": 1,
        "ok": all(item["passed"] for item in checks),
        "action_required": any(item["action_required"] for item in checks),
        "updates_available": sum(
            1 for item in checks if item["update_available"]
        ),
        "repairs_required": sum(
            1 for item in checks if item["repair_required"]
        ),
        "clones_required": sum(
            1 for item in checks if item["clone_required"]
        ),
        "query_failures": sum(1 for item in checks if not item["query_ok"]),
        "blocked": any(item["blockers"] for item in checks),
        "checks": checks,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def edgebench_rust_runtime_asset(edgebench_checkout: Path) -> dict[str, Any]:
    manifest_path = (
        edgebench_checkout / "sforge" / "harness" / "runtime_assets.json"
    )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"cannot read EdgeBench runtime asset manifest: {manifest_path}"
        ) from exc
    asset = payload.get("rust") if payload.get("schema_version") == 1 else None
    required = {"version", "target", "archive_name", "url", "sha256"}
    if not isinstance(asset, dict) or required - set(asset):
        raise RuntimeError(f"invalid EdgeBench runtime asset manifest: {manifest_path}")
    result: dict[str, Any] = {key: str(asset[key]) for key in required}
    download_urls = asset.get("download_urls")
    if download_urls is not None:
        if not isinstance(download_urls, list) or not all(
            isinstance(url, str) and url for url in download_urls
        ):
            raise RuntimeError(
                f"invalid Rust download_urls in runtime asset manifest: {manifest_path}"
            )
        result["download_urls"] = list(download_urls)
    return result


def rust_runtime_cache_path(
    asset: dict[str, Any], cache_root: Path | None = None
) -> Path:
    root = cache_root or Path.home() / ".cache" / "sforge" / "rust"
    return root / asset["archive_name"]


def _preserve_as_backup(path: Path) -> Path:
    suffix = 1
    while True:
        ending = "_bak" if suffix == 1 else f"_bak{suffix}"
        backup = path.with_name(path.name + ending)
        if not backup.exists():
            path.rename(backup)
            print(f"Preserved conflicting runtime asset as {backup}")
            return backup
        suffix += 1


def ensure_edgebench_rust_runtime(
    edgebench_checkout: Path,
    cache_root: Path | None = None,
) -> Path:
    """Download the pinned Rust distribution to a host cache atomically."""

    asset = edgebench_rust_runtime_asset(edgebench_checkout)
    destination = rust_runtime_cache_path(asset, cache_root)
    expected_sha256 = asset["sha256"]
    if destination.is_file() and sha256_file(destination) == expected_sha256:
        print(f"Rust runtime cache ready: {destination}")
        return destination
    if destination.exists():
        _preserve_as_backup(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + "_bootstrap_incomplete")
    if staging.is_file() and sha256_file(staging) == expected_sha256:
        staging.rename(destination)
        print(f"Rust runtime cache ready: {destination}")
        return destination
    if staging.exists():
        _preserve_as_backup(staging)

    urls = list(asset.get("download_urls") or [asset["url"]])
    failures: list[str] = []
    for url in urls:
        offset = staging.stat().st_size if staging.is_file() else 0
        print(
            f"Downloading Rust {asset['version']} ({asset['target']}) from {url} "
            f"to {destination} (resume={offset})"
        )
        request = urllib.request.Request(url)
        if offset:
            request.add_header("Range", f"bytes={offset}-")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                partial = getattr(response, "status", None) == 206
                if offset and not partial:
                    _preserve_as_backup(staging)
                    offset = 0
                mode = "ab" if offset and partial else "wb"
                with staging.open(mode) as sink:
                    shutil.copyfileobj(response, sink, length=1024 * 1024)
        except (OSError, urllib.error.URLError) as exc:
            failures.append(f"{url}: {exc}")
            continue

        actual_sha256 = sha256_file(staging)
        if actual_sha256 == expected_sha256:
            staging.rename(destination)
            print(f"Rust runtime cache ready: {destination}")
            return destination
        failures.append(
            f"{url}: checksum mismatch (expected {expected_sha256}, "
            f"got {actual_sha256})"
        )
        _preserve_as_backup(staging)

    raise RuntimeError(
        "failed to download the pinned Rust runtime; " + "; ".join(failures)
    )


def edgebench_rust_runtime_status(edgebench_checkout: Path) -> dict[str, Any]:
    try:
        asset = edgebench_rust_runtime_asset(edgebench_checkout)
        archive = rust_runtime_cache_path(asset)
        actual_sha256 = sha256_file(archive) if archive.is_file() else None
        return {
            "name": "runtime:edgebench-rust",
            "passed": actual_sha256 == asset["sha256"],
            "version": asset["version"],
            "target": asset["target"],
            "path": str(archive),
            "expected_sha256": asset["sha256"],
            "actual_sha256": actual_sha256,
        }
    except RuntimeError as exc:
        return {
            "name": "runtime:edgebench-rust",
            "passed": False,
            "error": str(exc),
        }


def package_versions(python: Path) -> dict[str, str | None]:
    packages = (
        "openevolve",
        "goal-plus",
        "skydiscover",
        "sforge",
        "fastapi",
        "fastmcp",
        "numpy",
        "scipy",
        "openai",
    )
    script = (
        "import importlib.metadata,json\n"
        f"names={packages!r}\n"
        "out={}\n"
        "for name in names:\n"
        "  try: out[name]=importlib.metadata.version(name)\n"
        "  except importlib.metadata.PackageNotFoundError: out[name]=None\n"
        "print(json.dumps(out))\n"
    )
    result = run([str(python), "-c", script], check=False)
    if result.returncode != 0:
        return {name: None for name in packages}
    return json.loads(result.stdout)


def parse_codex_version(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(part) for part in match.groups()) if match else None


def host_pi_check(manifest: dict[str, Any], *, required: bool = False) -> dict[str, Any]:
    pi_path = shutil.which("pi")
    pi_text = None
    pi_version = None
    if pi_path:
        result = run([pi_path, "--version"], check=False)
        pi_text = (result.stdout or result.stderr).strip()
        pi_version = parse_codex_version(pi_text)
    pi_minimum = tuple(int(part) for part in manifest["pi_min_version"].split("."))
    compatible = bool(pi_version and pi_version >= pi_minimum)
    return {
        "name": "host:pi",
        "passed": compatible if required else True,
        "required": required,
        "available": pi_path is not None,
        "compatible": compatible,
        "version": pi_text,
        "minimum": manifest["pi_min_version"],
    }


def host_codex_check(
    manifest: dict[str, Any], *, required: bool = False
) -> dict[str, Any]:
    codex_path = shutil.which("codex")
    codex_text = None
    codex_version = None
    if codex_path:
        result = run([codex_path, "--version"], check=False)
        codex_text = (result.stdout or result.stderr).strip()
        codex_version = parse_codex_version(codex_text)
    minimum = tuple(int(part) for part in manifest["codex_min_version"].split("."))
    compatible = bool(codex_version and codex_version >= minimum)
    return {
        "name": "host:codex",
        "passed": compatible if required else True,
        "required": required,
        "available": codex_path is not None,
        "compatible": compatible,
        "version": codex_text,
        "minimum": manifest["codex_min_version"],
    }


def collect_doctor(
    manifest: dict[str, Any],
    checkout_root: Path,
    venv: Path,
    lock: Path = DEFAULT_LOCK,
    only: list[str] | None = None,
    require_pi: bool = False,
    require_codex: bool = False,
    include_always: bool = True,
) -> dict[str, Any]:
    ensure_temp_root()
    python = venv_python(venv)
    chosen = selected_upstreams(
        manifest, only, include_always=include_always
    )
    paths = checkout_paths(
        manifest, checkout_root, only, include_always=include_always
    )
    checks: list[dict[str, Any]] = []
    checks.append(
        {
            "name": "runtime:repository-local-temp",
            "passed": bool(
                DEFAULT_TEMP_ROOT.parent == ROOT
                and DEFAULT_TEMP_ROOT.name == ".tmp"
                and DEFAULT_TEMP_ROOT.is_dir()
                and os.access(DEFAULT_TEMP_ROOT, os.W_OK)
            ),
            "path": ".tmp",
        }
    )

    for name, entry in chosen.items():
        branch = entry["tracking_branch"]
        state = git_state(paths[name], branch)
        passed = bool(
            state["is_git"]
            and state["branch"] == branch
            and state["upstream"] == f"origin/{branch}"
            and state["head"] == state["remote_head"]
            and normalize_repository(state["origin_url"])
            == normalize_repository(entry["repository"])
            and state["dirty"] is False
        )
        checks.append(
            {
                "name": f"checkout:{name}",
                "passed": passed,
                "expected_branch": branch,
                "expected_repository": entry["repository"],
                **state,
                "origin_url": redact_git_text(state["origin_url"]),
            }
        )

    python_version = None
    if python.is_file():
        result = run([str(python), "--version"], check=False)
        python_version = (result.stdout or result.stderr).strip()
    checks.append(
        {
            "name": "runtime:python",
            "passed": bool(
                python_version
                and python_version.startswith(f"Python {manifest['python']}.")
            ),
            "version": python_version,
        }
    )

    versions = package_versions(python) if python.is_file() else {}
    runtime_packages: list[str] = []
    if "openevolve" in chosen:
        runtime_packages.extend(("openevolve", "numpy", "scipy"))
    if "goal_plus" in chosen:
        runtime_packages.extend(("goal-plus", "fastmcp"))
    for package in runtime_packages:
        checks.append(
            {
                "name": f"package:{package}",
                "passed": bool(versions.get(package)),
                "version": versions.get(package),
            }
        )
    if "edgebench" in chosen:
        for package in ("sforge", "fastapi"):
            checks.append(
                {
                    "name": f"package:{package}",
                    "passed": bool(versions.get(package)),
                    "version": versions.get(package),
                }
            )
    if "skydiscover" in chosen:
        checks.append(
            {
                "name": "package:skydiscover",
                "passed": bool(versions.get("skydiscover")),
                "version": versions.get("skydiscover"),
            }
        )

    runtime_entrypoints: list[str] = []
    if "openevolve" in chosen:
        runtime_entrypoints.append("openevolve-run")
    if "goal_plus" in chosen:
        runtime_entrypoints.extend(
            (
                "goal-plus",
                "goal-plus-pi-tool",
                "goal-plus-pi-worker",
                "goal-plus-pi-pool",
            )
        )
    for executable in runtime_entrypoints:
        path = venv_bin(venv) / executable
        result = run([str(path), "--help"], check=False) if path.is_file() else None
        checks.append(
            {
                "name": f"entrypoint:{executable}",
                "passed": bool(result and result.returncode == 0),
            }
        )
    if "edgebench" in chosen:
        path = venv_bin(venv) / "sforge"
        result = run([str(path), "--help"], check=False) if path.is_file() else None
        checks.append(
            {
                "name": "entrypoint:sforge",
                "passed": bool(result and result.returncode == 0),
            }
        )
        checks.append(edgebench_rust_runtime_status(paths["edgebench"]))
    if "skydiscover" in chosen:
        path = venv_bin(venv) / "skydiscover-run"
        result = run([str(path), "--help"], check=False) if path.is_file() else None
        checks.append(
            {
                "name": "entrypoint:skydiscover-run",
                "passed": bool(result and result.returncode == 0),
            }
        )

    checks.append(host_codex_check(manifest, required=require_codex))
    checks.append(host_pi_check(manifest, required=require_pi))

    return {
        "schema_version": 2,
        "ok": all(item["passed"] for item in checks),
        "platform": platform.platform(),
        "python": str(python),
        "venv": str(venv),
        "checkout_root": str(checkout_root),
        "managed_checkouts": list(chosen),
        "include_always": include_always,
        "requirements_lock_sha256": sha256_file(lock) if lock.is_file() else None,
        "packages": versions,
        "checks": checks,
    }


def bootstrap_environment(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_manifest(args.manifest)
    checkout_root = args.checkout_root.expanduser().absolute()
    venv = args.venv.expanduser().absolute()
    include_always = not args.exact
    chosen = selected_upstreams(
        manifest, args.only, include_always=include_always
    )
    for name, entry in chosen.items():
        ensure_checkout(checkout_root / entry["checkout_dir"], entry)
    if "edgebench" in chosen:
        ensure_edgebench_rust_runtime(
            checkout_root / chosen["edgebench"]["checkout_dir"]
        )

    uv = shutil.which(args.uv)
    if not uv:
        raise RuntimeError("uv is required; install it first, then rerun bootstrap")
    python = venv_python(venv)
    if not python.is_file():
        venv.parent.mkdir(parents=True, exist_ok=True)
        run([uv, "venv", str(venv), "--python", manifest["python"]])
    version = run([str(python), "--version"], check=False)
    version_text = (version.stdout or version.stderr).strip()
    if not version_text.startswith(f"Python {manifest['python']}."):
        raise RuntimeError(
            f"existing venv uses {version_text}, expected Python {manifest['python']}; "
            "preserve it and choose a fresh --venv path"
        )
    if not args.skip_install:
        if not args.lock.is_file():
            raise FileNotFoundError(args.lock)
        run(
            [uv, "pip", "install", "--python", str(python), "-r", str(args.lock)],
            capture=False,
        )
        paths = checkout_paths(
            manifest,
            checkout_root,
            args.only,
            include_always=include_always,
        )
        editable_paths = [
            paths[name] for name, entry in chosen.items() if entry.get("editable") is True
        ]
        if editable_paths:
            editable_args: list[str] = []
            for path in editable_paths:
                editable_args.extend(["-e", str(path)])
            run(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--no-build-isolation",
                    "--no-deps",
                    *editable_args,
                ],
                capture=False,
            )

    payload = collect_doctor(
        manifest,
        checkout_root,
        venv,
        args.lock,
        only=args.only,
        require_pi=args.require_pi,
        require_codex=args.require_codex,
        include_always=include_always,
    )
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def bootstrap(args: argparse.Namespace) -> int:
    payload = bootstrap_environment(args)
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


def doctor(args: argparse.Namespace) -> int:
    payload = collect_doctor(
        load_manifest(args.manifest),
        args.checkout_root.expanduser().absolute(),
        args.venv.expanduser().absolute(),
        args.lock,
        only=args.only,
        require_pi=args.require_pi,
        require_codex=args.require_codex,
        include_always=not args.exact,
    )
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


def _print_update_summary(updates: dict[str, Any]) -> None:
    print("Repository updates or repairs are available:")
    for item in updates["checks"]:
        if not item["action_required"]:
            continue
        current = (item.get("current_head") or "missing")[:12]
        advertised = (item.get("advertised_head") or "unknown")[:12]
        labels = []
        if item["update_available"]:
            labels.append(f"{current} -> {advertised}")
        if item["clone_required"]:
            labels.append("clone")
        if item["repair_required"]:
            labels.append("repair tracking branch")
        print(f"  {item['name']} ({item['branch']}): {', '.join(labels)}")


def update_decision(
    *,
    action_required: bool,
    assume_yes: bool,
    interactive: bool,
    response: str | None = None,
) -> str:
    if not action_required:
        return "not-needed"
    if assume_yes:
        return "accepted"
    if not interactive:
        return "non-interactive"
    answer = response
    if answer is None:
        answer = input("Update all repositories with fast-forward only? [y/N] ")
    return "accepted" if answer.strip().lower() in {"y", "yes"} else "declined"


def environment_check(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    checkout_root = args.checkout_root.expanduser().absolute()
    venv = args.venv.expanduser().absolute()
    doctor_payload = collect_doctor(
        manifest,
        checkout_root,
        venv,
        args.lock,
        only=args.only,
    )
    updates = collect_repository_updates(
        manifest,
        checkout_root,
        only=args.only,
        include_root=True,
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "action": "environment-check",
        "ok": bool(doctor_payload["ok"] and updates["ok"]),
        "doctor": doctor_payload,
        "repository_updates": updates,
        "inventory_gated": args.inventory_gated,
        "decision": "not-needed",
        "updated": False,
    }
    if updates["query_failures"] or updates["blocked"]:
        result["decision"] = "blocked"
        print(json.dumps(result, indent=2))
        return 1
    if not updates["action_required"]:
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    _print_update_summary(updates)
    if not args.inventory_gated:
        result["decision"] = "inventory-required"
        result["next_command"] = "python3 scripts/bench.py check --environment"
        print(json.dumps(result, indent=2))
        return 1
    decision = update_decision(
        action_required=True,
        assume_yes=args.yes,
        interactive=sys.stdin.isatty(),
    )
    result["decision"] = decision
    if decision != "accepted":
        if decision == "non-interactive":
            result["next_command"] = (
                "python3 scripts/bench.py check --environment --yes"
            )
        print(json.dumps(result, indent=2))
        return 1

    root_check = next(
        item for item in updates["checks"] if item["name"] == "bench_goal_plus"
    )
    if root_check["action_required"]:
        root_entry, error = root_repository_entry()
        if root_entry is None:
            raise RuntimeError(error or "cannot resolve bench-goal-plus root")
        ensure_checkout(ROOT, root_entry)

    update_args = argparse.Namespace(
        manifest=args.manifest,
        checkout_root=args.checkout_root,
        venv=args.venv,
        lock=args.lock,
        only=args.only,
        uv="uv",
        skip_install=False,
        require_pi=False,
        require_codex=False,
    )
    doctor_payload = bootstrap_environment(update_args)
    refreshed_manifest = load_manifest(args.manifest)
    updates = collect_repository_updates(
        refreshed_manifest,
        checkout_root,
        only=args.only,
        include_root=True,
    )
    result.update(
        {
            "ok": bool(doctor_payload["ok"] and updates["ok"]),
            "doctor": doctor_payload,
            "repository_updates": updates,
            "decision": "accepted",
            "updated": True,
        }
    )
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--checkout-root", type=Path, default=DEFAULT_CHECKOUT_ROOT)
    parser.add_argument("--venv", type=Path, default=DEFAULT_VENV)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = subparsers.add_parser("bootstrap")
    bootstrap_parser.add_argument("--uv", default="uv")
    bootstrap_parser.add_argument("--skip-install", action="store_true")
    bootstrap_parser.add_argument("--require-pi", action="store_true")
    bootstrap_parser.add_argument("--require-codex", action="store_true")
    bootstrap_parser.add_argument(
        "--exact",
        action="store_true",
        help="select only explicit --only checkouts, excluding always-managed runtimes",
    )
    bootstrap_parser.add_argument(
        "--only",
        action="append",
        help="clone/check one named benchmark plus the always-managed runtime checkouts",
    )

    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--only", action="append")
    doctor_parser.add_argument(
        "--exact",
        action="store_true",
        help="check only explicit --only checkouts, excluding always-managed runtimes",
    )
    doctor_parser.add_argument("--require-pi", action="store_true")
    doctor_parser.add_argument("--require-codex", action="store_true")
    check_parser = subparsers.add_parser(
        "check",
        help="inspect advertised repository heads without provisioning assets",
    )
    check_parser.add_argument("--only", action="append")
    check_parser.add_argument(
        "--yes",
        action="store_true",
        help="accept updates after the public inventory gate",
    )
    check_parser.add_argument(
        "--inventory-gated",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def main() -> int:
    configure_temp_environment()
    args = build_parser().parse_args()
    if args.command == "bootstrap":
        return bootstrap(args)
    if args.command == "doctor":
        return doctor(args)
    return environment_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
