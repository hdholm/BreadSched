"""Plan totals convert foreign-currency activity once, with disclosed evidence.

A EUR schedule must never be added to reporting-currency totals as if its units
were dollars (issue #124). With a quote, the amount is converted exactly at the
report as-of date and the quote date, source, and path are disclosed. Without
one, the activity is excluded from totals and listed as unconverted.
"""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.engine import valuation
from breadsched.gen.engine.activity import build_activity_report, build_category_report
from breadsched.gen.engine.currency import reporting_currency_handle
from breadsched.gen.lib import (
    Commodity,
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)

START = date(2026, 10, 1)
END = date(2026, 10, 31)
AS_OF = date(2026, 9, 27)


@pytest.fixture
def euro_book(db, book):
    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    local = ScheduledTransaction(
        name="Rent",
        recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 10, 1)),
        splits=[
            ScheduledSplit(book.rent, Money(1000)),
            ScheduledSplit(book.checking, Money(-1000)),
        ],
    )
    flat = ScheduledTransaction(
        name="Flat in Lyon",
        recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 10, 3)),
        splits=[ScheduledSplit(book.rent, Money(500)), ScheduledSplit(book.checking, Money(-500))],
    )
    with db.transaction("Two currencies") as txn:
        db.add_commodity(euro, txn)
        flat.currency = euro.handle
        db.add_scheduled(local, txn)
        db.add_scheduled(flat, txn)
    return book, euro


def _quote(db, euro, value, when=date(2026, 9, 1)):
    valuation.save_currency_quote(
        db,
        source_handle=euro.handle,
        target_handle=reporting_currency_handle(db),
        quote_date=when,
        value=Money(value),
    )


def test_foreign_schedule_is_converted_with_disclosed_quote(db, euro_book):
    book, euro = euro_book
    _quote(db, euro, "1.10")
    # A later quote is not yet known on the report as-of date.
    _quote(db, euro, "2.00", when=date(2026, 10, 2))

    report = build_activity_report(db, START, END, as_of=AS_OF)

    october = report.periods[0]
    assert october.planned_amount == Money("1550.00")
    assert october.planned_expense == Money("1550.00")
    assert october.planned_cash_change == Money("-1550.00")
    assert not october.unconverted
    [evidence] = report.conversions
    assert (evidence.source_currency, evidence.target_currency) == (
        euro.handle,
        reporting_currency_handle(db),
    )
    assert evidence.quote_date == date(2026, 9, 1)
    assert evidence.path == "direct"
    assert evidence.rate == Money("1.10")


def test_inverse_quote_is_disclosed_as_inverse(db, euro_book):
    _book, euro = euro_book
    valuation.save_currency_quote(
        db,
        source_handle=reporting_currency_handle(db),
        target_handle=euro.handle,
        quote_date=date(2026, 9, 1),
        value=Money("0.80"),
    )

    report = build_activity_report(db, START, END, as_of=AS_OF)

    assert report.periods[0].planned_expense == Money("1625.00")
    assert [item.path for item in report.conversions] == ["inverse"]


def test_missing_quote_excludes_and_lists_foreign_activity(db, euro_book):
    book, euro = euro_book
    paid = Transaction.simple(date(2026, 10, 4), "Lyon deposit", book.rent, book.checking, "200")
    paid.currency = euro.handle
    with db.transaction("Foreign actual") as txn:
        db.add_transaction(paid, txn)

    report = build_activity_report(db, START, END, as_of=AS_OF)

    october = report.periods[0]
    # Never 1,500.00: the EUR amount is not added as dollars.
    assert october.planned_expense == Money(1000)
    assert october.actual_expense == Money(0)
    assert [
        (item.kind, item.description, item.amount, item.currency) for item in october.unconverted
    ] == [
        ("planned", "Flat in Lyon", Money(500), euro.handle),
        ("actual", "Lyon deposit", Money(200), euro.handle),
    ]
    assert report.unconverted == tuple(october.unconverted)
    assert report.conversions == ()

    categories = build_category_report(db, START, END, as_of=AS_OF)
    rent = next(row for row in categories.categories if row.account == book.rent)
    assert rent.planned == [Money(1000)]
    assert rent.actual == [Money(0)]


def test_converted_actual_reconciles_detail_and_explorer_remaining(db, euro_book):
    from breadsched.gen.engine.activity import explain_category_period
    from breadsched.gen.services.expense_explorer import query_expense_explorer
    from breadsched.gen.services.plan import PlanQuery

    book, euro = euro_book
    _quote(db, euro, "1.10")
    paid = Transaction.simple(date(2026, 10, 4), "Lyon deposit", book.rent, book.checking, "200")
    paid.currency = euro.handle
    with db.transaction("Foreign actual") as txn:
        db.add_transaction(paid, txn)
    today = date(2026, 10, 31)

    detail = explain_category_period(db, book.rent, START, END, as_of=today)
    assert detail.planned == Money("1550.00")
    assert detail.actual == Money("220.00")
    assert [item.amount for item in detail.actual_transactions] == [Money("220.00")]

    result = query_expense_explorer(
        db, PlanQuery(start=START, end=END, today=today), account=book.rent, period_index=0
    )
    assert result.value is not None
    rent = next(row for row in result.value.categories if row.account == book.rent)
    assert rent.periods[0].remaining == Money("1330.00")
    assert rent.periods[0].remaining_reason is None
    assert result.value.drilldown is not None
    assert result.value.drilldown.merchants[0].amount == Money("220.00")


