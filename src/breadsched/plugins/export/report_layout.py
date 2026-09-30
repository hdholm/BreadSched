"""Printable report layouts, independent of how they are drawn.

A layout turns the engine results a view already shows (a Dashboard, a Plan
report, a Projection) into a small document of headings, summary cards, notes,
tables, and a chart. The browser print preview renders it as HTML
(``html_report``) and the desktop draws it natively through GTK printing
(``gui.printing``), so both describe the same applied view state with the same
words and numbers. Layouts never query the database or recalculate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import zip_longest

from ...gen.engine.activity import CategoryReport, PlanMeasure
from ...gen.engine.dashboard import Dashboard, MissedGroup
from ...gen.engine.projection import Projection
from ...gen.lib.account import AccountClass
from ...gen.lib.money import Money
from ...gen.services.plan import PlanGoalMilestone
from ...presentation import goal_status_text, plan_goal_text, projection_goal_notes

__all__ = [
    "Card",
    "Cards",
    "Cell",
    "Chart",
    "ChartSeries",
    "Column",
    "Heading",
    "Paragraph",
    "ReportDocument",
    "Section",
    "Table",
    "TableRow",
    "dashboard_layout",
    "money_text",
    "plan_layout",
    "projection_layout",
    "signed_money_text",
]


# ------------------------------------------------------------------ model


@dataclass(frozen=True)
class Card:
    """One summary figure; ``alarm`` marks a value that needs attention."""

    label: str
    value: str
    alarm: bool = False


@dataclass(frozen=True)
class Cards:
    items: tuple[Card, ...]


@dataclass(frozen=True)
class Heading:
    text: str


@dataclass(frozen=True)
class Paragraph:
    """Explanatory text; a ``warning`` is drawn in the alarm colour."""

    text: str
    warning: bool = False
    bullet: bool = False


@dataclass(frozen=True)
class Column:
    label: str
    numeric: bool = False


@dataclass(frozen=True)
class Cell:
    """One table cell.

    ``indent`` is a nesting depth; ``note`` follows the text in a quieter style,
    and ``below`` puts it on its own line; ``hint`` is hover text on screen.
    """

    text: str
    numeric: bool = False
    negative: bool = False
    indent: int = 0
    note: str = ""
    below: bool = False
    hint: str = ""


@dataclass(frozen=True)
class TableRow:
    """A row; ``style`` is "", "heading", "section", "total", or "grand".

    A "section" row has one cell, which spans the whole table.
    """

    cells: tuple[Cell, ...]
    style: str = ""


@dataclass(frozen=True)
class Table:
    columns: tuple[Column, ...]
    rows: tuple[TableRow, ...]
    #: Shown instead of the table when it has no rows.
    empty: str = ""


@dataclass(frozen=True)
class ChartSeries:
    name: str
    values: tuple[float, ...]
    colour: str


@dataclass(frozen=True)
class Chart:
    label: str
    series: tuple[ChartSeries, ...]
    #: (point index, label) for the horizontal axis.
    ticks: tuple[tuple[int, str], ...] = ()


Block = Heading | Paragraph | Cards | Table | Chart


@dataclass(frozen=True)
class Section:
    """Blocks printed together. ``optional`` ones print only when asked for,
    starting a new page."""

    blocks: tuple[Block, ...]
    name: str = ""
    optional: bool = False


@dataclass(frozen=True)
class ReportDocument:
    title: str
    subtitle: str
    sections: tuple[Section, ...]
    #: Label of the choice that includes optional sections, if any exist.
    optional_label: str = ""
    kind: str = field(default="")

    @property
    def has_optional(self) -> bool:
        return any(section.optional for section in self.sections)

    def text(self, *, include_optional: bool = False) -> str:
        """Every word the report prints, for tests and accessibility checks."""
        parts = [self.title, self.subtitle]
        for section in self.sections:
            if section.optional and not include_optional:
                continue
            for block in section.blocks:
                parts.extend(_block_text(block))
        return "\n".join(part for part in parts if part)


def _block_text(block: Block) -> list[str]:
    if isinstance(block, Heading):
        return [block.text]
    if isinstance(block, Paragraph):
        return [block.text]
    if isinstance(block, Cards):
        return [f"{card.label} {card.value}" for card in block.items]
    if isinstance(block, Chart):
        return [block.label, *(series.name for series in block.series)]
    if not block.rows:
        return [block.empty]
    lines = [" | ".join(column.label for column in block.columns)]
    for row in block.rows:
        lines.append(
            " | ".join(f"{cell.text} {cell.note}".strip() for cell in row.cells if cell.text)
        )
    return lines


# ---------------------------------------------------------------- helpers


def money_text(value: Money | None) -> str:
    return "—" if value is None else value.format(parens_negative=True)


def signed_money_text(value: Money | None) -> str:
    if value is None:
        return "—"
    rendered = value.format(parens_negative=True)
    return f"+{rendered}" if value > 0 else rendered


def _amount(value: Money | None, *, signed: bool = False) -> Cell:
    text = signed_money_text(value) if signed else money_text(value)
    return Cell(text, numeric=True, negative=value is not None and value < 0)


def _text(text: object, **kwargs) -> Cell:
    return Cell(str(text), **kwargs)


def _columns(*specs: str) -> tuple[Column, ...]:
    """Columns from labels; a leading "#" marks a numeric column."""
    return tuple(Column(spec[1:], True) if spec.startswith("#") else Column(spec) for spec in specs)


def _notes(lines, *, warning: bool = False) -> tuple[Paragraph, ...]:
    return tuple(Paragraph(line, warning=warning) for line in lines if line)


# -------------------------------------------------------------- Dashboard


def dashboard_layout(board: Dashboard, *, book_name: str = "") -> ReportDocument:
    """The currently built Dashboard."""
    summary = board.summary()
    report = board.report_summary()

    def visible(field_name: str, value: Money) -> str:
        if report[field_name] is None:
            return board.unavailable_reason(field_name)
        return money_text(value)

    cards = [
        Card("Net worth", visible("net_worth", summary["net_worth"])),
        Card("Liquid", visible("liquid", summary["liquid"])),
        Card(
            f"Needed in {board.config.liquidity_days} days",
            money_text(summary["required_liquid"]),
        ),
        Card(
            "Available",
            visible("available", summary["available"]),
            report["available"] is not None and summary["available"] < 0,
        ),
        Card(
            f"Emergency fund ({board.config.emergency_months} mo)",
            visible("emergency_fund", summary["emergency_fund"]),
        ),
        Card(
            "Committed emergency outgoings / mo",
            money_text(summary["emergency_monthly_outgoings"]),
        ),
        Card(
            "Including estimates / mo",
            money_text(summary["emergency_monthly_outgoings_with_estimates"]),
        ),
        Card(
            "Months covered",
            f"{summary['months_covered']:g}"
            if report["months_covered"] is not None
            else board.unavailable_reason("months_covered"),
            report["months_covered"] is not None
            and summary["months_covered"] < board.config.emergency_months,
        ),
    ]
    if report["emergency_shortfall"] is not None and summary["emergency_shortfall"] > 0:
        cards.append(Card("Short of the fund", money_text(summary["emergency_shortfall"]), True))
    if summary["goals_set_aside"] > 0:
        goals_value = money_text(summary["goals_set_aside"])
        if summary["goals_held"] != summary["goals_set_aside"]:
            goals_value += f" ({money_text(summary['goals_held'])} held from spendable cash)"
        cards.append(Card("Set aside for goals", goals_value))

    group_rows = []
    for group in board.groups:
        ratio = (
            f"{group.report_loan_to_value:.1%}" if group.report_loan_to_value is not None else ""
        )
        group_rows.append(
            TableRow(
                (
                    Cell(
                        group.name,
                        indent=group.depth,
                        note=f"— {group.note}" if group.note else "",
                        hint=group.path,
                    ),
                    _amount(group.report_value),
                    _amount(group.report_debt),
                    _amount(
                        group.report_equity
                        if group.report_equity is not None
                        else group.report_total
                    ),
                    Cell(ratio, numeric=True),
                    _text(group.loan_end or ""),
                ),
                "heading" if group.heading else "",
            )
        )
    balances = Table(
        _columns("Group", "#Value", "#Owed", "#Equity / total", "#LTV", "Loan end"),
        tuple(group_rows),
        empty="No Dashboard groups are configured.",
    )

    def due_cells(item) -> tuple[Cell, Cell]:
        days = item.days_until(board.as_of)
        due_in = f"{-days} days overdue" if days < 0 else "today" if days == 0 else f"{days} days"
        if isinstance(item, MissedGroup):
            # Printed pages cannot expand on demand, so the missed dates are listed.
            dates = ", ".join(
                f"{when.isoformat()} {money_text(amount)}" for when, amount in item.occurrences
            )
            return (
                Cell(
                    f"{item.next_due.isoformat()} to {item.last_due.isoformat()}",
                    note=dates,
                    below=True,
                ),
                Cell(f"{item.count} missed, {due_in}"),
            )
        return Cell(item.next_due.isoformat()), Cell(due_in)

    bills = Table(
        _columns(
            "Item",
            "Due",
            "Due in",
            "Frequency",
            "#Amount",
            "#Monthly",
            "#Hold now",
            "#Annual",
            "Kind",
        ),
        tuple(
            TableRow(
                (
                    _text(item.name),
                    *due_cells(item),
                    _text(item.frequency),
                    _amount(item.amount),
                    _amount(None if item.generated else item.monthly),
                    _amount(None if item.income else item.held),
                    _amount(None if item.generated else item.annual),
                    _text("Account payment" if item.generated else "Committed"),
                )
            )
            for item in board.display_bills
        ),
    )
    income = Table(
        _columns("Item", "Due", "Due in", "Frequency", "#Amount", "#Monthly", "#Annual"),
        tuple(
            TableRow(
                (
                    _text(item.name),
                    *due_cells(item),
                    _text(item.frequency),
                    _amount(item.amount),
                    _amount(item.monthly),
                    _amount(item.annual),
                )
            )
            for item in board.display_incomes
        ),
    )
    blocks: list[Block] = [
        Cards(tuple(cards)),
        Heading("Balances"),
        balances,
        *_notes(board.coverage_notes),
        Heading("Pending bills"),
        bills,
        Heading("Expected income"),
        income,
    ]
    if board.goals:
        blocks += [
            Heading("Savings goals"),
            Table(
                _columns("Goal", "Target date", "#Target", "#Set aside", "#Remaining", "Status"),
                tuple(
                    TableRow(
                        (
                            _text(item.goal.name),
                            _text(item.goal.target_date.isoformat()),
                            _amount(item.target),
                            _amount(item.set_aside),
                            _amount(item.remaining),
                            _text(goal_status_text(item)),
                        )
                    )
                    for item in board.goals
                ),
            ),
        ]
    subtitle = f"{book_name + ' · ' if book_name else ''}As at {board.as_of.isoformat()}"
    return ReportDocument("Dashboard", subtitle, (Section(tuple(blocks)),), kind="dashboard")


# ------------------------------------------------------------------- Plan


def plan_layout(
    report: CategoryReport,
    measure: PlanMeasure,
    *,
    scenario_name: str,
    book_name: str = "",
    goal_milestones: Sequence[PlanGoalMilestone] = (),
) -> ReportDocument:
    """The applied Plan horizon, grouping, measure, and scenario.

    The category detail is an optional section: it prints only when asked for.
    """
    del book_name  # the Plan subtitle names the horizon, not the book
    activity = report.activity
    position = report.cash_position
    cards = Cards(
        (
            Card("Opening spendable cash", money_text(position.opening), position.opening < 0),
            Card("Ending spendable cash", money_text(position.closing), position.closing < 0),
            Card(
                f"Lowest spendable cash ({position.minimum_date.isoformat()})",
                money_text(position.minimum),
                position.minimum < 0,
            ),
            Card(
                "Projected change in spendable cash",
                signed_money_text(activity.planned_cash_change),
                activity.planned_cash_change < 0,
            ),
            Card(
                f"Actual change through {report.as_of.isoformat()}",
                signed_money_text(report.actual_cash_through_as_of),
                (report.actual_cash_through_as_of or Money(0)) < 0,
            ),
            Card(
                "Variance through as-of date",
                signed_money_text(report.cash_variance_through_as_of),
                (report.cash_variance_through_as_of or Money(0)) < 0,
            ),
            Card("Expected unresolved", str(activity.unresolved_count)),
            Card("Actuals to review", str(activity.unresolved_actual_count)),
        )
    )
    period_columns = tuple(Column(period.label, True) for period in activity.periods)

    def values_row(
        label: Cell, values, total: Money | None, style: str = "", *, signed: bool = False
    ) -> TableRow:
        cells = tuple(_amount(value, signed=signed) for value in values)
        return TableRow((label, *cells, _amount(total, signed=signed)), style)

    def section_row(text: str) -> TableRow:
        return TableRow((Cell(text),), "section")

    summary_rows = [section_row("Spendable cash bridge")]
    for bridge in report.cash_bridge:
        summary_rows.append(
            values_row(
                Cell(bridge.name), bridge.values(measure), bridge.total(measure), signed=True
            )
        )
    summary_rows.append(
        values_row(
            Cell("Net change in spendable cash"),
            report.cash_bridge_totals(measure),
            report.cash_bridge_grand_total(measure),
            "grand",
            signed=True,
        )
    )

    detail_rows: list[TableRow] = []
    for section, categories, account_class in (
        ("Income", report.income, AccountClass.INCOME),
        ("Expenses", report.expenses, AccountClass.EXPENSE),
    ):
        detail_rows.append(section_row(section))
        for category in categories:
            label = Cell(category.name, indent=category.depth, hint=category.full_name)
            detail_rows.append(values_row(label, category.values(measure), category.total(measure)))
        detail_rows.append(
            values_row(
                Cell(f"{section} total"),
                report.category_totals(account_class, measure),
                report.category_grand_total(account_class, measure),
                "total",
            )
        )
    detail_rows.append(
        values_row(
            Cell("Income less expenses"),
            report.operating_net_totals(measure),
            report.operating_net_grand_total(measure),
            "grand",
            signed=True,
        )
    )
    if report.mortgage_payments:
        detail_rows.append(section_row("Cash requirements (informational)"))
        hint = (
            "Whole mortgage payment; classified components appear below and are not "
            "added to this row."
        )
        for payment in report.mortgage_payments:
            detail_rows.append(
                values_row(
                    Cell(payment.name, hint=hint), payment.values(measure), payment.total(measure)
                )
            )
        detail_rows.append(
            values_row(
                Cell("Mortgage cash required"),
                report.mortgage_payment_totals(measure),
                report.mortgage_payment_grand_total(measure),
                "total",
            )
        )
    if report.planning_flows:
        detail_rows.append(section_row("Balance-sheet classifications (informational)"))
        for flow in report.planning_flows:
            detail_rows.append(
                values_row(
                    Cell(flow.name, hint=flow.full_name), flow.values(measure), flow.total(measure)
                )
            )

    total_column = (Column("Total", True),)
    lead: list[Block] = [cards, *_notes(report.currency_notes)]
    sections = [Section(tuple(lead))]
    if goal_milestones:
        sections.append(
            Section(
                (
                    Heading("Savings goals reaching their target"),
                    *_notes(plan_goal_text(item) for item in goal_milestones),
                ),
                name="plan-goals",
            )
        )
    sections.append(
        Section(
            (
                Heading("Cash outlook"),
                Table(
                    (Column("Cash source / use"), *period_columns, *total_column),
                    tuple(summary_rows),
                ),
            ),
            name="plan-summary",
        )
    )
    sections.append(
        Section(
            (
                Heading("Budget and classifications"),
                Table((Column("Category"), *period_columns, *total_column), tuple(detail_rows)),
            ),
            name="plan-detail",
            optional=True,
        )
    )
    subtitle = (
        f"{activity.start.isoformat()} through {activity.end.isoformat()} · "
        f"{activity.period.value.title()} · {measure.value.title()} · {scenario_name}"
    )
    return ReportDocument(
        "Plan",
        subtitle,
        tuple(sections),
        optional_label="Include category detail when printing",
        kind="plan",
    )


# ------------------------------------------------------------- Projection

_CHART_COLOURS = ("#2f6fd0", "#1c7a4a", "#8b4ab8", "#d47817")


def projection_layout(
    result: Projection,
    *,
    comparison: Projection | None = None,
    book_name: str = "",
) -> ReportDocument:
    """The current Projection result, comparison, and annual assumptions."""
    shortfall = result.first_shortfall()
    cards = Cards(
        (
            Card(
                "Ending net worth", money_text(result.ending_net_worth), result.ending_net_worth < 0
            ),
            Card("Ending cash", money_text(result.ending_cash), result.ending_cash < 0),
            Card("Lowest cash", money_text(result.minimum_cash), result.minimum_cash < 0),
            Card("Total growth", money_text(result.total("investment_growth"))),
            Card("Contributions", money_text(result.total("contributions"))),
            Card("Taxable withdrawals", money_text(result.total("withdrawals"))),
            Card("Retirement distributions", money_text(result.total("retirement_distributions"))),
            Card("Investment income", money_text(result.total("investment_income"))),
            Card("Investment fees", money_text(result.total("investment_fees"))),
            Card("Retirement rollovers", money_text(result.total("rollovers"))),
            Card("Cash runs out", shortfall.label if shortfall else "Never", shortfall is not None),
        )
    )
    assumptions = result.scenario.assumptions_for(result.scenario.start)
    sources = result.scenario.assumption_sources(result.scenario.start)
    assumption_table = Table(
        _columns("Assumption", "#Annual rate", "Source"),
        tuple(
            TableRow(
                (
                    Cell(label),
                    Cell(f"{getattr(assumptions, key):.2%}", numeric=True),
                    Cell(sources[key]),
                )
            )
            for key, label in (
                ("income_growth", "Income growth"),
                ("expense_inflation", "Expense inflation"),
                ("investment_return", "Investment return"),
                ("cash_interest", "Cash interest"),
                ("liability_interest", "Liability interest"),
            )
        ),
    )
    year_end = Table(
        _columns(
            "Month", "#Income", "#Expense", "#Cash", "#Holdings", "#Liabilities", "#Net worth"
        ),
        tuple(
            TableRow(
                (
                    Cell(row.label),
                    _amount(row.income),
                    _amount(row.expense),
                    _amount(row.cash_close),
                    _amount(row.holdings),
                    _amount(row.liabilities),
                    _amount(row.net_worth),
                )
            )
            for row in result.rows[11::12]
        ),
    )
    blocks: list[Block] = [cards]
    if result.warnings:
        blocks.append(Paragraph("  ".join(result.warnings), warning=True))
    goal_notes = projection_goal_notes(result)
    if goal_notes:
        blocks += [Heading("Savings goals"), *_notes(goal_notes)]
    escrow = [
        f"{row.label}: {message}"
        for row in result.rows
        for message in row.ledger.escrow_explanations
    ]
    if escrow:
        blocks += [Heading("Escrow treatment"), *(Paragraph(item, bullet=True) for item in escrow)]
    blocks += [
        Heading("Projection chart"),
        _projection_chart(result, comparison),
        Heading("Annual assumptions"),
        assumption_table,
    ]
    if comparison is not None:
        blocks += _projection_comparison(result, comparison)
    blocks += [Heading("Year-end values"), year_end]
    start = result.rows[0].month.isoformat() if result.rows else str(result.scenario.start or "")
    end = result.rows[-1].month.isoformat() if result.rows else ""
    prefix = f"{book_name} · " if book_name else ""
    subtitle = f"{prefix}{result.scenario.name} · {start} through {end}"
    return ReportDocument("Projection", subtitle, (Section(tuple(blocks)),), kind="projection")


def _projection_chart(result: Projection, comparison: Projection | None) -> Chart:
    series = [
        ("Cash", [row.cash_close for row in result.rows]),
        ("Investments", [row.holdings for row in result.rows]),
        ("Net worth", [row.net_worth for row in result.rows]),
    ]
    if comparison is not None:
        series.append(
            (f"{comparison.scenario.name} net worth", [row.net_worth for row in comparison.rows])
        )
    ticks: tuple[tuple[int, str], ...] = ()
    if result.rows:
        indices = sorted({0, len(result.rows) // 2, len(result.rows) - 1})
        ticks = tuple((index, result.rows[index].label) for index in indices)
    return Chart(
        "Projection chart",
        tuple(
            ChartSeries(name, tuple(float(value.to_decimal()) for value in values), colour)
            for (name, values), colour in zip(series, _CHART_COLOURS, strict=False)
        ),
        ticks,
    )


def _projection_comparison(result: Projection, comparison: Projection) -> list[Block]:
    rows = []
    for primary, compared in zip_longest(result.rows[11::12], comparison.rows[11::12]):
        if primary is None or compared is None:
            continue
        rows.append(
            TableRow(
                (
                    Cell(primary.label),
                    _amount(primary.cash_close),
                    _amount(compared.cash_close),
                    _amount(primary.cash_close - compared.cash_close),
                    _amount(primary.net_worth),
                    _amount(compared.net_worth),
                    _amount(primary.net_worth - compared.net_worth),
                )
            )
        )
    name, other = result.scenario.name, comparison.scenario.name
    return [
        Heading(f"{name} versus {other}"),
        Table(
            _columns(
                "Month",
                f"#{name} cash",
                f"#{other} cash",
                "#Cash difference",
                f"#{name} net worth",
                f"#{other} net worth",
                "#Net worth difference",
            ),
            tuple(rows),
        ),
    ]
