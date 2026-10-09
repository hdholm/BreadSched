"""How long projected cash lasts, and how two scenarios' runways compare."""

from __future__ import annotations

from datetime import date

from test_drawdown import flat, nest_egg  # noqa: F401 - the fixture is used by name

from breadsched.gen.engine import projection
from breadsched.gen.lib import (
    Drawdown,
    Money,
    PeriodType,
    Recurrence,
    Scenario,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)
from breadsched.presentation import projection_notes, runway_comparison_text, runway_lines

START = date(2026, 1, 1)


def _spend(db, book, monthly: str) -> None:
    rent = ScheduledTransaction(
        name="Rent",
        recurrence=Recurrence(PeriodType.MONTH, interval=1, start=date(2026, 1, 15)),
        splits=[
            ScheduledSplit(book.rent, Money(monthly)),
            ScheduledSplit(book.checking, Money(f"-{monthly}")),
        ],
    )
    with db.transaction("Rent and opening cash") as txn:
        db.add_scheduled(rent, txn)
        db.add_transaction(
            Transaction.simple(date(2025, 12, 1), "Opening", book.checking, book.opening, "3000"),
            txn,
        )


def _scenario(name: str = "Base", *drawdowns: Drawdown) -> Scenario:
    scenario = Scenario(name=name, start=START, years=1, assumptions=flat())
    scenario.drawdowns = list(drawdowns)
    return scenario


def test_cash_that_runs_out_reports_the_month_and_months_covered(db, book):
    _spend(db, book, "1000")
    runway = projection.project(db, _scenario()).runway()

    assert runway.months == 12
    assert runway.months_covered == 3
    assert runway.first_shortfall == date(2026, 4, 1)
    assert (runway.lowest_cash, runway.lowest_month) == (Money("-9000"), date(2026, 12, 1))
    assert not runway.lasts
    assert runway_lines(runway) == [
        "Cash runs out in Apr 2026, after 3 months.",
        "Lowest cash: (9,000.00) in Dec 2026.",
    ]
    assert runway.as_dict()["first_shortfall"] == date(2026, 4, 1)


def test_cash_that_lasts_says_so(db, book):
    _spend(db, book, "100")
    result = projection.project(db, _scenario())
    runway = result.runway()

    assert runway.lasts and runway.months_covered == 12
    assert runway_lines(runway)[0] == "Cash lasts the whole projection (12 months)."
    assert projection_notes(result)[:2] == runway_lines(runway)


def test_a_drawdown_that_empties_its_account_is_a_depletion(db, nest_egg):  # noqa: F811
    rule = Drawdown(nest_egg.ira, nest_egg.checking, START, annual_amount=Money("600000"))
    runway = projection.project(db, _scenario("Spend fast", rule)).runway()

    assert runway.depletions == (("IRA", date(2026, 3, 1)),)
    assert "IRA runs out in Mar 2026." in runway_lines(runway)


def test_two_runways_say_which_lasts_longer(db, book):
    _spend(db, book, "1000")
    short = projection.project(db, _scenario("Base")).runway()
    long = projection.project(db, _scenario("Frugal")).runway()
    assert runway_comparison_text("Base", short, "Frugal", long) == (
        "Base: cash runs out in Apr 2026 (3 months); Frugal: cash runs out in Apr 2026 "
        "(3 months); the same runway."
    )
    lasting = projection.Projection(scenario=_scenario("Lasting"), rows=[]).runway()
    assert runway_comparison_text("Base", short, "Lasting", lasting).endswith(
        "Lasting: cash lasts the whole 0 months; Base lasts 3 months longer."
    )


def test_web_and_print_show_the_runway(db, book):
    from breadsched.plugins.export.html_report import projection_report as projection_html
    from breadsched.web.projection_resource import (
        projection_comparison_report,
        projection_report,
    )

    _spend(db, book, "1000")
    scenario = _scenario()
    payload = projection_report(db, scenario)
    assert payload["runway"]["months_covered"] == 3
    assert payload["runway_notes"][0] == "Cash runs out in Apr 2026, after 3 months."

    compared = projection_comparison_report(
        db, scenario, _scenario("Other"), primary_base=False, comparison_base=False
    )
    assert compared["comparison"]["runway"]["months_covered"] == 3
    assert compared["comparison"]["runway_comparison"].endswith("the same runway.")

    result = projection.project(db, scenario)
    html = projection_html(result, comparison=projection.project(db, _scenario("Other")))
    assert "Cash runway" in html
    assert "Cash runs out in Apr 2026, after 3 months." in html
    assert "the same runway." in html


def test_the_projection_chart_marks_the_first_shortfall_and_overlays_a_comparison(db, book):
    from breadsched.gen.engine.chart_model import LINE
    from breadsched.gen.engine.projection_result import projection_chart
    from breadsched.plugins.export.html_report import model_chart_svg
    from breadsched.presentation import chart_line_layout

    _spend(db, book, "1000")
    result = projection.project(db, _scenario())
    calmer = projection.project(db, _scenario("Calmer"))
    chart = projection_chart(result, calmer, "USD")
    assert (chart.kind, len(chart.categories), chart.currency) == (LINE, 12, "USD")
    assert [series.name for series in chart.series] == [
        "Cash",
        "Investments",
        "Net worth",
        "Calmer net worth",
    ]
    assert [series.slot for series in chart.series] == [1, 2, 3, 4]
    # The chart's values are the projection's own.
    assert chart.series[0].values == tuple(row.cash_close for row in result.rows)
    [marker] = chart.markers
    assert (marker.index, marker.label) == (3, "First shortfall: Apr 2026")
    assert chart.partial_from is None and chart.partial_note == ""

    layout = chart_line_layout(chart, 0, 0, 1100, 200)
    assert layout.xs[0] == 0 and layout.xs[-1] == 1100
    assert layout.markers == ((layout.xs[3], "First shortfall: Apr 2026"),)
    svg = model_chart_svg(chart)
    assert svg.count("<polyline") == 4 and "First shortfall: Apr 2026" in svg
    assert "Apr 2026\nCash: (1,000.00) USD" in svg.replace("&#x27;", "'")

    plain = projection_chart(result)
    assert len(plain.series) == 3


def test_projection_prints_its_chart_and_optionally_every_value(db, book):
    from breadsched.plugins.export.report_layout import ModelChart, Table, projection_layout

    _spend(db, book, "1000")
    result = projection.project(db, _scenario())
    document = projection_layout(result, currency="USD")
    main, values = document.sections
    assert any(isinstance(block, ModelChart) for block in main.blocks)
    assert values.optional and values.name == "Projection chart values"
    [table] = [block for block in values.blocks if isinstance(block, Table)]
    assert len(table.rows) == 12
    assert "Apr 2026 | (1,000.00)" in document.text(include_optional=True)
