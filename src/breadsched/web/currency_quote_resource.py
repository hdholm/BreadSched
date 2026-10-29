"""HTTP input and output for manual currency quotes.

The quote is checked and saved by the book server, ``api.book`` (#354).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any

from ..gen.lib import Money
from ..gen.services.exchange_rates import SaveExchangeRate

if TYPE_CHECKING:
    from .context import Api


def save_currency_quote(api: Api, payload: Mapping[str, Any]) -> dict[str, str]:
    """Parse web fields; the shared service validates and writes the entire quote."""
    source = payload.get("from")
    target = payload.get("to")
    raw_date = payload.get("date")
    raw_rate = payload.get("rate")
    if not isinstance(source, str) or not isinstance(target, str):
        raise ValueError("choose source and target currencies")
    if not isinstance(raw_date, str):
        raise ValueError("quote date is invalid")
    try:
        quote_date = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise ValueError("quote date is invalid") from exc
    if not isinstance(raw_rate, str):
        raise ValueError("rate must be an exact number entered as text")
    try:
        rate = Money(raw_rate)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("rate must be a valid exact number") from exc
    quote = api.book.save_exchange_rate(SaveExchangeRate(source, target, quote_date, rate))
    return {
        "handle": quote.handle,
        "from": source,
        "to": target,
        "date": quote_date.isoformat(),
        "rate": f"{quote.value.numerator}/{quote.value.denominator}",
        "source": quote.source,
    }
