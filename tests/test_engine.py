"""Reading and reporting: balances, registers, budget versus actual."""

from datetime import date
from decimal import Decimal

import pytest

from breadsched.gen.engine import cashflow, ledger, schedule
from breadsched.gen.lib import AccountClass, Money


class TestBalances:
    def test_bank_balance_after_a_month(self, db, funded_book):
        # 5000 opening + 4200 salary - 1800 rent - 310.55 groceries
        assert ledger.balance(db, funded_book.checking, as_of=date(2026, 1, 31)) == Money("7089.45")

    def test_credit_card_reports_what_is_owed_as_positive(self, db, funded_book):
        """A liability's natural sign is the amount owed, not the ledger's minus."""
        assert ledger.balance(db, funded_book.card) == Money("86.40")

    def test_raw_sign_is_available_for_arithmetic_that_must_balance(self, db, funded_book):
        assert ledger.balance(db, funded_book.card, natural_sign=False) == Money("-86.40")

    def test_expense_balance_over_a_window(self, db, funded_book):
        assert ledger.balance(
            db, funded_book.rent, since=date(2026, 2, 1), as_of=date(2026, 2, 28)
        ) == Money("1800.00")

    def test_parent_totals_include_children(self, db, funded_book):
        assert ledger.balance_recursive(db, funded_book.expenses, as_of=date(2026, 2, 28)) == Money(
            "3996.95"
        )

    def test_a_placeholder_holds_nothing_of_its_own(self, db, funded_book):
        assert ledger.balance(db, funded_book.expenses) == Money(0)

    def test_every_transaction_nets_to_zero_across_the_book(self, db, funded_book):
        total = Money(0)
        for account in db.iter_accounts():
            total = total + ledger.balance(db, account.handle, natural_sign=False)
        assert total == Money(0)

    def test_net_worth_is_assets_less_liabilities(self, db, funded_book):
        # Checking 5289.45 at the end of February, less 86.40 on the card.
        assert ledger.net_worth(db, as_of=date(2026, 2, 28)) == Money("5203.05")

    def test_cash_on_hand_ignores_investments(self, db, funded_book):
        assert ledger.cash_on_hand(db, as_of=date(2026, 1, 31)) == Money("7089.45")


class TestRegister:
    def test_running_balance_accumulates_in_date_order(self, db, funded_book):
        rows = ledger.register(db, funded_book.checking)
        assert [row.description for row in rows] == [
            "Opening balance",
            "January rent",
            "Weekly shop",
            "January salary",
            "February rent",
        ]
        assert rows[-1].running == Money("5289.45")

    def test_debit_and_credit_columns_are_exclusive(self, db, funded_book):
        rows = ledger.register(db, funded_book.checking)
        rent = next(r for r in rows if r.description == "January rent")
        assert rent.credit == Money("1800.00")
        assert rent.debit is None

    def test_transfer_column_names_the_other_side(self, db, funded_book):
        rows = ledger.register(db, funded_book.checking)
        rent = next(r for r in rows if r.description == "January rent")
        assert rent.transfer_label(db) == "Expenses:Rent"

    def test_a_windowed_register_opens_from_the_prior_balance(self, db, funded_book):
        rows = ledger.register(db, funded_book.checking, start=date(2026, 2, 1))
        assert len(rows) == 1
        assert rows[0].running == Money("5289.45")

    def test_multi_split_transactions_show_as_split(self, db, book):
        from breadsched.gen.lib import Split, Transaction

        with db.transaction("Split purchase") as txn:
            purchase = Transaction(post_date=date(2026, 3, 1), description="Big shop")
            purchase.add_split(Split(book.groceries, Money("60.00")))
            purchase.add_split(Split(book.utilities, Money("40.00")))
            purchase.add_split(Split(book.checking, Money("-100.00")))
            db.add_transaction(purchase, txn)

        rows = ledger.register(db, book.checking)
        assert rows[0].transfer_label(db) == "-- Split --"


class TestTotalsByClass:
    def test_groups_by_account_class(self, db, funded_book):
        totals = ledger.totals_by_class(db, as_of=date(2026, 2, 28))
        assert totals[AccountClass.LIABILITY] == Money("86.40")
        assert totals[AccountClass.INCOME] == Money("4200.00")


