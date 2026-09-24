"""Expense exploration reconciles with the existing Plan expense cells."""

from datetime import date

from breadsched.gen.engine.activity import ReportingPeriod
from breadsched.gen.lib import (
    Account,
    AccountType,
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
    assert [(group.name, group.amount) for group in explorer.drilldown.merchants] == [
        ("Shop A", Money(55)),
        ("Shop B", Money(65)),
    ]
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
    assert result.value.totals[0].variance is None
