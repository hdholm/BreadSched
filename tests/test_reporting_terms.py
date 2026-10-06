"""Reporting terms have one definition across Plan, activity and exports (#235).

``Period actual`` and ``Period variance`` describe whole periods: everything
posted in the period counts, even when it is dated after the as-of date.  The
summary figures stop at the as-of date instead: *planned through as-of* is the
cash change of expectations dated on or before it (whole events, never
prorated), *actual through as-of* is what posted on or before it, and the
summary variance is their difference, so both operands cover the same dates.
"""

from datetime import date

import pytest

from breadsched.gen.engine import activity, plan_detail, planning
from breadsched.gen.lib import (
    AccountClass,
    Money,
    PeriodType,
    Recurrence,
    Scenario,
    ScenarioSchedule,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)
from breadsched.gen.services.expense_explorer import query_expense_explorer
from breadsched.gen.services.plan import PlanQuery

AS_OF = date(2026, 9, 15)


def _once(book, name: str, on: date, amount: str) -> ScheduledTransaction:
    return ScheduledTransaction(
        name=name,
        recurrence=Recurrence(PeriodType.ONCE, start=on),
        splits=[
            ScheduledSplit(book.utilities, Money(amount)),
            ScheduledSplit(book.checking, Money(f"-{amount}")),
        ],
    )


def _spend(book, on: date, amount: str, account=None) -> Transaction:
    return Transaction.simple(on, "Spending", account or book.utilities, book.checking, amount)


def _post(db, *transactions: Transaction) -> None:
    with db.transaction("post") as txn:
        for transaction in transactions:
            db.add_transaction(transaction, txn)


def _plan(db, *schedules: ScheduledTransaction) -> None:
    with db.transaction("plan") as txn:
        for schedule in schedules:
            db.add_scheduled(schedule, txn)


def _september(db, as_of: date = AS_OF) -> activity.CategoryReport:
    return activity.build_category_report(db, date(2026, 9, 1), date(2026, 9, 30), as_of=as_of)


def _row(report: activity.CategoryReport, account: str) -> activity.CategoryActivity:
    return next(item for item in report.expenses if item.account == account)


class TestTheReportedCase:
    """The field report: 40 on 9/5, 100 on 9/25, nothing planned, as-of 9/15."""

    def test_each_measure_has_its_documented_value(self, db, book):
        _post(
            db, _spend(book, date(2026, 9, 5), "40.00"), _spend(book, date(2026, 9, 25), "100.00")
        )

        report = _september(db)
        row = _row(report, book.utilities)

        # Period figures: the whole of September, the 9/25 posting included.
        assert row.actual == [Money("140.00")]
        assert row.variance == [Money("140.00")]
        assert report.cash_variance == Money("-140.00")
        # Summary figures: both sides stop at 9/15.
        assert report.planned_cash_through_as_of == Money(0)
        assert report.actual_cash_through_as_of == Money("-40.00")
        assert report.cash_variance_through_as_of == Money("-40.00")

    def test_summary_variance_equals_actual_less_planned_through_as_of(self, db, book):
        _plan(db, _once(book, "Electric", date(2026, 9, 7), "100.00"))
        _post(
            db, _spend(book, date(2026, 9, 5), "40.00"), _spend(book, date(2026, 9, 25), "100.00")
        )

        report = _september(db)

        assert report.planned_cash_through_as_of == Money("-100.00")
        assert report.actual_cash_through_as_of == Money("-40.00")
        assert report.cash_variance_through_as_of == (
            report.actual_cash_through_as_of - report.planned_cash_through_as_of
        )
        assert report.cash_variance_through_as_of == Money("60.00")


