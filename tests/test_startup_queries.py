"""The database answers the questions startup asks most, without decoding rows (#389).

Opening a large book spent seconds decoding every split to total a balance and
every day's transactions to ask whether a schedule had posted. These tests pin
the faster queries to the answers the slow ones gave.
"""

from __future__ import annotations

from datetime import date

from breadsched.gen.engine import ledger
from breadsched.gen.engine import schedule as engine
from breadsched.gen.lib import Money, Transaction


def _scanned_total(db, account, start=None, end=None) -> Money:
    """The balance as it was computed before: one decoded split at a time."""
    total = Money(0)
    for row in db.split_rows(account, start=start, end=end):
        total += Money(row["value_num"], row["value_den"])
    return total


def test_split_totals_add_exactly_across_denominators_and_dates(db, book):
    amounts = ["10.01", "0.005", "1/3", "-2.50", "1234567.89", "7"]
    with db.transaction("Spend") as txn:
        for day, amount in enumerate(amounts, start=1):
            db.add_transaction(
                Transaction.simple(
                    date(2026, 3, day), f"Item {day}", book.groceries, book.checking, amount
                ),
                txn,
            )
    rows = db.split_totals(book.groceries)
    # Splits sharing a denominator are summed into one row.
    assert len(rows) < len(amounts)
    expected = sum((Money(amount) for amount in amounts), Money(0))
    assert sum((Money(num, den) for _cur, den, num in rows), Money(0)) == expected
    for start, end in ((None, None), (date(2026, 3, 2), None), (None, date(2026, 3, 4))):
        assert ledger.balance_amount(
            db, book.groceries, as_of=end, since=start, natural_sign=False
        ).value == _scanned_total(db, book.groceries, start, end)
        assert ledger.balance_amount(
            db, book.checking, as_of=end, since=start, natural_sign=False
        ).value == _scanned_total(db, book.checking, start, end)
    assert db.split_totals(book.rent) == []
    assert ledger.balance_amount(db, book.rent).value == Money(0)


def test_has_scheduled_posting_matches_the_days_transactions(db, book, payday_schedule):
    [first, second, *_rest] = engine.due_occurrences(db, as_of=date(2026, 2, 20))
    assert not engine.already_posted(db, payday_schedule.handle, first.when)
    # A transaction the same day, not from the schedule, is not its posting.
    with db.transaction("Same day") as txn:
        db.add_transaction(
            Transaction.simple(first.when, "Salary", book.checking, book.salary, "1938.46"), txn
        )
    assert not db.has_scheduled_posting(payday_schedule.handle, first.when)
    engine.post_occurrences(db, [first])
    assert db.has_scheduled_posting(payday_schedule.handle, first.when)
    assert engine.already_posted(db, payday_schedule.handle, first.when)
    assert not engine.already_posted(db, payday_schedule.handle, second.when)
    assert not db.has_scheduled_posting("another schedule", first.when)
    # The slower scan the question replaced gives the same answers.
    for when in (first.when, second.when):
        scanned = any(
            txn.scheduled_from == payday_schedule.handle
            for txn in db.iter_transactions(start=when, end=when)
        )
        assert scanned == db.has_scheduled_posting(payday_schedule.handle, when)


def test_first_transaction_date_is_the_earliest_or_none(db, book):
    """The Plan's earliest month comes from one query, not every transaction (#389)."""
    assert db.first_transaction_date() is None
    with db.transaction("Spend") as txn:
        for when in (date(2026, 5, 2), date(2024, 11, 30), date(2025, 1, 1)):
            db.add_transaction(
                Transaction.simple(when, "Item", book.groceries, book.checking, "1"), txn
            )
    assert db.first_transaction_date() == date(2024, 11, 30)
    assert db.first_transaction_date() == min(t.post_date for t in db.iter_transactions())
