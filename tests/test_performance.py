"""Realistic-size performance guards for storage operations."""

from datetime import date
from time import perf_counter

import pytest

from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.lib import Account, AccountType, Transaction


@pytest.mark.performance
def test_single_commit_stays_fast_with_thirty_thousand_transactions():
    """Catch accidental restoration of O(book) verification on each write.

    The setup is deliberately outside the measured region.  The threshold is much
    looser than normal incremental-commit performance, but comfortably below the
    multi-second whole-book verification cost this regression is meant to detect.
    """
    db = DbSQLite.create_memory()
    try:
        with db.transaction("chart") as txn:
            checking = Account(name="Checking", atype=AccountType.BANK)
            expense = Account(name="Household expense", atype=AccountType.EXPENSE)
            db.add_account(checking, txn)
            db.add_account(expense, txn)

        with db.transaction("realistic history") as txn:
            for index in range(30_000):
                posted = Transaction.simple(
                    date(2026, 1, 1),
                    f"Historical transaction {index}",
                    expense.handle,
                    checking.handle,
                    "1.00",
                )
                db.add_transaction(posted, txn)

        start = perf_counter()
        with db.transaction("one current edit") as txn:
            posted = Transaction.simple(
                date(2026, 9, 1),
                "Current transaction",
                expense.handle,
                checking.handle,
                "1.00",
            )
            db.add_transaction(posted, txn)
        elapsed = perf_counter() - start

        assert elapsed < 0.5, f"single commit took {elapsed:.3f}s on a 30k-transaction book"
    finally:
        db.close()
