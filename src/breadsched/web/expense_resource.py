"""Web Expense Explorer response projection over the shared read-only service."""

from __future__ import annotations

from calendar import monthrange
from datetime import date

from ..gen.db.sqlite import DbSQLite
from ..gen.engine.activity import ReportingPeriod
from ..gen.services import PlanQuery, query_expense_explorer
from .resources import ResourceError


def expense_report(
    db: DbSQLite,
    start_month: str | None = None,
    through_month: str | None = None,
    period: str | None = None,
    scenario_handle: str | None = None,
    account_handle: str | None = None,
    period_index: int | None = None,
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
            "variance": item.variance,
        }

    detail = explorer.drilldown
    return {
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
        "drilldown": None
        if detail is None
        else {
            "account": detail.account,
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