class TestBudgetReport:
    def test_actuals_land_in_the_right_period(self, db, funded_book, monthly_budget):
        report = cashflow.build_report(db, monthly_budget)
        rent = next(line for line in report.lines if line.name == "Expenses:Rent")
        assert rent.periods[0].actual == Money("1800.00")
        assert rent.periods[1].actual == Money("1800.00")
        assert rent.periods[2].actual == Money(0)

    def test_seasonal_budget_amounts_are_preserved(self, db, funded_book, monthly_budget):
        report = cashflow.build_report(db, monthly_budget)
        utilities = next(line for line in report.lines if line.name == "Expenses:Utilities")
        assert utilities.periods[0].budgeted == Money("310.00")
        assert utilities.periods[5].budgeted == Money("150.00")

    def test_underspending_is_favourable_but_underearning_is_not(
        self, db, funded_book, monthly_budget
    ):
        report = cashflow.build_report(db, monthly_budget)
        groceries = next(line for line in report.lines if line.name == "Expenses:Groceries")
        salary = next(line for line in report.lines if line.name == "Income:Salary")
        # Spent 310.55 against a 600 budget; earned 4200 against 4200 then nothing.
        assert groceries.periods[0].favourable is True
        assert salary.periods[1].variance == Money("-4200.00")
        assert salary.periods[1].favourable is False

    def test_progress_fraction_for_a_bar(self, db, funded_book, monthly_budget):
        report = cashflow.build_report(db, monthly_budget)
        rent = next(line for line in report.lines if line.name == "Expenses:Rent")
        assert rent.periods[0].used == 1.0

    def test_net_cash_flow_per_period(self, db, funded_book, monthly_budget):
        report = cashflow.build_report(db, monthly_budget)
        # Budget: 4200 in, 1800 + 600 + 310 out in January.
        assert report.net_cash_flow(0) == Money("1490.00")

    def test_shortfall_periods_are_identified(self, db, book, monthly_budget):
        monthly_budget.set_amount(book.rent, 3, "9000.00")
        report = cashflow.build_report(db, monthly_budget)
        assert 3 in report.shortfall_periods()
        assert 0 not in report.shortfall_periods()

    def test_cumulative_flow_runs_to_the_end_of_the_budget(self, db, funded_book, monthly_budget):
        cumulative = cashflow.build_report(db, monthly_budget).cumulative_cash_flow()
        assert len(cumulative) == 12
        assert cumulative[-1] > cumulative[0]

    def test_unbudgeted_spending_still_appears(self, db, book, monthly_budget):
        from breadsched.gen.lib import Transaction

        surprise = db.get_account_by_name("Expenses:Groceries")
        monthly_budget.lines.pop(surprise.handle, None)
        with db.transaction("Unbudgeted") as txn:
            db.add_transaction(
                Transaction.simple(
                    date(2026, 4, 4), "Vet bill", book.groceries, book.checking, "455.00"
                ),
                txn,
            )
        report = cashflow.build_report(db, monthly_budget, include_unbudgeted=True)
        line = next(line for line in report.lines if line.name == "Expenses:Groceries")
        assert line.budgeted_total == Money(0)
        assert line.actual_total == Money("455.00")

    def test_period_labels_read_as_months(self, db, monthly_budget):
        report = cashflow.build_report(db, monthly_budget)
        assert report.labels[0] == "Jan 2026"
        assert report.labels[11] == "Dec 2026"


class TestScheduleEngine:
    def test_due_list_covers_missed_occurrences(self, db, payday_schedule):
        due = schedule.due_occurrences(db, as_of=date(2026, 2, 1), horizon_days=0)
        assert [occ.when for occ in due] == [
            date(2026, 1, 2),
            date(2026, 1, 16),
            date(2026, 1, 30),
        ]

    def test_posting_writes_real_transactions(self, db, book, payday_schedule):
        posted = schedule.post_due(db, as_of=date(2026, 1, 20))
        assert len(posted) == 2
        assert ledger.balance(db, book.checking) == Money("3876.92")

    def test_posting_twice_does_not_duplicate(self, db, payday_schedule):
        schedule.post_due(db, as_of=date(2026, 1, 20))
        again = schedule.post_due(db, as_of=date(2026, 1, 20))
        assert again == []

    def test_manual_schedules_are_left_alone_by_default(self, db, book, payday_schedule):
        payday_schedule.auto_create = False
        with db.transaction("Make manual") as txn:
            db.commit_scheduled(payday_schedule, txn)
        assert schedule.post_due(db, as_of=date(2026, 1, 20)) == []
        assert len(schedule.post_due(db, as_of=date(2026, 1, 20), only_auto=False)) == 2

    def test_posting_advances_the_last_posted_marker(self, db, payday_schedule):
        schedule.post_due(db, as_of=date(2026, 1, 20))
        assert db.get_scheduled(payday_schedule.handle).last_posted == date(2026, 1, 16)

    def test_forecast_ignores_whether_anything_was_posted(self, db, payday_schedule):
        schedule.post_due(db, as_of=date(2026, 1, 20))
        forecast = schedule.forecast_occurrences(db, date(2026, 1, 1), date(2026, 1, 31))
        assert len(forecast) == 3

    def test_disabled_schedules_do_not_fire(self, db, payday_schedule):
        payday_schedule.enabled = False
        with db.transaction("Disable") as txn:
            db.commit_scheduled(payday_schedule, txn)
        assert schedule.due_occurrences(db, as_of=date(2026, 6, 1)) == []


