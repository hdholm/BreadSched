"""Manual exchange rates: the book's currencies and one dated, directional quote.

CLI, GTK, and web adapters call these functions. ``engine.valuation`` writes the
quote; this module checks the request first and names each refusal with a stable
code, so a refused rate leaves the book unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import valuation
from ..lib.commodity import CommodityPrice
from ..lib.money import Money
from .contracts import ServiceError, ServiceResult

__all__ = [
    "CurrencyChoice",
    "SaveExchangeRate",
    "currencies",
    "find_currency",
    "save_exchange_rate",
]


@dataclass(frozen=True, slots=True)
class CurrencyChoice:
    handle: str
    mnemonic: str
    fullname: str


@dataclass(frozen=True, slots=True)
class SaveExchangeRate:
    """``rate`` target units per one ``source`` unit on ``quote_date``."""

    source: str
    target: str
    quote_date: date
    rate: Money


def currencies(db: DbSQLite, book_order: bool = False) -> tuple[CurrencyChoice, ...]:
    """The book's currencies, by mnemonic; in the order the book holds them with
    ``book_order`` (the first is an editor's default quote currency)."""
    held = list(db.iter_commodities())
    if not book_order:
        held.sort(key=lambda item: item.mnemonic)
    return tuple(
        CurrencyChoice(item.handle, item.mnemonic, item.fullname)
        for item in held
        if item.is_currency
    )


def find_currency(db: DbSQLite, reference: str) -> ServiceResult[CurrencyChoice]:
    """A currency by exact handle or unambiguous mnemonic."""
    direct = db.get_commodity(reference) if reference else None
    if direct is not None:
        if not direct.is_currency:
            return ServiceResult.failure(ServiceError("currency.not_currency", ("currency",)))
        return ServiceResult.success(
            CurrencyChoice(direct.handle, direct.mnemonic, direct.fullname)
        )
    matches = [item for item in currencies(db) if item.mnemonic.upper() == reference.upper()]
    if not matches:
        return ServiceResult.failure(ServiceError("currency.not_found", ("currency",)))
    if len(matches) > 1:
        return ServiceResult.failure(ServiceError("currency.reference.ambiguous", ("currency",)))
    return ServiceResult.success(matches[0])


def save_exchange_rate(db: DbSQLite, request: SaveExchangeRate) -> ServiceResult[CommodityPrice]:
    """Save a manual quote; a later entry for the same pair and date replaces it."""
    source = db.get_commodity(request.source)
    target = db.get_commodity(request.target)
    if source is None or not source.is_currency:
        return ServiceResult.failure(ServiceError("rate.source.invalid", ("source",)))
    if target is None or not target.is_currency:
        return ServiceResult.failure(ServiceError("rate.target.invalid", ("target",)))
    if source.handle == target.handle:
        return ServiceResult.failure(ServiceError("rate.currency.same", ("target",)))
    if request.rate <= 0:
        return ServiceResult.failure(ServiceError("rate.value.invalid", ("rate",)))
    quote = valuation.save_currency_quote(
        db,
        source_handle=source.handle,
        target_handle=target.handle,
        quote_date=request.quote_date,
        value=request.rate,
    )
    return ServiceResult.success(quote)
