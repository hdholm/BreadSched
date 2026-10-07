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

from ...gen.engine.activity import PlanMeasure
from ...gen.engine.category_report import CategoryReport
from ...gen.engine.completeness import Completeness, combine
from ...gen.engine.dashboard import Dashboard, MissedGroup
from ...gen.engine.projection_result import Projection
from ...gen.lib.account import AccountClass
from ...gen.lib.money import Money
from ...gen.services.expense_explorer import ExpenseDrilldown, ExpenseExplorer, SpendingPoint
from ...gen.services.net_worth import NetWorthChange, NetWorthHistory
from ...gen.services.plan import PlanGoalMilestone
from ...presentation import (
    PLAN_REIMBURSABLE_HEADING,
    goal_status_text,
    plan_goal_text,
    plan_reimbursable_text,
    projection_goal_notes,
    reimbursement_outlook_text,
    runway_comparison_text,
    runway_lines,
)

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
    "expense_explorer_layout",
    "net_worth_change_layout",
    "net_worth_history_layout",
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
    and ``below`` puts it on its own line; ``hint`` is hover text on screen. Text
    may hold line breaks. ``span`` is how many columns the cell covers.
    """

    text: str
    numeric: bool = False
    negative: bool = False
    indent: int = 0
    note: str = ""
    below: bool = False
    hint: str = ""
    span: int = 1


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


def _completeness(coverage: Completeness, what: str) -> tuple[Paragraph, ...]:
    """A partial or withheld value's label, what it leaves out, and the fix (#236)."""
    if coverage.complete:
        return ()
    return (
        Paragraph(f"{what}: {coverage.label}.", warning=True),
        *_notes(coverage.detail(), warning=True),
    )


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
    if summary["receivables_owed"] > 0:
        owed = money_text(summary["receivables_owed"])
        if summary["receivables_attention"] > 0:
            owed += f" ({money_text(summary['receivables_attention'])} disputed or overdue)"
        cards.append(Card("Reimbursements due", owed, summary["receivables_attention"] > 0))
    if summary["fsa_claims_attention"]:
        cards.append(
            Card("FSA claims needing attention", str(summary["fsa_claims_attention"]), True)
        )
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
        *_completeness(board.completeness, "Net worth"),
        *(
            _completeness(board.liquid_completeness, "Liquid cash")
            if board.liquid_completeness.excluded != board.completeness.excluded
            else ()
        ),
        Heading("Pending bills"),
        bills,
        Heading("Expected income"),
        income,
    ]
    if board.claim_alerts:
        blocks += [
            Heading("FSA claims needing attention"),
            Table(
                _columns("Service date", "Provider", "Status", "#Remaining", "Needs attention"),
                tuple(
                    TableRow(
                        (
                            Cell(line.summary.claim.service_date.isoformat()),
                            Cell(line.summary.claim.provider),
                            Cell(line.summary.status.label),
                            _amount(line.summary.remaining_reimbursable),
                            Cell("\n".join(item.text for item in line.attention)),
                        )
                    )
                    for line in board.claim_alerts
                ),
            ),
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
                f"Planned change through {report.as_of.isoformat()}",
                signed_money_text(report.planned_cash_through_as_of),
                (report.planned_cash_through_as_of or Money(0)) < 0,
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
    lead: list[Block] = [
        cards,
        *_completeness(report.completeness, "Plan totals"),
        *_notes(report.currency_notes),
    ]
    sections = [Section(tuple(lead))]
    if report.reimbursable_categories:
        sections.append(
            Section(
                (
                    Heading(PLAN_REIMBURSABLE_HEADING),
                    *_notes(plan_reimbursable_text(row) for row in report.reimbursable_categories),
                ),
                name="plan-reimbursable",
            )
        )
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
    blocks: list[Block] = [
        cards,
        *_completeness(
            combine(
                result.completeness, *(() if comparison is None else (comparison.completeness,))
            ),
            "Projection",
        ),
    ]
    if result.warnings:
        blocks.append(Paragraph("  ".join(result.warnings), warning=True))
    blocks += [Heading("Cash runway"), *_notes(runway_lines(result.runway()))]
    goal_notes = projection_goal_notes(result)
    if goal_notes:
        blocks += [Heading("Savings goals"), *_notes(goal_notes)]
    if result.reimbursements:
        blocks += [
            Heading("Reimbursable expenses: gross and net cost"),
            *_notes(reimbursement_outlook_text(item) for item in result.reimbursements),
        ]
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
                    Cell(
                        combine(
                            result.month_completeness(primary.index),
                            comparison.month_completeness(compared.index),
                        ).label
                        or "—"
                    ),
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
                "Coverage",
            ),
            tuple(rows),
        ),
        *_notes([runway_comparison_text(name, result.runway(), other, comparison.runway())]),
    ]


