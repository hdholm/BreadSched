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

from ...gen.engine.activity import PlanMeasure
from ...gen.engine.category_report import CategoryReport
from ...gen.engine.chart_model import ChartModel
from ...gen.engine.dashboard import Dashboard
from ...gen.engine.projection_result import Projection
from ...gen.services.expense_explorer import ExpenseDrilldown, ExpenseExplorer
from ...gen.services.net_worth import NetWorthChange, NetWorthHistory
from ...gen.services.plan import PlanGoalMilestone
from .report_layout import (
    Cards,
    Cell,
    Chart,
    Heading,
    ModelChart,
    Paragraph,
    ReportDocument,
    Table,
    dashboard_layout,
    expense_explorer_layout,
    net_worth_change_layout,
    net_worth_history_layout,
    plan_layout,
    projection_layout,
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


def expense_explorer_report(
    explorer: ExpenseExplorer, income_detail: ExpenseDrilldown | None = None
) -> str:
    """Print the selected expense cell and its shared Plan breakdown."""
    return render_html(expense_explorer_layout(explorer, income_detail))


def net_worth_history_report(history: NetWorthHistory) -> str:
    """Print net worth at each period end, with each point's top-level breakdown."""
    return render_html(net_worth_history_layout(history))


def net_worth_change_report(change: NetWorthChange) -> str:
    """Print the postings behind one net worth change and what they reconcile to."""
    return render_html(net_worth_change_layout(change))


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
    if isinstance(block, ModelChart):
        return model_chart_svg(block.model)
    return _table_html(block)


def _cell_html(cell: Cell, columns: int | None = None) -> str:
    classes = []
    if cell.numeric:
        classes.append("num")
    if cell.negative:
        classes.append("neg")
    attrs = f' class="{" ".join(classes)}"' if classes else ""
    span = columns if columns is not None else cell.span
    if span > 1:
        attrs += f' colspan="{span}"'
    text = "&nbsp;" * (cell.indent * 4) + escape(cell.text).replace("\n", "<br>")
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


def _bar_path(x: float, y: float, width: float, height: float, negative: bool) -> str:
    """A column with a 4-unit rounded data end and a square foot on the baseline."""
    r = min(4.0, width / 2, height)
    if negative:
        return (
            f"M{x:.1f},{y:.1f}h{width:.1f}v{height - r:.1f}"
            f"a{r},{r} 0 0 1 {-r:.1f},{r:.1f}h{-(width - 2 * r):.1f}"
            f"a{r},{r} 0 0 1 {-r:.1f},{-r:.1f}z"
        )
    return (
        f"M{x:.1f},{y + height:.1f}v{-(height - r):.1f}"
        f"a{r},{r} 0 0 1 {r:.1f},{-r:.1f}h{width - 2 * r:.1f}"
        f"a{r},{r} 0 0 1 {r:.1f},{r:.1f}v{height - r:.1f}z"
    )


def model_chart_svg(model: ChartModel, *, dark: bool = False) -> str:
    """Grouped columns for an engine chart, with a legend and exact-value tooltips."""
    from ...presentation import chart_bar_layout, chart_chrome, chart_series_colour

    width, height = 1000, 300
    left, top, right, bottom = 84, 40, 16, 40
    layout = chart_bar_layout(model, left, top, width - left - right, height - top - bottom)
    ink, muted = chart_chrome("ink", dark=dark), chart_chrome("muted", dark=dark)
    grid, axis = chart_chrome("grid", dark=dark), chart_chrome("axis", dark=dark)
    parts: list[str] = []
    for value, y in layout.ticks:
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            f'stroke="{axis if value == 0 else grid}" stroke-width="1"/>'
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="{muted}">{value:,.0f}</text>'
        )
    for bar in layout.bars:
        series = model.series[bar.series]
        exact = series.values[bar.category]
        amount = exact.format(parens_negative=True) if exact is not None else ""
        tip = f"{model.categories[bar.category]}, {series.name}: {amount} {model.currency}".strip()
        parts.append(
            f'<path d="{_bar_path(bar.x, bar.y, bar.width, bar.height, bar.negative)}" '
            f'fill="{chart_series_colour(series.slot, dark=dark)}">'
            f"<title>{escape(tip)}</title></path>"
        )
    for centre, label in zip(layout.centres, model.categories, strict=True):
        parts.append(
            f'<text x="{centre:.1f}" y="{height - bottom + 16}" text-anchor="middle" '
            f'font-size="11" fill="{muted}">{escape(label)}</text>'
        )
    x = left
    for series in model.series:
        parts.append(
            f'<rect x="{x}" y="10" width="12" height="12" rx="2" '
            f'fill="{chart_series_colour(series.slot, dark=dark)}"/>'
            f'<text x="{x + 18}" y="20" font-size="12" fill="{ink}">{escape(series.name)}</text>'
        )
        x += 30 + 8 * len(series.name)
    label = f"{model.title} ({model.currency})" if model.currency else model.title
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}" '
        "font-family=\"system-ui, -apple-system, 'Segoe UI', sans-serif\">"
        f"{''.join(parts)}</svg>"
    )
