"""Spending charts: the Expense Explorer's own values on the shared chart model.

The lines, the stacked categories, and the shares are drawn from ``SpendingPoint``
values without recomputing them; stacked and share geometry is shared by every
renderer, and each chart prints with a table of its exact values.
"""

from datetime import date

import pytest

from breadsched.gen.engine.chart_model import BARS, LINE, SHARE, STACKED, ChartModel, ChartSeries
from breadsched.gen.lib import (
    Account,
    AccountType,
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)
from breadsched.gen.services.expense_explorer import (
    category_trend_chart,
    query_expense_explorer,
    spending_charts,
)
from breadsched.gen.services.plan import PlanQuery
from breadsched.plugins.export.html_report import expense_explorer_report, model_chart_svg
from breadsched.plugins.export.report_layout import (
    ModelChart,
    Table,
    chart_blocks,
    expense_explorer_layout,
)
from breadsched.presentation import chart_bar_layout, chart_line_layout, chart_tick_label


def _spend(db, book, entries):
    with db.transaction("Spending history") as txn:
        for when, account, amount in entries:
            db.add_transaction(
                Transaction.simple(when, "Spend", account, book.checking, amount), txn
            )


def _explorer(db, **kwargs):
    result = query_expense_explorer(
        db,
        PlanQuery(start=date(2026, 1, 1), end=date(2026, 3, 31), today=date(2026, 2, 10)),
        **kwargs,
    )
    assert result.value is not None
    return result.value


def test_spending_charts_draw_the_explorers_own_values(db, book):
    estimate = ScheduledTransaction(
        name="Monthly groceries",
        recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 5)),
        splits=[
            ScheduledSplit(book.groceries, Money(100)),
            ScheduledSplit(book.checking, Money(-100)),
        ],
    )
    with db.transaction("Estimate") as txn:
        db.add_scheduled(estimate, txn)
    _spend(
        db,
        book,
        (
            (date(2026, 1, 9), book.groceries, "80"),
            (date(2026, 1, 20), book.utilities, "45"),
            (date(2026, 2, 6), book.groceries, "120"),
        ),
    )
    explorer = _explorer(db)
    lines, stacked, share = spending_charts(explorer, currency="USD")
    points = explorer.spending
    labels = tuple(point.label for point in points)

    assert (lines.key, lines.kind, lines.currency) == ("spending", LINE, "USD")
    assert lines.categories == labels
    assert [series.name for series in lines.series] == ["Plan", "Actual"]
    assert lines.series[0].values == tuple(point.planned for point in points)
    assert lines.series[1].values == tuple(point.actual for point in points)
    # The rule marks the as-of date's period (February is partial on Feb 10).
    assert [(marker.index, marker.label) for marker in lines.markers] == [(1, "As of 2026-02-10")]
    assert lines.partial_from is None and not lines.partial_note

    assert (stacked.key, stacked.kind, share.key, share.kind) == (
        "spending_categories",
        STACKED,
        "spending_share",
        SHARE,
    )
    assert stacked.totals == share.totals == tuple(point.actual for point in points)
    # Ranked by actual over the range: groceries (200) before utilities (45).
    names = [series.name for series in stacked.series]
    assert names[:2] == ["Expenses:Groceries", "Expenses:Utilities"]
    assert [series.slot for series in stacked.series] == list(range(1, len(names) + 1))
    for index, point in enumerate(points):
        values = [series.values[index] for series in stacked.series]
        assert sum((value or Money(0) for value in values), Money(0)) == point.actual
    # Shares divide by the reported total: January is 80 of 125.
    groceries = names.index("Expenses:Groceries")
    assert share.share(groceries, 0) == pytest.approx(64.0)
    assert share.share(groceries, 2) is None  # March: no actual, no share

    trend = category_trend_chart(
        next(row for row in explorer.categories if row.account == book.groceries),
        explorer.plan.report.as_of,
        "USD",
    )
    assert (trend.key, trend.kind) == ("category_trend", LINE)
    assert trend.series[0].values == (Money(100), Money(100), Money(100))
    assert trend.series[1].values == (Money(80), Money(120), Money(0))
    assert trend.markers[0].index == 1


def test_categories_past_the_seventh_combine_as_other_and_still_reconcile(db, book):
    with db.transaction("More categories") as txn:
        extra = []
        for number in range(9):
            account = Account(
                name=f"Category {number}", atype=AccountType.EXPENSE, parent=book.expenses
            )
            db.add_account(account, txn)
            extra.append(account.handle)
    _spend(
        db,
        book,
        [(date(2026, 1, 3), handle, str(10 * (number + 1))) for number, handle in enumerate(extra)],
    )
    explorer = _explorer(db)
    _lines, stacked, _share = spending_charts(explorer)
    names = [series.name for series in stacked.series]
    assert len(names) == 8 and names[-1] == "Other"
    assert [series.slot for series in stacked.series] == list(range(1, 9))
    # The seven largest are named, largest first; the two smallest are Other.
    assert names[:7] == [f"Expenses:Category {number}" for number in range(8, 1, -1)]
    assert stacked.series[-1].values[0] == Money(10 + 20)
    january = sum((series.values[0] or Money(0) for series in stacked.series), Money(0))
    assert january == explorer.spending[0].actual


