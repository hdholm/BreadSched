"""The report and dashboard breadlets shipped with BreadSched.

Each one reads the book through the book server it is given and returns a
:class:`~breadsched.plugins.export.report_layout.ReportDocument`. The charted
reports follow HomeBank's lead: where the money went as a ring, income beside
spending as columns, and net worth as a line, each with the exact table beside it.
The rest present a report the application already had (budget jars, tax year,
realized gains), so every report is reached the same way in every interface.

An add-on breadlet is written the same way; see ``docs/design/breadlets.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from ...gen.engine.activity import ReportingPeriod
from ...gen.engine.chart_model import BARS, PIE, ChartModel, ChartSeries
from ...gen.lib.money import Money
from ...gen.plug._breadlets import (
    CHOICE,
    DASHBOARD,
    DATE,
    INTEGER,
    BreadletOption,
    month_end,
    month_start,
)
from ...gen.plug._manager import REPORT, Plugin, PluginManager
from ...gen.services.plan import PlanQuery
from ..export.report_layout import (
    Block,
    Cell,
    Column,
    Heading,
    ModelChart,
    Paragraph,
    ReportDocument,
    Section,
    Table,
    TableRow,
    budget_jars_layout,
    chart_blocks,
    money_text,
    net_worth_history_layout,
    realized_gains_layout,
    tax_year_layout,
)

__all__ = ["income_and_spending", "register", "spending_by_category"]

#: Slices named before the rest are folded into "Other".
_NAMED_SLICES = 7

_PERIODS = (("month", "Month"), ("quarter", "Quarter"), ("year", "Year"))


def _this_month(today: date) -> str:
    return month_start(today).isoformat()


def _end_of_month(today: date) -> str:
    return month_end(today).isoformat()


def _year_ago(today: date) -> str:
    return month_start(today, 11).isoformat()


def _start_of_year(today: date) -> str:
    return date(today.year, 1, 1).isoformat()


def _end_of_year(today: date) -> str:
    return date(today.year, 12, 31).isoformat()


def _range(start_default, end_default, *, period: bool = False) -> tuple[BreadletOption, ...]:
    options: tuple[BreadletOption, ...] = (
        BreadletOption("start", "From", DATE, start_default),
        BreadletOption("end", "Through", DATE, end_default),
    )
    if period:
        options += (BreadletOption("period", "Group by", CHOICE, "month", _PERIODS),)
    return options


def _subtitle(start: date, end: date) -> str:
    return f"{start.isoformat()} through {end.isoformat()}"


def _explorer(book: Any, start: date, end: date, period: str, today: date | None):
    """The spending and income of ``start`` through ``end``, kept within the months
    the book has (a range reaching before its first month starts there)."""
    bounds = book.plan_bounds(today)
    start = min(max(start, bounds.minimum), bounds.maximum)
    end = max(end, start)
    query = PlanQuery(start=start, end=end, period=ReportingPeriod(period), today=today)
    return book.expense_explorer(query)


def _explored(explorer: Any) -> str:
    return _subtitle(explorer.plan.start, explorer.plan.end)


def spending_by_category(explorer: Any, currency: str = "") -> tuple[ChartModel, Money]:
    """Spending in the explorer's range by top-level category, as a ring.

    The largest seven categories are named and the rest are one "Other" slice. A
    category whose refunds exceed its spending is left out of the ring (and named in
    the table); the ring's whole is the spending it shows, and the second value
    returned is the net total, refunds included.
    """
    names = {row.account: row.name for row in explorer.categories}
    totals: dict[str, Money] = {}
    for point in explorer.spending:
        for handle, amount in point.categories:
            totals[handle] = totals.get(handle, Money(0)) + amount
    net = sum(totals.values(), Money(0))
    ranked = sorted(
        (handle for handle, amount in totals.items() if amount > Money(0)),
        key=lambda handle: (-totals[handle], names.get(handle, handle)),
    )
    named = ranked if len(ranked) <= _NAMED_SLICES + 1 else ranked[:_NAMED_SLICES]
    labels = [names.get(handle, handle) for handle in named]
    values = [totals[handle] for handle in named]
    rest = sum((totals[handle] for handle in ranked[len(named) :]), Money(0))
    if rest > Money(0):
        labels.append("Other")
        values.append(rest)
    whole = sum(values, Money(0))
    model = ChartModel(
        "spending-by-category",
        "Spending by category",
        PIE,
        tuple(labels),
        (ChartSeries("spending", "Spending", tuple(values), 1),),
        currency,
        totals=(whole,),
    )
    return model, net


def income_and_spending(explorer: Any, currency: str = "") -> ChartModel:
    """Income beside spending for each period of the explorer's range."""
    points = list(zip(explorer.income, explorer.spending, strict=False))
    return ChartModel(
        "income-and-spending",
        "Income and spending",
        BARS,
        tuple(spent.label for _income, spent in points),
        (
            ChartSeries("income", "Income", tuple(income.actual for income, _s in points), 3),
            ChartSeries("spending", "Spending", tuple(spent.actual for _i, spent in points), 2),
        ),
        currency,
    )


def _incomplete(explorer: Any) -> tuple[Block, ...]:
    if any(point.currency_incomplete for point in (*explorer.spending, *explorer.income)):
        return (
            Paragraph(
                "Some foreign-currency activity has no exchange rate for its date and is left out.",
                warning=True,
            ),
        )
    return ()


def _spending_blocks(explorer: Any, currency: str) -> list[Block]:
    model, net = spending_by_category(explorer, currency)
    blocks: list[Block] = list(chart_blocks(model))
    if model.empty:
        blocks.append(Paragraph("No spending in this range."))
    elif model.totals and net != model.totals[0]:
        blocks.append(
            Paragraph(f"Net of refunds, spending was {money_text(net)} {currency}".rstrip() + ".")
        )
    return [*blocks, *_incomplete(explorer)]


