"""Typed schedule mutations with one validation and transaction boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import estimates, investment
from ..engine import schedule as schedule_engine
from ..lib.money import Money
from ..lib.scenario import ScenarioSchedule
from ..lib.scheduled import ScheduledTransaction
from .contracts import ServiceError, ServiceResult


@dataclass(frozen=True, slots=True)
class SaveSchedule:
    """A complete schedule candidate and the saved identity it replaces, if any."""

    schedule: ScheduledTransaction
    existing_handle: str | None = None


@dataclass(frozen=True, slots=True)
class SavedSchedule:
    handle: str
    name: str


@dataclass(frozen=True, slots=True)
class SaveScenarioSchedule:
    """A scenario-owned candidate and the scenario that atomically owns it."""

    scenario_handle: str
    schedule: ScenarioSchedule


@dataclass(frozen=True, slots=True)
class SavedScenarioSchedule:
    scenario_handle: str
    source_schedule: str | None
    name: str


def _on_recurrence(recurrence, when: date) -> bool:
    return when in recurrence.occurrences(when, since=when)


def _candidate_errors(
    db: DbSQLite,
    candidate: ScheduledTransaction | ScenarioSchedule,
    existing: ScheduledTransaction | ScenarioSchedule | None,
) -> list[ServiceError]:
    errors: list[ServiceError] = []
    if not candidate.name.strip():
        errors.append(ServiceError("schedule.name.required", ("name",)))
    if len(candidate.splits) < 2:
        errors.append(ServiceError("schedule.splits.too_few", ("splits",)))

    for index, split in enumerate(candidate.splits):
        account = db.get_account(split.account)
        if account is None:
            errors.append(ServiceError("schedule.account.not_found", (f"splits.{index}.account",)))
        elif account.hidden and (
            existing is None or split.account not in {item.account for item in existing.splits}
        ):
            errors.append(ServiceError("schedule.account.hidden", (f"splits.{index}.account",)))

    recurrence = candidate.recurrence
    if recurrence.end is not None and recurrence.end < recurrence.start:
        errors.append(ServiceError("schedule.recurrence.end_before_start", ("recurrence.end",)))
    if recurrence.count is not None and recurrence.count < 1:
        errors.append(ServiceError("schedule.recurrence.count_invalid", ("recurrence.count",)))

    def unique_dates(values: list[date], code: str, field: str) -> None:
        if len(values) != len(set(values)):
            errors.append(ServiceError(code, (field,)))

    amount_dates = [item.start for item in candidate.amount_changes]
    unique_dates(amount_dates, "schedule.amount_changes.duplicate", "amount_changes")
    if any(when < recurrence.start for when in amount_dates):
        errors.append(ServiceError("schedule.amount_changes.before_start", ("amount_changes",)))
    months = [item.month for item in candidate.seasonal_amounts]
    if len(months) != len(set(months)):
        errors.append(ServiceError("schedule.seasonal_amounts.duplicate", ("seasonal_amounts",)))

    unique_dates(candidate.skipped, "schedule.skipped.duplicate", "skipped")
    if any(not _on_recurrence(recurrence, when) for when in candidate.skipped):
        errors.append(ServiceError("schedule.skipped.not_occurrence", ("skipped",)))
    adjustment_dates = [item.when for item in candidate.occurrence_adjustments]
    unique_dates(
        adjustment_dates,
        "schedule.occurrence_adjustments.duplicate",
        "occurrence_adjustments",
    )
    if any(not _on_recurrence(recurrence, when) for when in adjustment_dates):
        errors.append(
            ServiceError(
                "schedule.occurrence_adjustments.not_occurrence",
                ("occurrence_adjustments",),
            )
        )
    if set(candidate.skipped) & set(adjustment_dates):
        errors.append(
            ServiceError(
                "schedule.occurrence_conflict",
                ("skipped", "occurrence_adjustments"),
            )
        )

    split_change_dates: set[date] = set()
    for index, split in enumerate(candidate.splits):
        dates = [item.start for item in split.amount_changes]
        unique_dates(
            dates,
            "schedule.split_amount_changes.duplicate",
            f"splits.{index}.amount_changes",
        )
        if any(when < recurrence.start for when in dates):
            errors.append(
                ServiceError(
                    "schedule.split_amount_changes.before_start",
                    (f"splits.{index}.amount_changes",),
                )
            )
        split_change_dates.update(dates)
    for when in sorted(split_change_dates):
        if sum(
            (split.resolve(candidate.variables, when=when) for split in candidate.splits),
            Money(0),
        ) != Money(0):
            errors.append(ServiceError("schedule.split_amount_changes.unbalanced", ("splits",)))
            break

    if isinstance(existing, ScheduledTransaction):
        editability = schedule_engine.schedule_editability(db, existing)
        if not editability.editable:
            errors.append(ServiceError("schedule.read_only", ("handle",)))
    if existing is not None and any(split.formula for split in existing.splits):
        existing_indices = tuple(
            index for index, split in enumerate(existing.splits) if split.formula
        )
        candidate_indices = tuple(
            index for index, split in enumerate(candidate.splits) if split.formula
        )
        if existing_indices != candidate_indices:
            errors.append(ServiceError("schedule.formula.ownership", ("splits",)))
        protected_existing = [
            (
                split.account,
                split.amount,
                split.memo,
                split.planning_flow,
                split.investment_activity,
            )
            for split in existing.splits
        ]
        protected_candidate = [
            (
                split.account,
                split.amount,
                split.memo,
                split.planning_flow,
                split.investment_activity,
            )
            for split in candidate.splits
        ]
        if protected_existing != protected_candidate:
            errors.append(ServiceError("schedule.formula.ownership", ("splits",)))

    try:
        estimates.validate_historical_estimate_adjustment(db, candidate)
    except ValueError:
        errors.append(ServiceError("schedule.estimate.invalid", ("estimate_evidence",)))
    if investment.scheduled_activity_problems(db, candidate):
        errors.append(ServiceError("schedule.investment.invalid", ("splits",)))
    return errors


def save_schedule(db: DbSQLite, request: SaveSchedule) -> ServiceResult[SavedSchedule]:
    """Validate and atomically create or replace one baseline schedule."""
    candidate = ScheduledTransaction.from_dict(request.schedule.serialize())
    existing = db.get_scheduled(request.existing_handle) if request.existing_handle else None
    if request.existing_handle and existing is None:
        return ServiceResult.failure(ServiceError("schedule.not_found", ("handle",)))
    if existing is not None and candidate.handle != existing.handle:
        return ServiceResult.failure(ServiceError("schedule.identity.changed", ("handle",)))
    errors = _candidate_errors(db, candidate, existing)
    if errors:
        return ServiceResult.failure(*errors)

    action = "Add" if existing is None else "Update"
    with db.transaction(f"{action} scheduled {candidate.name}") as txn:
        if existing is None:
            db.add_scheduled(candidate, txn)
        else:
            db.commit_scheduled(candidate, txn)
    return ServiceResult.success(SavedSchedule(candidate.handle, candidate.name))


def save_scenario_schedule(
    db: DbSQLite,
    request: SaveScenarioSchedule,
) -> ServiceResult[SavedScenarioSchedule]:
    """Validate and atomically replace/add one scenario-owned recurring event."""
    scenario = db.get_scenario(request.scenario_handle)
    if scenario is None:
        return ServiceResult.failure(ServiceError("scenario.not_found", ("scenario",)))
    candidate = ScenarioSchedule.from_dict(request.schedule.serialize())
    existing: ScheduledTransaction | ScenarioSchedule | None = None
    if candidate.source_schedule is not None:
        existing = next(
            (
                item
                for item in scenario.schedule_overrides
                if item.source_schedule == candidate.source_schedule
            ),
            None,
        ) or db.get_scheduled(candidate.source_schedule)
        if existing is None:
            return ServiceResult.failure(
                ServiceError("schedule.source.not_found", ("source_schedule",))
            )
    errors = _candidate_errors(db, candidate, existing)
    if errors:
        return ServiceResult.failure(*errors)

    updated = scenario.clone()
    if candidate.source_schedule is not None:
        updated.schedule_overrides = [
            item
            for item in updated.schedule_overrides
            if item.source_schedule != candidate.source_schedule
        ]
    updated.schedule_overrides.append(candidate)
    with db.transaction(f"Update scenario {updated.name}") as txn:
        db.commit_scenario(updated, txn)
    return ServiceResult.success(
        SavedScenarioSchedule(updated.handle, candidate.source_schedule, candidate.name)
    )
