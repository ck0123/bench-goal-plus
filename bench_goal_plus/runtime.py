"""Host gates and repository-managed environment setup."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .errors import ContractError
from .models import TargetDefinition
from .paths import ROOT, managed_python


def command_text(command: list[str]) -> str:
    return shlex.join(command)


class CommandExecutor:
    def __init__(self, *, root: Path = ROOT) -> None:
        self.root = root

    def execute(self, commands: list[list[str]], *, dry_run: bool) -> None:
        for command in commands:
            print("+ " + command_text(command), flush=True)
            if not dry_run:
                subprocess.run(command, cwd=self.root, check=True)


class RuntimeManager:
    def validate_host(
        self,
        targets: tuple[TargetDefinition, ...],
        *,
        dry_run: bool,
        require_uv: bool = True,
    ) -> list[str]:
        problems: list[str] = []
        if sys.version_info < (3, 10):
            problems.append(
                f"Python 3.10+ is required; current interpreter is {sys.version.split()[0]}"
            )
        prerequisites = ["git", "codex"]
        if require_uv:
            prerequisites.append("uv")
        if any(item.docker.requirement != "not_required" for item in targets):
            prerequisites.append("docker")
        missing = [name for name in prerequisites if shutil.which(name) is None]
        if missing:
            problems.append("missing host prerequisite(s): " + ", ".join(missing))
        if problems and not dry_run:
            raise ContractError("; ".join(problems))
        return problems

    def setup_commands(
        self,
        targets: tuple[TargetDefinition, ...],
        *,
        skip_bootstrap: bool,
        skip_provision: bool,
        bootstrap_targets: tuple[str, ...] | None = None,
    ) -> list[list[str]]:
        commands: list[list[str]] = []
        docker_targets = [
            item for item in targets if item.docker.requirement != "not_required"
        ]
        if docker_targets:
            commands.append(["docker", "info"])
        upstreams = sorted(
            set(bootstrap_targets)
            if bootstrap_targets is not None
            else {name for target in targets for name in target.bootstrap_targets}
        )
        exact_upstreams = bootstrap_targets is not None
        if not skip_bootstrap:
            bootstrap = [sys.executable, "scripts/repro_env.py", "bootstrap"]
            if exact_upstreams:
                bootstrap.append("--exact")
            for upstream in upstreams:
                bootstrap.extend(["--only", upstream])
            commands.append(bootstrap)
        doctor = [sys.executable, "scripts/repro_env.py", "doctor"]
        if exact_upstreams:
            doctor.append("--exact")
        for upstream in upstreams:
            doctor.extend(["--only", upstream])
        commands.append(doctor)
        for target in targets:
            if (
                target.docker.owner == "adapter"
                and target.docker.provision_mode == "eager"
            ):
                adapter_commands = [
                    [
                        str(managed_python()),
                        "-m",
                        "bench_goal_plus.docker_hooks",
                        "doctor",
                        "--target",
                        target.target_id,
                    ]
                ]
                if not skip_provision:
                    adapter_commands.insert(
                        0,
                        [
                            str(managed_python()),
                            "-m",
                            "bench_goal_plus.docker_hooks",
                            "provision",
                            "--target",
                            target.target_id,
                        ],
                    )
                commands.extend(adapter_commands)
        return commands
