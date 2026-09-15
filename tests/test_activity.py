"""Period reports are derived from exact-dated plan events and ledger actuals."""

from datetime import date

from breadsched.gen.engine import activity, planning
from breadsched.gen.lib import (
    Account,
    AccountType,
    Money,
    PeriodType,
    PlanningFlowKind,
    PlanningResolution,
    Recurrence,
    Scenario,
    ScenarioSchedule,
    ScheduledSplit,
    ScheduledTransaction,
    Split,
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
    def test_months_group_expected_and_unexpected_actuals_without_driving_dates(self, db, book):
        bill = _monthly_bill(book, start=date(2026, 1, 7))
        with db.transaction("plan") as txn:
            db.add_scheduled(bill, txn)
            actual = Transaction.simple(
                date(2026, 1, 20),
                "Unplanned groceries",
                book.groceries,
                book.checking,
                "35.00",
            )
            planning.mark_unexpected(actual)
            db.add_transaction(actual, txn)

        report = activity.build_activity_report(db, date(2026, 1, 1), date(2026, 2, 28))

        january, february = report.periods
        assert [event.planned_date for event in january.planned_events] == [date(2026, 1, 7)]
        assert [event.planned_date for event in february.planned_events] == [date(2026, 2, 7)]
        assert january.planned_amount == Money("100.00")
        assert january.actual_amount == Money("35.00")
        assert january.planned_cash_change == Money("-100.00")
        assert january.actual_cash_change == Money("-35.00")
        assert len(january.unresolved) == 1
        assert len(january.unexpected) == 1
        assert report.unresolved_count == 2
        assert report.unresolved_actual_count == 0
        assert report.unexpected_count == 1

    def test_matched_actual_keeps_expected_and_actual_in_their_own_date_periods(self, db, book):
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
        expected = planning.scheduled_events(db, date(2026, 1, 1), date(2026, 2, 7))[0]
        planning.actualize_transaction(actual, expected)
        with db.transaction("actual") as txn:
            db.add_transaction(actual, txn)

        report = activity.build_activity_report(db, date(2026, 1, 1), date(2026, 2, 28))
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

    def test_unclassified_actual_is_distinct_from_explicitly_unexpected(self, db, book):
        actual = Transaction.simple(
            date(2026, 4, 3),
            "Needs review",
            book.groceries,
            book.checking,
            "42.00",
        )
        with db.transaction("unresolved actual") as txn:
            db.add_transaction(actual, txn)

        report = activity.build_activity_report(db, date(2026, 4, 1), date(2026, 4, 30))

        assert report.unresolved_actual_count == 1
        assert report.unexpected_count == 0
        posted = report.periods[0].actual_transactions[0]
        assert posted.unresolved is True
        assert posted.unexpected is False

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

        report = activity.build_activity_report(db, date(2026, 4, 1), date(2026, 4, 30))

        assert report.unexpected_count == 0
        assert report.periods[0].actual_transactions[0].unexpected is False

    def test_json_shape_contains_drill_down_provenance(self, db, book):
        bill = _monthly_bill(book, start=date(2026, 3, 8))
        with db.transaction("plan") as txn:
            db.add_scheduled(bill, txn)

        report = activity.build_activity_report(db, date(2026, 3, 1), date(2026, 3, 31))
        payload = report.as_dict()

        assert payload["period"] == "month"
        assert payload["unresolved_count"] == 1
        period = payload["periods"][0]
        assert period["label"] == "Mar 2026"
        assert period["planned_events"][0]["planned_date"] == date(2026, 3, 8)
        assert period["planned_events"][0]["source"] == "scheduled"


class TestCategoryPlanning:
    def test_recurring_estimate_builds_category_period_values(self, db, book):
        groceries = ScheduledTransaction(
            name="Weekly groceries estimate",
            recurrence=Recurrence(PeriodType.WEEK, start=date(2026, 1, 2)),
            splits=[
                ScheduledSplit(book.groceries, Money("300.00")),
                ScheduledSplit(book.card, Money("-300.00")),
            ],
        )
        with db.transaction("weekly estimate") as txn:
            db.add_scheduled(groceries, txn)

        report = activity.build_category_report(db, date(2026, 1, 1), date(2026, 1, 31))
        row = next(item for item in report.expenses if item.account == book.groceries)
        assert row.planned == [Money("1500.00")]
        assert row.actual == [Money(0)]

    def test_scenario_recurring_estimate_feeds_category_plan(self, db, book):
        groceries = ScenarioSchedule(
            name="Weekly groceries estimate",
            recurrence=Recurrence(PeriodType.WEEK, start=date(2026, 1, 2)),
            splits=[
                ScheduledSplit(book.groceries, Money("300.00")),
                ScheduledSplit(book.card, Money("-300.00")),
            ],
        )
        scenario = Scenario(name="Higher grocery plan", schedule_overrides=[groceries])

        report = activity.build_category_report(
            db,
            date(2026, 1, 1),
            date(2026, 1, 31),
            scenario=scenario,
        )
        row = next(item for item in report.expenses if item.account == book.groceries)
        assert row.planned == [Money("1500.00")]

    def test_asset_transfer_does_not_become_income_or_expense(self, db, book):
        transfer = ScheduledTransaction(
            name="Move money to savings",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 2, 5)),
            splits=[
                ScheduledSplit(book.savings, Money("500.00")),
                ScheduledSplit(book.checking, Money("-500.00")),
            ],
        )
        with db.transaction("transfer") as txn:
            db.add_scheduled(transfer, txn)

        report = activity.build_category_report(db, date(2026, 2, 1), date(2026, 2, 28))
        assert report.income == ()
        assert report.expenses == ()

    def test_parent_category_rolls_up_descendant_activity(self, db, book):
        bill = _monthly_bill(book, start=date(2026, 3, 7), amount="125.00")
        with db.transaction("utility estimate") as txn:
            db.add_scheduled(bill, txn)

        report = activity.build_category_report(db, date(2026, 3, 1), date(2026, 3, 31))
        parent = next(item for item in report.expenses if item.account == book.expenses)
        utility = next(item for item in report.expenses if item.account == book.utilities)
        assert parent.planned == [Money("125.00")]
        assert utility.planned == [Money("125.00")]

    def test_totals_do_not_add_parent_and_child_rollups_twice(self, db, book):
        bill = _monthly_bill(book, start=date(2026, 3, 7), amount="125.00")
        with db.transaction("utility estimate") as txn:
            db.add_scheduled(bill, txn)

        report = activity.build_category_report(
            db,
            date(2026, 3, 1),
            date(2026, 3, 31),
            as_of=date(2026, 3, 15),
        )

        assert report.category_totals(
            AccountType.EXPENSE.account_class, activity.PlanMeasure.PLANNED
        ) == [Money("125.00")]
        assert report.category_grand_total(
            AccountType.EXPENSE.account_class, activity.PlanMeasure.PLANNED
        ) == Money("125.00")
        assert report.grand_total(activity.PlanMeasure.PLANNED) == Money("-125.00")

    def test_row_and_variance_totals_follow_the_selected_horizon(self, db, book):
        bill = _monthly_bill(book, start=date(2026, 1, 7), amount="100.00")
        with db.transaction("three month estimate") as txn:
            db.add_scheduled(bill, txn)

        report = activity.build_category_report(
            db,
            date(2026, 1, 1),
            date(2026, 3, 31),
            as_of=date(2026, 2, 15),
        )
        utility = next(item for item in report.expenses if item.account == book.utilities)

        assert utility.total(activity.PlanMeasure.PLANNED) == Money("300.00")
        assert utility.variance == [Money("-100.00"), Money("-100.00"), None]
        assert utility.total(activity.PlanMeasure.VARIANCE) == Money("-200.00")
        assert report.cash_totals(activity.PlanMeasure.VARIANCE) == [
            Money("100.00"),
            Money("100.00"),
            None,
        ]

    def test_plan_settings_round_trip_through_book_metadata(self, db):
        settings = activity.PlanSettings(
            start=date(2027, 4, 1),
            end=date(2028, 6, 30),
            period=activity.ReportingPeriod.QUARTER,
            measure=activity.PlanMeasure.VARIANCE,
            scenario="scenario-id",
            compare="__base__",
        )

        settings.save(db)

        assert activity.PlanSettings.load(db, date(2026, 1, 1), date(2026, 12, 31)) == settings

    def test_future_period_variance_is_not_applicable_but_actual_is_retained(self, db, book):
        bill = _monthly_bill(book, start=date(2026, 1, 7), amount="100.00")
        future_actual = Transaction.simple(
            date(2026, 3, 7),
            "Future-dated electric",
            book.utilities,
            book.checking,
            "90.00",
        )
        with db.transaction("future plan and actual") as txn:
            db.add_scheduled(bill, txn)
            db.add_transaction(future_actual, txn)

        report = activity.build_category_report(
            db,
            date(2026, 1, 1),
            date(2026, 3, 31),
            as_of=date(2026, 2, 15),
        )
        row = next(item for item in report.expenses if item.account == book.utilities)

        assert row.planned == [Money("100.00"), Money("100.00"), Money("100.00")]
        assert row.actual == [Money(0), Money(0), Money("90.00")]
        assert row.variance == [Money("-100.00"), Money("-100.00"), None]
        assert report.cash_variance == Money("200.00")

    def test_future_period_detail_variance_is_not_applicable(self, db, book):
        bill = _monthly_bill(book, start=date(2026, 3, 7), amount="100.00")
        with db.transaction("future plan") as txn:
            db.add_scheduled(bill, txn)

        detail = activity.explain_category_period(
            db,
            book.utilities,
            date(2026, 3, 1),
            date(2026, 3, 31),
            as_of=date(2026, 2, 15),
        )

        assert detail.planned == Money("100.00")
        assert detail.actual == Money(0)
        assert detail.variance is None

    def test_category_detail_explains_account_type_and_pending_resolution(self, db, book):
        bill = _monthly_bill(book, start=date(2026, 3, 7), amount="100.00")
        with db.transaction("explain future plan") as txn:
            db.add_scheduled(bill, txn)

        detail = activity.explain_category_period(
            db,
            book.utilities,
            date(2026, 3, 1),
            date(2026, 3, 31),
            as_of=date(2026, 3, 1),
        )

        explanation = " ".join(detail.planned_events[0].explanation)
        assert "type EXPENSE" in explanation
        assert "this split contributes 100.00" in explanation
        assert "remains pending" in explanation

    def test_actual_detail_explains_unresolved_and_unexpected_decisions(self, db, book):
        unresolved = Transaction.simple(
            date(2026, 3, 8),
            "Unresolved utility",
            book.utilities,
            book.checking,
            "40.00",
        )
        unexpected = Transaction.simple(
            date(2026, 3, 9),
            "Unexpected utility",
            book.utilities,
            book.checking,
            "50.00",
        )
        planning.mark_unexpected(unexpected)
        with db.transaction("explain actual decisions") as txn:
            db.add_transaction(unresolved, txn)
            db.add_transaction(unexpected, txn)

        detail = activity.explain_category_period(
            db,
            book.utilities,
            date(2026, 3, 1),
            date(2026, 3, 31),
            as_of=date(2026, 3, 31),
        )

        explanations = {
            item.resolution: " ".join(item.explanation) for item in detail.actual_transactions
        }
        assert "Use Resolve actuals" in explanations[PlanningResolution.UNRESOLVED]
        assert "explicitly marked unexpected" in explanations[PlanningResolution.UNEXPECTED]


