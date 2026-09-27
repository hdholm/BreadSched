"""Expense exploration reconciles with the existing Plan expense cells."""

from datetime import date

from breadsched.gen.engine.activity import ReportingPeriod
from breadsched.gen.lib import (
    Account,
    AccountType,
    Commodity,
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    Split,
    Transaction,
)
from breadsched.gen.services.expense_explorer import query_expense_explorer
from breadsched.gen.services.plan import PlanQuery
from breadsched.plugins.export.html_report import expense_explorer_report


def test_merchant_grouping_keeps_category_plan_unallocated(db, book):
    estimate = ScheduledTransaction(
        name="Category estimate",
        recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 5)),
        splits=[
            ScheduledSplit(book.groceries, Money(100)),
            ScheduledSplit(book.checking, Money(-100)),
        ],
    )
    with db.transaction("expense explorer") as txn:
        db.add_scheduled(estimate, txn)
        for name, amount in ((" Shop A ", "40"), ("shop a", "15"), ("Shop B", "65")):
            db.add_transaction(
                Transaction.simple(date(2026, 1, 12), name, book.groceries, book.checking, amount),
                txn,
            )
    request = PlanQuery(start=date(2026, 1, 1), end=date(2026, 1, 31), today=date(2026, 1, 31))
    result = query_expense_explorer(db, request, account=book.groceries, period_index=0)
    assert result.ok
    explorer = result.value
    assert explorer is not None and explorer.drilldown is not None
    assert explorer.drilldown.period.planned == Money(100)
    assert explorer.drilldown.period.actual == Money(120)
    assert explorer.drilldown.period.variance == Money(20)
    assert explorer.drilldown.period.remaining == Money(-20)
    assert explorer.totals[0].remaining == Money(-20)
    assert explorer.totals[0].planned == Money(100)
    assert explorer.totals[0].variance == Money(20)
    assert [(group.name, group.amount) for group in explorer.drilldown.merchants] == [
        ("Shop A", Money(55)),
        ("Shop B", Money(65)),
    ]
    assert [len(group.transactions) for group in explorer.drilldown.merchants] == [2, 1]
    assert explorer.totals[0].actual == Money(120)


def test_multisplit_refund_and_blank_description_reconcile(db, book):
    with db.transaction("expense explorer") as txn:
        purchase = Transaction(post_date=date(2026, 2, 2), description="  ")
        purchase.splits = [
            Split(book.groceries, Money(30)),
            Split(book.utilities, Money(20)),
            Split(book.checking, Money(-50)),
        ]
        db.add_transaction(purchase, txn)
        db.add_transaction(
            Transaction.simple(date(2026, 2, 3), " ", book.groceries, book.checking, "-5"),
            txn,
        )
    request = PlanQuery(
        start=date(2026, 2, 1),
        end=date(2026, 2, 28),
        period=ReportingPeriod.MONTH,
        today=date(2026, 2, 28),
    )
    result = query_expense_explorer(db, request, account=book.expenses, period_index=0)
    assert result.ok
    explorer = result.value
    assert explorer is not None and explorer.drilldown is not None
    assert [(group.name, group.amount) for group in explorer.drilldown.merchants] == [
        ("Unknown merchant", Money(45))
    ]
    assert explorer.totals[0].actual == Money(45)
    assert explorer.totals[0].remaining == Money(-45)
    assert not query_expense_explorer(db, request, account=book.groceries, period_index=2).ok


def test_printable_merchant_detail_escapes_descriptions(db, book):
    with db.transaction("expense explorer") as txn:
        db.add_transaction(
            Transaction.simple(date(2026, 3, 2), "<Store>", book.groceries, book.checking, "8"),
            txn,
        )
    request = PlanQuery(start=date(2026, 3, 1), end=date(2026, 3, 31))
    result = query_expense_explorer(db, request, account=book.groceries, period_index=0)
    assert result.value is not None
    html = expense_explorer_report(result.value)
    assert "&lt;Store&gt;" in html
    assert "<Store>" not in html
    assert "Category plan is unallocated" in html
    assert "Remaining" in html