def _run_spending(book: Any, options: Mapping[str, Any], today: date) -> ReportDocument:
    start, end = options["start"], options["end"]
    explorer = _explorer(book, start, end, "month", today)
    blocks = _spending_blocks(explorer, book.reporting_currency())
    return ReportDocument(
        "Spending by category",
        _explored(explorer),
        (Section(tuple(blocks)),),
        kind="spending-by-category",
    )


def _income_blocks(explorer: Any, currency: str) -> list[Block]:
    model = income_and_spending(explorer, currency)
    rows = []
    for income, spent in zip(explorer.income, explorer.spending, strict=False):
        net = income.actual - spent.actual
        rows.append(
            TableRow(
                (
                    Cell(spent.label),
                    Cell(money_text(income.actual), numeric=True),
                    Cell(money_text(spent.actual), numeric=True),
                    Cell(money_text(net), numeric=True, negative=net < Money(0)),
                )
            )
        )
    blocks: list[Block] = []
    if not model.empty:
        blocks.append(ModelChart(model))
    blocks.append(
        Table(
            (
                Column("Period"),
                Column("Income", True),
                Column("Spending", True),
                Column("Left over", True),
            ),
            tuple(rows),
            empty="No income or spending in this range.",
        )
    )
    return [*blocks, *_incomplete(explorer)]


def _run_income(book: Any, options: Mapping[str, Any], today: date) -> ReportDocument:
    start, end = options["start"], options["end"]
    explorer = _explorer(book, start, end, options["period"], today)
    blocks = _income_blocks(explorer, book.reporting_currency())
    return ReportDocument(
        "Income and spending",
        _explored(explorer),
        (Section(tuple(blocks)),),
        kind="income-and-spending",
    )


def _run_net_worth(book: Any, options: Mapping[str, Any], today: date) -> ReportDocument:
    period = ReportingPeriod(options["period"])
    history = book.net_worth_history(options["start"], options["end"], period, today)
    return net_worth_history_layout(history, book.reporting_currency())


def _run_jars(book: Any, options: Mapping[str, Any], today: date) -> ReportDocument:
    period = ReportingPeriod(options["period"])
    view = book.budget_jars(options["start"], options["end"], period, today)
    return budget_jars_layout(view.report, view.labels())


def _run_tax_year(book: Any, options: Mapping[str, Any], _today: date) -> ReportDocument:
    view = book.tax_year(options["year"])
    return tax_year_layout(view.report, view.labels())


def _run_gains(book: Any, options: Mapping[str, Any], _today: date) -> ReportDocument:
    view = book.realized_gains(options["year"])
    return realized_gains_layout(view.report, view.labels(), year=view.year)


def _run_dashboard_spending(book: Any, _options: Mapping[str, Any], today: date) -> ReportDocument:
    start, end = month_start(today), month_end(today)
    explorer = _explorer(book, start, end, "month", today)
    blocks = [
        Heading("Spending this month"),
        *_spending_blocks(explorer, book.reporting_currency()),
    ]
    return ReportDocument(
        "Spending this month", _explored(explorer), (Section(tuple(blocks)),), kind="dashboard"
    )


def _run_dashboard_income(book: Any, _options: Mapping[str, Any], today: date) -> ReportDocument:
    start, end = month_start(today, 5), month_end(today)
    explorer = _explorer(book, start, end, "month", today)
    model = income_and_spending(explorer, book.reporting_currency())
    blocks: list[Block] = [Heading("Income and spending, last six months")]
    blocks += [ModelChart(model)] if not model.empty else [Paragraph("No income or spending yet.")]
    return ReportDocument(
        "Income and spending", _explored(explorer), (Section(tuple(blocks)),), kind="dashboard"
    )


def register(manager: PluginManager) -> None:
    """Register the shipped report and dashboard breadlets."""
    year = BreadletOption(
        "year", "Year", INTEGER, "", minimum=1900, maximum=9999, help="Blank is the latest year."
    )
    for plugin in (
        Plugin(
            "spending-by-category",
            "Spending by category",
            REPORT,
            _run_spending,
            "Where the money went: each top-level category's share, as a ring",
            options=_range(_this_month, _end_of_month),
        ),
        Plugin(
            "income-and-spending",
            "Income and spending",
            REPORT,
            _run_income,
            "Income beside spending for each month, quarter or year, and what was left over",
            options=_range(_year_ago, _end_of_month, period=True),
        ),
        Plugin(
            "net-worth",
            "Net worth over time",
            REPORT,
            _run_net_worth,
            "What you own less what you owe at each period end, at market value",
            options=_range(_year_ago, _end_of_month, period=True),
        ),
        Plugin(
            "budget-jars",
            "Budget jars",
            REPORT,
            _run_jars,
            "Each jar's fills, draws and level",
            options=_range(_start_of_year, _end_of_year, period=True),
        ),
        Plugin(
            "tax-year",
            "Tax year",
            REPORT,
            _run_tax_year,
            "One calendar year's gains, tax-relevant totals and income by source",
            options=(year,),
        ),
        Plugin(
            "realized-gains",
            "Realized gains",
            REPORT,
            _run_gains,
            "Every sale with the lots it took, and yearly totals",
            options=(year,),
        ),
        Plugin(
            "spending-this-month",
            "Spending this month",
            DASHBOARD,
            _run_dashboard_spending,
            "This month's spending by category, as a ring",
            shown_by_default=True,
        ),
        Plugin(
            "recent-income-and-spending",
            "Income and spending",
            DASHBOARD,
            _run_dashboard_income,
            "Income beside spending for the last six months",
            shown_by_default=True,
        ),
    ):
        manager.register(plugin)
