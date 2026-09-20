"""Reporting- and transaction-currency identity."""

from __future__ import annotations

from ..db.sqlite import DbSQLite
from ..lib.commodity import DEFAULT_CURRENCY_HANDLE, Commodity

__all__ = ["book_currency", "reporting_currency_handle"]


def book_currency(db: DbSQLite) -> Commodity | None:
    """The configured reporting currency, then USD, then the first currency."""
    configured = db.get_metadata("default_currency")
    if isinstance(configured, str):
        found = db.get_commodity(configured) or db.get_commodity_by_mnemonic(configured)
        if found is not None and found.is_currency:
            return found
    usd = db.get_commodity_by_mnemonic("USD")
    if usd is not None and usd.is_currency:
        return usd
    return next((commodity for commodity in db.iter_commodities() if commodity.is_currency), None)


def reporting_currency_handle(db: DbSQLite) -> str:
    """Return the reporting tag, including the stable legacy-empty fallback."""
    currency = book_currency(db)
    return currency.handle if currency is not None else DEFAULT_CURRENCY_HANDLE
