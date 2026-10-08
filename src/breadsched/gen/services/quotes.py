"""Store online quotes for the commodities that ask for them.

A commodity asks for online quotes through its ``quote_source``: ``tsp`` (Thrift
Savings Plan funds), ``alphavantage`` (securities, with the user's key),
``currency`` (exchange rates, from the European Central Bank), or any other
Finance::Quote method name. GnuCash import carries the source GnuCash used.

This service decides what to ask for and stores what comes back; it never touches
the network. The caller passes a :class:`QuoteFetcher` (``plugins.quotes`` builds
the real one), so every interface fetches the same way and tests run offline.

Every stored quote is an ordinary dated price whose ``source`` names where it came
from (``Online: tsp.gov``, for example), so valuation shows its provenance and
date like any other quote. A quote already stored for the same commodity, currency,
date, and source is updated rather than duplicated. A source that fails is reported
per commodity and changes nothing; manual and imported prices stay in use.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from ..db.sqlite import DbSQLite
from ..engine.currency import reporting_currency_handle
from ..lib.commodity import CommodityPrice
from ..lib.money import Money
from .contracts import ServiceError, ServiceResult

__all__ = [
    "NATIVE_SOURCES",
    "FetchedQuote",
    "QuoteFailure",
    "QuoteFetcher",
    "QuoteRequest",
    "QuoteUpdate",
    "SetQuoteSource",
    "StoredQuote",
    "quote_requests",
    "set_quote_source",
    "update_quotes",
]

#: Sources BreadSched fetches itself; any other name goes to Finance::Quote.
NATIVE_SOURCES = ("tsp", "alphavantage", "currency")
_PREFIX = "Online: "


@dataclass(frozen=True, slots=True)
class QuoteRequest:
    commodity: str
    symbol: str
    source: str


@dataclass(frozen=True, slots=True)
class FetchedQuote:
    symbol: str
    source: str
    value: Decimal
    #: Currency mnemonic the value is in.
    currency: str
    when: date
    #: Who supplied it, for the stored price's provenance (``tsp.gov``).
    origin: str


@dataclass(frozen=True, slots=True)
class QuoteFailure:
    symbol: str
    source: str
    reason: str


class QuoteFetcher(Protocol):
    def fetch(
        self, requests: Sequence[QuoteRequest], reporting_currency: str
    ) -> tuple[list[FetchedQuote], list[QuoteFailure]]: ...


@dataclass(frozen=True, slots=True)
class StoredQuote:
    commodity: str
    symbol: str
    value: Money
    currency: str
    when: date
    source: str
    #: ``False`` when an identical price was already stored.
    changed: bool


@dataclass(frozen=True, slots=True)
class QuoteUpdate:
    stored: tuple[StoredQuote, ...]
    failures: tuple[QuoteFailure, ...]
    #: Nothing was written: the update only previewed what it would store.
    dry_run: bool


@dataclass(frozen=True, slots=True)
class SetQuoteSource:
    commodity: str
    #: Empty turns online quotes off for the commodity.
    source: str


def quote_requests(db: DbSQLite) -> list[QuoteRequest]:
    """Every commodity with a quote source, except the reporting currency itself."""
    reporting = reporting_currency_handle(db)
    requests = []
    for commodity in db.iter_commodities():
        source = commodity.quote_source.strip()
        if not source or commodity.handle == reporting:
            continue
        if commodity.is_currency and source != "currency":
            continue  # GnuCash marks currencies "currency"; nothing else applies
        requests.append(QuoteRequest(commodity.handle, commodity.mnemonic, source))
    return sorted(requests, key=lambda item: (item.source, item.symbol))


def update_quotes(
    db: DbSQLite, fetcher: QuoteFetcher, *, dry_run: bool = False
) -> ServiceResult[QuoteUpdate]:
    """Fetch and store a quote for every commodity that asks for one."""
    requests = quote_requests(db)
    reporting = db.get_commodity(reporting_currency_handle(db))
    if reporting is None:
        return ServiceResult.failure(ServiceError("quotes.currency.not_found", ("currency",)))
    if not requests:
        return ServiceResult.success(QuoteUpdate((), (), dry_run))
    fetched, failures = fetcher.fetch(requests, reporting.mnemonic)
    by_key = {(item.symbol, item.source): item for item in requests}
    currencies = {item.mnemonic: item for item in db.iter_commodities() if item.is_currency}
    stored: list[StoredQuote] = []
    problems = list(failures)
    plans: list[tuple[CommodityPrice, CommodityPrice | None]] = []
    for quote in fetched:
        request = by_key.get((quote.symbol, quote.source))
        if request is None:
            continue
        currency = currencies.get(quote.currency)
        if currency is None:
            reason = f"currency {quote.currency} is not in the book"
            problems.append(QuoteFailure(quote.symbol, quote.source, reason))
            continue
        if quote.value <= 0 or not quote.value.is_finite():
            problems.append(QuoteFailure(quote.symbol, quote.source, "the source gave no price"))
            continue
        source = f"{_PREFIX}{quote.origin}"
        value = Money(quote.value)
        existing = next(
            (
                price
                for price in db.iter_prices(request.commodity)
                if price.currency == currency.handle
                and price.quote_date == quote.when
                and price.source == source
            ),
            None,
        )
        changed = existing is None or existing.value != value
        if changed:
            price = existing or CommodityPrice(
                commodity=request.commodity,
                currency=currency.handle,
                quote_date=quote.when,
                value=value,
                source=source,
                quote_type="last",
            )
            price.value = value
            plans.append((price, existing))
        stored.append(
            StoredQuote(
                commodity=request.commodity,
                symbol=quote.symbol,
                value=value,
                currency=currency.mnemonic,
                when=quote.when,
                source=source,
                changed=changed,
            )
        )
    if plans and not dry_run:
        with db.transaction("Update online quotes") as txn:
            for price, existing in plans:
                if existing is None:
                    db.add_price(price, txn)
                else:
                    db.commit_price(price, txn)
    return ServiceResult.success(QuoteUpdate(tuple(stored), tuple(problems), dry_run))


def set_quote_source(db: DbSQLite, request: SetQuoteSource) -> ServiceResult[str]:
    """Choose, change, or clear (empty) where a commodity's online quotes come from."""
    commodity = db.get_commodity(request.commodity)
    if commodity is None:
        return ServiceResult.failure(ServiceError("quotes.commodity.not_found", ("commodity",)))
    source = request.source.strip()
    if any(character.isspace() for character in source) or len(source) > 64:
        return ServiceResult.failure(ServiceError("quotes.source.invalid", ("source",)))
    if commodity.is_currency and source not in {"", "currency"}:
        return ServiceResult.failure(ServiceError("quotes.source.currency", ("source",)))
    if source == "currency" and not commodity.is_currency:
        return ServiceResult.failure(ServiceError("quotes.source.currency", ("source",)))
    if commodity.quote_source != source:
        commodity.quote_source = source
        with db.transaction(f"Quote source for {commodity.mnemonic}") as txn:
            db.commit_commodity(commodity, txn)
    return ServiceResult.success(source)
