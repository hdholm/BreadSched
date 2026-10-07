"""Retirement drawdown: balance-dependent withdrawals a scenario projects."""

from datetime import date
from decimal import Decimal

import pytest

from breadsched.gen.engine import projection
from breadsched.gen.lib import (
    Account,
    AccountType,
    Assumptions,
    Drawdown,
    Money,
    Scenario,
    Transaction,
)
from breadsched.gen.services import (
    SaveDrawdown,
    drawdown_accounts,
    remove_drawdown,
    save_drawdown,
)


def flat(**overrides) -> Assumptions:
    base = dict(
        income_growth="0",
        expense_inflation="0",
        investment_return="0",
        cash_interest="0",
        liability_interest="0",
    )
    base.update({k: str(v) for k, v in overrides.items()})
    return Assumptions(**base)


@pytest.fixture
def nest_egg(db, book):
    """A retirement account holding 120,000 and no growth of its own."""
    with db.transaction("Retirement account") as txn:
        ira = Account(name="IRA", atype=AccountType.RETIREMENT, parent=book.assets)
        ira.annual_return = Decimal(0)
        db.add_account(ira, txn)
        db.add_transaction(
            Transaction.simple(date(2025, 12, 1), "Rollover", ira.handle, book.opening, "120000"),
            txn,
        )
    book.ira = ira.handle
    return book


def _scenario(db, assumptions, *drawdowns, years=2):
    scenario = Scenario(name="Retire", start=date(2026, 1, 1), years=years, assumptions=assumptions)
    scenario.drawdowns = list(drawdowns)
    with db.transaction("Add scenario") as txn:
        db.add_scenario(scenario, txn)
    return scenario


def _withdrawals(result, handle):
    return [
        sum(
            (
                split.amount
                for event in row.ledger.events
                if event.key.startswith("drawdown:")
                for split in event.expected_splits
                if split.account == handle
            ),
            Money(0),
        )
        for row in result.rows
    ]


class TestProjection:
    def test_rate_drawdown_takes_a_fraction_of_the_current_balance(self, db, nest_egg):
        rule = Drawdown(
            nest_egg.ira, nest_egg.checking, date(2026, 1, 15), annual_rate=Decimal("0.04")
        )
        result = projection.project(db, _scenario(db, flat(), rule, years=1))

        drawn = _withdrawals(result, nest_egg.checking)
        assert drawn[0] == Money("400.00")
        # Each later month is 4%/12 of what is left, so it shrinks.
        assert drawn[1] == Money("398.67")
        assert all(later < earlier for earlier, later in zip(drawn, drawn[1:], strict=False))
        assert all(row.ledger.reconciles() for row in result.rows)
        assert result.rows[-1].ledger.closing_holdings[nest_egg.ira] == Money("120000") - sum(
            drawn, Money(0)
        )

    def test_fixed_drawdown_escalates_on_each_anniversary(self, db, nest_egg):
        rule = Drawdown(
            nest_egg.ira, nest_egg.checking, date(2026, 3, 10), annual_amount=Money("12000")
        )
        result = projection.project(db, _scenario(db, flat(expense_inflation="0.10"), rule))

        drawn = _withdrawals(result, nest_egg.checking)
        assert drawn[:2] == [Money(0), Money(0)]
        assert drawn[2] == Money("1000.00")
        assert drawn[13] == Money("1000.00")
        assert drawn[14] == Money("1100.00")
        assert result.rows[2].ledger.events[0].planned_date == date(2026, 3, 10)

    def test_fixed_drawdown_without_escalation_stays_level(self, db, nest_egg):
        rule = Drawdown(
            nest_egg.ira,
            nest_egg.checking,
            date(2026, 1, 1),
            annual_amount=Money("12000"),
            escalate=False,
        )
        result = projection.project(db, _scenario(db, flat(expense_inflation="0.10"), rule))

        assert set(_withdrawals(result, nest_egg.checking)) == {Money("1000.00")}

    def test_drawdown_stops_at_its_end_date(self, db, nest_egg):
        rule = Drawdown(
            nest_egg.ira,
            nest_egg.checking,
            date(2026, 1, 5),
            annual_amount=Money("12000"),
            end=date(2026, 6, 30),
        )
        result = projection.project(db, _scenario(db, flat(), rule, years=1))

        drawn = _withdrawals(result, nest_egg.checking)
        assert drawn[:6] == [Money("1000.00")] * 6
        assert drawn[6:] == [Money(0)] * 6

    def test_drawdown_that_runs_out_takes_only_what_is_left(self, db, nest_egg):
        rule = Drawdown(
            nest_egg.ira, nest_egg.checking, date(2026, 1, 1), annual_amount=Money("600000")
        )
        result = projection.project(db, _scenario(db, flat(), rule, years=1))

        drawn = _withdrawals(result, nest_egg.checking)
        assert drawn[:2] == [Money("50000.00"), Money("50000.00")]
        assert drawn[2] == Money("20000.00")
        assert drawn[3:] == [Money(0)] * 9
        assert result.rows[-1].ledger.closing_holdings[nest_egg.ira] == Money(0)
        assert any("runs out of money" in warning for warning in result.warnings)

    def test_retirement_withdrawal_is_a_distribution_and_raises_cash(self, db, nest_egg):
        rule = Drawdown(
            nest_egg.ira, nest_egg.checking, date(2026, 1, 1), annual_amount=Money("24000")
        )
        result = projection.project(db, _scenario(db, flat(), rule, years=1))

        first = result.rows[0]
        assert first.cash_close - first.cash_open == Money("2000.00")
        assert first.ledger.retirement_distributions.get(nest_egg.ira) == Money("2000.00")
        assert first.ledger.reconciles()

    def test_drawdown_into_a_non_cash_account_is_left_out_with_a_warning(self, db, nest_egg):
        rule = Drawdown(
            nest_egg.ira, nest_egg.brokerage, date(2026, 1, 1), annual_amount=Money("1200")
        )
        result = projection.project(db, _scenario(db, flat(), rule, years=1))

        assert _withdrawals(result, nest_egg.brokerage) == [Money(0)] * 12
        assert any("not projected spendable cash" in warning for warning in result.warnings)


