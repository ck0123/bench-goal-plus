#!/usr/bin/env python3
"""Audit run directories for worker network egress.

Scans every Pi session transcript (worker ``host-sessions`` and the main
session) inside one or more run directories, pairs each real fetch attempt
(curl/wget/git-remote/python http client against an http(s) URL) with its
recorded tool result, and classifies it as ``succeeded``, ``failed`` or
``no-result``. A campaign is CONTAMINATED when any fetch succeeded.

Usage:
    python3 scripts/audit_network_access.py runs/detect-foo [runs/detect-bar ...]
    python3 scripts/audit_network_access.py --json runs/detect-foo

Exit code: 0 = clean, 1 = contamination found, 2 = usage error.
Output previews are truncated and never include request bodies.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

FETCH_PATTERN = re.compile(
    r"\b(curl|wget)\b[^|;&]*https?://"
    r"|git\s+(clone|ls-remote|fetch)\s+https?://"
    r"|urllib\.request\.(urlopen|Request)"
    r"|requests\.(get|post)\(\s*['\"]https?://"
    r"|pip3?\s+install\s+[a-z]"
    r"|subprocess.*curl"
    r"|api\.(osv|github)\.dev?"
    r"|nvd\.nist"
    r"|cveawg\.mitre"
    r"|raw\.githubusercontent"
    r"|gitlab\.[a-z]+/"
    r"|git\.kernel\.org",
    re.IGNORECASE,
)

FAILURE_MARKERS = (
    "could not resolve",
    "connection refused",
    "network is unreachable",
    "timed out",
    "failed to connect",
    "exit=77",
    "certificate verify",
    "exit=2\n",
    "exited with code 1",
    "exited with code 2",
    "no such file",
    "cannot access",
    "blocked",
    "code': 3",
    "403 ",
    "000 0",
    "rc=6",
    "rc=7",
    "rc=28",
    "errno -2",
    "errno -3",
    "name or service",
)

MAX_PREVIEW = 110


def _result_text(content: object) -> str:
    if isinstance(content, list):
        return " ".join(
            item.get("text", "") for item in content if isinstance(item, dict)
        )
    return str(content or "")


def audit_file(path: Path) -> list[dict[str, object]]:
    """Return fetch events with an outcome for one session transcript."""
    events: list[dict[str, object]] = []
    pending: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "message":
                    continue
                message = record.get("message") or {}
                role = message.get("role")
                if role == "assistant":
                    for block in message.get("content") or []:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "toolCall":
                            continue
                        arguments = block.get("arguments") or {}
                        command = str(
                            arguments.get("command") or arguments.get("prompt") or ""
                        )
                        if FETCH_PATTERN.search(command):
                            pending[block.get("id")] = command
                elif role == "toolResult":
                    call_id = message.get("toolCallId")
                    if call_id in pending:
                        command = pending.pop(call_id)
                        output = _result_text(message.get("content"))
                        lowered = output.lower()
                        if not output or len(output) < 20 or any(
                            marker in lowered for marker in FAILURE_MARKERS
                        ):
                            outcome = "failed"
                        else:
                            outcome = "succeeded"
                        events.append(
                            {
                                "command": command[:MAX_PREVIEW],
                                "outcome": outcome,
                                "result_preview": output[:MAX_PREVIEW]
                                .replace("\n", " "),
                            }
                        )
    except OSError as exc:
        events.append(
            {"command": f"<unreadable session {path.name}>", "outcome": f"error: {exc}", "result_preview": ""}
        )
    for command in pending.values():
        events.append(
            {"command": command[:MAX_PREVIEW], "outcome": "no-result", "result_preview": ""}
        )
    return events


def audit_campaign(campaign_dir: Path) -> dict[str, object]:
    sessions = sorted(
        glob.glob(str(campaign_dir / "cells" / "*" / "workspace" / ".gp" / "host-sessions" / "pi" / "*" / "*.jsonl"))
    ) + sorted(
        glob.glob(str(campaign_dir / "cells" / "*" / "pi-main-session" / "*.jsonl"))
    )
    events = []
    for session in sessions:
        for event in audit_file(Path(session)):
            kind = "main" if "pi-main-session" in session else "worker"
            events.append({**event, "session": kind, "file": Path(session).name})
    succeeded = [event for event in events if event["outcome"] == "succeeded"]
    return {
        "campaign": campaign_dir.name,
        "sessions_scanned": len(sessions),
        "fetch_events": events,
        "succeeded": len(succeeded),
        "failed": len(events) - len(succeeded),
        "contaminated": bool(succeeded),
    }


def resolve_campaigns(arguments: list[str]) -> list[Path]:
    campaigns: list[Path] = []
    for argument in arguments:
        path = Path(argument).expanduser().resolve()
        if not path.is_dir():
            raise SystemExit(f"not a directory: {path}")
        if (path / "campaign.json").is_file():
            campaigns.append(path)
        else:
            found = sorted(child for child in path.glob("detect-*") if (child / "campaign.json").is_file())
            if not found:
                raise SystemExit(f"no detect-* campaigns under {path}")
            campaigns.extend(found)
    return campaigns


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+", help="run dir or campaign dir")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args(argv)

    campaigns = resolve_campaigns(args.run_dirs)
    reports = [audit_campaign(campaign) for campaign in campaigns]
    contaminated = [report for report in reports if report["contaminated"]]

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=1))
    else:
        for report in reports:
            flag = "CONTAMINATED" if report["contaminated"] else "clean"
            print(f"{report['campaign']}: {flag} "
                  f"(sessions={report['sessions_scanned']}, "
                  f"succeeded={report['succeeded']}, failed={report['failed']})")
            for event in report["fetch_events"]:
                if event["outcome"] == "succeeded":
                    print(f"  [succeeded/{event['session']}] {event['command']}")
                    print(f"    -> {event['result_preview']}")
        if not reports:
            print("no campaigns found")
    return 1 if contaminated else 0


if __name__ == "__main__":
    sys.exit(main())
