"""Period reports are derived from exact-dated plan events and ledger actuals."""

from datetime import date

from cashperspective.gen.engine import activity, planning
from cashperspective.gen.lib import (
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)


def _monthly_bill(book, *, start: date, amount: str = "100.00") -> ScheduledTransaction:
    return ScheduledTransaction(
        name="Electric",
        recurrence=Recurrence(PeriodType.MONTH, start=start),
        splits=[
            ScheduledSplit(book.utilities, Money(amount)),
            ScheduledSplit(book.checking, Money(f"-{amount}")),
        ],
    )


class TestActivityAggregation:
    def test_months_group_expected_and_unexpected_actuals_without_driving_dates(
        self, db, book
    ):
        bill = _monthly_bill(book, start=date(2026, 1, 7))
        with db.transaction("plan") as txn:
            db.add_scheduled(bill, txn)
            db.add_transaction(
                Transaction.simple(
                    date(2026, 1, 20),
                    "Unplanned groceries",
                    book.groceries,
                    book.checking,
                    "35.00",
                ),
                txn,
            )

        report = activity.build_activity_report(
            db, date(2026, 1, 1), date(2026, 2, 28)
        )

        january, february = report.periods
        assert [event.planned_date for event in january.planned_events] == [
            date(2026, 1, 7)
        ]
        assert [event.planned_date for event in february.planned_events] == [
            date(2026, 2, 7)
        ]
        assert january.planned_amount == Money("100.00")
        assert january.actual_amount == Money("35.00")
        assert january.planned_cash_change == Money("-100.00")
        assert january.actual_cash_change == Money("-35.00")
        assert len(january.unresolved) == 1
        assert len(january.unexpected) == 1
        assert report.unresolved_count == 2
        assert report.unexpected_count == 1

    def test_matched_actual_keeps_expected_and_actual_in_their_own_date_periods(
        self, db, book
    ):
        bill = ScheduledTransaction(
            name="Month-end electric",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 31)),
            splits=[
                ScheduledSplit(book.utilities, Money("180.00")),
                ScheduledSplit(book.checking, Money("-180.00")),
            ],
        )
        with db.transaction("plan") as txn:
            db.add_scheduled(bill, txn)

        actual = Transaction.simple(
            date(2026, 2, 1),
            "ELECTRIC CO",
            book.utilities,
            book.checking,
            "193.42",
        )
        expected = planning.scheduled_events(
            db, date(2026, 1, 1), date(2026, 2, 7)
        )[0]
        planning.actualize_transaction(actual, expected)
        with db.transaction("actual") as txn:
            db.add_transaction(actual, txn)

        report = activity.build_activity_report(
            db, date(2026, 1, 1), date(2026, 2, 28)
        )
        january, february = report.periods

        assert january.planned_amount == Money("180.00")
        assert january.actual_amount == Money(0)
        assert len(january.resolved) == 1
        assert len(january.unresolved) == 0
        assert february.planned_amount == Money(0)
        assert february.actual_amount == Money("193.42")
        posted = february.actual_transactions[0]
        assert posted.unexpected is False
        assert posted.planned_for == date(2026, 1, 31)
        assert posted.variance == Money("13.42")
        assert posted.date_variance_days == 1

    def test_changing_display_period_does_not_change_report_totals(self, db, book):
        payday = ScheduledTransaction(
            name="Biweekly pay",
            recurrence=Recurrence(
                PeriodType.WEEK,
                interval=2,
                start=date(2026, 1, 2),
            ),
            splits=[
                ScheduledSplit(book.checking, Money("1000.00")),
                ScheduledSplit(book.salary, Money("-1000.00")),
            ],
        )
        bill = _monthly_bill(book, start=date(2026, 1, 7), amount="125.00")
        with db.transaction("plan") as txn:
            db.add_scheduled(payday, txn)
            db.add_scheduled(bill, txn)

        start, end = date(2026, 1, 1), date(2026, 6, 30)
        monthly = activity.build_activity_report(
            db, start, end, period=activity.ReportingPeriod.MONTH
        )
        quarterly = activity.build_activity_report(
            db, start, end, period=activity.ReportingPeriod.QUARTER
        )
        yearly = activity.build_activity_report(
            db, start, end, period=activity.ReportingPeriod.YEAR
        )

        assert len(monthly.periods) == 6
        assert len(quarterly.periods) == 2
        assert len(yearly.periods) == 1
        assert monthly.planned_amount == quarterly.planned_amount == yearly.planned_amount
        assert (
            monthly.planned_cash_change
            == quarterly.planned_cash_change
            == yearly.planned_cash_change
        )
        assert monthly.unresolved_count == quarterly.unresolved_count == yearly.unresolved_count

    def test_legacy_scheduled_actual_is_not_reported_as_unexpected(self, db, book):
        bill = _monthly_bill(book, start=date(2026, 4, 9))
        with db.transaction("plan") as txn:
            db.add_scheduled(bill, txn)

        actual = Transaction.simple(
            date(2026, 4, 9),
            "Legacy scheduled bill",
            book.utilities,
            book.checking,
            "100.00",
        )
        actual.scheduled_from = bill.handle
        with db.transaction("legacy actual") as txn:
            db.add_transaction(actual, txn)

        report = activity.build_activity_report(
            db, date(2026, 4, 1), date(2026, 4, 30)
        )

        assert report.unexpected_count == 0
        assert report.periods[0].actual_transactions[0].unexpected is False

    def test_json_shape_contains_drill_down_provenance(self, db, book):
        bill = _monthly_bill(book, start=date(2026, 3, 8))
        with db.transaction("plan") as txn:
            db.add_scheduled(bill, txn)

        report = activity.build_activity_report(
            db, date(2026, 3, 1), date(2026, 3, 31)
        )
        payload = report.as_dict()

        assert payload["period"] == "month"
        assert payload["unresolved_count"] == 1
        period = payload["periods"][0]
        assert period["label"] == "Mar 2026"
        assert period["planned_events"][0]["planned_date"] == date(2026, 3, 8)
        assert period["planned_events"][0]["source"] == "scheduled"
