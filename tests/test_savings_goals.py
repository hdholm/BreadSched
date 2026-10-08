"""Savings goals set aside a prorated share of each income, like a pending bill."""

from datetime import date

import pytest

from breadsched.gen.engine import savings_goals
from breadsched.gen.lib import (
    GoalAllocation,
    Money,
    PeriodType,
    Recurrence,
    SavingsGoal,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)


def _monthly_pay(db, book, last_posted):
    pay = ScheduledTransaction(
        name="Pay",
        recurrence=Recurrence(PeriodType.MONTH, interval=1, start=date(2026, 1, 1)),
        splits=[
            ScheduledSplit(book.checking, Money(1000)),
            ScheduledSplit(book.salary, Money(-1000)),
        ],
    )
    pay.last_posted = last_posted
    with db.transaction("Pay") as txn:
        db.add_scheduled(pay, txn)
    return pay


def _goal(db, book, account=None, **kwargs):
    goal = SavingsGoal(
        name="New roof",
        account=account or book.savings,
        target_amount=Money(1200),
        start_date=date(2026, 1, 1),
        target_date=date(2026, 12, 31),
        **kwargs,
    )
    with db.transaction("Goal") as txn:
        db.add_savings_goal(goal, txn)
    return goal


def test_each_income_received_sets_aside_its_share(db, book):
    _monthly_pay(db, book, last_posted=date(2026, 6, 1))
    goal = _goal(db, book)
    progress = savings_goals.goal_progress(db, goal, date(2026, 6, 15))
    # Twelve monthly paychecks fund the goal; six have arrived.
    assert (progress.set_aside, progress.remaining) == (Money(600), Money(600))
    assert (progress.status, progress.basis) == ("saving", "income")
    assert progress.from_income == Money(600) and progress.allocated == Money(0)
    before = savings_goals.goal_progress(db, goal, date(2025, 12, 31))
    assert (before.set_aside, before.status) == (Money(0), "not started")


def test_extra_money_is_set_aside_in_full_and_later_income_spreads_the_rest(db, book):
    _monthly_pay(db, book, last_posted=date(2026, 8, 1))
    goal = _goal(db, book, allocations=[GoalAllocation(date(2026, 6, 10), Money(300), "Bonus")])
    # 600 from six paychecks, then 300 extra: the remaining 300 is spread over
    # the six paychecks from July to December, 50 each.
    assert savings_goals.goal_progress(db, goal, date(2026, 6, 10)).set_aside == Money(900)
    progress = savings_goals.goal_progress(db, goal, date(2026, 8, 15))
    assert progress.set_aside == Money(1000)
    assert (progress.allocated, progress.from_income) == (Money(300), Money(700))


def test_missed_income_sets_nothing_aside(db, book):
    _monthly_pay(db, book, last_posted=date(2026, 4, 1))
    goal = _goal(db, book)
    # May and June pay never arrived: four received of ten that can still count.
    progress = savings_goals.goal_progress(db, goal, date(2026, 6, 15))
    assert progress.set_aside == Money(480)


def test_without_expected_income_time_is_the_basis(db, book):
    goal = _goal(db, book)
    progress = savings_goals.goal_progress(db, goal, date(2026, 6, 30))
    assert progress.basis == "time"
    assert progress.set_aside == (Money(1200) * 181 / 365).quantize(100)


def test_after_the_target_date_the_whole_target_stays_set_aside_until_closed(db, book):
    goal = _goal(db, book)
    reached = savings_goals.goal_progress(db, goal, date(2027, 1, 5))
    assert (reached.set_aside, reached.status) == (Money(1200), "reached")
    goal.closed_on = date(2027, 2, 1)
    closed = savings_goals.goal_progress(db, goal, date(2027, 2, 1))
    assert (closed.set_aside, closed.status) == (Money(0), "closed")


