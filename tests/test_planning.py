"""Scheduled events are the source of truth for planning and actualization."""

from datetime import date
from decimal import Decimal

from breadsched.gen.engine import planning, projection, schedule
from breadsched.gen.lib import (
    Assumptions,
    Money,
    PeriodType,
    ProjectionBasis,
    Recurrence,
    Scenario,
    ScenarioSchedule,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)


def _flat() -> Assumptions:
    return Assumptions(
        income_growth="0",
        expense_inflation="0",
        investment_return="0",
        cash_interest="0",
        liability_interest="0",
    )


class TestEventDomain:
    def test_new_scenarios_are_schedule_driven_by_default(self):
        assert Scenario().basis is ProjectionBasis.SCHEDULED

    def test_schedule_occurrences_keep_their_exact_dates(self, db, book):
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
        with db.transaction("pay schedule") as txn:
            db.add_scheduled(payday, txn)

        events = planning.scheduled_events(db, date(2026, 1, 1), date(2026, 1, 31))

        assert [event.planned_date for event in events] == [
            date(2026, 1, 2),
            date(2026, 1, 16),
            date(2026, 1, 30),
        ]
        assert all(event.key.startswith(f"scheduled:{payday.handle}:") for event in events)

    def test_scenario_can_replace_a_baseline_schedule_without_mutating_it(self, db, book):
        salary = ScheduledTransaction(
            name="Salary",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 5)),
            splits=[
                ScheduledSplit(book.checking, Money("1000.00")),
                ScheduledSplit(book.salary, Money("-1000.00")),
            ],
        )
        with db.transaction("salary") as txn:
            db.add_scheduled(salary, txn)

        alternate = ScenarioSchedule.from_scheduled(salary)
        alternate.splits = [
            ScheduledSplit(book.checking, Money("600.00")),
            ScheduledSplit(book.salary, Money("-600.00")),
        ]
        scenario = Scenario(name="Reduced hours", schedule_overrides=[alternate])
        events = planning.scenario_events(
            db, scenario, date(2026, 1, 1), date(2026, 1, 31)
        )

        assert len(events) == 1
        assert events[0].source is planning.EventSource.SCENARIO_SCHEDULE
        assert events[0].expected_amount == Money("600.00")
        assert salary.amount(when=date(2026, 1, 5)) == Money("1000.00")

    def test_disabled_scenario_override_suppresses_a_baseline_schedule(self, db, book):
        salary = ScheduledTransaction(
            name="Salary",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 5)),
            splits=[
                ScheduledSplit(book.checking, Money("1000.00")),
                ScheduledSplit(book.salary, Money("-1000.00")),
            ],
        )
        with db.transaction("salary") as txn:
            db.add_scheduled(salary, txn)

        scenario = Scenario(
            name="Retired",
            schedule_overrides=[ScenarioSchedule.from_scheduled(salary, enabled=False)],
        )

        assert planning.scenario_events(
            db, scenario, date(2026, 1, 1), date(2026, 1, 31)
        ) == []

    def test_posting_a_schedule_actualizes_the_generated_occurrence(self, db, book):
        bill = ScheduledTransaction(
            name="Electric",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 5)),
            splits=[
                ScheduledSplit(book.utilities, Money("180.00")),
                ScheduledSplit(book.checking, Money("-180.00")),
            ],
            auto_create=True,
        )
        with db.transaction("bill schedule") as txn:
            db.add_scheduled(bill, txn)

        posted = schedule.post_due(db, as_of=date(2026, 1, 5))
        assert len(posted) == 1
        transaction = posted[0]
        assert transaction.planned_occurrence == bill.occurrence_key(date(2026, 1, 5))
        assert transaction.planned_for == date(2026, 1, 5)
        assert transaction.planned_amount == Money("180.00")

        event = planning.scheduled_events(
            db, date(2026, 1, 1), date(2026, 1, 31)
        )[0]
        assert event.status is planning.EventStatus.ACTUALIZED
        assert event.actual_transaction == transaction.handle
        assert event.expected_amount == Money("180.00")
        assert event.actual_amount == Money("180.00")
        assert event.variance == Money(0)

    def test_a_new_actual_can_be_matched_without_destroying_the_estimate(self, db, book):
        bill = ScheduledTransaction(
            name="Electric",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 2, 7)),
            splits=[
                ScheduledSplit(book.utilities, Money("180.00")),
                ScheduledSplit(book.checking, Money("-180.00")),
            ],
        )
        with db.transaction("bill schedule") as txn:
            db.add_scheduled(bill, txn)

        actual = Transaction.simple(
            date(2026, 2, 8),
            "ELECTRIC CO",
            book.utilities,
            book.checking,
            "193.42",
        )
        candidates = planning.match_candidates(db, actual)
        assert candidates
        assert candidates[0].event.source_handle == bill.handle

        planning.actualize_transaction(actual, candidates[0].event)
        with db.transaction("record actual") as txn:
            db.add_transaction(actual, txn)

        event = planning.scheduled_events(
            db, date(2026, 2, 1), date(2026, 2, 28)
        )[0]
        assert event.planned_date == date(2026, 2, 7)
        assert event.actual_date == date(2026, 2, 8)
        assert event.expected_amount == Money("180.00")
        assert event.actual_amount == Money("193.42")
        assert event.variance == Money("13.42")

        # Editing the future schedule must not rewrite what this actual was
        # compared against when it was resolved.
        bill.splits[0].amount = Money("200.00")
        bill.splits[1].amount = Money("-200.00")
        with db.transaction("revise future estimate") as txn:
            db.commit_scheduled(bill, txn)
        event = planning.scheduled_events(
            db, date(2026, 2, 1), date(2026, 2, 28)
        )[0]
        assert event.expected_amount == Money("180.00")
        assert event.variance == Money("13.42")

        result = projection.project(
            db,
            Scenario(
                name="Actualized February",
                start=date(2026, 2, 1),
                years=1,
                assumptions=_flat(),
            ),
        )
        assert result.rows[0].expense == Money("193.42")
        assert result.rows[0].ledger.events[0].planned_date == date(2026, 2, 7)
        assert result.rows[0].ledger.events[0].when == date(2026, 2, 8)


