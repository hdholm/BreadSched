"""Read-only web Plan query and response projection over the shared service."""

from __future__ import annotations

from calendar import monthrange
from datetime import date

from ..gen.db.sqlite import DbSQLite
from ..gen.engine.activity import PlanMeasure, ReportingPeriod
from ..gen.lib import AccountClass, Money
from ..gen.services import PlanQuery, query_plan
from .resources import ResourceError


def plan_report(
    db: DbSQLite,
    start_month: str | None = None,
    through_month: str | None = None,
    period: str | None = None,
    scenario_handle: str | None = None,
    compare_handle: str | None = None,
    measure: str | None = None,
) -> dict:
    """Derived category Plan using the same event stream as the GTK view."""
    use_saved = all(
        value is None
        for value in (
            start_month,
            through_month,
            period,
            scenario_handle,
            compare_handle,
            measure,
        )
    )
    start = date.fromisoformat(f"{start_month}-01") if start_month is not None else None
    through = date.fromisoformat(f"{through_month}-01") if through_month is not None else None
    end = (
        date(through.year, through.month, monthrange(through.year, through.month)[1])
        if through is not None
        else None
    )
    service_result = query_plan(
        db,
        PlanQuery(
            start=start,
            end=end,
            period=ReportingPeriod(period) if period is not None else None,
            measure=PlanMeasure(measure) if measure is not None else None,
            scenario=scenario_handle,
            compare=compare_handle,
            use_saved=use_saved,
        ),
    )
    if service_result.value is None:
        error = service_result.errors[0]
        messages = {
            "plan.scenario.not_found": "Scenario was not found.",
            "plan.comparison.not_found": "Comparison scenario was not found.",
            "plan.start.before_book_data": "From cannot be earlier than the first book data.",
            "plan.end.before_start": "Through must be the same month as From or later.",
            "plan.end.after_maximum": "Through exceeds the supported planning horizon.",
            "plan.comparison.same": "Plan comparison must use a different scenario.",
        }
        status = 404 if error.code.endswith(".not_found") else 400
        raise ResourceError(
            status,
            error.code,
            error.fields,
            messages.get(error.code, error.code),
        )
    plan = service_result.value
    start = plan.start
    end = plan.end
    minimum = plan.minimum
    maximum_month = plan.maximum
    grouping = plan.period
    selected_measure = plan.measure
    scenario_handle = plan.scenario.handle
    compare_handle = plan.compare_handle
    report = plan.report
    totals = report.activity

    comparison: dict[str, object] | None = None
    if plan.comparison is not None:
        compare_identity = plan.comparison.scenario.handle
        compare_name = plan.comparison.scenario.name
        compare_report = plan.comparison.report
        compare_rows = {row.account: row for row in compare_report.categories}
        compare_bridges = {row.kind: row for row in compare_report.cash_bridge}
        compare_flows = {(row.kind, row.account): row for row in compare_report.planning_flows}
        compare_mortgages = {row.account: row for row in compare_report.mortgage_payments}
        comparison_categories: list[dict[str, object]] = []
        comparison_bridges: list[dict[str, object]] = []
        comparison_flows: list[dict[str, object]] = []
        comparison_mortgages: list[dict[str, object]] = []
        comparison = {
            "handle": compare_identity,
            "name": compare_name,
            "assumption_sources": plan.comparison.assumption_sources,
            "summary": {
                "planned_cash": compare_report.activity.planned_cash_change,
                "actual_cash": compare_report.actual_cash_through_as_of,
                "variance": compare_report.cash_variance_through_as_of,
                "opening_cash": compare_report.cash_position.opening,
                "ending_cash": compare_report.cash_position.closing,
                "minimum_cash": compare_report.cash_position.minimum,
                "minimum_cash_date": compare_report.cash_position.minimum_date,
                "planned_cash_delta": (
                    totals.planned_cash_change - compare_report.activity.planned_cash_change
                ),
                "actual_cash_delta": (
                    report.actual_cash_through_as_of - compare_report.actual_cash_through_as_of
                    if report.actual_cash_through_as_of is not None
                    and compare_report.actual_cash_through_as_of is not None
                    else None
                ),
                "variance_delta": (
                    report.cash_variance_through_as_of - compare_report.cash_variance_through_as_of
                    if report.cash_variance_through_as_of is not None
                    and compare_report.cash_variance_through_as_of is not None
                    else None
                ),
            },
            "categories": comparison_categories,
            "cash_bridge": comparison_bridges,
            "mortgage_payments": comparison_mortgages,
            "planning_flows": comparison_flows,
        }
        for bridge_row in report.cash_bridge:
            other_bridge = compare_bridges.get(bridge_row.kind)
            zeroes = [Money(0) for _ in bridge_row.planned]
            bridge_planned = other_bridge.planned if other_bridge is not None else zeroes
            bridge_actual = other_bridge.actual if other_bridge is not None else zeroes
            bridge_variance: list[Money | None] = (
                other_bridge.variance if other_bridge is not None else list(zeroes)
            )
            comparison_bridges.append(
                {
                    "kind": bridge_row.kind.value,
                    "planned": bridge_planned,
                    "actual": bridge_actual,
                    "variance": bridge_variance,
                    "planned_delta": [
                        value - alternate
                        for value, alternate in zip(bridge_row.planned, bridge_planned, strict=True)
                    ],
                    "actual_delta": [
                        value - alternate
                        for value, alternate in zip(bridge_row.actual, bridge_actual, strict=True)
                    ],
                    "variance_delta": [
                        (value - alternate if value is not None and alternate is not None else None)
                        for value, alternate in zip(
                            bridge_row.variance, bridge_variance, strict=True
                        )
                    ],
                }
            )
        for row in report.categories:
            other = compare_rows.get(row.account)
            zeroes = [Money(0) for _ in row.planned]
            other_planned = other.planned if other is not None else zeroes
            other_actual = other.actual if other is not None else zeroes
            other_variance: list[Money | None] = (
                other.variance if other is not None else list(zeroes)
            )
            comparison_categories.append(
                {
                    "account": row.account,
                    "planned": other_planned,
                    "actual": other_actual,
                    "variance": other_variance,
                    "planned_delta": [
                        value - alternate
                        for value, alternate in zip(row.planned, other_planned, strict=True)
                    ],
                    "actual_delta": [
                        value - alternate
                        for value, alternate in zip(row.actual, other_actual, strict=True)
                    ],
                    "variance_delta": [
                        (value - alternate if value is not None and alternate is not None else None)
                        for value, alternate in zip(row.variance, other_variance, strict=True)
                    ],
                }
            )
        for payment in report.mortgage_payments:
            other_payment = compare_mortgages.get(payment.account)
            zeroes = [Money(0) for _ in payment.planned]
            other_planned = other_payment.planned if other_payment is not None else zeroes
            other_actual = other_payment.actual if other_payment is not None else zeroes
            payment_variance: list[Money | None] = (
                other_payment.variance if other_payment is not None else list(zeroes)
            )
            comparison_mortgages.append(
                {
                    "account": payment.account,
                    "planned": other_planned,
                    "actual": other_actual,
                    "variance": payment_variance,
                    "planned_delta": [
                        value - alternate
                        for value, alternate in zip(payment.planned, other_planned, strict=True)
                    ],
                    "actual_delta": [
                        value - alternate
                        for value, alternate in zip(payment.actual, other_actual, strict=True)
                    ],
                    "variance_delta": [
                        (value - alternate if value is not None and alternate is not None else None)
                        for value, alternate in zip(payment.variance, payment_variance, strict=True)
                    ],
                }
            )
        for flow_row in report.planning_flows:
            other_flow = compare_flows.get((flow_row.kind, flow_row.account))
            flow_zeroes = [Money(0) for _ in flow_row.planned]
            other_flow_planned = other_flow.planned if other_flow is not None else flow_zeroes
            other_flow_actual = other_flow.actual if other_flow is not None else flow_zeroes
            other_flow_variance: list[Money | None] = (
                other_flow.variance if other_flow is not None else list(flow_zeroes)
            )
            comparison_flows.append(
                {
                    "kind": flow_row.kind.value,
                    "account": flow_row.account,
                    "planned": other_flow_planned,
                    "actual": other_flow_actual,
                    "variance": other_flow_variance,
                    "planned_delta": [
                        value - alternate
                        for value, alternate in zip(
                            flow_row.planned, other_flow_planned, strict=True
                        )
                    ],
                    "actual_delta": [
                        value - alternate
                        for value, alternate in zip(flow_row.actual, other_flow_actual, strict=True)
                    ],
                    "variance_delta": [
                        (value - alternate if value is not None and alternate is not None else None)
                        for value, alternate in zip(
                            flow_row.variance, other_flow_variance, strict=True
                        )
                    ],
                }
            )

    return {
        "controls": {
            "from": start.strftime("%Y-%m"),
            "through": end.strftime("%Y-%m"),
            "minimum": minimum.strftime("%Y-%m"),
            "maximum": maximum_month.strftime("%Y-%m"),
            "period": grouping.value,
            "measure": selected_measure.value,
            "scenario": scenario_handle,
            "compare": compare_handle,
            "scenarios": [{"handle": item.handle, "name": item.name} for item in plan.scenarios],
            "assumption_sources": plan.assumption_sources,
        },
        "periods": [
            {
                "label": item.label,
                "start": item.start,
                "end": item.end,
            }
            for item in totals.periods
        ],
        "summary": {
            "planned_cash": totals.planned_cash_change,
            "actual_cash": report.actual_cash_through_as_of,
            "variance": report.cash_variance_through_as_of,
            "opening_cash": report.cash_position.opening,
            "ending_cash": report.cash_position.closing,
            "minimum_cash": report.cash_position.minimum,
            "minimum_cash_date": report.cash_position.minimum_date,
            "unresolved_expected": totals.unresolved_count,
            "unresolved_actuals": totals.unresolved_actual_count,
        },
        "comparison": comparison,
        "cash_bridge": [
            {
                "kind": row.kind.value,
                "name": row.name,
                "planned": row.planned,
                "actual": row.actual,
                "variance": row.variance,
                "totals": {item.value: row.total(item) for item in PlanMeasure},
            }
            for row in report.cash_bridge
        ],
        "categories": [
            {
                "account": row.account,
                "name": row.name,
                "full_name": row.full_name,
                "class": row.account_class.value,
                "depth": row.depth,
                "planned": row.planned,
                "actual": row.actual,
                "variance": row.variance,
                "totals": {item.value: row.total(item) for item in PlanMeasure},
            }
            for row in report.categories
        ],
        "mortgage_payments": [
            {
                "account": row.account,
                "account_name": row.account_name,
                "full_name": row.full_name,
                "name": row.name,
                "planned": row.planned,
                "actual": row.actual,
                "variance": row.variance,
                "totals": {item.value: row.total(item) for item in PlanMeasure},
            }
            for row in report.mortgage_payments
        ],
        "planning_flows": [
            {
                "kind": row.kind.value,
                "account": row.account,
                "account_name": row.account_name,
                "full_name": row.full_name,
                "name": row.name,
                "planned": row.planned,
                "actual": row.actual,
                "variance": row.variance,
                "totals": {item.value: row.total(item) for item in PlanMeasure},
            }
            for row in report.planning_flows
        ],
        "column_totals": {
            "income": {
                item.value: {
                    "periods": report.category_totals(AccountClass.INCOME, item),
                    "total": report.category_grand_total(AccountClass.INCOME, item),
                }
                for item in PlanMeasure
            },
            "expense": {
                item.value: {
                    "periods": report.category_totals(AccountClass.EXPENSE, item),
                    "total": report.category_grand_total(AccountClass.EXPENSE, item),
                }
                for item in PlanMeasure
            },
            "operating_net": {
                item.value: {
                    "periods": report.operating_net_totals(item),
                    "total": report.operating_net_grand_total(item),
                }
                for item in PlanMeasure
            },
            "cash_bridge": {
                item.value: {
                    "periods": report.cash_bridge_totals(item),
                    "total": report.cash_bridge_grand_total(item),
                }
                for item in PlanMeasure
            },
            "planning_flows": {
                item.value: {
                    "periods": report.planning_flow_totals(item),
                    "total": report.planning_flow_grand_total(item),
                }
                for item in PlanMeasure
            },
            "mortgage_payments": {
                item.value: {
                    "periods": report.mortgage_payment_totals(item),
                    "total": report.mortgage_payment_grand_total(item),
                }
                for item in PlanMeasure
            },
            "net_cash": {
                item.value: {
                    "periods": report.cash_totals(item),
                    "total": report.grand_total(item),
                }
                for item in PlanMeasure
            },
        },
    }
