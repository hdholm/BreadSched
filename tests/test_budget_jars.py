"""Budget jars: scheduled outflows and goals filled from income and drawn by actuals."""

from __future__ import annotations

from datetime import date

from breadsched.gen.engine.activity import ReportingPeriod
from breadsched.gen.engine.budget_jars import JarKind, budget_jars
from breadsched.gen.lib import (
    Money,
    PeriodType,
    PlanningResolution,
    Recurrence,
    SavingsGoal,
    ScheduledSplit,
    ScheduledTransaction,
    Split,
    Transaction,
)

TODAY = date(2026, 7, 10)


def _schedule(db, name, recurrence, splits, *, placeholder=False, last_posted=None):
    sched = ScheduledTransaction(name=name, recurrence=recurrence, splits=splits)
    sched.placeholder = placeholder
    sched.last_posted = last_posted
    with db.transaction(name) as txn:
        db.add_scheduled(sched, txn)
    return sched


def _pay(db, book, last_posted=date(2026, 7, 1)):
    """1,000 on the 1st and the 15th."""
    return _schedule(
        db,
        "Pay",
        Recurrence(PeriodType.SEMI_MONTH, interval=1, start=date(2026, 1, 1)),
        [ScheduledSplit(book.checking, Money(1000)), ScheduledSplit(book.salary, Money(-1000))],
        last_posted=last_posted,
    )


def _groceries(db, book):
    """An estimate: about 600 of groceries on the 20th of each month, from June."""
    return _schedule(
        db,
        "Groceries",
        Recurrence(PeriodType.MONTH, interval=1, start=date(2026, 6, 20)),
        [ScheduledSplit(book.groceries, Money(600)), ScheduledSplit(book.checking, Money(-600))],
        placeholder=True,
    )


def _actual(db, book, sched, planned, when, amount):
    txn = Transaction(post_date=when, description="Market")
    txn.splits = [Split(book.groceries, Money(amount)), Split(book.checking, -Money(amount))]
    txn.planned_occurrence = sched.occurrence_key(planned)
    txn.planned_for = planned
    txn.planning_resolution = PlanningResolution.MATCHED
    with db.transaction("Market") as handle:
        db.add_transaction(txn, handle)
    return txn


def test_an_estimate_fills_from_each_paycheck_and_is_drawn_by_its_actual(db, book):
    _pay(db, book)
    groceries = _groceries(db, book)
    market = _actual(db, book, groceries, date(2026, 6, 20), date(2026, 6, 21), "550")

    report = budget_jars(db, date(2026, 6, 1), date(2026, 7, 31), today=TODAY)
    [bundle] = report.accounts
    [jar] = bundle.jars
    assert (jar.kind, jar.name, jar.account_name) == (
        JarKind.ESTIMATE,
        "Groceries",
        "Expenses:Groceries",
    )
    june, july = jar.periods
    # The June 20 occurrence fills 300 from each of the June 1 and 15 paychecks.
    assert [(e.when, e.amount) for e in jar.events if e.kind == "fill"][:2] == [
        (date(2026, 6, 1), Money(300)),
        (date(2026, 6, 15), Money(300)),
    ]
    assert (june.filled, june.planned, june.actual, june.level) == (
        Money(600),
        Money(600),
        Money(550),
        Money(50),
    )
    assert june.variance == Money(-50)
    # July's occurrence is unmatched: it is planned and filled but nothing is drawn.
    assert (july.filled, july.planned, july.actual, july.level) == (
        Money(600),
        Money(600),
        Money(0),
        Money(650),
    )
    [actual] = [event for event in jar.events if event.kind == "actual"]
    assert (actual.when, actual.transaction) == (date(2026, 6, 21), market.handle)
    assert bundle.periods == jar.periods
    [total] = report.totals
    assert total.periods[0].actual == Money(550)