class TestDatesAroundAsOf:
    def test_an_expectation_or_posting_dated_on_as_of_counts(self, db, book):
        _plan(db, _once(book, "Due today", AS_OF, "70.00"))
        _post(db, _spend(book, AS_OF, "65.00"))

        report = _september(db)

        assert report.planned_cash_through_as_of == Money("-70.00")
        assert report.actual_cash_through_as_of == Money("-65.00")
        assert report.cash_variance_through_as_of == Money("5.00")

    def test_expectations_dated_after_as_of_are_not_yet_planned(self, db, book):
        _plan(
            db,
            _once(book, "Early", date(2026, 9, 7), "100.00"),
            _once(book, "Late", date(2026, 9, 25), "50.00"),
        )

        report = _september(db)
        row = _row(report, book.utilities)

        assert row.planned == [Money("150.00")]
        assert report.planned_cash_through_as_of == Money("-100.00")
        assert report.actual_cash_through_as_of == Money(0)
        assert report.cash_variance_through_as_of == Money("100.00")

    def test_a_bill_paid_before_its_planned_date_shows_as_spent_early(self, db, book):
        # Whole dated events, no proration: an early payment is ahead of plan
        # until the planned date passes.
        _plan(db, _once(book, "Insurance", date(2026, 9, 25), "100.00"))
        expected = planning.scheduled_events(db, date(2026, 9, 1), date(2026, 9, 30))[0]
        paid = _spend(book, date(2026, 9, 10), "100.00")
        planning.actualize_transaction(paid, expected)
        _post(db, paid)

        assert _september(db).cash_variance_through_as_of == Money("-100.00")
        assert _september(db, date(2026, 9, 30)).cash_variance_through_as_of == Money(0)

    def test_a_matched_occurrence_compares_the_paid_and_planned_amounts(self, db, book):
        _plan(db, _once(book, "Electric", date(2026, 9, 7), "100.00"))
        expected = planning.scheduled_events(db, date(2026, 9, 1), date(2026, 9, 30))[0]
        paid = _spend(book, date(2026, 9, 6), "95.00")
        planning.actualize_transaction(paid, expected)
        _post(db, paid)

        report = _september(db)

        assert report.planned_cash_through_as_of == Money("-100.00")
        assert report.actual_cash_through_as_of == Money("-95.00")
        assert report.cash_variance_through_as_of == Money("5.00")

    def test_refunds_reduce_actual_through_as_of(self, db, book):
        purchase = _spend(book, date(2026, 9, 5), "60.00", book.groceries)
        refund = Transaction.simple(
            date(2026, 9, 10), "Refund", book.checking, book.groceries, "20.00"
        )
        late_refund = Transaction.simple(
            date(2026, 9, 20), "Refund", book.checking, book.groceries, "10.00"
        )
        _post(db, purchase, refund, late_refund)

        report = _september(db)
        row = _row(report, book.groceries)

        assert row.actual == [Money("30.00")]
        assert report.actual_cash_through_as_of == Money("-40.00")

    def test_a_horizon_starting_after_as_of_has_no_through_figures(self, db, book):
        _plan(db, _once(book, "Electric", date(2026, 10, 7), "100.00"))

        report = activity.build_category_report(
            db, date(2026, 10, 1), date(2026, 10, 31), as_of=AS_OF
        )

        assert report.planned_cash_through_as_of is None
        assert report.actual_cash_through_as_of is None
        assert report.cash_variance_through_as_of is None


class TestPeriodsAroundAsOf:
    @pytest.fixture
    def history(self, db, book):
        bill = ScheduledTransaction(
            name="Electric",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 7, 7)),
            splits=[
                ScheduledSplit(book.utilities, Money("100.00")),
                ScheduledSplit(book.checking, Money("-100.00")),
            ],
        )
        _plan(db, bill)
        _post(
            db,
            _spend(book, date(2026, 7, 7), "100.00"),
            _spend(book, date(2026, 8, 7), "110.00"),
            _spend(book, date(2026, 9, 7), "90.00"),
            _spend(book, date(2026, 9, 20), "30.00"),
            _spend(book, date(2026, 11, 7), "80.00"),
        )
        return db

    def test_past_current_and_future_periods(self, history, book):
        report = activity.build_category_report(
            history, date(2026, 7, 1), date(2026, 12, 31), as_of=AS_OF
        )
        row = _row(report, book.utilities)

        assert row.actual == [
            Money("100.00"),
            Money("110.00"),
            Money("120.00"),
            Money(0),
            Money("80.00"),
            Money(0),
        ]
        # Periods that have not started have no variance; the current period's
        # variance is still a whole-period figure.
        assert row.variance == [Money(0), Money("10.00"), Money("20.00"), None, None, None]
        assert report.cash_variance == Money("-30.00")
        assert report.planned_cash_through_as_of == Money("-300.00")
        assert report.actual_cash_through_as_of == Money("-300.00")
        assert report.cash_variance_through_as_of == Money(0)

    @pytest.mark.parametrize("period", list(activity.ReportingPeriod))
    def test_through_figures_do_not_depend_on_the_grouping(self, history, period):
        report = activity.build_category_report(
            history, date(2026, 7, 1), date(2026, 12, 31), period=period, as_of=AS_OF
        )

        assert report.planned_cash_through_as_of == Money("-300.00")
        assert report.actual_cash_through_as_of == Money("-300.00")
        assert report.cash_variance_through_as_of == Money(0)

    def test_activity_json_carries_the_through_figures(self, history):
        report = activity.build_activity_report(
            history, date(2026, 7, 1), date(2026, 12, 31), as_of=AS_OF
        )
        payload = report.as_dict()

        assert payload["planned_cash_through_as_of"] == Money("-300.00")
        assert payload["actual_cash_through_as_of"] == Money("-300.00")
        assert payload["cash_variance_through_as_of"] == Money(0)


