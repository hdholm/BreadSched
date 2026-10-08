"""Interpret what a person types into a register: dates, amounts, accounts, numbers.

GTK and web registers share these rules, so a shortcut means the same thing in
both. The functions are pure: they take the text, the value the field held, and
any context they need, and return the interpreted value or raise
:class:`EntryInputError`. Nothing here writes to a book.

Dates follow GnuCash's register shortcuts. Typed relative to the date the field
holds: ``+`` and ``-`` move a day (``+7``, ``--``), ``]`` and ``[`` a month,
``t`` is today, ``m``/``h`` the month's first and last day, and ``y``/``r`` the
year's. A bare day (``15``) stays in the field's month, a month/day (``3/15``) in
its year, and ``3/15/26`` or ``2026-03-15`` name a full date.

Amounts accept arithmetic (``12.50+3*2``, ``(100-20)/4``) through the safe formula
language. Each number is read with the entry's decimal convention before the
expression is evaluated, and a computed result is rounded half up to the
currency's smallest unit. A plain amount is read exactly as before.

Accounts complete segment by segment: ``Ex:Gr`` matches ``Expenses:Groceries``
because each typed segment begins the account's segment at the same depth.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterable
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Literal

from ..lib.formula import FormulaError, evaluate
from ..utils.amount_input import NumberFormat, detect_number_format, parse_user_amount

__all__ = [
    "DATE_KEYS",
    "EntryInputError",
    "complete_account",
    "is_arithmetic",
    "parse_entry_amount",
    "parse_entry_date",
    "step_num",
]

#: Single keys that change a date field in place rather than being typed.
DATE_KEYS = frozenset("+-=_[]tTmMhHyYrR")
_ACCOUNT_SEPARATOR = ":"
_MAX_TEXT = 200


class EntryInputError(ValueError):
    """Typed text that cannot be read as the field's value."""


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    if not 1 <= year <= 9999:
        raise EntryInputError("date out of range")
    last = calendar.monthrange(year, month + 1)[1]
    return date(year, month + 1, min(day.day, last))


def _repeat(text: str, symbol: str) -> int | None:
    """``+`` → 1, ``+++`` → 3, ``+7`` → 7; ``None`` when ``text`` is not that form."""
    if text and set(text) == {symbol}:
        return len(text)
    if len(text) > 1 and text[0] == symbol and text[1:].isdigit():
        return int(text[1:])
    return None


def _full_year(text: str, base: date) -> int:
    year = int(text)
    if len(text) <= 2:
        # A two-digit year is the one in the century nearest the field's date.
        century = base.year - base.year % 100
        year += century
        if year > base.year + 50:
            year -= 100
        elif year < base.year - 50:
            year += 100
    return year


def parse_entry_date(text: str, base: date, today: date | None = None) -> date:
    """Read ``text`` as a date, relative to ``base`` (the date the field held)."""
    today = today or date.today()
    raw = text.strip()
    if not raw:
        raise EntryInputError("enter a date")
    if len(raw) > _MAX_TEXT:
        raise EntryInputError("date text is too long")
    key = raw.lower()
    try:
        if key == "t":
            return today
        if key == "m":
            return base.replace(day=1)
        if key == "h":
            return base.replace(day=calendar.monthrange(base.year, base.month)[1])
        if key == "y":
            return base.replace(month=1, day=1)
        if key == "r":
            return base.replace(month=12, day=31)
        for symbols, unit in (("+=", 1), ("-_", -1)):
            for symbol in symbols:
                count = _repeat(raw, symbol)
                if count is not None:
                    return base + timedelta(days=unit * count)
        for symbol, unit in (("]", 1), ("[", -1)):
            count = _repeat(raw, symbol)
            if count is not None:
                return _add_months(base, unit * count)
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", raw):
            year, month, day = (int(part) for part in raw.split("-"))
            return date(year, month, day)
        if re.fullmatch(r"\d{1,2}", raw):
            return base.replace(day=int(raw))
        parts = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2}|\d{4}))?", raw)
        if parts:
            year = _full_year(parts[3], base) if parts[3] else base.year
            return date(year, int(parts[1]), int(parts[2]))
    except (OverflowError, ValueError) as exc:
        if isinstance(exc, EntryInputError):
            raise
        raise EntryInputError(f"no such date: {raw}") from exc
    raise EntryInputError(f"not a date: {raw}")


