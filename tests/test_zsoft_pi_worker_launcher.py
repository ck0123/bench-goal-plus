from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

try:
    import pytest
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(
        "pytest is required for Bubblewrap integration tests"
    ) from exc

from experiments.benchmark_compare import pi_worker_launcher
from experiments.benchmark_compare.pi_worker_launcher import (
    REAL_PI_BIN_ENV,
    SANDBOX_POLICY_ENV,
    TOOL_SOCKET_ENV,
    BubblewrapWorker,
    LaunchContext,
    SandboxPolicy,
    WorkerToolProxy,
    _OPAQUE_RESULTS_LEDGER,
    _runtime_root,
    _sandbox_environment,
    _shim_worker_launch,
    run_pi_shim,
)


def _context(workspace: Path) -> LaunchContext:
    return LaunchContext(
        run_id="run_1",
        candidate_id="c001",
        agent_session_id="agent_1",
        workspace=workspace,
    )


def _policy(
    *paths: str,
    writable: tuple[str, ...] = (),
    pass_env: tuple[str, ...] = (),
    read_only_host: tuple[str, ...] = (),
    evaluation_mode: str = "visible",
) -> SandboxPolicy:
    return SandboxPolicy(
        engine="bubblewrap",
        workspace_access="read_only",
        read_only_workspace_paths=paths,
        writable_workspace_paths=writable,
        pass_env=pass_env,
        read_only_host_paths=read_only_host,
        evaluation_mode=evaluation_mode,
    )


def _worker_command(
    script: str,
    *,
    session_root: Path,
    extension: Path,
    session_id: str = "session_1",
) -> list[str]:
    return [
        sys.executable,
        "-c",
        script,
        "--session-dir",
        str(session_root),
        "--session-id",
        session_id,
        "-e",
        str(extension),
    ]


def _init_git(workspace: Path) -> None:
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.name", "Test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.email", "test@example.com"],
        check=True,
    )
    (workspace / "results.tsv").write_text(
        "iteration\tformat_valid\n1\tprivate-score-sentinel\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(workspace), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-q", "-m", "fixture"],
        check=True,
    )


def _candidate_paths(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / ".gp"
    workspace = root / "runs" / "run_1" / "workspace" / "c001"
    workspace.parent.mkdir(parents=True, exist_ok=True)
    return root, workspace


def _fixture_extension(tmp_path: Path) -> Path:
    extension_dir = tmp_path / "pi-extension"
    extension_dir.mkdir()
    extension = extension_dir / "goal-plus.ts"
    extension.write_text(
        "export default function fixture() {}\n",
        encoding="utf-8",
    )
    return extension


def test_worker_sandbox_routes_goal_plus_tools_through_host_proxy(
    tmp_path: Path,
) -> None:
    environment = {
        "GOAL_PLUS_PI_DEV_ROOT": str(tmp_path / "goal-plus"),
        "GOAL_PLUS_PYTHON": sys.executable,
    }

    sandbox = _sandbox_environment(
        environment,
        policy=_policy(),
        pi_runtime=Path("/usr"),
        runtime_root=tmp_path / ".gp",
        socket_path=tmp_path / "proxy.sock",
        private_git_admin=None,
    )

    assert sandbox["GOAL_PLUS_PI_DEV_ROOT"] == environment["GOAL_PLUS_PI_DEV_ROOT"]
    assert sandbox["GOAL_PLUS_PYTHON"] == "/opt/bench-goal-plus/bin/goal-plus-pi-tool"


def _write_session_record(
    root: Path,
    workspace: Path,
    *,
    session_id: str = "agent_1",
    run_id: str = "run_1",
    candidate_id: str = "c001",
    host: str = "pi-rpc",
) -> Path:
    path = root / "runs" / run_id / "agent_sessions" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "agent_session_id": session_id,
                "run_id": run_id,
                "candidate_id": candidate_id,
                "host": host,
                "workspace": str(workspace.resolve()),
                "launch": {"role": "worker"},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_launch_context_and_policy_are_strict() -> None:
    context = LaunchContext.from_json(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "run_1",
                "candidate_id": "c001",
                "agent_session_id": "agent_1",
                "workspace": str(Path.cwd()),
            }
        )
    )
    assert context.candidate_id == "c001"

    with pytest.raises(ValueError, match="fields do not match"):
        LaunchContext.from_json(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "run_1",
                    "candidate_id": "c001",
                    "agent_session_id": "agent_1",
                    "workspace": str(Path.cwd()),
                    "api_key": "forbidden",
                }
            )
        )

    policy = SandboxPolicy.from_environment(
        {
            SANDBOX_POLICY_ENV: json.dumps(
                {
                    "engine": "bubblewrap",
                    "workspace_access": "read_only",
                    "read_only_workspace_paths": ["source"],
                    "read_only_host_paths": ["/public-tests"],
                    "writable_workspace_paths": ["submission"],
                    "pass_env": ["OPENAI_API_KEY"],
                }
            )
        }
    )
    assert policy.read_only_workspace_paths == ("source",)
    assert policy.read_only_host_paths == ("/public-tests",)
    assert policy.writable_workspace_paths == ("submission",)

    with pytest.raises(ValueError, match="absolute non-root"):
        SandboxPolicy.from_environment(
            {
                SANDBOX_POLICY_ENV: json.dumps(
                    {
                        "engine": "bubblewrap",
                        "workspace_access": "read_only",
                        "read_only_host_paths": ["public-tests"],
                        "read_only_workspace_paths": [],
                        "writable_workspace_paths": [],
                    }
                )
            }
        )

    with pytest.raises(ValueError, match="without '..'"):
        SandboxPolicy.from_environment(
            {
                SANDBOX_POLICY_ENV: json.dumps(
                    {
                        "engine": "bubblewrap",
                        "workspace_access": "read_only",
                        "read_only_workspace_paths": ["../cases"],
                        "writable_workspace_paths": [],
                    }
                )
            }
        )

    with pytest.raises(ValueError, match="cannot override reserved variables"):
        SandboxPolicy.from_environment(
            {
                SANDBOX_POLICY_ENV: json.dumps(
                    {
                        "engine": "bubblewrap",
                        "workspace_access": "read_only",
                        "read_only_workspace_paths": [],
                        "writable_workspace_paths": [],
                        "pass_env": ["GIT_DIR"],
                    }
                )
            }
        )

    with pytest.raises(ValueError, match="overlap"):
        SandboxPolicy.from_environment(
            {
                SANDBOX_POLICY_ENV: json.dumps(
                    {
                        "engine": "bubblewrap",
                        "workspace_access": "read_only",
                        "read_only_workspace_paths": ["source"],
                        "writable_workspace_paths": ["source/generated"],
                    }
                )
            }
        )


