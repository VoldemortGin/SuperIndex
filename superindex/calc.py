"""The `calculate` tool: safe decimal arithmetic for the answering agent.

Evaluation is `simpleeval` (MIT; `ast`-based, no eval/exec) restricted to
arithmetic: numbers, ``+ - * / // % **`` (``^`` too), parentheses, unary
signs, the functions in `FUNCTIONS` and caller-named `variables`; attribute
access, subscripts, keywords, comparisons, strings and unknown names are
rejected, and simpleeval's own `MAX_POWER` guard stays in force. Number
literals are read as `decimal.Decimal` (50 significant digits, results below
1e100), so 0.1 + 0.2 is 0.3.

Numbers: no thousands separators inside the expression — ``max(1,234)`` is
two arguments, so a comma is never read as grouping (a top-level comma is
reported as an error that says so). Variable values may carry them
(``"1,234.5"``). A number directly followed by ``%`` and no operand
(``12.5%``, ``12.5% * 200``) is a percentage, 0.125; ``10 % 3`` is modulo.
"""
from __future__ import annotations

import ast
import json
import operator
import re
from decimal import (
    ROUND_HALF_UP,
    Context,
    Decimal,
    DecimalException,
    Overflow,
    localcontext,
)
from typing import Any

import simpleeval

TOOL_NAME = "calculate"
PRECISION = 50
MAX_EXPRESSION = 1000        # characters
EXP_LIMIT = 99               # |any value| < 1e100 (Decimal Overflow beyond)
ROUND_PLACES = 4

DESCRIPTION = (
    "Exact decimal calculator. Use it for EVERY arithmetic step: sums, differences, "
    "growth rates, shares/ratios, unit conversions — never compute in your head. "
    "Supports + - * / // % ** (or ^), parentheses, round(x, n), abs, min, max, sum, "
    "avg, pct_change(new, old) (percent change, in %), cagr(end, start, years) "
    "(compound annual growth rate, in %), ratio(a, b). Write numbers WITHOUT "
    "thousands separators (6610, not 6,610); 12.5% means 0.125. Name the figures "
    "you read via `variables` (e.g. {\"opat_2023\": 6610, \"opat_2022\": 6300}) and "
    "use the names in the expression; the reply echoes the substituted values."
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
    "HK$ vs US$), then calculate. Show the formula with the figures in your answer."
)


class CalcError(ValueError):
    """An expression that cannot be evaluated; the message says why."""


def _context() -> Context:
    return Context(prec=PRECISION, rounding=ROUND_HALF_UP, Emax=EXP_LIMIT,
                   Emin=-EXP_LIMIT)


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


# ───────────────────────────────────────────────────────────── functions
def _avg(*xs: Decimal) -> Decimal:
    return sum(xs, Decimal(0)) / len(xs)


def _round(x: Decimal, places: Decimal = Decimal(0)) -> Decimal:
    if places != places.to_integral_value() or not 0 <= places <= 20:
        raise CalcError("round(x, n): n must be a whole number from 0 to 20")
    return x.quantize(Decimal(1).scaleb(-int(places)))


def _nonzero(x: Decimal, what: str) -> Decimal:
    if x == 0:
        raise CalcError(f"division by zero ({what} is 0)")
    return x


def _pct_change(new: Decimal, old: Decimal) -> Decimal:
    return (new - old) / abs(_nonzero(old, "old")) * 100


def _cagr(end: Decimal, start: Decimal, years: Decimal) -> Decimal:
    if years <= 0:
        raise CalcError("cagr(end, start, years): years must be positive")
    growth = end / _nonzero(start, "start")
    if growth <= 0:
        raise CalcError("cagr(end, start, years): end and start must have the same sign")
    return (growth ** (1 / years) - 1) * 100


def _ratio(a: Decimal, b: Decimal) -> Decimal:
    return a / _nonzero(b, "b")


FUNCTIONS: dict[str, Any] = {
    "round": _round,
    "abs": abs,
    "min": min,
    "max": max,
    "sum": lambda *xs: sum(xs, Decimal(0)),
    "avg": _avg,
    "pct_change": _pct_change,
    "cagr": _cagr,
    "ratio": _ratio,
}
OPERATORS: dict[type, Any] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: simpleeval.safe_power, ast.USub: operator.neg, ast.UAdd: operator.pos,
}
NODES = (ast.Expr, ast.Name, ast.UnaryOp, ast.BinOp, ast.Call, ast.Constant)


# ───────────────────────────────────────────────────────────── evaluation
_PERCENT_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)%(?!\s*[\w.(])")
_GROUPED_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")


def _preprocess(expression: str) -> str:
    text = expression.replace("×", "*").replace("÷", "/").replace("−", "-")
    text = text.replace("^", "**")
    return _PERCENT_RE.sub(r"(\1/100)", text)


class _Evaluator(simpleeval.SimpleEval):
    """simpleeval limited to `NODES`, with number literals as Decimal."""

    def __init__(self, variables: dict[str, Decimal]) -> None:
        super().__init__(operators=OPERATORS, functions=FUNCTIONS, names=variables)
        self.nodes = {k: v for k, v in self.nodes.items() if k in NODES}
        self.nodes[ast.Constant] = self._decimal

    def _decimal(self, node: ast.Constant) -> Decimal:
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalcError(f"unsupported constant: {node.value!r}")
        # the literal's own text, so no binary-float rounding creeps in
        text = ast.get_source_segment(self.expr, node) or repr(node.value)
        return Decimal(text.replace("_", ""))


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
    return _plain(value.quantize(places))


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
                or str(name) in FUNCTIONS:
            raise CalcError(f"bad variable name {name!r}: use letters, digits and "
                            "underscores, not starting with _ and not a function name")
        names[str(name)] = _number(value, f"variable {name!r}")

    source = _preprocess(expression)
    with localcontext(_context()):
        try:
            tree = ast.parse(source, mode="eval")
            value = _Evaluator(names).eval(source, previously_parsed=tree.body)
            if not isinstance(value, Decimal):
                raise CalcError(f"not a number: {value!r}")
            value = value.normalize()
        except CalcError:
            raise
        except SyntaxError as exc:
            raise CalcError(f"invalid expression: {exc.msg}") from None
        except ZeroDivisionError:
            raise CalcError("division by zero") from None
        except Overflow:
            raise CalcError(f"result out of range (|value| < 1e{EXP_LIMIT + 1})") from None
        except simpleeval.FeatureNotAvailable as exc:
            hint = " — write numbers without thousands separators (6610, not 6,610)" \
                if "Tuple" in str(exc) and _GROUPED_RE.search(expression) else ""
            raise CalcError(f"not allowed: {exc}{hint}") from None
        except (simpleeval.InvalidExpression, DecimalException, TypeError, ValueError,
                RecursionError, MemoryError) as exc:
            raise CalcError(f"cannot evaluate: {type(exc).__name__}: {exc}") from None
        result = {
            "expression": expression,
            "normalized": ast.unparse(tree),
            "result": _plain(value),
            "rounded": rounded(value),
        }
    if names:
        result["variables"] = {k: _plain(v) for k, v in names.items()}
        result["substituted"] = _substituted(tree, names)
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
                ("Plain numbers only (no units, no thousands separators); "
                 "put figures you read in `variables`")]},
        }, ensure_ascii=False), True
    return json.dumps(payload, ensure_ascii=False), False