@pytest.mark.parametrize("brokerage_balance", ["0", "500", "2000"])
def test_only_money_not_already_in_a_non_cash_account_is_held_from_spending(
    db, book, brokerage_balance
):
    with db.transaction("Fund") as txn:
        db.add_transaction(
            Transaction.simple(
                date(2025, 12, 1), "Opening", book.brokerage, book.opening, brokerage_balance
            ),
            txn,
        )
    cash_goal = _goal(db, book)
    invested = _goal(db, book, account=book.brokerage)
    progress = savings_goals.goals_progress(db, date(2027, 1, 5), [cash_goal, invested])
    assert [item.liquid_account for item in progress] == [True, False]
    covered = min(Money(1200), Money(brokerage_balance))
    assert savings_goals.spendable_hold(progress) == Money(1200) + Money(1200) - covered


def test_dashboard_holds_goal_earmarks_like_bill_reserves(db, book):
    from breadsched.gen.engine import dashboard

    _monthly_pay(db, book, last_posted=date(2026, 6, 1))
    with db.transaction("Cash") as txn:
        db.add_transaction(
            Transaction.simple(date(2026, 1, 1), "Opening", book.savings, book.opening, "5000"),
            txn,
        )
    before = dashboard.build(db, as_of=date(2026, 6, 15))
    _goal(db, book)
    after = dashboard.build(db, as_of=date(2026, 6, 15))
    assert after.liquid == before.liquid == Money(5000)
    assert (after.goals_set_aside, after.goals_held) == (Money(600), Money(600))
    assert after.available == before.available - Money(600)
    summary = after.summary()
    assert (summary["goals_set_aside"], summary["goals_held"]) == (Money(600), Money(600))
    assert [item.goal.name for item in after.goals] == ["New roof"]


def _request(book, **changes):
    from breadsched.gen.services import SaveSavingsGoal

    values = {
        "name": "New roof",
        "account": book.savings,
        "target_amount": Money(1200),
        "target_date": date(2026, 12, 31),
        "start_date": date(2026, 1, 1),
    }
    values.update(changes)
    return SaveSavingsGoal(**values)


def test_service_validates_and_rejected_edits_keep_the_stored_goal(db, book):
    from breadsched.gen.services import save_savings_goal

    saved = save_savings_goal(db, _request(book))
    assert saved.ok
    goal = saved.value
    for changes, code in (
        ({"name": "  "}, "savings_goal.name.required"),
        ({"account": book.groceries}, "savings_goal.account.invalid"),
        ({"account": book.assets}, "savings_goal.account.invalid"),
        ({"target_amount": Money(0)}, "savings_goal.target.invalid"),
        ({"target_date": date(2026, 1, 1)}, "savings_goal.dates.invalid"),
    ):
        rejected = save_savings_goal(db, _request(book, handle=goal.handle, **changes))
        assert not rejected.ok and rejected.errors[0].code == code
        stored = db.get_savings_goal(goal.handle)
        assert stored.serialize() == goal.serialize()
    missing = save_savings_goal(db, _request(book, handle="missing"))
    assert missing.errors[0].code == "savings_goal.not_found"