class TestService:
    def test_accounts_offer_holdings_as_sources_and_cash_as_targets(self, db, nest_egg):
        sources, targets = drawdown_accounts(db)
        assert nest_egg.ira in sources and nest_egg.brokerage in sources
        assert nest_egg.checking in targets and nest_egg.savings in targets
        assert nest_egg.checking not in sources and nest_egg.assets not in sources

    def test_save_replace_and_remove(self, db, nest_egg):
        scenario = _scenario(db, flat())
        saved = save_drawdown(
            db,
            SaveDrawdown(
                scenario.handle,
                nest_egg.ira,
                nest_egg.checking,
                date(2027, 1, 1),
                annual_rate=Decimal("0.04"),
            ),
        )
        assert saved.ok
        handle = saved.value.handle
        replaced = save_drawdown(
            db,
            SaveDrawdown(
                scenario.handle,
                nest_egg.ira,
                nest_egg.savings,
                date(2027, 1, 1),
                annual_amount=Money("30000"),
                escalate=False,
                handle=handle,
            ),
        )
        assert replaced.ok
        (stored,) = db.get_scenario(scenario.handle).drawdowns
        assert (stored.handle, stored.into, stored.annual_amount, stored.annual_rate) == (
            handle,
            nest_egg.savings,
            Money("30000"),
            None,
        )
        assert stored.escalate is False

        assert remove_drawdown(db, scenario.handle, handle).ok
        assert db.get_scenario(scenario.handle).drawdowns == []
        missing = remove_drawdown(db, scenario.handle, handle)
        assert missing.errors[0].code == "scenario.drawdown.not_found"

    @pytest.mark.parametrize(
        ("changes", "code"),
        [
            ({"account": "checking"}, "scenario.drawdown.account"),
            ({"into": "brokerage"}, "scenario.drawdown.into"),
            ({"annual_amount": Money("100")}, "scenario.drawdown.method"),
            ({"annual_rate": None}, "scenario.drawdown.method"),
            ({"annual_rate": None, "annual_amount": Money(0)}, "scenario.drawdown.amount"),
            ({"annual_rate": Decimal("1.5")}, "scenario.drawdown.rate"),
            ({"end": date(2026, 12, 31)}, "scenario.drawdown.dates"),
            ({"handle": "nope"}, "scenario.drawdown.not_found"),
            ({"scenario": "nope"}, "scenario.not_found"),
        ],
    )
    def test_rejected_drawdown_leaves_the_scenario_unchanged(self, db, nest_egg, changes, code):
        kept = Drawdown(
            nest_egg.ira, nest_egg.checking, date(2026, 1, 1), annual_rate=Decimal("0.03")
        )
        scenario = _scenario(db, flat(), kept)
        before = db.get_scenario(scenario.handle).serialize()
        fields = {
            "scenario": scenario.handle,
            "account": nest_egg.ira,
            "into": nest_egg.checking,
            "start": date(2027, 1, 1),
            "annual_rate": Decimal("0.04"),
        }
        for key, value in changes.items():
            fields[key] = getattr(nest_egg, value) if key in ("account", "into") else value
        result = save_drawdown(db, SaveDrawdown(**fields))

        assert [error.code for error in result.errors] == [code]
        assert db.get_scenario(scenario.handle).serialize() == before


class TestStorage:
    @pytest.mark.parametrize("role", ["account", "into"])
    def test_drawdown_accounts_cannot_be_deleted_from_under_it(self, db, book, role):
        from breadsched.gen.db.base import DbError

        with db.transaction("Accounts") as txn:
            pension = Account(name="Pension", atype=AccountType.RETIREMENT, parent=book.assets)
            spending = Account(name="Spending", atype=AccountType.BANK, parent=book.assets)
            db.add_account(pension, txn)
            db.add_account(spending, txn)
        rule = Drawdown(
            pension.handle, spending.handle, date(2026, 1, 1), annual_rate=Decimal("0.04")
        )
        scenario = _scenario(db, flat(), rule)
        doomed = pension.handle if role == "account" else spending.handle
        with pytest.raises(DbError, match="scenario.missing_account"):
            with db.transaction("Remove") as txn:
                db.remove_account(doomed, txn)
        assert db.get_account(doomed) is not None
        assert scenario.account_references() >= {pension.handle, spending.handle}

    def test_drawdowns_round_trip_and_older_scenarios_load_without_them(self, db, nest_egg):
        rule = Drawdown(
            nest_egg.ira,
            nest_egg.checking,
            date(2030, 7, 31),
            annual_amount=Money("40000.50"),
            end=date(2045, 1, 1),
            escalate=False,
        )
        scenario = _scenario(db, flat(), rule)
        (loaded,) = db.get_scenario(scenario.handle).drawdowns
        assert loaded.serialize() == rule.serialize()

        data = scenario.serialize()
        del data["drawdowns"]
        older = Scenario()
        older.unserialize(data)
        assert older.drawdowns == []
