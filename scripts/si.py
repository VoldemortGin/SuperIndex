#!/usr/bin/env python3
"""Source-checkout entry for the superindex CLI (needs `uv sync`, which installs the package editable).

Usage:
    uv run python scripts/si.py index|ask|search|serve|batch ...
"""
import sys

from superindex.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