def test_runtime_root_is_bound_to_run_candidate_and_workspace(tmp_path: Path) -> None:
    root, workspace = _candidate_paths(tmp_path)
    workspace.mkdir()
    context = _context(workspace.resolve())

    assert _runtime_root({"GOAL_PLUS_ROOT": str(root)}, context) == root.resolve()

    with pytest.raises(ValueError, match="does not match GOAL_PLUS_ROOT identity"):
        _runtime_root(
            {"GOAL_PLUS_ROOT": str(root)},
            LaunchContext(
                run_id="run_1",
                candidate_id="c002",
                agent_session_id="agent_1",
                workspace=workspace.resolve(),
            ),
        )


def test_main_tool_shim_reserves_host_closeout() -> None:
    shim = (
        Path(__file__).resolve().parents[1]
        / "experiments/benchmark_compare/main-bin/goal-plus-pi-tool"
    )
    environment = {**os.environ, "BENCH_GOAL_PLUS_CONTROLLER_ONLY_CLOSEOUT": "1"}
    for tool in (
        "goal_plus_record_search_result",
        "goal_plus_set_status",
        "search_promote",
        "search_report",
        "search_select",
    ):
        completed = subprocess.run(
            [sys.executable, str(shim), "--root", ".gp", "--args-json", "{}", tool],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 1
        assert json.loads(completed.stderr)["tool"] == tool


def test_bench_pi_shim_derives_a_trusted_worker_context(tmp_path: Path) -> None:
    root, workspace = _candidate_paths(tmp_path)
    workspace.mkdir()
    _write_session_record(root, workspace)
    environment = {
        "GOAL_PLUS_PI_ROLE": "worker",
        "GOAL_PLUS_ROOT": str(root),
        REAL_PI_BIN_ENV: str(Path(sys.executable).resolve()),
        SANDBOX_POLICY_ENV: json.dumps(
            {
                "engine": "bubblewrap",
                "evaluation_mode": "blind",
                "workspace_access": "read_only",
                "read_only_workspace_paths": [],
                "writable_workspace_paths": [],
                "pass_env": [],
            }
        ),
    }
    command = [
        "--model",
        "provider/model",
        "--mode",
        "rpc",
        "--session-id",
        "agent_1",
    ]

    planned = _shim_worker_launch(command, environment, workspace)

    assert planned is not None
    context, wrapped = planned
    assert context == _context(workspace.resolve())
    assert wrapped[:1] == [str(Path(sys.executable).resolve())]
    assert wrapped[1 : 1 + len(command)] == command
    assert wrapped[-2] == "--append-system-prompt"
    assert "official metric never enter" in wrapped[-1]
    assert "GOAL_PLUS_PI_WORKER_LAUNCHER" not in environment


@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        ("host", "does not match"),
        ("candidate", "does not match"),
        ("session", "no trusted"),
        ("cwd", "outside"),
        ("session_chars", "safe identity"),
    ),
)
def test_bench_pi_shim_rejects_tampered_worker_identity(
    tmp_path: Path,
    mutation: str,
    error: str,
) -> None:
    root, workspace = _candidate_paths(tmp_path)
    workspace.mkdir()
    host = "codex" if mutation == "host" else "pi-rpc"
    candidate = "c002" if mutation == "candidate" else "c001"
    _write_session_record(
        root,
        workspace,
        host=host,
        candidate_id=candidate,
    )
    session_id = (
        "agent_missing"
        if mutation == "session"
        else "../agent_1"
        if mutation == "session_chars"
        else "agent_1"
    )
    cwd = tmp_path / "outside" if mutation == "cwd" else workspace
    if mutation == "cwd":
        cwd.mkdir()
    environment = {
        "GOAL_PLUS_PI_ROLE": "worker",
        "GOAL_PLUS_ROOT": str(root),
        REAL_PI_BIN_ENV: str(Path(sys.executable).resolve()),
    }

    with pytest.raises((RuntimeError, ValueError), match=error):
        _shim_worker_launch(
            ["--mode", "rpc", "--session-id", session_id],
            environment,
            cwd,
        )


