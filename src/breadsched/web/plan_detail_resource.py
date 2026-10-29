"""Read-only web adapter for a Plan category, flow, or cash requirement cell."""

from __future__ import annotations

from datetime import date

from ..api.protocol import BookApi
from ..gen.engine import plan_detail
from ..gen.lib import PlanningFlowKind
from ..gen.services.plan_cells import PlanCellQuery
from ..presentation import plan_detail_cost_text


def plan_detail_report(
    book: BookApi,
    account_handle: str,
    start_value: str,
    end_value: str,
    scenario_handle: str | None = None,
    flow_kind: str | None = None,
    requirement_kind: str | None = None,
) -> dict:
    """Explain one Plan category, planning-flow, or cash-requirement cell."""
    explained = book.explain_plan_cell(
        PlanCellQuery(
            account=account_handle,
            start=date.fromisoformat(start_value),
            end=date.fromisoformat(end_value),
            scenario=scenario_handle or None,
            flow_kind=PlanningFlowKind(flow_kind) if flow_kind else None,
            requirement_kind=requirement_kind or None,
        )
    )
    scenario_name = explained.scenario_name
    detail: (
        plan_detail.CategoryPeriodDetail
        | plan_detail.PlanningFlowPeriodDetail
        | plan_detail.MortgagePaymentPeriodDetail
    )
    if explained.mortgage is not None:
        detail = explained.mortgage
        category = {
            "account": detail.account,
            "name": detail.name,
            "full_name": detail.full_name,
            "class": "cash_requirement",
            "kind": "mortgage",
        }
    elif explained.flow is not None:
        detail = explained.flow
        category = {
            "account": detail.account,
            "name": detail.name,
            "full_name": detail.full_name,
            "class": "planning_flow",
            "kind": detail.kind.value,
        }
    else:
        assert explained.category is not None
        detail = explained.category
        category = {
            "account": detail.account,
            "name": detail.name,
            "full_name": detail.full_name,
            "class": detail.account_class.value,
        }
    return {
        "category": category,
        "period": {"start": detail.start, "end": detail.end},
        "scenario": {"handle": scenario_handle, "name": scenario_name},
        "summary": {
            "planned": detail.planned,
            "actual": detail.actual,
            "variance": detail.variance,
            **_cost(detail),
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


def _cost(detail: object) -> dict[str, object]:
    """A category cell's gross and net cost, when reimbursements changed it."""
    if not isinstance(detail, plan_detail.CategoryPeriodDetail) or not detail.reimbursable:
        return {}
    return {
        "gross": detail.gross,
        "reimbursable": detail.reimbursable,
        "cost_text": plan_detail_cost_text(detail.gross, detail.reimbursable, detail.actual),
    }
