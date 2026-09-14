#!/usr/bin/env python3
"""Launch one Codex or Pi trajectory behind the aibench Bubblewrap boundary."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


PUBLIC_TESTS_DESTINATION = "/aibench-public-tests"
PUBLIC_TESTS_RELATIVE = Path(".bench-runtime/public-tests")
XDG_RUNTIME_DESTINATION = "/tmp/aibench-xdg-runtime"
TMP_DESTINATION = "/tmp"
SYSTEM_ROOTS = (
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/lib"),
    Path("/lib64"),
)
CONTROLLER_PATHS = (
    "adapters/portable.py",
    "bench_goal_plus",
    "bench_runtime_paths.py",
    "environment/upstreams.json",
    "experiments/aibench_coding",
    "experiments/benchmark_compare",
)


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


def _ensure_parents(command: list[str], created: set[Path], destination: Path) -> None:
    for parent in reversed(destination.absolute().parents[:-1]):
        if parent not in created:
            command.extend(["--dir", str(parent)])
            created.add(parent)


def _bind(
    command: list[str],
    created: set[Path],
    source: Path,
    destination: Path | None = None,
    *,
    writable: bool = False,
) -> None:
    destination = (destination or source).absolute()
    source = source.resolve(strict=True)
    _ensure_parents(command, created, destination)
    command.extend(
        ["--bind" if writable else "--ro-bind", str(source), str(destination)]
    )
    created.add(destination)


def _mount_system(command: list[str], created: set[Path]) -> None:
    _bind(command, created, Path("/usr"))
    for path in SYSTEM_ROOTS[1:]:
        if path.is_symlink():
            command.extend(["--symlink", os.readlink(path), str(path)])
        elif path.exists():
            _bind(command, created, path)
    _ensure_parents(command, created, Path("/etc/placeholder"))
    for value in (
        "/etc/alternatives",
        "/etc/group",
        "/etc/hosts",
        "/etc/ld.so.cache",
        "/etc/ld.so.conf",
        "/etc/ld.so.conf.d",
        "/etc/localtime",
        "/etc/nsswitch.conf",
        "/etc/passwd",
        "/etc/pki",
        "/etc/resolv.conf",
        "/etc/ssl",
    ):
        path = Path(value)
        if path.exists():
            _bind(command, created, path)


def _system_path(path: Path) -> bool:
    resolved = path.resolve(strict=True)
    return any(resolved == root or root in resolved.parents for root in SYSTEM_ROOTS)


def _lexical_system_path(path: Path) -> bool:
    absolute = path.absolute()
    return any(absolute == root or root in absolute.parents for root in SYSTEM_ROOTS)


def _runtime_mount(executable: Path) -> Path:
    resolved = executable.resolve(strict=True)
    home = Path.home().resolve(strict=True)
    for parent in resolved.parents:
        if parent in {Path("/"), home}:
            break
        if parent.name == "node_modules":
            pnpm_store = next(
                (ancestor for ancestor in parent.parents if ancestor.name == ".pnpm"),
                None,
            )
            return pnpm_store if pnpm_store is not None else parent
    if resolved.name.startswith("python") and resolved.parent.name == "bin":
        return resolved.parent.parent
    return resolved


def _reject_broad_runtime_mount(path: Path, protected_roots: tuple[Path, ...]) -> None:
    if path == Path("/"):
        raise RuntimeError("aibench runtime mount must not expose the host root")
    for protected in protected_roots:
        protected = protected.resolve(strict=True)
        if path == protected or path in protected.parents:
            raise RuntimeError(
                f"aibench runtime mount is broader than protected root: {protected}"
            )


def _bind_runtime(
    command: list[str],
    created: set[Path],
    executable: Path,
    protected_roots: tuple[Path, ...],
) -> None:
    runtime = _runtime_mount(executable)
    _reject_broad_runtime_mount(runtime, protected_roots)
    if not _system_path(runtime):
        _bind(command, created, runtime)
    resolved = executable.resolve(strict=True)
    alias = executable.absolute()
    if (
        alias != resolved
        and runtime not in alias.parents
        and not _lexical_system_path(alias)
    ):
        _ensure_parents(command, created, alias)
        command.extend(["--symlink", str(resolved), str(alias)])
        created.add(alias)


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
    controller_root = _required_path("AIBENCH_CONTROLLER_ROOT", directory=True)
    agent_runtime = _required_path("AIBENCH_AGENT_RUNTIME", directory=True)
    grader_runtime = _required_path("AIBENCH_GRADER_RUNTIME", directory=True)
    cell_root = _required_path("AIBENCH_CELL_ROOT", directory=True)
    workspace = Path.cwd().absolute()
    if cell_root not in workspace.parents:
        raise RuntimeError("agent cwd is outside the prepared cell")
    protected_roots = (Path.home(), controller_root, cell_root)
    public_tests = workspace / PUBLIC_TESTS_RELATIVE
    if public_tests.is_symlink() or not public_tests.is_dir():
        raise RuntimeError("aibench public-test bundle is unavailable")
    public_tests = public_tests.resolve(strict=True)
    try:
        public_tests.relative_to(workspace.resolve(strict=True))
    except ValueError as error:
        raise RuntimeError("aibench public-test bundle escapes the workspace") from error

    command = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--share-net",
        "--unshare-user",
        "--cap-drop",
        "ALL",
        "--hostname",
        "aibench-agent",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        TMP_DESTINATION,
        "--tmpfs",
        "/run",
    ]
    created: set[Path] = {
        Path("/dev"),
        Path("/proc"),
        Path(TMP_DESTINATION),
        Path("/run"),
    }
    _mount_system(command, created)
    _bind_runtime(command, created, real_binary, protected_roots)
    node = shutil.which("node", path=os.environ.get("PATH"))
    if node is None:
        raise RuntimeError("aibench agent isolation requires node")
    _bind_runtime(command, created, Path(node), protected_roots)
    _bind(command, created, agent_runtime)
    agent_python = agent_runtime / "bin/python"
    if agent_python.exists():
        _bind_runtime(command, created, agent_python, protected_roots)
    _bind(command, created, grader_runtime)
    grader_python = grader_runtime / "bin/python"
    if grader_python.exists():
        _bind_runtime(command, created, grader_python, protected_roots)
    for relative in CONTROLLER_PATHS:
        _bind(command, created, controller_root / relative)
    if method.startswith("goal-plus-"):
        goal_plus_runtime = _required_path(
            "AIBENCH_GOAL_PLUS_RUNTIME", directory=True
        )
        _bind(command, created, goal_plus_runtime)
        _bind(command, created, cell_root, writable=True)
        writable_root = cell_root / "controller-runtime" / "agent-home"
    else:
        lane_dir = cell_root / "lanes" / workspace.name
        for path in (workspace, lane_dir):
            path.mkdir(parents=True, exist_ok=True)
            _bind(command, created, path, writable=True)
        writable_root = lane_dir / "agent-home"
    command.extend(
        [
            "--ro-bind",
            str(public_tests),
            str(public_tests),
            "--ro-bind",
            str(public_tests),
            PUBLIC_TESTS_DESTINATION,
        ]
    )
    writable_root.mkdir(parents=True, exist_ok=True)
    temporary = writable_root / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    if method == "goal-plus-pi":
        xdg_runtime = writable_root / "xdg-runtime"
        if xdg_runtime.is_symlink():
            raise RuntimeError("aibench XDG runtime directory must not be a symlink")
        xdg_runtime.mkdir(parents=True, exist_ok=True)
        xdg_runtime.chmod(0o700)
        command.extend(["--bind", str(xdg_runtime), XDG_RUNTIME_DESTINATION])
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
            "AIBENCH_PUBLIC_TESTS",
            PUBLIC_TESTS_DESTINATION,
        ]
    )
    if method == "goal-plus-pi":
        command.extend(["--setenv", "XDG_RUNTIME_DIR", XDG_RUNTIME_DESTINATION])
    command.extend(
        [
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
            "--",
            str(real_binary.resolve(strict=True)),
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