def test_service_allocates_closes_reopens_and_reports(db, book):
    from breadsched.gen.services import (
        AllocateToGoal,
        allocate_to_goal,
        close_savings_goal,
        delete_savings_goal,
        query_savings_goals,
        reopen_savings_goal,
        save_savings_goal,
    )

    goal = save_savings_goal(db, _request(book)).value
    allocated = allocate_to_goal(db, AllocateToGoal(goal.handle, Money(200), date(2026, 3, 1)))
    assert allocated.ok and allocated.value.allocated(date(2026, 3, 1)) == Money(200)
    too_much = allocate_to_goal(db, AllocateToGoal(goal.handle, Money(1001), date(2026, 3, 2)))
    assert too_much.errors[0].code == "savings_goal.allocation.exceeds_target"
    assert db.get_savings_goal(goal.handle).allocated(date.max) == Money(200)
    below = save_savings_goal(db, _request(book, handle=goal.handle, target_amount=Money(100)))
    assert below.errors[0].code == "savings_goal.target.below_allocated"

    report = query_savings_goals(db, date(2026, 3, 5)).value
    assert [item.goal.name for item in report.goals] == ["New roof"]
    assert report.set_aside == report.held == report.goals[0].set_aside
    assert report.goals[0].allocated == Money(200)

    assert close_savings_goal(db, goal.handle, date(2026, 4, 1)).ok
    assert query_savings_goals(db, date(2026, 4, 2)).value.goals == ()
    closed = query_savings_goals(db, date(2026, 4, 2), include_closed=True).value
    assert [item.status for item in closed.goals] == ["closed"]
    late = allocate_to_goal(db, AllocateToGoal(goal.handle, Money(5), date(2026, 4, 2)))
    assert late.errors[0].code == "savings_goal.closed"
    assert reopen_savings_goal(db, goal.handle).ok
    assert delete_savings_goal(db, goal.handle).ok
    assert db.get_savings_goal(goal.handle) is None


def test_goal_account_cannot_be_deleted_from_under_a_goal(db, book):
    from breadsched.gen.db.base import DbError
    from breadsched.gen.services import save_savings_goal

    save_savings_goal(db, _request(book))
    with pytest.raises(DbError, match="savings_goal.missing_account"):
        with db.transaction("Remove") as txn:
            db.remove_account(book.savings, txn)
    assert db.get_account(book.savings) is not None


def test_schema_9_book_migrates_to_savings_goals(tmp_path):
    import sqlite3
    from pathlib import Path

    from breadsched.gen.db.sqlite import DbSQLite

    path = tmp_path / "schema-9.breadsched"
    sql = (Path(__file__).parent / "fixtures" / "native" / "schema-9.sql").read_text()
    with sqlite3.connect(path) as raw:
        raw.executescript(sql)
    db = DbSQLite()
    db.load(str(path))
    try:
        assert db.get_metadata("schema_version") == 11
        assert db.get_metadata("fixture_marker") == "schema-9"
        assert list(db.iter_savings_goals()) == []
        goal = SavingsGoal(
            name="Trip",
            account="fixture-account",
            target_amount=Money(500),
            start_date=date(2026, 9, 1),
            target_date=date(2027, 6, 1),
        )
        with db.transaction("Goal") as txn:
            db.add_savings_goal(goal, txn)
        assert db.get_savings_goal(goal.handle).serialize() == goal.serialize()
        assert [
            row[0]
            for row in db._require().execute(
                "SELECT version FROM schema_migration ORDER BY version"
            )
        ] == [7, 8, 9, 10, 11]
        assert db.integrity_problems() == []
    finally:
        db.close()
    assert (tmp_path / "schema-9.breadsched.pre-migration-v9.bak").exists()


