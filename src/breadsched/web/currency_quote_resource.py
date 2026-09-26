"""HTTP input and output for manual currency quotes."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import valuation
from ..gen.lib import Money


def save_currency_quote(db: DbSQLite, payload: Mapping[str, Any]) -> dict[str, str]:
    """Parse web fields; let shared valuation validate and write the entire quote."""
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
    quote = valuation.save_currency_quote(
        db,
        source_handle=source,
        target_handle=target,
        quote_date=quote_date,
        value=rate,
    )
    return {
        "handle": quote.handle,
        "from": source,
        "to": target,
        "date": quote_date.isoformat(),
        "rate": f"{quote.value.numerator}/{quote.value.denominator}",
        "source": quote.source,
    }
