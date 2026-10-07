"""Typed lifecycle mutations for saved projection scenarios."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..db.sqlite import DbSQLite
from ..lib.account import AccountClass
from ..lib.base import create_handle
from ..lib.money import Money
from ..lib.scenario import Drawdown, ReimbursementOverride, Scenario, ScenarioSchedule
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


@dataclass(frozen=True, slots=True)
class SaveDrawdown:
    """Add a drawdown to a scenario, or replace one (``handle``)."""

    scenario: str
    account: str
    into: str
    start: date
    #: Exactly one of a fixed yearly amount or a yearly fraction of the balance.
    annual_amount: Money | None = None
    annual_rate: Decimal | None = None
    end: date | None = None
    #: Grow a fixed amount with the scenario's expense inflation each year.
    escalate: bool = True
    handle: str | None = None


def drawdown_accounts(db: DbSQLite) -> tuple[list[str], list[str]]:
    """Accounts a drawdown can withdraw from (holdings) and pay into (spendable cash)."""
    sources: list[str] = []
    targets: list[str] = []
    for account in db.iter_accounts():
        if account.placeholder or account.is_root or account.exclude_from_projection:
            continue
        if account.is_spendable_cash:
            targets.append(account.handle)
        elif account.account_class is AccountClass.ASSET:
            sources.append(account.handle)
    return sources, targets


def _drawdown_error(db: DbSQLite, request: SaveDrawdown) -> ServiceError | None:
    sources, targets = drawdown_accounts(db)
    if request.account not in sources:
        return ServiceError("scenario.drawdown.account", ("account",))
    if request.into not in targets:
        return ServiceError("scenario.drawdown.into", ("into",))
    if (request.annual_amount is None) == (request.annual_rate is None):
        return ServiceError("scenario.drawdown.method", ("annual_amount", "annual_rate"))
    if request.annual_amount is not None and request.annual_amount <= 0:
        return ServiceError("scenario.drawdown.amount", ("annual_amount",))
    if request.annual_rate is not None and not Decimal(0) < request.annual_rate <= 1:
        return ServiceError("scenario.drawdown.rate", ("annual_rate",))
    if request.end is not None and request.end < request.start:
        return ServiceError("scenario.drawdown.dates", ("end",))
    return None


def save_drawdown(db: DbSQLite, request: SaveDrawdown) -> ServiceResult[Drawdown]:
    """Validate and store one drawdown in its scenario as one undo step."""
    scenario = db.get_scenario(request.scenario)
    if scenario is None:
        return ServiceResult.failure(ServiceError("scenario.not_found", ("scenario",)))
    if request.handle is not None and not any(
        item.handle == request.handle for item in scenario.drawdowns
    ):
        return ServiceResult.failure(ServiceError("scenario.drawdown.not_found", ("handle",)))
    if (problem := _drawdown_error(db, request)) is not None:
        return ServiceResult.failure(problem)
    drawdown = Drawdown(
        request.account,
        request.into,
        request.start,
        annual_amount=request.annual_amount,
        annual_rate=request.annual_rate,
        end=request.end,
        escalate=request.escalate,
        handle=request.handle,
    )
    kept = [item for item in scenario.drawdowns if item.handle != drawdown.handle]
    scenario.drawdowns = [*kept, drawdown]
    with db.transaction(f"Save drawdown in {scenario.name}") as txn:
        db.commit_scenario(scenario, txn)
    return ServiceResult.success(drawdown)


def remove_drawdown(db: DbSQLite, scenario_handle: str, handle: str) -> ServiceResult[Drawdown]:
    """Remove one drawdown from its scenario as one undo step."""
    scenario = db.get_scenario(scenario_handle)
    if scenario is None:
        return ServiceResult.failure(ServiceError("scenario.not_found", ("scenario",)))
    found = [item for item in scenario.drawdowns if item.handle == handle]
    if not found:
        return ServiceResult.failure(ServiceError("scenario.drawdown.not_found", ("handle",)))
    scenario.drawdowns = [item for item in scenario.drawdowns if item.handle != handle]
    with db.transaction(f"Remove drawdown from {scenario.name}") as txn:
        db.commit_scenario(scenario, txn)
    return ServiceResult.success(found[0])


@dataclass(frozen=True, slots=True)
class SetReimbursementOverride:
    """Change, in one scenario, what a payer is expected to reimburse and when.

    ``amount`` is what the payer pays in the scenario (zero for nothing) and ``on``
    the expected date; leaving both ``None`` returns the scenario to the
    receivable's own expectation.
    """

    scenario: str
    receivable: str
    amount: Money | None = None
    on: date | None = None


def set_reimbursement_override(
    db: DbSQLite, request: SetReimbursementOverride, *, today: date | None = None
) -> ServiceResult[Scenario]:
    """Validate and store one scenario's change to an expected reimbursement.

    Only a reimbursement that is currently expected (``receivables.expected_receipt``)
    can be changed; the amount is between zero and what is still owed, and the date
    is not in the past. Returns the saved scenario.
    """
    from ..engine.receivables import expected_receipt

    scenario = db.get_scenario(request.scenario)
    if scenario is None:
        return ServiceResult.failure(ServiceError("scenario.not_found", ("scenario",)))
    receivable = db.get_receivable(request.receivable)
    if receivable is None:
        return ServiceResult.failure(ServiceError("receivable.not_found", ("receivable",)))
    day = today or date.today()
    receipt = expected_receipt(db, receivable, as_of=day)
    if receipt is None:
        return ServiceResult.failure(
            ServiceError("scenario.reimbursement.not_expected", ("receivable",))
        )
    if request.amount is not None and not Money(0) <= request.amount <= receipt.amount:
        return ServiceResult.failure(ServiceError("scenario.reimbursement.amount", ("amount",)))
    if request.on is not None and request.on < day:
        return ServiceResult.failure(ServiceError("scenario.reimbursement.date", ("on",)))
    if request.amount is None and request.on is None:
        scenario.reimbursement_overrides.pop(receivable.handle, None)
    else:
        scenario.reimbursement_overrides[receivable.handle] = ReimbursementOverride(
            request.amount, request.on
        )
    with db.transaction(f"Change expected reimbursement in {scenario.name}") as txn:
        db.commit_scenario(scenario, txn)
    return ServiceResult.success(scenario)
