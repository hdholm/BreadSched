"""A scenario can model spending a savings goal on what it was saved for.

The purchase is a scenario-only one-off on or after the target date: the target
(as the scenario sets it) leaves the goal's account for an expense or asset
account, it never posts, it is not escalated by inflation, and from that month
the goal sets nothing aside. Base and other scenarios are unchanged.
"""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.engine import planning, projection
from breadsched.gen.lib import (
    Money,
    PeriodType,
    Recurrence,
    SavingsGoal,
    Scenario,
    ScheduledSplit,
    ScheduledTransaction,
)
from breadsched.gen.services import SetGoalOverride, set_goal_override


@pytest.fixture
def household(db, book):
    pay = ScheduledTransaction(
        name="Pay",
        recurrence=Recurrence(PeriodType.MONTH, interval=1, start=date(2026, 1, 1)),
        splits=[
            ScheduledSplit(book.checking, Money(1000)),
            ScheduledSplit(book.salary, Money(-1000)),
        ],
    )
    pay.last_posted = date(2026, 6, 1)
    goal = SavingsGoal(
        name="New roof",
        account=book.savings,
        target_amount=Money(1200),
        start_date=date(2026, 1, 1),
        target_date=date(2026, 12, 31),
    )
    scenario = Scenario(name="Buy the roof", start=date(2026, 7, 1), years=1)
    base = Scenario(name="Base", start=date(2026, 7, 1), years=1)
    with db.transaction("Set up") as txn:
        db.add_scheduled(pay, txn)
        db.add_savings_goal(goal, txn)
        db.add_scenario(scenario, txn)
        db.add_scenario(base, txn)
    return goal, scenario, base


def _buy(db, goal, scenario, when=date(2027, 2, 10), account=None, **extra):
    return set_goal_override(
        db,
        SetGoalOverride(
            scenario.handle, goal.handle, purchase_on=when, purchase_account=account, **extra
        ),
    )


def test_the_purchase_spends_the_target_and_ends_the_earmark(db, book, household):
    goal, scenario, base = household
    saved = _buy(db, goal, scenario, account=book.utilities)
    assert saved.ok, saved.errors
    scenario = db.get_scenario(scenario.handle)

    events = planning.scenario_events(db, scenario, date(2026, 7, 1), date(2027, 6, 30))
    [purchase] = [event for event in events if event.description == "Buy: New roof"]
    assert purchase.planned_date == date(2027, 2, 10)
    assert {(split.account, split.amount) for split in purchase.splits} == {
        (book.utilities, Money(1200)),
        (book.savings, Money(-1200)),
    }
    assert purchase.placeholder and purchase.actual_transaction is None

    bought = projection.project(db, scenario)
    unchanged = projection.project(db, db.get_scenario(base.handle))
    feb = next(i for i, row in enumerate(bought.rows) if row.month == date(2027, 2, 1))
    # Earmarked through January, spent in February, never escalated by inflation.
    assert bought.rows[feb - 1].goals_set_aside == Money(1200)
    assert all(row.goals_set_aside == Money(0) for row in bought.rows[feb:])
    assert bought.rows[feb].expense - unchanged.rows[feb].expense == Money(1200)
    assert unchanged.rows[-1].goals_set_aside == Money(1200)
    [milestone] = bought.goal_milestones
    assert milestone.purchase_on == date(2027, 2, 10)
    assert milestone.purchase_account_name == "Expenses:Utilities"
    assert milestone.as_dict()["purchase_on"] == date(2027, 2, 10)

    from breadsched.presentation import goal_milestone_text

    assert goal_milestone_text(milestone).endswith(
        "; this scenario buys it on 2027-02-10 into Expenses:Utilities"
    )
    # Base never models the purchase.
    assert not any(
        event.description.startswith("Buy:")
        for event in planning.scenario_events(
            db, db.get_scenario(base.handle), date(2026, 7, 1), date(2027, 6, 30)
        )
    )


