"""Entry point for `python -m superindex` and for a PyInstaller build.

Offline defaults are set before anything can import litellm, and
`freeze_support()` runs first so a frozen Windows build does not re-enter the
CLI in spawned worker processes.
"""
import multiprocessing
import os
import sys

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from superindex.cli import main

    sys.exit(main())
