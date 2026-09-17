"""Typed schedule mutations with one validation and transaction boundary."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import estimates, investment
from ..engine import schedule as schedule_engine
from ..lib.account import AccountClass
from ..lib.formula import FormulaError, evaluate
from ..lib.money import Money
from ..lib.recurrence import Recurrence
from ..lib.scenario import ScenarioSchedule
from ..lib.scheduled import (
    ScheduledAmountChange,
    ScheduledMonthAmount,
    ScheduledOccurrenceAdjustment,
    ScheduledSplit,
    ScheduledSplitAmountChange,
    ScheduledTransaction,
    ScheduleGrowthPolicy,
)
from ..lib.transaction import InvestmentActivityKind, PlanningFlowKind
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
class DuplicateSchedule:
    handle: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class DeleteSchedule:
    handle: str


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


@dataclass(frozen=True, slots=True)
class FixedSplitInput:
    """One additional fixed leg expressed in positive planning terms."""

    account: str
    amount: Money
    planning_flow: PlanningFlowKind | None = None
    investment_activity: InvestmentActivityKind | None = None
    memo: str = ""
    opposite_direction: bool = False


@dataclass(frozen=True, slots=True)
class FixedScheduleInput:
    """All typed fields from which the service constructs a fixed definition."""

    name: str
    recurrence: Recurrence
    category: str
    funding: str
    amount: Money
    category_planning_flow: PlanningFlowKind | None = None
    funding_planning_flow: PlanningFlowKind | None = None
    investment_activity: InvestmentActivityKind | None = None
    category_ledger_direction: int | None = None
    additional_splits: tuple[FixedSplitInput, ...] = ()
    category_memo: str = ""
    funding_memo: str = ""
    enabled: bool = True
    auto_create: bool = False
    placeholder: bool = False
    growth_policy: ScheduleGrowthPolicy = ScheduleGrowthPolicy.AUTO
    amount_changes: tuple[ScheduledAmountChange, ...] = ()
    split_amount_changes: Mapping[str, tuple[ScheduledSplitAmountChange, ...]] | None = None
    seasonal_amounts: tuple[ScheduledMonthAmount, ...] = ()
    skipped: tuple[date, ...] = ()
    occurrence_adjustments: tuple[ScheduledOccurrenceAdjustment, ...] = ()
    estimate_evidence: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class SaveFixedSchedule:
    definition: FixedScheduleInput
    existing_handle: str | None = None
    source: ScheduledTransaction | None = None


@dataclass(frozen=True, slots=True)
class SaveFixedScenarioSchedule:
    scenario_handle: str
    definition: FixedScheduleInput
    source_schedule: str | None = None
    source: ScheduledTransaction | None = None
    current: ScenarioSchedule | None = None


@dataclass(frozen=True, slots=True)
class FormulaScheduleInput:
    """The only fields a formula-owned baseline definition permits adapters to edit."""

    existing_handle: str | None
    name: str
    recurrence: Recurrence
    formulas: Mapping[int, str]
    variables: Mapping[str, str]
    enabled: bool
    auto_create: bool
    placeholder: bool
    growth_policy: ScheduleGrowthPolicy
    skipped: tuple[date, ...] = ()
    source: ScheduledTransaction | None = None


@dataclass(frozen=True, slots=True)
class FormulaScenarioScheduleInput:
    scenario_handle: str
    source_schedule: str
    name: str
    recurrence: Recurrence
    formulas: Mapping[int, str]
    variables: Mapping[str, str]
    growth_policy: ScheduleGrowthPolicy
    skipped: tuple[date, ...] = ()
    source: ScheduledTransaction | None = None
    current: ScenarioSchedule | None = None


def _on_recurrence(recurrence, when: date) -> bool:
    return when in recurrence.occurrences(when, since=when)


def _fixed_splits(
    db: DbSQLite,
    definition: FixedScheduleInput,
    source: ScheduledTransaction | ScenarioSchedule | None = None,
) -> ServiceResult[list[ScheduledSplit]]:
    errors: list[ServiceError] = []
    category = db.get_account(definition.category)
    funding = db.get_account(definition.funding)
    if category is None:
        errors.append(ServiceError("schedule.account.not_found", ("category",)))
    if funding is None:
        errors.append(ServiceError("schedule.account.not_found", ("funding",)))
    if definition.category == definition.funding:
        errors.append(ServiceError("schedule.accounts.same", ("category", "funding")))
    selected = Counter((definition.category, definition.funding))
    source_counts = Counter(split.account for split in source.splits) if source is not None else {}
    for index, split in enumerate(definition.additional_splits):
        selected[split.account] += 1
        if selected[split.account] > 1 and selected[split.account] > source_counts.get(
            split.account, 1
        ):
            errors.append(
                ServiceError("schedule.accounts.duplicate", (f"additional_splits.{index}.account",))
            )
        if db.get_account(split.account) is None:
            errors.append(
                ServiceError(
                    "schedule.account.not_found",
                    (f"additional_splits.{index}.account",),
                )
            )
        if split.amount <= 0:
            errors.append(
                ServiceError("schedule.amount.non_positive", (f"additional_splits.{index}.amount",))
            )
    if definition.amount <= 0:
        errors.append(ServiceError("schedule.amount.non_positive", ("amount",)))
    if definition.category_planning_flow is not None and definition.investment_activity is not None:
        errors.append(
            ServiceError(
                "schedule.category.classification_conflict",
                ("category_planning_flow", "investment_activity"),
            )
        )
    if category is not None and (
        category.account_class not in {AccountClass.INCOME, AccountClass.EXPENSE}
        and definition.category_planning_flow is None
        and definition.investment_activity is None
        and definition.category_ledger_direction not in {-1, 1}
    ):
        errors.append(ServiceError("schedule.category.role_required", ("category",)))
    if errors:
        return ServiceResult.failure(*errors)
    assert category is not None

    category_value = (
        definition.category_planning_flow.ledger_amount(definition.amount)
        if definition.category_planning_flow is not None
        else definition.amount * definition.investment_activity.direction
        if definition.investment_activity is not None and definition.investment_activity.direction
        else definition.amount * definition.category_ledger_direction
        if definition.category_ledger_direction is not None
        else definition.amount * category.sign()
    )
    additional: list[ScheduledSplit] = []
    additional_total = Money(0)
    for item in definition.additional_splits:
        account = db.get_account(item.account)
        assert account is not None
        value = (
            item.amount * item.investment_activity.direction
            if item.investment_activity is not None and item.investment_activity.direction
            else item.planning_flow.ledger_amount(item.amount)
            if item.planning_flow is not None
            else item.amount * account.sign() * (-1 if item.opposite_direction else 1)
        )
        additional_total += value
        additional.append(
            ScheduledSplit(
                item.account,
                value,
                memo=item.memo,
                planning_flow=item.planning_flow,
                investment_activity=item.investment_activity,
            )
        )
    return ServiceResult.success(
        [
            ScheduledSplit(
                definition.category,
                category_value,
                memo=definition.category_memo,
                planning_flow=definition.category_planning_flow,
                investment_activity=definition.investment_activity,
            ),
            *additional,
            ScheduledSplit(
                definition.funding,
                -(category_value + additional_total),
                memo=definition.funding_memo,
                planning_flow=definition.funding_planning_flow,
                investment_activity=(
                    InvestmentActivityKind.ROLLOVER
                    if definition.investment_activity is InvestmentActivityKind.ROLLOVER
                    else None
                ),
            ),
        ]
    )


def build_fixed_schedule(
    db: DbSQLite,
    request: SaveFixedSchedule,
) -> ServiceResult[ScheduledTransaction]:
    """Construct a lossless baseline candidate from typed presentation input."""
    existing = request.source
    if existing is None and request.existing_handle:
        existing = db.get_scheduled(request.existing_handle)
    if request.existing_handle and existing is None:
        return ServiceResult.failure(ServiceError("schedule.not_found", ("handle",)))
    splits = _fixed_splits(db, request.definition, existing)
    if splits.value is None:
        return ServiceResult.failure(*splits.errors)
    candidate = (
        ScheduledTransaction.from_dict(existing.serialize())
        if existing is not None
        else ScheduledTransaction()
    )
    old_name = candidate.name
    definition = request.definition
    candidate.name = definition.name.strip()
    if existing is None or candidate.description == old_name:
        candidate.description = candidate.name
    candidate.recurrence = Recurrence.from_dict(definition.recurrence.serialize())
    candidate.splits = splits.value
    for split in candidate.splits:
        split.amount_changes = list((definition.split_amount_changes or {}).get(split.account, ()))
    candidate.enabled = definition.enabled
    candidate.auto_create = definition.auto_create and not definition.placeholder
    candidate.placeholder = definition.placeholder
    candidate.growth_policy = definition.growth_policy
    candidate.amount_changes = list(definition.amount_changes)
    candidate.seasonal_amounts = list(definition.seasonal_amounts)
    candidate.skipped = list(definition.skipped)
    candidate.occurrence_adjustments = list(definition.occurrence_adjustments)
    if definition.estimate_evidence is not None or existing is None:
        candidate.estimate_evidence = definition.estimate_evidence
    return ServiceResult.success(candidate)


def build_formula_schedule(
    db: DbSQLite,
    request: FormulaScheduleInput,
) -> ServiceResult[ScheduledTransaction]:
    """Clone a formula-owned definition and replace only its permitted inputs."""
    existing = request.source
    if existing is None and request.existing_handle:
        existing = db.get_scheduled(request.existing_handle)
    if existing is None:
        return ServiceResult.failure(ServiceError("schedule.not_found", ("handle",)))
    try:
        candidate = schedule_engine.apply_formula_inputs(
            db,
            existing,
            dict(request.formulas),
            dict(request.variables),
        )
    except ValueError:
        return ServiceResult.failure(ServiceError("schedule.formula.invalid", ("formulas",)))
    old_name = candidate.name
    candidate.name = request.name.strip()
    if candidate.description == old_name:
        candidate.description = candidate.name
    candidate.recurrence = Recurrence.from_dict(request.recurrence.serialize())
    candidate.enabled = request.enabled
    candidate.auto_create = request.auto_create and not request.placeholder
    candidate.placeholder = request.placeholder
    candidate.growth_policy = request.growth_policy
    candidate.skipped = list(request.skipped)
    return ServiceResult.success(candidate)


def save_formula_schedule(
    db: DbSQLite,
    request: FormulaScheduleInput,
) -> ServiceResult[SavedSchedule]:
    built = build_formula_schedule(db, request)
    if built.value is None:
        return ServiceResult.failure(*built.errors)
    return save_schedule(db, SaveSchedule(built.value, request.existing_handle))


def build_formula_scenario_schedule(
    db: DbSQLite,
    request: FormulaScenarioScheduleInput,
) -> ServiceResult[ScenarioSchedule]:
    scenario = db.get_scenario(request.scenario_handle)
    source = request.source or db.get_scheduled(request.source_schedule)
    if source is None:
        return ServiceResult.failure(
            ServiceError("schedule.source.not_found", ("source_schedule",))
        )
    current = request.current
    if current is None and scenario is not None:
        current = next(
            (
                item
                for item in scenario.schedule_overrides
                if item.source_schedule == request.source_schedule
            ),
            None,
        )
    candidate = (
        ScenarioSchedule.from_dict(current.serialize())
        if current is not None
        else ScenarioSchedule.from_scheduled(source)
    )
    expected = {index for index, split in enumerate(candidate.splits) if split.formula}
    if set(request.formulas) != expected:
        return ServiceResult.failure(ServiceError("schedule.formula.ownership", ("formulas",)))
    variables: dict[str, str] = {}
    for name, value in request.variables.items():
        if not name.isidentifier() or not value.strip():
            return ServiceResult.failure(
                ServiceError("schedule.formula.variables.invalid", ("variables",))
            )
        try:
            evaluate(value, {})
        except (FormulaError, ValueError, ArithmeticError):
            return ServiceResult.failure(
                ServiceError("schedule.formula.variables.invalid", ("variables",))
            )
        variables[name] = value.strip()
    for index, expression in request.formulas.items():
        if not expression.strip():
            return ServiceResult.failure(
                ServiceError("schedule.formula.invalid", (f"formulas.{index}",))
            )
        candidate.splits[index].formula = expression.strip()
    candidate.variables = variables
    check = ScheduledTransaction(
        name=candidate.name,
        recurrence=Recurrence.from_dict(request.recurrence.serialize()),
        splits=[ScheduledSplit.from_dict(split.serialize()) for split in candidate.splits],
    )
    check.variables = dict(variables)
    if check.formula_problem():
        return ServiceResult.failure(ServiceError("schedule.formula.invalid", ("formulas",)))
    old_name = candidate.name
    candidate.name = request.name.strip()
    if candidate.description == old_name:
        candidate.description = candidate.name
    candidate.recurrence = Recurrence.from_dict(request.recurrence.serialize())
    candidate.growth_policy = request.growth_policy
    candidate.skipped = list(request.skipped)
    candidate.source_schedule = request.source_schedule
    return ServiceResult.success(candidate)


def save_formula_scenario_schedule(
    db: DbSQLite,
    request: FormulaScenarioScheduleInput,
) -> ServiceResult[SavedScenarioSchedule]:
    built = build_formula_scenario_schedule(db, request)
    if built.value is None:
        return ServiceResult.failure(*built.errors)
    return save_scenario_schedule(
        db,
        SaveScenarioSchedule(request.scenario_handle, built.value),
    )


def save_fixed_schedule(
    db: DbSQLite,
    request: SaveFixedSchedule,
) -> ServiceResult[SavedSchedule]:
    built = build_fixed_schedule(db, request)
    if built.value is None:
        return ServiceResult.failure(*built.errors)
    return save_schedule(db, SaveSchedule(built.value, request.existing_handle))


def build_fixed_scenario_schedule(
    db: DbSQLite,
    request: SaveFixedScenarioSchedule,
) -> ServiceResult[ScenarioSchedule]:
    """Construct a scenario candidate without adapter-owned split rules."""
    source = request.source
    if source is None and request.source_schedule:
        source = db.get_scheduled(request.source_schedule)
    if request.source_schedule and source is None:
        return ServiceResult.failure(
            ServiceError("schedule.source.not_found", ("source_schedule",))
        )
    splits = _fixed_splits(db, request.definition, request.current or source)
    if splits.value is None:
        return ServiceResult.failure(*splits.errors)
    definition = request.definition
    candidate = ScenarioSchedule(
        name=definition.name.strip(),
        recurrence=Recurrence.from_dict(definition.recurrence.serialize()),
        splits=splits.value,
        source_schedule=request.source_schedule,
        enabled=definition.enabled,
        placeholder=definition.placeholder if source is None else source.placeholder,
        growth_policy=definition.growth_policy,
        amount_changes=list(definition.amount_changes),
        seasonal_amounts=list(definition.seasonal_amounts),
        skipped=list(definition.skipped),
        occurrence_adjustments=list(definition.occurrence_adjustments),
        estimate_evidence=(
            definition.estimate_evidence
            if definition.estimate_evidence is not None
            else source.estimate_evidence
            if source is not None
            else None
        ),
    )
    return ServiceResult.success(candidate)


def save_fixed_scenario_schedule(
    db: DbSQLite,
    request: SaveFixedScenarioSchedule,
) -> ServiceResult[SavedScenarioSchedule]:
    built = build_fixed_scenario_schedule(db, request)
    if built.value is None:
        return ServiceResult.failure(*built.errors)
    return save_scenario_schedule(
        db,
        SaveScenarioSchedule(request.scenario_handle, built.value),
    )


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


def duplicate_schedule(db: DbSQLite, request: DuplicateSchedule) -> ServiceResult[SavedSchedule]:
    """Persist an independent exact copy, including protected custom structure."""
    source = db.get_scheduled(request.handle)
    if source is None:
        return ServiceResult.failure(ServiceError("schedule.not_found", ("handle",)))
    candidate = schedule_engine.duplicate_definition(source)
    if request.name is not None:
        chosen = request.name.strip()
        if not chosen:
            return ServiceResult.failure(ServiceError("schedule.name.required", ("name",)))
        old_name = candidate.name
        candidate.name = chosen
        if candidate.description == old_name:
            candidate.description = chosen
    return save_schedule(db, SaveSchedule(candidate))


def delete_schedule(db: DbSQLite, request: DeleteSchedule) -> ServiceResult[SavedSchedule]:
    """Delete a baseline definition without leaving live scenario references."""
    source = db.get_scheduled(request.handle)
    if source is None:
        return ServiceResult.failure(ServiceError("schedule.not_found", ("handle",)))
    if any(
        item.source_schedule == request.handle
        for scenario in db.iter_scenarios()
        for item in scenario.schedule_overrides
    ):
        return ServiceResult.failure(
            ServiceError("schedule.scenario_reference.exists", ("handle",))
        )
    with db.transaction(f"Delete scheduled {source.name}") as txn:
        db.remove_scheduled(source.handle, txn)
    return ServiceResult.success(SavedSchedule(source.handle, source.name))


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
