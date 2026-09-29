"""Savings goals inside a projection: earmarks by month and target-date milestones.

Goals are pinned: every scenario carries every open goal unless its
``goal_overrides`` change the target amount or date, or leave the goal out.

The projection starts from what each goal has actually set aside on the day
before the scenario starts, then applies the same rule as the Dashboard to the
scenario's own projected income. Each month sets aside the gap still open times
that month's income over the income projected from that month through the target
month, so the whole target is set aside by the target month. Projected months
are the unit, not days. Extra allocations dated in the horizon are set aside in
the month they fall in. Without projected income, the gap is spread by day. A
goal spends nothing: after its target month the whole target stays set aside.

Money set aside for a goal held in a cash account is held from projected cash,
so ``cash_after_goals`` shows what a scenario leaves spendable. A goal held in a
non-cash account is reported but not compared with cash, because a projection
row does not carry that account's projected balance.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction

from ..db.sqlite import DbSQLite
from ..lib.money import Money
from ..lib.savings_goal import SavingsGoal
from ..lib.scenario import Scenario
from .currency import reporting_fraction
from .savings_goals import goal_progress

__all__ = ["GoalMilestone", "GoalMonth", "effective_goals", "project_goals"]


@dataclass(frozen=True, slots=True)
class GoalMonth:
    """The projected earmarks at one month's end."""

    set_aside: Money
    #: The part held from projected cash (goals in cash accounts).
    held: Money


@dataclass(frozen=True, slots=True)
class GoalMilestone:
    """A goal's target date as it falls in one scenario's projection."""

    goal: SavingsGoal
    target: Money
    target_date: date
    overridden: bool
    cash_account: bool
    #: Projection row of the target month; ``None`` when it is outside the horizon.
    month_index: int | None
    set_aside: Money | None
    cash_close: Money | None
    goals_held: Money | None

    @property
    def covered(self) -> bool | None:
        """Whether projected cash covers every earmark held from it that month."""
        if self.cash_close is None or self.goals_held is None or not self.cash_account:
            return None
        return self.cash_close >= self.goals_held


def effective_goals(
    db: DbSQLite, scenario: Scenario, as_of: date
) -> list[tuple[SavingsGoal, bool]]:
    """Every goal open on ``as_of`` with this scenario's changes, and whether it changed."""
    found: list[tuple[SavingsGoal, bool]] = []
    for goal in db.iter_savings_goals():
        if not goal.is_open(as_of):
            continue
        override = scenario.goal_overrides.get(goal.handle)
        if override is None:
            found.append((goal, False))
            continue
        if override.excluded:
            continue
        changed = SavingsGoal.from_dict(goal.serialize())
        if override.target_amount is not None:
            changed.target_amount = override.target_amount
        if override.target_date is not None:
            changed.target_date = override.target_date
        found.append((changed, True))
    return sorted(found, key=lambda item: (item[0].target_date, item[0].name.casefold()))


def project_goals(
    db: DbSQLite,
    scenario: Scenario,
    months: Sequence[date],
    income: Sequence[Money],
    cash_close: Sequence[Money],
) -> tuple[list[GoalMonth], list[GoalMilestone]]:
    """Month-end earmarks for every row, and each goal's target-date milestone."""
    if not months:
        return [], []
    fraction = reporting_fraction(db)
    day_before = months[0] - timedelta(days=1)
    ends = [
        (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        for month in months
    ]
    set_aside = [Money(0)] * len(months)
    held = [Money(0)] * len(months)
    earmarks: list[tuple[SavingsGoal, bool, bool, list[Money]]] = []
    for goal, overridden in effective_goals(db, scenario, day_before):
        account = db.get_account(goal.account)
        cash = account is not None and account.atype.is_cash_like and not account.placeholder
        target = goal.target_amount
        current = min(target, goal_progress(db, goal, day_before).set_aside)
        by_month: list[Money] = []
        for index, (start, end) in enumerate(zip(months, ends, strict=True)):
            extra = sum(
                (
                    item.amount
                    for item in goal.allocations
                    if max(start, day_before + timedelta(days=1)) <= item.allocated_on <= end
                ),
                Money(0),
            )
            current = min(target, current + extra)
            gap = target - current
            if gap > 0 and end >= goal.start_date:
                if start > goal.target_date:
                    current = target
                else:
                    through = [
                        amount
                        for when, amount in zip(months[index:], income[index:], strict=True)
                        if when <= goal.target_date
                    ]
                    remaining = sum(through, Money(0))
                    if remaining > 0 and income[index] > 0:
                        current = current + gap * (income[index] / remaining)
                    elif remaining <= 0:
                        opened = max(start, goal.start_date) - timedelta(days=1)
                        days = (goal.target_date - opened).days
                        share = Fraction(min((end - opened).days, days), days) if days > 0 else 1
                        current = current + gap * share
            current = min(target, current).quantize(fraction)
            by_month.append(current)
            set_aside[index] = set_aside[index] + current
            if cash:
                held[index] = held[index] + current
        earmarks.append((goal, overridden, cash, by_month))
    rows = [GoalMonth(amount, hold) for amount, hold in zip(set_aside, held, strict=True)]
    milestones: list[GoalMilestone] = []
    for goal, overridden, cash, by_month in earmarks:
        target_index = next(
            (i for i, (start, end) in enumerate(zip(months, ends, strict=True))
             if start <= goal.target_date <= end),
            None,
        )  # fmt: skip
        milestones.append(
            GoalMilestone(
                goal,
                goal.target_amount,
                goal.target_date,
                overridden,
                cash,
                target_index,
                by_month[target_index] if target_index is not None else None,
                cash_close[target_index] if target_index is not None else None,
                held[target_index] if target_index is not None else None,
            )
        )
    return rows, milestones
