#!/usr/bin/env python3
"""Launch one Codex or Pi trajectory behind the aibench Bubblewrap boundary."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _required_path(name: str, *, directory: bool = False) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing sandbox environment: {name}")
    path = Path(value).expanduser().absolute()
    if directory and not path.is_dir():
        raise FileNotFoundError(path)
    if not directory and not path.is_file():
        raise FileNotFoundError(path)
    return path


def _mkdir_chain(
    command: list[str], created: set[Path], root: Path, destination: Path
) -> None:
    relative = destination.relative_to(root)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current not in created:
            command.extend(["--dir", str(current)])
            created.add(current)


def build_command(arguments: list[str]) -> list[str]:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise RuntimeError("aibench agent isolation requires Linux Bubblewrap")
    role = os.environ.get("AIBENCH_AGENT_ROLE")
    if role not in {"codex", "pi"}:
        raise RuntimeError("AIBENCH_AGENT_ROLE must be codex or pi")
    method = os.environ.get("AIBENCH_METHOD")
    if method not in {
        "plain-codex",
        "plain-pi",
        "goal-plus-codex",
        "goal-plus-pi",
    }:
        raise RuntimeError("AIBENCH_METHOD is invalid")
    real_binary = _required_path(f"AIBENCH_REAL_{role.upper()}_BIN")
    hidden_checkout = _required_path("AIBENCH_HIDDEN_CHECKOUT", directory=True)
    cell_root = _required_path("AIBENCH_CELL_ROOT", directory=True)
    cells_root = cell_root.parent
    workspace = Path.cwd().absolute()
    if cell_root not in workspace.parents:
        raise RuntimeError("agent cwd is outside the prepared cell")

    command = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/",
        "/",
        "--dev-bind",
        "/dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        str(hidden_checkout),
        "--tmpfs",
        str(cells_root),
    ]
    created: set[Path] = set()
    if method.startswith("goal-plus-"):
        _mkdir_chain(command, created, cells_root, cell_root)
        command.extend(["--bind", str(cell_root), str(cell_root)])
        writable_root = cell_root / "controller-runtime" / "agent-home"
    else:
        lane_dir = cell_root / "lanes" / workspace.name
        for path in (workspace, lane_dir):
            path.mkdir(parents=True, exist_ok=True)
            _mkdir_chain(command, created, cells_root, path)
            command.extend(["--bind", str(path), str(path)])
        writable_root = lane_dir / "agent-home"
    writable_root.mkdir(parents=True, exist_ok=True)
    temporary = writable_root / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    command.extend(
        [
            "--chdir",
            str(workspace),
            "--setenv",
            "HOME",
            str(writable_root),
            "--setenv",
            "TMPDIR",
            str(temporary),
            "--setenv",
            "TMP",
            str(temporary),
            "--setenv",
            "TEMP",
            str(temporary),
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
            "--",
            str(real_binary),
            *arguments,
        ]
    )
    return command


def main() -> int:
    arguments = sys.argv[1:]
    role = os.environ.get("AIBENCH_AGENT_ROLE")
    if arguments == ["--version"] and role in {"codex", "pi"}:
        binary = _required_path(f"AIBENCH_REAL_{role.upper()}_BIN")
        os.execve(binary, [str(binary), *arguments], os.environ)
    command = build_command(arguments)
    os.execvpe(command[0], command, os.environ)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
