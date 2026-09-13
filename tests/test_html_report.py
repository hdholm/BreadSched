"""Printable reports preserve the applied engine values without external assets."""

from datetime import date

from breadsched.gen.engine import activity, dashboard, projection
from breadsched.gen.lib import (
    Money,
    PeriodType,
    Recurrence,
    Scenario,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)
from breadsched.plugins.export.html_report import (
    dashboard_report,
    plan_report,
    projection_report,
)


def test_dashboard_report_contains_current_verdict_groups_and_pending_flow(db, book):
    with db.transaction("Printable household") as txn:
        db.add_transaction(
            Transaction.simple(
                date(2026, 1, 1),
                "Opening",
                book.checking,
                book.opening,
                "2500.00",
            ),
            txn,
        )
        db.add_scheduled(
            ScheduledTransaction(
                name="Utilities <estimate>",
                recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 2, 15)),
                splits=[
                    ScheduledSplit(book.utilities, Money("125.00")),
                    ScheduledSplit(book.checking, Money("-125.00")),
                ],
            ),
            txn,
        )
    config = dashboard.DashboardConfig(
        groups=[dashboard.GroupConfig("Ready cash", [book.checking], "liquid")]
    )
    board = dashboard.build(db, config, as_of=date(2026, 2, 1))

    document = dashboard_report(board, book_name="household.breadsched")

    assert "Ready cash" in document
    assert "Utilities &lt;estimate&gt;" in document
    assert "Pending cash flow" in document
    assert "2,500.00" in document
    assert "window.print()" in document
    assert "http://" not in document and "https://" not in document


def test_plan_report_uses_selected_measure_horizon_totals_and_scenario(db, book):
    with db.transaction("Printable plan") as txn:
        db.add_scheduled(
            ScheduledTransaction(
                name="Monthly utility",
                recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 7)),
                splits=[
                    ScheduledSplit(book.utilities, Money("100.00")),
                    ScheduledSplit(book.checking, Money("-100.00")),
                ],
            ),
            txn,
        )
    report = activity.build_category_report(
        db,
        date(2026, 1, 1),
        date(2026, 3, 31),
        period=activity.ReportingPeriod.MONTH,
    )

    document = plan_report(
        report,
        activity.PlanMeasure.PLANNED,
        scenario_name="Careful <case>",
    )

    assert "Jan 2026" in document and "Mar 2026" in document
    assert "Expenses total" in document
    assert "Net cash change" in document
    assert "300.00" in document
    assert "Careful &lt;case&gt;" in document
    assert "Planned" in document


def test_projection_report_includes_chart_assumptions_year_end_values_and_comparison(db, book):
    primary = projection.project(
        db,
        Scenario(name="Base <draft>", start=date(2026, 1, 1), years=1),
    )
    compared = projection.project(
        db,
        Scenario(name="Alternative", start=date(2026, 1, 1), years=1),
    )

    document = projection_report(primary, comparison=compared)

    assert "Projection chart" in document
    assert 'aria-label="Projection chart"' in document
    assert "Annual assumptions" in document
    assert "Year-end values" in document
    assert "Base &lt;draft&gt; versus Alternative" in document
    assert "Dec 2026" in document
    assert "Net worth difference" in document
    assert "Contributions" in document
    assert "Taxable withdrawals" in document
    assert "Retirement distributions" in document
    assert "Investment income" in document
    assert "Investment fees" in document
    assert "Retirement rollovers" in document
