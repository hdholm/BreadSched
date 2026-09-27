"""Projection converts foreign schedules and balances once, or leaves them out.

A EUR schedule or a EUR account balance must not move projected USD cash unit
for unit. Each currency uses the one quote applicable on the day before the
projection starts (the opening valuation date), and the result's warnings
disclose the quote or list what was left out.
"""

from __future__ import annotations

from datetime import date

import pytest
from test_projection import flat_assumptions

from breadsched.gen.engine import projection, valuation
from breadsched.gen.engine.currency import reporting_currency_handle
from breadsched.gen.lib import (
    Account,
    AccountType,
    Commodity,
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)
from breadsched.gen.lib.scenario import Scenario

START = date(2026, 10, 1)


@pytest.fixture
def euro_book(db, book):
    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    flat = ScheduledTransaction(
        name="Flat in Lyon",
        recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 10, 3)),
        splits=[ScheduledSplit(book.rent, Money(500)), ScheduledSplit(book.checking, Money(-500))],
    )
    flat.currency = euro.handle
    with db.transaction("EUR schedule") as txn:
        db.add_commodity(euro, txn)
        db.add_scheduled(flat, txn)
    return book, euro


def _scenario():
    return Scenario(name="FX", start=START, years=1, assumptions=flat_assumptions())


def _quote(db, euro, value, when=date(2026, 9, 1)):
    valuation.save_currency_quote(
        db,
        source_handle=euro.handle,
        target_handle=reporting_currency_handle(db),
        quote_date=when,
        value=Money(value),
    )


def test_missing_quote_leaves_foreign_event_out_and_warns(db, euro_book):
    result = projection.project(db, _scenario())

    october = result.rows[0]
    assert october.cash_close - october.cash_open == Money(0)
    assert october.expense == Money(0)
    assert any(
        item.startswith("Not included in totals") and "Flat in Lyon 500.00 EUR" in item
        for item in result.warnings
    )


def test_quoted_foreign_event_is_converted_and_disclosed(db, euro_book):
    _book, euro = euro_book
    _quote(db, euro, "1.10")
    # Dated after the opening valuation date, so not yet known.
    _quote(db, euro, "2.00", when=date(2026, 10, 2))

    result = projection.project(db, _scenario())

    october = result.rows[0]
    assert october.cash_close - october.cash_open == Money("-550.00")
    assert october.expense == Money("550.00")
    assert any(
        item.startswith("EUR amounts are converted to USD at 1.1 USD per EUR")
        and "dated 2026-09-01" in item
        and "applicable on 2026-09-30" in item
        for item in result.warnings
    )


def test_foreign_opening_balance_needs_a_quote(db, euro_book):
    book, euro = euro_book
    with db.transaction("Euro account") as txn:
        account = Account(
            name="Euro savings",
            atype=AccountType.BANK,
            parent=db.get_account(book.checking).parent,
            commodity=euro.handle,
        )
        db.add_account(account, txn)
        deposit = Transaction.simple(
            date(2026, 9, 5), "Opening", account.handle, book.opening, "1000"
        )
        deposit.currency = euro.handle
        db.add_transaction(deposit, txn)
    usd_only = projection.project(db, _scenario()).rows[0].cash_open

    missing = projection.project(db, _scenario())
    assert any("Euro savings" in item and "1,000.00 EUR" in item for item in missing.warnings)

    _quote(db, euro, "1.10")
    converted = projection.project(db, _scenario())
    assert converted.rows[0].cash_open - usd_only == Money("1100.00")


def test_cli_projection_prints_currency_warning(tmp_path, capsys):
    from breadsched.cli.main import main
    from breadsched.gen.db.sqlite import DbSQLite
    from breadsched.gen.sample_book import create_sample_book

    path = tmp_path / "fx.breadsched"
    create_sample_book(path, as_of=date(2026, 9, 15))
    db = DbSQLite()
    db.load(str(path))
    try:
        euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
        accounts = [item for item in db.iter_accounts() if not item.placeholder]
        cash = next(item for item in accounts if item.is_spendable_cash)
        expense = next(item for item in accounts if item.account_class.value == "expense")
        flat = ScheduledTransaction(
            name="Flat in Lyon",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 10, 3)),
            splits=[
                ScheduledSplit(expense.handle, Money(500)),
                ScheduledSplit(cash.handle, Money(-500)),
            ],
        )
        flat.currency = euro.handle
        with db.transaction("EUR schedule") as txn:
            db.add_commodity(euro, txn)
            db.add_scheduled(flat, txn)
    finally:
        db.close()

    assert main(["project", str(path), "--start", "2026-10-01", "--years", "1"]) == 0
    out = capsys.readouterr().out
    assert "warning: Not included in totals" in out
    assert "Flat in Lyon 500.00 EUR" in out
