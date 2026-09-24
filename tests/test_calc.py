"""Offline tests for `superindex.calc` — the `calculate` agent tool.

    pytest tests/test_calc.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from superindex import calc  # noqa: E402
from superindex.calc import CalcError, evaluate  # noqa: E402


def value(expression: str, variables: dict[str, object] | None = None) -> str:
    return evaluate(expression, variables)["result"]


def test_arithmetic_and_precedence() -> None:
    assert value("2 + 3 * 4") == "14"
    assert value("(2 + 3) * 4") == "20"
    assert value("-3 ** 2") == "-9"
    assert value("7 // 2 + 7 % 2") == "4"
    assert value("2^10") == "1024"
    assert value("6 × 7 ÷ 2 − 1") == "20"


def test_decimal_precision() -> None:
    assert value("0.1 + 0.2") == "0.3"
    assert value("1.1 * 3") == "3.3"
    out = evaluate("1 / 3")
    assert out["result"].startswith("0.3333333333333333333333") and out["rounded"] == "0.3333"
    assert evaluate("0.00001234 * 0.5")["rounded"] == "0.00000617"
    assert value("round(2.5)") == "3" and value("round(2 / 3, 2)") == "0.67"


def test_percent_and_modulo() -> None:
    assert value("12.5%") == "0.125"
    assert value("12.5% * 200") == "25"
    assert value("(100 + 5%)") == "100.05"
    assert value("10 % 3") == "1" and value("10%3") == "1"


def test_functions() -> None:
    assert evaluate("pct_change(6610, 6300)")["rounded"] == "4.9206"
    assert value("pct_change(90, 100)") == "-10"
    assert evaluate("cagr(200, 100, 5)")["rounded"] == "14.8698"
    assert value("ratio(1, 4)") == "0.25"
    assert value("avg(1, 2, 3, 4)") == "2.5"
    assert value("sum(1, 2, 3) + max(1, 234) - min(5, 4) + abs(-1)") == "237"


def test_variables_are_substituted_and_echoed() -> None:
    out = evaluate("pct_change(opat_2023, opat_2022)",
                   {"opat_2023": 6610, "opat_2022": "6,300"})
    assert out["rounded"] == "4.9206"
    assert out["variables"] == {"opat_2023": "6610", "opat_2022": "6300"}
    assert out["substituted"] == "pct_change(6610, 6300)"
    assert evaluate("a - b", {"a": -5, "b": 2.5})["substituted"] == "(-5) - 2.5"
    with pytest.raises(CalcError, match="not defined"):
        evaluate("x + 1")
    for bad in ({"_x": 1}, {"round": 1}, {"a b": 1}, {"x": "abc"}, {"x": True}):
        with pytest.raises(CalcError):
            evaluate("1", bad)


def test_errors_are_readable() -> None:
    for expression, message in (("1 / 0", "division by zero"),
                                ("ratio(1, 0)", "division by zero"),
                                ("pct_change(1, 0)", "division by zero"),
                                ("1 +", "invalid expression"),
                                ("x = 1", "invalid expression"),
                                ("1; 2", "invalid expression"),
                                ("", "required"),
                                ("1,234 + 5", "thousands separators"),
                                ("'a' * 3", "unsupported constant"),
                                ("True + 1", "unsupported constant"),
                                ("1 < 2", "not allowed"),
                                ("pct_change(1)", "missing")):
        with pytest.raises(CalcError, match=message):
            evaluate(expression)


def test_rejects_code() -> None:
    for expression in ("__import__('os')", "open('x')", "(1).real", "(1).__class__",
                       "[1, 2]", "a[0]", "lambda: 1", "abs(x=1)", "1 if 1 else 2",
                       "round"):
        with pytest.raises(CalcError):
            evaluate(expression, {"a": 1})


def test_rejects_huge_numbers() -> None:
    for expression in ("10 ** 5000000", "2 ** 3000000", "10 ** 100", "1e200", "9" * 120):
        with pytest.raises(CalcError, match="too|range|evaluate"):
            evaluate(expression)
    with pytest.raises(CalcError, match="too long"):
        evaluate("1+" * 600 + "1")


def test_run_calculate_envelope() -> None:
    text, err = calc.run_calculate({"expression": "a * 2", "variables": {"a": 1.5}})
    payload = json.loads(text)
    assert not err and payload["success"] and payload["result"] == "3"
    text, err = calc.run_calculate({"expression": "1 / 0"})
    payload = json.loads(text)
    assert err and payload["errorCode"] == "INVALID_INPUT" and "zero" in payload["error"]
    text, err = calc.run_calculate({"expression": "1", "variables": [1]})
    assert err


def test_registered_next_to_search_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pageindex import PageIndexClient, agent_tools

    from superindex import agent_search

    monkeypatch.setattr(agent_tools, "_tool_specs", agent_tools._tool_specs)
    agent_search.install()
    client = PageIndexClient(chat_model="openai/offline-test", storage_path=str(tmp_path))
    specs = {s[0]: s for s in agent_tools._tool_specs(client)}
    assert {"search_pages", "calculate"} <= set(specs)
    blocks, err = specs["calculate"][3]({"expression": "0.1 + 0.2"})
    assert not err and json.loads(blocks[0]["text"])["result"] == "0.3"