class TestPlanningFlowClassification:
    def test_planning_purpose_maps_positive_economic_amounts_to_ledger_signs(self):
        amount = Money("250.00")
        assert PlanningFlowKind.RETIREMENT_SAVING.ledger_amount(amount) == amount
        assert PlanningFlowKind.BENEFIT_FUNDING.ledger_amount(amount) == amount
        assert PlanningFlowKind.DEBT_PRINCIPAL.ledger_amount(amount) == amount
        assert PlanningFlowKind.RETIREMENT_INCOME.ledger_amount(amount) == -amount

    def test_account_kinds_infer_common_balance_sheet_flows(self, db, book):
        brokerage = db.get_account(book.brokerage)
        assert brokerage is not None
        brokerage.atype = AccountType.RETIREMENT
        with db.transaction("mark retirement") as txn:
            db.commit_account(brokerage, txn)
            db.add_transaction(
                Transaction.simple(
                    date(2026, 1, 15), "401k", book.brokerage, book.checking, "500.00"
                ),
                txn,
            )
            db.add_transaction(
                Transaction.simple(
                    date(2026, 1, 20), "distribution", book.checking, book.brokerage, "200.00"
                ),
                txn,
            )

        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2026, 1, 31)
        )
        flows = {(row.kind, row.account): row.actual[0] for row in report.planning_flows}
        assert flows[(PlanningFlowKind.RETIREMENT_SAVING, book.brokerage)] == Money("500")
        assert flows[(PlanningFlowKind.RETIREMENT_INCOME, book.brokerage)] == Money("200")

    def test_fsa_and_debt_kinds_infer_funding_and_principal(self, db, book):
        brokerage = db.get_account(book.brokerage)
        assert brokerage is not None
        brokerage.atype = AccountType.FSA
        debt = Account(name="Loan", atype=AccountType.LIABILITY, parent=book.root)
        debt.atype = AccountType.LOAN
        with db.transaction("mark benefit and debt") as txn:
            db.commit_account(brokerage, txn)
            db.add_account(debt, txn)
            db.add_transaction(
                Transaction.simple(
                    date(2026, 1, 10), "FSA funding", book.brokerage, book.checking, "125"
                ),
                txn,
            )
            db.add_transaction(
                Transaction.simple(
                    date(2026, 1, 20), "Loan principal", debt.handle, book.checking, "300"
                ),
                txn,
            )

        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2026, 1, 31)
        )
        flows = {(row.kind, row.account): row.actual[0] for row in report.planning_flows}
        assert flows[(PlanningFlowKind.BENEFIT_FUNDING, book.brokerage)] == Money("125")
        assert flows[(PlanningFlowKind.DEBT_PRINCIPAL, debt.handle)] == Money("300")

    def test_kind_inference_keeps_retirement_transfers_neutral(self, db, book):
        brokerage = db.get_account(book.brokerage)
        savings = db.get_account(book.savings)
        assert brokerage is not None and savings is not None
        brokerage.atype = AccountType.RETIREMENT
        savings.atype = AccountType.RETIREMENT
        with db.transaction("mark retirement accounts") as txn:
            db.commit_account(brokerage, txn)
            db.commit_account(savings, txn)
            db.add_transaction(
                Transaction.simple(
                    date(2026, 1, 15), "rollover", book.savings, book.brokerage, "500.00"
                ),
                txn,
            )

        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2026, 1, 31)
        )
        assert report.planning_flows == []

    def test_escrow_funding_is_expense_and_payout_is_not_counted_twice(self, db, book):
        escrow = Account(
            name="Escrow",
            atype=AccountType.BANK,
            parent=book.assets,
        )
        escrow.atype = AccountType.ESCROW
        with db.transaction("Escrow cycle") as txn:
            db.add_account(escrow, txn)
            db.add_transaction(
                Transaction.simple(
                    date(2026, 1, 5), "Fund escrow", escrow.handle, book.checking, "100"
                ),
                txn,
            )
            payout = Transaction(post_date=date(2026, 1, 20), description="Escrow payout")
            payout.add_split(Split(book.utilities, Money("80")))
            payout.add_split(Split(escrow.handle, Money("-80")))
            db.add_transaction(payout, txn)

        activity_report = activity.build_activity_report(db, date(2026, 1, 1), date(2026, 1, 31))
        assert activity_report.periods[0].actual_expense == Money("100")
        assert activity_report.actual_cash_change == Money("-100")

        category_report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2026, 1, 31)
        )
        utilities = next(row for row in category_report.categories if row.account == book.utilities)
        assert utilities.actual == [Money(0)]
        flows = {(row.kind, row.account): row.actual[0] for row in category_report.planning_flows}
        assert flows[(PlanningFlowKind.ESCROW_FUNDING, escrow.handle)] == Money("100")

    def test_partial_escrow_payout_only_suppresses_the_covered_expense(self, db, book):
        escrow = Account(name="Escrow", atype=AccountType.ASSET, parent=book.assets)
        escrow.atype = AccountType.ESCROW
        with db.transaction("Partial payout") as txn:
            db.add_account(escrow, txn)
            payout = Transaction(post_date=date(2026, 1, 20), description="Partial escrow payout")
            payout.add_split(Split(book.utilities, Money("100")))
            payout.add_split(Split(escrow.handle, Money("-60")))
            payout.add_split(Split(book.checking, Money("-40")))
            db.add_transaction(payout, txn)

        report = activity.build_activity_report(db, date(2026, 1, 1), date(2026, 1, 31))
        assert report.periods[0].actual_expense == Money("40")

    def test_escrow_refunds_and_restorations_do_not_create_phantom_funding(self, db, book):
        escrow = Account(name="Escrow", atype=AccountType.ESCROW, parent=book.assets)
        with db.transaction("Escrow corrections") as txn:
            db.add_account(escrow, txn)
            restored = Transaction(post_date=date(2026, 1, 10), description="Vendor credit")
            restored.add_split(Split(book.utilities, Money("-25")))
            restored.add_split(Split(escrow.handle, Money("25")))
            db.add_transaction(restored, txn)
            returned = Transaction(post_date=date(2026, 1, 20), description="Cash refund")
            returned.add_split(Split(escrow.handle, Money("-30")))
            returned.add_split(Split(book.checking, Money("30")))
            db.add_transaction(returned, txn)

        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2026, 1, 31)
        )
        utilities = next(row for row in report.categories if row.account == book.utilities)
        escrow_flow = next(
            row for row in report.planning_flows if row.kind is PlanningFlowKind.ESCROW_FUNDING
        )

        assert utilities.actual == [Money(0)]
        assert escrow_flow.actual == [Money("-30")]
        assert report.activity.periods[0].actual_expense == Money("-30")

    def test_combined_mortgage_payment_counts_interest_and_escrow_but_not_principal(self, db, book):
        escrow = Account(name="Escrow", atype=AccountType.ESCROW, parent=book.assets)
        loan = Account(name="Mortgage", atype=AccountType.LOAN, parent=book.liabilities)
        payment = ScheduledTransaction(
            name="Mortgage payment",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 15)),
            splits=[
                ScheduledSplit(loan.handle, Money("800")),
                ScheduledSplit(book.utilities, Money("1150")),
                ScheduledSplit(escrow.handle, Money("450")),
                ScheduledSplit(book.checking, Money("-2400")),
            ],
        )
        with db.transaction("Mortgage with escrow") as txn:
            db.add_account(escrow, txn)
            db.add_account(loan, txn)
            db.add_scheduled(payment, txn)

        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2026, 1, 31)
        )
        escrow_flow = next(
            row for row in report.planning_flows if row.kind is PlanningFlowKind.ESCROW_FUNDING
        )
        principal_flow = next(
            row for row in report.planning_flows if row.kind is PlanningFlowKind.DEBT_PRINCIPAL
        )

        mortgage = report.mortgage_payments[0]
        assert mortgage.account == loan.handle
        assert mortgage.planned == [Money("2400")]
        assert mortgage.total(activity.PlanMeasure.PLANNED) == Money("2400")
        assert report.mortgage_payment_totals(activity.PlanMeasure.PLANNED) == [Money("2400")]
        assert report.activity.periods[0].planned_cash_change == Money("-2400")
        assert report.activity.periods[0].planned_expense == Money("1600")
        assert escrow_flow.planned == [Money("450")]
        assert principal_flow.planned == [Money("800")]
        detail = activity.explain_planning_flow_period(
            db,
            PlanningFlowKind.ESCROW_FUNDING,
            escrow.handle,
            date(2026, 1, 1),
            date(2026, 1, 31),
        )
        assert "principal only reduces the liability" in " ".join(
            detail.planned_events[0].explanation
        )
        payment_detail = activity.explain_mortgage_payment_period(
            db,
            loan.handle,
            date(2026, 1, 1),
            date(2026, 1, 31),
            as_of=date(2026, 1, 31),
        )
        assert payment_detail.planned == Money("2400")
        explanation = " ".join(payment_detail.planned_events[0].explanation)
        assert "Debt principal $800.00" in explanation
        assert "ordinary expense $1,150.00" in explanation
        assert "Escrow funding $450.00" in explanation
        assert "not added to it again" in explanation

    def test_one_actual_resolves_the_whole_payment_when_components_change(self, db, book):
        escrow = Account(name="Escrow", atype=AccountType.ESCROW, parent=book.assets)
        loan = Account(name="Mortgage", atype=AccountType.LOAN, parent=book.liabilities)
        payment = ScheduledTransaction(
            name="Mortgage payment",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 15)),
            splits=[
                ScheduledSplit(loan.handle, Money("800")),
                ScheduledSplit(book.utilities, Money("1150")),
                ScheduledSplit(escrow.handle, Money("450")),
                ScheduledSplit(book.checking, Money("-2400")),
            ],
        )
        with db.transaction("Mortgage definition") as txn:
            db.add_account(escrow, txn)
            db.add_account(loan, txn)
            db.add_scheduled(payment, txn)
        expected = planning.scheduled_events(db, date(2026, 1, 1), date(2026, 1, 31))[0]
        posted = Transaction(
            post_date=date(2026, 1, 16),
            description="Mortgage actual",
            splits=[
                Split(loan.handle, Money("825")),
                Split(book.utilities, Money("1125")),
                Split(escrow.handle, Money("450")),
                Split(book.checking, Money("-2400")),
            ],
        )
        planning.actualize_transaction(posted, expected)
        with db.transaction("Post mortgage") as txn:
            db.add_transaction(posted, txn)

        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2026, 1, 31)
        )
        mortgage = report.mortgage_payments[0]
        assert mortgage.planned == [Money("2400")]
        assert mortgage.actual == [Money("2400")]
        assert mortgage.variance == [Money(0)]
        detail = activity.explain_mortgage_payment_period(
            db,
            loan.handle,
            date(2026, 1, 1),
            date(2026, 1, 31),
            as_of=date(2026, 1, 31),
        )
        assert len(detail.planned_events) == 1
        assert detail.planned_events[0].actual == Money("2400")
        assert len(detail.actual_transactions) == 1
        assert detail.actual_transactions[0].resolution is PlanningResolution.MATCHED
        assert detail.actual_transactions[0].expected == Money("2400")

    def test_extra_principal_and_fee_are_one_cash_requirement(self, db, book):
        loan = Account(name="Mortgage", atype=AccountType.LOAN, parent=book.liabilities)
        payment = ScheduledTransaction(
            name="Extra mortgage payment",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 15)),
            splits=[
                ScheduledSplit(loan.handle, Money("1000")),
                ScheduledSplit(book.utilities, Money("50")),
                ScheduledSplit(book.checking, Money("-1050")),
            ],
        )
        with db.transaction("Extra principal and fee") as txn:
            db.add_account(loan, txn)
            db.add_scheduled(payment, txn)

        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2026, 1, 31)
        )
        assert report.mortgage_payments[0].planned == [Money("1050")]
        principal = next(
            row for row in report.planning_flows if row.kind is PlanningFlowKind.DEBT_PRINCIPAL
        )
        assert principal.planned == [Money("1000")]
        utilities = next(row for row in report.categories if row.account == book.utilities)
        assert utilities.planned == [Money("50")]

    def test_mortgage_origination_is_not_a_payment(self, db, book):
        house = Account(name="House", atype=AccountType.ASSET, parent=book.assets)
        loan = Account(name="Mortgage", atype=AccountType.LOAN, parent=book.liabilities)
        purchase = Transaction(
            post_date=date(2026, 1, 15),
            description="Buy house",
            splits=[
                Split(house.handle, Money("400000")),
                Split(book.checking, Money("-80000")),
                Split(loan.handle, Money("-320000")),
            ],
        )
        purchase.planning_resolution = PlanningResolution.HISTORICAL
        with db.transaction("Mortgage origination") as txn:
            db.add_account(house, txn)
            db.add_account(loan, txn)
            db.add_transaction(purchase, txn)

        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2026, 1, 31)
        )
        assert report.mortgage_payments == []
        assert report.activity.periods[0].actual_cash_change == Money("-80000")
        assert report.activity.periods[0].actual_expense == Money(0)

    def test_mortgage_special_transactions_remain_distinct(self, db, book):
        house = Account(name="House", atype=AccountType.ASSET, parent=book.assets)
        old_loan = Account(name="Old mortgage", atype=AccountType.LOAN, parent=book.liabilities)
        new_loan = Account(name="New mortgage", atype=AccountType.LOAN, parent=book.liabilities)
        escrow = Account(name="Escrow", atype=AccountType.ESCROW, parent=book.assets)
        events = [
            ScheduledTransaction(
                name="Refinance with cash closing costs",
                recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 5)),
                splits=[
                    ScheduledSplit(old_loan.handle, Money("320000")),
                    ScheduledSplit(new_loan.handle, Money("-324000")),
                    ScheduledSplit(book.utilities, Money("5000")),
                    ScheduledSplit(book.checking, Money("-1000")),
                ],
            ),
            ScheduledTransaction(
                name="Sell house and repay mortgage",
                recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 10)),
                splits=[
                    ScheduledSplit(book.checking, Money("100000")),
                    ScheduledSplit(old_loan.handle, Money("300000")),
                    ScheduledSplit(house.handle, Money("-400000")),
                ],
            ),
            ScheduledTransaction(
                name="Escrow refund",
                recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 12)),
                splits=[
                    ScheduledSplit(book.checking, Money("200")),
                    ScheduledSplit(escrow.handle, Money("-200")),
                ],
            ),
            ScheduledTransaction(
                name="Mortgage payment with escrow shortage",
                recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 15)),
                splits=[
                    ScheduledSplit(old_loan.handle, Money("800")),
                    ScheduledSplit(book.utilities, Money("1150")),
                    ScheduledSplit(escrow.handle, Money("550")),
                    ScheduledSplit(book.checking, Money("-2500")),
                ],
            ),
        ]
        with db.transaction("Mortgage special transactions") as txn:
            for account in (house, old_loan, new_loan, escrow):
                db.add_account(account, txn)
            for event in events:
                db.add_scheduled(event, txn)

        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2026, 1, 31)
        )

        assert len(report.mortgage_payments) == 1
        assert report.mortgage_payments[0].account == old_loan.handle
        assert report.mortgage_payments[0].planned == [Money("2500")]
        detail = activity.explain_mortgage_payment_period(
            db,
            old_loan.handle,
            date(2026, 1, 1),
            date(2026, 1, 31),
            as_of=date(2026, 1, 31),
        )
        assert len(detail.planned_events) == 1
        explanation = " ".join(detail.planned_events[0].explanation)
        assert "Escrow funding $550.00" in explanation

    def test_classified_balance_sheet_splits_appear_in_plan(self, db, book):
        contribution = ScheduledTransaction(
            name="401k contribution",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 15)),
            splits=[
                ScheduledSplit(book.salary, Money("-1000.00")),
                ScheduledSplit(
                    book.brokerage,
                    Money("1000.00"),
                    planning_flow=PlanningFlowKind.RETIREMENT_SAVING,
                ),
            ],
        )
        with db.transaction("plan retirement contribution") as txn:
            db.add_scheduled(contribution, txn)

        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2026, 1, 31)
        )

        assert len(report.planning_flows) == 1
        flow = report.planning_flows[0]
        assert flow.kind is PlanningFlowKind.RETIREMENT_SAVING
        assert flow.planned == [Money("1000.00")]
        assert flow.actual == [Money(0)]
        assert flow.variance == [Money("-1000.00")]

    def test_planning_flow_period_detail_reconciles_planned_occurrence(self, db, book):
        contribution = ScheduledTransaction(
            name="401k contribution",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 15)),
            splits=[
                ScheduledSplit(book.salary, Money("-500.00")),
                ScheduledSplit(
                    book.brokerage,
                    Money("500.00"),
                    planning_flow=PlanningFlowKind.RETIREMENT_SAVING,
                ),
            ],
        )
        with db.transaction("plan retirement contribution") as txn:
            db.add_scheduled(contribution, txn)

        detail = activity.explain_planning_flow_period(
            db,
            PlanningFlowKind.RETIREMENT_SAVING,
            book.brokerage,
            date(2026, 1, 1),
            date(2026, 1, 31),
            as_of=date(2026, 1, 31),
        )

        assert detail.planned == Money("500.00")
        assert detail.actual == Money(0)
        assert len(detail.planned_events) == 1
        assert detail.planned_events[0].description == "401k contribution"
        assert detail.planned_events[0].expected == Money("500.00")
        assert "Explicit split planning purpose: Retirement saving" in " ".join(
            detail.planned_events[0].explanation
        )

    def test_inferred_flow_detail_names_the_account_type_decision(self, db, book):
        retirement = Account(
            name="Workplace plan",
            atype=AccountType.RETIREMENT,
            parent=book.assets,
        )
        contribution = ScheduledTransaction(
            name="Automatic contribution",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 15)),
            splits=[
                ScheduledSplit(retirement.handle, Money("300.00")),
                ScheduledSplit(book.checking, Money("-300.00")),
            ],
        )
        with db.transaction("inferred retirement flow") as txn:
            db.add_account(retirement, txn)
            db.add_scheduled(contribution, txn)

        detail = activity.explain_planning_flow_period(
            db,
            PlanningFlowKind.RETIREMENT_SAVING,
            retirement.handle,
            date(2026, 1, 1),
            date(2026, 1, 31),
        )

        explanation = " ".join(detail.planned_events[0].explanation)
        assert "Inferred Retirement saving" in explanation
        assert "Retirement account Workplace plan" in explanation

    def test_planning_flows_remain_distinct_by_destination_account(self, db, book):
        contribution = ScheduledTransaction(
            name="Split retirement contribution",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 15)),
            splits=[
                ScheduledSplit(book.salary, Money("-1000.00")),
                ScheduledSplit(
                    book.brokerage,
                    Money("600.00"),
                    planning_flow=PlanningFlowKind.RETIREMENT_SAVING,
                ),
                ScheduledSplit(
                    book.savings,
                    Money("400.00"),
                    planning_flow=PlanningFlowKind.RETIREMENT_SAVING,
                ),
            ],
        )
        with db.transaction("plan split contribution") as txn:
            db.add_scheduled(contribution, txn)

        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2026, 1, 31)
        )

        assert len(report.planning_flows) == 2
        amounts = {row.account: row.planned[0] for row in report.planning_flows}
        assert amounts == {
            book.brokerage: Money("600.00"),
            book.savings: Money("400.00"),
        }
        names = {row.account: row.name for row in report.planning_flows}
        assert names[book.brokerage].endswith("Assets:Brokerage")
        assert names[book.savings].endswith("Assets:Savings")

    def test_instantiated_schedule_preserves_planning_flow_for_actuals(self, db, book):
        contribution = ScheduledTransaction(
            name="401k contribution",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 15)),
            splits=[
                ScheduledSplit(book.salary, Money("-1000.00")),
                ScheduledSplit(
                    book.brokerage,
                    Money("1000.00"),
                    planning_flow=PlanningFlowKind.RETIREMENT_SAVING,
                ),
            ],
        )
        actual = contribution.instantiate(date(2026, 1, 15))
        with db.transaction("realize retirement contribution") as txn:
            db.add_scheduled(contribution, txn)
            db.add_transaction(actual, txn)

        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2026, 1, 31)
        )

        flow = report.planning_flows[0]
        assert flow.planned == [Money("1000.00")]
        assert flow.actual == [Money("1000.00")]
        assert flow.variance == [Money(0)]
        stored = db.get_transaction(actual.handle)
        assert stored is not None
        assert stored.splits[1].planning_flow is PlanningFlowKind.RETIREMENT_SAVING

    def test_retirement_distribution_reverses_asset_sign_for_plan(self, db, book):
        withdrawal = Transaction.simple(
            date(2026, 1, 20),
            "Retirement distribution",
            book.checking,
            book.brokerage,
            "750.00",
        )
        withdrawal.splits[1].planning_flow = PlanningFlowKind.RETIREMENT_INCOME
        with db.transaction("retirement income") as txn:
            db.add_transaction(withdrawal, txn)

        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2026, 1, 31)
        )

        flow = report.planning_flows[0]
        assert flow.kind is PlanningFlowKind.RETIREMENT_INCOME
        assert flow.actual == [Money("750.00")]

    def test_retirement_distribution_collapses_cash_counterpart_and_funds_bridge(self, db, book):
        withdrawal = ScheduledTransaction(
            name="Retirement distribution",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 20)),
            splits=[
                ScheduledSplit(
                    book.checking,
                    Money("750.00"),
                    planning_flow=PlanningFlowKind.RETIREMENT_INCOME,
                ),
                ScheduledSplit(
                    book.brokerage,
                    Money("-750.00"),
                    planning_flow=PlanningFlowKind.RETIREMENT_INCOME,
                ),
            ],
        )
        with db.transaction("planned retirement income") as txn:
            db.add_scheduled(withdrawal, txn)

        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2025, 12, 31)
        )

        distributions = [
            row for row in report.planning_flows if row.kind is PlanningFlowKind.RETIREMENT_INCOME
        ]
        assert len(distributions) == 1
        assert distributions[0].account == book.brokerage
        assert distributions[0].planned == [Money("750.00")]
        bridge = {row.kind: row.planned[0] for row in report.cash_bridge}
        assert bridge[activity.CashBridgeKind.RETIREMENT_DISTRIBUTION] == Money("750.00")
        assert report.cash_bridge_totals(activity.PlanMeasure.PLANNED) == [Money("750.00")]
        assert report.cash_bridge_totals(activity.PlanMeasure.PLANNED) == report.cash_totals(
            activity.PlanMeasure.PLANNED
        )

    def test_cash_bridge_exposes_credit_timing_without_losing_expense(self, db, book):
        charge = ScheduledTransaction(
            name="Card purchase",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 10)),
            splits=[
                ScheduledSplit(book.utilities, Money("100.00")),
                ScheduledSplit(book.card, Money("-100.00")),
            ],
        )
        with db.transaction("planned card charge") as txn:
            db.add_scheduled(charge, txn)

        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2025, 12, 31)
        )
        bridge = {row.kind: row.planned[0] for row in report.cash_bridge}

        assert bridge[activity.CashBridgeKind.EXPENSE] == Money("-100.00")
        assert bridge[activity.CashBridgeKind.OTHER] == Money("100.00")
        assert report.cash_bridge_grand_total(activity.PlanMeasure.PLANNED) == Money(0)
        assert report.operating_net_grand_total(activity.PlanMeasure.PLANNED) == Money("-100")

    def test_projected_cash_position_names_exact_low_point(self, db, book):
        with db.transaction("opening and timed plan") as txn:
            db.add_transaction(
                Transaction.simple(
                    date(2025, 12, 31),
                    "Opening cash",
                    book.checking,
                    book.opening,
                    "1000",
                ),
                txn,
            )
            db.add_scheduled(
                ScheduledTransaction(
                    name="Large bill",
                    recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 5)),
                    splits=[
                        ScheduledSplit(book.utilities, Money("700")),
                        ScheduledSplit(book.checking, Money("-700")),
                    ],
                ),
                txn,
            )
            db.add_scheduled(
                ScheduledTransaction(
                    name="Payday",
                    recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 10)),
                    splits=[
                        ScheduledSplit(book.checking, Money("500")),
                        ScheduledSplit(book.salary, Money("-500")),
                    ],
                ),
                txn,
            )

        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2025, 12, 31)
        )

        assert report.cash_position.opening == Money("1000")
        assert report.cash_position.closing == Money("800")
        assert report.cash_position.minimum == Money("300")
        assert report.cash_position.minimum_date == date(2026, 1, 5)
        assert report.operating_net_totals(activity.PlanMeasure.PLANNED) == [Money("-200")]

    def test_matching_actual_inherits_planning_flow_classification(self, db, book):
        contribution = ScheduledTransaction(
            name="401k contribution",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 15)),
            splits=[
                ScheduledSplit(book.salary, Money("-1000.00")),
                ScheduledSplit(
                    book.brokerage,
                    Money("1000.00"),
                    planning_flow=PlanningFlowKind.RETIREMENT_SAVING,
                ),
            ],
        )
        with db.transaction("plan contribution") as txn:
            db.add_scheduled(contribution, txn)
        event = planning.event_by_key(db, contribution.occurrence_key(date(2026, 1, 15)))
        assert event is not None
        actual = Transaction.simple(
            date(2026, 1, 15),
            "401k actual",
            book.brokerage,
            book.salary,
            "1000.00",
        )
        planning.actualize_transaction(actual, event)
        with db.transaction("match contribution") as txn:
            db.add_transaction(actual, txn)

        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2026, 1, 31)
        )

        flow = report.planning_flows[0]
        assert flow.planned == [Money("1000.00")]
        assert flow.actual == [Money("1000.00")]
        assert flow.variance == [Money(0)]