def test_missed_income_fills_nothing_and_no_income_fills_at_the_cycle_start(db, book):
    # Pay posted only through June 1: June 15 never arrived.
    _pay(db, book, last_posted=date(2026, 6, 1))
    groceries = _groceries(db, book)
    report = budget_jars(db, date(2026, 6, 1), date(2026, 6, 30), today=TODAY)
    [jar] = report.jars
    june = groceries.occurrence_key(date(2026, 6, 20))
    fills = [(e.when, e.amount) for e in jar.events if e.kind == "fill" and e.occurrence == june]
    # Only the June 1 paycheck counts, so it sets aside the whole 600.
    assert fills == [(date(2026, 6, 1), Money(600))]
    # July's occurrence begins its cycle on June 20 and fills from July 15 (expected).
    july = groceries.occurrence_key(date(2026, 7, 20))
    assert [
        (e.when, e.amount) for e in jar.events if e.occurrence == july and e.kind == "fill"
    ] == [(date(2026, 7, 15), Money(600))]


def test_without_income_the_whole_amount_is_set_aside_when_the_cycle_starts(db, book):
    _groceries(db, book)
    report = budget_jars(db, date(2026, 6, 1), date(2026, 6, 30), today=TODAY)
    [jar] = report.jars
    # June's is the first occurrence: its cycle is the month before it (from May 21).
    # July's fills on June 20.
    assert [(e.when, e.amount) for e in jar.events if e.kind == "fill"] == [
        (date(2026, 5, 21), Money(600)),
        (date(2026, 6, 20), Money(600)),
    ]
    # Filled before the range: it is the opening level.
    assert jar.opening == Money(600)
    [june] = jar.periods
    assert (june.filled, june.planned, june.level) == (Money(600), Money(600), Money(1200))


def test_a_quarterly_bill_due_after_the_range_fills_inside_it(db, book):
    _pay(db, book)
    water = _schedule(
        db,
        "Water",
        Recurrence(PeriodType.MONTH, interval=3, start=date(2026, 3, 31)),
        [ScheduledSplit(book.utilities, Money(300)), ScheduledSplit(book.checking, Money(-300))],
    )
    # The March and June bills were paid exactly, so nothing is carried in.
    for due in (date(2026, 3, 31), date(2026, 6, 30)):
        paid = Transaction(post_date=due, description="Water")
        paid.splits = [Split(book.utilities, Money(300)), Split(book.checking, Money(-300))]
        paid.planned_occurrence = water.occurrence_key(due)
        paid.planning_resolution = PlanningResolution.MATCHED
        with db.transaction("Water") as handle:
            db.add_transaction(paid, handle)
    report = budget_jars(db, date(2026, 7, 1), date(2026, 7, 31), today=date(2026, 6, 1))
    [jar] = report.jars
    assert jar.kind == JarKind.BILL
    [july] = jar.periods
    # Due September 30; its quarter's six paychecks each set aside 50.
    assert (july.filled, july.planned, july.level) == (Money(100), Money(0), Money(100))


def test_quarters_only_group_dated_events(db, book):
    _pay(db, book)
    _groceries(db, book)
    monthly = budget_jars(db, date(2026, 7, 1), date(2026, 9, 30), today=TODAY)
    quarterly = budget_jars(
        db, date(2026, 7, 1), date(2026, 9, 30), period=ReportingPeriod.QUARTER, today=TODAY
    )
    [quarter] = quarterly.jars[0].periods
    months = monthly.jars[0].periods
    assert quarter.label != months[0].label
    assert quarter.filled == sum((m.filled for m in months), Money(0))
    assert quarter.planned == Money(1800) and quarter.level == months[-1].level


