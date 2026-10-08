"""HTTP adapter for online quotes: sources, the Alpha Vantage key, and fetching.

Choosing a source and storing quotes go through ``gen/services/quotes``; fetching
uses ``plugins.quotes`` and is the only network access the web server makes. The
key is never sent back to the browser, which learns only whether one is set.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..gen.engine.currency import reporting_currency_handle
from ..gen.services.quotes import (
    Prefetched,
    SetQuoteSource,
    quote_requests,
    set_quote_source,
    update_quotes,
)
from ..gen.utils.settings import Settings
from ..plugins.quotes import (
    OnlineQuotes,
    alphavantage_key,
    finance_quote_status,
    save_alphavantage_key,
)
from ..presentation import price_text
from .controls import service_error

if TYPE_CHECKING:
    from .context import Api


def settings() -> Settings:
    """The user's settings; replaced in tests."""
    return Settings()


def make_fetcher(key: str) -> Any:
    """The quote fetcher; replaced in tests so they never touch the network."""
    return OnlineQuotes(alphavantage_key=key or None)


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def quotes(api: Api) -> dict[str, object]:
    db = api.db
    reporting = reporting_currency_handle(db)
    available, reason = finance_quote_status()
    return {
        "commodities": [
            {
                "handle": item.handle,
                "mnemonic": item.mnemonic,
                "fullname": item.fullname,
                "currency": item.is_currency,
                "quote_source": item.quote_source,
            }
            for item in sorted(db.iter_commodities(), key=lambda c: (c.is_currency, c.mnemonic))
            if item.handle != reporting
        ],
        "finance_quote": {"available": available, "reason": reason},
        "alphavantage_key": bool(alphavantage_key(settings())),
    }


def quote_source_save(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    result = set_quote_source(
        api.db, SetQuoteSource(_text(payload, "commodity"), _text(payload, "source"))
    )
    if result.value is None:
        raise service_error(result.errors[0])
    return {"source": result.value}


def quote_key_save(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    if not save_alphavantage_key(settings(), _text(payload, "key")):
        raise ValueError("the Alpha Vantage key could not be saved")
    return {"alphavantage_key": bool(_text(payload, "key").strip())}


def quotes_update(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    db = api.db
    requests = quote_requests(db)
    reporting = db.get_commodity(reporting_currency_handle(db))
    fetched: tuple[list, list] = ([], [])
    if requests and reporting is not None:
        fetched = make_fetcher(alphavantage_key(settings())).fetch(requests, reporting.mnemonic)
    result = update_quotes(db, Prefetched(*fetched))
    if result.value is None:
        raise service_error(result.errors[0])
    update = result.value
    return {
        "stored": [
            {
                "symbol": item.symbol,
                "price": price_text(item.value),
                "currency": item.currency,
                "date": item.when.isoformat(),
                "source": item.source,
                "changed": item.changed,
            }
            for item in update.stored
        ],
        "failures": [
            {"symbol": item.symbol, "source": item.source, "reason": item.reason}
            for item in update.failures
        ],
    }
