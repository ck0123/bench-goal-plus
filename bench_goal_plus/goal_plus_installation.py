"""Run-local Goal Plus installation through the plugin's public installer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


GOAL_PLUS_CONTROLLER_CAPABILITIES = (
    "goal_plus.controller_exact_selection.v1",
    "goal_plus.controller_owned_closeout.v1",
)


def installation_environment(run_dir: Path) -> dict[str, str]:
    return {
        "GOAL_PLUS_INSTALL_HOME": str(run_dir / "controller-runtime/goal-plus-install"),
        "CODEX_HOME": str(run_dir / "controller-runtime/codex-home"),
        "PI_CODING_AGENT_DIR": str(run_dir / "pi-home"),
    }


def prepare_bootstrap_python(run_dir: Path, environment: dict[str, str]) -> None:
    """Reuse a verified uv installation without copying a venv or downloading Python."""
    uv = environment.get("GOAL_PLUS_UV") or shutil.which("uv", path=environment.get("PATH"))
    if not uv:
        raise RuntimeError("Goal Plus setup requires uv and an installed managed Python 3.12")
    uv = str(Path(uv).resolve())
    environment["GOAL_PLUS_UV"] = uv
    probe_environment = {
        key: value for key, value in environment.items()
        if not key.startswith("UV_") and key not in (
            "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX",
        )
    }
    destination = Path(environment["GOAL_PLUS_INSTALL_HOME"]) / "bootstrap/python"

    def find_python(store: Path) -> str | None:
        result = subprocess.run(
            [uv, "python", "find", "--managed-python", "--no-python-downloads",
             "--no-project", "--offline", "--no-config", "3.12"],
            env={**probe_environment, "UV_PYTHON_INSTALL_DIR": str(store)},
            cwd=run_dir, capture_output=True, text=True, timeout=15, check=False,
        )
        executable = result.stdout.strip()
        return executable if result.returncode == 0 and Path(executable).is_file() else None

    stores = []
    if destination.exists() or destination.is_symlink():
        stores.append(destination)
    # Bench's locked venv can use a uv store outside the current user's default.
    base = Path(sys.base_prefix).resolve()
    if base.name.startswith("cpython-3.12"):
        stores.append(base.parent)
    selected = None
    for store in stores:
        executable = find_python(store)
        if executable:
            selected = (store, executable)
            break
    if selected is None:
        result = subprocess.run(
            [uv, "python", "dir", "--no-config"], env=probe_environment,
            cwd=run_dir, capture_output=True, text=True, timeout=15, check=True,
        )
        store = Path(result.stdout.strip())
        executable = find_python(store)
        if executable:
            selected = (store, executable)
    if selected is None:
        raise RuntimeError(
            "No local uv-managed Python 3.12 is available. Install it during environment "
            "setup (uv python install 3.12), then retry with a new campaign. "
            "Campaign preparation will not download Python."
        )
    store, executable = selected
    if store != destination:
        if destination.exists() or destination.is_symlink():
            raise RuntimeError(
                f"Existing bootstrap Python directory is unusable: {destination}; "
                "preserve this campaign and prepare a new one"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(store.resolve(), target_is_directory=True)
    (run_dir / "goal-plus-bootstrap-python.json").write_text(json.dumps({
        "store": str(store.resolve()), "python": str(Path(executable).resolve()),
        "downloaded": False,
    }, indent=2) + "\n")


def install_goal_plus(source: Path, workspace: Path, agent_harness: str) -> None:
    if agent_harness not in {"codex", "pi"}:
        raise ValueError(f"unsupported Agent harness: {agent_harness}")
    run_dir = workspace.parent
    environment = {**os.environ, **installation_environment(run_dir)}
    prepare_bootstrap_python(run_dir, environment)
    installer = str(source / "install.sh")
    with (run_dir / "goal-plus-install.log").open("w") as output:
        subprocess.run(
            [installer, f"--{agent_harness}", "--yes"], cwd=workspace,
            env=environment, stdout=output, stderr=subprocess.STDOUT, check=True,
        )
    result = subprocess.run(
        [installer, "--runtime-info"], cwd=workspace, env=environment,
        capture_output=True, text=True, check=True,
    )
    receipt = json.loads(result.stdout)
    if not Path(receipt["python"]).is_file() or not Path(receipt["package"]).is_dir():
        raise RuntimeError("Goal Plus installer returned an incomplete runtime")
    (run_dir / "goal-plus-runtime.json").write_text(json.dumps(receipt, indent=2) + "\n")


def require_goal_plus_runtime_capabilities(
    run_dir: Path, required: tuple[str, ...] = GOAL_PLUS_CONTROLLER_CAPABILITIES
) -> dict:
    """Fail before launch when the installed runtime cannot honor the controller contract."""
    receipt_path = run_dir / "goal-plus-runtime.json"
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"Goal Plus runtime receipt is unavailable or invalid: {receipt_path}"
        ) from error
    if not isinstance(receipt, dict):
        raise RuntimeError(f"Goal Plus runtime receipt is not an object: {receipt_path}")
    capabilities = set(receipt.get("capabilities") or [])
    missing = sorted(set(required) - capabilities)
    if missing:
        raise RuntimeError(
            "Goal Plus runtime lacks required capabilities: " + ", ".join(missing)
        )
    return receipt


def bind_goal_plus_environment(environment: dict[str, str], run_dir: Path) -> None:
    receipt = json.loads((run_dir / "goal-plus-runtime.json").read_text())
    environment.update(installation_environment(run_dir))
    environment["GOAL_PLUS_PYTHON"] = receipt["python"]
    environment["PATH"] = str(Path(receipt["python"]).parent) + os.pathsep + environment["PATH"]
