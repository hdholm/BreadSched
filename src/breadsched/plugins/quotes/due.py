"""Fetch the online quotes that entering scheduled shares needs (#417).

A schedule that buys shares stores only their dollar value. Before entering an
occurrence, an interface asks the book server which legs buy or sell shares
(``due_quotes``), fetches an online quote for each whose commodity has a quote
source and whose stored price is older than the occurrence, stores what comes
back, and asks again. A leg still without any price is then given one by the
person entering it.
"""

from __future__ import annotations

from typing import Any

from ...gen.services.due_review import DueQuoteNeeds, DueQuotes
from ...gen.services.quotes import QuoteFailure, QuoteFetcher, StoreQuotes

__all__ = ["fetch_due_quotes"]


def fetch_due_quotes(
    book: Any, request: DueQuotes, fetcher: QuoteFetcher
) -> tuple[DueQuoteNeeds, tuple[QuoteFailure, ...]]:
    """What entering ``request`` needs once any online quotes are stored.

    ``book`` is the book server. A source that fails is reported and changes
    nothing; the stored price, if any, is used instead.
    """
    needs = book.due_quotes(request)
    if not needs.requests or needs.currency is None:
        return needs, ()
    try:
        quotes, failures = fetcher.fetch(needs.requests, needs.currency)
    except Exception as error:  # noqa: BLE001 - a fetch failure must not stop entry
        return needs, tuple(
            QuoteFailure(item.symbol, item.source, str(error)) for item in needs.requests
        )
    try:
        update = book.store_quotes(StoreQuotes(tuple(quotes), tuple(failures)))
    except Exception as error:  # noqa: BLE001 - e.g. a user who may only read the book
        return needs, tuple(
            QuoteFailure(item.symbol, item.source, f"not stored: {error}")
            for item in needs.requests
        )
    return book.due_quotes(request), update.failures
