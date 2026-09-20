"""A very small, safe arithmetic evaluator.

Scheduled transactions and scenario assumptions both let a user type an expression
such as ``balance * rate / 12``.  Running that through :func:`eval` would hand
arbitrary code execution to anyone who can send you a book file, so instead the
expression is parsed with :mod:`ast` and only a whitelist of node types is walked.
Numbers are :class:`~decimal.Decimal` throughout, so no binary rounding creeps in.
"""

from __future__ import annotations

import ast
import operator
import re
from collections.abc import Callable
from decimal import Context, Decimal, DecimalException, getcontext, localcontext
from typing import Any

from . import finance

__all__ = ["evaluate", "normalise", "FormulaError", "FUNCTIONS"]

#: A comma sitting between digits, with three digits and no more after it.
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_MAX_RAW_LENGTH = 4096
_MAX_NORMALISED_LENGTH = 4096
_MAX_NODES = 256
_MAX_DEPTH = 32
_MAX_POWER_ABS = Decimal("1000")
_FORMULA_CONTEXT = Context(prec=64, Emin=-999, Emax=999)


class FormulaError(ValueError):
    """Raised for an unparseable, unsafe, or unresolvable expression."""


_BINARY: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}

_UNARY: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


#: Functions a formula may call. Whitelisted by name, so a formula still cannot
#: reach anything that was not deliberately offered to it.
#:
#: These exist because GnuCash's loan assistant writes scheduled transactions whose
#: splits are ``pmt``, ``ipmt`` and ``ppmt`` expressions. Without them, every
#: mortgage payment in an imported book evaluates to nothing, and a forecast built
#: on that quietly says the loan costs the household zero.
def _magnitude(function: Callable[..., Any]) -> Callable[..., Any]:
    """Present a loan payment as a positive amount.

    The :mod:`~breadsched.gen.lib.finance` functions use the spreadsheet sign
    convention, where a payment you make is negative. GnuCash's formula language
    does not: its templates put ``ipmt(...)`` straight into a debit slot and expect
    a positive number. Since these names are reached through *formulas*, they
    follow the formula language's convention, and the module keeps the
    spreadsheet's.
    """

    def wrapper(*arguments: Any) -> Any:
        return -function(*arguments)

    wrapper.__name__ = function.__name__
    wrapper.__doc__ = function.__doc__
    return wrapper


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "pmt": _magnitude(finance.pmt),
    "ipmt": _magnitude(finance.ipmt),
    "ppmt": _magnitude(finance.ppmt),
    "fv": finance.fv,
    "pv": finance.pv,
    "nper": finance.nper,
    "abs": abs,
    "min": min,
    "max": max,
}


def _to_decimal(value: Any) -> Decimal:
    if hasattr(value, "decimal") and isinstance(value.decimal, Decimal):  # a Rate
        value = value.decimal
    elif hasattr(value, "rate"):  # a Money
        value = value.rate()
    elif isinstance(value, float):
        value = str(value)
    elif not isinstance(value, (Decimal, int, str)):
        raise FormulaError(f"cannot use {value!r} in a formula")
    try:
        result = getcontext().create_decimal(value)
    except (DecimalException, ValueError, TypeError, OverflowError) as exc:
        raise FormulaError(f"cannot use {value!r} in a formula") from exc
    if not result.is_finite():
        raise FormulaError("formula values must be finite")
    return result


def _walk(node: ast.AST, variables: dict[str, Any]) -> Decimal:
    if isinstance(node, ast.Expression):
        return _walk(node.body, variables)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or node.value is None:
            raise FormulaError("only numbers are allowed in formulas")
        return _to_decimal(node.value)
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise FormulaError(f"unknown variable {node.id!r}")
        return _to_decimal(variables[node.id])
    if isinstance(node, ast.BinOp):
        binary_func = _BINARY.get(type(node.op))
        if binary_func is None:
            raise FormulaError(f"operator {type(node.op).__name__} is not allowed")
        left, right = _walk(node.left, variables), _walk(node.right, variables)
        if isinstance(node.op, ast.Div) and right == 0:
            raise FormulaError("division by zero")
        if isinstance(node.op, ast.Pow):
            if abs(right) > _MAX_POWER_ABS:
                raise FormulaError("power exponent is too large")
            return left**right
        return binary_func(left, right)
    if isinstance(node, ast.UnaryOp):
        unary_func = _UNARY.get(type(node.op))
        if unary_func is None:
            raise FormulaError("only + and - may be used as unary operators")
        return unary_func(_walk(node.operand, variables))
    if isinstance(node, ast.Call):
        name = getattr(node.func, "id", None)
        if name not in FUNCTIONS:
            known = ", ".join(sorted(FUNCTIONS))
            raise FormulaError(f"unknown function {name or '?'!r}; formulas may call: {known}")
        if node.keywords:
            raise FormulaError("formula functions take positional arguments only")
        arguments = [_walk(argument, variables) for argument in node.args]
        try:
            return _to_decimal(FUNCTIONS[name](*arguments))
        except FormulaError:
            raise
        except (ValueError, ArithmeticError, TypeError) as exc:
            raise FormulaError(f"{name}(): {exc}") from exc
    raise FormulaError(f"{type(node).__name__} is not allowed in a formula")


def _uses_gnucash_argument_syntax(expression: str) -> bool:
    depth = 0
    for character in expression:
        if character in "([":
            depth += 1
        elif character in ")]":
            depth = max(0, depth - 1)
        elif character == ":" and depth > 0:
            return True
    return False


def normalise(expression: str) -> str:
    """Rewrite GnuCash's formula dialect into one Python can parse.

    GnuCash loan formulas use colons between function arguments and may group
    digits with commas, for example ``399,200.00``.  Ordinary Python-style
    formulas already use commas as argument separators, so grouping commas are
    stripped only when a colon-delimited GnuCash call is actually present.
    """
    if len(expression) > _MAX_RAW_LENGTH:
        raise FormulaError("formula text is too long")
    if _uses_gnucash_argument_syntax(expression):
        expression = _THOUSANDS.sub("", expression)
    out: list[str] = []
    depth = 0
    for character in expression:
        if character in "([":
            depth += 1
        elif character in ")]":
            depth = max(0, depth - 1)
        out.append("," if character == ":" and depth > 0 else character)
    result = "".join(out)
    if len(result) > _MAX_NORMALISED_LENGTH:
        raise FormulaError("normalised formula is too long")
    return result


def _check_complexity(tree: ast.AST) -> None:
    count = 0
    stack: list[tuple[ast.AST, int]] = [(tree, 1)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > _MAX_NODES:
            raise FormulaError("formula is too complex")
        if depth > _MAX_DEPTH:
            raise FormulaError("formula is nested too deeply")
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))


def evaluate(expression: str, variables: dict[str, Any] | None = None) -> Decimal:
    """Evaluate an arithmetic expression over ``variables``."""
    try:
        normalised = normalise(expression)
        if not normalised.strip():
            return Decimal(0)
        with localcontext(_FORMULA_CONTEXT):
            tree = ast.parse(normalised, mode="eval")
            _check_complexity(tree)
            return _walk(tree, variables or {})
    except FormulaError:
        raise
    except SyntaxError as exc:
        raise FormulaError(f"cannot parse {expression!r}: {exc.msg}") from exc
    except (
        DecimalException,
        ArithmeticError,
        MemoryError,
        TypeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise FormulaError(f"cannot evaluate {expression!r}: {exc}") from exc