def test_cli_adds_funds_lists_and_closes_goals(tmp_path, capsys):
    import json

    from breadsched.cli.main import main

    path = str(tmp_path / "goals.breadsched")
    assert main(["sample", path, "--as-of", "2026-09-15"]) == 0
    capsys.readouterr()
    assert main(["accounts", path, "--json"]) == 0
    accounts = json.loads(capsys.readouterr().out)
    bank = next(item for item in accounts if item.get("type", item.get("atype")) == "BANK")
    add = ["goals", path, "--add", "Vacation", "--account", bank["handle"], "--target", "1200"]
    assert main([*add, "--by", "2027-06-30", "--start", "2026-07-01"]) == 0
    capsys.readouterr()
    assert (
        main(["goals", path, "--allocate", "Vacation", "--amount", "100", "--on", "2026-08-01"])
        == 0
    )
    capsys.readouterr()
    assert main(["goals", path, "--as-of", "2026-09-15", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    [goal] = report["goals"]
    assert goal["name"] == "Vacation" and Money(goal["allocated"]) == Money(100)
    assert Money(goal["set_aside"]) + Money(goal["remaining"]) == Money(1200)
    assert Money(report["held"]) == Money(goal["set_aside"])
    assert main(["goals", path, "--as-of", "2026-09-15"]) == 0
    assert "Set aside for goals" in capsys.readouterr().out
    assert main(["dashboard", path, "--as-of", "2026-09-15"]) == 0
    assert "Set aside for goals" in capsys.readouterr().out
    assert main(["goals", path, "--close", "Vacation", "--on", "2026-09-16"]) == 0
    capsys.readouterr()
    assert main(["goals", path, "--as-of", "2026-09-20"]) == 0
    assert "No savings goals." in capsys.readouterr().out
    assert main(["goals", path, "--allocate", "Nope", "--amount", "5"]) != 0


def test_dashboard_printout_shows_what_goals_set_aside(db, book):
    from breadsched.gen.engine import dashboard
    from breadsched.plugins.export.html_report import dashboard_report

    _monthly_pay(db, book, last_posted=date(2026, 6, 1))
    _goal(db, book)
    html = dashboard_report(dashboard.build(db, as_of=date(2026, 6, 15)))
    assert "Set aside for goals" in html and "600.00" in html
    assert "<h2>Savings goals</h2>" in html and "New roof" in html and "saving" in html


def test_cli_migrate_brings_an_older_book_to_the_current_schema(tmp_path, capsys):
    import json
    import sqlite3
    from pathlib import Path

    from breadsched.cli.main import main

    path = tmp_path / "old.breadsched"
    sql = (Path(__file__).parent / "fixtures" / "native" / "schema-9.sql").read_text()
    with sqlite3.connect(path) as raw:
        raw.executescript(sql)
    # A read-only command cannot migrate, and says how to.
    assert main(["verify", str(path)]) != 0
    assert main(["accounts", str(path)]) != 0
    assert "breadsched migrate" in capsys.readouterr().err
    assert main(["migrate", str(path), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "schema_before": 9,
        "schema": 11,
        "migrated": True,
        "backup": f"{path}.pre-migration-v9.bak",
    }
    assert Path(result["backup"]).exists()
    assert main(["verify", str(path)]) == 0
    capsys.readouterr()
    assert main(["migrate", str(path)]) == 0
    assert "nothing to migrate" in capsys.readouterr().out


def _projected(db, book, **overrides):
    from breadsched.gen.engine import projection
    from breadsched.gen.lib import GoalOverride, Scenario

    _monthly_pay(db, book, last_posted=date(2026, 6, 1))
    goal = _goal(db, book)
    scenario = Scenario(name="Next year", start=date(2026, 7, 1), years=1)
    if overrides:
        scenario.goal_overrides[goal.handle] = GoalOverride(**overrides)
    return goal, projection.project(db, scenario)


def test_projection_sets_aside_a_share_of_each_projected_income(db, book):
    goal, result = _projected(db, book)
    # 600 set aside by June 30; July to December income spreads the other 600.
    assert [row.goals_set_aside for row in result.rows[:6]] == [
        Money(700),
        Money(800),
        Money(900),
        Money(1000),
        Money(1100),
        Money(1200),
    ]
    assert all(row.goals_set_aside == Money(1200) for row in result.rows[6:])
    assert all(row.goals_held == row.goals_set_aside for row in result.rows)
    assert result.rows[0].cash_after_goals == result.rows[0].cash_close - Money(700)
    [milestone] = result.goal_milestones
    assert (milestone.goal.handle, milestone.month_index) == (goal.handle, 5)
    assert (milestone.target, milestone.set_aside) == (Money(1200), Money(1200))
    assert milestone.cash_close == result.rows[5].cash_close
    assert milestone.covered == (result.rows[5].cash_close >= Money(1200))
    assert not milestone.overridden
    data = result.rows[0].as_dict()
    assert (data["goals_set_aside"], data["goals_held"]) == (Money(700), Money(700))


def test_a_scenario_overrides_or_leaves_out_a_pinned_goal(db, book):
    goal, raised = _projected(db, book, target_amount=Money(1800), target_date=date(2027, 3, 31))
    [milestone] = raised.goal_milestones
    assert milestone.overridden and milestone.target == Money(1800)
    assert (milestone.target_date, milestone.month_index) == (date(2027, 3, 31), 8)
    # The larger target applies from the goal's start: 6 of 15 paychecks set aside
    # 720 by June 30, and July's paycheck adds a ninth of the remaining 1,080.
    assert raised.rows[0].goals_set_aside == Money(840)
    assert raised.rows[8].goals_set_aside == Money(1800)
    # The goal itself is unchanged, and other scenarios still carry it.
    assert db.get_savings_goal(goal.handle).target_amount == Money(1200)


def test_a_scenario_can_leave_a_goal_out(db, book):
    _goal, result = _projected(db, book, excluded=True)
    assert result.goal_milestones == []
    assert all(row.goals_set_aside == Money(0) for row in result.rows)


def test_goal_overrides_survive_saving_the_scenario(db, book):
    from breadsched.gen.lib import GoalOverride, Scenario

    scenario = Scenario(name="Saved", start=date(2026, 7, 1))
    scenario.goal_overrides["goal"] = GoalOverride(Money(5), date(2027, 1, 1), excluded=False)
    reloaded = Scenario.from_dict(scenario.serialize())
    assert reloaded.goal_overrides == scenario.goal_overrides


def test_service_overrides_a_goal_per_scenario_and_cleans_up(db, book):
    from breadsched.gen.lib import Scenario
    from breadsched.gen.services import (
        SetGoalOverride,
        delete_savings_goal,
        save_savings_goal,
        set_goal_override,
    )

    goal = save_savings_goal(db, _request(book)).value
    scenario = Scenario(name="Lean", start=date(2026, 7, 1))
    with db.transaction("Scenario") as txn:
        db.add_scenario(scenario, txn)
    changed = set_goal_override(
        db, SetGoalOverride(scenario.handle, goal.handle, target_amount=Money(900))
    )
    assert changed.ok and changed.value.goal_overrides[goal.handle].target_amount == Money(900)
    assert db.get_scenario(scenario.handle).goal_overrides[goal.handle].target_amount == Money(900)
    before = db.get_scenario(scenario.handle).serialize()
    for request, code in (
        (SetGoalOverride("missing", goal.handle), "scenario.not_found"),
        (SetGoalOverride(scenario.handle, "missing"), "savings_goal.not_found"),
        (SetGoalOverride(scenario.handle, goal.handle, Money(0)), "savings_goal.target.invalid"),
        (
            SetGoalOverride(scenario.handle, goal.handle, target_date=date(2025, 1, 1)),
            "savings_goal.dates.invalid",
        ),
    ):
        rejected = set_goal_override(db, request)
        assert rejected.errors[0].code == code
        assert db.get_scenario(scenario.handle).serialize() == before
    cleared = set_goal_override(db, SetGoalOverride(scenario.handle, goal.handle))
    assert cleared.ok and cleared.value.goal_overrides == {}
    assert db.get_scenario(scenario.handle).goal_overrides == {}
    set_goal_override(db, SetGoalOverride(scenario.handle, goal.handle, excluded=True))
    assert delete_savings_goal(db, goal.handle).ok
    assert db.get_scenario(scenario.handle).goal_overrides == {}


def test_transfers_into_a_goal_account_are_not_projected_as_expenses(db, book):
    from breadsched.gen.engine import projection
    from breadsched.gen.lib import Scenario

    _monthly_pay(db, book, last_posted=date(2026, 6, 1))
    _goal(db, book)
    move = ScheduledTransaction(
        name="Save for the roof",
        recurrence=Recurrence(PeriodType.MONTH, interval=1, start=date(2026, 7, 2)),
        splits=[
            ScheduledSplit(book.savings, Money(100)),
            ScheduledSplit(book.checking, Money(-100)),
        ],
    )
    with db.transaction("Transfer") as txn:
        db.add_scheduled(move, txn)
    result = projection.project(db, Scenario(name="Base", start=date(2026, 7, 1), years=1))
    assert all(row.expense == Money(0) for row in result.rows)
    assert result.rows[0].goals_set_aside == Money(700)


def test_cli_overrides_a_goal_in_a_scenario_and_projects_its_milestone(tmp_path, capsys):
    import json

    from breadsched.cli.main import main

    path = str(tmp_path / "plan.breadsched")
    assert main(["sample", path, "--as-of", "2026-09-15"]) == 0
    capsys.readouterr()
    assert main(["accounts", path, "--json"]) == 0
    accounts = json.loads(capsys.readouterr().out)
    bank = next(item for item in accounts if item.get("type", item.get("atype")) == "BANK")
    assert (
        main(
            [
                "goals",
                path,
                "--add",
                "Car",
                "--account",
                bank["handle"],
                "--target",
                "6000",
                "--by",
                "2027-06-30",
                "--start",
                "2026-09-01",
            ]
        )
        == 0
    )
    assert main(["scenario", path, "save", "--name", "Lean", "--start", "2026-09-01"]) == 0
    capsys.readouterr()
    name = "Lean"
    assert (
        main(["goals", path, "--override", "Car", "--scenario", name, "--target", "8000", "--json"])
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["override"]["excluded"] is False
    assert main(["project", path, "--scenario", name, "--years", "1", "--json"]) == 0
    projected = json.loads(capsys.readouterr().out)
    [milestone] = projected["goal_milestones"]
    assert milestone["name"] == "Car" and milestone["overridden"] is True
    assert Money(milestone["target"]) == Money(8000)
    assert "goals_set_aside" in projected["rows"][0]
    assert main(["project", path, "--scenario", name, "--years", "1"]) == 0
    assert "Goal Car: 8,000.00 by 2027-06-30 (changed in this scenario)" in (
        capsys.readouterr().out
    )
    assert main(["goals", path, "--override", "Car", "--scenario", name]) == 0
    assert "follows the goal unchanged" in capsys.readouterr().out


def test_projection_outputs_carry_goal_milestones(db, book, tmp_path):
    from breadsched.plugins.export.csv_export import export_projection
    from breadsched.plugins.export.html_report import projection_report
    from breadsched.presentation import projection_goal_notes

    _goal, result = _projected(db, book)
    notes = projection_goal_notes(result)
    assert notes[-1].startswith("Goal New roof: 1,200.00 by 2026-12-31; projected cash of")
    html = projection_report(result)
    assert "<h2>Savings goals</h2>" in html and "New roof" in html
    path = tmp_path / "projection.csv"
    export_projection(result, path)
    header, first, *_rest = path.read_text(encoding="utf-8").splitlines()
    assert header.endswith("net_worth,goals_set_aside,cash_after_goals,completeness")
    assert first.split(",")[-3] == "700.0000" or first.split(",")[-3].startswith("700")
    assert first.split(",")[-1] == "complete"


def test_plan_lists_goals_reaching_their_target_in_range(db, book):
    from breadsched.gen.services import PlanQuery, query_plan
    from breadsched.plugins.export.html_report import plan_report

    _monthly_pay(db, book, last_posted=date(2026, 6, 1))
    _goal(db, book)
    result = query_plan(
        db, PlanQuery(start=date(2026, 1, 1), end=date(2026, 12, 31), today=date(2026, 6, 15))
    ).value
    [milestone] = result.goal_milestones
    assert (milestone.target, milestone.set_aside, milestone.remaining) == (
        Money(1200),
        Money(600),
        Money(600),
    )
    outside = query_plan(
        db, PlanQuery(start=date(2026, 1, 1), end=date(2026, 6, 30), today=date(2026, 6, 15))
    ).value
    assert outside.goal_milestones == ()
    html = plan_report(
        result.report, result.measure, scenario_name="Base", goal_milestones=result.goal_milestones
    )
    assert "Savings goals reaching their target" in html
    assert "600.00 set aside so far" in html


def _brokerage_goals(db, book):
    from breadsched.gen.lib import Split

    opening = Money(5000)

    _monthly_pay(db, book, last_posted=date(2026, 6, 1))
    with db.transaction("Opening brokerage") as txn:
        db.add_transaction(
            Transaction(
                post_date=date(2026, 1, 2),
                description="Opening",
                splits=[Split(book.brokerage, opening), Split(book.opening, -opening)],
            ),
            txn,
        )
    goals = []
    for name, account in (
        ("New roof", book.brokerage),
        ("Car", book.brokerage),
        ("Holiday", book.savings),
    ):
        goal = SavingsGoal(
            name=name,
            account=account,
            target_amount=Money(1200),
            start_date=date(2026, 1, 1),
            target_date=date(2026, 12, 31),
        )
        with db.transaction("Goal") as txn:
            db.add_savings_goal(goal, txn)
        goals.append(goal)
    return tuple(goals)


def test_a_goal_in_a_non_cash_account_is_compared_with_its_projected_balance(db, book):
    from breadsched.gen.engine import projection
    from breadsched.gen.lib import Scenario
    from breadsched.presentation import goal_milestone_text

    roof, car, _cash = _brokerage_goals(db, book)
    result = projection.project(db, Scenario(name="Next year", start=date(2026, 7, 1), years=1))

    milestones = {item.goal.handle: item for item in result.goal_milestones}
    item = milestones[roof.handle]
    assert not item.cash_account and item.month_index == 5
    assert item.account_name == "Assets:Brokerage"
    assert item.account_close == result.rows[5].ledger.closing_holdings[book.brokerage]
    # Both goals in the brokerage account count against its balance; cash goals do not.
    assert item.account_held == Money(2400) == milestones[car.handle].account_held
    assert item.covered is True
    assert item.cash_close == result.rows[5].cash_close
    assert result.rows[5].goals_held == Money(1200)
    text = goal_milestone_text(item)
    assert "Assets:Brokerage is projected at" in text and "which covers the 2,400.00" in text
    data = item.as_dict()
    assert (data["account_close"], data["account_held"], data["covered"]) == (
        item.account_close,
        Money(2400),
        True,
    )


def test_a_short_non_cash_account_does_not_cover_its_goals(db, book):
    from breadsched.gen.engine import projection
    from breadsched.gen.lib import Scenario
    from breadsched.presentation import goal_milestone_text

    roof, _car, _cash = _brokerage_goals(db, book)
    scenario = Scenario(name="Lean", start=date(2026, 7, 1), years=1)
    scenario.opening_overrides[book.brokerage] = Money(100)

    [item] = [
        m for m in projection.project(db, scenario).goal_milestones if m.goal.handle == roof.handle
    ]

    assert item.account_close is not None and item.account_close < Money(2400)
    assert item.covered is False
    assert "which does not cover the 2,400.00 set aside there" in goal_milestone_text(item)


def test_a_goal_in_an_account_left_out_of_the_projection_is_not_compared(db, book):
    from breadsched.gen.engine import projection
    from breadsched.gen.lib import Scenario
    from breadsched.presentation import goal_milestone_text

    roof, _car, _cash = _brokerage_goals(db, book)
    account = db.get_account(book.brokerage)
    account.exclude_from_projection = True
    with db.transaction("Leave out") as txn:
        db.commit_account(account, txn)

    result = projection.project(db, Scenario(name="Next year", start=date(2026, 7, 1), years=1))

    [item] = [m for m in result.goal_milestones if m.goal.handle == roof.handle]
    assert (item.account_close, item.account_held, item.covered) == (None, None, None)
    assert goal_milestone_text(item).endswith(
        "held in Assets:Brokerage, which this projection does not project, so it is not compared"
    )
