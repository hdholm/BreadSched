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
    assert not explorer.drilldown.income
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


def test_rollover_is_opt_in_and_bridges_completed_periods(db, book):
    estimate = ScheduledTransaction(
        name="Monthly food plan",
        recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 5, 2)),
        splits=[
            ScheduledSplit(book.groceries, Money(100)),
            ScheduledSplit(book.checking, Money(-100)),
        ],
    )
    with db.transaction("Two months") as txn:
        db.add_scheduled(estimate, txn)
        for when, amount in ((date(2026, 5, 5), "80"), (date(2026, 6, 5), "130")):
            db.add_transaction(
                Transaction.simple(when, "Food", book.groceries, book.checking, amount), txn
            )
    request = PlanQuery(start=date(2026, 5, 1), end=date(2026, 6, 30), today=date(2026, 6, 20))
    plain = query_expense_explorer(db, request).value
    carried = query_expense_explorer(
        db, request, account=book.groceries, period_index=1, rollover=True
    ).value
    assert plain is not None and carried is not None and carried.drilldown is not None
    plain_row = next(row for row in plain.categories if row.account == book.groceries)
    row = next(row for row in carried.categories if row.account == book.groceries)
    assert [item.carry_in for item in plain_row.periods] == [None, None]
    assert plain_row.periods[1].remaining == Money(-30)
    assert [(item.carry_in, item.remaining) for item in row.periods] == [
        (Money(0), Money(20)),
        (Money(20), Money(-10)),
    ]
    assert carried.drilldown.period == row.periods[1]
    assert carried.totals[1].carry_in == Money(20)
    assert carried.totals[1].remaining == Money(-10)
    html = expense_explorer_report(carried)
    assert "Carry in" in html
    assert "(10.00)" in html


def test_rollover_stops_when_a_prior_currency_period_is_unavailable(db, book):
    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    first = Transaction.simple(
        date(2026, 5, 5), "Foreign food", book.groceries, book.checking, "20"
    )
    first.currency = euro.handle
    with db.transaction("Foreign prior period") as txn:
        db.add_commodity(euro, txn)
        db.add_transaction(first, txn)
        db.add_transaction(
            Transaction.simple(date(2026, 6, 5), "Food", book.groceries, book.checking, "10"),
            txn,
        )
    result = query_expense_explorer(
        db,
        PlanQuery(start=date(2026, 5, 1), end=date(2026, 6, 30), today=date(2026, 6, 20)),
        rollover=True,
    )
    assert result.value is not None
    row = next(row for row in result.value.categories if row.account == book.groceries)
    assert row.periods[0].remaining_reason == "Currency conversion unavailable"
    assert row.periods[1].remaining is None
    assert row.periods[1].remaining_reason == "Prior period unavailable"


def test_spending_over_time_splits_totals_by_category_and_marks_as_of(db, book):
    estimate = ScheduledTransaction(
        name="Monthly groceries",
        recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 5)),
        splits=[
            ScheduledSplit(book.groceries, Money(100)),
            ScheduledSplit(book.checking, Money(-100)),
        ],
    )
    with db.transaction("Spending history") as txn:
        db.add_scheduled(estimate, txn)
        for when, account, amount in (
            (date(2026, 1, 9), book.groceries, "80"),
            (date(2026, 1, 20), book.utilities, "45"),
            (date(2026, 2, 6), book.groceries, "120"),
            (date(2026, 2, 7), book.expenses, "5"),
        ):
            db.add_transaction(
                Transaction.simple(when, "Spend", account, book.checking, amount), txn
            )
    result = query_expense_explorer(
        db,
        PlanQuery(start=date(2026, 1, 1), end=date(2026, 3, 31), today=date(2026, 2, 10)),
    )
    assert result.value is not None
    points = result.value.spending
    assert [point.label for point in points] == [item.label for item in result.value.totals]
    assert [(point.future, point.partial) for point in points] == [
        (False, False),
        (False, True),
        (True, False),
    ]
    for point, total in zip(points, result.value.totals, strict=True):
        assert (point.planned, point.actual) == (total.planned, total.actual)
        assert sum((amount for _handle, amount in point.categories), Money(0)) == point.actual
        assert not point.currency_incomplete
    # The lone "Expenses" root is split into its child categories; what was
    # posted to it directly keeps its own entry.
    january, february = dict(points[0].categories), dict(points[1].categories)
    assert january[book.groceries] == Money(80)
    assert january[book.utilities] == Money(45)
    assert february[book.groceries] == Money(120)
    assert february[book.expenses] == Money(5)
    assert points[0].planned == Money(100)