class TestEventDrivenProjection:
    def test_months_are_reporting_buckets_for_exact_dated_events(self, db, book):
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
        with db.transaction("pay schedule") as txn:
            db.add_scheduled(payday, txn)

        result = projection.project(
            db,
            Scenario(
                name="Event dates",
                start=date(2026, 1, 1),
                years=1,
                basis=ProjectionBasis.SCHEDULED,
                assumptions=_flat(),
            ),
        )

        january = result.rows[0]
        assert january.income == Money("3000.00")
        assert [event.when for event in january.ledger.events] == [
            date(2026, 1, 2),
            date(2026, 1, 16),
            date(2026, 1, 30),
        ]
        assert january.ledger.reconciles()

    def test_income_growth_escalates_future_scheduled_paychecks(self, db, book):
        payday = ScheduledTransaction(
            name="Monthly pay",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 5)),
            splits=[
                ScheduledSplit(book.checking, Money("1000.00")),
                ScheduledSplit(book.salary, Money("-1000.00")),
            ],
        )
        with db.transaction("pay schedule") as txn:
            db.add_scheduled(payday, txn)

        result = projection.project(
            db,
            Scenario(
                name="Raises",
                start=date(2026, 1, 1),
                years=2,
                basis=ProjectionBasis.SCHEDULED,
                assumptions=Assumptions(
                    income_growth="0.10",
                    expense_inflation="0",
                    investment_return="0",
                    cash_interest="0",
                    liability_interest="0",
                ),
            ),
        )

        assert result.rows[0].income == Money("1000.00")
        assert result.rows[12].income == Money("1100.00")
        assert result.rows[12].cash_close - result.rows[11].cash_close == Money("1100.00")

    def test_expense_inflation_escalates_the_balanced_scheduled_bill(self, db, book):
        bill = ScheduledTransaction(
            name="Monthly utility",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 8)),
            splits=[
                ScheduledSplit(book.utilities, Money("100.00")),
                ScheduledSplit(book.checking, Money("-100.00")),
            ],
        )
        with db.transaction("bill schedule") as txn:
            db.add_scheduled(bill, txn)

        result = projection.project(
            db,
            Scenario(
                name="Inflation",
                start=date(2026, 1, 1),
                years=2,
                basis=ProjectionBasis.SCHEDULED,
                assumptions=Assumptions(
                    income_growth="0",
                    expense_inflation="0.05",
                    investment_return="0",
                    cash_interest="0",
                    liability_interest="0",
                ),
            ),
        )

        assert result.rows[0].expense == Money("100.00")
        assert result.rows[12].expense == Money("105.00")
        assert result.rows[12].ledger.reconciles()

    def test_event_date_changes_how_long_a_contribution_earns_return(self, db, book):
        contribution = ScheduledTransaction(
            name="Contribution",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(book.brokerage, Money("10000.00")),
                ScheduledSplit(book.checking, Money("-10000.00")),
            ],
        )
        with db.transaction("contribution") as txn:
            db.add_scheduled(contribution, txn)

        scenario = Scenario(
            name="Timing",
            start=date(2026, 1, 1),
            years=1,
            basis=ProjectionBasis.SCHEDULED,
            assumptions=_flat(),
        )
        scenario.assumptions.per_account[book.brokerage] = Decimal("0.12")
        early = projection.project(db, scenario)

        contribution.recurrence = Recurrence(PeriodType.ONCE, start=date(2026, 1, 31))
        with db.transaction("move contribution") as txn:
            db.commit_scheduled(contribution, txn)
        late = projection.project(db, scenario)

        assert early.rows[0].investment_growth > late.rows[0].investment_growth
        assert early.rows[-1].holdings > late.rows[-1].holdings

    def test_liability_charges_and_payments_apply_to_the_named_debt(self, db, book):
        charge = ScheduledTransaction(
            name="Card purchase",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 3)),
            splits=[
                ScheduledSplit(book.groceries, Money("100.00")),
                ScheduledSplit(book.card, Money("-100.00")),
            ],
        )
        payment = ScheduledTransaction(
            name="Card payment",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 20)),
            splits=[
                ScheduledSplit(book.card, Money("40.00")),
                ScheduledSplit(book.checking, Money("-40.00")),
            ],
        )
        with db.transaction("card plan") as txn:
            db.add_scheduled(charge, txn)
            db.add_scheduled(payment, txn)

        scenario = Scenario(
            name="Named debt",
            start=date(2026, 1, 1),
            years=1,
            basis=ProjectionBasis.SCHEDULED,
            assumptions=_flat(),
        )
        scenario.assumptions.per_account[book.card] = Decimal("0")
        january = projection.project(db, scenario).rows[0]

        assert january.ledger.liability_movements[book.card] == Money("60.00")
        assert january.ledger.debt_payments[book.card] == Money("40.00")
        assert january.liabilities == Money("60.00")
        assert january.ledger.reconciles()

