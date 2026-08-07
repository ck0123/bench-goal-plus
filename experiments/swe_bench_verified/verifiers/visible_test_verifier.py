#!/usr/bin/env python3
"""Normalize an Agent-selected visible test command into a Goal Plus metric."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from typing import Any


MAX_OUTPUT_CHARS = 4000


def _tail(value: str) -> str:
    return value[-MAX_OUTPUT_CHARS:]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metric", default="visible_test_score")
    parser.add_argument(
        "--ranking-signal",
        action="store_true",
        help=(
            "Return success after a completed test command so freeze preflight can "
            "observe a legitimate zero baseline. Timeouts and launch failures still fail."
        ),
    )
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("a visible test command is required after --")
    if args.timeout_seconds < 1:
        raise SystemExit("--timeout-seconds must be positive")

    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    pytest_options = environment.get("PYTEST_ADDOPTS", "").strip()
    if "no:cacheprovider" not in pytest_options:
        environment["PYTEST_ADDOPTS"] = (
            f"{pytest_options} -p no:cacheprovider".strip()
        )
    payload: dict[str, Any]
    returncode = 1
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=args.timeout_seconds,
            env=environment,
        )
        payload = {
            args.metric: 1.0 if completed.returncode == 0 else 0.0,
            "test_returncode": completed.returncode,
            "timed_out": False,
            "failure_kind": (
                None if completed.returncode == 0 else "test_command_failed"
            ),
            "stdout_tail": _tail(completed.stdout),
            "stderr_tail": _tail(completed.stderr),
        }
        returncode = 0 if completed.returncode == 0 or args.ranking_signal else 1
    except subprocess.TimeoutExpired as error:
        payload = {
            args.metric: 0.0,
            "test_returncode": None,
            "timed_out": True,
            "failure_kind": "timeout",
            "stdout_tail": _tail(error.stdout or ""),
            "stderr_tail": _tail(error.stderr or ""),
        }
    except OSError as error:
        payload = {
            args.metric: 0.0,
            "test_returncode": None,
            "timed_out": False,
            "failure_kind": "command_unavailable",
            "stdout_tail": "",
            "stderr_tail": _tail(f"{type(error).__name__}: {error}"),
        }
    payload["test_elapsed_seconds"] = time.monotonic() - started
    print(json.dumps(payload, sort_keys=True))
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
