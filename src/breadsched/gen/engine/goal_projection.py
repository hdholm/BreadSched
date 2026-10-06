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
goal spends nothing: after its target month the whole target stays set aside,
unless the scenario models its purchase (``GoalOverride.purchase_on``). That
purchase is a planned one-off moving the target out of the goal's account
(``planning.goal_purchase_events``), so from the purchase month the goal sets
nothing aside.

Money set aside for a goal held in a cash account is held from projected cash,
so ``cash_after_goals`` shows what a scenario leaves spendable. A goal held in a
non-cash asset account, such as a brokerage account, is compared at its target
month with that account's projected closing balance against everything goals in
that account have set aside then. An account the projection leaves out has no
projected balance, so its goals are reported without a comparison.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    #: The goal's account, by full name.
    account_name: str = ""
    #: The purchase this scenario models, if any, and the account it is bought into.
    purchase_on: date | None = None
    purchase_account_name: str = ""
    #: For a non-cash account: its projected closing balance in the target month,
    #: and what every goal held in it has set aside then. ``None`` for a cash
    #: account, outside the horizon, or for an account the projection leaves out.
    account_close: Money | None = None
    account_held: Money | None = None

    @property
    def covered(self) -> bool | None:
        """Whether the money holding the goal covers every earmark on it that month.

        Cash goals compare projected cash with everything held from cash; a non-cash
        goal compares its account's projected balance with that account's earmarks.
        """
        if self.cash_account:
            if self.cash_close is None or self.goals_held is None:
                return None
            return self.cash_close >= self.goals_held
        if self.account_close is None or self.account_held is None:
            return None
        return self.account_close >= self.account_held

    def as_dict(self) -> dict[str, object]:
        return {
            "goal": self.goal.handle,
            "name": self.goal.name,
            "target": self.target,
            "target_date": self.target_date,
            "overridden": self.overridden,
            "cash_account": self.cash_account,
            "account": self.goal.account,
            "account_name": self.account_name,
            "month_index": self.month_index,
            "set_aside": self.set_aside,
            "cash_close": self.cash_close,
            "goals_held": self.goals_held,
            "account_close": self.account_close,
            "account_held": self.account_held,
            "covered": self.covered,
            "purchase_on": self.purchase_on,
            "purchase_account_name": self.purchase_account_name,
        }


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
    account_close: Sequence[Mapping[str, Money]] | None = None,
) -> tuple[list[GoalMonth], list[GoalMilestone]]:
    """Month-end earmarks for every row, and each goal's target-date milestone.

    ``account_close`` gives each row's projected closing balance by non-cash asset
    account; goals held in those accounts are compared with it.
    """
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
        override = scenario.goal_overrides.get(goal.handle)
        bought = override.purchase_on if override is not None and override.purchases else None
        by_month: list[Money] = []
        for index, (start, end) in enumerate(zip(months, ends, strict=True)):
            if bought is not None and bought <= end:
                # Spent: the purchase one-off has taken the money out of the account.
                by_month.append(Money(0))
                continue
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
    # What goals in each non-cash account have set aside, month by month.
    by_account: dict[str, list[Money]] = {}
    for goal, _overridden, cash, by_month in earmarks:
        if not cash:
            totals = by_account.setdefault(goal.account, [Money(0)] * len(months))
            for index, amount in enumerate(by_month):
                totals[index] = totals[index] + amount
    milestones: list[GoalMilestone] = []
    for goal, overridden, cash, by_month in earmarks:
        target_index = next(
            (i for i, (start, end) in enumerate(zip(months, ends, strict=True))
             if start <= goal.target_date <= end),
            None,
        )  # fmt: skip
        override = scenario.goal_overrides.get(goal.handle)
        purchase = override if override is not None and override.purchases else None
        balance = held_there = None
        if not cash and target_index is not None and account_close is not None:
            balance = account_close[target_index].get(goal.account)
            if balance is not None:
                held_there = by_account[goal.account][target_index]
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
                account_name=db.full_name(goal.account) or "",
                purchase_on=purchase.purchase_on if purchase is not None else None,
                purchase_account_name=(
                    db.full_name(purchase.purchase_account) or ""
                    if purchase is not None and purchase.purchase_account
                    else ""
                ),
                account_close=balance,
                account_held=held_there,
            )
        )
    return rows, milestones
