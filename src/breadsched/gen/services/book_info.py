"""The open book as a whole: its counts and headline values, and its verification.

``engine.valuation`` values the book; ``db.verification`` checks it. Each view here
carries the result whole, with every missing quote, so an interface reports the
book without reading it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from ..db.base import DbError
from ..db.sqlite import DbSQLite
from ..db.verification import BookVerification
from ..engine import valuation
from ..engine.valuation import ValuationAggregate

__all__ = [
    "BOOK_ID_KEY",
    "BookCounts",
    "BookSummary",
    "book_counts",
    "book_id",
    "book_summary",
    "verify_book",
]

#: The book's permanent id, by which a host keeps who may use it (decision 0004).
BOOK_ID_KEY = "book.id"


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


@dataclass(frozen=True, slots=True)
class BookCounts:
    """How many accounts and transactions the book holds; quick to read on every change."""

    accounts: int
    transactions: int


def book_counts(db: DbSQLite) -> BookCounts:
    counts = db.summary()
    return BookCounts(counts["account"], counts["txn"])


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


def book_id(db: DbSQLite) -> str | None:
    """The book's permanent id, made the first time it is asked for in a book open for
    writing; None for a book open only to read that has none yet. A copy of the book
    keeps it, so access is granted to the book wherever its host serves it."""
    found = db.get_metadata(BOOK_ID_KEY)
    if isinstance(found, str) and found:
        return found
    made = uuid.uuid4().hex
    try:
        db.set_metadata(BOOK_ID_KEY, made)
    except DbError:
        return None
    return made