def test_bench_pi_shim_rejects_symlinked_root_and_non_rpc_worker(
    tmp_path: Path,
) -> None:
    root, workspace = _candidate_paths(tmp_path)
    workspace.mkdir()
    _write_session_record(root, workspace)
    root_link = tmp_path / "runtime-link"
    root_link.symlink_to(root, target_is_directory=True)
    environment = {
        "GOAL_PLUS_PI_ROLE": "worker",
        "GOAL_PLUS_ROOT": str(root_link),
        REAL_PI_BIN_ENV: str(Path(sys.executable).resolve()),
    }

    with pytest.raises(ValueError, match="must not be a symlink"):
        _shim_worker_launch(
            ["--mode", "rpc", "--session-id", "agent_1"],
            environment,
            workspace,
        )
    with pytest.raises(RuntimeError, match="must use RPC mode"):
        _shim_worker_launch(
            ["--mode", "json", "--session-id", "agent_1"],
            {**environment, "GOAL_PLUS_ROOT": str(root)},
            workspace,
        )


def test_bench_pi_shim_passes_non_worker_pi_to_the_real_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def fake_execve(path: str, argv: list[str], environment: dict[str, str]) -> None:
        observed.update(path=path, argv=argv, environment=environment)
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(os, "execve", fake_execve)
    environment = {
        "GOAL_PLUS_PI_ROLE": "main",
        REAL_PI_BIN_ENV: str(Path(sys.executable).resolve()),
    }
    with pytest.raises(RuntimeError, match="exec intercepted"):
        run_pi_shim(["--version"], environment, tmp_path)

    real_pi = str(Path(sys.executable).resolve())
    assert observed["path"] == real_pi
    assert observed["argv"] == [real_pi, "--version"]
    assert observed["environment"] == environment


def test_extension_bundle_must_not_overlap_runtime_root(tmp_path: Path) -> None:
    if not shutil.which("bwrap"):
        pytest.skip("bwrap is unavailable")
    root, workspace = _candidate_paths(tmp_path)
    workspace.mkdir()
    _init_git(workspace)
    session_root = root / "host-sessions" / "pi"
    session_root.mkdir(parents=True)
    extension_dir = root / "pi-extension"
    extension_dir.mkdir()
    extension = extension_dir / "goal-plus.ts"
    extension.write_text("// fixture\n", encoding="utf-8")
    pi_home = tmp_path / "pi-home"
    pi_home.mkdir()
    worker = BubblewrapWorker(
        context=_context(workspace),
        policy=_policy(),
        command=_worker_command(
            "pass",
            session_root=session_root,
            extension=extension,
        ),
        environment={
            **os.environ,
            "GOAL_PLUS_ROOT": str(root),
            "PI_CODING_AGENT_DIR": str(pi_home),
        },
    )

    with pytest.raises(ValueError, match="disjoint from GOAL_PLUS_ROOT"):
        worker.prepare()


def test_host_tool_proxy_enforces_worker_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, str, dict[str, Any]]] = []

    def fake_call(
        root: Path,
        tool: str,
        args: dict[str, Any],
        _environment: dict[str, str],
    ) -> dict[str, Any]:
        calls.append((root, tool, args))
        return {"workspace": "/candidate"}

    monkeypatch.setattr(pi_worker_launcher, "_run_host_tool", fake_call)
    proxy = WorkerToolProxy(
        root=tmp_path / ".gp",
        context=_context(tmp_path),
        socket_dir=tmp_path / "proxy",
        evaluation_mode="visible",
    )

    response = proxy.dispatch(
        {
            "tool": "search_get_agent_context",
            "args": {"agent_session_id": "agent_1"},
        }
    )
    assert response == {"ok": True, "result": {"workspace": "/candidate"}}
    assert calls[0][1] == "search_get_agent_context"

    with pytest.raises(PermissionError, match="bound agent_session_id"):
        proxy.dispatch(
            {
                "tool": "search_get_global_evidence",
                "args": {"agent_session_id": "agent_other"},
            }
        )
    with pytest.raises(PermissionError, match="different candidate_id"):
        proxy.dispatch(
            {
                "tool": "search_list_iterations",
                "args": {"run_id": "run_1", "candidate_id": "c002"},
            }
        )
    with pytest.raises(PermissionError, match="does not allow"):
        proxy.dispatch({"tool": "search_select", "args": {"run_id": "run_1"}})


