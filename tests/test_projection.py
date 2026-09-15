"""The forecast: compounding, growth, double-counting, and saved scenarios."""

from datetime import date
from decimal import Decimal

import pytest

from breadsched.gen.engine import projection
from breadsched.gen.lib import (
    Account,
    AccountType,
    AssumptionPeriod,
    Assumptions,
    Money,
    PeriodType,
    Recurrence,
    Scenario,
    ScenarioSchedule,
    ScheduledSplit,
    ScheduledTransaction,
    ScheduleGrowthPolicy,
    Transaction,
)


def flat_assumptions(**overrides) -> Assumptions:
    """No growth anywhere, so a test can isolate one moving part at a time."""
    base = dict(
        income_growth="0",
        expense_inflation="0",
        investment_return="0",
        cash_interest="0",
        liability_interest="0",
    )
    base.update({k: str(v) for k, v in overrides.items()})
    return Assumptions(**base)


class TestAccountRoots:
    def test_parentless_non_root_account_is_not_hidden_from_projection(self, db):
        with db.transaction("rootless chart") as txn:
            checking = Account(name="Checking", atype=AccountType.BANK)
            equity = Account(name="Opening equity", atype=AccountType.EQUITY)
            db.add_account(checking, txn)
            db.add_account(equity, txn)
            db.add_transaction(
                Transaction.simple(
                    date(2026, 1, 1),
                    "Opening balance",
                    checking.handle,
                    equity.handle,
                    "1000.00",
                ),
                txn,
            )

        scenario = Scenario(
            name="Rootless projection",
            start=date(2026, 2, 1),
            years=1,
            assumptions=flat_assumptions(),
        )
        result = projection.project(db, scenario)

        assert result.rows[0].cash_open == Money("1000.00")

    def test_projection_covers_each_month_and_reports_progress(self, db, book):
        scenario = Scenario(
            name="Progress",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(),
        )
        updates = []

        result = projection.project(db, scenario, progress=updates.append)

        assert len(result.rows) == 12
        assert result.rows[0].month == date(2026, 1, 1)
        assert result.rows[-1].month == date(2026, 12, 1)
        assert updates[0].current == date(2026, 1, 1)
        assert updates[-1].current == updates[-1].end
        assert updates[-1].fraction == 1.0


class TestCompounding:
    def test_investments_grow_at_the_account_rate(self, db, book):
        with db.transaction("Seed brokerage") as txn:
            db.add_transaction(
                Transaction.simple(
                    date(2025, 12, 1),
                    "Transfer in",
                    book.brokerage,
                    book.opening,
                    "100000.00",
                ),
                txn,
            )
        scenario = Scenario(
            name="Growth",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(),
        )
        result = projection.project(db, scenario)
        # The account carries its own 7% rate, which beats the (zeroed) global one.
        assert result.rows[-1].holdings.to_decimal() == Decimal("107000.00")

    def test_a_scenario_override_beats_the_account_rate(self, db, book):
        with db.transaction("Seed brokerage") as txn:
            db.add_transaction(
                Transaction.simple(
                    date(2025, 12, 1),
                    "Transfer in",
                    book.brokerage,
                    book.opening,
                    "100000.00",
                ),
                txn,
            )
        scenario = Scenario(
            name="Pessimistic",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(),
        )
        scenario.assumptions.per_account[book.brokerage] = Decimal("0.02")
        result = projection.project(db, scenario)
        assert result.rows[-1].holdings.to_decimal() == Decimal("102000.00")

    def test_cash_interest_compounds_on_the_opening_balance(self, db, funded_book):
        scenario = Scenario(
            name="Savings rate",
            start=date(2026, 3, 1),
            years=1,
            assumptions=flat_assumptions(cash_interest="0.12"),
        )
        result = projection.project(db, scenario)
        assert result.total("interest_earned") > Money(0)
        assert result.rows[-1].cash_close > Money("5289.45")

    def test_debt_grows_when_it_is_not_paid(self, db, book):
        with db.transaction("Card balance") as txn:
            db.add_transaction(
                Transaction.simple(
                    date(2025, 12, 1),
                    "Balance carried",
                    book.groceries,
                    book.card,
                    "5000.00",
                ),
                txn,
            )
        scenario = Scenario(
            name="Minimum payments",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(),
        )
        result = projection.project(db, scenario)
        # The card's own 18.99% rate applies.
        assert result.rows[-1].liabilities.to_decimal() == Decimal("5949.50")
        assert result.rows[-1].net_worth < Money(0)


