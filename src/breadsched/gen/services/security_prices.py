"""Securities and their manual prices: the list with each latest quote, and one save.

CLI, GTK, and web adapters call these functions. ``engine.valuation`` writes the
quote (creating the security, or a US dollar to quote it in, when needed); this
module checks the request first and names each refusal with a stable code, so a
refused price leaves the book unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import valuation
from ..lib.commodity import Commodity, CommodityPrice
from ..lib.money import Money
from .contracts import ServiceError, ServiceResult

__all__ = [
    "SaveSecurityPrice",
    "SavedSecurityPrice",
    "SecurityQuote",
    "save_security_price",
    "securities",
]

_CURRENCY_NAMESPACES = {"CURRENCY", "ISO4217"}


@dataclass(frozen=True, slots=True)
class SecurityQuote:
    """One security with its latest exact dated price (``None`` when it has none)."""

    security: Commodity
    latest: CommodityPrice | None


@dataclass(frozen=True, slots=True)
class SaveSecurityPrice:
    """One dated quote for ``security``; without one, a new security is created.

    ``mnemonic``, ``fullname``, ``namespace``, and ``fraction`` describe the new
    security and are ignored for an existing one. Without ``currency`` the quote is
    in US dollars.
    """

    security: str | None
    currency: str | None
    quote_date: date
    value: Money
    mnemonic: str = ""
    fullname: str = ""
    namespace: str = "FUND"
    fraction: int = 10000


@dataclass(frozen=True, slots=True)
class SavedSecurityPrice:
    security: Commodity
    price: CommodityPrice
    currency: Commodity


def securities(db: DbSQLite) -> tuple[SecurityQuote, ...]:
    """Every security (not currency), in book order, with its latest quote."""
    return tuple(
        SecurityQuote(item, valuation.latest_price(db, item))
        for item in db.iter_commodities()
        if not item.is_currency
    )


def _refusals(db: DbSQLite, request: SaveSecurityPrice) -> list[ServiceError]:
    errors = []
    if request.security is not None:
        found = db.get_commodity(request.security)
        if found is None:
            errors.append(ServiceError("security.not_found", ("security",)))
        elif found.is_currency:
            errors.append(ServiceError("security.is_currency", ("security",)))
    if request.value <= 0:
        errors.append(ServiceError("security.price.invalid", ("value",)))
    if request.currency:
        currency = db.get_commodity(request.currency)
        if currency is None or not currency.is_currency:
            errors.append(ServiceError("security.currency.invalid", ("currency",)))
    if request.security is None:
        symbol = request.mnemonic.strip().upper()
        if not symbol:
            errors.append(ServiceError("security.symbol.required", ("mnemonic",)))
        elif db.get_commodity_by_mnemonic(symbol) is not None:
            errors.append(ServiceError("security.symbol.duplicate", ("mnemonic",)))
        if (request.namespace.strip().upper() or "FUND") in _CURRENCY_NAMESPACES:
            errors.append(ServiceError("security.namespace.invalid", ("namespace",)))
        if request.fraction < 1:
            errors.append(ServiceError("security.fraction.invalid", ("fraction",)))
    return errors


def save_security_price(
    db: DbSQLite, request: SaveSecurityPrice
) -> ServiceResult[SavedSecurityPrice]:
    """Save a manual quote; a later entry for the same security, currency, and date
    replaces it."""
    errors = _refusals(db, request)
    if errors:
        return ServiceResult.failure(*errors)
    security, price = valuation.save_security_price(
        db,
        security_handle=request.security,
        currency_handle=request.currency,
        quote_date=request.quote_date,
        value=request.value,
        mnemonic=request.mnemonic,
        fullname=request.fullname,
        namespace=request.namespace,
        fraction=request.fraction,
    )
    currency = db.get_commodity(price.currency)
    assert currency is not None
    return ServiceResult.success(SavedSecurityPrice(security, price, currency))
