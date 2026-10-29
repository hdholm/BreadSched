"""Explain one Plan cell: a category, planning-flow, or cash-requirement period (#358).

``engine.plan_detail`` reconciles a cell's planned occurrences with the actual
transactions; this service chooses the scenario (a saved one, or the Base scenario
from the stored base assumptions), refuses what it cannot explain with stable codes,
and returns exactly one kind of detail, so the book server can carry it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import plan_detail
from ..lib import Assumptions, PlanningFlowKind, Scenario
from .contracts import ServiceError, ServiceResult

__all__ = ["PlanCellDetail", "PlanCellQuery", "explain_plan_cell"]


@dataclass(frozen=True, slots=True)
class PlanCellQuery:
    account: str
    start: date
    end: date
    #: A saved scenario; None for the Base scenario.
    scenario: str | None = None
    #: A planning flow's cell rather than a category's.
    flow_kind: PlanningFlowKind | None = None
    #: A cash requirement's cell; only ``mortgage`` is explained.
    requirement_kind: str | None = None


@dataclass(frozen=True, slots=True)
class PlanCellDetail:
    """One of ``category``, ``flow``, or ``mortgage``, as the query asked."""

    scenario_name: str
    category: plan_detail.CategoryPeriodDetail | None = None
    flow: plan_detail.PlanningFlowPeriodDetail | None = None
    mortgage: plan_detail.MortgagePaymentPeriodDetail | None = None


def explain_plan_cell(db: DbSQLite, query: PlanCellQuery) -> ServiceResult[PlanCellDetail]:
    if query.end < query.start:
        return ServiceResult.failure(ServiceError("plan.end.before_start", ("end", "start")))
    if query.requirement_kind and query.requirement_kind != "mortgage":
        return ServiceResult.failure(
            ServiceError("plan.requirement_kind.invalid", ("requirement_kind",))
        )
    if db.get_account(query.account) is None:
        return ServiceResult.failure(ServiceError("account.not_found", ("account",)))
    if query.scenario:
        scenario = db.get_scenario(query.scenario)
        if scenario is None:
            return ServiceResult.failure(ServiceError("scenario.not_found", ("scenario",)))
        name = scenario.name
    else:
        scenario = Scenario(
            name="Base scenario",
            start=query.start,
            years=max(1, query.end.year - query.start.year + 1),
        )
        stored = db.get_metadata("planning.base_assumptions", None)
        if isinstance(stored, dict):
            scenario.assumptions = Assumptions.from_dict(stored)
        name = "Base scenario"
    args = (query.account, query.start, query.end)
    if query.requirement_kind:
        mortgage = plan_detail.explain_mortgage_payment_period(db, *args, scenario=scenario)
        return ServiceResult.success(PlanCellDetail(name, mortgage=mortgage))
    if query.flow_kind is not None:
        flow = plan_detail.explain_planning_flow_period(
            db, query.flow_kind, *args, scenario=scenario
        )
        return ServiceResult.success(PlanCellDetail(name, flow=flow))
    category = plan_detail.explain_category_period(db, *args, scenario=scenario)
    return ServiceResult.success(PlanCellDetail(name, category=category))