class TestScheduledTemplates:
    def test_a_formula_split_resolves_at_instantiation(self, db, book):
        from breadsched.gen.lib import (
            PeriodType,
            Recurrence,
            ScheduledSplit,
            ScheduledTransaction,
        )

        sched = ScheduledTransaction(
            name="Interest",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(book.utilities, formula="principal * rate / 12"),
                ScheduledSplit(book.checking, formula="-(principal * rate / 12)"),
            ],
        )
        sched.variables = {"principal": "240000", "rate": "0.05"}
        txn = sched.instantiate(date(2026, 1, 1))
        assert txn.is_balanced()
        assert txn.value_for(book.utilities).quantize(100) == Money("1000.00")

    def test_instantiated_transactions_remember_their_schedule(self, db, payday_schedule):
        txn = payday_schedule.instantiate(date(2026, 1, 2))
        assert txn.scheduled_from == payday_schedule.handle


class TestRateConversion:
    def test_monthly_rate_compounds_back_to_the_annual_rate(self):
        from breadsched.gen.engine.projection import monthly_rate

        monthly = monthly_rate(Decimal("0.06"))
        assert round((1 + monthly) ** 12 - 1, 10) == Decimal("0.06")

    def test_a_zero_rate_stays_zero(self):
        from breadsched.gen.engine.projection import monthly_rate

        assert monthly_rate(Decimal("0")) == Decimal("0")


class TestAnnualNetCashFlow:
    """A budget has to answer 'does the year work', not only 'does March work'."""

    def test_the_net_for_the_period_is_reported(self, db, funded_book, monthly_budget):
        report = cashflow.build_report(db, monthly_budget)
        # 12 x 4200 income, less 12 x (1800 + 600 + 150) plus the winter uplifts.
        assert report.annual_summary()["income"] == Money("50400.00")
        assert report.annual_summary()["expense"] == Money("30900.00")
        assert report.net_cash_flow_total() == Money("19500.00")

    def test_it_equals_the_sum_of_the_periods(self, db, funded_book, monthly_budget):
        report = cashflow.build_report(db, monthly_budget)
        by_period = Money(0)
        for period in range(monthly_budget.periods):
            by_period = by_period + report.net_cash_flow(period)
        assert report.net_cash_flow_total() == by_period

    def test_actuals_get_their_own_total(self, db, funded_book, monthly_budget):
        report = cashflow.build_report(db, monthly_budget)
        assert report.net_cash_flow_total(actual=True) != report.net_cash_flow_total()

    def test_a_year_can_end_ahead_yet_run_dry_inside_it(self, db, book, monthly_budget):
        """Why the annual total does not replace the per-period row."""
        monthly_budget.set_amount(book.rent, 2, "40000.00")
        monthly_budget.set_amount(book.salary, 11, "60000.00")
        report = cashflow.build_report(db, monthly_budget)
        assert report.net_cash_flow_total() > Money(0)
        assert report.shortfall_periods() != []


class TestUnbalancedSchedules:
    """A schedule whose calculated legs disagree must not stop a forecast."""

    @pytest.fixture
    def lopsided(self, db, book):
        from breadsched.gen.lib import (
            PeriodType,
            Recurrence,
            ScheduledSplit,
            ScheduledTransaction,
        )

        sched = ScheduledTransaction(
            name="Mortgage",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(book.rent, formula="1000 + 200"),
                # References something only GnuCash can supply.
                ScheduledSplit(book.checking, formula="-(principal * rate)"),
            ],
        )
        with db.transaction("Add") as txn:
            db.add_scheduled(sched, txn)
        return sched

    def test_the_imbalance_is_measurable(self, lopsided):
        assert lopsided.imbalance() == Money("1200.00")

    def test_strict_instantiation_still_refuses(self, lopsided):
        from breadsched.gen.lib import UnbalancedError

        with pytest.raises(UnbalancedError):
            lopsided.instantiate(date(2026, 1, 1))

    def test_lenient_instantiation_is_allowed_for_forecasting(self, lopsided):
        txn = lopsided.instantiate(date(2026, 1, 1), strict=False)
        assert len(txn.splits) == 2

    def test_a_projection_completes_and_says_which_schedule_is_wrong(self, db, book, lopsided):
        from breadsched.gen.engine import projection
        from breadsched.gen.lib import ProjectionBasis, Scenario

        scenario = Scenario(
            name="With a broken schedule",
            start=date(2026, 1, 1),
            years=2,
            basis=ProjectionBasis.SCHEDULED,
        )
        result = projection.project(db, scenario)

        assert len(result.rows) == 24, "the projection must still run"
        assert any("Mortgage" in warning for warning in result.warnings)
        assert any("does not balance" in warning for warning in result.warnings)

    def test_the_warning_is_not_repeated_for_every_month(self, db, book, lopsided):
        from breadsched.gen.engine import projection
        from breadsched.gen.lib import ProjectionBasis, Scenario

        scenario = Scenario(
            name="Long",
            start=date(2026, 1, 1),
            years=10,
            basis=ProjectionBasis.SCHEDULED,
        )
        result = projection.project(db, scenario)
        assert len([w for w in result.warnings if "Mortgage" in w]) == 1

    def test_a_budget_can_still_be_built_from_it(self, db, book, lopsided):
        from breadsched.gen.engine import budgeting

        budget = budgeting.from_schedules(db, name="2026", start=date(2026, 1, 1), periods=12)
        assert budget.lines