# ------------------------------------------------------ Expense Explorer


def _over_time(
    title: str, points: tuple[SpendingPoint, ...], names: dict[str, str], as_of: str
) -> list[Block]:
    """Total plan and actual by period, with actual split by top-level category."""
    parts = points[0].categories if points else ()
    rows = []
    for point in points:
        notes = [
            text
            for flag, text in (
                (point.partial, "to date"),
                (point.future, "future"),
                (point.currency_incomplete, point.completeness.label or "missing quote"),
            )
            if flag
        ]
        rows.append(
            TableRow(
                (
                    Cell(point.label),
                    _amount(point.planned),
                    _amount(point.actual),
                    *(_amount(amount) for _handle, amount in point.categories),
                    Cell(", ".join(notes) or "—"),
                )
            )
        )
    return [
        Heading(title),
        Paragraph(
            "Total plan and actual by period, with actual split by top-level category. "
            f"Actual is posted through {as_of}."
        ),
        Table(
            (
                Column("Period"),
                Column("Plan", True),
                Column("Actual", True),
                *(Column(names.get(handle, handle), True) for handle, _ in parts),
                Column("Note"),
            ),
            tuple(rows),
        ),
    ]


def _income_detail(explorer: ExpenseExplorer, detail: ExpenseDrilldown) -> list[Block]:
    """The dated planned occurrences and receipts behind one income period."""
    name = next(
        (row.full_name for row in explorer.income_categories if row.account == detail.account),
        detail.account,
    )
    return [
        Heading(f"Income detail — {name} — {detail.period.label}"),
        Table(
            _columns("Planned date", "Scheduled", "#Expected"),
            tuple(
                TableRow(
                    (
                        Cell(item.planned_date.isoformat()),
                        Cell(item.description),
                        _amount(item.expected),
                    )
                )
                for item in detail.planned_events
            ),
        ),
        Table(
            _columns("Received", "Payer", "#Actual"),
            tuple(
                TableRow((Cell(item.post_date.isoformat()), Cell(group.name), _amount(item.amount)))
                for group in detail.merchants
                for item in group.transactions
            ),
        ),
        Paragraph(
            f"Planned {money_text(detail.period.planned)}; "
            f"actual {money_text(detail.period.actual)}."
        ),
    ]


def expense_explorer_layout(
    explorer: ExpenseExplorer, income_detail: ExpenseDrilldown | None = None
) -> ReportDocument:
    """The selected expense cell and its shared Plan breakdown.

    ``income_detail``, an income category's drilldown for the same period, adds the
    dated occurrences and receipts behind it.
    """
    detail = explorer.drilldown
    if detail is None:
        raise ValueError("expense printout requires a selected category and period")
    category = next(item for item in explorer.categories if item.account == detail.account)
    selected = detail.period

    def remaining(item) -> Cell:
        if item.remaining is not None:
            return _amount(item.remaining)
        return Cell(item.remaining_reason or "—", numeric=True)

    def values(label: str, item) -> TableRow:
        return TableRow(
            (
                Cell(label),
                _amount(item.planned),
                _amount(item.actual),
                _amount(item.variance),
                _amount(item.carry_in),
                remaining(item),
            )
        )

    index = next(i for i, item in enumerate(category.periods) if item.start == selected.start)
    value_columns = ("#Plan", "#Period actual", "#Period variance", "#Carry in", "#Remaining")
    as_of = explorer.plan.report.as_of.isoformat()
    blocks: list[Block] = [
        *_over_time(
            "Spending over time",
            explorer.spending,
            {row.account: row.full_name for row in explorer.categories},
            as_of,
        ),
        *_over_time(
            "Income over time",
            explorer.income,
            {row.account: row.full_name for row in explorer.income_categories},
            as_of,
        ),
        Heading("Category comparison"),
        Paragraph(
            "Remaining uses actual through the as-of date; Period actual and Period "
            "variance count everything dated in the period, even after that date. "
            "Carry in is shown only when rollover is enabled."
        ),
        Table(
            _columns("Category", *value_columns),
            tuple(values(row.full_name, row.periods[index]) for row in explorer.categories),
        ),
        Heading(f"{category.full_name} trend"),
        Table(
            _columns("Period", *value_columns),
            tuple(values(item.label, item) for item in category.periods),
        ),
        Heading("Merchants — actual only"),
        Paragraph("Category plan is unallocated across merchants."),
        Table(
            _columns("Merchant", "#Actual", "Transactions"),
            tuple(
                TableRow(
                    (
                        Cell(group.name),
                        _amount(group.amount),
                        Cell(
                            "\n".join(
                                f"{item.post_date.isoformat()} · "
                                f"{item.description.strip() or 'Unknown merchant'} · "
                                f"{money_text(item.amount)}"
                                for item in group.transactions
                            )
                        ),
                    )
                )
                for group in detail.merchants
            ),
        ),
    ]
    if income_detail is not None and income_detail.income:
        blocks += _income_detail(explorer, income_detail)
    subtitle = (
        f"{explorer.plan.scenario.name} · {explorer.plan.start.isoformat()} through "
        f"{explorer.plan.end.isoformat()} · {selected.label}"
    )
    return ReportDocument(
        "Expense Explorer", subtitle, (Section(tuple(blocks)),), kind="expense-explorer"
    )


