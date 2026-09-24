"""The `calculate` tool: safe decimal arithmetic for the answering agent.

Evaluation is `avada-eval` (MIT; https://github.com/VoldemortGin/avada-eval, a
Decimal fork of simpleeval: `ast`-based, no eval/exec, resource limits built
in) through `avada_eval.llm.evaluate_for_llm`, with its finance functions and
its thousands-separator / percent preprocessing. This module only adds the
tool contract: variable checks, a 50-digit ROUND_HALF_UP context, ``^`` / ``×``
/ ``÷`` / ``−``, a numeric-result check, `rounded` and the substituted echo.

Numbers: ``1,234.5`` (comma with no spaces, valid grouping) is one number, so
function arguments need ``", "`` — ``max(1, 234)``; ``max(1,234)`` is
``max(1234)``. A number glued to ``%`` with no operand after it (``12.5%``,
``12.5% * 200``) is a percentage, 0.125; ``10 % 3`` / ``10%3`` is modulo.
"""
from __future__ import annotations

import ast
import json
from decimal import ROUND_HALF_UP, Context, Decimal, DecimalException, localcontext
from typing import Any

from avada_eval import DEFAULT_FUNCTIONS
from avada_eval.decimal_ops import decimal_sum
from avada_eval.llm import FINANCE_FUNCTIONS, TOOL_DESCRIPTION, evaluate_for_llm

TOOL_NAME = "calculate"
PRECISION = 50
MAX_EXPRESSION = 1000        # characters
ROUND_PLACES = 4

DESCRIPTION = (
    "Exact decimal calculator. Use it for EVERY arithmetic step: sums, differences, "
    "growth rates, shares/ratios, unit conversions — never compute in your head. "
    + TOOL_DESCRIPTION
    + " Name the figures you read via `variables` (e.g. {\"opat_2023\": 6610, "
    "\"opat_2022\": 6300}) and use the names in the expression; the reply echoes "
    "the substituted values."
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "expression": {
            "type": "string",
            "description": "Arithmetic expression, e.g. \"pct_change(opat_2023, opat_2022)\" "
                           "or \"(4034 - 3378) / 3378 * 100\".",
        },
        "variables": {
            "type": "object",
            "description": "Optional: named numbers used in the expression, e.g. "
                           "{\"opat_2023\": 6610, \"opat_2022\": 6300}.",
            "additionalProperties": {"type": "number"},
        },
    },
    "required": ["expression"],
}

GUIDANCE = (
    "CALCULATIONS:\n"
    "- Any addition, subtraction, multiplication, division, growth rate, share, "
    "difference or unit conversion MUST go through calculate(); never do arithmetic "
    "in your head.\n"
    "- First bring the figures to the same unit and currency (millions vs thousands, "
    "HK$ vs US$), then calculate. Show the formula with the figures in your answer.\n"
    "- In calculate() expressions put a space after every comma between function "
    "arguments: max(1, 234). A comma with no spaces is a thousands separator "
    "(1,234 is 1234)."
)

CONTEXT = Context(prec=PRECISION, rounding=ROUND_HALF_UP)
# sum(1, 2, 3) as in the tool description (avada's own sum takes one iterable)
FUNCTIONS: dict[str, Any] = {"sum": lambda *xs: decimal_sum(xs)}
FUNCTION_NAMES = frozenset(DEFAULT_FUNCTIONS) | frozenset(FINANCE_FUNCTIONS)
_ERROR_PREFIX = {
    "DivisionByZero": "division by zero",
    "SyntaxError": "invalid expression",
    "AssignmentAttempted": "invalid expression",
    "MultipleExpressions": "invalid expression",
}


class CalcError(ValueError):
    """An expression that cannot be evaluated; the message says why."""


def _number(value: Any, what: str) -> Decimal:
    if isinstance(value, bool):
        raise CalcError(f"{what}: expected a number, got {value!r}")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        try:
            number = Decimal(text[:-1]) / 100 if text.endswith("%") else Decimal(text)
        except DecimalException:
            raise CalcError(f"{what}: not a number: {value!r}") from None
        if number.is_finite():
            return number
    raise CalcError(f"{what}: expected a number, got {value!r}")


def _substituted(tree: ast.AST, variables: dict[str, Decimal]) -> str:
    class Sub(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name) -> ast.AST:
            if node.id in variables:
                text = _plain(variables[node.id])
                return ast.Name(id=f"({text})" if text.startswith("-") else text)
            return node

    return ast.unparse(Sub().visit(tree))


def _plain(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "0") else text


def rounded(value: Decimal) -> str:
    """`value` to `ROUND_PLACES` decimals, or 6 significant digits when that
    would round a non-zero value to 0."""
    places = Decimal(1).scaleb(-ROUND_PLACES)
    if value and abs(value) < places:
        places = Decimal(1).scaleb(value.adjusted() - 5)
    with localcontext(prec=max(PRECISION, value.adjusted() + ROUND_PLACES + 2)):
        return _plain(value.quantize(places))


def _preprocess(expression: str) -> str:
    text = expression.replace("×", "*").replace("÷", "/").replace("−", "-")
    return text.replace("^", "**")


def evaluate(expression: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate `expression`; raises `CalcError` with a readable message."""
    expression = str(expression or "").strip()
    if not expression:
        raise CalcError("expression is required")
    if len(expression) > MAX_EXPRESSION:
        raise CalcError(f"expression too long (max {MAX_EXPRESSION} characters)")
    names: dict[str, Decimal] = {}
    for name, value in (variables or {}).items():
        if not str(name).isidentifier() or str(name).startswith("_") \
                or str(name) in FUNCTION_NAMES:
            raise CalcError(f"bad variable name {name!r}: use letters, digits and "
                            "underscores, not starting with _ and not a function name")
        names[str(name)] = _number(value, f"variable {name!r}")

    try:
        out = evaluate_for_llm(_preprocess(expression), names, functions=FUNCTIONS,
                               context=CONTEXT)
    except Exception as exc:  # noqa: BLE001 — avada lets unexpected (e.g. beartype) errors through
        raise CalcError(f"cannot evaluate: {type(exc).__name__}: {exc}") from None
    if not out.ok:
        prefix = _ERROR_PREFIX.get(out.error_type or "", "cannot evaluate")
        raise CalcError(f"{prefix}: {out.error}")
    if out.value_type not in ("Decimal", "int"):
        raise CalcError(f"not a number: {out.value} ({out.value_type})")
    with localcontext(CONTEXT):
        value = +Decimal(out.value or "")  # to PRECISION significant digits
        result = {
            "expression": expression,
            "normalized": out.normalized,
            "result": _plain(value),
            "rounded": rounded(value),
        }
        if names:
            result["variables"] = {k: _plain(v) for k, v in names.items()}
            result["substituted"] = _substituted(ast.parse(out.normalized, mode="eval"),
                                                 names)
    return result


def run_calculate(arguments: dict[str, Any]) -> tuple[str, bool]:
    """Execute one `calculate` call; returns (JSON envelope, is_error)."""
    variables = arguments.get("variables")
    try:
        if variables is not None and not isinstance(variables, dict):
            raise CalcError("variables must be an object of name -> number")
        payload = {"success": True, **evaluate(arguments.get("expression") or "", variables)}
    except CalcError as exc:
        return json.dumps({
            "error": str(exc), "errorCode": "INVALID_INPUT",
            "next_steps": {"options": [
                "Fix the expression and call calculate again",
                ("Plain numbers only (no units); separate function arguments with "
                 "\", \"; put figures you read in `variables`")]},
        }, ensure_ascii=False), True
    return json.dumps(payload, ensure_ascii=False), False