def test_blind_tool_proxy_exposes_only_frozen_context_and_receipt_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = WorkerToolProxy(
        root=tmp_path / ".gp",
        context=_context(tmp_path),
        socket_dir=tmp_path / "proxy",
        evaluation_mode="blind",
    )
    context_request = {
        "tool": "search_get_agent_context",
        "args": {"agent_session_id": "agent_1"},
    }
    context_result = {
        "agent_session_id": "agent_1",
        "run_id": "run_1",
        "candidate_id": "c001",
        "workspace": str(tmp_path),
        "evaluation_mode": "blind",
        "metric_name": "format_valid",
        "metric_direction": "maximize",
        "candidate_task": {
            "run_id": "run_1",
            "candidate_id": "c001",
            "workspace": str(tmp_path),
            "hypothesis": "independent audit",
            "allowed_files": ["submission"],
            "denied_files": ["task.json"],
            "instructions": ["Commit the artifact."],
            "expected_artifacts": ["submission"],
        },
        "latest_result": {"score": 1.0, "process_passed": True},
        "recent_iterations": [{"summary": "private annotation"}],
        "best_iteration": {"score": 1.0},
        "results_tsv": "/private/results.tsv",
        "resume": {"latest_handoff": {"summary": "private handoff"}},
    }
    monkeypatch.setattr(
        pi_worker_launcher, "_run_host_tool", lambda *_args: context_result
    )
    response = proxy.dispatch(context_request)
    assert response["ok"] is True
    assert response["result"] == {
        "agent_session_id": "agent_1",
        "run_id": "run_1",
        "candidate_id": "c001",
        "workspace": str(tmp_path),
        "metric_name": "format_valid",
        "metric_direction": "maximize",
        "candidate_task": {
            "run_id": "run_1",
            "candidate_id": "c001",
            "workspace": str(tmp_path),
            "allowed_files": ["submission"],
            "denied_files": ["task.json"],
            "expected_artifacts": ["submission"],
        },
    }
    serialized = json.dumps(response)
    for hidden in (
        "latest_result",
        "recent_iterations",
        "results.tsv",
        "private annotation",
        "independent audit",
        "Commit the artifact",
    ):
        assert hidden not in serialized

    forbidden_keys = (
        "score",
        "metrics",
        "process_passed",
        "process_passed_reason",
        "disposition",
        "bestCandidateId",
        "log_path",
        "failure_class",
        "globalEvidence",
        "global_evidence_stats",
        "evidence_summary",
        "annotation",
        "view",
        "raw_error",
    )
    for key in forbidden_keys:
        sentinel = f"secret-{key}"

        def forbidden_result(
            _root: Path,
            _tool: str,
            _args: dict[str, Any],
            _environment: dict[str, str],
            *,
            response_key: str = key,
            response_value: str = sentinel,
        ) -> dict[str, Any]:
            return {**context_result, response_key: response_value}

        monkeypatch.setattr(pi_worker_launcher, "_run_host_tool", forbidden_result)
        response = proxy.dispatch(context_request)
        assert response == {
            "ok": False,
            "error": "worker tool response is unavailable",
        }
        assert sentinel not in json.dumps(response)

    def raises_raw_error(
        _root: Path,
        _tool: str,
        _args: dict[str, Any],
        _environment: dict[str, str],
    ) -> dict[str, Any]:
        raise RuntimeError("secret-host-exception")

    monkeypatch.setattr(pi_worker_launcher, "_run_host_tool", raises_raw_error)
    response = proxy.dispatch(context_request)
    assert response == {
        "ok": False,
        "error": "worker tool response is unavailable",
    }
    assert "secret-host-exception" not in json.dumps(response)



