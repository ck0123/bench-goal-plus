#!/usr/bin/env python3
"""Small subprocess bridge to the upstream aibench materializer and grader."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bench_runtime_paths import temporary_directory  # noqa: E402


BOUNDARY_FILE = ".aibench-boundary.json"


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


def _safe_case_path(value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RuntimeError(f"unsafe aibench case path: {value!r}")
    return relative


def _regular_file(root: Path, relative: Path) -> Path:
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise RuntimeError(f"aibench case path must not be a symlink: {relative}")
    if not current.is_file():
        raise RuntimeError(f"aibench case path is not a regular file: {relative}")
    try:
        current.resolve(strict=True).relative_to(root.resolve(strict=True))
    except ValueError as error:
        raise RuntimeError(f"aibench case path escapes its root: {relative}") from error
    return current


def _split_materialized_workspace(
    complete: Path,
    submission: Path,
    public_tests: Path,
    protected_paths: list[str],
) -> list[dict[str, str]]:
    if submission.exists() or public_tests.exists():
        raise FileExistsError(submission if submission.exists() else public_tests)
    relative_paths = [_safe_case_path(value) for value in protected_paths]
    if len(relative_paths) != len(set(relative_paths)):
        raise RuntimeError("aibench protected paths must be unique")
    protected = set(relative_paths)

    public_tests.mkdir(parents=True)
    records = []
    for relative in relative_paths:
        source = _regular_file(complete, relative)
        destination = public_tests / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        records.append(
            {
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            }
        )

    def exclude_protected(directory: str, names: list[str]) -> set[str]:
        parent = Path(directory).resolve().relative_to(complete.resolve())
        return {
            name
            for name in names
            if (parent / name) in protected
        }

    shutil.copytree(complete, submission, symlinks=True, ignore=exclude_protected)
    return records


def _seal_public_tests(public_tests: Path) -> None:
    for path in public_tests.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    public_tests.chmod(0o555)


def _restore_protected_files(
    clean: Path,
    evaluated: Path,
    protected_paths: list[str],
) -> None:
    evaluated_root = evaluated.resolve(strict=True)
    for value in protected_paths:
        relative = _safe_case_path(value)
        source = _regular_file(clean, relative)
        target = evaluated / relative
        if target.exists() or target.is_symlink():
            raise RuntimeError(
                f"submission contains controller-owned public test: {relative}"
            )
        current = evaluated
        for part in relative.parts[:-1]:
            current /= part
            if current.is_symlink() or (current.exists() and not current.is_dir()):
                raise RuntimeError(
                    f"submission conflicts with public test parent: {relative}"
                )
            current.mkdir(exist_ok=True)
        try:
            target.parent.resolve(strict=True).relative_to(evaluated_root)
        except ValueError as error:
            raise RuntimeError(
                f"public test destination escapes submission: {relative}"
            ) from error
        shutil.copy2(source, target)


def materialize(args: argparse.Namespace) -> dict[str, Any]:
    from aibench.validity import case_fingerprint
    from aibench.workspace import materialize_workspace, safe_relpath

    source_root = args.source_root.resolve()
    case, raw, set_fingerprint = _select(source_root, args.case_set, args.case_id)
    case_dir = source_root / "benchmarks" / "ai_coding" / "cases" / args.case_set
    metadata = raw.get("metadata") or {}
    case_files = {str(safe_relpath(item.path)): item.content for item in case.files}
    protected_paths = []
    for declared in case.grader.protected_paths:
        relative = str(safe_relpath(declared))
        if relative not in case_files:
            raise RuntimeError(
                f"protected path is absent from case context: {declared}"
            )
        protected_paths.append(relative)
    with temporary_directory(
        prefix="materialize-", namespace="aibench-coding/materialize"
    ) as temporary:
        complete = temporary / "complete"
        result = materialize_workspace(
            case,
            complete,
            case_set_dir=case_dir,
            allow_network=False,
        )
        public_test_files = _split_materialized_workspace(
            complete,
            args.destination.resolve(),
            args.public_tests.resolve(),
            protected_paths,
        )
        boundary = {
            "schema_version": 1,
            "case_id": case.case_id,
            "case_set": args.case_set,
            "case_set_fingerprint": set_fingerprint,
            "case_fingerprint": case_fingerprint(case),
            "language": case.language,
            "grader_command": str((raw.get("grader") or {}).get("command") or ""),
            "public_test_files": public_test_files,
        }
        boundary_path = args.public_tests.resolve() / BOUNDARY_FILE
        boundary_bytes = (
            json.dumps(boundary, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        boundary_path.write_bytes(boundary_bytes)
        _seal_public_tests(args.public_tests.resolve())
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
        "public_test_files": public_test_files,
        "public_test_boundary_sha256": hashlib.sha256(boundary_bytes).hexdigest(),
        "validity_ok": metadata.get("validity_ok"),
        "materialization": result.to_dict(),
    }


def grade(args: argparse.Namespace) -> dict[str, Any]:
    from aibench.grading import grade_case, workspace_inventory
    from aibench.validity import case_fingerprint
    from aibench.workspace import materialize_workspace, safe_relpath

    source_root = args.source_root.resolve()
    case, _raw, set_fingerprint = _select(source_root, args.case_set, args.case_id)
    if set_fingerprint != args.case_set_fingerprint:
        raise RuntimeError("aibench case-set fingerprint changed after preparation")
    if case_fingerprint(case) != args.case_fingerprint:
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
        _restore_protected_files(
            clean,
            evaluated,
            [str(safe_relpath(path)) for path in case.grader.protected_paths],
        )
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
    materialize_parser.add_argument("--public-tests", type=Path, required=True)
    grade_parser.add_argument("--submission", type=Path, required=True)
    grade_parser.add_argument("--case-set-fingerprint", required=True)
    grade_parser.add_argument("--case-fingerprint", required=True)
    args = parser.parse_args(argv)
    payload = materialize(args) if args.command == "materialize" else grade(args)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
