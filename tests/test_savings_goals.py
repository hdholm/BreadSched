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
        assert db.get_metadata("schema_version") == 10
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
        ] == [7, 8, 9, 10]
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
        "schema": 10,
        "migrated": True,
        "backup": f"{path}.pre-migration-v9.bak",
    }
    assert Path(result["backup"]).exists()
    assert main(["verify", str(path)]) == 0
    capsys.readouterr()
    assert main(["migrate", str(path)]) == 0
    assert "nothing to migrate" in capsys.readouterr().out
