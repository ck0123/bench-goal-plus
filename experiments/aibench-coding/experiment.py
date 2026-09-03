#!/usr/bin/env python3
"""Stable entrypoint for the native aibench coding controller."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from experiments.aibench_coding.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
