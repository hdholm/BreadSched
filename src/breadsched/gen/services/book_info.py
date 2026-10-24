"""The open book as a whole: its counts and headline values, and its verification.

``engine.valuation`` values the book; ``db.verification`` checks it. Each view here
carries the result whole, with every missing quote, so an interface reports the
book without reading it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..db.sqlite import DbSQLite
from ..db.verification import BookVerification
from ..engine import valuation
from ..engine.valuation import ValuationAggregate

__all__ = ["BookSummary", "book_summary", "verify_book"]


@dataclass(frozen=True, slots=True)
class BookSummary:
    #: The book's file as the server sees it (``None`` for one held in memory).
    path: str | None
    accounts: int
    transactions: int
    #: Cash-like accounts, and the whole book, in the reporting currency.
    cash: ValuationAggregate
    net_worth: ValuationAggregate
    scenarios: tuple[str, ...]


def book_summary(db: DbSQLite) -> BookSummary:
    counts = db.summary()
    cash = valuation.aggregate_value(
        db,
        accounts=[a for a in db.iter_accounts() if a.atype.is_cash_like and not a.placeholder],
    )
    return BookSummary(
        db.path,
        counts["account"],
        counts["txn"],
        cash,
        valuation.aggregate_value(db, net_worth=True),
        tuple(item.name for item in db.iter_scenarios()),
    )


def verify_book(db: DbSQLite) -> BookVerification:
    """Physical and logical checks of the open book; writes nothing."""
    return db.verification_report()