def test_escrow_payout_does_not_create_merchant_expense(db, book):
    escrow = Account(name="Escrow", atype=AccountType.ESCROW, parent=book.assets)
    with db.transaction("expense explorer") as txn:
        db.add_account(escrow, txn)
        db.add_transaction(
            Transaction.simple(date(2026, 4, 3), "Fund", escrow.handle, book.checking, "100"),
            txn,
        )
        payout = Transaction(post_date=date(2026, 4, 10), description="Tax office")
        payout.add_split(Split(book.utilities, Money(80)))
        payout.add_split(Split(escrow.handle, Money(-80)))
        db.add_transaction(payout, txn)
    result = query_expense_explorer(
        db,
        PlanQuery(start=date(2026, 4, 1), end=date(2026, 4, 30)),
        account=book.utilities,
        period_index=0,
    )
    assert result.value is not None and result.value.drilldown is not None
    assert result.value.drilldown.period.actual == Money(0)
    assert result.value.drilldown.merchants == ()


def test_future_expense_variance_is_not_applicable(db, book):
    estimate = ScheduledTransaction(
        name="Future estimate",
        recurrence=Recurrence(PeriodType.ONCE, start=date(2028, 2, 2)),
        splits=[
            ScheduledSplit(book.groceries, Money(12)),
            ScheduledSplit(book.checking, Money(-12)),
        ],
    )
    with db.transaction("expense explorer") as txn:
        db.add_scheduled(estimate, txn)
    result = query_expense_explorer(
        db,
        PlanQuery(start=date(2028, 2, 1), end=date(2028, 2, 29)),
        account=book.groceries,
        period_index=0,
    )
    assert result.value is not None and result.value.drilldown is not None
    assert result.value.drilldown.period.planned == Money(12)
    assert result.value.drilldown.period.variance is None
    assert result.value.drilldown.period.remaining is None
    assert result.value.drilldown.period.remaining_reason == "Future period"
    assert result.value.totals[0].variance is None


def test_remaining_counts_refunds_once_and_stops_actuals_at_as_of(db, book):
    estimate = ScheduledTransaction(
        name="Food plan",
        recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 5, 2)),
        splits=[
            ScheduledSplit(book.groceries, Money(100)),
            ScheduledSplit(book.checking, Money(-100)),
        ],
    )
    with db.transaction("Dated expenses") as txn:
        db.add_scheduled(estimate, txn)
        for day, amount in ((4, "60"), (5, "-10"), (25, "30")):
            db.add_transaction(
                Transaction.simple(
                    date(2026, 5, day), "Food", book.groceries, book.checking, amount
                ),
                txn,
            )
    result = query_expense_explorer(
        db,
        PlanQuery(start=date(2026, 5, 1), end=date(2026, 5, 31), today=date(2026, 5, 12)),
        account=book.groceries,
        period_index=0,
    )
    assert result.value is not None and result.value.drilldown is not None
    assert result.value.drilldown.period.remaining == Money(50)
    parent = next(row for row in result.value.categories if row.account == book.expenses)
    assert parent.periods[0].remaining == Money(50)
    assert result.value.totals[0].remaining == Money(50)


def test_foreign_expense_suppresses_only_affected_category_remaining(db, book):
    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    foreign = Transaction.simple(
        date(2026, 6, 2), "Foreign groceries", book.groceries, book.checking, "20"
    )
    foreign.currency = euro.handle
    with db.transaction("Foreign expense") as txn:
        db.add_commodity(euro, txn)
        db.add_transaction(foreign, txn)
        db.add_transaction(
            Transaction.simple(date(2026, 6, 3), "Utilities", book.utilities, book.checking, "10"),
            txn,
        )
    result = query_expense_explorer(
        db, PlanQuery(start=date(2026, 6, 1), end=date(2026, 6, 30), today=date(2026, 6, 10))
    )
    assert result.value is not None
    rows = {row.account: row.periods[0] for row in result.value.categories}
    assert rows[book.groceries].remaining is None
    assert rows[book.groceries].remaining_reason == "Currency conversion unavailable"
    assert rows[book.expenses].remaining is None
    assert result.value.totals[0].remaining is None
    assert rows[book.utilities].remaining == Money(-10)
