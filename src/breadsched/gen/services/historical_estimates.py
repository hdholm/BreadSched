"""Category estimates proposed from closed history, and accepting one.

``engine.estimates`` analyses the history, drafts each proposal as an ordinary
estimate, and writes an accepted one; this module checks an acceptance first and
names each refusal with a stable code, so a refused one leaves the book unchanged.
The view carries every proposal with its editable draft, so an interface shows and
adjusts proposals without reading the book itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import estimates
from ..engine.estimates import HistoricalEstimateProposal
from .contracts import ServiceError, ServiceResult
from .schedule_editing import ScheduleEditorView, editor_view

__all__ = [
    "AcceptHistoricalEstimate",
    "AcceptedEstimate",
    "EstimateTarget",
    "HistoricalEstimatesView",
    "accept_historical_estimate",
    "historical_estimates",
]


@dataclass(frozen=True, slots=True)
class EstimateTarget:
    """Where an accepted estimate goes: Base (``handle`` ``None``) or a scenario."""

    handle: str | None
    name: str


@dataclass(frozen=True, slots=True)
class HistoricalEstimatesView:
    months: int
    proposals: tuple[HistoricalEstimateProposal, ...]
    #: Each proposal's Base draft, in the same order, ready for a schedule editor.
    drafts: tuple[ScheduleEditorView, ...]
    targets: tuple[EstimateTarget, ...]


@dataclass(frozen=True, slots=True)
class AcceptHistoricalEstimate:
    """Accept the proposal named by ``key`` (or, without one, for ``category``)."""

    key: str = ""
    category: str = ""
    months: int = 12
    min_active_months: int = 3
    #: A saved scenario to add the estimate to; Base when ``None``.
    scenario: str | None = None
    as_of: date | None = None


@dataclass(frozen=True, slots=True)
class AcceptedEstimate:
    handle: str
    category_name: str


def _window(months: int, min_active_months: int) -> list[ServiceError]:
    errors = []
    if not 1 <= months <= 1200:
        errors.append(ServiceError("estimate.months.invalid", ("months",)))
    if not 1 <= min_active_months <= 1200:
        errors.append(ServiceError("estimate.active_months.invalid", ("min_active_months",)))
    return errors


def historical_estimates(
    db: DbSQLite,
    months: int = 12,
    min_active_months: int = 3,
    scenario: str | None = None,
    as_of: date | None = None,
) -> ServiceResult[HistoricalEstimatesView]:
    """Residual category estimates for Base, or for ``scenario``; writes nothing."""
    errors = _window(months, min_active_months)
    if scenario is not None and db.get_scenario(scenario) is None:
        errors.append(ServiceError("estimate.scenario.not_found", ("scenario",)))
    if errors:
        return ServiceResult.failure(*errors)
    proposals = tuple(
        estimates.propose_historical_estimates(
            db,
            as_of=as_of,
            months=months,
            min_active_months=min_active_months,
            scenario_handle=scenario,
        )
    )
    drafts = tuple(
        editor_view(db, estimates.draft_historical_estimate(db, item)) for item in proposals
    )
    targets = (
        EstimateTarget(None, "Base"),
        *(EstimateTarget(item.handle, item.name) for item in db.iter_scenarios()),
    )
    return ServiceResult.success(HistoricalEstimatesView(months, proposals, drafts, targets))


def accept_historical_estimate(
    db: DbSQLite, request: AcceptHistoricalEstimate
) -> ServiceResult[AcceptedEstimate]:
    """Save one current proposal as an ordinary Base or scenario estimate."""
    found = historical_estimates(
        db, request.months, request.min_active_months, request.scenario, request.as_of
    )
    if found.value is None:
        return ServiceResult.failure(*found.errors)
    proposal = next(
        (
            item
            for item in found.value.proposals
            if item.key == request.key or (not request.key and item.category == request.category)
        ),
        None,
    )
    if proposal is None:
        return ServiceResult.failure(ServiceError("estimate.proposal.unavailable", ("key",)))
    try:
        handle = estimates.accept_historical_estimate(
            db, proposal, scenario_handle=request.scenario
        )
    except ValueError:
        return ServiceResult.failure(ServiceError("estimate.draft.invalid", ("key",)))
    return ServiceResult.success(AcceptedEstimate(handle, proposal.category_name))
