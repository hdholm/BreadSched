"""Typed mutations for projection assumptions and dated regimes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from ..db.sqlite import DbSQLite
from ..lib.account import AccountClass
from ..lib.money import Rate
from ..lib.scenario import ASSUMPTION_FIELDS, AssumptionPeriod, Assumptions, Scenario
from .contracts import ServiceError, ServiceResult
from .scenarios import SavedScenario, SaveScenario, save_scenario

BASE_ASSUMPTIONS_KEY = "planning.base_assumptions"


@dataclass(frozen=True, slots=True)
class SaveBaseAssumptions:
    assumptions: Assumptions


@dataclass(frozen=True, slots=True)
class SaveAssumptionPeriod:
    scenario: str
    period: AssumptionPeriod
    index: int | None = None


@dataclass(frozen=True, slots=True)
class DeleteAssumptionPeriod:
    scenario: str
    index: int


@dataclass(frozen=True, slots=True)
class SaveScenarioAssumptions:
    scenario: Scenario
    existing_handle: str | None = None


def save_base_assumptions(db: DbSQLite, request: SaveBaseAssumptions) -> ServiceResult[Assumptions]:
    """Validate and persist the book-level Base assumptions."""
    errors = _assumption_errors(db, request.assumptions)
    if errors:
        return ServiceResult.failure(*errors)
    db.set_metadata(BASE_ASSUMPTIONS_KEY, request.assumptions.serialize())
    return ServiceResult.success(request.assumptions)


def save_scenario_assumptions(
    db: DbSQLite, request: SaveScenarioAssumptions
) -> ServiceResult[SavedScenario]:
    """Validate projection assumptions before delegating the scenario write."""
    scenario = request.scenario
    errors = list(_assumption_errors(db, scenario.assumptions))
    if scenario.years < 1 or scenario.years > 100:
        errors.append(ServiceError("assumptions.years.out_of_range", ("years",)))
    for index, period in enumerate(scenario.assumption_periods):
        for error in _period_errors(db, period):
            errors.append(
                ServiceError(
                    error.code,
                    tuple(f"periods.{index}.{field}" for field in error.fields),
                )
            )
    if errors:
        return ServiceResult.failure(*errors)
    return save_scenario(
        db,
        SaveScenario(scenario, existing_handle=request.existing_handle),
    )


def save_assumption_period(
    db: DbSQLite, request: SaveAssumptionPeriod
) -> ServiceResult[SavedScenario]:
    """Add or replace one dated assumption regime on a saved scenario."""
    scenario = db.get_scenario(request.scenario)
    if scenario is None:
        return ServiceResult.failure(ServiceError("assumptions.scenario.not_found", ("scenario",)))
    errors = _period_errors(db, request.period)
    if errors:
        return ServiceResult.failure(*errors)
    if request.index is None:
        scenario.assumption_periods.append(request.period)
    elif request.index < 0 or request.index >= len(scenario.assumption_periods):
        return ServiceResult.failure(ServiceError("assumptions.period.not_found", ("index",)))
    else:
        scenario.assumption_periods[request.index] = request.period
    return save_scenario(db, SaveScenario(scenario, existing_handle=scenario.handle))


def delete_assumption_period(
    db: DbSQLite, request: DeleteAssumptionPeriod
) -> ServiceResult[SavedScenario]:
    """Delete one dated assumption regime from a saved scenario."""
    scenario = db.get_scenario(request.scenario)
    if scenario is None:
        return ServiceResult.failure(ServiceError("assumptions.scenario.not_found", ("scenario",)))
    if request.index < 0 or request.index >= len(scenario.assumption_periods):
        return ServiceResult.failure(ServiceError("assumptions.period.not_found", ("index",)))
    del scenario.assumption_periods[request.index]
    return save_scenario(db, SaveScenario(scenario, existing_handle=scenario.handle))


def _period_errors(db: DbSQLite, period: AssumptionPeriod) -> tuple[ServiceError, ...]:
    values = {
        field: value for field in ASSUMPTION_FIELDS if (value := getattr(period, field)) is not None
    }
    return _rate_errors(db, values, period.per_account)


def _assumption_errors(db: DbSQLite, assumptions: Assumptions) -> tuple[ServiceError, ...]:
    values = {field: getattr(assumptions, field) for field in ASSUMPTION_FIELDS}
    return _rate_errors(db, values, assumptions.per_account)


def _rate_errors(
    db: DbSQLite,
    values: Mapping[str, Rate],
    per_account: Mapping[str, Rate],
) -> tuple[ServiceError, ...]:
    errors: list[ServiceError] = []
    for field, value in values.items():
        if value < Decimal("-1") or value > Decimal("1"):
            errors.append(ServiceError("assumptions.rate.out_of_range", (field,)))
    for handle, value in per_account.items():
        path = f"per_account.{handle}"
        account = db.get_account(handle)
        if account is None:
            errors.append(ServiceError("assumptions.account.not_found", (path,)))
            continue
        if not (
            account.account_class is AccountClass.LIABILITY
            or (account.account_class is AccountClass.ASSET and account.atype.is_investment)
        ):
            errors.append(ServiceError("assumptions.account.unsupported", (path,)))
        if value < Decimal("-1") or value > Decimal("1"):
            errors.append(ServiceError("assumptions.rate.out_of_range", (path,)))
    return tuple(errors)