def test_currency_notes_disclose_quote_or_exclusion(db, euro_book):
    from breadsched.gen.engine.activity import currency_notes

    _book, euro = euro_book
    missing = build_category_report(db, START, END, as_of=AS_OF)
    [note] = missing.currency_notes
    assert note.startswith(
        "Not included in totals: no EUR→USD or USD→EUR quote applies on 2026-09-27"
    )
    assert "Flat in Lyon 500.00 EUR (planned 2026-10-03)" in note

    valuation.save_currency_quote(
        db,
        source_handle=reporting_currency_handle(db),
        target_handle=euro.handle,
        quote_date=date(2026, 9, 1),
        value=Money(3),
    )
    report = build_activity_report(db, START, END, as_of=AS_OF)
    [note] = currency_notes(db, report)
    assert note.startswith("EUR amounts are converted to USD at ≈0.33333333 USD per EUR")
    assert "the inverse of the USD→EUR quote dated 2026-09-01" in note
    assert note.endswith("applicable on 2026-09-27.")


def test_web_plan_and_print_disclose_currency(db, euro_book):
    from breadsched.gen.engine.activity import PlanMeasure
    from breadsched.plugins.export.html_report import plan_report as print_plan
    from breadsched.web.plan_resource import plan_report

    _book, euro = euro_book
    missing = plan_report(db, "2026-10", "2026-10")
    [note] = missing["currency"]["notes"]
    assert note.startswith("Not included in totals")
    assert missing["currency"]["conversions"] == []
    assert [item["description"] for item in missing["currency"]["unconverted"]] == ["Flat in Lyon"]

    _quote(db, euro, "1.10")
    converted = plan_report(db, "2026-10", "2026-10")
    [evidence] = converted["currency"]["conversions"]
    assert (evidence["rate"], evidence["quote_date"], evidence["path"]) == (
        Money("1.10"),
        date(2026, 9, 1),
        "direct",
    )
    assert converted["currency"]["unconverted"] == []

    report = build_category_report(db, START, END, as_of=AS_OF)
    html = print_plan(report, PlanMeasure.PLANNED, scenario_name="Base scenario")
    assert "EUR amounts are converted to USD at 1.1 USD per EUR" in html


def test_cli_activity_discloses_currency(tmp_path, capsys):
    import json

    from breadsched.cli.main import main
    from breadsched.gen.db.sqlite import DbSQLite
    from breadsched.gen.sample_book import create_sample_book

    path = tmp_path / "fx.breadsched"
    create_sample_book(path, as_of=date(2026, 9, 15))
    db = DbSQLite()
    db.load(str(path))
    try:
        euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
        checking = next(item for item in db.iter_accounts() if item.name == "Checking")
        expense = next(
            item
            for item in db.iter_accounts()
            if item.account_class.value == "expense" and not item.placeholder
        )
        flat = ScheduledTransaction(
            name="Flat in Lyon",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 10, 3)),
            splits=[
                ScheduledSplit(expense.handle, Money(500)),
                ScheduledSplit(checking.handle, Money(-500)),
            ],
        )
        with db.transaction("EUR schedule") as txn:
            db.add_commodity(euro, txn)
            flat.currency = euro.handle
            db.add_scheduled(flat, txn)
    finally:
        db.close()
    command = ["activity", str(path), "--start", "2026-10-01", "--end", "2026-10-31"]

    assert main([*command, "--as-of", "2026-09-27"]) == 0
    assert "Flat in Lyon 500.00 EUR (planned 2026-10-03)" in capsys.readouterr().out

    assert (
        main(
            [
                "rate",
                str(path),
                "--from",
                "EUR",
                "--to",
                "USD",
                "--date",
                "2026-09-01",
                "--value",
                "1.25",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main([*command, "--as-of", "2026-09-27", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["unconverted"] == []
    assert data["conversions"][0]["rate"] == "1.25"
    assert data["currency_as_of"] == "2026-09-27"


def test_cash_position_uses_converted_events(db, euro_book):
    book, euro = euro_book
    categories = build_category_report(db, START, END, as_of=AS_OF)
    position = categories.cash_position
    assert position.closing - position.opening == Money(-1000)

    _quote(db, euro, "1.10")
    converted = build_category_report(db, START, END, as_of=AS_OF).cash_position
    assert converted.closing - converted.opening == Money("-1550.00")
    assert converted.minimum == converted.closing


def test_unconverted_income_leaves_expense_remaining_available(db, book):
    from breadsched.gen.services.expense_explorer import query_expense_explorer
    from breadsched.gen.services.plan import PlanQuery

    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    bonus = Transaction.simple(date(2026, 10, 2), "Paris bonus", book.checking, book.salary, "90")
    bonus.currency = euro.handle
    with db.transaction("Foreign income") as txn:
        db.add_commodity(euro, txn)
        db.add_transaction(bonus, txn)
        db.add_transaction(
            Transaction.simple(date(2026, 10, 3), "Food", book.groceries, book.checking, "10"),
            txn,
        )
    result = query_expense_explorer(db, PlanQuery(start=START, end=END, today=END))
    assert result.value is not None
    assert result.value.totals[0].remaining == Money(-10)
    report = build_category_report(db, START, END, as_of=END)
    assert book.salary in report.unconverted_accounts[0]
