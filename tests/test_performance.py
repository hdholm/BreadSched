"""Realistic-size performance guards for storage and projection operations."""

from datetime import date
from time import perf_counter

import pytest

from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine import projection
from breadsched.gen.lib import Account, AccountType, Assumptions, Scenario, Transaction


@pytest.fixture(scope="module")
def realistic_book():
    """A 30k-transaction household history shared by serial performance tests."""
    db = DbSQLite.create_memory()
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

    yield db, checking, expense
    db.close()


@pytest.mark.performance
def test_single_commit_stays_fast_with_thirty_thousand_transactions(realistic_book):
    """Catch accidental restoration of O(book) verification on each write."""
    db, checking, expense = realistic_book

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


@pytest.mark.performance
def test_thirty_year_projection_stays_responsive_with_large_history(realistic_book):
    """Guard the full-ledger scans that feed planning/projection linked-actual lookup."""
    db, _checking, _expense = realistic_book
    scenario = Scenario(
        name="Performance projection",
        start=date(2026, 9, 1),
        years=30,
        assumptions=Assumptions(
            income_growth="0",
            expense_inflation="0",
            investment_return="0",
            cash_interest="0",
            liability_interest="0",
        ),
    )

    start = perf_counter()
    result = projection.project(db, scenario)
    elapsed = perf_counter() - start

    assert len(result.rows) == 360
    assert elapsed < 3.0, f"30-year projection took {elapsed:.3f}s on a 30k-transaction book"