def test_printable_expense_report_includes_spending_over_time(db, book):
    with db.transaction("Spend") as txn:
        db.add_transaction(
            Transaction.simple(date(2026, 1, 9), "Spend", book.groceries, book.checking, "80"),
            txn,
        )
    result = query_expense_explorer(
        db,
        PlanQuery(start=date(2026, 1, 1), end=date(2026, 2, 28), today=date(2026, 1, 15)),
        account=book.groceries,
        period_index=0,
    )
    assert result.value is not None
    html = expense_explorer_report(result.value)
    assert "<h2>Spending over time</h2>" in html
    assert "to date" in html and "future" in html


def test_income_over_time_uses_plan_income_and_reconciles(db, book):
    pay = ScheduledTransaction(
        name="Pay",
        recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 15)),
        splits=[
            ScheduledSplit(book.checking, Money(3000)),
            ScheduledSplit(book.salary, Money(-3000)),
        ],
    )
    with db.transaction("Income history") as txn:
        db.add_scheduled(pay, txn)
        for when, amount in ((date(2026, 1, 15), "3000"), (date(2026, 2, 13), "3100")):
            db.add_transaction(
                Transaction.simple(when, "Pay", book.checking, book.salary, amount), txn
            )
        db.add_transaction(
            Transaction.simple(date(2026, 1, 9), "Spend", book.groceries, book.checking, "80"),
            txn,
        )
    result = query_expense_explorer(
        db,
        PlanQuery(start=date(2026, 1, 1), end=date(2026, 3, 31), today=date(2026, 2, 20)),
    )
    assert result.value is not None
    explorer = result.value
    points = explorer.income
    assert [point.label for point in points] == [point.label for point in explorer.spending]
    assert [(point.future, point.partial) for point in points] == [
        (False, False),
        (False, True),
        (True, False),
    ]
    # Income is positive, planned from the schedule, and actual from the ledger;
    # expenses never leak into it.
    assert [point.actual for point in points] == [Money(3000), Money(3100), Money(0)]
    assert [point.planned for point in points] == [Money(3000), Money(3000), Money(3000)]
    for point in points:
        assert sum((amount for _handle, amount in point.categories), Money(0)) == point.actual
        assert not point.currency_incomplete
    assert dict(points[0].categories) == {book.salary: Money(3000)}
    names = {row.account: row.full_name for row in explorer.income_categories}
    assert names[book.salary].endswith("Salary")
    selected = query_expense_explorer(
        db,
        PlanQuery(start=date(2026, 1, 1), end=date(2026, 3, 31), today=date(2026, 2, 20)),
        account=book.groceries,
        period_index=0,
    )
    assert selected.value is not None
    html = expense_explorer_report(selected.value)
    assert "<h2>Income over time</h2>" in html
    assert "3,100.00" in html


def test_income_drilldown_lists_the_dated_events_behind_a_period(db, book):
    pay = ScheduledTransaction(
        name="Pay",
        recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 15)),
        splits=[
            ScheduledSplit(book.checking, Money(3000)),
            ScheduledSplit(book.salary, Money(-3000)),
        ],
    )
    with db.transaction("Income history") as txn:
        db.add_scheduled(pay, txn)
        db.add_transaction(
            Transaction.simple(date(2026, 2, 13), "Employer", book.checking, book.salary, "3100"),
            txn,
        )
        db.add_transaction(
            Transaction.simple(date(2026, 2, 20), " ", book.checking, book.salary, "25"), txn
        )
    request = PlanQuery(start=date(2026, 1, 1), end=date(2026, 3, 31), today=date(2026, 2, 25))
    result = query_expense_explorer(db, request, account=book.salary, period_index=1)
    assert result.value is not None and result.value.drilldown is not None
    detail = result.value.drilldown
    assert detail.income and detail.account == book.salary
    assert detail.period.label == "Feb 2026"
    # The dated planned occurrence and the actuals that make the period's total.
    assert [event.planned_date for event in detail.planned_events] == [date(2026, 2, 15)]
    assert detail.planned_events[0].expected == Money(3000)
    assert [(item.post_date, item.amount) for item in detail.actual_transactions] == [
        (date(2026, 2, 13), Money(3100)),
        (date(2026, 2, 20), Money(25)),
    ]
    assert [(group.name, group.amount) for group in detail.merchants] == [
        ("Employer", Money(3100)),
        ("Unknown payer", Money(25)),
    ]
    assert detail.period.actual == Money(3125)