class TestScheduleDriven:
    def test_scheduled_pay_reaches_the_cash_balance(self, db, book, payday_schedule):
        scenario = Scenario(
            name="Payday",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(),
        )
        result = projection.project(db, scenario)
        assert result.rows[0].income == Money("5815.38")  # three paydays in January
        assert result.rows[0].cash_close == Money("5815.38")

    def test_scheduled_transfers_land_in_holdings_not_expenses(self, db, book):
        sched = ScheduledTransaction(
            name="Invest",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(book.brokerage, Money("500.00")),
                ScheduledSplit(book.checking, Money("-500.00")),
            ],
        )
        with db.transaction("Add") as txn:
            db.add_scheduled(sched, txn)
        scenario = Scenario(
            name="Investing",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(),
        )
        # Zero the brokerage's own 7% so this test measures routing, not growth.
        scenario.assumptions.per_account[book.brokerage] = Decimal("0")
        result = projection.project(db, scenario)
        assert result.rows[0].expense == Money(0)
        assert result.rows[0].contributions == Money("500.00")
        assert result.rows[-1].holdings == Money("6000.00")
        assert result.rows[-1].cash_close == Money("-6000.00")


class TestOneOffs:
    def test_a_one_off_lands_in_its_month(self, db, book):
        scenario = Scenario(
            name="New roof",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(),
        )
        scenario.add_one_off(date(2026, 6, 15), book.utilities, "12000.00", "New roof")
        result = projection.project(db, scenario)
        assert result.rows[5].expense > result.rows[4].expense
        assert result.rows[5].expense - result.rows[4].expense == Money("12000.00")

    def test_a_one_off_outside_the_window_is_ignored(self, db, book):
        scenario = Scenario(
            name="Later",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(),
        )
        scenario.add_one_off(date(2030, 1, 1), book.utilities, "12000.00")
        result = projection.project(db, scenario)
        assert result.total("expense") == Money(0)


class TestDatedAssumptions:
    def test_periods_overlay_base_assumptions_and_later_periods_win(self):
        scenario = Scenario(
            name="Phased",
            assumptions=Assumptions(
                income_growth="0.03",
                expense_inflation="0.02",
                investment_return="0.06",
            ),
            assumption_periods=[
                AssumptionPeriod(
                    date(2030, 1, 1),
                    investment_return="0.04",
                    description="More conservative",
                ),
                AssumptionPeriod(
                    date(2035, 1, 1),
                    investment_return="0.02",
                    expense_inflation="0.05",
                ),
            ],
        )

        before = scenario.assumptions_for(date(2029, 12, 1))
        middle = scenario.assumptions_for(date(2032, 6, 1))
        later = scenario.assumptions_for(date(2036, 1, 1))

        assert before.investment_return == Decimal("0.06")
        assert middle.investment_return == Decimal("0.04")
        assert middle.expense_inflation == Decimal("0.02")
        assert later.investment_return == Decimal("0.02")
        assert later.expense_inflation == Decimal("0.05")

    def test_period_end_is_inclusive(self):
        scenario = Scenario(
            assumptions=flat_assumptions(),
            assumption_periods=[
                AssumptionPeriod(
                    date(2026, 4, 1),
                    end=date(2026, 6, 30),
                    cash_interest="0.08",
                )
            ],
        )
        assert scenario.assumptions_for(date(2026, 6, 30)).cash_interest == Decimal("0.08")
        assert scenario.assumptions_for(date(2026, 7, 1)).cash_interest == Decimal("0")

    def test_an_invalid_period_is_rejected(self):
        try:
            AssumptionPeriod(date(2030, 2, 1), end=date(2030, 1, 1))
        except ValueError as exc:
            assert "end must not precede" in str(exc)
        else:
            raise AssertionError("an inverted assumption period should be rejected")

    def test_dated_per_account_return_changes_projection_mid_run(self, db, book):
        with db.transaction("Seed brokerage") as txn:
            db.add_transaction(
                Transaction.simple(
                    date(2025, 12, 1),
                    "Transfer in",
                    book.brokerage,
                    book.opening,
                    "100000.00",
                ),
                txn,
            )
        scenario = Scenario(
            name="Return changes",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(),
            assumption_periods=[
                AssumptionPeriod(
                    date(2026, 1, 1),
                    end=date(2026, 6, 30),
                    per_account={book.brokerage: "0.12"},
                ),
                AssumptionPeriod(
                    date(2026, 7, 1),
                    per_account={book.brokerage: "0"},
                ),
            ],
        )

        result = projection.project(db, scenario)
        assert result.rows[5].investment_growth > Money(0)
        assert result.rows[6].investment_growth == Money(0)
        assert result.rows[11].holdings == result.rows[5].holdings