def test_a_goal_fills_on_income_dates_and_is_drawn_when_closed(db, book):
    _schedule(
        db,
        "Monthly pay",
        Recurrence(PeriodType.MONTH, interval=1, start=date(2026, 1, 1)),
        [ScheduledSplit(book.checking, Money(1000)), ScheduledSplit(book.salary, Money(-1000))],
        last_posted=date(2026, 12, 1),
    )
    goal = SavingsGoal(
        name="New roof",
        account=book.savings,
        target_amount=Money(1200),
        start_date=date(2026, 1, 1),
        target_date=date(2026, 12, 31),
        closed_on=date(2026, 9, 15),
    )
    with db.transaction("Goal") as txn:
        db.add_savings_goal(goal, txn)
    report = budget_jars(db, date(2026, 7, 1), date(2026, 9, 30), today=date(2026, 10, 1))
    [jar] = [jar for jar in report.jars if jar.kind == JarKind.GOAL]
    assert jar.opening == Money(600)
    july, august, september = jar.periods
    assert [(e.when, e.amount) for e in jar.events if e.kind == "fill"] == [
        (date(2026, 7, 1), Money(100)),
        (date(2026, 8, 1), Money(100)),
        (date(2026, 9, 1), Money(100)),
    ]
    assert (july.level, august.level) == (Money(700), Money(800))
    assert (september.filled, september.actual, september.level) == (
        Money(100),
        Money(900),
        Money(0),
    )
    assert jar.account_name == "Assets:Savings"


def test_amounts_in_different_currencies_are_separate_bundles(db, book):
    from breadsched.gen.lib import Commodity

    eur = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    with db.transaction("EUR") as txn:
        db.add_commodity(eur, txn)
    _pay(db, book)
    _groceries(db, book)
    abroad = _schedule(
        db,
        "Groceries abroad",
        Recurrence(PeriodType.MONTH, interval=1, start=date(2026, 1, 20)),
        [ScheduledSplit(book.groceries, Money(100)), ScheduledSplit(book.checking, Money(-100))],
        placeholder=True,
    )
    abroad.currency = eur.handle
    with db.transaction("Currency") as txn:
        db.commit_scheduled(abroad, txn)
    report = budget_jars(db, date(2026, 6, 1), date(2026, 6, 30), today=TODAY)
    assert len(report.accounts) == 2 and len(report.totals) == 2
    assert {bundle.currency for bundle in report.accounts} >= {eur.handle}


def test_the_user_guide_worked_example(db, book):
    """The "Jars: how the pieces fit together" example, through every view it names."""
    from breadsched.gen.engine import dashboard, planning
    from breadsched.gen.services import ReviewOccurrence, match_review
    from breadsched.gen.services.plan import PlanQuery, query_plan

    _pay(db, book)
    groceries = _groceries(db, book)

    # Filling: half of the cycle's income each, 300 from June 1 and 300 from June 15.
    report = budget_jars(db, date(2026, 6, 1), date(2026, 7, 31), today=TODAY)
    june_key = groceries.occurrence_key(date(2026, 6, 20))
    [jar] = report.jars
    assert [
        (e.when, e.amount) for e in jar.events if e.kind == "fill" and e.occurrence == june_key
    ] == [
        (date(2026, 6, 1), Money(300)),
        (date(2026, 6, 15), Money(300)),
    ]

    # Dashboard: the estimate is not a bill, so nothing is held for it.
    board = dashboard.build(db, as_of=date(2026, 6, 10))
    assert not [
        bill for bill in board.bills if bill.schedule is groceries or bill.name == "Groceries"
    ]

    # Actual: Review offers the June 20 occurrence for a 550 purchase on June 21.
    market = Transaction(post_date=date(2026, 6, 21), description="Market")
    market.splits = [Split(book.groceries, Money(550)), Split(book.checking, Money(-550))]
    with db.transaction("Market") as handle:
        db.add_transaction(market, handle)
    offered = [candidate.event.key for candidate in planning.match_candidates(db, market)]
    assert june_key in offered
    assert match_review(db, ReviewOccurrence(market.handle, june_key)).value is not None

    # Plan: June's Groceries row, 600 planned and 550 actual.
    plan = query_plan(
        db,
        PlanQuery(
            start=date(2026, 6, 1),
            end=date(2026, 6, 30),
            period=ReportingPeriod.MONTH,
            today=TODAY,
        ),
    ).value
    [row] = [item for item in plan.report.categories if item.account == book.groceries]
    assert (row.planned[0], row.actual[0]) == (Money(600), Money(550))

    # Budget jars: June filled 600, planned 600, actual 550, level 50; July 650.
    [jar] = budget_jars(db, date(2026, 6, 1), date(2026, 7, 31), today=TODAY).jars
    june, july = jar.periods
    assert (june.filled, june.planned, june.actual, june.level) == (
        Money(600),
        Money(600),
        Money(550),
        Money(50),
    )
    assert (july.planned, july.actual, july.level) == (Money(600), Money(0), Money(650))


