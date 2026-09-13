"""Investment activity has explicit, shared meanings before lots are introduced."""

from datetime import date

from breadsched.gen.engine import investment, projection
from breadsched.gen.lib import (
    Account,
    AccountType,
    Assumptions,
    InvestmentActivityKind,
    Money,
    PeriodType,
    Rate,
    Recurrence,
    Scenario,
    ScheduledSplit,
    ScheduledTransaction,
    Split,
)


def _accounts(db):
    root = Account(name="Root", atype=AccountType.ROOT)
    bank = Account(name="Household bank", atype=AccountType.BANK, parent=root.handle)
    taxable = Account(name="Taxable holding", atype=AccountType.INVESTMENT, parent=root.handle)
    retirement = Account(name="Retirement", atype=AccountType.RETIREMENT, parent=root.handle)
    retirement_holding = Account(
        name="Fund A", atype=AccountType.INVESTMENT, parent=retirement.handle
    )
    retirement_target = Account(
        name="Fund B", atype=AccountType.INVESTMENT, parent=retirement.handle
    )
    income = Account(name="Investment income", atype=AccountType.INCOME, parent=root.handle)
    expense = Account(name="Investment costs", atype=AccountType.EXPENSE, parent=root.handle)
    with db.transaction("Investment fixture") as txn:
        for account in (
            root,
            bank,
            taxable,
            retirement,
            retirement_holding,
            retirement_target,
            income,
            expense,
        ):
            db.add_account(account, txn)
    return bank, taxable, retirement_holding, retirement_target, income, expense


def _schedule(when, name, *splits):
    return ScheduledTransaction(
        name=name,
        recurrence=Recurrence(PeriodType.ONCE, start=when),
        splits=list(splits),
    )


def test_activity_round_trips_through_transactions_and_schedules():
    split = Split(
        "holding",
        "125",
        investment_activity=InvestmentActivityKind.CONTRIBUTION,
    )
    assert (
        Split.from_dict(split.serialize()).investment_activity
        is InvestmentActivityKind.CONTRIBUTION
    )

    scheduled = ScheduledSplit(
        "holding",
        "-25",
        investment_activity=InvestmentActivityKind.FEE,
    )
    restored = ScheduledSplit.from_dict(scheduled.serialize())
    assert restored.investment_activity is InvestmentActivityKind.FEE


def test_shared_validation_uses_direction_and_retirement_context(db):
    _bank, taxable, retirement, target, _income, _expense = _accounts(db)
    assert investment.retirement_context(db, retirement)
    assert not investment.retirement_context(db, taxable)

    assert investment.activity_problems(
        db,
        [Split(taxable.handle, "-10", investment_activity="contribution")],
    ) == ["Contribution must increase the investment holding"]
    assert investment.activity_problems(
        db,
        [Split(retirement.handle, "-10", investment_activity="withdrawal")],
    ) == ["use Retirement distribution for a withdrawal from retirement"]
    assert (
        investment.activity_problems(
            db,
            [
                Split(retirement.handle, "-10", investment_activity="rollover"),
                Split(target.handle, "10", investment_activity="rollover"),
            ],
        )
        == []
    )


def test_projection_separates_investment_activity_without_changing_state(db):
    bank, taxable, retirement, target, income, expense = _accounts(db)
    schedules = [
        _schedule(
            date(2026, 1, 2),
            "Contribution",
            ScheduledSplit(taxable.handle, "100", investment_activity="contribution"),
            ScheduledSplit(bank.handle, "-100"),
        ),
        _schedule(
            date(2026, 1, 3),
            "Dividend",
            ScheduledSplit(taxable.handle, "10", investment_activity="dividend"),
            ScheduledSplit(income.handle, "-10"),
        ),
        _schedule(
            date(2026, 1, 4),
            "Fee",
            ScheduledSplit(taxable.handle, "-5", investment_activity="fee"),
            ScheduledSplit(expense.handle, "5"),
        ),
        _schedule(
            date(2026, 1, 5),
            "Withdrawal",
            ScheduledSplit(taxable.handle, "-20", investment_activity="withdrawal"),
            ScheduledSplit(bank.handle, "20"),
        ),
        _schedule(
            date(2026, 1, 6),
            "Retirement distribution",
            ScheduledSplit(
                retirement.handle,
                "-15",
                investment_activity="retirement_distribution",
            ),
            ScheduledSplit(bank.handle, "15"),
        ),
        _schedule(
            date(2026, 1, 7),
            "Rollover",
            ScheduledSplit(retirement.handle, "-25", investment_activity="rollover"),
            ScheduledSplit(target.handle, "25", investment_activity="rollover"),
        ),
    ]
    with db.transaction("Investment schedules") as txn:
        for scheduled in schedules:
            db.add_scheduled(scheduled, txn)

    scenario = Scenario(
        name="Investment activity",
        start=date(2026, 1, 1),
        years=1,
        assumptions=Assumptions(
            income_growth=Rate(0),
            expense_inflation=Rate(0),
            investment_return=Rate(0),
            cash_interest=Rate(0),
            liability_interest=Rate(0),
        ),
    )
    row = projection.project(db, scenario).rows[0]

    assert row.contributions == Money("100")
    assert row.withdrawals == Money("20")
    assert row.retirement_distributions == Money("15")
    assert row.investment_income == Money("10")
    assert row.investment_fees == Money("5")
    assert row.rollovers == Money("25")
    assert row.holdings == Money("70")
    assert row.ledger.reconciles()


def test_schedule_instantiation_preserves_activity():
    schedule = _schedule(
        date(2026, 2, 1),
        "Contribution",
        ScheduledSplit("holding", "50", investment_activity="contribution"),
        ScheduledSplit("cash", "-50"),
    )
    transaction = schedule.instantiate(date(2026, 2, 1))
    assert transaction.splits[0].investment_activity is InvestmentActivityKind.CONTRIBUTION
