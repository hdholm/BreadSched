"""Self-contained printable HTML reports for the household views.

The report functions consume the same engine results that GTK renders.  They do
not query the database or recalculate financial values, so a printout describes
the applied view state rather than a subtly different second report.
"""

from __future__ import annotations

from html import escape
from itertools import zip_longest

from ...gen.engine.activity import CategoryReport, PlanMeasure
from ...gen.engine.dashboard import Dashboard
from ...gen.engine.projection import Projection
from ...gen.lib.account import AccountClass
from ...gen.lib.money import Money

__all__ = ["dashboard_report", "plan_report", "projection_report"]


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


def _document(title: str, subtitle: str, body: str, *, optional_plan_detail: bool = False) -> str:
    safe_title = escape(title)
    detail_control = ""
    if optional_plan_detail:
        detail_control = (
            '<label><input type="checkbox" onchange="document.body.classList.toggle('
            "'include-plan-detail',this.checked)\"> Include category detail when printing</label>"
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


def _money(value: Money | None) -> str:
    return "—" if value is None else value.format(parens_negative=True)


def _signed_money(value: Money | None) -> str:
    if value is None:
        return "—"
    rendered = value.format(parens_negative=True)
    return f"+{rendered}" if value > 0 else rendered


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


def dashboard_report(board: Dashboard, *, book_name: str = "") -> str:
    """Render the currently built Dashboard as printable HTML."""
    summary = board.summary()
    cards = _cards(
        [
            ("Net worth", _money(summary["net_worth"]), False),
            ("Liquid", _money(summary["liquid"]), False),
            (
                f"Needed in {board.config.liquidity_days} days",
                _money(summary["required_liquid"]),
                False,
            ),
            ("Available", _money(summary["available"]), summary["available"] < 0),
            (
                f"Emergency fund ({board.config.emergency_months} mo)",
                _money(summary["emergency_fund"]),
                False,
            ),
            (
                "Committed emergency outgoings / mo",
                _money(summary["emergency_monthly_outgoings"]),
                False,
            ),
            (
                "Including estimates / mo",
                _money(summary["emergency_monthly_outgoings_with_estimates"]),
                False,
            ),
            (
                "Months covered",
                f"{summary['months_covered']:g}",
                summary["months_covered"] < board.config.emergency_months,
            ),
        ]
    )
    if summary["emergency_shortfall"] > 0:
        cards = cards.removesuffix("</div>") + (
            '<div class="card"><small>Short of the fund</small>'
            f'<strong class="neg">{escape(_money(summary["emergency_shortfall"]))}</strong>'
            "</div></div>"
        )

    group_rows = []
    for group in board.groups:
        css = "heading" if group.heading else ""
        name = f"{'&nbsp;' * (group.depth * 4)}{escape(group.name)}"
        if group.note:
            name += f' <span class="note">— {escape(group.note)}</span>'
        ratio = f"{group.loan_to_value:.1%}" if group.loan_to_value is not None else ""
        group_rows.append(
            f'<tr class="{css}"><td title="{escape(group.path, quote=True)}">{name}</td>'
            + _amount(group.value)
            + _amount(group.debt)
            + _amount(group.equity if group.equity is not None else group.total)
            + f'<td class="num">{ratio}</td><td>{escape(str(group.loan_end or ""))}</td></tr>'
        )
    groups = (
        '<table><thead><tr><th>Group</th><th class="num">Value</th>'
        '<th class="num">Owed</th><th class="num">Equity / total</th>'
        '<th class="num">LTV</th><th>Loan end</th></tr></thead>'
        f"<tbody>{''.join(group_rows)}</tbody></table>"
        if group_rows
        else '<p class="note">No Dashboard groups are configured.</p>'
    )

    pending_rows = []
    for item in board.pending:
        days = item.days_until(board.as_of)
        due_in = f"{-days} days overdue" if days < 0 else "today" if days == 0 else f"{days} days"
        kind = "Account payment" if item.generated else "Estimate" if item.estimate else "Committed"
        pending_rows.append(
            f"<tr><td>{escape(item.name)}</td><td>{'Income' if item.income else 'Bill'}</td>"
            f"<td>{item.next_due.isoformat()}</td><td>{due_in}</td>"
            f"<td>{escape(item.frequency)}</td>{_amount(item.amount)}{_amount(item.monthly)}"
            f"{_amount(None if item.income else item.held)}{_amount(item.annual)}"
            f"<td>{kind}</td></tr>"
        )
    pending = (
        "<table><thead><tr><th>Item</th><th>Flow</th><th>Next due</th><th>Due in</th>"
        '<th>Frequency</th><th class="num">Amount</th><th class="num">Monthly</th>'
        '<th class="num">Hold now</th><th class="num">Annual</th><th>Kind</th></tr>'
        f"<tbody>{''.join(pending_rows)}</tbody></table>"
    )
    subtitle = f"{book_name + ' · ' if book_name else ''}As at {board.as_of.isoformat()}"
    body = f"{cards}<h2>Balances</h2>{groups}<h2>Pending cash flow</h2>{pending}"
    return _document("Dashboard", subtitle, body)


def plan_report(
    report: CategoryReport,
    measure: PlanMeasure,
    *,
    scenario_name: str,
    book_name: str = "",
) -> str:
    """Render the applied Plan horizon, grouping, measure, and scenario."""
    activity = report.activity
    position = report.cash_position
    cards = _cards(
        [
            ("Opening spendable cash", _money(position.opening), position.opening < 0),
            ("Ending spendable cash", _money(position.closing), position.closing < 0),
            (
                f"Lowest spendable cash ({position.minimum_date.isoformat()})",
                _money(position.minimum),
                position.minimum < 0,
            ),
            (
                "Projected change in spendable cash",
                _signed_money(activity.planned_cash_change),
                activity.planned_cash_change < 0,
            ),
            (
                f"Actual change through {report.as_of.isoformat()}",
                _signed_money(report.actual_cash_through_as_of),
                (report.actual_cash_through_as_of or Money(0)) < 0,
            ),
            (
                "Variance through as-of date",
                _signed_money(report.cash_variance_through_as_of),
                (report.cash_variance_through_as_of or Money(0)) < 0,
            ),
            ("Expected unresolved", activity.unresolved_count, False),
            ("Actuals to review", activity.unresolved_actual_count, False),
        ]
    )
    headers = "".join(f'<th class="num">{escape(period.label)}</th>' for period in activity.periods)
    summary_rows: list[str] = []
    detail_rows: list[str] = []

    def values_row(
        label: str,
        values,
        total: Money | None,
        css: str = "",
        *,
        signed: bool = False,
    ) -> str:
        cell = _signed_amount if signed else _amount
        cells = "".join(cell(value) for value in values)
        return f'<tr class="{css}"><td>{label}</td>{cells}{cell(total)}</tr>'

    summary_rows.append(
        '<tr class="section"><td colspan="'
        f'{len(activity.periods) + 2}">Spendable cash bridge</td></tr>'
    )
    for bridge in report.cash_bridge:
        summary_rows.append(
            values_row(
                escape(bridge.name),
                bridge.values(measure),
                bridge.total(measure),
                signed=True,
            )
        )
    summary_rows.append(
        values_row(
            "Net change in spendable cash",
            report.cash_bridge_totals(measure),
            report.cash_bridge_grand_total(measure),
            "grand",
            signed=True,
        )
    )

    for section, categories, account_class in (
        ("Income", report.income, AccountClass.INCOME),
        ("Expenses", report.expenses, AccountClass.EXPENSE),
    ):
        detail_rows.append(
            f'<tr class="section"><td colspan="{len(activity.periods) + 2}">{section}</td></tr>'
        )
        for category in categories:
            indent = "&nbsp;" * (category.depth * 4)
            label = f'<span title="{escape(category.full_name, quote=True)}">{indent}'
            label += f"{escape(category.name)}</span>"
            detail_rows.append(values_row(label, category.values(measure), category.total(measure)))
        totals = report.category_totals(account_class, measure)
        detail_rows.append(
            values_row(
                f"{section} total",
                totals,
                report.category_grand_total(account_class, measure),
                "total",
            )
        )

    detail_rows.append(
        values_row(
            "Income less expenses",
            report.operating_net_totals(measure),
            report.operating_net_grand_total(measure),
            "grand",
            signed=True,
        )
    )

    if report.mortgage_payments:
        detail_rows.append(
            '<tr class="section"><td colspan="'
            f'{len(activity.periods) + 2}">Cash requirements (informational)</td></tr>'
        )
        for payment in report.mortgage_payments:
            title = (
                "Whole mortgage payment; classified components appear below and are not "
                "added to this row."
            )
            label = f'<span title="{escape(title, quote=True)}">{escape(payment.name)}</span>'
            detail_rows.append(values_row(label, payment.values(measure), payment.total(measure)))
        totals = report.mortgage_payment_totals(measure)
        detail_rows.append(
            values_row(
                "Mortgage cash required",
                totals,
                report.mortgage_payment_grand_total(measure),
                "total",
            )
        )

    if report.planning_flows:
        detail_rows.append(
            '<tr class="section"><td colspan="'
            f'{len(activity.periods) + 2}">Balance-sheet classifications (informational)'
            "</td></tr>"
        )
        for flow in report.planning_flows:
            label = f'<span title="{escape(flow.full_name, quote=True)}">{escape(flow.name)}</span>'
            detail_rows.append(values_row(label, flow.values(measure), flow.total(measure)))
    summary_header = (
        f'<thead><tr><th>Cash source / use</th>{headers}<th class="num">Total</th></tr></thead>'
    )
    detail_header = f'<thead><tr><th>Category</th>{headers}<th class="num">Total</th></tr></thead>'
    summary_table = (
        f'<section class="plan-summary"><h2>Cash outlook</h2><table>{summary_header}'
        f"<tbody>{''.join(summary_rows)}</tbody></table></section>"
    )
    detail_table = (
        f'<section class="plan-detail"><h2>Budget and classifications</h2><table>{detail_header}'
        f"<tbody>{''.join(detail_rows)}</tbody></table></section>"
    )
    subtitle = (
        f"{activity.start.isoformat()} through {activity.end.isoformat()} · "
        f"{activity.period.value.title()} · {measure.value.title()} · {scenario_name}"
    )
    return _document(
        "Plan",
        subtitle,
        f"{cards}{summary_table}{detail_table}",
        optional_plan_detail=True,
    )


def projection_report(
    result: Projection,
    *,
    comparison: Projection | None = None,
    book_name: str = "",
) -> str:
    """Render the current Projection result, comparison, and annual assumptions."""
    shortfall = result.first_shortfall()
    cards = _cards(
        [
            ("Ending net worth", _money(result.ending_net_worth), result.ending_net_worth < 0),
            ("Ending cash", _money(result.ending_cash), result.ending_cash < 0),
            ("Lowest cash", _money(result.minimum_cash), result.minimum_cash < 0),
            ("Total growth", _money(result.total("investment_growth")), False),
            ("Contributions", _money(result.total("contributions")), False),
            ("Taxable withdrawals", _money(result.total("withdrawals")), False),
            (
                "Retirement distributions",
                _money(result.total("retirement_distributions")),
                False,
            ),
            ("Investment income", _money(result.total("investment_income")), False),
            ("Investment fees", _money(result.total("investment_fees")), False),
            ("Retirement rollovers", _money(result.total("rollovers")), False),
            ("Cash runs out", shortfall.label if shortfall else "Never", shortfall is not None),
        ]
    )
    assumptions = result.scenario.assumptions_for(result.scenario.start)
    assumption_sources = result.scenario.assumption_sources(result.scenario.start)
    assumption_rows = []
    for key, label in (
        ("income_growth", "Income growth"),
        ("expense_inflation", "Expense inflation"),
        ("investment_return", "Investment return"),
        ("cash_interest", "Cash interest"),
        ("liability_interest", "Liability interest"),
    ):
        assumption_rows.append(
            f'<tr><td>{label}</td><td class="num">{getattr(assumptions, key):.2%}</td>'
            f"<td>{escape(assumption_sources[key])}</td></tr>"
        )
    assumptions_table = (
        '<table><thead><tr><th>Assumption</th><th class="num">Annual rate</th>'
        "<th>Source</th></tr></thead>"
        f"<tbody>{''.join(assumption_rows)}</tbody></table>"
    )
    chart = _projection_chart(result, comparison)
    annual_rows = []
    for row in result.rows[11::12]:
        annual_rows.append(
            f"<tr><td>{escape(row.label)}</td>{_amount(row.income)}{_amount(row.expense)}"
            f"{_amount(row.cash_close)}{_amount(row.holdings)}{_amount(row.liabilities)}"
            f"{_amount(row.net_worth)}</tr>"
        )
    annual = (
        '<table><thead><tr><th>Month</th><th class="num">Income</th>'
        '<th class="num">Expense</th><th class="num">Cash</th>'
        '<th class="num">Holdings</th><th class="num">Liabilities</th>'
        '<th class="num">Net worth</th></tr></thead>'
        f"<tbody>{''.join(annual_rows)}</tbody></table>"
    )
    comparison_table = _projection_comparison(result, comparison) if comparison else ""
    warnings = (
        f'<p class="warnings">{escape("  ".join(result.warnings))}</p>' if result.warnings else ""
    )
    escrow_items = [
        f"<li>{escape(row.label)}: {escape(message)}</li>"
        for row in result.rows
        for message in row.ledger.escrow_explanations
    ]
    escrow = f"<h2>Escrow treatment</h2><ul>{''.join(escrow_items)}</ul>" if escrow_items else ""
    start = result.rows[0].month.isoformat() if result.rows else str(result.scenario.start or "")
    end = result.rows[-1].month.isoformat() if result.rows else ""
    prefix = f"{book_name} · " if book_name else ""
    subtitle = f"{prefix}{result.scenario.name} · {start} through {end}"
    body = (
        f"{cards}{warnings}{escrow}<h2>Projection chart</h2>{chart}"
        f"<h2>Annual assumptions</h2>{assumptions_table}"
        f"{comparison_table}<h2>Year-end values</h2>{annual}"
    )
    return _document("Projection", subtitle, body)


def _projection_chart(result: Projection, comparison: Projection | None) -> str:
    series = [
        ("Cash", [row.cash_close for row in result.rows], "#2f6fd0"),
        ("Investments", [row.holdings for row in result.rows], "#1c7a4a"),
        ("Net worth", [row.net_worth for row in result.rows], "#8b4ab8"),
    ]
    if comparison is not None:
        series.append(
            (
                f"{comparison.scenario.name} net worth",
                [row.net_worth for row in comparison.rows],
                "#d47817",
            )
        )
    values = [float(value.to_decimal()) for _, points, _ in series for value in points]
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
    count = max((len(points) for _, points, _ in series), default=1)

    def point(index: int, value: Money) -> str:
        x = left + usable_width * index / max(1, count - 1)
        y = top + usable_height * (high - float(value.to_decimal())) / (high - low)
        return f"{x:.1f},{y:.1f}"

    lines = []
    legend = []
    for index, (name, points, colour) in enumerate(series):
        coords = " ".join(point(i, value) for i, value in enumerate(points))
        lines.append(
            f'<polyline fill="none" stroke="{colour}" stroke-width="2" points="{coords}"/>'
        )
        legend.append(
            f'<line x1="{left + index * 210}" y1="12" x2="{left + index * 210 + 18}" '
            f'y2="12" stroke="{colour}" stroke-width="3"/><text x="{left + index * 210 + 24}" '
            f'y="16" font-size="12">{escape(name)}</text>'
        )
    zero = ""
    if low <= 0 <= high:
        zero_y = top + usable_height * high / (high - low)
        zero = (
            f'<line x1="{left}" y1="{zero_y:.1f}" x2="{width - right}" '
            f'y2="{zero_y:.1f}" stroke="#aeb3ba" stroke-dasharray="4 4"/>'
        )
    labels = []
    if result.rows:
        indices = sorted({0, len(result.rows) // 2, len(result.rows) - 1})
        for index in indices:
            x = left + usable_width * index / max(1, len(result.rows) - 1)
            labels.append(
                f'<text x="{x:.1f}" y="{height - 10}" text-anchor="middle" '
                f'font-size="11">{escape(result.rows[index].label)}</text>'
            )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Projection chart">'
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" '
        'stroke="#aeb3ba"/>'
        f'<line x1="{left}" y1="{height - bottom}" x2="{width - right}" '
        f'y2="{height - bottom}" stroke="#aeb3ba"/>{zero}{"".join(lines)}'
        f"{''.join(legend)}{''.join(labels)}</svg>"
    )


def _projection_comparison(result: Projection, comparison: Projection) -> str:
    rows = []
    primary_years = result.rows[11::12]
    comparison_years = comparison.rows[11::12]
    for primary, compared in zip_longest(primary_years, comparison_years):
        if primary is None or compared is None:
            continue
        rows.append(
            f"<tr><td>{escape(primary.label)}</td>{_amount(primary.cash_close)}"
            f"{_amount(compared.cash_close)}{_amount(primary.cash_close - compared.cash_close)}"
            f"{_amount(primary.net_worth)}{_amount(compared.net_worth)}"
            f"{_amount(primary.net_worth - compared.net_worth)}</tr>"
        )
    return (
        f"<h2>{escape(result.scenario.name)} versus {escape(comparison.scenario.name)}</h2>"
        "<table><thead><tr><th>Month</th>"
        f'<th class="num">{escape(result.scenario.name)} cash</th>'
        f'<th class="num">{escape(comparison.scenario.name)} cash</th>'
        '<th class="num">Cash difference</th>'
        f'<th class="num">{escape(result.scenario.name)} net worth</th>'
        f'<th class="num">{escape(comparison.scenario.name)} net worth</th>'
        '<th class="num">Net worth difference</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
