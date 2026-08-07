"""Validation for the repository baseline baked into official task images."""

from __future__ import annotations

import re
from typing import Any


OFFICIAL_SETUP_EMAIL = "setup@swebench.config"
OFFICIAL_SETUP_SUBJECT = "SWE-bench"
FULL_SHA = re.compile(r"[0-9a-f]{40}")

CHECKOUT_PROBE_SCRIPT = r"""
set -eu
git -C /testbed show -s --format="observed_head=%H%nparents=%P%nauthor_email=%ae%ncommitter_email=%ce%nsubject=%s%nobserved_tree=%T" HEAD
printf 'base_tree='
git -C /testbed rev-parse "$1^{tree}"
status="$(git -C /testbed status --porcelain=v1 --untracked-files=all)"
if [ -z "$status" ]; then
    printf 'clean=true\n'
else
    printf 'clean=false\n'
fi
""".strip()

_PROBE_FIELDS = {
    "observed_head",
    "parents",
    "author_email",
    "committer_email",
    "subject",
    "observed_tree",
    "base_tree",
    "clean",
}


def validate_checkout_probe(output: str, base_commit: str) -> dict[str, Any]:
    """Validate a clean dataset base or the setup commit created by the harness."""

    values: dict[str, str] = {}
    parse_errors: list[str] = []
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in _PROBE_FIELDS:
            parse_errors.append(f"unexpected probe line: {line!r}")
            continue
        if key in values:
            parse_errors.append(f"duplicate probe field: {key}")
            continue
        values[key] = value
    missing = sorted(_PROBE_FIELDS - set(values))
    if missing:
        parse_errors.append("missing probe fields: " + ", ".join(missing))

    observed_head = values.get("observed_head")
    parents = values.get("parents", "").split()
    author_email = values.get("author_email")
    committer_email = values.get("committer_email")
    subject = values.get("subject")
    observed_tree = values.get("observed_tree")
    base_tree = values.get("base_tree")
    clean_value = values.get("clean")
    clean = clean_value == "true"

    if observed_head is not None and FULL_SHA.fullmatch(observed_head) is None:
        parse_errors.append("observed HEAD is not a full commit SHA")
    if observed_tree is not None and FULL_SHA.fullmatch(observed_tree) is None:
        parse_errors.append("observed tree is not a full SHA")
    if base_tree is not None and FULL_SHA.fullmatch(base_tree) is None:
        parse_errors.append("base tree is not a full SHA")
    if clean_value not in {"true", "false"}:
        parse_errors.append("clean must be true or false")

    at_dataset_base = observed_head == base_commit
    official_setup_commit = bool(
        observed_head
        and not at_dataset_base
        and parents == [base_commit]
        and author_email == OFFICIAL_SETUP_EMAIL
        and committer_email == OFFICIAL_SETUP_EMAIL
        and subject == OFFICIAL_SETUP_SUBJECT
    )
    failures = list(parse_errors)
    if not at_dataset_base and not official_setup_commit:
        if parents != [base_commit]:
            failures.append("HEAD must have exactly the dataset base commit as its parent")
        if author_email != OFFICIAL_SETUP_EMAIL:
            failures.append("HEAD author email is not the official setup identity")
        if committer_email != OFFICIAL_SETUP_EMAIL:
            failures.append("HEAD committer email is not the official setup identity")
        if subject != OFFICIAL_SETUP_SUBJECT:
            failures.append("HEAD subject is not the official setup subject")
    if not clean:
        failures.append("image checkout working tree is not clean")

    passed = not failures and bool(at_dataset_base or official_setup_commit)
    return {
        "passed": passed,
        "failure_reason": "; ".join(failures) or None,
        "baseline_kind": (
            "dataset-base"
            if at_dataset_base
            else "official-setup"
            if official_setup_commit
            else "invalid"
        ),
        "base_commit": base_commit,
        "observed_head": observed_head,
        "parents": parents,
        "author_email": author_email,
        "committer_email": committer_email,
        "subject": subject,
        "clean": clean,
        "base_tree": base_tree,
        "observed_tree": observed_tree,
        "tree_matches_base": bool(base_tree and observed_tree == base_tree),
        "synthetic_head": bool(observed_head and observed_head != base_commit),
        "official_setup_commit": official_setup_commit,
        "patch_base_commit": observed_head if passed else None,
    }