class TestActualResolutionWorkflow:
    def _bill_and_actual(self, db, book):
        bill = ScheduledTransaction(
            name="Electric",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 5, 7)),
            splits=[
                ScheduledSplit(book.utilities, Money("180.00")),
                ScheduledSplit(book.checking, Money("-180.00")),
            ],
        )
        with db.transaction("plan bill") as txn:
            db.add_scheduled(bill, txn)
        actual = Transaction.simple(
            date(2026, 5, 8),
            "ELECTRIC CO",
            book.utilities,
            book.checking,
            "193.42",
        )
        return bill, actual

    def test_rejected_candidate_is_not_offered_again_and_persists(self, db, book):
        _bill, actual = self._bill_and_actual(db, book)
        candidate = planning.match_candidates(db, actual)[0]
        planning.reject_candidate(actual, candidate.event)
        with db.transaction("record unresolved actual") as txn:
            db.add_transaction(actual, txn)

        reloaded = db.get_transaction(actual.handle)
        assert reloaded is not None
        assert reloaded.planning_resolution.value == "unresolved"
        assert candidate.event.key in reloaded.rejected_plan_occurrences
        assert planning.match_candidates(db, reloaded) == []

    def test_marking_unexpected_suppresses_future_match_suggestions(self, db, book):
        _bill, actual = self._bill_and_actual(db, book)
        assert planning.match_candidates(db, actual)

        planning.mark_unexpected(actual)
        with db.transaction("record unexpected actual") as txn:
            db.add_transaction(actual, txn)

        reloaded = db.get_transaction(actual.handle)
        assert reloaded is not None
        assert reloaded.planning_resolution.value == "unexpected"
        assert planning.match_candidates(db, reloaded) == []

    def test_matching_sets_explicit_resolution_and_clears_rejections(self, db, book):
        _bill, actual = self._bill_and_actual(db, book)
        event = planning.unresolved_events(
            db, date(2026, 5, 1), date(2026, 5, 31)
        )[0]
        actual.rejected_plan_occurrences.append("scheduled:not-this-one:2026-05-07")

        planning.actualize_transaction(actual, event)

        assert actual.planning_resolution.value == "matched"
        assert actual.rejected_plan_occurrences == []
        assert actual.planned_occurrence == event.key
        assert planning.event_by_key(db, event.key) is not None