def test_blind_tool_proxy_reduces_verifier_and_iteration_results_to_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = WorkerToolProxy(
        root=tmp_path / ".gp",
        context=_context(tmp_path),
        socket_dir=tmp_path / "proxy",
        evaluation_mode="blind",
    )
    verifier_request = {
        "tool": "search_run_verifier",
        "args": {
            "run_id": "run_1",
            "candidate_id": "c001",
            "agent_session_id": "agent_1",
            "hypothesis": "final public format check",
        },
    }
    monkeypatch.setattr(
        pi_worker_launcher,
        "_run_host_tool",
        lambda *_args: {
            "run_id": "run_1",
            "candidate_id": "c001",
            "agent_session_id": "agent_1",
            "iteration": 1,
            "commit": "a" * 40,
            "state": "recorded",
        },
    )
    verified = proxy.dispatch(verifier_request)
    assert verified == {
        "ok": True,
        "result": {
            "run_id": "run_1",
            "candidate_id": "c001",
            "agent_session_id": "agent_1",
            "iteration": 1,
            "commit": "a" * 40,
            "state": "recorded",
        },
    }
    assert "passed" not in json.dumps(verified)
    assert "score" not in json.dumps(verified)
    monkeypatch.setattr(
        pi_worker_launcher,
        "_run_host_tool",
        lambda *_args: {
            "run_id": "run_1",
            "candidate_id": "c001",
            "agent_session_id": "agent_1",
            "iteration": 1,
            "commit": "a" * 40,
            "state": "passed",
        },
    )
    assert proxy.dispatch(verifier_request) == {
        "ok": False,
        "error": "worker tool response is unavailable",
    }

    legacy_private_marker = "legacy-private-score-and-summary"
    monkeypatch.setattr(
        pi_worker_launcher,
        "_run_host_tool",
        lambda *_args: {
            "run_id": "run_1",
            "candidate_id": "c001",
            "parent_id": None,
            "validity_passed": True,
            "process_passed": True,
            "promotion_passed": None,
            "aggregate_score": 0.75,
            "verifier_results": [
                {
                    "metrics": {"private": legacy_private_marker},
                    "summary": legacy_private_marker,
                }
            ],
        },
    )
    legacy_receipt = proxy.dispatch(verifier_request)
    assert legacy_receipt == {
        "ok": True,
        "result": {
            "run_id": "run_1",
            "candidate_id": "c001",
            "recorded": True,
        },
    }
    assert legacy_private_marker not in json.dumps(legacy_receipt)

    def raises_private_verifier_error(*_args: Any) -> dict[str, Any]:
        raise RuntimeError("private-verifier-exception")

    monkeypatch.setattr(
        pi_worker_launcher, "_run_host_tool", raises_private_verifier_error
    )
    verifier_error = proxy.dispatch(verifier_request)
    assert verifier_error == {
        "ok": False,
        "error": "worker tool response is unavailable",
    }
    assert "private-verifier-exception" not in json.dumps(verifier_error)

    iteration_request = {
        "tool": "search_list_iterations",
        "args": {
            "run_id": "run_1",
            "candidate_id": "c001",
            "agent_session_id": "agent_1",
        },
    }
    monkeypatch.setattr(
        pi_worker_launcher,
        "_run_host_tool",
        lambda *_args: [
            {
                "run_id": "run_1",
                "candidate_id": "c001",
                "agent_session_id": "agent_1",
                "iteration": 1,
                "commit": "a" * 40,
                "state": "recorded",
            }
        ],
    )
    iterations = proxy.dispatch(iteration_request)
    assert iterations == {
        "ok": True,
        "result": [
            {
                "run_id": "run_1",
                "candidate_id": "c001",
                "agent_session_id": "agent_1",
                "iteration": 1,
                "commit": "a" * 40,
                "state": "recorded",
            }
        ],
    }
    assert "summary" not in json.dumps(iterations)

    monkeypatch.setattr(
        pi_worker_launcher,
        "_run_host_tool",
        lambda *_args: [
            {
                "run_id": "run_1",
                "candidate_id": "c001",
                "agent_session_id": "agent_other",
                "iteration": 1,
                "commit": "a" * 40,
                "state": "recorded",
            }
        ],
    )
    assert proxy.dispatch(iteration_request) == {
        "ok": False,
        "error": "worker tool response is unavailable",
    }

    legacy_iteration_marker = "legacy-private-iteration"
    monkeypatch.setattr(
        pi_worker_launcher,
        "_run_host_tool",
        lambda *_args: [
            {
                "iteration": 2,
                "agent_session_id": "agent_legacy",
                "git_head": "b" * 40,
                "score": 0.95,
                "process_passed": True,
                "summary": legacy_iteration_marker,
                "metrics": {"private": legacy_iteration_marker},
            }
        ],
    )
    legacy_iterations = proxy.dispatch(iteration_request)
    assert legacy_iterations == {
        "ok": True,
        "result": [{"iteration": 2, "recorded": True}],
    }
    assert legacy_iteration_marker not in json.dumps(legacy_iterations)

    wrong_session_request = {
        "tool": "search_list_iterations",
        "args": {
            "run_id": "run_1",
            "candidate_id": "c001",
            "agent_session_id": "agent_other",
        },
    }
    with pytest.raises(PermissionError, match="bound agent_session_id"):
        proxy.dispatch(wrong_session_request)

    evidence_request = {
        "tool": "search_get_global_evidence",
        "args": {"agent_session_id": "agent_1"},
    }
    called = False

    def must_not_call(*_args: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(pi_worker_launcher, "_run_host_tool", must_not_call)
    assert proxy.dispatch(evidence_request) == {
        "ok": False,
        "error": "worker tool response is unavailable",
    }
    assert called is True

    called = False
    monkeypatch.setattr(pi_worker_launcher, "_run_host_tool", must_not_call)
    blocked = proxy.dispatch(
        {
            "tool": "search_get_evidence_detail",
            "args": {
                "agent_session_id": "agent_1",
                "candidate_id": "c001",
                "iteration": 1,
            },
        }
    )
    assert blocked == {
        "ok": False,
        "error": "worker tool response is unavailable",
    }
    assert called is False


@pytest.mark.skipif(sys.platform != "linux", reason="Bubblewrap is Linux-only")
def test_bubblewrap_hides_runtime_and_ground_truth_but_keeps_host_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not shutil.which("bwrap"):
        pytest.skip("bwrap is unavailable")
    root, workspace = _candidate_paths(tmp_path)
    source = workspace / "source"
    source.mkdir(parents=True)
    submission = workspace / "submission"
    submission.mkdir()
    (workspace / "task.json").write_text('{"controller": true}\n')
    _init_git(workspace)
    (source / "input.c").write_text("/* public source */\n", encoding="utf-8")
    runtime_secret = root / "runs" / "all-candidates.json"
    runtime_secret.write_text('{"secret": true}\n', encoding="utf-8")
    candidate_state = root / "runs" / "run_1" / "candidates" / "c001"
    candidate_state.mkdir(parents=True)
    candidate_record = candidate_state / "candidate.json"
    candidate_record.write_text(
        '{"candidate_id": "c001", "execution_generation": 1}\n',
        encoding="utf-8",
    )
    peer_record = (
        root / "runs" / "run_1" / "candidates" / "c002" / "candidate.json"
    )
    peer_record.parent.mkdir()
    peer_record.write_text('{"secret": true}\n', encoding="utf-8")
    ground_truth = tmp_path / "cases" / "ground-truth.json"
    ground_truth.parent.mkdir()
    ground_truth.write_text('{"answer": true}\n', encoding="utf-8")
    public_tests = tmp_path / "public-tests"
    public_tests.mkdir()
    (public_tests / "test.txt").write_text("public\n", encoding="utf-8")
    session_root = root / "host-sessions" / "pi"
    session_root.mkdir(parents=True)
    extension = _fixture_extension(tmp_path)
    pi_home = tmp_path / "pi-home"
    pi_home.mkdir()
    (pi_home / "models.json").write_text('{"models": []}\n', encoding="utf-8")
    (pi_home / "auth.json").write_text("{}\n", encoding="utf-8")
    (pi_home / "models-store.json").write_text("{}\n", encoding="utf-8")

    def fake_call(
        _root: Path,
        tool: str,
        args: dict[str, Any],
        _environment: dict[str, str],
    ) -> dict[str, Any]:
        assert tool == "search_get_agent_context"
        assert args == {"agent_session_id": "agent_1"}
        return {
            "agent_session_id": "agent_1",
            "run_id": "run_1",
            "candidate_id": "c001",
            "workspace": str(workspace),
            "metric_name": "format_valid",
            "metric_direction": "maximize",
            "candidate_task": {
                "run_id": "run_1",
                "candidate_id": "c001",
                "workspace": str(workspace),
            },
        }

    monkeypatch.setattr(
        "experiments.benchmark_compare.pi_worker_launcher._run_host_tool",
        fake_call,
    )
    script = "\n".join(
        (
            "import json, os, pathlib, subprocess, sys",
            f"assert not pathlib.Path({str(ground_truth)!r}).exists()",
            f"assert not pathlib.Path({str(runtime_secret)!r}).exists()",
            "runtime_root = pathlib.Path(os.environ['GOAL_PLUS_ROOT'])",
            "candidate_record = runtime_root / 'runs' / 'run_1' / 'candidates' / 'c001' / 'candidate.json'",
            "assert json.loads(candidate_record.read_text())['execution_generation'] == 1",
            f"assert not pathlib.Path({str(peer_record)!r}).exists()",
            f"public_test = pathlib.Path({str(public_tests / 'test.txt')!r})",
            "assert public_test.read_text() == 'public\\n'",
            "try:",
            " public_test.write_text('changed')",
            " raise AssertionError('public test was writable')",
            "except OSError:",
            " pass",
            "try:",
            " candidate_record.write_text('{}')",
            " raise AssertionError('candidate fencing state was writable')",
            "except OSError:",
            " pass",
            "assert os.environ['TEST_ALLOWED'] == 'yes'",
            "assert 'TEST_HIDDEN' not in os.environ",
            f"assert os.environ['GOAL_PLUS_ROOT'] == {str(root)!r}",
            "assert 'GOAL_PLUS_SOURCE_PATH' not in os.environ",
            f"assert pathlib.Path(os.environ[{TOOL_SOCKET_ENV!r}]).exists()",
            "pi_home = pathlib.Path(os.environ['PI_CODING_AGENT_DIR'])",
            "assert (pi_home / 'models.json').is_file()",
            "try:",
            " (pi_home / 'models.json').write_text('changed')",
            " raise AssertionError('Pi configuration was writable')",
            "except OSError:",
            " pass",
            "(pi_home / 'auth.json.lock').write_text('worker lock')",
            "(pi_home / 'models-store.json.lock').write_text('worker lock')",
            "(pi_home / 'auth.json').write_text('{\\\"worker\\\": true}')",
            "(pi_home / 'models-store.json').write_text('{\\\"worker\\\": true}')",
            "locked = pi_home / 'locked'",
            "locked.mkdir()",
            "locked.joinpath('state').write_text('private')",
            "locked.chmod(0)",
            "pathlib.Path('submission/output.json').write_text('{}')",
            "pathlib.Path('.tmp/handoff.json').write_text('{}')",
            "try:",
            " pathlib.Path('task.json').write_text('changed')",
            " raise AssertionError('controller metadata was writable')",
            "except OSError:",
            " pass",
            "try:",
            " pathlib.Path('source/input.c').write_text('changed')",
            " raise AssertionError('source was writable')",
            "except OSError:",
            " pass",
            "session_dir = pathlib.Path(sys.argv[sys.argv.index('--session-dir') + 1])",
            "session_dir.joinpath('native.txt').write_text('session')",
            "allowed = subprocess.run([",
            " 'goal-plus-pi-tool', '--root', '.gp', '--args-json',",
            " json.dumps({'agent_session_id': 'agent_1'}),",
            " 'search_get_agent_context'",
            "], capture_output=True, text=True)",
            "assert allowed.returncode == 0, allowed.stderr",
            "assert json.loads(allowed.stdout)['candidate_id'] == 'c001'",
            "denied = subprocess.run([",
            " 'goal-plus-pi-tool', '--root', '.gp', '--args-json',",
            " json.dumps({'run_id': 'run_1'}), 'search_select'",
            "], capture_output=True, text=True)",
            "assert denied.returncode != 0",
        )
    )
    environment = {
        **os.environ,
        "GOAL_PLUS_ROOT": str(root),
        "GOAL_PLUS_SOURCE_PATH": "/host/goal-plus",
        "PI_CODING_AGENT_DIR": str(pi_home),
        "TEST_ALLOWED": "yes",
        "TEST_HIDDEN": "no",
    }
    worker_command = _worker_command(
        script,
        session_root=session_root,
        extension=extension,
    )
    worker_command[0] = "/usr/bin/python3"
    worker = BubblewrapWorker(
        context=_context(workspace),
        policy=_policy(
            "source",
            writable=("submission",),
            pass_env=("TEST_ALLOWED",),
            read_only_host=(str(public_tests),),
            evaluation_mode="blind",
        ),
        command=worker_command,
        environment=environment,
    )
    private_runtime = worker.proxy.socket_dir
    command, sandbox_environment = worker.prepare()
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            env=sandbox_environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        worker.close()

    assert completed.returncode == 0, completed.stderr
    assert (workspace / "submission" / "output.json").read_text() == "{}"
    assert (workspace / ".tmp" / "handoff.json").read_text() == "{}"
    assert (workspace / "task.json").read_text() == '{"controller": true}\n'
    assert (source / "input.c").read_text(encoding="utf-8") == "/* public source */\n"
    assert runtime_secret.is_file()
    assert (session_root / "session_1" / "native.txt").read_text() == "session"
    assert (pi_home / "models.json").read_text() == '{"models": []}\n'
    assert (pi_home / "auth.json").read_text() == "{}\n"
    assert (pi_home / "models-store.json").read_text() == "{}\n"
    assert not (pi_home / "auth.json.lock").exists()
    assert not (pi_home / "models-store.json.lock").exists()
    assert not private_runtime.exists()


def test_declared_source_symlink_fails_closed(tmp_path: Path) -> None:
    if not shutil.which("bwrap"):
        pytest.skip("bwrap is unavailable")
    source_cache = tmp_path / "source-cache"
    source_cache.mkdir()
    (source_cache / "input.c").write_text("/* cached source */\n", encoding="utf-8")
    private = tmp_path / "private" / "reference-poc"
    private.parent.mkdir()
    private.write_text("ground truth\n", encoding="utf-8")
    root, workspace = _candidate_paths(tmp_path)
    workspace.mkdir()
    (workspace / "source").symlink_to(source_cache, target_is_directory=True)
    _init_git(workspace)
    session_root = root / "host-sessions" / "pi"
    session_root.mkdir(parents=True)
    extension = _fixture_extension(tmp_path)
    pi_home = tmp_path / "pi-home"
    pi_home.mkdir()
    worker = BubblewrapWorker(
        context=_context(workspace),
        policy=_policy("source"),
        command=_worker_command(
            "pass",
            session_root=session_root,
            extension=extension,
        ),
        environment={
            **os.environ,
            "GOAL_PLUS_ROOT": str(root),
            "PI_CODING_AGENT_DIR": str(pi_home),
        },
    )
    with pytest.raises(RuntimeError, match="must not be a symlink"):
        worker.prepare()
    assert (source_cache / "input.c").read_text() == "/* cached source */\n"
    assert private.read_text() == "ground truth\n"


def test_nested_protected_symlink_escape_fails_closed(tmp_path: Path) -> None:
    if not shutil.which("bwrap"):
        pytest.skip("bwrap is unavailable")
    root, workspace = _candidate_paths(tmp_path)
    source = workspace / "source"
    source.mkdir(parents=True)
    private = tmp_path / "private.txt"
    private.write_text("private\n")
    (source / "escape").symlink_to(private)
    _init_git(workspace)
    session_root = root / "host-sessions" / "pi"
    session_root.mkdir(parents=True)
    pi_home = tmp_path / "pi-home"
    pi_home.mkdir()
    worker = BubblewrapWorker(
        context=_context(workspace),
        policy=_policy("source"),
        command=_worker_command(
            "pass",
            session_root=session_root,
            extension=_fixture_extension(tmp_path),
        ),
        environment={
            **os.environ,
            "GOAL_PLUS_ROOT": str(root),
            "PI_CODING_AGENT_DIR": str(pi_home),
        },
    )

    with pytest.raises(RuntimeError, match="symlink escapes"):
        worker.prepare()
    assert private.read_text() == "private\n"


def test_writable_mount_and_scratch_roots_must_not_be_symlinks(
    tmp_path: Path,
) -> None:
    if not shutil.which("bwrap"):
        pytest.skip("bwrap is unavailable")
    root, workspace = _candidate_paths(tmp_path)
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "submission").symlink_to(outside, target_is_directory=True)
    _init_git(workspace)
    session_root = root / "host-sessions" / "pi"
    session_root.mkdir(parents=True)
    pi_home = tmp_path / "pi-home"
    pi_home.mkdir()
    worker = BubblewrapWorker(
        context=_context(workspace),
        policy=_policy(writable=("submission",)),
        command=_worker_command(
            "pass",
            session_root=session_root,
            extension=_fixture_extension(tmp_path),
        ),
        environment={
            **os.environ,
            "GOAL_PLUS_ROOT": str(root),
            "PI_CODING_AGENT_DIR": str(pi_home),
        },
    )
    with pytest.raises(RuntimeError, match="writable.*must not be a symlink"):
        worker.prepare()

    (workspace / "submission").unlink()
    (workspace / "submission").mkdir()
    (workspace / ".tmp").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="workspace .tmp must not be a symlink"):
        worker.prepare()