def test_the_purchase_uses_the_scenario_target(db, book, household):
    goal, scenario, _base = household
    assert _buy(db, goal, scenario, account=book.utilities, target_amount=Money(1500)).ok
    events = planning.scenario_events(
        db, db.get_scenario(scenario.handle), date(2026, 7, 1), date(2027, 6, 30)
    )
    [purchase] = [event for event in events if event.description == "Buy: New roof"]
    assert purchase.expected_amount == Money(1500)


@pytest.mark.parametrize(
    ("when", "account", "code", "field"),
    [
        (date(2027, 2, 10), None, "savings_goal.purchase.incomplete", "purchase_account"),
        (None, "utilities", "savings_goal.purchase.incomplete", "purchase_on"),
        (date(2026, 12, 30), "utilities", "savings_goal.purchase.before_target", "purchase_on"),
        (date(2027, 2, 10), "savings", "savings_goal.purchase.account", "purchase_account"),
        (date(2027, 2, 10), "salary", "savings_goal.purchase.account", "purchase_account"),
        (date(2027, 2, 10), "expenses", "savings_goal.purchase.account", "purchase_account"),
    ],
)
def test_invalid_purchases_are_refused_and_keep_the_scenario(
    db, book, household, when, account, code, field
):
    goal, scenario, _base = household
    before = db.get_scenario(scenario.handle).serialize()
    result = _buy(
        db, goal, scenario, when=when, account=getattr(book, account) if account else None
    )
    assert not result.ok
    assert (result.errors[0].code, result.errors[0].fields) == (code, (field,))
    assert db.get_scenario(scenario.handle).serialize() == before


def test_leaving_the_goal_out_drops_its_purchase(db, book, household):
    goal, scenario, _base = household
    assert _buy(db, goal, scenario, account=book.utilities).ok
    assert _buy(db, goal, scenario, account=book.utilities, excluded=True).ok
    override = db.get_scenario(scenario.handle).goal_overrides[goal.handle]
    assert override.excluded and override.purchase_on is None and not override.purchases
    # Clearing every change returns the scenario to the pinned goal.
    cleared = set_goal_override(db, SetGoalOverride(scenario.handle, goal.handle))
    assert cleared.ok and goal.handle not in db.get_scenario(scenario.handle).goal_overrides


def test_an_older_override_without_a_purchase_still_loads():
    from breadsched.gen.lib.scenario import GoalOverride

    old = GoalOverride.from_dict({"target_amount": [5, 1], "target_date": None})
    assert (old.purchase_on, old.purchase_account, old.purchases) == (None, None, False)
    again = GoalOverride.from_dict(old.serialize())
    assert again == old


def test_cli_models_the_purchase(tmp_path, capsys):
    from breadsched.cli.main import main

    book_path = tmp_path / "cli.breadsched"
    main(["init", str(book_path)])
    for argv in (
        ["account", str(book_path), "add", "--name", "Savings", "--type", "BANK",
         "--parent", "Assets", "--opening", "500", "--opening-date", "2026-09-30"],
        ["account", str(book_path), "add", "--name", "Roof", "--type", "EXPENSE",
         "--parent", "Expenses"],
        ["goals", str(book_path), "--add", "New roof", "--account", "Assets:Savings",
         "--target", "400", "--by", "2026-12-31", "--start", "2026-10-01"],
        ["scenario", str(book_path), "save", "--name", "Base", "--years", "1",
         "--start", "2026-10-01"],
    ):  # fmt: skip
        assert main(argv) == 0, argv
    capsys.readouterr()
    command = ["goals", str(book_path), "--override", "New roof", "--scenario", "Base"]
    assert main([*command, "--buy-on", "2027-01-15", "--buy-into", "Expenses:Roof"]) == 0
    assert "bought on 2027-01-15 into Expenses:Roof" in capsys.readouterr().out
    assert main(["project", str(book_path), "--scenario", "Base", "--monthly"]) == 0
    assert "this scenario buys it on 2027-01-15 into Expenses:Roof" in capsys.readouterr().out
    assert main([*command, "--buy-on", "2026-11-01", "--buy-into", "Expenses:Roof"]) == 2
    assert "on or after the goal's target date" in capsys.readouterr().err