def test_without_activity_every_chart_is_empty(db, book):
    # Renderers skip an empty chart; its periods still exist, all zero.
    explorer = _explorer(db)
    charts = spending_charts(explorer, income=True)
    assert [chart.key for chart in charts] == ["income", "income_categories", "income_share"]
    assert all(chart.empty for chart in charts)


def _stacked_model(kind=STACKED):
    return ChartModel(
        "test",
        "Test",
        kind,
        ("Jan", "Feb"),
        (
            ChartSeries("a", "A", (Money(60), Money(30)), 1),
            ChartSeries("b", "B", (Money(40), Money(-10)), 2),
            ChartSeries("c", "C", (None, Money(0)), 3),
        ),
        totals=(Money(100), Money(20)),
    )


def test_stacked_geometry_stacks_up_and_down_from_zero():
    layout = chart_bar_layout(_stacked_model(), 0, 0, 200, 100)
    assert not layout.percent
    january = [bar for bar in layout.bars if bar.category == 0]
    february = [bar for bar in layout.bars if bar.category == 1]
    # One column per category: every segment shares the category's x.
    assert len({bar.x for bar in january}) == 1 and len(january) == 2
    lower, upper = january
    # A continues into B: A is square-ended with a 1-unit gap; B carries the rounded end.
    assert (lower.series, lower.rounded, upper.series, upper.rounded) == (0, False, 1, True)
    assert upper.y + upper.height <= lower.y
    assert lower.y + lower.height == pytest.approx(layout.baseline)
    # February: 30 up, -10 down; each is the outermost in its direction.
    up, down = february
    assert (up.negative, up.rounded, down.negative, down.rounded) == (False, True, True, True)
    assert up.y + up.height == pytest.approx(layout.baseline)
    assert down.y == pytest.approx(layout.baseline)
    # The scale covers the stacked total, not the largest single value.
    assert layout.ticks[-1][0] >= 100
    assert [band for band in layout.bands] == [(0, 100), (100, 100)]


def test_share_geometry_uses_the_reported_totals_and_a_percent_scale():
    model = _stacked_model(SHARE)
    layout = chart_bar_layout(model, 0, 0, 200, 100)
    assert layout.percent
    assert [tick for tick, _y in layout.ticks][-1] >= 100
    january = [bar for bar in layout.bars if bar.category == 0]
    assert [bar.share for bar in january] == [pytest.approx(60.0), pytest.approx(40.0)]
    # February's 30 of a reported 20 is 150%; -10 is -50%.
    february = [bar.share for bar in layout.bars if bar.category == 1]
    assert february == [pytest.approx(150.0), pytest.approx(-50.0)]
    assert chart_tick_label(50.0, percent=True) == "50%"
    assert chart_tick_label(1500.0) == "1,500"


def test_line_bands_reach_halfway_to_the_neighbours():
    model = ChartModel(
        "t",
        "T",
        LINE,
        ("a", "b", "c"),
        (ChartSeries("x", "X", (Money(1), Money(2), Money(3)), 1),),
    )
    layout = chart_line_layout(model, 10, 0, 200, 100)
    assert layout.bands == ((10, 50), (60, 100), (160, 50))


def test_partial_columns_are_shaded_from_their_band():
    model = ChartModel(
        "t",
        "T",
        BARS,
        ("a", "b"),
        (ChartSeries("x", "X", (Money(1), Money(2)), 1),),
        partial_from=1,
        partial_note="Partial",
    )
    assert chart_bar_layout(model, 0, 0, 200, 100).partial_x == 100


def test_share_chart_prints_percentages_beside_its_totals():
    blocks = chart_blocks(_stacked_model(SHARE))
    assert isinstance(blocks[0], ModelChart)
    table = blocks[1]
    assert isinstance(table, Table)
    assert [column.label for column in table.columns] == ["Category", "A", "B", "C", "Total"]
    assert [cell.text for cell in table.rows[0].cells] == [
        "Jan",
        "60.0%",
        "40.0%",
        "—",
        "100.00",
    ]
    svg = model_chart_svg(_stacked_model(SHARE))
    assert "Jan, A: 60% (60.00)" in svg and ">100%<" in svg
    stacked = model_chart_svg(_stacked_model())
    assert "Jan, B: 40.00" in stacked and "%" not in stacked


def test_printed_explorer_draws_its_charts_beside_the_tables(db, book):
    _spend(db, book, ((date(2026, 1, 9), book.groceries, "80"),))
    explorer = _explorer(db, account=book.groceries, period_index=0)
    document = expense_explorer_layout(explorer, currency="USD")
    charts = [
        block.model.key
        for section in document.sections
        for block in section.blocks
        if isinstance(block, ModelChart)
    ]
    assert charts == [
        "spending",
        "spending_categories",
        "spending_share",
        "category_trend",
    ]
    html = expense_explorer_report(explorer, currency="USD")
    assert 'aria-label="Spending: total plan and actual (USD)"' in html
    assert 'aria-label="Share of spending by category (USD)"' in html