class TestCellDetail:
    def test_a_posting_after_as_of_explains_where_it_counts(self, db, book):
        _post(
            db, _spend(book, date(2026, 9, 5), "40.00"), _spend(book, date(2026, 9, 25), "100.00")
        )

        detail = plan_detail.explain_category_period(
            db, book.utilities, date(2026, 9, 1), date(2026, 9, 30), as_of=AS_OF
        )
        by_date = {
            item.post_date: " ".join(item.explanation) for item in detail.actual_transactions
        }

        assert "after the as-of date (2026-09-15)" in by_date[date(2026, 9, 25)]
        assert "counted in the period actual and period variance" in by_date[date(2026, 9, 25)]
        assert "not in actual through as-of" in by_date[date(2026, 9, 25)]
        assert "after the as-of date" not in by_date[date(2026, 9, 5)]


class TestPlanSources:
    def test_a_recurring_estimate_counts_only_occurrences_dated_through_as_of(self, db, book):
        groceries = ScheduledTransaction(
            name="Weekly groceries estimate",
            recurrence=Recurrence(PeriodType.WEEK, start=date(2026, 9, 4)),
            splits=[
                ScheduledSplit(book.groceries, Money("300.00")),
                ScheduledSplit(book.checking, Money("-300.00")),
            ],
        )
        _plan(db, groceries)

        report = _september(db)

        # 9/4, 9/11, 9/18 and 9/25 are planned; only the first two are due by 9/15.
        assert _row(report, book.groceries).planned == [Money("1200.00")]
        assert report.planned_cash_through_as_of == Money("-600.00")

    def test_scenarios_share_actuals_and_differ_only_in_plan(self, db, book):
        _plan(db, _once(book, "Electric", date(2026, 9, 7), "100.00"))
        _post(db, _spend(book, date(2026, 9, 6), "90.00"))
        schedule = ScenarioSchedule(
            name="Higher electric",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 9, 10)),
            splits=[
                ScheduledSplit(book.utilities, Money("150.00")),
                ScheduledSplit(book.checking, Money("-150.00")),
            ],
        )
        scenario = Scenario(name="Higher bills", schedule_overrides=[schedule])

        base = _september(db)
        alternate = activity.build_category_report(
            db, date(2026, 9, 1), date(2026, 9, 30), scenario=scenario, as_of=AS_OF
        )

        assert base.actual_cash_through_as_of == alternate.actual_cash_through_as_of
        assert base.cash_variance_through_as_of == Money("10.00")
        assert alternate.planned_cash_through_as_of == (
            base.planned_cash_through_as_of - Money("150.00")
        )
        assert alternate.cash_variance_through_as_of == Money("160.00")


class TestHierarchyAndRollover:
    def test_parent_rows_roll_up_period_figures_without_changing_the_summary(self, db, book):
        _post(
            db, _spend(book, date(2026, 9, 5), "40.00"), _spend(book, date(2026, 9, 25), "100.00")
        )

        report = _september(db)

        assert _row(report, book.expenses).actual == [Money("140.00")]
        assert _row(report, book.utilities).actual == [Money("140.00")]
        assert report.category_grand_total(
            AccountClass.EXPENSE, activity.PlanMeasure.ACTUAL
        ) == Money("140.00")
        assert report.actual_cash_through_as_of == Money("-40.00")

    def test_remaining_and_rollover_use_actual_through_as_of(self, db, book):
        bill = ScheduledTransaction(
            name="Electric",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 8, 7)),
            splits=[
                ScheduledSplit(book.utilities, Money("100.00")),
                ScheduledSplit(book.checking, Money("-100.00")),
            ],
        )
        _plan(db, bill)
        _post(
            db,
            _spend(book, date(2026, 8, 7), "80.00"),
            _spend(book, date(2026, 9, 5), "40.00"),
            _spend(book, date(2026, 9, 25), "100.00"),
        )
        request = PlanQuery(start=date(2026, 8, 1), end=date(2026, 10, 31), today=AS_OF)

        result = query_expense_explorer(db, request, rollover=True)

        assert result.value is not None
        category = next(c for c in result.value.categories if c.account == book.utilities)
        august, september, october = category.periods
        assert (august.actual, august.actual_to_date, august.remaining) == (
            Money("80.00"),
            Money("80.00"),
            Money("20.00"),
        )
        assert september.actual == Money("140.00")
        assert september.variance == Money("40.00")
        assert september.actual_to_date == Money("40.00")
        assert september.carry_in == Money("20.00")
        # Carry 20 + plan 100 - actual through as-of 40; the 9/25 posting waits.
        assert september.remaining == Money("80.00")
        assert october.remaining is None
        assert october.remaining_reason == "Future period"
