"""Scheduled bills on the Dashboard: their cycles, reserves, and what is pending.

A bill's amount means nothing without its cycle, so every scheduled outflow is
expressed per month and per year from its own recurrence (``cycle_days``,
``BillRow``). Its reserve accrues on the household's scheduled income dates in
proportion to the income in that bill cycle, and an overdue bill stays a current
obligation (``MissedGroup`` gathers missed occurrences of one schedule). These rows
and the pending cash flow behind liquidity are built here; ``dashboard`` assembles
them with the account groups into the view.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from ..db.sqlite import DbSQLite
from ..lib.account import AccountClass
from ..lib.money import Money
from ..lib.recurrence import PeriodType, Recurrence
from ..lib.scheduled import ScheduledTransaction
from . import schedule
from .cash_flow import flow_amounts as _flow_amounts
from .cash_flow import income_occurrences
from .escrow import recognition as escrow_recognition

__all__ = [
    "BillRow",
    "DAYS_PER_MONTH",
    "DAYS_PER_YEAR",
    "MissedGroup",
    "cycle_days",
    "group_missed",
    "pending_cash_flow",
    "pending_from_ledger",
]

#: Average length of a month. Used to convert any cycle to a monthly equivalent;
#: 30 would drift by five days a year and make annual bills disagree with monthly
#: ones.
DAYS_PER_MONTH = Decimal("30.436875")
DAYS_PER_YEAR = Decimal("365.2425")

#: Cycle length in days for each recurrence period, before its interval.
_PERIOD_DAYS = {
    PeriodType.DAY: Decimal(1),
    PeriodType.WEEK: Decimal(7),
    PeriodType.SEMI_MONTH: DAYS_PER_MONTH / 2,
    PeriodType.MONTH: DAYS_PER_MONTH,
    PeriodType.NTH_WEEKDAY: DAYS_PER_MONTH,
    PeriodType.LAST_WEEKDAY: DAYS_PER_MONTH,
    PeriodType.YEAR: DAYS_PER_YEAR,
}


def cycle_days(sched: ScheduledTransaction) -> Decimal:
    """How many days one cycle of this schedule lasts.

    A one-off has no cycle; treated as a year so it does not divide by zero and
    does not dominate a monthly average.
    """
    recurrence = sched.recurrence
    if recurrence.period is PeriodType.ONCE:
        return DAYS_PER_YEAR
    base = _PERIOD_DAYS.get(recurrence.period, DAYS_PER_MONTH)
    return base * Decimal(max(1, recurrence.interval))


@dataclass
class BillRow:
    """One pending dated cash flow, normalised for long-range comparisons."""

    name: str
    next_due: date
    amount: Money
    cycle_days: Decimal
    schedule: ScheduledTransaction | None = None
    estimate: bool = False
    income: bool = False
    held: Money = field(default_factory=lambda: Money(0))
    reserve_for: date | None = None
    account: str | None = None
    recurrence: Recurrence | None = None
    generated: bool = False
    emergency_amount: Money = field(default_factory=lambda: Money(0))
    fraction: int = 100

    @property
    def frequency(self) -> str:
        """The schedule's own words for how often it recurs.

        "every 3 months" is what the user set; 3.0000 is an implementation
        detail of the monthly conversion and reads as though it were the truth.
        """
        if self.recurrence is not None:
            return self.recurrence.describe()
        months = self.cycle_months
        return f"every {months:g} month(s)"

    @property
    def cycle_months(self) -> Decimal:
        return (self.cycle_days / DAYS_PER_MONTH).quantize(Decimal("0.0001"))

    @property
    def monthly(self) -> Money:
        return (self.amount * (DAYS_PER_MONTH / self.cycle_days)).quantize(self.fraction)

    @property
    def annual(self) -> Money:
        return (self.amount * (DAYS_PER_YEAR / self.cycle_days)).quantize(self.fraction)

    @property
    def emergency_monthly(self) -> Money:
        return (self.emergency_amount * (DAYS_PER_MONTH / self.cycle_days)).quantize(self.fraction)

    def days_until(self, today: date) -> int:
        return (self.next_due - today).days

    def hold(self, today: date) -> Money | None:
        """Funds reserved by income already received; income rows have no hold."""
        del today  # The builder calculates the dated reserve once for its as-of date.
        return None if self.income else self.held

    def due_within(self, today: date, days: int) -> bool:
        return self.days_until(today) <= days


@dataclass(frozen=True, slots=True)
class MissedGroup:
    """Overdue occurrences of one schedule, shown as a single Dashboard row.

    Six missed monthly rent payments are one question for the household ("what
    happened to rent since April?"), not six identical rows whose monthly and
    annual figures would each repeat the same schedule. Totals still come from
    the individual occurrences, which remain available as ``occurrences``.
    """

    rows: tuple[BillRow, ...]

    @property
    def first(self) -> BillRow:
        return self.rows[0]

    @property
    def name(self) -> str:
        return self.first.name

    @property
    def schedule(self) -> ScheduledTransaction | None:
        return self.first.schedule

    @property
    def income(self) -> bool:
        return self.first.income

    @property
    def generated(self) -> bool:
        return self.first.generated

    @property
    def account(self) -> str | None:
        return self.first.account

    @property
    def estimate(self) -> bool:
        return self.first.estimate

    @property
    def frequency(self) -> str:
        return self.first.frequency

    @property
    def cycle_months(self) -> Decimal:
        return self.first.cycle_months

    @property
    def next_due(self) -> date:
        """The oldest missed date, which is what makes the group overdue."""
        return self.first.next_due

    @property
    def last_due(self) -> date:
        return self.rows[-1].next_due

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def occurrences(self) -> tuple[tuple[date, Money], ...]:
        return tuple((row.next_due, row.amount) for row in self.rows)

    @property
    def amount(self) -> Money:
        return sum((row.amount for row in self.rows), Money(0))

    @property
    def held(self) -> Money:
        return sum((row.held for row in self.rows), Money(0))

    @property
    def monthly(self) -> Money:
        """The schedule's normalised monthly figure, counted once."""
        return self.first.monthly

    @property
    def annual(self) -> Money:
        return self.first.annual

    def days_until(self, today: date) -> int:
        return self.first.days_until(today)

    def missed_label(self, today: date) -> str:
        return (
            f"{self.count} missed, {self.next_due.isoformat()} to "
            f"{self.last_due.isoformat()} ({-self.days_until(today)} days overdue)"
        )


def group_missed(rows: list[BillRow], today: date) -> list[BillRow | MissedGroup]:
    """Collapse two or more overdue rows of the same schedule into one group."""
    keyed: dict[tuple[str, str], list[BillRow]] = {}
    for row in rows:
        if row.next_due < today:
            key = ("schedule", row.schedule.handle) if row.schedule else ("name", row.name)
            keyed.setdefault(key, []).append(row)
    grouped: list[BillRow | MissedGroup] = []
    emitted: set[tuple[str, str]] = set()
    for row in rows:
        if row.next_due >= today:
            grouped.append(row)
            continue
        key = ("schedule", row.schedule.handle) if row.schedule else ("name", row.name)
        members = keyed[key]
        if len(members) == 1:
            grouped.append(row)
        elif key not in emitted:
            emitted.add(key)
            grouped.append(MissedGroup(tuple(sorted(members, key=lambda item: item.next_due))))
    return grouped


def _emergency_outflow(db: DbSQLite, sched: ScheduledTransaction, when: date) -> Money:
    """Return the non-duplicated portion retained when income stops.

    The choice belongs to the economically meaningful positive leg. Cash/bank
    funding legs are credits and never counted. Escrow draws reduce a previously
    funded restricted asset, so their covered expense legs are suppressed.
    """
    legs = list(sched.resolved_splits(when=when))
    accounts = {
        handle: account
        for handle, _amount in legs
        if (account := db.get_account(handle)) is not None
    }
    escrow = escrow_recognition(legs, accounts)
    positive: dict[str, Money] = {}
    for handle, amount in legs:
        if amount > 0:
            positive[handle] = positive.get(handle, Money(0)) + amount

    total = Money(0)
    for handle, amount in positive.items():
        account = accounts.get(handle)
        if account is None or not account.emergency_fund_included:
            continue
        if account.account_class is AccountClass.EXPENSE:
            amount = amount - escrow.covered_expenses.get(handle, Money(0))
        if amount > 0:
            total = total + amount
    return total


def _next_unskipped(sched: ScheduledTransaction, after: date) -> date | None:
    """First occurrence after a date, respecting per-date skips."""
    candidate = sched.recurrence.next_after(after)
    while candidate is not None and candidate in sched.skipped:
        candidate = sched.recurrence.next_after(candidate)
    return candidate


def _cycle_start(recurrence: Recurrence, due: date, cycle_length: Decimal) -> date:
    """Previous firing for a bill cycle, or an inferred first-cycle boundary."""
    lookback = timedelta(days=int(cycle_length) + 15)
    previous = recurrence.occurrences(due - timedelta(days=1), since=due - lookback)
    return previous[-1] if previous else due - timedelta(days=max(1, int(cycle_length)))


def _income_events_for_cycle(
    db: DbSQLite,
    schedules: list[ScheduledTransaction],
    start: date,
    through: date,
    today: date,
) -> tuple[Money, Money]:
    """Return total cycle income and the share already received by ``today``."""
    total = Money(0)
    received = Money(0)
    for when, income in income_occurrences(db, schedules, start, through, today):
        total = total + income
        if when <= today:
            received = received + income
    return total, received


def _reserve_bill(
    db: DbSQLite,
    bill: BillRow,
    income_schedules: list[ScheduledTransaction],
    today: date,
) -> None:
    """Attach the exact income-triggered reserve for one bill row."""
    if bill.generated:
        bill.held = bill.amount
        bill.reserve_for = bill.next_due
        return
    recurrence = bill.recurrence
    if recurrence is None:
        bill.held = bill.amount
        bill.reserve_for = bill.next_due
        return

    target_due = bill.next_due
    target_amount = bill.amount
    if target_due < today:
        if bill.schedule is None:
            bill.held = bill.amount
            bill.reserve_for = bill.next_due
            return
        next_due = _next_unskipped(bill.schedule, today)
        if next_due is None:
            return
        _income, next_amount = _flow_amounts(db, bill.schedule, next_due)
        if next_amount <= 0:
            return
        target_due = next_due
        target_amount = next_amount

    start = _cycle_start(recurrence, target_due, bill.cycle_days)
    total_income, received_income = _income_events_for_cycle(
        db, income_schedules, start, target_due, today
    )
    bill.reserve_for = target_due
    if total_income <= 0:
        # With no identified income before the due date, existing cash is the
        # only known funding source; protect the complete obligation.
        bill.held = target_amount
        return
    bill.held = (target_amount * (received_income / total_income)).quantize(bill.fraction)


def _credit_card_rows(
    db: DbSQLite,
    today: date,
    schedules: list[ScheduledTransaction],
    fraction: int,
) -> list[BillRow]:
    """Account-tied card payments shared with Scheduled and Upcoming."""
    rows: list[BillRow] = []
    for definition in schedule.account_payment_definitions(db, today, schedules):
        if definition.amount_due <= 0:
            continue
        account = db.get_account(definition.account)
        rows.append(
            BillRow(
                name=definition.name,
                next_due=definition.next_due,
                amount=definition.amount_due,
                cycle_days=DAYS_PER_MONTH,
                account=definition.account,
                recurrence=definition.recurrence,
                generated=True,
                fraction=fraction,
                emergency_amount=(
                    definition.amount_due
                    if account is not None and account.emergency_fund_included
                    else Money(0)
                ),
            )
        )
    return rows


def pending_cash_flow(
    db: DbSQLite,
    today: date,
    horizon_days: int,
    paid_off: set[str],
    fraction: int,
) -> tuple[list[BillRow], list[BillRow], Money, Money, date | None, list[tuple[date, Money]]]:
    """Build dated pending income and bills, plus income used by liquidity."""
    horizon = today + timedelta(days=horizon_days)
    schedules = [sched for sched in db.iter_scheduled() if sched.enabled and sched.usable]
    due_by_schedule: dict[str, list[date]] = {}
    for occurrence in schedule.due_occurrences(db, as_of=today, horizon_days=0):
        due_by_schedule.setdefault(occurrence.schedule.handle, []).append(occurrence.when)

    pending: list[BillRow] = []
    estimates: list[BillRow] = []
    bills: list[BillRow] = []
    income_schedules: list[ScheduledTransaction] = []
    income_events: list[tuple[date, Money]] = []
    income_per_month = Money(0)
    income_per_month_with_estimates = Money(0)

    for sched in schedules:
        unresolved = due_by_schedule.get(sched.handle, [])
        next_due = _next_unskipped(sched, today)
        dates = unresolved or ([next_due] if next_due is not None else [])
        representative = (dates[-1] if dates else None) or sched.recurrence.last_occurrence()
        if representative is None:
            continue
        income, outflow = _flow_amounts(db, sched, representative)
        if income > 0 and income >= outflow and not sched.placeholder:
            income_schedules.append(sched)
        if not dates:
            continue

        days = cycle_days(sched)
        if income > 0 and income >= outflow:
            normalised_income = (income * (DAYS_PER_MONTH / days)).quantize(fraction)
            income_per_month_with_estimates = income_per_month_with_estimates + normalised_income
            if not sched.placeholder:
                income_per_month = income_per_month + normalised_income
            for when in dates:
                event_income, event_outflow = _flow_amounts(db, sched, when)
                if event_income <= 0 or event_income < event_outflow:
                    continue
                row = BillRow(
                    name=sched.name,
                    next_due=when,
                    amount=event_income,
                    cycle_days=days,
                    schedule=sched,
                    estimate=sched.placeholder,
                    income=True,
                    recurrence=sched.recurrence,
                    fraction=fraction,
                )
                (estimates if sched.placeholder else pending).append(row)
            for occurrence_date in sched.recurrence.occurrences(horizon, since=today):
                if sched.placeholder:
                    continue
                if occurrence_date in sched.skipped:
                    continue
                if occurrence_date <= today and schedule.already_posted(
                    db, sched.handle, occurrence_date
                ):
                    continue
                event_income, event_outflow = _flow_amounts(db, sched, occurrence_date)
                if event_income > 0 and event_income >= event_outflow:
                    income_events.append((occurrence_date, event_income))
            continue

        for when in dates:
            _income, event_outflow = _flow_amounts(db, sched, when)
            if event_outflow <= 0:
                continue
            legs = list(sched.resolved_splits(when=when))
            if any(
                handle in paid_off
                and amount > 0
                and (account := db.get_account(handle)) is not None
                and account.account_class is AccountClass.LIABILITY
                for handle, amount in legs
            ):
                continue
            bill = BillRow(
                name=sched.name,
                next_due=when,
                amount=event_outflow,
                cycle_days=days,
                schedule=sched,
                estimate=sched.placeholder,
                recurrence=sched.recurrence,
                emergency_amount=_emergency_outflow(db, sched, when),
                fraction=fraction,
            )
            bills.append(bill)
            (estimates if sched.placeholder else pending).append(bill)

    card_rows = _credit_card_rows(db, today, schedules, fraction)
    bills.extend(card_rows)
    pending.extend(card_rows)
    latest_overdue = {
        bill.schedule.handle: bill
        for bill in bills
        if bill.next_due < today and bill.schedule is not None
    }
    for bill in bills:
        if (
            bill.next_due < today
            and bill.schedule is not None
            and latest_overdue[bill.schedule.handle] is not bill
        ):
            continue
        _reserve_bill(db, bill, income_schedules, today)

    pending.sort(key=lambda row: (row.next_due, not row.income, row.name.casefold()))
    income_events.sort(key=lambda item: item[0])
    next_income = next((when for when, _amount in income_events if when >= today), None)
    return (
        pending,
        estimates,
        income_per_month,
        income_per_month_with_estimates,
        next_income,
        income_events,
    )


def pending_from_ledger(db: DbSQLite, today: date | None = None) -> list:
    """Occurrences already due but not yet posted, for the pending list."""
    return schedule.due_occurrences(db, as_of=today or date.today(), horizon_days=0)