class TestComparison:
    def test_two_scenarios_differ_by_their_assumptions(self, db, funded_book):
        groceries = ScenarioSchedule(
            name="Groceries",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 3, 1)),
            splits=[
                ScheduledSplit(funded_book.groceries, Money("500")),
                ScheduledSplit(funded_book.checking, Money("-500")),
            ],
        )
        common = dict(
            start=date(2026, 3, 1),
            years=5,
            schedule_overrides=[groceries],
        )
        cautious = Scenario(
            name="Cautious",
            assumptions=flat_assumptions(expense_inflation="0.06"),
            **common,
        )
        hopeful = Scenario(
            name="Hopeful", assumptions=flat_assumptions(expense_inflation="0.01"), **common
        )
        left = projection.project(db, cautious)
        right = projection.project(db, hopeful)
        rows = projection.compare(left, right)
        assert len(rows) == 60
        assert rows[-1]["net_worth_delta"] > Money(0)


class TestScenarioScheduledEvents:
    def test_recurring_scenario_estimate_changes_projection_state(self, db, funded_book):
        groceries = ScenarioSchedule(
            name="Weekly groceries estimate",
            recurrence=Recurrence(PeriodType.WEEK, start=date(2026, 1, 2)),
            splits=[
                ScheduledSplit(funded_book.groceries, Money("300.00")),
                ScheduledSplit(funded_book.card, Money("-300.00")),
            ],
        )
        scenario = Scenario(
            name="Groceries",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(),
            schedule_overrides=[groceries],
        )

        january = projection.project(db, scenario).rows[0]
        assert january.expense == Money("1500.00")
        assert january.ledger.liability_movements[funded_book.card] == Money("1500.00")


