"""Security price quotes read from QIF and OFX files.

A quote is imported through the same contract as a manual one: it must name a
security already in the book (matched by its symbol), be quoted in a currency the
book knows, and be greater than zero. It is stored as ordinary price evidence with
its own source ("qif" or "ofx"), so it never replaces a quote entered in
BreadSched. A quote for an unknown security or currency is reported as skipped;
the importer never invents a security to hold it.
"""

from __future__ import annotations

from datetime import date
from uuid import NAMESPACE_URL, uuid5

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.commodity import Commodity
from ...gen.lib.money import Money
from .gnucash_common import ImportSink

__all__ = ["record_security_quote", "security_by_symbol"]


def security_by_symbol(db: DbSQLite, symbol: str) -> Commodity | None:
    """The one non-currency commodity whose mnemonic is ``symbol``, if unique."""
    wanted = symbol.strip().casefold()
    if not wanted:
        return None
    matches = [
        item
        for item in db.iter_commodities()
        if not item.is_currency and item.mnemonic.casefold() == wanted
    ]
    return matches[0] if len(matches) == 1 else None


def _currency_by_code(db: DbSQLite, code: str) -> Commodity | None:
    wanted = code.strip().casefold()
    matches = [
        item
        for item in db.iter_commodities()
        if item.is_currency and item.mnemonic.casefold() == wanted
    ]
    return matches[0] if len(matches) == 1 else None


def record_security_quote(
    sink: ImportSink,
    db: DbSQLite,
    *,
    source: str,
    symbol: str,
    quote_date: date,
    value: Money,
    currency: str | None = None,
    currency_code: str | None = None,
) -> bool:
    """Store one dated quote for a known security; False (and a skip) otherwise.

    ``currency`` is a commodity handle; ``currency_code`` a mnemonic such as
    ``"USD"``, resolved against the book's currencies. Re-importing the same
    quote updates it in place: its handle depends only on the source, security,
    currency, and date.
    """
    result = sink.result
    identity = f"{source}:{symbol}:{quote_date.isoformat()}"
    security = security_by_symbol(db, symbol)
    if security is None:
        result.skip(
            f"{source.upper()} price for a security not in the book",
            symbol or "(no symbol)",
            identity=identity,
            kind="price",
        )
        return False
    if currency is None and currency_code:
        found = _currency_by_code(db, currency_code)
        currency = found.handle if found is not None else None
    if currency is None:
        result.skip(
            f"{source.upper()} price in a currency not in the book",
            f"{symbol} ({currency_code or 'unknown currency'})",
            identity=identity,
            kind="price",
        )
        return False
    if value <= 0:
        result.skip(
            f"{source.upper()} price must be greater than zero",
            symbol,
            identity=identity,
            kind="price",
        )
        return False
    handle = uuid5(
        NAMESPACE_URL,
        f"breadsched:{source}:price:{security.handle}:{currency}:{quote_date.isoformat()}",
    ).hex
    return (
        sink.price(
            handle, security.handle, currency, quote_date, value, source=source, quote_type="last"
        )
        is not None
    )
