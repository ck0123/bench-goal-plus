#!/usr/bin/env python3
"""Small subprocess bridge to the upstream aibench materializer and grader."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bench_runtime_paths import temporary_directory  # noqa: E402


def _case_paths(source_root: Path, case_set: str) -> list[Path]:
    root = source_root / "benchmarks" / "ai_coding" / "cases" / case_set
    paths = sorted(path for path in root.glob("*.json") if not path.name.startswith("_"))
    if not paths:
        raise FileNotFoundError(f"no aibench cases under {root}")
    return paths


def _load(source_root: Path, case_set: str) -> tuple[list[Any], dict[str, Any]]:
    from aibench.models import Case

    raw_cases = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in _case_paths(source_root, case_set)
    ]
    return [Case.from_dict(raw) for raw in raw_cases], {
        str(raw["case_id"]): raw for raw in raw_cases
    }


def _select(source_root: Path, case_set: str, case_id: str) -> tuple[Any, dict[str, Any], str]:
    from aibench.validity import set_fingerprint

    cases, raw_by_id = _load(source_root, case_set)
    by_id = {str(case.case_id): case for case in cases}
    try:
        case = by_id[case_id]
        raw = raw_by_id[case_id]
    except KeyError as error:
        raise ValueError(f"unknown aibench case: {case_id}") from error
    return case, raw, str(set_fingerprint(cases))


def _submission_root(path: Path) -> Path:
    submission = path.expanduser().absolute()
    if submission.is_symlink() or not submission.is_dir():
        raise RuntimeError("aibench submission root must be a real directory")
    return submission


def materialize(args: argparse.Namespace) -> dict[str, Any]:
    from aibench.validity import case_fingerprint
    from aibench.workspace import materialize_workspace

    source_root = args.source_root.resolve()
    case, raw, set_fingerprint = _select(source_root, args.case_set, args.case_id)
    case_dir = source_root / "benchmarks" / "ai_coding" / "cases" / args.case_set
    result = materialize_workspace(
        case,
        args.destination.resolve(),
        case_set_dir=case_dir,
        allow_network=False,
    )
    metadata = raw.get("metadata") or {}
    return {
        "schema_version": 1,
        "case_id": case.case_id,
        "case_set": args.case_set,
        "case_set_fingerprint": set_fingerprint,
        "case_fingerprint": case_fingerprint(case),
        "task_type": case.task_type,
        "language": case.language,
        "prompt": str(raw.get("prompt") or ""),
        "grader_command": str((raw.get("grader") or {}).get("command") or ""),
        "validity_ok": metadata.get("validity_ok"),
        "materialization": result.to_dict(),
    }


def grade(args: argparse.Namespace) -> dict[str, Any]:
    from aibench.grading import grade_case, workspace_inventory
    from aibench.validity import case_fingerprint
    from aibench.workspace import materialize_workspace

    source_root = args.source_root.resolve()
    case, _raw, set_fingerprint = _select(source_root, args.case_set, args.case_id)
    expected = json.loads(args.public_metadata.read_text(encoding="utf-8"))
    if set_fingerprint != expected.get("case_set_fingerprint"):
        raise RuntimeError("aibench case-set fingerprint changed after preparation")
    if case_fingerprint(case) != expected.get("case_fingerprint"):
        raise RuntimeError("aibench case fingerprint changed after preparation")
    case_dir = source_root / "benchmarks" / "ai_coding" / "cases" / args.case_set
    with temporary_directory(
        prefix="official-grade-", namespace="aibench-coding/official-grader"
    ) as temporary:
        clean = temporary / "clean"
        evaluated = temporary / "evaluated"
        materialize_workspace(
            case,
            clean,
            case_set_dir=case_dir,
            allow_network=False,
        )
        baseline = workspace_inventory(clean)
        shutil.copytree(_submission_root(args.submission), evaluated, symlinks=True)
        result = grade_case(case, evaluated, baseline=baseline, env_passthrough=())
    return {
        "schema_version": 1,
        "case_id": case.case_id,
        "case_set_fingerprint": set_fingerprint,
        "case_fingerprint": case_fingerprint(case),
        "grade": result.to_dict(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    children = parser.add_subparsers(dest="command", required=True)
    materialize_parser = children.add_parser("materialize")
    grade_parser = children.add_parser("grade")
    for child in (materialize_parser, grade_parser):
        child.add_argument("--source-root", type=Path, required=True)
        child.add_argument("--case-set", required=True)
        child.add_argument("--case-id", required=True)
    materialize_parser.add_argument("--destination", type=Path, required=True)
    grade_parser.add_argument("--submission", type=Path, required=True)
    grade_parser.add_argument("--public-metadata", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = materialize(args) if args.command == "materialize" else grade(args)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