class TestScenarioPersistence:
    def test_derived_scenario_inherits_only_untouched_base_assumptions(self):
        base = Assumptions(
            income_growth="0.04",
            expense_inflation="0.03",
            investment_return="0.07",
            per_account={"brokerage": "0.08"},
        )
        scenario = Scenario.derived_from_base(base, name="Alternative")
        scenario.set_assumption_override("income_growth", "0.01")

        changed_base = Assumptions(
            income_growth="0.05",
            expense_inflation="0.035",
            investment_return="0.06",
            per_account={"brokerage": "0.09"},
        )
        scenario.attach_base_assumptions(changed_base)
        effective = scenario.effective_assumptions()

        assert effective.income_growth == Decimal("0.01")
        assert effective.expense_inflation == Decimal("0.035")
        assert effective.investment_return == Decimal("0.06")
        assert effective.per_account["brokerage"] == Decimal("0.09")
        assert scenario.account_assumption_sources()["brokerage"] == "Base"
        assert scenario.assumption_sources()["income_growth"] == "Alternative"
        assert scenario.assumption_sources()["expense_inflation"] == "Base"

        scenario.set_account_assumption_override("brokerage", "0.055")
        assert scenario.effective_assumptions().per_account["brokerage"] == Decimal("0.055")
        assert scenario.account_assumption_sources()["brokerage"] == "Alternative"
        stored = scenario.serialize()["assumptions"]
        assert stored == {
            "income_growth": "0.01",
            "per_account": {"brokerage": "0.055"},
        }

    def test_previous_alpha_scenario_values_migrate_as_deliberate_overrides(self):
        original = Scenario(
            name="Existing",
            assumptions=Assumptions(income_growth="0.041", investment_return="0.071"),
        ).serialize()
        del original["assumption_inheritance"]

        migrated = Scenario.from_dict(original)
        migrated.attach_base_assumptions(Assumptions(income_growth="0.09"))

        assert migrated.inherits_base_assumptions is True
        assert migrated.assumption_overrides == {
            "income_growth",
            "expense_inflation",
            "investment_return",
            "cash_interest",
            "liability_interest",
        }
        assert migrated.effective_assumptions().income_growth == Decimal("0.041")

    def test_reloaded_derived_scenario_tracks_current_book_base(self, db, funded_book):
        first_base = Assumptions(income_growth="0.04", expense_inflation="0.03")
        db.set_metadata("planning.base_assumptions", first_base.serialize())
        scenario = Scenario.derived_from_base(first_base, name="Live alternative")
        scenario.set_assumption_override("income_growth", "0.01")
        with db.transaction("Save inheriting scenario") as txn:
            db.add_scenario(scenario, txn)

        db.set_metadata(
            "planning.base_assumptions",
            Assumptions(income_growth="0.08", expense_inflation="0.045").serialize(),
        )
        reloaded = db.get_scenario(scenario.handle)
        assert reloaded is not None

        assert reloaded.effective_assumptions().income_growth == Decimal("0.01")
        assert reloaded.effective_assumptions().expense_inflation == Decimal("0.045")

    def test_saved_scenarios_resolve_parent_chains_with_original_provenance(self, db, funded_book):
        base = Assumptions(income_growth="0.04", expense_inflation="0.03")
        db.set_metadata("planning.base_assumptions", base.serialize())
        parent = Scenario.derived_from_base(base, name="Earlier retirement")
        parent.set_assumption_override("income_growth", "0.01")
        parent.set_account_assumption_override(funded_book.savings, "0.055")
        with db.transaction("Save parent") as txn:
            db.add_scenario(parent, txn)
        child = Scenario.derived_from_base(
            parent.effective_assumptions(),
            name="Earlier retirement with lower returns",
            parent_handle=parent.handle,
        )
        child.set_assumption_override("investment_return", "0.035")
        with db.transaction("Save child") as txn:
            db.add_scenario(child, txn)

        reloaded = db.get_scenario(child.handle)

        assert reloaded is not None
        assert reloaded.effective_assumptions().income_growth == Decimal("0.01")
        assert reloaded.effective_assumptions().expense_inflation == Decimal("0.03")
        assert reloaded.effective_assumptions().investment_return == Decimal("0.035")
        assert reloaded.effective_assumptions().per_account[funded_book.savings] == Decimal("0.055")
        assert reloaded.assumption_sources()["income_growth"] == "Earlier retirement"
        assert reloaded.assumption_sources()["expense_inflation"] == "Base"
        assert (
            reloaded.assumption_sources()["investment_return"]
            == "Earlier retirement with lower returns"
        )
        assert reloaded.account_assumption_sources()[funded_book.savings] == "Earlier retirement"

    def test_reparenting_preserves_local_override_identity(self, db, funded_book):
        base = Assumptions(income_growth="0.04", expense_inflation="0.03")
        db.set_metadata("planning.base_assumptions", base.serialize())
        first = Scenario.derived_from_base(base, name="First parent")
        first.set_assumption_override("income_growth", "0.02")
        second = Scenario.derived_from_base(base, name="Second parent")
        second.set_assumption_override("income_growth", "0.07")
        with db.transaction("Save parents") as txn:
            db.add_scenario(first, txn)
            db.add_scenario(second, txn)
        child = Scenario.derived_from_base(
            first.effective_assumptions(), name="Child", parent_handle=first.handle
        )
        child.set_assumption_override("expense_inflation", "0.01")
        with db.transaction("Save child") as txn:
            db.add_scenario(child, txn)

        child.parent_handle = second.handle
        with db.transaction("Reparent child") as txn:
            db.commit_scenario(child, txn)
        reloaded = db.get_scenario(child.handle)

        assert reloaded is not None
        assert reloaded.assumption_overrides == {"expense_inflation"}
        assert reloaded.effective_assumptions().income_growth == Decimal("0.07")
        assert reloaded.effective_assumptions().expense_inflation == Decimal("0.01")

    def test_parent_cycles_and_parent_deletion_are_rejected(self, db, funded_book):
        base = Assumptions()
        orphan = Scenario.derived_from_base(base, name="Orphan", parent_handle="missing")
        with pytest.raises(ValueError, match="does not exist"):
            with db.transaction("Save orphan") as txn:
                db.add_scenario(orphan, txn)
        first = Scenario.derived_from_base(base, name="Parent")
        with db.transaction("Save parent") as txn:
            db.add_scenario(first, txn)
        child = Scenario.derived_from_base(base, name="Child", parent_handle=first.handle)
        with db.transaction("Save child") as txn:
            db.add_scenario(child, txn)

        first.parent_handle = child.handle
        with pytest.raises(ValueError, match="cycle"):
            with db.transaction("Create cycle") as txn:
                db.commit_scenario(first, txn)
        with pytest.raises(ValueError, match="Child"):
            with db.transaction("Delete live parent") as txn:
                db.remove_scenario(first.handle, txn)

        assert db.get_scenario(first.handle) is not None
        assert db.get_scenario(child.handle) is not None

    def test_a_saved_scenario_reproduces_its_forecast(self, db, funded_book):
        scenario = Scenario(
            name="Saved",
            start=date(2026, 3, 1),
            years=2,
            assumptions=Assumptions(income_growth="0.04", expense_inflation="0.03"),
        )
        scenario.add_one_off(date(2026, 9, 1), funded_book.utilities, "2500.00", "Boiler")
        scenario.schedule_overrides.append(
            ScenarioSchedule(
                name="Weekly groceries",
                recurrence=Recurrence(PeriodType.WEEK, start=date(2026, 3, 6)),
                splits=[
                    ScheduledSplit(funded_book.groceries, Money("275.00")),
                    ScheduledSplit(funded_book.card, Money("-275.00")),
                ],
            )
        )
        scenario.opening_overrides[funded_book.savings] = Money("15000.00")
        with db.transaction("Save scenario") as txn:
            db.add_scenario(scenario, txn)

        reloaded = db.get_scenario(scenario.handle)
        assert reloaded.assumptions.income_growth == Decimal("0.04")
        assert reloaded.one_offs[0].description == "Boiler"
        assert reloaded.schedule_overrides[0].name == "Weekly groceries"
        assert reloaded.schedule_overrides[0].splits[0].amount == Money("275.00")
        assert reloaded.opening_overrides[funded_book.savings] == Money("15000.00")

        before = projection.project(db, scenario).summary()
        after = projection.project(db, reloaded).summary()
        assert before == after

    def test_dated_assumptions_round_trip_with_the_scenario(self, db, funded_book):
        scenario = Scenario(
            name="Retirement transition",
            start=date(2030, 1, 1),
            assumptions=Assumptions(investment_return="0.07"),
            assumption_periods=[
                AssumptionPeriod(
                    date(2035, 7, 1),
                    investment_return="0.045",
                    cash_interest="0.025",
                    per_account={funded_book.savings: "0.03"},
                    description="Post-work assumptions",
                )
            ],
        )
        with db.transaction("Save dated scenario") as txn:
            db.add_scenario(scenario, txn)

        reloaded = db.get_scenario(scenario.handle)
        assert reloaded is not None
        assert len(reloaded.assumption_periods) == 1
        period = reloaded.assumption_periods[0]
        assert period.start == date(2035, 7, 1)
        assert period.description == "Post-work assumptions"
        resolved = reloaded.assumptions_for(date(2036, 1, 1))
        assert resolved.investment_return == Decimal("0.045")
        assert resolved.cash_interest == Decimal("0.025")
        assert resolved.per_account[funded_book.savings] == Decimal("0.03")

    def test_scenarios_are_listed_by_name(self, db, funded_book):
        with db.transaction("Save two") as txn:
            db.add_scenario(Scenario(name="Optimistic"), txn)
            db.add_scenario(Scenario(name="Careful"), txn)
        assert [s.name for s in db.iter_scenarios()] == ["Careful", "Optimistic"]

    def test_a_saved_scenario_picks_up_new_actuals(self, db, funded_book):
        """Scenarios store assumptions, never results, so they never go stale."""
        scenario = Scenario(
            name="Live",
            start=date(2026, 3, 1),
            years=1,
            assumptions=flat_assumptions(),
        )
        with db.transaction("Save") as txn:
            db.add_scenario(scenario, txn)
        first = projection.project(db, db.get_scenario(scenario.handle))

        with db.transaction("A windfall arrives") as txn:
            db.add_transaction(
                Transaction.simple(
                    date(2026, 2, 20),
                    "Bonus",
                    funded_book.checking,
                    funded_book.salary,
                    "10000.00",
                ),
                txn,
            )
        second = projection.project(db, db.get_scenario(scenario.handle))
        assert second.rows[0].cash_open - first.rows[0].cash_open == Money("10000.00")


