"""Building budgets, and comparing them against reality period by period.

Two ideas here that a strictly-monthly budget cannot express.

**A budget is generated from what is already scheduled.** Rent, salary and the
insurance premium are already described by recurrence rules; retyping them into a
budget grid is duplicated work that immediately drifts out of step. ``from_schedules``
walks the schedules over the budget's span and drops each occurrence into the period
it actually falls in.

**Periodicity is preserved, not averaged.** A fortnightly salary pays three times in
some months and twice in others; quarterly insurance hits four months a year and
nothing in the other eight. Spreading either into equal twelfths produces a budget
that is wrong in every single month while looking right for the year. The generator
places each occurrence on its date, so a period's budget is the sum of what is
actually expected to happen in it.

**Placeholder schedules** cover the rest: income and expenses with no real
scheduled transaction behind them, described by the same recurrence machinery so
they participate in exactly the same way. They are marked ``placeholder`` and are
never posted to the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..lib.account import AccountClass
from ..lib.budget import Budget, PeriodKind
from ..lib.money import Money
from ..lib.recurrence import add_months
from ..lib.scheduled import ScheduledTransaction
from . import schedule as schedule_engine

__all__ = [
    "CURRENT_BUDGET_KEY",
    "current_budget",
    "set_current_budget",
    "clone_budget",
    "members_of",
    "from_schedules",
    "period_bounds",
    "coverage",
    "ScheduleCoverage",
    "suggest_from_history",
]


#: Book metadata key naming the budget the dashboard and new projections use.
CURRENT_BUDGET_KEY = "current_budget"


def current_budget(db: DbSQLite) -> Budget | None:
    """The budget in force, or the only one, or None.

    Falling back to "the only one" matters: a household with a single budget
    should never have to nominate it before anything works.
    """
    handle = db.get_metadata(CURRENT_BUDGET_KEY)
    if handle:
        budget = db.get_budget(handle)
        if budget is not None:
            return budget
    budgets = list(db.iter_budgets())
    return budgets[0] if len(budgets) == 1 else (budgets[0] if budgets else None)


def set_current_budget(db: DbSQLite, budget: Budget | str | None) -> None:
    handle = budget.handle if isinstance(budget, Budget) else budget
    db.set_metadata(CURRENT_BUDGET_KEY, handle)


def members_of(db: DbSQLite, budget_handle: str | None) -> list:
    """Schedules that count towards a budget."""
    return [
        sched for sched in db.iter_scheduled() if sched.enabled and sched.in_budget(budget_handle)
    ]


def clone_budget(db: DbSQLite, budget: Budget, name: str, with_scenario: bool = True):
    """Copy a budget, its schedule memberships, and optionally its projection.

    Every schedule that belonged to the original is given membership of the copy
    as well, or the new budget would start empty and look like the original had
    been misread.
    """
    from ..lib.scenario import Scenario

    copy = budget.clone(name)
    scenario = None
    if with_scenario and budget.scenario:
        original = db.get_scenario(budget.scenario)
        if original is not None:
            scenario = Scenario(
                name=f"{name}",
                description=original.description,
                start=original.start,
                years=original.years,
                basis=original.basis,
                assumptions=original.assumptions,
            )
            scenario.budget = copy.handle
            copy.scenario = scenario.handle

    handles = [b.handle for b in db.iter_budgets()]
    with db.transaction(f"Clone budget as {name}") as txn:
        db.add_budget(copy, txn)
        if scenario is not None:
            db.add_scenario(scenario, txn)
        for sched in db.iter_scheduled():
            if sched.in_budget(budget.handle):
                sched.add_to_budget(copy.handle, handles + [copy.handle])
                db.commit_scheduled(sched, txn)
    return copy


def period_bounds(budget: Budget, index: int) -> tuple[date, date]:
    """Inclusive start and end dates of one budget period."""
    return budget.period_start(index), budget.period_end(index)


def from_schedules(
    db: DbSQLite,
    name: str,
    start: date,
    periods: int = 12,
    kind: PeriodKind | str = PeriodKind.MONTH,
    include_placeholders: bool = True,
    include_disabled: bool = False,
    flows_only: bool = True,
    budget_handle: str | None = None,
) -> Budget:
    """Build a budget by dropping every scheduled occurrence into its period.

    The result is a real :class:`Budget` the user can then edit; nothing here is
    magic or hidden, and regenerating it later simply produces the figures again.

    ``flows_only`` keeps the budget to income and expense accounts, which is what
    a budget normally means. Without it, a rent payment produces both an expense
    line and a mirror-image negative line on the bank account it was paid from, and
    the two look like separate commitments. Turn it off to budget transfers and
    debt payments as well, giving a full cash plan.
    """
    budget = Budget(name=name, start=start, periods=periods, kind=kind)

    for index in range(periods):
        begin, finish = period_bounds(budget, index)
        for occurrence in schedule_engine.forecast_occurrences(
            db, begin, finish, include_disabled=include_disabled
        ):
            sched = occurrence.schedule
            if sched.placeholder and not include_placeholders:
                continue
            # A budget only counts the flows that belong to it, so two budgets
            # can genuinely differ rather than being copies of one another.
            if not sched.in_budget(budget_handle):
                continue
            # A schedule with disagreeing calculated legs still tells us what it
            # intends to spend; refusing to budget at all would help nobody.
            txn = sched.instantiate(occurrence.when, strict=False)
            for split in txn.splits:
                account = db.get_account(split.account)
                if account is None or account.is_root:
                    continue
                if flows_only and not account.atype.is_flow:
                    continue
                # Budget lines are natural-sign: income and expenses both positive.
                amount = split.value * account.sign()
                if not amount:
                    continue
                line = budget.line(split.account)
                assert line is not None
                line.set_amount(index, line.amount(index) + amount)
                line.note = line.note or f"from schedule: {sched.name}"

    return budget


@dataclass(slots=True)
class ScheduleCoverage:
    """How much of a budget line is explained by scheduled transactions."""

    account: str
    name: str
    budgeted: Money
    scheduled: Money

    @property
    def unexplained(self) -> Money:
        return self.budgeted - self.scheduled

    @property
    def fully_explained(self) -> bool:
        return not self.unexplained


def coverage(db: DbSQLite, budget: Budget) -> list[ScheduleCoverage]:
    """Compare each budget line against what the schedules alone would produce.

    Answers "which of my budgeted amounts are actually pinned to something", which
    is the question that tells a user where their plan is a guess.
    """
    generated = from_schedules(db, "coverage", budget.start, budget.periods, budget.kind)
    rows: list[ScheduleCoverage] = []
    for handle, line in budget.lines.items():
        account = db.get_account(handle)
        rows.append(
            ScheduleCoverage(
                account=handle,
                name=db.full_name(account) if account else handle,
                budgeted=line.total(),
                scheduled=generated.lines[handle].total()
                if handle in generated.lines
                else Money(0),
            )
        )
    return sorted(rows, key=lambda row: row.name)


def suggest_from_history(
    db: DbSQLite,
    name: str,
    start: date,
    periods: int = 12,
    kind: PeriodKind | str = PeriodKind.MONTH,
    months_of_history: int = 12,
) -> Budget:
    """Seed a budget from what actually happened over the preceding months.

    Uses the mean of the months in which an account saw any activity, not the mean
    of all months: an account touched twice a year should budget its usual amount
    in those two periods rather than a sixth of it every month. Where the history
    shows a consistent month-of-year pattern it is preserved.
    """
    from . import ledger

    budget = Budget(name=name, start=start, periods=periods, kind=kind)
    history_start = add_months(start, -months_of_history, day=start.day)

    for account in db.iter_accounts():
        if account.is_root or account.placeholder or not account.atype.is_flow:
            continue
        monthly: list[Money] = []
        for offset in range(months_of_history):
            begin = add_months(history_start, offset, day=start.day)
            finish = date.fromordinal(
                add_months(history_start, offset + 1, day=start.day).toordinal() - 1
            )
            monthly.append(ledger.balance(db, account.handle, since=begin, as_of=finish))

        active = [amount for amount in monthly if amount]
        if not active:
            continue
        total = Money(0)
        for amount in active:
            total = total + amount
        typical = (total / len(active)).quantize(100)
        frequency = len(active) / months_of_history

        line = budget.line(account.handle)
        assert line is not None
        for index in range(periods):
            if frequency >= 0.75:
                line.set_amount(index, typical)
            else:
                # Irregular: place it in the same months it happened before.
                month = budget.period_start(index).month
                seen = any(
                    monthly[offset]
                    and add_months(history_start, offset, day=start.day).month == month
                    for offset in range(months_of_history)
                )
                line.set_amount(index, typical if seen else Money(0))
        line.note = f"from {len(active)} month(s) of history"

    return budget


def placeholder_schedule(
    name: str,
    account: str,
    counter_account: str,
    amount: Money | str,
    recurrence,
    account_class: AccountClass | None = None,
) -> ScheduledTransaction:
    """A recurring budget item with no real transaction behind it.

    Same recurrence machinery as a real schedule, so a quarterly placeholder lands
    in four periods rather than being smeared across twelve, but flagged so nothing
    ever posts it to the ledger.
    """
    from ..lib.scheduled import ScheduledSplit

    value = amount if isinstance(amount, Money) else Money(amount)
    sched = ScheduledTransaction(name=name, recurrence=recurrence)
    sched.placeholder = True
    sched.auto_create = False
    sched.splits = [
        ScheduledSplit(account, value),
        ScheduledSplit(counter_account, -value),
    ]
    return sched
