"""Read-only web adapter for a Plan category, flow, or cash requirement cell."""

from __future__ import annotations

from datetime import date

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import activity
from ..gen.lib import Assumptions, PlanningFlowKind, Scenario


def plan_detail_report(
    db: DbSQLite,
    account_handle: str,
    start_value: str,
    end_value: str,
    scenario_handle: str | None = None,
    flow_kind: str | None = None,
    requirement_kind: str | None = None,
) -> dict:
    """Explain one Plan category, planning-flow, or cash-requirement cell."""
    start = date.fromisoformat(start_value)
    end = date.fromisoformat(end_value)
    scenarios = list(db.iter_scenarios())
    if scenario_handle:
        scenario = next((item for item in scenarios if item.handle == scenario_handle), None)
        if scenario is None:
            raise KeyError(scenario_handle)
        scenario_name = scenario.name
    else:
        scenario = Scenario(
            name="Base scenario",
            start=start,
            years=max(1, end.year - start.year + 1),
        )
        stored = db.get_metadata("planning.base_assumptions", None)
        if isinstance(stored, dict):
            scenario.assumptions = Assumptions.from_dict(stored)
        scenario_name = "Base scenario"

    detail: (
        activity.CategoryPeriodDetail
        | activity.PlanningFlowPeriodDetail
        | activity.MortgagePaymentPeriodDetail
    )
    if requirement_kind:
        if requirement_kind != "mortgage":
            raise ValueError("unknown Plan cash-requirement kind")
        mortgage_detail = activity.explain_mortgage_payment_period(
            db, account_handle, start, end, scenario=scenario
        )
        detail = mortgage_detail
        category = {
            "account": mortgage_detail.account,
            "name": mortgage_detail.name,
            "full_name": mortgage_detail.full_name,
            "class": "cash_requirement",
            "kind": "mortgage",
        }
    elif flow_kind:
        kind = PlanningFlowKind(flow_kind)
        flow_detail = activity.explain_planning_flow_period(
            db, kind, account_handle, start, end, scenario=scenario
        )
        detail = flow_detail
        category = {
            "account": flow_detail.account,
            "name": flow_detail.name,
            "full_name": flow_detail.full_name,
            "class": "planning_flow",
            "kind": kind.value,
        }
    else:
        category_detail = activity.explain_category_period(
            db, account_handle, start, end, scenario=scenario
        )
        detail = category_detail
        category = {
            "account": category_detail.account,
            "name": category_detail.name,
            "full_name": category_detail.full_name,
            "class": category_detail.account_class.value,
        }
    return {
        "category": category,
        "period": {"start": detail.start, "end": detail.end},
        "scenario": {"handle": scenario_handle, "name": scenario_name},
        "summary": {
            "planned": detail.planned,
            "actual": detail.actual,
            "variance": detail.variance,
        },
        "planned": [
            {
                "occurrence": item.occurrence,
                "date": item.planned_date,
                "description": item.description,
                "source": item.source,
                "status": item.status,
                "expected": item.expected,
                "actual": item.actual,
                "variance": item.variance,
                "actual_transaction": item.actual_transaction,
                "actual_date": item.actual_date,
                "explanation": list(item.explanation),
            }
            for item in detail.planned_events
        ],
        "actuals": [
            {
                "transaction": item.transaction,
                "date": item.post_date,
                "description": item.description,
                "amount": item.amount,
                "resolution": item.resolution.value,
                "planned_occurrence": item.planned_occurrence,
                "planned_for": item.planned_for,
                "expected": item.expected,
                "variance": item.variance,
                "date_variance_days": item.date_variance_days,
                "explanation": list(item.explanation),
            }
            for item in detail.actual_transactions
        ],
    }
