"""The Dashboard's cash-flow chart: cash on hand over the liquidity period.

The synthetic sample book, seen on 2026-03-15, has cash in the bank, bills and
wages scheduled each month, and a liquidity period of 30 days. The chart starts
at liquid cash less overdue bills and moves on each bill's and each pay's date,
using the same bills and income the *Needed* figure counts.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine import dashboard
from breadsched.gen.engine.chart_model import LINE
from breadsched.gen.lib import Money
from breadsched.gen.sample_book import create_sample_book
from breadsched.plugins.export.report_layout import ModelChart, dashboard_layout

AS_OF = date(2026, 3, 15)


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "sample.breadsched"
    create_sample_book(path, as_of=AS_OF)
    db = DbSQLite()
    db.load(str(path))
    yield dashboard.build(db, as_of=AS_OF)
    db.close()


def _expected_end(board) -> Money:
    horizon = AS_OF + timedelta(days=board.config.liquidity_days)
    bills = sum((bill.amount for bill in board.bills if bill.next_due <= horizon), Money(0))
    return board.liquid - bills + board.income_within(board.config.liquidity_days)


def test_the_chart_runs_daily_from_today_through_the_liquidity_period(board):
    chart = board.cash_flow_chart("USD")

    assert (chart.kind, chart.currency) == (LINE, "USD")
    assert chart.title == "Cash flow over the next 30 days"
    assert chart.categories[0] == AS_OF.isoformat()
    assert chart.categories[-1] == (AS_OF + timedelta(days=30)).isoformat()
    assert len(chart.categories) == 31
    [series] = chart.series
    assert series.name == "Cash on hand"
    # Every bill and every pay inside the period has moved the line by its amount.
    assert series.values[-1] == _expected_end(board)
    assert not chart.empty


def test_each_bill_and_pay_moves_the_line_on_its_own_date(board):
    values = board.cash_flow_chart().series[0].values
    days = {(bill.next_due - AS_OF).days: bill for bill in board.bills if bill.next_due >= AS_OF}
    day, bill = min(days.items())
    assert 0 < day <= 30
    income = sum(
        (amount for when, amount in board._income_events if when == bill.next_due), Money(0)
    )
    others = sum((item.amount for item in board.bills if item.next_due == bill.next_due), Money(0))
    assert values[day] - values[day - 1] == income - others


def test_an_overdue_bill_lowers_the_first_day_and_the_lowest_day_is_marked(board):
    first = board.bills[0]
    late = replace(first, next_due=AS_OF - timedelta(days=3), amount=Money("20000.00"))
    board.pending = [late if row is first else row for row in board.pending]

    chart = board.cash_flow_chart()

    values = chart.series[0].values
    assert values[0] <= board.liquid - Money("20000.00")
    lowest = values.index(min(values))
    if lowest:
        assert chart.markers[0].index == lowest
        assert chart.markers[0].label.startswith("Lowest: ")
    else:
        assert chart.markers == ()


def test_a_long_period_is_drawn_by_the_week(board):
    board.config.liquidity_days = 180
    chart = board.cash_flow_chart()

    assert len(chart.categories) == 26
    assert chart.categories[1] == (AS_OF + timedelta(days=7)).isoformat()
    assert chart.title == "Cash flow over the next 180 days"


def test_nothing_dated_in_the_period_draws_nothing(board):
    board.pending = []
    board._income_events = []

    assert board.cash_flow_chart().empty


def test_the_printed_dashboard_carries_the_chart_unless_its_section_is_hidden(board):
    def charts(document):
        return [
            block
            for section in document.sections
            for block in section.blocks
            if isinstance(block, ModelChart)
        ]

    shown = dashboard_layout(board)
    assert [item.model.key for item in charts(shown)] == ["dashboard-cash-flow"]
    assert "Cash flow over the next 30 days" in shown.text()

    board.config.sections["cash_flow"] = False
    assert charts(dashboard_layout(board)) == []
