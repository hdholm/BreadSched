"""Expense exploration reconciles with the existing Plan expense cells."""

from datetime import date

from breadsched.gen.engine.activity import ReportingPeriod
from breadsched.gen.lib import (
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
