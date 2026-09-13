"""Shared validation and context for explicitly classified investment activity."""

from __future__ import annotations

from collections.abc import Iterable

from ..db.base import DbBase
from ..lib.account import Account, AccountType
from ..lib.money import Money
from ..lib.scenario import ScenarioSchedule
from ..lib.scheduled import ScheduledTransaction
from ..lib.transaction import InvestmentActivityKind, Split

__all__ = [
    "activity_problems",
    "retirement_context",
    "scheduled_activity_problems",
]


def retirement_context(db: DbBase, account: Account | str) -> bool:
    """Whether an account is, or is nested beneath, a retirement wrapper."""
    current = db.get_account(account) if isinstance(account, str) else account
    seen: set[str] = set()
    while current is not None and current.handle not in seen:
        if current.atype is AccountType.RETIREMENT:
            return True
        seen.add(current.handle)
        current = db.get_account(current.parent) if current.parent is not None else None
    return False


def activity_problems(db: DbBase, splits: Iterable[Split]) -> list[str]:
    """Return semantic problems in classified concrete split activity."""
    rows = [split for split in splits if split.investment_activity is not None]
    return _activity_problems(
        db,
        [(split.account, split.value, split.investment_activity) for split in rows],
    )


def scheduled_activity_problems(
    db: DbBase,
    schedule: ScheduledTransaction | ScenarioSchedule,
) -> list[str]:
    """Validate classifications at a schedule's first occurrence."""
    resolved = schedule.resolved_splits(when=schedule.recurrence.start)
    return _activity_problems(
        db,
        [
            (account, amount, split.investment_activity)
            for split, (account, amount) in zip(schedule.splits, resolved, strict=True)
            if split.investment_activity is not None
        ],
    )


def _activity_problems(
    db: DbBase,
    rows: list[tuple[str, Money, InvestmentActivityKind | None]],
) -> list[str]:
    problems: list[str] = []
    rollovers: list[tuple[str, Money]] = []
    for account_handle, amount, kind in rows:
        if kind is None:
            continue
        account = db.get_account(account_handle)
        if account is None:
            continue
        if not account.atype.is_investment:
            problems.append(f"{kind.label} must be attached to an investment account split")
            continue
        in_retirement = retirement_context(db, account)
        if kind is InvestmentActivityKind.WITHDRAWAL and in_retirement:
            problems.append("use Retirement distribution for a withdrawal from retirement")
        elif kind is InvestmentActivityKind.RETIREMENT_DISTRIBUTION and not in_retirement:
            problems.append("a Retirement distribution requires a retirement account")
        elif kind is InvestmentActivityKind.ROLLOVER:
            if not in_retirement:
                problems.append("a Retirement rollover requires retirement accounts")
            rollovers.append((account_handle, amount))
        elif kind.direction and amount * kind.direction <= 0:
            direction = "increase" if kind.direction > 0 else "decrease"
            problems.append(f"{kind.label} must {direction} the investment holding")

    if rollovers:
        if len({handle for handle, _amount in rollovers}) < 2:
            problems.append("a Retirement rollover requires two retirement accounts")
        if sum((amount for _handle, amount in rollovers), Money(0)):
            problems.append("retirement rollover investment legs must balance each other")
    return list(dict.fromkeys(problems))