def test_the_layout_prints_totals_and_each_account(db, book):
    from breadsched.gen.engine.budget_jars import currency_labels
    from breadsched.plugins.export.report_layout import budget_jars_layout

    _pay(db, book)
    _groceries(db, book)
    report = budget_jars(db, date(2026, 6, 1), date(2026, 7, 31), today=TODAY)
    document = budget_jars_layout(report, currency_labels(db, report))
    assert (document.title, document.kind) == ("Budget jars", "budget-jars")
    assert document.subtitle == "2026-06-01 to 2026-07-31 by month"
    text = document.text()
    for expected in ("All jars", "Expenses:Groceries", "Groceries (estimate)", "1,200.00"):
        assert expected in text, expected


def test_the_command_line_lists_jars(tmp_path, capsys):
    import json

    from breadsched.cli.main import main as cli
    from breadsched.gen.db.sqlite import DbSQLite
    from breadsched.gen.lib import Account, AccountType

    path = tmp_path / "jars.breadsched"
    assert cli(["init", str(path)]) == 0
    db = DbSQLite()
    db.load(str(path))
    try:
        tops = {a.name: a for a in db.iter_accounts()}
        checking = Account(name="Checking", atype=AccountType.BANK, parent=tops["Assets"].handle)
        income = Account(name="Salary", atype=AccountType.INCOME, parent=tops["Income"].handle)
        expense = Account(
            name="Groceries", atype=AccountType.EXPENSE, parent=tops["Expenses"].handle
        )
        with db.transaction("Accounts") as txn:
            for account in (checking, income, expense):
                db.add_account(account, txn)
        _schedule(
            db,
            "Pay",
            Recurrence(PeriodType.SEMI_MONTH, interval=1, start=date(2026, 1, 1)),
            [
                ScheduledSplit(checking.handle, Money(1000)),
                ScheduledSplit(income.handle, Money(-1000)),
            ],
            last_posted=date(2026, 7, 1),
        )
        _schedule(
            db,
            "Groceries",
            Recurrence(PeriodType.MONTH, interval=1, start=date(2026, 1, 20)),
            [
                ScheduledSplit(expense.handle, Money(600)),
                ScheduledSplit(checking.handle, Money(-600)),
            ],
            placeholder=True,
        )
        expense_name = db.full_name(expense)
    finally:
        db.close()
    capsys.readouterr()
    args = [
        "jars",
        str(path),
        "--start",
        "2026-06-01",
        "--end",
        "2026-06-30",
        "--as-of",
        "2026-07-10",
    ]
    assert cli([*args, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    [bundle] = data["accounts"]
    assert bundle["name"] == expense_name
    [june] = bundle["periods"]
    assert (june["filled"], june["planned"], june["actual"]) == ("600.00", "600.00", "0.00")
    assert bundle["jars"][0]["kind"] == "estimate"
    assert cli(args) == 0
    text = capsys.readouterr().out
    assert "Groceries (estimate)" in text and expense_name in text
    assert cli(["jars", str(path), "--start", "2026-07-01", "--end", "2026-06-01"]) == 2


def test_jar_charts_pair_planned_with_actual_and_level_with_target(db, book):
    from breadsched.gen.engine.budget_jars import currency_labels, jar_charts
    from breadsched.gen.engine.chart_model import BARS

    _pay(db, book)
    groceries = _groceries(db, book)
    _actual(db, book, groceries, date(2026, 6, 20), date(2026, 6, 21), "550")
    report = budget_jars(db, date(2026, 6, 1), date(2026, 7, 31), today=TODAY)
    draws, levels = jar_charts(report, currency_labels(db, report))
    assert (draws.kind, draws.categories) == (BARS, tuple(label for *_x, label in report.labels))
    planned, actual = draws.series
    assert (planned.name, planned.slot, actual.name, actual.slot) == ("Planned", 1, "Actual", 2)
    assert planned.values == (Money(600), Money(600))
    assert actual.values == (Money(550), Money(0))
    # The chart's values are the report's own: the table beside it repeats them.
    assert draws.rows()[0] == (report.labels[0][2], (Money(600), Money(550)))
    level, target = levels.series
    assert levels.categories == ("Groceries",)
    # 650 left at the end of July, filling toward August's 600.
    assert (level.values, target.values, level.slot, target.slot) == (
        (Money(650),),
        (Money(600),),
        3,
        4,
    )
    data = draws.as_dict()
    assert data["series"][0]["values"] == [Money(600), Money(600)]


def test_chart_geometry_scales_from_zero_with_capped_columns():
    from breadsched.gen.engine.chart_model import BARS, ChartModel, ChartSeries
    from breadsched.presentation import chart_bar_layout, chart_nice_ticks

    assert chart_nice_ticks(0, 650) == [0, 200, 400, 600, 800]
    assert chart_nice_ticks(-120, 650)[0] < 0 and 0 in chart_nice_ticks(-120, 650)
    model = ChartModel(
        "k",
        "Groceries",
        BARS,
        ("Jun", "Jul"),
        (
            ChartSeries("planned", "Planned", (Money(600), Money(600)), 1),
            ChartSeries("actual", "Actual", (Money(550), Money(-50)), 2),
        ),
        "USD",
    )
    layout = chart_bar_layout(model, 0, 0, 400, 200)
    assert len(layout.bars) == 4 and all(bar.width <= 24 for bar in layout.bars)
    negative = next(bar for bar in layout.bars if bar.negative)
    assert negative.y == layout.baseline
    planned, actual = (bar for bar in layout.bars if bar.category == 0)
    assert actual.x - (planned.x + planned.width) == 2
    assert planned.height > actual.height


def test_charts_print_beside_their_tables(db, book):
    from breadsched.gen.engine.budget_jars import currency_labels
    from breadsched.plugins.export.html_report import model_chart_svg
    from breadsched.plugins.export.report_layout import ModelChart, Table, budget_jars_layout

    _pay(db, book)
    _groceries(db, book)
    report = budget_jars(db, date(2026, 6, 1), date(2026, 7, 31), today=TODAY)
    document = budget_jars_layout(report, currency_labels(db, report))
    blocks = document.sections[0].blocks
    charts = [index for index, block in enumerate(blocks) if isinstance(block, ModelChart)]
    assert len(charts) == 2
    assert all(isinstance(blocks[index + 1], Table) for index in charts)
    svg = model_chart_svg(blocks[charts[0]].model)
    # Nothing was spent, so only the two planned columns are drawn; the legend has both.
    assert svg.count("<path") == 2 and "#2a78d6" in svg and "#eb6834" in svg
    assert "<title>" in svg and "Planned: 600.00" in svg
    assert "planned and actual" in document.text()


def test_earlier_occurrences_are_carried_in(db, book):
    """A level accounts for every earlier fill and draw, not only the range's."""
    _pay(db, book)
    groceries = _schedule(
        db,
        "Groceries",
        Recurrence(PeriodType.MONTH, interval=1, start=date(2026, 3, 20)),
        [ScheduledSplit(book.groceries, Money(600)), ScheduledSplit(book.checking, Money(-600))],
        placeholder=True,
    )
    # March under plan by 100, April over plan by 50, May never matched.
    _actual(db, book, groceries, date(2026, 3, 20), date(2026, 3, 21), "500")
    _actual(db, book, groceries, date(2026, 4, 20), date(2026, 4, 20), "650")
    report = budget_jars(db, date(2026, 6, 1), date(2026, 6, 30), today=TODAY)
    [jar] = report.jars
    # 100 left in March, 50 overspent in April, and May's 600 still set aside; June's
    # own fills are inside the range.
    assert jar.opening == Money(100) - Money(50) + Money(600)
    [june] = jar.periods
    assert (june.filled, june.planned, june.actual) == (Money(600), Money(600), Money(0))
    assert june.level == jar.opening + Money(600)
    # The same months reported from March show the same end.
    from_march = budget_jars(db, date(2026, 3, 1), date(2026, 6, 30), today=TODAY)
    assert from_march.jars[0].periods[-1].level == june.level


def _spend(db, book, when, amount, account=None):
    """An ordinary purchase nobody matched to a planned occurrence."""
    txn = Transaction(post_date=when, description="Market")
    txn.splits = [
        Split(account or book.groceries, Money(amount)),
        Split(book.checking, -Money(amount)),
    ]
    with db.transaction("Market") as handle:
        db.add_transaction(txn, handle)
    return txn


def test_unmatched_spending_draws_the_accounts_jar_like_the_plan(db, book):
    # #312: real purchases in an estimate's account are actuals even when nobody
    # matched them in Review, as the Plan's category row already counts them.
    from breadsched.gen.engine import category_report

    _pay(db, book)
    groceries = _groceries(db, book)
    _actual(db, book, groceries, date(2026, 6, 20), date(2026, 6, 21), "550")
    _spend(db, book, date(2026, 6, 25), "40")
    _spend(db, book, date(2026, 7, 3), "120")
    _spend(db, book, date(2026, 7, 6), "-20")  # a refund puts money back
    _spend(db, book, date(2026, 5, 2), "75")  # before the jar's first cycle began

    report = budget_jars(db, date(2026, 6, 1), date(2026, 7, 31), today=TODAY)
    [bundle] = report.accounts
    [jar] = bundle.jars
    june, july = jar.periods
    assert (june.actual, june.level) == (Money(590), Money(10))
    assert (july.actual, july.level) == (Money(100), Money(510))
    unmatched = [e for e in jar.events if e.kind == "actual" and not e.occurrence]
    assert [(e.when, e.amount) for e in unmatched] == [
        (date(2026, 6, 25), Money(40)),
        (date(2026, 7, 3), Money(120)),
        (date(2026, 7, 6), Money(-20)),
    ]
    plan = category_report.build_category_report(
        db, date(2026, 6, 1), date(2026, 7, 31), as_of=TODAY
    )
    row = next(row for row in plan.categories if row.account == book.groceries)
    assert [period.actual for period in bundle.periods] == row.actual


def test_unmatched_spending_with_several_jars_has_its_own_line(db, book):
    _pay(db, book)
    _groceries(db, book)
    _schedule(
        db,
        "Market box",
        Recurrence(PeriodType.MONTH, interval=1, start=date(2026, 6, 5)),
        [ScheduledSplit(book.groceries, Money(80)), ScheduledSplit(book.checking, Money(-80))],
        placeholder=True,
    )
    _spend(db, book, date(2026, 6, 25), "40")

    report = budget_jars(db, date(2026, 6, 1), date(2026, 6, 30), today=TODAY)
    [bundle] = report.accounts
    assert [jar.kind for jar in bundle.jars] == [
        JarKind.ESTIMATE,
        JarKind.ESTIMATE,
        JarKind.UNMATCHED,
    ]
    other = bundle.jars[-1]
    assert (other.name, other.periods[0].actual, other.periods[0].level) == (
        "Unmatched spending",
        Money(40),
        Money(-40),
    )
    assert bundle.periods[0].actual == Money(40)
