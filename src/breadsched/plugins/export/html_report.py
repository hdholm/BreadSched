"""Self-contained printable HTML reports for the household views.

The report functions consume the same engine results that GTK renders.  They do
not query the database or recalculate financial values, so a printout describes
the applied view state rather than a subtly different second report. Dashboard,
Plan, and Projection are laid out once in ``report_layout``; this module renders
that layout as HTML, and the desktop's native printing draws the same layout.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from ...gen.engine.activity import CategoryReport, PlanMeasure
from ...gen.engine.dashboard import Dashboard
from ...gen.engine.projection import Projection
from ...gen.lib.money import Money
from ...gen.services.expense_explorer import ExpenseDrilldown, ExpenseExplorer, SpendingPoint
from ...gen.services.net_worth import NetWorthChange, NetWorthHistory
from ...gen.services.plan import PlanGoalMilestone
from .report_layout import (
    Cards,
    Cell,
    Chart,
    Heading,
    Paragraph,
    ReportDocument,
    Table,
    dashboard_layout,
    money_text,
    plan_layout,
    projection_layout,
    signed_money_text,
)

__all__ = [
    "dashboard_report",
    "expense_explorer_report",
    "plan_report",
    "projection_report",
    "render_html",
]


_STYLE = """
@page { size: landscape; margin: 12mm; }
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; color: #1c1f24; font: 10pt/1.35 system-ui, sans-serif; }
h1 { margin: 0; font-size: 20pt; }
h2 { margin: 18px 0 7px; font-size: 13pt; }
.meta { color: #5d6470; margin: 3px 0 14px; }
.cards { display: flex; flex-wrap: wrap; gap: 7px; }
.card { border: 1px solid #cfd3d9; border-radius: 5px; min-width: 135px;
        padding: 6px 9px; break-inside: avoid; }
.card small { color: #5d6470; display: block; }
.card strong { font-size: 14pt; font-variant-numeric: tabular-nums; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { border-bottom: 1px solid #dfe2e6; padding: 4px 6px; text-align: left;
         vertical-align: top; }
th { color: #535a65; font-size: 8.5pt; }
.num { text-align: right; white-space: nowrap; }
.neg { color: #9d201b; }
.section td { background: #eef0f3; font-weight: 700; }
.total td { border-top: 1px solid #8b919b; font-weight: 700; }
.grand td { border-top: 2px solid #4e5560; font-weight: 700; }
.heading td { font-weight: 700; }
.note { color: #5d6470; }
.warnings { color: #9d201b; }
.screen-tools { background: #eef4fc; border-bottom: 1px solid #b8c9e2; padding: 10px;
                margin-bottom: 14px; }
.screen-tools button { font: inherit; padding: 5px 12px; }
.screen-tools label { margin-left: 14px; }
.plan-summary { margin-top: 14px; }
.plan-detail { margin-top: 18px; }
svg { width: 100%; height: auto; }
@media print {
  .screen-tools { display: none; }
  thead { display: table-header-group; }
  thead th { position: static; }
  tr, .card { break-inside: avoid; }
  .plan-summary th, .plan-summary td { padding: 5px 7px; }
  .plan-detail { display: none; }
  body.include-plan-detail .plan-detail { display: block; break-before: page; }
}
"""


def _document(
    title: str,
    subtitle: str,
    body: str,
    *,
    optional_plan_detail: bool = False,
    optional_label: str = "Include category detail when printing",
) -> str:
    safe_title = escape(title)
    detail_control = ""
    if optional_plan_detail:
        detail_control = (
            '<label><input type="checkbox" onchange="document.body.classList.toggle('
            f"'include-plan-detail',this.checked)\"> {escape(optional_label)}</label>"
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width"><title>{safe_title}</title>'
        f"<style>{_STYLE}</style></head><body>"
        '<div class="screen-tools"><button type="button" onclick="window.print()">Print</button> '
        "This preview contains the applied report values. Use the print dialog to choose a "
        f"printer or save a PDF.{detail_control}</div>"
        f'<h1>{safe_title}</h1><p class="meta">{escape(subtitle)}</p>{body}'
        '<script>window.addEventListener("load",()=>setTimeout(()=>window.print(),100));</script>'
        "</body></html>"
    )


_money = money_text
_signed_money = signed_money_text


def _amount(value: Money | None) -> str:
    css = "num"
    if value is not None and value < 0:
        css += " neg"
    return f'<td class="{css}">{escape(_money(value))}</td>'


def _signed_amount(value: Money | None) -> str:
    css = "num"
    if value is not None:
        css += " neg" if value < 0 else " pos" if value > 0 else ""
    return f'<td class="{css}">{escape(_signed_money(value))}</td>'


def _cards(items: list[tuple[str, object, bool]]) -> str:
    cards = []
    for label, value, alarm in items:
        css = "neg" if alarm else ""
        cards.append(
            '<div class="card">'
            f'<small>{escape(label)}</small><strong class="{css}">{escape(str(value))}</strong>'
            "</div>"
        )
    return f'<div class="cards">{"".join(cards)}</div>'


def _over_time_table(
    title: str, points: tuple[SpendingPoint, ...], names: dict[str, str], as_of: str
) -> str:
    """Total plan and actual by period, with actual split by top-level category."""
    parts = points[0].categories if points else ()
    rows = []
    for point in points:
        notes = [
            text
            for flag, text in (
                (point.partial, "to date"),
                (point.future, "future"),
                (point.currency_incomplete, "missing quote"),
            )
            if flag
        ]
        rows.append(
            f"<tr><td>{escape(point.label)}</td>{_amount(point.planned)}{_amount(point.actual)}"
            + "".join(_amount(amount) for _handle, amount in point.categories)
            + f"<td>{escape(', '.join(notes) or '—')}</td></tr>"
        )
    return (
        f"<h2>{escape(title)}</h2><p class='note'>Total plan and actual by period, "
        f"with actual split by top-level category. Actual is posted through {as_of}.</p>"
        '<table><thead><tr><th>Period</th><th class="num">Plan</th><th class="num">Actual</th>'
        + "".join(
            f'<th class="num">{escape(names.get(handle, handle))}</th>' for handle, _ in parts
        )
        + f"<th>Note</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _income_detail(explorer: ExpenseExplorer, detail: ExpenseDrilldown) -> str:
    """The dated planned occurrences and receipts behind one income period."""
    name = next(
        (row.full_name for row in explorer.income_categories if row.account == detail.account),
        detail.account,
    )
    planned = "".join(
        f"<tr><td>{item.planned_date.isoformat()}</td><td>{escape(item.description)}</td>"
        f"{_amount(item.expected)}</tr>"
        for item in detail.planned_events
    )
    received = "".join(
        f"<tr><td>{item.post_date.isoformat()}</td><td>{escape(group.name)}</td>"
        f"{_amount(item.amount)}</tr>"
        for group in detail.merchants
        for item in group.transactions
    )
    return (
        f"<h2>Income detail — {escape(name)} — {escape(detail.period.label)}</h2>"
        "<table><thead><tr><th>Planned date</th><th>Scheduled</th>"
        f'<th class="num">Expected</th></tr></thead><tbody>{planned}</tbody></table>'
        "<table><thead><tr><th>Received</th><th>Payer</th>"
        f'<th class="num">Actual</th></tr></thead><tbody>{received}</tbody></table>'
        f"<p class='note'>Planned {escape(_money(detail.period.planned))}; actual "
        f"{escape(_money(detail.period.actual))}.</p>"
    )


def expense_explorer_report(
    explorer: ExpenseExplorer, income_detail: ExpenseDrilldown | None = None
) -> str:
    """Print the selected expense cell and its shared Plan breakdown.

    ``income_detail``, an income category's drilldown for the same period, adds the
    dated occurrences and receipts behind it.
    """
    detail = explorer.drilldown
    if detail is None:
        raise ValueError("expense printout requires a selected category and period")
    category = next(item for item in explorer.categories if item.account == detail.account)
    selected = detail.period
    comparison_rows = []

    def remaining_cell(item) -> str:
        value = (
            _money(item.remaining) if item.remaining is not None else item.remaining_reason or "—"
        )
        return f'<td class="num">{escape(value)}</td>'

    index = next(i for i, item in enumerate(category.periods) if item.start == selected.start)
    for row in explorer.categories:
        value = row.periods[index]
        comparison_rows.append(
            f"<tr><td>{escape(row.full_name)}</td>{_amount(value.planned)}"
            f"{_amount(value.actual)}{_amount(value.variance)}"
            f"{_amount(value.carry_in)}{remaining_cell(value)}</tr>"
        )
    comparison = (
        "<h2>Category comparison</h2><p class='note'>Remaining uses actual through "
        "the as-of date; Actual and Variance show full-period values. "
        "Carry in is shown only when rollover is enabled.</p>"
        "<table><thead><tr><th>Category</th>"
        '<th class="num">Plan</th><th class="num">Actual</th>'
        '<th class="num">Variance</th><th class="num">Carry in</th>'
        '<th class="num">Remaining</th></tr></thead><tbody>'
        f"{''.join(comparison_rows)}</tbody></table>"
    )
    trend_rows = "".join(
        f"<tr><td>{escape(item.label)}</td>{_amount(item.planned)}"
        f"{_amount(item.actual)}{_amount(item.variance)}"
        f"{_amount(item.carry_in)}{remaining_cell(item)}</tr>"
        for item in category.periods
    )
    trend = (
        f"<h2>{escape(category.full_name)} trend</h2><table><thead><tr><th>Period</th>"
        '<th class="num">Plan</th><th class="num">Actual</th>'
        f'<th class="num">Variance</th><th class="num">Carry in</th>'
        f'<th class="num">Remaining</th></tr></thead>'
        f"<tbody>{trend_rows}</tbody></table>"
    )
    merchant_rows = []
    for group in detail.merchants:
        merchant_rows.append(
            f"<tr><td>{escape(group.name)}</td>{_amount(group.amount)}<td>"
            + "<br>".join(
                f"{item.post_date.isoformat()} · "
                f"{escape(item.description.strip() or 'Unknown merchant')}"
                f" · {escape(_money(item.amount))}"
                for item in group.transactions
            )
            + "</td></tr>"
        )
    merchants = (
        "<h2>Merchants — actual only</h2><p class='note'>Category plan is unallocated "
        "across merchants.</p><table><thead><tr><th>Merchant</th>"
        '<th class="num">Actual</th><th>Transactions</th></tr></thead><tbody>'
        f"{''.join(merchant_rows)}</tbody></table>"
    )
    as_of = explorer.plan.report.as_of.isoformat()
    spending = _over_time_table(
        "Spending over time",
        explorer.spending,
        {row.account: row.full_name for row in explorer.categories},
        as_of,
    ) + _over_time_table(
        "Income over time",
        explorer.income,
        {row.account: row.full_name for row in explorer.income_categories},
        as_of,
    )
    subtitle = (
        f"{explorer.plan.scenario.name} · {explorer.plan.start.isoformat()} through "
        f"{explorer.plan.end.isoformat()} · {escape(selected.label)}"
    )
    income = (
        _income_detail(explorer, income_detail)
        if income_detail is not None and income_detail.income
        else ""
    )
    return _document(
        "Expense Explorer", subtitle, spending + comparison + trend + merchants + income
    )


def net_worth_history_report(history: NetWorthHistory) -> str:
    """Print net worth at each period end, with each point's top-level breakdown."""
    rows = []
    for point in history.points:
        notes = [
            text
            for text in (
                "to date" if point.partial else "",
                f"missing quote: {', '.join(point.missing)}" if point.missing else "",
            )
            if text
        ]
        breakdown = "<br>".join(
            escape(f"{line.name} ({line.kind}): {_money(line.value)}") for line in point.lines
        )
        rows.append(
            f"<tr><td>{escape(point.label)}</td><td>{point.valued_on.isoformat()}</td>"
            f"{_amount(point.assets)}{_amount(point.debts)}{_amount(point.net_worth)}"
            f"<td class='num'>{escape(_signed_money(point.change))}</td>"
            f"<td>{escape('; '.join(notes) or '—')}</td><td>{breakdown or '—'}</td></tr>"
        )
    body = (
        "<p class='note'>Assets less debts, market-valued at each period end. A point "
        "with a missing quote shows — and names the account rather than guessing a "
        "conversion.</p><table><thead><tr><th>Period</th><th>Valued on</th>"
        '<th class="num">Assets</th><th class="num">Debts</th><th class="num">Net worth</th>'
        '<th class="num">Change</th><th>Note</th><th>Top-level accounts</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    subtitle = (
        f"{history.start.isoformat()} through {history.end.isoformat()} · "
        f"by {history.period.value} · valued through {history.as_of.isoformat()}"
    )
    return _document("Net worth history", subtitle, body)


def net_worth_change_report(change: NetWorthChange) -> str:
    """Print the postings behind one net worth change and what they reconcile to."""
    rows = "".join(
        f"<tr><td>{posting.posted.isoformat()}</td><td>{escape(posting.description)}</td>"
        f"<td>{escape('; '.join(posting.accounts))}</td><td>{escape(posting.currency)}</td>"
        f"{_amount(posting.effect)}"
        f"<td>{'missing quote' if posting.effect is None else ''}</td></tr>"
        for posting in change.postings
    )
    totals = "".join(
        f"<tr><th colspan='4'>{escape(label)}</th>{_amount(value)}<td></td></tr>"
        for label, value in (
            (f"Opening net worth ({change.opening_on.isoformat()})", change.opening),
            ("Postings", change.posted),
            ("Market and exchange-rate changes", change.revaluation),
            (f"Closing net worth ({change.closing_on.isoformat()})", change.closing),
            ("Change", change.change),
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
    body = (
        "".join(f"<p class='note'>{escape(note)}</p>" for note in notes)
        + "<table><thead><tr><th>Date</th><th>Description</th><th>Accounts</th>"
        '<th>Currency</th><th class="num">Net worth effect</th><th>Note</th></tr></thead>'
        f"<tbody>{rows}</tbody><tfoot>{totals}</tfoot></table>"
    )
    subtitle = f"{change.start.isoformat()} through {change.closing_on.isoformat()}" + (
        " · to date" if change.partial else ""
    )
    return _document("Net worth change", subtitle, body)


def dashboard_report(board: Dashboard, *, book_name: str = "") -> str:
    """Render the currently built Dashboard as printable HTML."""
    return render_html(dashboard_layout(board, book_name=book_name))


def plan_report(
    report: CategoryReport,
    measure: PlanMeasure,
    *,
    scenario_name: str,
    book_name: str = "",
    goal_milestones: Sequence[PlanGoalMilestone] = (),
) -> str:
    """Render the applied Plan horizon, grouping, measure, and scenario."""
    return render_html(
        plan_layout(
            report,
            measure,
            scenario_name=scenario_name,
            book_name=book_name,
            goal_milestones=goal_milestones,
        )
    )


def projection_report(
    result: Projection,
    *,
    comparison: Projection | None = None,
    book_name: str = "",
) -> str:
    """Render the current Projection result, comparison, and annual assumptions."""
    return render_html(projection_layout(result, comparison=comparison, book_name=book_name))


# ------------------------------------------------------- layout rendering


def render_html(document: ReportDocument) -> str:
    """A report layout as one self-contained, print-ready HTML page."""
    body = []
    for section in document.sections:
        inner = "".join(_block_html(block) for block in section.blocks)
        if section.name:
            body.append(f'<section class="{section.name}">{inner}</section>')
        else:
            body.append(inner)
    return _document(
        document.title,
        document.subtitle,
        "".join(body),
        optional_plan_detail=document.has_optional,
        optional_label=document.optional_label,
    )


def _block_html(block) -> str:
    if isinstance(block, Heading):
        return f"<h2>{escape(block.text)}</h2>"
    if isinstance(block, Paragraph):
        if block.bullet:
            return f"<ul><li>{escape(block.text)}</li></ul>"
        css = "warnings" if block.warning else "note"
        return f'<p class="{css}">{escape(block.text)}</p>'
    if isinstance(block, Cards):
        return _cards([(card.label, card.value, card.alarm) for card in block.items])
    if isinstance(block, Chart):
        return _chart_svg(block)
    return _table_html(block)


def _cell_html(cell: Cell, columns: int = 1) -> str:
    classes = []
    if cell.numeric:
        classes.append("num")
    if cell.negative:
        classes.append("neg")
    attrs = f' class="{" ".join(classes)}"' if classes else ""
    if columns > 1:
        attrs += f' colspan="{columns}"'
    text = "&nbsp;" * (cell.indent * 4) + escape(cell.text)
    if cell.hint:
        text = f'<span title="{escape(cell.hint, quote=True)}">{text}</span>'
    if cell.note:
        note = f'<span class="note">{escape(cell.note)}</span>'
        text += f"<br>{note}" if cell.below else f" {note}"
    return f"<td{attrs}>{text}</td>"


def _table_html(table: Table) -> str:
    if not table.rows and table.empty:
        return f'<p class="note">{escape(table.empty)}</p>'
    header = "".join(
        f'<th class="num">{escape(column.label)}</th>'
        if column.numeric
        else f"<th>{escape(column.label)}</th>"
        for column in table.columns
    )
    rows = []
    for row in table.rows:
        css = f' class="{row.style}"' if row.style else ""
        if row.style == "section":
            cells = _cell_html(row.cells[0], len(table.columns))
        else:
            cells = "".join(_cell_html(cell) for cell in row.cells)
        rows.append(f"<tr{css}>{cells}</tr>")
    return f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _chart_svg(chart: Chart) -> str:
    values = [value for series in chart.series for value in series.values]
    if not values:
        return '<p class="note">No projected values.</p>'
    low, high = min(values), max(values)
    if low == high:
        low -= 1
        high += 1
    width, height = 1000, 340
    left, top, right, bottom = 72, 28, 18, 42
    usable_width = width - left - right
    usable_height = height - top - bottom
    count = max((len(series.values) for series in chart.series), default=1)

    def point(index: int, value: float) -> str:
        x = left + usable_width * index / max(1, count - 1)
        y = top + usable_height * (high - value) / (high - low)
        return f"{x:.1f},{y:.1f}"

    lines = []
    legend = []
    for index, series in enumerate(chart.series):
        coords = " ".join(point(i, value) for i, value in enumerate(series.values))
        lines.append(
            f'<polyline fill="none" stroke="{series.colour}" stroke-width="2" points="{coords}"/>'
        )
        legend.append(
            f'<line x1="{left + index * 210}" y1="12" x2="{left + index * 210 + 18}" '
            f'y2="12" stroke="{series.colour}" stroke-width="3"/>'
            f'<text x="{left + index * 210 + 24}" y="16" font-size="12">'
            f"{escape(series.name)}</text>"
        )
    zero = ""
    if low <= 0 <= high:
        zero_y = top + usable_height * high / (high - low)
        zero = (
            f'<line x1="{left}" y1="{zero_y:.1f}" x2="{width - right}" '
            f'y2="{zero_y:.1f}" stroke="#aeb3ba" stroke-dasharray="4 4"/>'
        )
    labels = []
    for index, label in chart.ticks:
        x = left + usable_width * index / max(1, count - 1)
        labels.append(
            f'<text x="{x:.1f}" y="{height - 10}" text-anchor="middle" '
            f'font-size="11">{escape(label)}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(chart.label)}">'
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" '
        'stroke="#aeb3ba"/>'
        f'<line x1="{left}" y1="{height - bottom}" x2="{width - right}" '
        f'y2="{height - bottom}" stroke="#aeb3ba"/>{zero}{"".join(lines)}'
        f"{''.join(legend)}{''.join(labels)}</svg>"
    )