_NUMBER = re.compile(r"\d[\d.,]*|[.,]\d+")
_OPERATORS = set("+-*/() ")


def _is_plain(text: str) -> bool:
    """A single signed or parenthesised number, read exactly without the formula."""
    inner = text
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1].strip()
    if inner[:1] in "+-":
        inner = inner[1:].strip()
    return _NUMBER.fullmatch(inner.replace(" ", "")) is not None


def is_arithmetic(text: str) -> bool:
    """Whether ``text`` is a calculation rather than one typed number."""
    raw = text.strip().replace("$", "")
    return bool(raw) and not _is_plain(raw)


def parse_entry_amount(
    text: str,
    number_format: NumberFormat | Literal["auto"] = "auto",
    fraction: int = 100,
) -> Decimal | None:
    """Read an amount, evaluating arithmetic; ``None`` for a blank field.

    A computed result is rounded half up to ``1/fraction`` (the currency's
    smallest unit); a single typed number keeps every digit given.
    """
    raw = text.strip().replace("$", "")
    if not raw:
        return None
    if len(raw) > _MAX_TEXT:
        raise EntryInputError("amount text is too long")
    try:
        if _is_plain(raw):
            return parse_user_amount(raw, number_format)
        numbers = _NUMBER.findall(raw)
        if not numbers or set(_NUMBER.sub("", raw)) - _OPERATORS:
            raise EntryInputError(f"not an amount: {text.strip()}")
        selected = number_format
        if selected == "auto":
            try:
                selected = detect_number_format(numbers) or "auto"
            except ValueError as exc:
                raise EntryInputError("mixed decimal separators") from exc
        expression = _NUMBER.sub(lambda m: str(parse_user_amount(m[0], selected)), raw)
        result = evaluate(expression)
    except FormulaError as exc:
        raise EntryInputError(f"cannot calculate {text.strip()}") from exc
    except EntryInputError:
        raise
    except ValueError as exc:
        raise EntryInputError(f"not an amount: {text.strip()}") from exc
    if not result.is_finite():
        raise EntryInputError(f"cannot calculate {text.strip()}")
    try:
        # A large result needs more digits than the default context to round.
        with localcontext() as context:
            context.prec = 64
            unit = Decimal(1) / Decimal(max(1, fraction))
            return (result / unit).quantize(Decimal(1), rounding=ROUND_HALF_UP) * unit
    except ArithmeticError as exc:
        raise EntryInputError(f"cannot calculate {text.strip()}") from exc


def complete_account(text: str, names: Iterable[str]) -> list[str]:
    """Full account names that ``text`` completes to, best first.

    Every ``:``-separated segment typed must begin (ignoring case) the account's
    segment at the same depth. Accounts exactly as deep as the text come first,
    then deeper ones; each group is in name order.
    """
    typed = [segment.strip().casefold() for segment in text.split(_ACCOUNT_SEPARATOR)]
    if not text.strip():
        return []
    found: list[tuple[int, str, str]] = []
    for name in names:
        segments = name.split(_ACCOUNT_SEPARATOR)
        if len(segments) < len(typed):
            continue
        if all(segments[index].casefold().startswith(part) for index, part in enumerate(typed)):
            found.append((len(segments) != len(typed), name.casefold(), name))
    return [name for *_key, name in sorted(found)]


def step_num(text: str, step: int, latest: str = "") -> str:
    """The check number ``step`` away: ``101`` → ``102``, keeping leading zeros.

    An empty field steps from ``latest`` (the register's last number). Text with
    no trailing digits is returned unchanged, and a number never goes below zero.
    """
    current = text.strip() or latest.strip()
    match = re.fullmatch(r"(.*?)(\d+)", current)
    if match is None:
        return text
    prefix, digits = match.groups()
    value = max(0, int(digits) + step)
    return f"{prefix}{value:0{len(digits)}d}"
