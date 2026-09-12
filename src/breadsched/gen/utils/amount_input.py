"""Strict amount parsing helpers for external text formats.

Core :class:`Money` deliberately accepts only unambiguous numeric text.  Importers
that consume locale-shaped text should choose a decimal convention explicitly or
infer one from the complete source before parsing individual amounts.
"""

from __future__ import annotations

import locale
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Literal

NumberFormat = Literal["dot", "comma"]

__all__ = [
    "NumberFormat",
    "detect_number_format",
    "parse_decimal_amount",
    "parse_user_amount",
    "user_number_format",
]


def _unsigned_text(raw: str) -> str:
    text = raw.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    if text[:1] in {"+", "-"}:
        text = text[1:].strip()
    return text


def detect_number_format(values: Iterable[str]) -> NumberFormat | None:
    """Infer one decimal convention from a collection of amount strings.

    A separator followed by one or two digits is treated as decimal evidence.
    When both separators occur in one value, the rightmost separator is decimal.
    Values such as ``1,234`` are deliberately ambiguous and provide no evidence.
    Conflicting evidence raises ``ValueError`` rather than silently mixing formats.
    """
    evidence: set[NumberFormat] = set()
    for raw in values:
        text = _unsigned_text(raw).replace(" ", "")
        if not text:
            continue
        if "." in text and "," in text:
            evidence.add("dot" if text.rfind(".") > text.rfind(",") else "comma")
            continue
        separators: tuple[tuple[str, NumberFormat], ...] = ((".", "dot"), (",", "comma"))
        for separator, convention in separators:
            if separator not in text:
                continue
            tail = text.rsplit(separator, 1)[1]
            if tail.isdigit() and 1 <= len(tail) <= 2:
                evidence.add(convention)
    if len(evidence) > 1:
        raise ValueError("source contains conflicting decimal number formats")
    return next(iter(evidence), None)


def parse_decimal_amount(raw: str, number_format: NumberFormat) -> Decimal:
    """Parse one amount using an explicit decimal/grouping convention."""
    text = raw.strip()
    negative_parentheses = text.startswith("(") and text.endswith(")")
    if negative_parentheses:
        text = text[1:-1].strip()
    sign = ""
    if text[:1] in {"+", "-"}:
        sign, text = text[0], text[1:].strip()
    text = text.replace(" ", "")
    decimal_sep, grouping_sep = (".", ",") if number_format == "dot" else (",", ".")
    if text.count(decimal_sep) > 1:
        raise ValueError(f"invalid amount {raw!r}")
    if decimal_sep in text:
        integer, fraction = text.split(decimal_sep, 1)
        if not fraction or not fraction.isdigit():
            raise ValueError(f"invalid amount {raw!r}")
    else:
        integer, fraction = text, ""
    groups = integer.split(grouping_sep)
    if len(groups) > 1:
        if not groups[0] or any(len(group) != 3 for group in groups[1:]):
            raise ValueError(f"invalid amount {raw!r}")
    digits = "".join(groups)
    if not digits or not digits.isdigit():
        raise ValueError(f"invalid amount {raw!r}")
    normalized = f"{digits}.{fraction}" if fraction else digits
    if sign == "-" or negative_parentheses:
        normalized = f"-{normalized}"
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount {raw!r}") from exc


def user_number_format() -> NumberFormat:
    """Return the current process' preferred decimal convention for user input.

    Python may run with the neutral ``C`` numeric locale even when the desktop is
    localized, so callers should treat this only as the tie-breaker for genuinely
    ambiguous values.  Unambiguous comma- or period-decimal text is detected from
    the value itself by :func:`parse_user_amount`.
    """
    return "comma" if locale.localeconv().get("decimal_point") == "," else "dot"


def parse_user_amount(
    raw: str,
    number_format: NumberFormat | Literal["auto"] = "auto",
) -> Decimal:
    """Parse an amount typed by a person without weakening core ``Money`` parsing.

    ``auto`` accepts unambiguous period- or comma-decimal text and uses the current
    numeric locale only when a single value cannot identify its convention (for
    example ``1,234``).  A presentation that knows its locale, such as the web
    client, should pass an explicit convention so ambiguous grouping is stable.
    """
    raw = raw.strip().replace("$", "")
    selected = number_format
    if selected == "auto":
        selected = detect_number_format([raw]) or user_number_format()
    return parse_decimal_amount(raw, selected)
