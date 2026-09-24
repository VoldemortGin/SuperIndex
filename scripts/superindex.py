#!/usr/bin/env python3
"""Source-checkout entry for the superindex CLI.

Usage:
    uv run scripts/superindex.py index|ask|search|serve|batch ...
"""
import sys
from pathlib import Path

# Repo root first, ahead of scripts/ (which holds this file, also named superindex).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from superindex.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
