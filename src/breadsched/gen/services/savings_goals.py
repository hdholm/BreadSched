"""Create, fund, close, and report savings goals for CLI, GTK, and web.

Every write is one undoable database transaction, and a rejected request leaves
the stored goal unchanged. A goal never posts ledger transactions: contributions
are ordinary transfers the household records, never expenses. See
``lib.savings_goal`` and ``engine.savings_goals`` for the model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine.currency import reporting_currency_handle
from ..engine.savings_goals import GoalProgress, goals_progress, spendable_hold
from ..lib.account import AccountClass
from ..lib.money import Money
from ..lib.savings_goal import GoalAllocation, SavingsGoal
from .contracts import ServiceError, ServiceResult

__all__ = [
    "AllocateToGoal",
    "GoalProgress",
    "SaveSavingsGoal",
    "SavingsGoalReport",
    "allocate_to_goal",
    "close_savings_goal",
    "delete_savings_goal",
    "goal_accounts",
    "query_savings_goals",
    "reopen_savings_goal",
    "save_savings_goal",
]


@dataclass(frozen=True, slots=True)
class SaveSavingsGoal:
    name: str
    account: str
    target_amount: Money
    target_date: date
    start_date: date
    description: str = ""
    #: The goal to update; ``None`` creates one.
    handle: str | None = None


@dataclass(frozen=True, slots=True)
class AllocateToGoal:
    goal: str
    amount: Money
    allocated_on: date
    memo: str = ""


@dataclass(frozen=True, slots=True)
class SavingsGoalReport:
    as_of: date
    goals: tuple[GoalProgress, ...]
    #: Everything set aside for goals, and the part held from spendable cash.
    set_aside: Money
    held: Money


def goal_accounts(db: DbSQLite) -> list[tuple[str, str]]:
    """(handle, full name) of every account that can hold a goal's money."""
    reporting = reporting_currency_handle(db)
    found = [
        (account.handle, db.full_name(account))
        for account in db.iter_accounts()
        if not account.is_root
        and not account.placeholder
        and account.account_class is AccountClass.ASSET
        and (account.commodity or reporting) == reporting
    ]
    return sorted(found, key=lambda item: item[1].casefold())


def _validate(db: DbSQLite, request: SaveSavingsGoal) -> list[ServiceError]:
    errors: list[ServiceError] = []
    if not " ".join(request.name.split()):
        errors.append(ServiceError("savings_goal.name.required", ("name",)))
    if request.account not in {handle for handle, _name in goal_accounts(db)}:
        errors.append(ServiceError("savings_goal.account.invalid", ("account",)))
    if request.target_amount <= 0:
        errors.append(ServiceError("savings_goal.target.invalid", ("target_amount",)))
    if request.target_date <= request.start_date:
        errors.append(ServiceError("savings_goal.dates.invalid", ("start_date", "target_date")))
    return errors


def save_savings_goal(db: DbSQLite, request: SaveSavingsGoal) -> ServiceResult[SavingsGoal]:
    """Create a goal, or replace one's name, account, target, and dates."""
    existing = db.get_savings_goal(request.handle) if request.handle is not None else None
    if request.handle is not None and existing is None:
        return ServiceResult.failure(ServiceError("savings_goal.not_found", ("handle",)))
    errors = _validate(db, request)
    if existing is not None and request.target_amount < existing.allocated(date.max):
        errors.append(ServiceError("savings_goal.target.below_allocated", ("target_amount",)))
    if errors:
        return ServiceResult.failure(*errors)
    goal = existing or SavingsGoal()
    goal.name = " ".join(request.name.split())
    goal.account = request.account
    goal.target_amount = request.target_amount
    goal.target_date = request.target_date
    goal.start_date = request.start_date
    goal.description = request.description.strip()
    with db.transaction(f"Save savings goal {goal.name}") as txn:
        if existing is None:
            db.add_savings_goal(goal, txn)
        else:
            db.commit_savings_goal(goal, txn)
    return ServiceResult.success(goal)


def allocate_to_goal(db: DbSQLite, request: AllocateToGoal) -> ServiceResult[SavingsGoal]:
    """Set extra money aside for a goal; later income then spreads only the rest."""
    goal = db.get_savings_goal(request.goal)
    if goal is None:
        return ServiceResult.failure(ServiceError("savings_goal.not_found", ("goal",)))
    if not goal.is_open(request.allocated_on):
        return ServiceResult.failure(ServiceError("savings_goal.closed", ("allocated_on",)))
    if request.amount <= 0:
        return ServiceResult.failure(ServiceError("savings_goal.allocation.invalid", ("amount",)))
    if goal.allocated(date.max) + request.amount > goal.target_amount:
        return ServiceResult.failure(
            ServiceError("savings_goal.allocation.exceeds_target", ("amount",))
        )
    goal.allocations = sorted(
        [*goal.allocations, GoalAllocation(request.allocated_on, request.amount, request.memo)],
        key=lambda item: item.allocated_on,
    )
    with db.transaction(f"Allocate to savings goal {goal.name}") as txn:
        db.commit_savings_goal(goal, txn)
    return ServiceResult.success(goal)


def close_savings_goal(db: DbSQLite, handle: str, closed_on: date) -> ServiceResult[SavingsGoal]:
    """Release a goal's earmark from ``closed_on``, e.g. after the purchase."""
    goal = db.get_savings_goal(handle)
    if goal is None:
        return ServiceResult.failure(ServiceError("savings_goal.not_found", ("handle",)))
    if goal.closed_on is not None:
        return ServiceResult.failure(ServiceError("savings_goal.closed", ("handle",)))
    if closed_on < goal.start_date:
        return ServiceResult.failure(
            ServiceError("savings_goal.close.before_start", ("closed_on",))
        )
    goal.closed_on = closed_on
    with db.transaction(f"Close savings goal {goal.name}") as txn:
        db.commit_savings_goal(goal, txn)
    return ServiceResult.success(goal)


def reopen_savings_goal(db: DbSQLite, handle: str) -> ServiceResult[SavingsGoal]:
    goal = db.get_savings_goal(handle)
    if goal is None:
        return ServiceResult.failure(ServiceError("savings_goal.not_found", ("handle",)))
    if goal.closed_on is None:
        return ServiceResult.failure(ServiceError("savings_goal.open", ("handle",)))
    goal.closed_on = None
    with db.transaction(f"Reopen savings goal {goal.name}") as txn:
        db.commit_savings_goal(goal, txn)
    return ServiceResult.success(goal)


def delete_savings_goal(db: DbSQLite, handle: str) -> ServiceResult[str]:
    """Delete a goal; no ledger transaction is touched."""
    goal = db.get_savings_goal(handle)
    if goal is None:
        return ServiceResult.failure(ServiceError("savings_goal.not_found", ("handle",)))
    with db.transaction(f"Delete savings goal {goal.name}") as txn:
        db.remove_savings_goal(handle, txn)
    return ServiceResult.success(handle)


def query_savings_goals(
    db: DbSQLite, as_of: date, *, include_closed: bool = False
) -> ServiceResult[SavingsGoalReport]:
    """Every goal's earmark on ``as_of``, by target date."""
    progress = [
        item for item in goals_progress(db, as_of) if include_closed or item.status != "closed"
    ]
    progress.sort(key=lambda item: (item.goal.target_date, item.goal.name.casefold()))
    return ServiceResult.success(
        SavingsGoalReport(
            as_of,
            tuple(progress),
            sum((item.set_aside for item in progress), Money(0)),
            spendable_hold(progress),
        )
    )
