"""Scheduled events are the source of truth for planning and actualization."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

from breadsched.gen.engine import activity, planning, projection, schedule
from breadsched.gen.lib import (
    Account,
    AccountType,
    Assumptions,
    Money,
    PeriodType,
    Recurrence,
    Scenario,
    ScenarioSchedule,
    ScheduledAmountChange,
    ScheduledMonthAmount,
    ScheduledOccurrenceAdjustment,
    ScheduledSplit,
    ScheduledSplitAmountChange,
    ScheduledTransaction,
    Split,
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
    def test_scenarios_do_not_carry_a_projection_basis(self):
        assert not hasattr(Scenario(), "basis")

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

    def test_future_amount_changes_flow_into_exact_dated_events(self, db, book):
        rent = ScheduledTransaction(
            name="Rent",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(book.rent, Money("1800.00")),
                ScheduledSplit(book.checking, Money("-1800.00")),
            ],
            amount_changes=[
                ScheduledAmountChange(date(2026, 7, 1), Money("1950.00")),
                ScheduledAmountChange(date(2027, 1, 1), Money("2100.00")),
            ],
        )
        with db.transaction("rent schedule") as txn:
            db.add_scheduled(rent, txn)

        events = planning.scheduled_events(db, date(2026, 6, 1), date(2027, 1, 31))

        by_date = {event.planned_date: event.expected_amount for event in events}
        assert by_date[date(2026, 6, 1)] == Money("1800.00")
        assert by_date[date(2026, 7, 1)] == Money("1950.00")
        assert by_date[date(2026, 12, 1)] == Money("1950.00")
        assert by_date[date(2027, 1, 1)] == Money("2100.00")

    def test_future_amount_changes_round_trip_with_schedule(self):
        schedule = ScheduledTransaction(
            name="Rent",
            splits=[
                ScheduledSplit("expense", Money("1800")),
                ScheduledSplit("cash", Money("-1800")),
            ],
            amount_changes=[ScheduledAmountChange(date(2030, 1, 1), Money("1950"))],
        )

        clone = ScheduledTransaction.from_dict(schedule.serialize())

        assert clone.amount(when=date(2029, 12, 1)) == Money("1800")
        assert clone.amount(when=date(2030, 1, 1)) == Money("1950")

    def test_per_leg_timelines_round_trip_and_explain_plan_and_projection(self, db, book):
        effective = date(2026, 7, 1)
        payroll = ScheduledTransaction(
            name="Payroll with deductions",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(book.salary, Money("-1000")),
                ScheduledSplit(
                    book.utilities,
                    Money("200"),
                    amount_changes=[ScheduledSplitAmountChange(effective, Money("225"))],
                ),
                ScheduledSplit(
                    book.brokerage,
                    Money("100"),
                    amount_changes=[ScheduledSplitAmountChange(effective, Money("125"))],
                ),
                ScheduledSplit(
                    book.checking,
                    Money("700"),
                    amount_changes=[ScheduledSplitAmountChange(effective, Money("650"))],
                ),
            ],
        )
        clone = ScheduledTransaction.from_dict(payroll.serialize())

        before = dict(clone.resolved_splits(when=date(2026, 6, 1)))
        after = dict(clone.resolved_splits(when=effective))
        assert before[book.utilities] == Money("200")
        assert after[book.utilities] == Money("225")
        assert after[book.brokerage] == Money("125")
        assert after[book.checking] == Money("650")
        posted = clone.instantiate(effective)
        assert sum((split.value for split in posted.splits), Money(0)) == Money(0)

        with db.transaction("payroll schedule") as txn:
            db.add_scheduled(clone, txn)
        event = planning.scheduled_events(db, effective, effective)[0]
        sources = {split.account: split.amount_source for split in event.expected_splits}
        assert sources[book.utilities] == "per-leg amount effective 2026-07-01"
        assert event.as_dict()["amount_explanations"][1]["source"] == sources[book.utilities]

        plan_detail = activity.explain_category_period(
            db, book.utilities, effective, date(2026, 7, 31), as_of=effective
        )
        assert any(
            "Amount for Utilities: per-leg amount effective 2026-07-01." in line
            for line in plan_detail.planned_events[0].explanation
        )

        scenario = Scenario(
            name="Timeline explanation",
            start=effective,
            years=1,
            assumptions=_flat(),
        )
        result = projection.project(db, scenario)
        projected = projection.explain_month(db, result, 0).events[0]
        projected_sources = {
            split.account: split.amount_source for split in projected.expected_splits
        }
        assert projected_sources[book.brokerage] == "per-leg amount effective 2026-07-01"

    def test_seasonal_amount_profile_round_trips_and_posts_resolved_amount(self):
        schedule = ScheduledTransaction(
            name="Seasonal utilities",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 10)),
            splits=[
                ScheduledSplit("expense", Money("100")),
                ScheduledSplit("cash", Money("-100")),
            ],
            seasonal_amounts=[
                ScheduledMonthAmount(1, Money("240")),
                ScheduledMonthAmount(7, Money("240")),
            ],
        )

        clone = ScheduledTransaction.from_dict(schedule.serialize())

        assert clone.amount(when=date(2026, 1, 10)) == Money("240")
        assert clone.amount(when=date(2026, 4, 10)) == Money("100")
        assert clone.amount(when=date(2026, 7, 10)) == Money("240")
        posted = clone.instantiate(date(2026, 7, 10))
        assert posted.value_for("expense") == Money("240")
        assert posted.value_for("cash") == Money("-240")

    def test_occurrence_exceptions_skip_and_override_exact_dates(self, db, book):
        rent = ScheduledTransaction(
            name="Rent",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(book.rent, Money("1800.00")),
                ScheduledSplit(book.checking, Money("-1800.00")),
            ],
            skipped=[date(2026, 3, 1)],
            occurrence_adjustments=[
                ScheduledOccurrenceAdjustment(date(2026, 4, 1), Money("2300.00"))
            ],
        )
        with db.transaction("rent schedule") as txn:
            db.add_scheduled(rent, txn)

        events = planning.scheduled_events(db, date(2026, 1, 1), date(2026, 4, 30))

        by_date = {event.planned_date: event.expected_amount for event in events}
        assert date(2026, 3, 1) not in by_date
        assert by_date[date(2026, 2, 1)] == Money("1800.00")
        assert by_date[date(2026, 4, 1)] == Money("2300.00")

        forecast = schedule.forecast_occurrences(db, date(2026, 1, 1), date(2026, 4, 30))
        assert date(2026, 3, 1) not in {item.when for item in forecast}
        assert next(item.amount for item in forecast if item.when == date(2026, 4, 1)) == Money(
            "2300.00"
        )

    def test_occurrence_exceptions_round_trip_with_schedule(self):
        source = ScheduledTransaction(
            name="Rent",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit("expense", Money("1800")),
                ScheduledSplit("cash", Money("-1800")),
            ],
            skipped=[date(2026, 3, 1)],
            occurrence_adjustments=[ScheduledOccurrenceAdjustment(date(2026, 4, 1), Money("2300"))],
        )

        clone = ScheduledTransaction.from_dict(source.serialize())

        assert clone.skipped == [date(2026, 3, 1)]
        assert clone.amount(when=date(2026, 4, 1)) == Money("2300")

    def test_scenario_occurrence_exceptions_are_scenario_only(self, db, book):
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
        alternate.skipped = [date(2026, 2, 5)]
        alternate.occurrence_adjustments = [
            ScheduledOccurrenceAdjustment(date(2026, 3, 5), Money("1400.00"))
        ]
        scenario = Scenario(name="Alternate", schedule_overrides=[alternate])
        events = planning.scenario_events(db, scenario, date(2026, 1, 1), date(2026, 3, 31))

        by_date = {event.planned_date: event.expected_amount for event in events}
        assert date(2026, 2, 5) not in by_date
        assert by_date[date(2026, 3, 5)] == Money("1400.00")
        baseline = planning.scheduled_events(db, date(2026, 1, 1), date(2026, 3, 31))
        assert {event.planned_date for event in baseline} == {
            date(2026, 1, 5),
            date(2026, 2, 5),
            date(2026, 3, 5),
        }

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
        events = planning.scenario_events(db, scenario, date(2026, 1, 1), date(2026, 1, 31))

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

        assert planning.scenario_events(db, scenario, date(2026, 1, 1), date(2026, 1, 31)) == []

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

        event = planning.scheduled_events(db, date(2026, 1, 1), date(2026, 1, 31))[0]
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

        event = planning.scheduled_events(db, date(2026, 2, 1), date(2026, 2, 28))[0]
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
        event = planning.scheduled_events(db, date(2026, 2, 1), date(2026, 2, 28))[0]
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
        event = planning.unresolved_events(db, date(2026, 5, 1), date(2026, 5, 31))[0]
        actual.rejected_plan_occurrences.append("scheduled:not-this-one:2026-05-07")

        planning.actualize_transaction(actual, event)

        assert actual.planning_resolution.value == "matched"
        assert actual.rejected_plan_occurrences == []
        assert actual.planned_occurrence == event.key
        assert planning.event_by_key(db, event.key) is not None


class TestHistoricalEstimateProposals:
    def test_proposes_and_preserves_distinct_classified_balance_flows(self, db, book):
        from breadsched.gen.engine import estimates
        from breadsched.gen.lib import InvestmentActivityKind, PlanningFlowKind

        with db.transaction("Planning accounts") as txn:
            retirement = Account(
                name="Retirement", atype=AccountType.RETIREMENT, parent=book.assets
            )
            loan = Account(name="Mortgage", atype=AccountType.LOAN, parent=book.liabilities)
            fsa = Account(name="Health FSA", atype=AccountType.FSA, parent=book.assets)
            for account in (retirement, loan, fsa):
                db.add_account(account, txn)
            for month in (1, 2, 3):
                when = date(2026, month, 5)
                for description, target, amount, investment in (
                    ("Retirement saving", retirement.handle, Money("500"), None),
                    ("Retirement draw", retirement.handle, Money("-700"), None),
                    ("Mortgage principal", loan.handle, Money("300"), None),
                    ("FSA funding", fsa.handle, Money("100"), None),
                    (
                        "Taxable investment",
                        book.brokerage,
                        Money("250"),
                        InvestmentActivityKind.CONTRIBUTION,
                    ),
                    (
                        "Taxable withdrawal",
                        book.brokerage,
                        Money("-175"),
                        InvestmentActivityKind.WITHDRAWAL,
                    ),
                ):
                    db.add_transaction(
                        Transaction(
                            post_date=when,
                            description=description,
                            splits=[
                                Split(target, amount, investment_activity=investment),
                                Split(book.checking, -amount),
                            ],
                        ),
                        txn,
                    )

        proposals = estimates.propose_historical_estimates(db, as_of=date(2026, 4, 20), months=3)
        classified = {(item.planning_flow, item.investment_activity): item for item in proposals}
        assert classified[(PlanningFlowKind.RETIREMENT_SAVING, None)].amount == Money("500")
        assert classified[(PlanningFlowKind.RETIREMENT_INCOME, None)].amount == Money("700")
        assert classified[(PlanningFlowKind.DEBT_PRINCIPAL, None)].amount == Money("300")
        assert classified[(PlanningFlowKind.BENEFIT_FUNDING, None)].amount == Money("100")
        contribution = classified[(None, InvestmentActivityKind.CONTRIBUTION)]
        assert contribution.amount == Money("250")
        assert contribution.funding == book.checking
        withdrawal = classified[(None, InvestmentActivityKind.WITHDRAWAL)]
        assert withdrawal.amount == Money("175")
        assert withdrawal.source_name.endswith("Brokerage")
        assert withdrawal.destination_name.endswith("Checking")

        with db.transaction("Alternative") as txn:
            scenario = Scenario(name="Alternative")
            db.add_scenario(scenario, txn)
        estimates.accept_historical_estimate(db, contribution, scenario_handle=scenario.handle)
        saved_scenario = db.get_scenario(scenario.handle)
        assert saved_scenario is not None
        investment_split = next(
            split
            for split in saved_scenario.schedule_overrides[0].splits
            if split.account == book.brokerage
        )
        assert investment_split.investment_activity is InvestmentActivityKind.CONTRIBUTION
        assert any(
            item.investment_activity is InvestmentActivityKind.CONTRIBUTION
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=3
            )
        )
        assert all(
            item.investment_activity is not InvestmentActivityKind.CONTRIBUTION
            for item in estimates.propose_historical_estimates(
                db,
                as_of=date(2026, 4, 20),
                months=3,
                scenario_handle=scenario.handle,
            )
        )

        saving = classified[(PlanningFlowKind.RETIREMENT_SAVING, None)]
        handle = estimates.accept_historical_estimate(db, saving)
        saved = db.get_scheduled(handle)
        assert saved is not None
        target = next(split for split in saved.splits if split.account == retirement.handle)
        assert target.amount == Money("500")
        assert target.planning_flow is PlanningFlowKind.RETIREMENT_SAVING
        assert all(
            item.planning_flow is not PlanningFlowKind.RETIREMENT_SAVING
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=3
            )
        )

    def test_retirement_distribution_counterpart_is_not_proposed_twice(self, db, book):
        from breadsched.gen.engine import estimates
        from breadsched.gen.lib import PlanningFlowKind

        with db.transaction("Retirement distributions") as txn:
            retirement = Account(
                name="Retirement", atype=AccountType.RETIREMENT, parent=book.assets
            )
            db.add_account(retirement, txn)
            for month in (1, 2, 3):
                db.add_transaction(
                    Transaction(
                        post_date=date(2026, month, 15),
                        description="Distribution",
                        splits=[
                            Split(
                                retirement.handle,
                                "-15000",
                                planning_flow=PlanningFlowKind.RETIREMENT_INCOME,
                            ),
                            Split(
                                book.checking,
                                "15000",
                                planning_flow=PlanningFlowKind.RETIREMENT_INCOME,
                            ),
                        ],
                    ),
                    txn,
                )

        distributions = [
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=3
            )
            if item.planning_flow is PlanningFlowKind.RETIREMENT_INCOME
        ]
        assert len(distributions) == 1
        assert distributions[0].category == retirement.handle
        assert distributions[0].source_name.endswith("Retirement")
        assert distributions[0].destination_name.endswith("Checking")

    def test_investment_performance_does_not_become_ordinary_income_or_expense(self, db, book):
        from breadsched.gen.engine import estimates
        from breadsched.gen.lib import InvestmentActivityKind, Split, Transaction

        with db.transaction("Investment performance history") as txn:
            for month in (1, 2, 3):
                dividend = Transaction(
                    post_date=date(2026, month, 5), description="Reinvested distribution"
                )
                dividend.add_split(
                    Split(
                        book.brokerage,
                        Money("100"),
                        investment_activity=InvestmentActivityKind.DIVIDEND,
                    )
                )
                dividend.add_split(Split(book.salary, Money("-100")))
                db.add_transaction(dividend, txn)

                fee = Transaction(post_date=date(2026, month, 10), description="Fund fee")
                fee.add_split(Split(book.utilities, Money("10")))
                fee.add_split(
                    Split(
                        book.brokerage,
                        Money("-10"),
                        investment_activity=InvestmentActivityKind.FEE,
                    )
                )
                db.add_transaction(fee, txn)

        proposals = estimates.propose_historical_estimates(db, as_of=date(2026, 4, 20), months=3)

        assert all(item.category not in {book.salary, book.utilities} for item in proposals)

    def test_multisplit_balance_sheet_legs_do_not_replace_cash_as_funding(self, db, book):
        from breadsched.gen.engine import estimates
        from breadsched.gen.lib import Account, AccountType, Split, Transaction

        loan = Account(name="Household loan", atype=AccountType.LOAN, parent=book.liabilities)
        retirement = Account(
            name="Workplace plan", atype=AccountType.RETIREMENT, parent=book.assets
        )
        with db.transaction("Loan and payroll history") as txn:
            db.add_account(loan, txn)
            db.add_account(retirement, txn)
            for month in (1, 2, 3):
                payment = Transaction(post_date=date(2026, month, 5), description="Loan payment")
                payment.add_split(Split(book.utilities, Money("100")))
                payment.add_split(Split(loan.handle, Money("400")))
                payment.add_split(Split(book.checking, Money("-500")))
                db.add_transaction(payment, txn)

                payroll = Transaction(post_date=date(2026, month, 15), description="Payroll")
                payroll.add_split(Split(book.salary, Money("-1000")))
                payroll.add_split(Split(retirement.handle, Money("200")))
                payroll.add_split(Split(book.checking, Money("800")))
                db.add_transaction(payroll, txn)

        proposals = estimates.propose_historical_estimates(db, as_of=date(2026, 4, 20), months=3)
        by_category = {item.category: item for item in proposals}

        assert by_category[book.utilities].funding == book.checking
        assert by_category[book.salary].funding == book.checking

    def test_escrow_paid_expenses_do_not_become_uncovered_suggestions(self, db, book):
        from breadsched.gen.engine import estimates

        escrow = Account(name="Escrow", atype=AccountType.ASSET, parent=book.assets)
        escrow.atype = AccountType.ESCROW
        payout = ScheduledTransaction(
            name="Escrow payout",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 4, 5)),
            splits=[
                ScheduledSplit(book.utilities, Money("100")),
                ScheduledSplit(escrow.handle, Money("-100")),
            ],
        )
        with db.transaction("Escrow history and plan") as txn:
            db.add_account(escrow, txn)
            db.add_scheduled(payout, txn)
            for month in (1, 2, 3):
                actual = Transaction(post_date=date(2026, month, 5), description="Escrow payout")
                actual.add_split(Split(book.utilities, Money("100")))
                actual.add_split(Split(escrow.handle, Money("-100")))
                db.add_transaction(actual, txn)

        proposals = estimates.propose_historical_estimates(
            db, as_of=date(2026, 4, 20), months=3, min_active_months=1
        )
        assert all(item.category != book.utilities for item in proposals)

    def test_future_multisplit_commitment_covers_each_category_and_respects_scenario(
        self, db, book
    ):
        from breadsched.gen.engine import estimates

        bill = ScheduledTransaction(
            name="Combined bill",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 4, 5)),
            splits=[
                ScheduledSplit(book.groceries, Money("20")),
                ScheduledSplit(book.groceries, Money("40")),
                ScheduledSplit(book.groceries, Money("-10")),
                ScheduledSplit(book.utilities, Money("15")),
                ScheduledSplit(book.checking, Money("-65")),
            ],
        )
        suppressed = Scenario(
            name="Without bill",
            schedule_overrides=[ScenarioSchedule.from_scheduled(bill, enabled=False)],
        )
        with db.transaction("History and future bill") as txn:
            db.add_scheduled(bill, txn)
            db.add_scenario(suppressed, txn)
            for month in (1, 2, 3):
                actual = Transaction(post_date=date(2026, month, 8), description="Combined bill")
                actual.add_split(Split(book.groceries, Money("100")))
                actual.add_split(Split(book.utilities, Money("60")))
                actual.add_split(Split(book.checking, Money("-160")))
                db.add_transaction(actual, txn)

        def amounts(scenario_handle=None):
            return {
                item.category: item.amount
                for item in estimates.propose_historical_estimates(
                    db, as_of=date(2026, 4, 20), months=3, scenario_handle=scenario_handle
                )
            }

        assert amounts() == {book.groceries: Money("50"), book.utilities: Money("45")}
        assert amounts(suppressed.handle) == {
            book.groceries: Money("100"),
            book.utilities: Money("60"),
        }
        with db.transaction("Disable bill") as txn:
            bill.enabled = False
            db.commit_scheduled(bill, txn)
        assert amounts() == {book.groceries: Money("100"), book.utilities: Money("60")}

    def test_future_amount_changes_replace_historical_schedule_coverage(self, db, book):
        from breadsched.gen.engine import estimates

        bill = ScheduledTransaction(
            name="Known bill",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 5)),
            splits=[
                ScheduledSplit(book.rent, Money("80")),
                ScheduledSplit(book.checking, Money("-80")),
            ],
            amount_changes=[ScheduledAmountChange(date(2026, 4, 1), Money("120"))],
        )
        with db.transaction("History and changing bill") as txn:
            db.add_scheduled(bill, txn)
            for month in (1, 2, 3):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, month, 5), "Rent", book.rent, book.checking, "180"
                    ),
                    txn,
                )

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=3
            )
            if item.category == book.rent
        )
        assert proposal.amount == Money("60")
        assert proposal.scheduled_amount == Money("360")
        assert "historical median 180.00" in proposal.reason
        assert "median uncovered 60.00" in proposal.reason

    def test_expired_schedule_no_longer_covers_future_need(self, db, book):
        from breadsched.gen.engine import estimates

        expired = ScheduledTransaction(
            name="Former bill",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 5), count=3),
            splits=[
                ScheduledSplit(book.rent, Money("100")),
                ScheduledSplit(book.checking, Money("-100")),
            ],
        )
        with db.transaction("Past bill") as txn:
            db.add_scheduled(expired, txn)
            for month in (1, 2, 3):
                actual = Transaction.simple(
                    date(2026, month, 5), "Rent", book.rent, book.checking, "100"
                )
                actual.scheduled_from = expired.handle
                db.add_transaction(actual, txn)
        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=3
            )
            if item.category == book.rent
        )
        assert proposal.amount == Money("100")

    def test_partial_year_replacement_creates_only_an_uncovered_bridge(self, db, book):
        from breadsched.gen.engine import estimates

        replacement = ScheduledTransaction(
            name="Known replacement",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 10, 5)),
            splits=[
                ScheduledSplit(book.rent, Money("100")),
                ScheduledSplit(book.checking, Money("-100")),
            ],
        )
        without_replacement = Scenario(
            name="Without replacement",
            schedule_overrides=[ScenarioSchedule.from_scheduled(replacement, enabled=False)],
        )
        historical_months = [
            *[date(2025, month, 5) for month in range(4, 13)],
            *[date(2026, month, 5) for month in range(1, 4)],
        ]
        with db.transaction("History and future replacement") as txn:
            db.add_scheduled(replacement, txn)
            db.add_scenario(without_replacement, txn)
            for when in historical_months:
                db.add_transaction(
                    Transaction.simple(when, "Rent", book.rent, book.checking, "100"),
                    txn,
                )

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=12
            )
            if item.category == book.rent
        )
        assert proposal.amount == Money("100")
        assert proposal.recurrence.start == date(2026, 4, 5)
        assert proposal.recurrence.end == date(2026, 9, 30)
        assert proposal.scheduled_amount == Money("600")

        alternate = next(
            item
            for item in estimates.propose_historical_estimates(
                db,
                as_of=date(2026, 4, 20),
                months=12,
                scenario_handle=without_replacement.handle,
            )
            if item.category == book.rent
        )
        assert alternate.amount == Money("100")
        assert alternate.recurrence.end is None
        assert alternate.scheduled_amount == Money("0")

    def test_isolated_future_event_does_not_end_a_monthly_estimate(self, db, book):
        from breadsched.gen.engine import estimates

        isolated = ScheduledTransaction(
            name="Known one-time cost",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 10, 5)),
            splits=[
                ScheduledSplit(book.rent, Money("100")),
                ScheduledSplit(book.checking, Money("-100")),
            ],
        )
        historical_months = [
            *[date(2025, month, 5) for month in range(4, 13)],
            *[date(2026, month, 5) for month in range(1, 4)],
        ]
        with db.transaction("History and isolated future cost") as txn:
            db.add_scheduled(isolated, txn)
            for when in historical_months:
                db.add_transaction(
                    Transaction.simple(when, "Rent", book.rent, book.checking, "100"),
                    txn,
                )

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=12
            )
            if item.category == book.rent
        )
        assert proposal.amount == Money("100")
        assert proposal.recurrence.end is None
        assert proposal.scheduled_amount == Money("100")

    def test_infers_multi_year_cadence_and_next_due_date(self, db, book):
        from breadsched.gen.engine import estimates

        with db.transaction("Biennial history") as txn:
            for when in (date(2022, 9, 10), date(2024, 9, 10)):
                db.add_transaction(
                    Transaction.simple(when, "Treatment", book.utilities, book.checking, "600"),
                    txn,
                )

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 1, 20), months=48, min_active_months=2
            )
            if item.category == book.utilities
        )
        assert proposal.amount == Money("600")
        assert proposal.recurrence.period is PeriodType.YEAR
        assert proposal.recurrence.interval == 2
        assert proposal.recurrence.start == date(2026, 9, 10)
        assert "every 2 years" in proposal.reason

    def test_infers_quarterly_cadence_despite_posting_day_drift(self, db, book):
        from breadsched.gen.engine import estimates

        with db.transaction("Quarterly history") as txn:
            for when in (
                date(2025, 1, 28),
                date(2025, 4, 30),
                date(2025, 7, 27),
                date(2025, 10, 31),
            ):
                db.add_transaction(
                    Transaction.simple(
                        when, "Quarterly service", book.utilities, book.checking, "300"
                    ),
                    txn,
                )

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 1, 20), months=12
            )
            if item.category == book.utilities
        )
        assert proposal.amount == Money("300")
        assert proposal.recurrence.period is PeriodType.MONTH
        assert proposal.recurrence.interval == 3
        assert proposal.recurrence.start == date(2026, 1, 31)
        assert "every 3 months" in proposal.reason

    def test_single_historical_event_is_only_proposed_once(self, db, book):
        from breadsched.gen.engine import estimates

        with db.transaction("One-time history") as txn:
            db.add_transaction(
                Transaction.simple(
                    date(2026, 3, 5), "One-time", book.utilities, book.checking, "600"
                ),
                txn,
            )

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=3, min_active_months=1
            )
            if item.category == book.utilities
        )
        assert proposal.recurrence.period is PeriodType.ONCE
        assert proposal.recurrence.start == date(2026, 4, 1)
        assert proposal.amount == Money("600")

    def test_partial_estimates_converge_in_selected_future_plan(self, db, book):
        from breadsched.gen.engine import estimates

        with db.transaction("History") as txn:
            for month in (1, 2, 3):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, month, 5), "Groceries", book.groceries, book.checking, "100"
                    ),
                    txn,
                )
        first = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=3
            )
            if item.category == book.groceries
        )
        estimates.accept_historical_estimate(db, replace(first, amount=Money("40")))
        second = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=3
            )
            if item.category == book.groceries
        )
        assert second.amount == Money("60")
        estimates.accept_historical_estimate(db, second)
        assert all(
            item.category != book.groceries
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=3
            )
        )

    def test_proposes_median_monthly_category_estimate(self, db, book):
        from breadsched.gen.engine import estimates
        from breadsched.gen.lib import Transaction

        with db.transaction("History") as txn:
            for month, amount in [(1, "100"), (2, "120"), (3, "500"), (4, "110")]:
                db.add_transaction(
                    Transaction.simple(
                        date(2026, month, 10),
                        "Groceries",
                        book.groceries,
                        book.checking,
                        amount,
                    ),
                    txn,
                )
        proposals = estimates.propose_historical_estimates(db, as_of=date(2026, 5, 15), months=4)
        groceries = next(item for item in proposals if item.category == book.groceries)
        assert groceries.amount == Money("115.00")
        assert groceries.funding == book.checking
        assert groceries.recurrence.start == date(2026, 5, 10)
        assert groceries.active_months == 4
        assert "category-specific day-10 anchor" in groceries.evidence.cadence.explanation

    def test_historical_estimate_uses_the_reporting_currency_fraction(self, db, book):
        from breadsched.gen.engine import estimates
        from breadsched.gen.lib import Transaction

        currency = db.get_commodity_by_mnemonic("USD")
        assert currency is not None
        currency.fraction = 1
        with db.transaction("Whole-unit estimate history") as txn:
            db.commit_commodity(currency, txn)
            for month, amount in ((1, "100.2"), (2, "100.8")):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, month, 10),
                        "Groceries",
                        book.groceries,
                        book.checking,
                        amount,
                    ),
                    txn,
                )

        proposals = estimates.propose_historical_estimates(
            db, as_of=date(2026, 3, 15), months=2, min_active_months=1
        )
        groceries = next(item for item in proposals if item.category == book.groceries)

        assert groceries.amount == Money("101")

    def test_sparse_weekly_dates_fall_back_with_visible_evidence(self, db, book):
        from breadsched.gen.engine import estimates

        with db.transaction("Sparse groceries") as txn:
            for day in (6, 13):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, 3, day),
                        "Groceries",
                        book.groceries,
                        book.checking,
                        "50",
                    ),
                    txn,
                )

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=1, min_active_months=1
            )
            if item.category == book.groceries
        )
        assert proposal.recurrence.period is PeriodType.MONTH
        assert proposal.evidence.cadence.label == "monthly"
        assert "too sparse" in proposal.evidence.cadence.explanation
        assert "at least 4" in proposal.evidence.cadence.explanation

    def test_excludes_and_explains_an_isolated_amount_outlier(self, db, book):
        from breadsched.gen.engine import estimates

        with db.transaction("Mostly stable history") as txn:
            for month, amount in enumerate(
                ("100", "102", "98", "1000", "101", "99", "100", "103"),
                start=1,
            ):
                db.add_transaction(
                    Transaction.simple(
                        date(2025, month, 10),
                        "Utilities",
                        book.utilities,
                        book.checking,
                        amount,
                    ),
                    txn,
                )

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2025, 9, 20), months=8
            )
            if item.category == book.utilities
        )
        assert proposal.amount == Money("100")
        assert proposal.outlier_months == 1
        assert proposal.variability == "stable"
        assert "excluded 1 isolated outlier month" in proposal.reason
        assert len(proposal.evidence.history) == 8
        assert len(proposal.evidence.selected_months) == 7
        assert [(item.month, item.exclusion) for item in proposal.evidence.exclusions] == [
            (date(2025, 4, 1), "isolated amount outlier")
        ]
        assert proposal.evidence.cadence.label == "monthly"
        assert len(proposal.evidence.cadence.observed_dates) == 8
        assert proposal.evidence.trend is None
        assert proposal.evidence.seasonality.detected is False
        assert proposal.evidence.funding.selected == book.checking
        assert proposal.evidence.funding.candidates[0].transaction_count == 8
        assert proposal.evidence.confidence.score == proposal.confidence
        assert proposal.evidence.confidence.retained_ratio == Decimal("0.875")

    def test_short_history_is_not_trimmed_as_an_outlier(self, db, book):
        from breadsched.gen.engine import estimates

        with db.transaction("Short variable history") as txn:
            for month, amount in enumerate(("100", "100", "100", "1000"), start=1):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, month, 10),
                        "Utilities",
                        book.utilities,
                        book.checking,
                        amount,
                    ),
                    txn,
                )

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 5, 20), months=4
            )
            if item.category == book.utilities
        )
        assert proposal.outlier_months == 0
        assert "excluded" not in proposal.reason

    def test_preserves_reverse_flow_direction(self, db, book):
        from breadsched.gen.engine import estimates
        from breadsched.gen.lib import Transaction

        with db.transaction("Reverse history") as txn:
            for month in (1, 2, 3):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, month, 5),
                        "Expense reversal",
                        book.card,
                        book.groceries,
                        "75",
                    ),
                    txn,
                )

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=3
            )
            if item.category == book.groceries
        )
        assert proposal.amount == Money("-75.00")
        assert proposal.display_amount == Money("75.00")
        assert proposal.source_name == "Expenses:Groceries"
        assert proposal.destination_name == "Liabilities:Credit Card"

        handle = estimates.accept_historical_estimate(db, proposal)
        saved = db.get_scheduled(handle)
        assert saved is not None
        values = {split.account: split.amount for split in saved.splits}
        assert values[book.groceries] == Money("-75.00")
        assert values[book.card] == Money("75.00")

    def test_subtracts_existing_scheduled_activity_for_selected_target(self, db, book):
        from breadsched.gen.engine import estimates
        from breadsched.gen.lib import Scenario, ScenarioSchedule, Transaction

        schedule = ScheduledTransaction(
            name="Known rent",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 10)),
            splits=[
                ScheduledSplit(book.rent, Money("1000.00")),
                ScheduledSplit(book.checking, Money("-1000.00")),
            ],
        )
        scenario = Scenario(
            name="Without known rent",
            schedule_overrides=[ScenarioSchedule.from_scheduled(schedule, enabled=False)],
        )
        with db.transaction("History and known schedule") as txn:
            db.add_scheduled(schedule, txn)
            db.add_scenario(scenario, txn)
            for month in (1, 2, 3):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, month, 10),
                        "Rent",
                        book.rent,
                        book.checking,
                        "1800",
                    ),
                    txn,
                )

        base = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=3
            )
            if item.category == book.rent
        )
        alternate = next(
            item
            for item in estimates.propose_historical_estimates(
                db,
                as_of=date(2026, 4, 20),
                months=3,
                scenario_handle=scenario.handle,
            )
            if item.category == book.rent
        )

        assert base.amount == Money("800.00")
        assert base.scheduled_amount == Money("3000.00")
        assert [item.gross for item in base.evidence.selected_months] == [Money("1800")] * 3
        assert [item.planned for item in base.evidence.selected_months] == [Money("1000")] * 3
        assert [item.residual for item in base.evidence.selected_months] == [Money("800")] * 3
        assert "3,000.00" in base.evidence.residual_explanation
        assert alternate.amount == Money("1800.00")
        assert alternate.scheduled_amount == Money("0.00")

    def test_scheduled_activity_does_not_create_reverse_residual(self, db, book):
        from breadsched.gen.engine import estimates

        schedule = ScheduledTransaction(
            name="Known rent",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 10)),
            splits=[
                ScheduledSplit(book.rent, Money("1000.00")),
                ScheduledSplit(book.checking, Money("-1000.00")),
            ],
        )
        with db.transaction("Known schedule") as txn:
            db.add_scheduled(schedule, txn)

        proposals = estimates.propose_historical_estimates(
            db, as_of=date(2026, 4, 20), months=3, min_active_months=1
        )
        assert all(item.category != book.rent for item in proposals)

    def test_detects_weekly_residual_cadence(self, db, book):
        from breadsched.gen.engine import estimates
        from breadsched.gen.lib import Transaction

        with db.transaction("Weekly groceries") as txn:
            for day in (2, 9, 16, 23, 30):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, 1, day),
                        "Groceries",
                        book.groceries,
                        book.checking,
                        "100",
                    ),
                    txn,
                )
            for day in (6, 13, 20, 27):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, 2, day),
                        "Groceries",
                        book.groceries,
                        book.checking,
                        "100",
                    ),
                    txn,
                )
            for day in (6, 13, 20, 27):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, 3, day),
                        "Groceries",
                        book.groceries,
                        book.checking,
                        "100",
                    ),
                    txn,
                )

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=3
            )
            if item.category == book.groceries
        )
        assert proposal.recurrence.period is PeriodType.WEEK
        assert proposal.recurrence.interval == 1
        assert proposal.recurrence.start == date(2026, 4, 3)
        assert Money("90") <= proposal.amount <= Money("110")
        assert "weekly" in proposal.reason
        assert "First proposed occurrence: 2026-04-03" in (proposal.evidence.cadence.explanation)

    def test_fortnightly_cadence_retains_the_observed_phase(self, db, book):
        from breadsched.gen.engine import estimates

        with db.transaction("Fortnightly groceries") as txn:
            for when in (
                date(2026, 1, 2),
                date(2026, 1, 16),
                date(2026, 1, 30),
                date(2026, 2, 13),
                date(2026, 2, 27),
                date(2026, 3, 13),
                date(2026, 3, 27),
            ):
                db.add_transaction(
                    Transaction.simple(when, "Groceries", book.groceries, book.checking, "100"),
                    txn,
                )

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=3
            )
            if item.category == book.groceries
        )
        assert proposal.recurrence.period is PeriodType.WEEK
        assert proposal.recurrence.interval == 2
        assert proposal.recurrence.start == date(2026, 4, 10)
        assert proposal.evidence.cadence.label == "fortnightly"

    def test_detects_upward_trend_and_uses_recent_residual(self, db, book):
        from breadsched.gen.engine import estimates
        from breadsched.gen.lib import Transaction

        amounts = ["100", "105", "110", "150", "155", "160"]
        with db.transaction("Trending groceries") as txn:
            for month, amount in enumerate(amounts, start=1):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, month, 10),
                        "Groceries",
                        book.groceries,
                        book.checking,
                        amount,
                    ),
                    txn,
                )

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 7, 20), months=6
            )
            if item.category == book.groceries
        )
        assert proposal.trend is not None
        assert "upward trend" in proposal.trend
        assert proposal.amount == Money("155.00")
        assert "using recent median" in proposal.reason

    def test_detects_repeated_month_of_year_seasonality(self, db, book):
        from breadsched.gen.engine import estimates
        from breadsched.gen.lib import Transaction

        with db.transaction("Seasonal utilities") as txn:
            for year in (2024, 2025):
                for month in range(1, 13):
                    amount = "240" if month in (1, 2, 7, 8) else "100"
                    db.add_transaction(
                        Transaction.simple(
                            date(year, month, 10),
                            "Utilities",
                            book.utilities,
                            book.checking,
                            amount,
                        ),
                        txn,
                    )

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 1, 20), months=24
            )
            if item.category == book.utilities
        )
        assert proposal.seasonal is True
        assert proposal.outlier_months == 0
        assert proposal.variability == "seasonal by calendar month"
        assert "seasonal variation detected" in proposal.reason
        assert len(proposal.seasonal_amounts) == 12
        assert proposal.evidence.seasonality.detected is True
        assert proposal.evidence.seasonality.monthly_amounts == proposal.seasonal_amounts
        assert not proposal.evidence.exclusions

        handle = estimates.accept_historical_estimate(db, proposal)
        saved = db.get_scheduled(handle)
        assert saved is not None
        assert saved.amount(when=date(2026, 1, 10)) == Money("240.00")
        assert saved.amount(when=date(2026, 4, 10)) == Money("100.00")
        assert saved.amount(when=date(2026, 7, 10)) == Money("240.00")
        assert all(
            item.category != book.utilities
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 1, 20), months=24
            )
        )

    def test_sparse_repeated_months_do_not_claim_seasonality(self, db, book):
        from breadsched.gen.engine import estimates

        with db.transaction("Sparse apparent seasonality") as txn:
            for year in (2024, 2025):
                for month in range(1, 5):
                    amount = "300" if month == 1 else "100"
                    db.add_transaction(
                        Transaction.simple(
                            date(year, month, 10),
                            "Utilities",
                            book.utilities,
                            book.checking,
                            amount,
                        ),
                        txn,
                    )

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 1, 20), months=24
            )
            if item.category == book.utilities
        )
        assert proposal.seasonal is False
        assert "Only 4 calendar month(s) repeat" in (proposal.evidence.seasonality.explanation)

    def test_unstable_year_over_year_pattern_is_explained_as_noise(self, db, book):
        from breadsched.gen.engine import estimates

        with db.transaction("Noisy apparent seasonality") as txn:
            for year, amount in ((2024, "100"), (2025, "300")):
                for month in range(1, 13):
                    db.add_transaction(
                        Transaction.simple(
                            date(year, month, 10),
                            "Utilities",
                            book.utilities,
                            book.checking,
                            amount,
                        ),
                        txn,
                    )

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 1, 20), months=24
            )
            if item.category == book.utilities
        )
        assert proposal.seasonal is False
        assert "treated as noise" in proposal.evidence.seasonality.explanation

    def test_review_drafts_preserve_the_proposal_without_writing(self, db, book):
        from breadsched.gen.engine import estimates
        from breadsched.gen.lib import Transaction

        with db.transaction("Seasonal history") as txn:
            for year in (2024, 2025):
                for month in range(1, 13):
                    db.add_transaction(
                        Transaction.simple(
                            date(year, month, 10),
                            "Utilities",
                            book.utilities,
                            book.checking,
                            "240" if month in (1, 2, 7, 8) else "100",
                        ),
                        txn,
                    )

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 1, 20), months=24
            )
            if item.category == book.utilities
        )
        schedules_before = list(db.iter_scheduled())

        base = estimates.draft_historical_estimate(db, proposal)
        alternate = estimates.draft_scenario_estimate(db, proposal)

        assert list(db.iter_scheduled()) == schedules_before
        assert base.placeholder is True
        assert base.auto_create is False
        assert base.recurrence.serialize() == proposal.recurrence.serialize()
        assert alternate.recurrence.serialize() == proposal.recurrence.serialize()
        assert [item.serialize() for item in base.seasonal_amounts] == [
            item.serialize() for item in proposal.seasonal_amounts
        ]
        assert [item.serialize() for item in alternate.seasonal_amounts] == [
            item.serialize() for item in proposal.seasonal_amounts
        ]
        assert base.seasonal_amounts[0] is not proposal.seasonal_amounts[0]
        assert alternate.seasonal_amounts[0] is not proposal.seasonal_amounts[0]

    def test_accepted_base_estimate_is_subtracted_on_reanalysis(self, db, book):
        from breadsched.gen.engine import estimates
        from breadsched.gen.lib import Transaction

        with db.transaction("History") as txn:
            for month in (1, 2, 3):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, month, 5),
                        "Groceries",
                        book.groceries,
                        book.checking,
                        "25",
                    ),
                    txn,
                )

        first = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=3
            )
            if item.category == book.groceries
        )
        assert first.amount == Money("25.00")
        estimates.accept_historical_estimate(db, first)

        second = estimates.propose_historical_estimates(db, as_of=date(2026, 4, 20), months=3)
        assert all(item.category != book.groceries for item in second)

    def test_accepted_scenario_estimate_is_subtracted_only_in_that_scenario(self, db, book):
        from breadsched.gen.engine import estimates
        from breadsched.gen.lib import Scenario, Transaction

        with db.transaction("History and scenario") as txn:
            for month in (1, 2, 3):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, month, 5),
                        "Groceries",
                        book.groceries,
                        book.checking,
                        "25",
                    ),
                    txn,
                )
            scenario = Scenario(name="Alternative")
            db.add_scenario(scenario, txn)

        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=3
            )
            if item.category == book.groceries
        )
        estimates.accept_historical_estimate(db, proposal, scenario_handle=scenario.handle)

        base = estimates.propose_historical_estimates(db, as_of=date(2026, 4, 20), months=3)
        alternate = estimates.propose_historical_estimates(
            db, as_of=date(2026, 4, 20), months=3, scenario_handle=scenario.handle
        )
        assert next(item for item in base if item.category == book.groceries).amount == Money(
            "25.00"
        )
        assert all(item.category != book.groceries for item in alternate)

    def test_accepts_proposal_into_base_as_estimate(self, db, book):
        from breadsched.gen.engine import estimates
        from breadsched.gen.lib import Transaction

        with db.transaction("History") as txn:
            for month in (1, 2, 3):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, month, 5),
                        "Rent",
                        book.rent,
                        book.checking,
                        "1800",
                    ),
                    txn,
                )
        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=3
            )
            if item.category == book.rent
        )
        handle = estimates.accept_historical_estimate(db, proposal)
        saved = db.get_scheduled(handle)
        assert saved is not None
        assert saved.placeholder is True
        assert saved.auto_create is False
        assert saved.amount() == Money("1800.00")

    def test_accepts_proposal_into_saved_scenario_only(self, db, book):
        from breadsched.gen.engine import estimates
        from breadsched.gen.lib import Scenario, Transaction

        with db.transaction("History and scenario") as txn:
            for month in (1, 2, 3):
                db.add_transaction(
                    Transaction.simple(
                        date(2026, month, 5),
                        "Rent",
                        book.rent,
                        book.checking,
                        "1800",
                    ),
                    txn,
                )
            scenario = Scenario(name="Alternative")
            db.add_scenario(scenario, txn)
        proposal = next(
            item
            for item in estimates.propose_historical_estimates(
                db, as_of=date(2026, 4, 20), months=3
            )
            if item.category == book.rent
        )
        estimates.accept_historical_estimate(db, proposal, scenario_handle=scenario.handle)
        assert list(db.iter_scheduled()) == []
        saved = db.get_scenario(scenario.handle)
        assert saved is not None
        assert len(saved.schedule_overrides) == 1
        assert saved.schedule_overrides[0].placeholder is True
