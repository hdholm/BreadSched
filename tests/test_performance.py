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


@pytest.mark.performance
def test_net_worth_history_reads_each_split_once(realistic_book):
    """Guard the Dashboard's history against re-scanning the ledger per date."""
    from breadsched.gen.services import query_net_worth_history

    db, _checking, _expense = realistic_book
    start = perf_counter()
    result = query_net_worth_history(
        db, date(2025, 10, 1), date(2026, 9, 30), "month", today=date(2026, 9, 15)
    )
    elapsed = perf_counter() - start

    assert result.value is not None and len(result.value.points) == 12
    assert elapsed < 1.5, f"12-month net worth history took {elapsed:.3f}s on a 30k book"


@pytest.mark.performance
def test_net_worth_change_stays_interactive(realistic_book):
    """A month holding the whole 30k-transaction history still answers promptly."""
    from breadsched.gen.services import query_net_worth_change

    db, _checking, _expense = realistic_book
    start = perf_counter()
    result = query_net_worth_change(db, date(2026, 1, 1), date(2026, 1, 31), date(2026, 9, 15))
    elapsed = perf_counter() - start

    change = result.value
    assert change is not None and change.postings
    assert change.posted is not None and change.revaluation is not None
    assert change.posted + change.revaluation == change.change
    assert elapsed < 1.5, f"a month's net worth change took {elapsed:.3f}s on a 30k book"


@pytest.mark.performance
def test_budget_jars_carry_in_years_of_history_quickly(realistic_book):
    """Levels carry in every earlier occurrence; five weekly years must stay interactive."""
    from breadsched.gen.engine.budget_jars import budget_jars
    from breadsched.gen.lib import (
        Money,
        PeriodType,
        Recurrence,
        ScheduledSplit,
        ScheduledTransaction,
    )

    db, checking, expense = realistic_book
    income = Account(name="Pay", atype=AccountType.INCOME)
    with db.transaction("Jar schedules") as txn:
        db.add_account(income, txn)
        pay = ScheduledTransaction(
            name="Weekly pay",
            recurrence=Recurrence(PeriodType.WEEK, interval=1, start=date(2021, 9, 3)),
            splits=[
                ScheduledSplit(checking.handle, Money(800)),
                ScheduledSplit(income.handle, Money(-800)),
            ],
        )
        pay.last_posted = date(2026, 9, 1)
        groceries = ScheduledTransaction(
            name="Weekly groceries",
            recurrence=Recurrence(PeriodType.WEEK, interval=1, start=date(2021, 9, 6)),
            splits=[
                ScheduledSplit(expense.handle, Money(150)),
                ScheduledSplit(checking.handle, Money(-150)),
            ],
        )
        groceries.placeholder = True
        db.add_scheduled(pay, txn)
        db.add_scheduled(groceries, txn)
    try:
        start = perf_counter()
        report = budget_jars(db, date(2026, 7, 1), date(2026, 9, 30), today=date(2026, 9, 15))
        elapsed = perf_counter() - start
    finally:
        # The book is shared by the module's tests, which run in random order: a
        # projection after this test must not also project five years of weekly
        # schedules.
        with db.transaction("Remove jar schedules") as txn:
            db.remove_scheduled(pay.handle, txn)
            db.remove_scheduled(groceries.handle, txn)
            db.remove_account(income.handle, txn)

    [jar] = report.jars
    # About 260 earlier weeks were filled and never matched: all still set aside.
    assert jar.opening > Money(150 * 250)
    assert elapsed < 5.0, f"budget jars over five weekly years took {elapsed:.3f}s"