def test_external_git_administration_uses_candidate_private_view(
    tmp_path: Path,
) -> None:
    if not shutil.which("bwrap"):
        pytest.skip("bwrap is unavailable")
    root, workspace = _candidate_paths(tmp_path)
    repository = root / "runs" / "run_1" / "workspace-repository"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    (repository / "tracked.txt").write_text("fixture\n", encoding="utf-8")
    (repository / "results.tsv").write_text(
        "iteration\tformat_valid\n1\tprivate-score-sentinel\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-q", "-m", "fixture"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "add", "-q", str(workspace)],
        check=True,
    )
    candidate_head = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repository), "branch", "peer-only"],
        check=True,
    )
    session_root = root / "host-sessions" / "pi"
    session_root.mkdir(parents=True)
    extension = _fixture_extension(tmp_path)
    pi_home = tmp_path / "pi-home"
    pi_home.mkdir()
    worker = BubblewrapWorker(
        context=_context(workspace),
        policy=_policy(writable=("tracked.txt",), evaluation_mode="blind"),
        command=_worker_command(
            "\n".join(
                (
                    "import pathlib, subprocess",
                    f"assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() != {candidate_head!r}",
                    "refs = subprocess.check_output(['git', 'for-each-ref', '--format=%(refname)'], text=True).splitlines()",
                    "assert refs == ['refs/heads/sandbox'], refs",
                    "assert subprocess.check_output(['git', 'log', '-1', '--format=%s'], text=True).strip() == 'public candidate baseline'",
                    f"assert not pathlib.Path({str(root / 'worker-sandbox-state')!r}).exists()",
                    f"assert not pathlib.Path({str(root / 'runs' / 'run_1' / 'candidates')!r}).exists()",
                    "assert pathlib.Path('.git').read_text().strip() == 'gitdir: /opt/bench-goal-plus/git-admin'",
                    f"assert subprocess.run(['git', 'cat-file', '-e', {candidate_head!r}], capture_output=True).returncode != 0",
                    f"assert pathlib.Path('results.tsv').read_text() == {_OPAQUE_RESULTS_LEDGER!r}",
                    f"assert subprocess.check_output(['git', 'show', 'HEAD:results.tsv'], text=True) == {_OPAQUE_RESULTS_LEDGER!r}",
                    "assert 'private-score-sentinel' not in subprocess.check_output(['git', 'log', '-p', '--all'], text=True)",
                    "assert 'fixture' not in subprocess.check_output(['git', 'log', '--format=%s', '--all'], text=True).splitlines()",
                    "pathlib.Path('tracked.txt').write_text('changed\\n')",
                    "assert 'tracked.txt' in subprocess.check_output(['git', 'status', '--short'], text=True)",
                    "subprocess.run(['git', '-c', 'user.name=Worker', '-c', 'user.email=worker@example.com', 'add', 'tracked.txt'], check=True)",
                    "subprocess.run(['git', '-c', 'user.name=Worker', '-c', 'user.email=worker@example.com', 'commit', '-q', '-m', 'worker change'], check=True)",
                )
            ),
            session_root=session_root,
            extension=extension,
        ),
        environment={
            **os.environ,
            "GOAL_PLUS_ROOT": str(root),
            "PI_CODING_AGENT_DIR": str(pi_home),
        },
    )
    command, environment = worker.prepare()
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        worker.close()

    assert completed.returncode == 0, completed.stderr
    assert (workspace / "tracked.txt").read_text(encoding="utf-8") == "changed\n"
    assert "private-score-sentinel" in (workspace / "results.tsv").read_text(
        encoding="utf-8"
    )
    private_git = root / "worker-sandbox-state" / "run_1" / "c001" / "git-admin"
    assert private_git.is_dir()
    assert (
        subprocess.check_output(
            ["git", f"--git-dir={private_git}", "log", "-1", "--format=%s"],
            text=True,
        ).strip()
        == "worker change"
    )

    continued = BubblewrapWorker(
        context=_context(workspace),
        policy=_policy(writable=("tracked.txt",), evaluation_mode="blind"),
        command=_worker_command(
            "import subprocess; "
            "assert subprocess.check_output(["
            "'git', 'log', '-1', '--format=%s'"
            "], text=True).strip() == 'worker change'",
            session_root=session_root,
            extension=extension,
            session_id="session_2",
        ),
        environment={
            **os.environ,
            "GOAL_PLUS_ROOT": str(root),
            "PI_CODING_AGENT_DIR": str(pi_home),
        },
    )
    continued_command, continued_environment = continued.prepare()
    try:
        continued_result = subprocess.run(
            continued_command,
            cwd=workspace,
            env=continued_environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        continued.close()
    assert continued_result.returncode == 0, continued_result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="Bubblewrap is Linux-only")