# -------------------------------------------------------------- Net worth


def net_worth_history_layout(history: NetWorthHistory) -> ReportDocument:
    """Net worth at each period end, with each point's top-level breakdown."""
    rows = []
    for point in history.points:
        notes = [
            text
            for text in (
                "to date" if point.partial else "",
                f"missing quote: {', '.join(point.missing)}" if point.missing else "",
                *point.completeness.detail(),
            )
            if text
        ]
        breakdown = "\n".join(
            f"{line.name} ({line.kind}): {money_text(line.value)}" for line in point.lines
        )
        rows.append(
            TableRow(
                (
                    Cell(point.label),
                    Cell(point.valued_on.isoformat()),
                    _amount(point.assets),
                    _amount(point.debts),
                    _amount(point.net_worth),
                    _amount(point.change, signed=True),
                    Cell("; ".join(notes) or "—"),
                    Cell(breakdown or "—"),
                )
            )
        )
    blocks: tuple[Block, ...] = (
        Paragraph(
            "Assets less debts, market-valued at each period end. A point with a missing "
            "quote shows — and names the account rather than guessing a conversion."
        ),
        Table(
            _columns(
                "Period",
                "Valued on",
                "#Assets",
                "#Debts",
                "#Net worth",
                "#Change",
                "Note",
                "Top-level accounts",
            ),
            tuple(rows),
        ),
    )
    subtitle = (
        f"{history.start.isoformat()} through {history.end.isoformat()} · "
        f"by {history.period.value} · valued through {history.as_of.isoformat()}"
    )
    return ReportDocument(
        "Net worth history", subtitle, (Section(blocks),), kind="net-worth-history"
    )


def net_worth_change_layout(change: NetWorthChange) -> ReportDocument:
    """The postings behind one net worth change and what they reconcile to."""
    rows = [
        TableRow(
            (
                Cell(posting.posted.isoformat()),
                Cell(posting.description),
                Cell("; ".join(posting.accounts)),
                Cell(posting.currency),
                _amount(posting.effect),
                Cell("missing quote" if posting.effect is None else ""),
            )
        )
        for posting in change.postings
    ]
    for number, (label, value) in enumerate(
        (
            (f"Opening net worth ({change.opening_on.isoformat()})", change.opening),
            ("Postings", change.posted),
            ("Market and exchange-rate changes", change.revaluation),
            (f"Closing net worth ({change.closing_on.isoformat()})", change.closing),
            ("Change", change.change),
        )
    ):
        rows.append(
            TableRow(
                (Cell(label, span=4), _amount(value), Cell("")),
                "total" if number == 0 else "heading",
            )
        )
    notes = [
        "Each posting is the net of its splits in asset and debt accounts, converted "
        "with the quote applicable on its date. Market and exchange-rate changes are "
        "the rest of the change."
    ]
    if change.transfers:
        notes.append(
            f"{change.transfers} transfer(s) between your own accounts left out: "
            "they do not change net worth."
        )
    if change.missing:
        notes.append(f"Missing quote: {', '.join(change.missing)}. Totals are withheld.")
    blocks: tuple[Block, ...] = (
        *_notes(notes),
        *_completeness(change.completeness, "Change"),
        Table(
            _columns("Date", "Description", "Accounts", "Currency", "#Net worth effect", "Note"),
            tuple(rows),
        ),
    )
    subtitle = f"{change.start.isoformat()} through {change.closing_on.isoformat()}" + (
        " · to date" if change.partial else ""
    )
    return ReportDocument("Net worth change", subtitle, (Section(blocks),), kind="net-worth-change")