class TestMonthlyStateLedger:
    def test_escrow_is_restricted_asset_and_expense_is_recognized_when_funded(self, db, book):
        escrow = Account(name="Escrow", atype=AccountType.BANK, parent=book.assets)
        escrow.atype = AccountType.ESCROW
        funding = ScheduledTransaction(
            name="Fund escrow",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 5)),
            splits=[
                ScheduledSplit(escrow.handle, Money("100")),
                ScheduledSplit(book.checking, Money("-100")),
            ],
        )
        payout = ScheduledTransaction(
            name="Escrow payout",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 2, 5)),
            splits=[
                ScheduledSplit(book.utilities, Money("80")),
                ScheduledSplit(escrow.handle, Money("-80")),
            ],
        )
        with db.transaction("Escrow schedules") as txn:
            db.add_account(escrow, txn)
            db.add_scheduled(funding, txn)
            db.add_scheduled(payout, txn)

        scenario = Scenario(
            name="Escrow",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(),
        )
        rows = projection.project(db, scenario).rows
        assert rows[0].expense == Money("100")
        assert rows[0].cash_close == Money("-100")
        assert rows[0].holdings == Money("100")
        assert rows[1].expense == Money(0)
        assert rows[1].cash_close == Money("-100")
        assert rows[1].holdings == Money("20")
        assert all(row.ledger.reconciles() for row in rows)

    def test_mortgage_payment_reconciles_cash_escrow_debt_and_net_worth(self, db, book):
        house = Account(name="House", atype=AccountType.ASSET, parent=book.assets)
        escrow = Account(name="Escrow", atype=AccountType.ESCROW, parent=book.assets)
        mortgage = Account(name="Mortgage", atype=AccountType.LOAN, parent=book.liabilities)
        mortgage.linked_asset = house.handle
        payment = ScheduledTransaction(
            name="Mortgage payment",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 15)),
            splits=[
                ScheduledSplit(mortgage.handle, Money("800")),
                ScheduledSplit(book.utilities, Money("1150")),
                ScheduledSplit(escrow.handle, Money("450")),
                ScheduledSplit(book.checking, Money("-2400")),
            ],
        )
        with db.transaction("Mortgage example") as txn:
            db.add_account(house, txn)
            db.add_account(escrow, txn)
            db.add_account(mortgage, txn)
            db.add_transaction(
                Transaction.simple(
                    date(2025, 12, 31),
                    "Opening cash",
                    book.checking,
                    book.opening,
                    "10000",
                ),
                txn,
            )
            db.add_transaction(
                Transaction.simple(
                    date(2025, 12, 31), "House purchase", house.handle, book.opening, "400000"
                ),
                txn,
            )
            db.add_transaction(
                Transaction.simple(
                    date(2025, 12, 31),
                    "Opening mortgage",
                    book.opening,
                    mortgage.handle,
                    "320000",
                ),
                txn,
            )
            db.add_scheduled(payment, txn)

        result = projection.project(
            db,
            Scenario(
                name="Mortgage",
                start=date(2026, 1, 1),
                years=1,
                assumptions=flat_assumptions(),
            ),
        )
        january = result.rows[0]

        assert january.cash_open == Money("10000")
        assert january.cash_close == Money("7600")
        assert january.ledger.closing_holdings[escrow.handle] == Money("450")
        assert january.ledger.closing_holdings[house.handle] == Money("400000")
        assert january.ledger.closing_liabilities[mortgage.handle] == Money("319200")
        assert january.debt_payments == Money("800")
        assert january.expense == Money("1600")
        opening_net_worth = (
            january.cash_open + january.ledger.holdings_open - january.ledger.liabilities_open
        )
        assert january.net_worth == opening_net_worth - Money("1150")
        assert january.ledger.reconciles()

    def test_scenario_escrow_override_explains_a_negative_balance(self, db, book):
        escrow = Account(name="Escrow", atype=AccountType.ESCROW, parent=book.assets)
        draw = ScheduledTransaction(
            name="Annual escrow draw",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 20)),
            splits=[
                ScheduledSplit(book.utilities, Money("80")),
                ScheduledSplit(escrow.handle, Money("-80")),
            ],
        )
        with db.transaction("Escrow opening and schedule") as txn:
            db.add_account(escrow, txn)
            db.add_transaction(
                Transaction.simple(
                    date(2025, 12, 31),
                    "Opening restricted balance",
                    escrow.handle,
                    book.opening,
                    "100",
                ),
                txn,
            )
            db.add_scheduled(draw, txn)

        alternate = ScenarioSchedule.from_scheduled(draw)
        alternate.splits = [
            ScheduledSplit(book.utilities, Money("120")),
            ScheduledSplit(escrow.handle, Money("-120")),
        ]
        scenario = Scenario(
            name="Higher insurance",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(),
            schedule_overrides=[alternate],
        )

        result = projection.project(db, scenario)
        detail = projection.explain_month(db, result, 0)

        assert result.rows[0].expense == Money(0)
        assert result.rows[0].holdings == Money("-20")
        assert any("falls below zero by 20.00" in warning for warning in result.warnings)
        assert any("draw covers expense" in line for line in detail.escrow_explanations)
        assert result.rows[0].ledger.reconciles()

    def test_each_month_reconciles_from_opening_to_closing_state(self, db, funded_book):
        scenario = Scenario(
            name="Explainable",
            start=date(2026, 3, 1),
            years=2,
            assumptions=flat_assumptions(cash_interest="0.02"),
        )
        result = projection.project(db, scenario)

        assert result.rows
        assert all(row.ledger.reconciles() for row in result.rows)
        first = result.rows[0]
        assert first.ledger.opening_cash == first.cash_open
        assert first.ledger.closing_cash == first.cash_close
        assert first.ledger.holdings_close == first.holdings
        assert first.ledger.liabilities_close == first.liabilities
        assert (
            first.ledger.opening_cash + first.ledger.cash_flow + first.ledger.cash_interest
            == first.ledger.closing_cash
        )

    def test_investment_contributions_and_growth_are_attributed_by_account(self, db, book):
        sched = ScheduledTransaction(
            name="Invest",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(book.brokerage, Money("500.00")),
                ScheduledSplit(book.checking, Money("-500.00")),
            ],
        )
        with db.transaction("Add investment schedule") as txn:
            db.add_scheduled(sched, txn)

        scenario = Scenario(
            name="Explain investing",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(investment_return="0.06"),
        )
        row = projection.project(db, scenario).rows[0]

        assert row.ledger.holding_contributions[book.brokerage] == Money("500.00")
        assert book.brokerage in row.ledger.investment_growth
        assert row.ledger.reconciles()

    def test_debt_interest_and_payments_are_attributed_by_account(self, db, book):
        with db.transaction("Seed card and payment") as txn:
            db.add_transaction(
                Transaction.simple(
                    date(2025, 12, 1),
                    "Balance carried",
                    book.groceries,
                    book.card,
                    "1000.00",
                ),
                txn,
            )
            db.add_scheduled(
                ScheduledTransaction(
                    name="Card payment",
                    recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
                    splits=[
                        ScheduledSplit(book.card, Money("100.00")),
                        ScheduledSplit(book.checking, Money("-100.00")),
                    ],
                ),
                txn,
            )

        scenario = Scenario(
            name="Explain debt",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(),
        )
        row = projection.project(db, scenario).rows[0]

        assert row.ledger.opening_liabilities[book.card] == Money("1000.00")
        assert row.ledger.liability_interest[book.card] > Money(0)
        assert row.ledger.debt_payments[book.card] == Money("100.00")
        assert row.ledger.reconciles()

    def test_month_row_dict_contains_structured_ledger(self, db, book):
        scenario = Scenario(
            name="JSON detail",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(),
        )
        data = projection.project(db, scenario).rows[0].as_dict()

        ledger = data["ledger"]
        assert isinstance(ledger, dict)
        assert ledger["opening_cash"] == data["cash_open"]
        assert ledger["closing_cash"] == data["cash_close"]

    def test_month_explanation_names_accounts_and_reconciles_effects(self, db, book):
        with db.transaction("Seed card and investment schedule") as txn:
            db.add_transaction(
                Transaction.simple(
                    date(2025, 12, 1), "Card balance", book.groceries, book.card, "500"
                ),
                txn,
            )
            db.add_scheduled(
                ScheduledTransaction(
                    name="Invest",
                    recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
                    splits=[
                        ScheduledSplit(book.brokerage, Money("100")),
                        ScheduledSplit(book.checking, Money("-100")),
                    ],
                ),
                txn,
            )
        scenario = Scenario(
            name="Explain",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(investment_return="0.06"),
        )
        result = projection.project(db, scenario)
        detail = projection.explain_month(db, result, 0)

        assert detail.label == "Jan 2026"
        assert detail.cash_open + detail.cash_flow + detail.cash_interest == detail.cash_close
        assert any(item.handle == book.brokerage for item in detail.holdings)
        assert any(item.handle == book.card for item in detail.liabilities)
        assert detail.events[0].description == "Invest"
        assert detail.assumptions.investment_return == Decimal("0.06")

    def test_month_explanation_omits_inactive_zero_accounts(self, db, book):
        scenario = Scenario(
            name="Explain",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(),
        )
        result = projection.project(db, scenario)
        detail = projection.explain_month(db, result, 0)

        assert all(
            any((item.opening, item.movement, item.accrual, item.closing))
            for item in detail.holdings
        )
        assert all(
            any((item.opening, item.movement, item.accrual, item.closing))
            for item in detail.liabilities
        )

    def test_month_explanation_rejects_an_unknown_index(self, db, book):
        scenario = Scenario(
            name="Explain",
            start=date(2026, 1, 1),
            years=1,
            assumptions=flat_assumptions(),
        )
        result = projection.project(db, scenario)
        import pytest

        with pytest.raises(IndexError):
            projection.explain_month(db, result, 12)


