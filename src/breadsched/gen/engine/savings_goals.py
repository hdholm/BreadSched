"""How much of each savings goal is set aside, and what that leaves spendable.

A goal is funded like a pending bill. Each income received from the goal's start
date sets aside a prorated share of what the goal still needs: the share is that
income over all income expected from then through the target date, so the whole
target is set aside by the target date. Extra money allocated to the goal is set
aside in full on its date, and the income after it spreads only the remaining gap.
Income that was scheduled but never arrived sets nothing aside, exactly as for
bills. With no income expected before the target date, time is the basis instead:
the gap is spread evenly by day.

Once the target date has passed, the whole target stays set aside (a milestone
spends nothing) until the goal is closed, which releases the earmark.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction

from ..db.sqlite import DbSQLite
from ..lib.money import Money
from ..lib.savings_goal import SavingsGoal
from ..lib.scheduled import ScheduledTransaction
from . import ledger
from .cash_flow import income_occurrences, income_schedules
from .chart_model import ChartModel
from .currency import reporting_fraction

__all__ = ["GoalProgress", "goal_progress", "goals_chart", "goals_progress", "spendable_hold"]


@dataclass(frozen=True, slots=True)
class GoalProgress:
    """One goal's earmark on a date.

    ``status`` is ``not started`` (before the start date), ``saving``, ``reached``
    (the whole target is set aside), or ``closed``. ``basis`` names how income
    was prorated: ``income`` or ``time``.
    """

    goal: SavingsGoal
    as_of: date
    account_name: str
    target: Money
    allocated: Money
    from_income: Money
    set_aside: Money
    remaining: Money
    status: str
    basis: str
    liquid_account: bool
    account_balance: Money


def _segment_share(
    db: DbSQLite,
    schedules: list[ScheduledTransaction],
    after: date,
    through: date,
    target: date,
    as_of: date,
) -> tuple[Fraction, str]:
    """Share of the gap funded in (after, through]: income received there over
    income expected in (after, target], or elapsed days when none is expected."""
    expected = Money(0)
    received = Money(0)
    for when, income in income_occurrences(db, schedules, after, target, as_of):
        expected = expected + income
        if when <= through and when <= as_of:
            received = received + income
    if expected > 0:
        return received / expected, "income"
    return Fraction((through - after).days, (target - after).days), "time"


def goal_progress(
    db: DbSQLite,
    goal: SavingsGoal,
    as_of: date,
    *,
    schedules: list[ScheduledTransaction] | None = None,
) -> GoalProgress:
    """Compute what ``goal`` has set aside on ``as_of``."""
    account = db.get_account(goal.account)
    account_name = db.full_name(goal.account) if account is not None else goal.account
    liquid = account is not None and account.atype.is_cash_like and not account.placeholder
    balance = ledger.balance(db, goal.account, as_of=as_of) if account is not None else Money(0)
    target = goal.target_amount
    fraction = reporting_fraction(db)

    def result(allocated: Money, from_income: Money, status: str, basis: str) -> GoalProgress:
        set_aside = min(target, allocated + from_income).quantize(fraction)
        from_income = (set_aside - min(allocated, set_aside)).quantize(fraction)
        return GoalProgress(
            goal,
            as_of,
            account_name,
            target,
            min(allocated, set_aside),
            from_income,
            set_aside,
            target - set_aside,
            "reached" if status == "saving" and set_aside >= target else status,
            basis,
            liquid,
            balance,
        )

    if not goal.is_open(as_of):
        return GoalProgress(
            goal, as_of, account_name, target, Money(0), Money(0), Money(0), Money(0),
            "closed", "", liquid, balance,
        )  # fmt: skip
    allocated = goal.allocated(as_of)
    if as_of < goal.start_date:
        return result(allocated, Money(0), "not started", "")
    if as_of >= goal.target_date:
        # A milestone keeps its whole target set aside once the date has passed.
        return result(allocated, target - min(allocated, target), "saving", "")
    schedules = income_schedules(db, as_of) if schedules is None else schedules
    set_aside = sum(
        (item.amount for item in goal.allocations if item.allocated_on < goal.start_date),
        Money(0),
    )
    cursor = goal.start_date - timedelta(days=1)
    basis = "income"
    boundaries: Iterable[tuple[date, Money]] = (
        (item.allocated_on, item.amount)
        for item in goal.allocations
        if goal.start_date <= item.allocated_on <= as_of
    )
    for allocated_on, amount in [*boundaries, (as_of + timedelta(days=1), Money(0))]:
        # Income before the allocation's date spreads the gap as it stood then;
        # the allocation is set aside before that day's income.
        through = allocated_on - timedelta(days=1)
        if through > cursor:
            gap = target - set_aside
            if gap > 0:
                share, basis = _segment_share(
                    db, schedules, cursor, through, goal.target_date, as_of
                )
                set_aside = set_aside + gap * share
            cursor = through
        set_aside = set_aside + amount
    return result(allocated, set_aside - allocated, "saving", basis)


def goals_progress(
    db: DbSQLite, as_of: date, goals: Iterable[SavingsGoal] | None = None
) -> list[GoalProgress]:
    """Every goal's progress, sharing one scan for income schedules."""
    schedules = income_schedules(db, as_of)
    chosen = db.iter_savings_goals() if goals is None else goals
    return [goal_progress(db, goal, as_of, schedules=schedules) for goal in chosen]


def spendable_hold(progress: Iterable[GoalProgress]) -> Money:
    """What goal earmarks take out of spendable cash.

    Money set aside in a cash account is part of the liquid balance, so all of it
    is held. Money for a goal kept in a non-cash account (a brokerage or other
    asset) is only held from spendable cash where that account's balance does not
    already cover it: the part still to be moved there.
    """
    held = Money(0)
    uncovered: dict[str, tuple[Money, Money]] = {}
    for item in progress:
        if item.set_aside <= 0:
            continue
        if item.liquid_account:
            held = held + item.set_aside
            continue
        total, balance = uncovered.get(item.goal.account, (Money(0), item.account_balance))
        uncovered[item.goal.account] = (total + item.set_aside, balance)
    for total, balance in uncovered.values():
        covered = max(Money(0), min(total, balance))
        held = held + (total - covered)
    return held


def goals_chart(progress: Iterable[GoalProgress], currency: str = "") -> ChartModel:
    """Each open goal's progress toward its target by its target date.

    One column per goal, labelled with its target month: what is set aside (slot 1)
    below what is still to save (slot 2). The two are the goal's own values and sum
    exactly to its target (the chart's ``totals``). Closed goals are left out.
    """
    from .chart_model import STACKED, ChartSeries

    shown = [item for item in progress if item.status != "closed"]
    for item in shown:
        if item.set_aside + item.remaining != item.target:
            raise AssertionError("a goal's set-aside and remaining do not make its target")
    return ChartModel(
        "goals",
        "Savings goals: set aside toward each target",
        STACKED,
        tuple(f"{item.goal.name} ({item.goal.target_date:%b %Y})" for item in shown),
        (
            ChartSeries("set_aside", "Set aside", tuple(item.set_aside for item in shown), 1),
            ChartSeries("remaining", "Still to save", tuple(item.remaining for item in shown), 2),
        ),
        currency,
        totals=tuple(item.target for item in shown),
    )
