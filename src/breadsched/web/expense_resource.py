"""Web Expense Explorer response projection over the shared read-only service."""

from __future__ import annotations

from calendar import monthrange
from datetime import date

from ..gen.db.sqlite import DbSQLite
from ..gen.engine.activity import ReportingPeriod
from ..gen.services import PlanQuery, query_expense_explorer
from .controls import ResourceError


def expense_report(
    db: DbSQLite,
    start_month: str | None = None,
    through_month: str | None = None,
    period: str | None = None,
    scenario_handle: str | None = None,
    account_handle: str | None = None,
    period_index: int | None = None,
    rollover: bool = False,
) -> dict:
    """Expense categories and optional merchant drilldown from the shared service."""
    start = date.fromisoformat(f"{start_month}-01") if start_month else None
    through = date.fromisoformat(f"{through_month}-01") if through_month else None
    end = (
        date(through.year, through.month, monthrange(through.year, through.month)[1])
        if through
        else None
    )
    result = query_expense_explorer(
        db,
        PlanQuery(
            start=start,
            end=end,
            period=ReportingPeriod(period) if period else None,
            scenario=scenario_handle,
            use_saved=not any((start_month, through_month, period, scenario_handle)),
        ),
        account=account_handle,
        period_index=period_index,
        rollover=rollover,
    )
    if result.value is None:
        error = result.errors[0]
        raise ResourceError(400, error.code, error.fields, error.code)
    explorer = result.value

    def period_value(item):
        return {
            "start": item.start,
            "end": item.end,
            "label": item.label,
            "planned": item.planned,
            "actual": item.actual,
            "actual_to_date": item.actual_to_date,
            "carry_in": item.carry_in,
            "variance": item.variance,
            "remaining": item.remaining,
            "remaining_reason": item.remaining_reason,
        }

    def over_time(points, names):
        return [
            {
                "start": point.start,
                "end": point.end,
                "label": point.label,
                "planned": point.planned,
                "actual": point.actual,
                "future": point.future,
                "partial": point.partial,
                "currency_incomplete": point.currency_incomplete,
                "completeness": point.completeness.as_dict(),
                "categories": [
                    {"account": handle, "name": names.get(handle, handle), "actual": amount}
                    for handle, amount in point.categories
                ],
            }
            for point in points
        ]

    detail = explorer.drilldown
    names = {row.account: row.full_name for row in explorer.categories}
    return {
        "rollover": explorer.rollover,
        "scenario": explorer.plan.scenario.name,
        "period": explorer.plan.period.value,
        "categories": [
            {
                "account": row.account,
                "name": row.name,
                "full_name": row.full_name,
                "depth": row.depth,
                "periods": [period_value(item) for item in row.periods],
            }
            for row in explorer.categories
        ],
        "totals": [period_value(item) for item in explorer.totals],
        "as_of": explorer.plan.report.as_of,
        "spending": over_time(explorer.spending, names),
        "income": over_time(
            explorer.income, {row.account: row.full_name for row in explorer.income_categories}
        ),
        "income_categories": [
            {"account": row.account, "full_name": row.full_name}
            for row in explorer.income_categories
        ],
        "drilldown": None
        if detail is None
        else {
            "account": detail.account,
            "income": detail.income,
            "period": period_value(detail.period),
            "merchants": [
                {
                    "name": group.name,
                    "amount": group.amount,
                    "transactions": [
                        {
                            "transaction": item.transaction,
                            "date": item.post_date,
                            "description": item.description,
                            "amount": item.amount,
                        }
                        for item in group.transactions
                    ],
                }
                for group in detail.merchants
            ],
            "planned": [
                {
                    "occurrence": item.occurrence,
                    "date": item.planned_date,
                    "description": item.description,
                    "expected": item.expected,
                }
                for item in detail.planned_events
            ],
        },
    }