class TestScheduledGrowthPolicy:
    def test_mixed_payroll_grows_with_income_in_auto_mode(self, db, book):
        with db.transaction("Add payroll") as txn:
            db.add_scheduled(
                ScheduledTransaction(
                    name="Payroll",
                    recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
                    splits=[
                        ScheduledSplit(book.salary, Money("-100.00")),
                        ScheduledSplit(book.rent, Money("20.00")),
                        ScheduledSplit(book.checking, Money("80.00")),
                    ],
                ),
                txn,
            )

        scenario = Scenario(
            name="Income growth",
            start=date(2026, 1, 1),
            years=2,
            assumptions=flat_assumptions(income_growth="0.10"),
        )
        result = projection.project(db, scenario)

        assert result.rows[0].income == Money("100.00")
        assert result.rows[0].expense == Money("20.00")
        assert result.rows[12].income == Money("110.00")
        assert result.rows[12].expense == Money("22.00")
        assert result.rows[12].cash_close - result.rows[11].cash_close == Money("88.00")

    def test_explicit_none_disables_automatic_expense_inflation(self, db, book):
        with db.transaction("Add fixed expense") as txn:
            db.add_scheduled(
                ScheduledTransaction(
                    name="Fixed fee",
                    recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
                    splits=[
                        ScheduledSplit(book.rent, Money("25.00")),
                        ScheduledSplit(book.checking, Money("-25.00")),
                    ],
                    growth_policy=ScheduleGrowthPolicy.NONE,
                ),
                txn,
            )

        scenario = Scenario(
            name="Inflation",
            start=date(2026, 1, 1),
            years=2,
            assumptions=flat_assumptions(expense_inflation="0.10"),
        )
        result = projection.project(db, scenario)

        assert result.rows[0].expense == Money("25.00")
        assert result.rows[12].expense == Money("25.00")

    def test_scenario_override_preserves_growth_policy(self, db, book):
        source = ScheduledTransaction(
            name="Salary",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(book.salary, Money("-100.00")),
                ScheduledSplit(book.checking, Money("100.00")),
            ],
            growth_policy=ScheduleGrowthPolicy.INCOME,
        )

        copied = ScenarioSchedule.from_scheduled(source)
        reloaded = ScenarioSchedule.from_dict(copied.serialize())

        assert copied.growth_policy is ScheduleGrowthPolicy.INCOME
        assert reloaded.growth_policy is ScheduleGrowthPolicy.INCOME


