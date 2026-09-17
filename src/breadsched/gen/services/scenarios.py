"""Typed lifecycle mutations for saved projection scenarios."""

from __future__ import annotations

from dataclasses import dataclass

from ..db.sqlite import DbSQLite
from ..lib.base import create_handle
from ..lib.scenario import Scenario, ScenarioSchedule
from .contracts import ServiceError, ServiceResult


@dataclass(frozen=True, slots=True)
class SaveScenario:
    scenario: Scenario
    existing_handle: str | None = None


@dataclass(frozen=True, slots=True)
class DuplicateScenario:
    source: Scenario
    from_base: bool = False


@dataclass(frozen=True, slots=True)
class DeleteScenario:
    handle: str


@dataclass(frozen=True, slots=True)
class SuppressScenarioSchedule:
    scenario: str
    source_schedule: str


@dataclass(frozen=True, slots=True)
class SavedScenario:
    handle: str
    name: str


def save_scenario(db: DbSQLite, request: SaveScenario) -> ServiceResult[SavedScenario]:
    scenario = request.scenario
    scenario.name = scenario.name.strip()
    errors = _scenario_errors(db, scenario, request.existing_handle)
    if errors:
        return ServiceResult.failure(*errors)
    with db.transaction(
        f"Update scenario {scenario.name}"
        if request.existing_handle is not None
        else f"Save scenario {scenario.name}"
    ) as txn:
        if request.existing_handle is not None:
            db.commit_scenario(scenario, txn)
        else:
            db.add_scenario(scenario, txn)
    return ServiceResult.success(SavedScenario(scenario.handle, scenario.name))


def duplicate_scenario(db: DbSQLite, request: DuplicateScenario) -> ServiceResult[SavedScenario]:
    source = request.source
    clone = (
        Scenario.derived_from_base(
            source.assumptions,
            name=source.name,
            start=source.start,
            years=source.years,
        )
        if request.from_base
        else source.clone()
    )
    clone.handle = create_handle()
    clone.gid = ""
    clone.change = 0
    clone.name = _unique_copy_name(db, source.name)
    return save_scenario(db, SaveScenario(clone))


def delete_scenario(db: DbSQLite, request: DeleteScenario) -> ServiceResult[SavedScenario]:
    scenario = db.get_scenario(request.handle)
    if scenario is None:
        return ServiceResult.failure(ServiceError("scenario.not_found", ("handle",)))
    if any(item.parent_handle == scenario.handle for item in db.iter_scenarios()):
        return ServiceResult.failure(ServiceError("scenario.children.exist", ("handle",)))
    with db.transaction(f"Delete scenario {scenario.name}") as txn:
        db.remove_scenario(scenario.handle, txn)
    return ServiceResult.success(SavedScenario(scenario.handle, scenario.name))


def suppress_scenario_schedule(
    db: DbSQLite, request: SuppressScenarioSchedule
) -> ServiceResult[SavedScenario]:
    scenario = db.get_scenario(request.scenario)
    if scenario is None:
        return ServiceResult.failure(ServiceError("scenario.not_found", ("scenario",)))
    source = db.get_scheduled(request.source_schedule)
    if source is None:
        return ServiceResult.failure(
            ServiceError("scenario.schedule.not_found", ("source_schedule",))
        )
    scenario.schedule_overrides = [
        item
        for item in scenario.schedule_overrides
        if item.source_schedule != request.source_schedule
    ]
    scenario.schedule_overrides.append(ScenarioSchedule.from_scheduled(source, enabled=False))
    return save_scenario(db, SaveScenario(scenario, existing_handle=scenario.handle))


def _scenario_errors(
    db: DbSQLite, scenario: Scenario, existing_handle: str | None
) -> tuple[ServiceError, ...]:
    errors: list[ServiceError] = []
    if not scenario.name:
        errors.append(ServiceError("scenario.name.required", ("name",)))
    existing = db.get_scenario(existing_handle) if existing_handle is not None else None
    if existing_handle is not None and existing is None:
        errors.append(ServiceError("scenario.not_found", ("handle",)))
    if existing_handle is not None and scenario.handle != existing_handle:
        errors.append(ServiceError("scenario.identity.changed", ("handle",)))
    duplicate = db.get_scenario_by_name(scenario.name) if scenario.name else None
    if duplicate is not None and duplicate.handle != scenario.handle:
        errors.append(ServiceError("scenario.name.duplicate", ("name",)))
    if scenario.parent_handle is not None and not scenario.inherits_base_assumptions:
        errors.append(ServiceError("scenario.parent.requires_inheritance", ("parent",)))
    errors.extend(_parent_errors(db, scenario))
    return tuple(errors)


def _parent_errors(db: DbSQLite, scenario: Scenario) -> list[ServiceError]:
    parent_handle = scenario.parent_handle
    seen = {scenario.handle}
    while parent_handle is not None:
        if parent_handle in seen:
            return [ServiceError("scenario.parent.cycle", ("parent",))]
        seen.add(parent_handle)
        parent = db.get_scenario(parent_handle)
        if parent is None:
            return [ServiceError("scenario.parent.not_found", ("parent",))]
        parent_handle = parent.parent_handle
    return []


def _unique_copy_name(db: DbSQLite, name: str) -> str:
    base = f"{name} copy"
    candidate = base
    number = 2
    while db.get_scenario_by_name(candidate) is not None:
        candidate = f"{base} {number}"
        number += 1
    return candidate
