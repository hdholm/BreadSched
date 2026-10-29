"""HTTP adapter for online quotes: sources, the Alpha Vantage key, and fetching.

Choosing a source and storing quotes go through ``gen/services/quotes``; fetching
uses ``plugins.quotes`` and is the only network access the web server makes. The
key is never sent back to the browser, which learns only whether one is set. The
web adapter fetches and the book server, ``api.book``, stores (#354), so the book
server itself never reaches the network.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..gen.services.quotes import SetQuoteSource, StoreQuotes
from ..gen.utils.settings import Settings
from ..plugins.quotes import (
    OnlineQuotes,
    alphavantage_key,
    finance_quote_info,
    quote_source_choices,
    quote_source_warning,
    save_alphavantage_key,
)
from ..presentation import price_text

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
    info = finance_quote_info()
    return {
        "commodities": [
            {
                "handle": item.handle,
                "mnemonic": item.mnemonic,
                "fullname": item.fullname,
                "currency": item.currency,
                "quote_source": item.quote_source,
            }
            for item in api.book.quote_plan().commodities
            if not item.reporting
        ],
        "finance_quote": {"available": info.available, "reason": info.reason},
        # Every source a commodity may name, offered as choices (#317).
        "sources": list(quote_source_choices(info)),
        "alphavantage_key": bool(alphavantage_key(settings())),
    }


def quote_source_save(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    source = api.book.set_quote_source(
        SetQuoteSource(_text(payload, "commodity"), _text(payload, "source"))
    )
    return {"source": source, "warning": quote_source_warning(source, finance_quote_info())}


def quote_key_save(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    if not save_alphavantage_key(settings(), _text(payload, "key")):
        raise ValueError("the Alpha Vantage key could not be saved")
    return {"alphavantage_key": bool(_text(payload, "key").strip())}


def quotes_update(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    plan = api.book.quote_plan()
    fetched: tuple[list, list] = ([], [])
    if plan.requests and plan.currency is not None:
        fetched = make_fetcher(alphavantage_key(settings())).fetch(plan.requests, plan.currency)
    quotes, failures = fetched
    update = api.book.store_quotes(StoreQuotes(tuple(quotes), tuple(failures)))
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
