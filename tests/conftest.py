"""pytest glue for the script-style test modules (`check()` + `main()`).

Those modules run standalone (`uv run python tests/test_registry.py`) and record
results in a module-level `FAIL` list instead of asserting. Under pytest:
`tmp` is the per-test temp directory their tests take, and any `check()` that
fails during a test fails that test.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture(autouse=True)
def _failed_checks_fail_the_test(request: pytest.FixtureRequest):
    fails = getattr(request.module, "FAIL", None)
    before = len(fails) if isinstance(fails, list) else 0
    yield
    if isinstance(fails, list) and len(fails) > before:
        pytest.fail("failed checks: " + ", ".join(fails[before:]))