class TestEventProjectionScaling:
    def test_assumption_resolution_is_cached_across_many_events(self, db, book, monkeypatch):
        with db.transaction("Add daily income") as txn:
            db.add_scheduled(
                ScheduledTransaction(
                    name="Daily income",
                    recurrence=Recurrence(PeriodType.DAY, start=date(2026, 1, 1)),
                    splits=[
                        ScheduledSplit(book.checking, Money("10.00")),
                        ScheduledSplit(book.salary, Money("-10.00")),
                    ],
                ),
                txn,
            )

        scenario = Scenario(
            name="Long event stream",
            start=date(2026, 1, 1),
            years=10,
            assumptions=flat_assumptions(income_growth="0.03"),
        )
        calls = 0
        original = Scenario.assumptions_for

        def counted(self, when):
            nonlocal calls
            calls += 1
            return original(self, when)

        monkeypatch.setattr(Scenario, "assumptions_for", counted)
        result = projection.project(db, scenario)

        assert len(result.rows) == 120
        # Thousands of events share one precomputed timeline; resolving assumptions
        # must depend on change points, not on event count or distance into the plan.
        assert calls <= 2

    def test_long_daily_projection_keeps_money_rationals_bounded(self, db, funded_book):
        with db.transaction("Add daily expense") as txn:
            db.add_scheduled(
                ScheduledTransaction(
                    name="Daily expense",
                    recurrence=Recurrence(PeriodType.DAY, start=date(2026, 1, 1)),
                    splits=[
                        ScheduledSplit(funded_book.checking, Money("-1.00")),
                        ScheduledSplit(funded_book.groceries, Money("1.00")),
                    ],
                ),
                txn,
            )

        scenario = Scenario(
            name="Bounded rationals",
            start=date(2026, 1, 1),
            years=10,
            assumptions=flat_assumptions(cash_interest="0.04"),
        )
        result = projection.project(db, scenario)

        assert len(result.rows) == 120
        # Interest rates are Decimal approximations. Projection compounding must not
        # turn those into ever-growing exact-rational denominators as events accrue.
        assert max(row.cash_close.denominator for row in result.rows) <= 100_000_000
        assert all(row.ledger.reconciles() for row in result.rows)