def test_bench_path_shim_launches_standard_goal_plus_pi_worker(
    tmp_path: Path,
) -> None:
    real_pi_text = shutil.which("pi")
    if not shutil.which("bwrap") or real_pi_text is None:
        pytest.skip("bwrap or pi is unavailable")
    real_pi = Path(real_pi_text).absolute()
    root, workspace = _candidate_paths(tmp_path)
    workspace.mkdir()
    _init_git(workspace)
    _write_session_record(root, workspace)
    session_root = root / "host-sessions" / "pi"
    session_root.mkdir(parents=True)
    extension = _fixture_extension(tmp_path)
    pi_home = tmp_path / "pi-home"
    pi_home.mkdir()
    (pi_home / "auth.json").write_text("{}\n", encoding="utf-8")
    (pi_home / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "test-provider": {
                        "baseUrl": "http://127.0.0.1:1/v1",
                        "api": "openai-completions",
                        "apiKey": "$TEST_API_KEY",
                        "models": [
                            {
                                "id": "test-model",
                                "name": "test-model",
                                "reasoning": False,
                                "input": ["text"],
                                "contextWindow": 4096,
                                "maxTokens": 1024,
                            }
                        ],
                    }
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    shim_dir = (
        Path(__file__).resolve().parents[1]
        / "experiments"
        / "benchmark_compare"
        / "pi-shim"
    )
    assert not (shim_dir / "goal-plus-pi-tool").exists()
    environment = {
        **os.environ,
        "PATH": str(shim_dir) + os.pathsep + os.environ.get("PATH", ""),
        "GOAL_PLUS_ROOT": str(root),
        "GOAL_PLUS_PI_ROLE": "worker",
        "PI_CODING_AGENT_DIR": str(pi_home),
        "TEST_API_KEY": "fixture-key",
        REAL_PI_BIN_ENV: str(real_pi),
        SANDBOX_POLICY_ENV: json.dumps(
            {
                "engine": "bubblewrap",
                "evaluation_mode": "blind",
                "workspace_access": "read_only",
                "read_only_workspace_paths": [],
                "writable_workspace_paths": [],
                "pass_env": ["TEST_API_KEY"],
            }
        ),
    }

    completed = subprocess.run(
        [
            "pi",
            "--mode",
            "rpc",
            "--provider",
            "test-provider",
            "--model",
            "test-provider/test-model",
            "--approve",
            "--session-dir",
            str(session_root),
            "--session-id",
            "agent_1",
            "--no-extensions",
            "-e",
            str(extension),
        ],
        cwd=workspace,
        env=environment,
        input='{"type":"get_state","id":"shim-smoke"}\n',
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    responses = [
        json.loads(line)
        for line in completed.stdout.splitlines()
        if line.startswith("{")
    ]
    assert any(
        response.get("id") == "shim-smoke"
        and response.get("success") is True
        and response.get("command") == "get_state"
        for response in responses
    )
