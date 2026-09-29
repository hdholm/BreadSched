"""Spendable-cash flows of scheduled occurrences, shared by the Dashboard and goals.

Both the Dashboard's bill reserves and savings-goal earmarks are funded from
income as it is received, so they must agree on what an income occurrence is and
when it counts as received.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta

from ..db.sqlite import DbSQLite
from ..lib.money import Money
from ..lib.scheduled import ScheduledTransaction
from . import schedule

__all__ = ["flow_amounts", "income_occurrences", "income_schedules"]


def flow_amounts(db: DbSQLite, sched: ScheduledTransaction, when: date) -> tuple[Money, Money]:
    """Return the occurrence's direct spendable-cash inflow and outflow.

    Economic expense belongs in Plan. Dashboard liquidity changes only when a
    Bank/Cash leg changes, so a purchase charged to a card is not counted once at
    purchase and again when the account payment becomes due.
    """
    net_cash = Money(0)
    legs = list(sched.resolved_splits(when=when))
    for handle, amount in legs:
        account = db.get_account(handle)
        if account is None or not account.is_spendable_cash:
            continue
        net_cash = net_cash + amount
    if net_cash > 0:
        return net_cash, Money(0)
    if net_cash < 0:
        return Money(0), -net_cash
    return Money(0), Money(0)


def _next_unskipped(sched: ScheduledTransaction, after: date) -> date | None:
    candidate = sched.recurrence.next_after(after)
    while candidate is not None and candidate in sched.skipped:
        candidate = sched.recurrence.next_after(candidate)
    return candidate


def income_schedules(db: DbSQLite, today: date) -> list[ScheduledTransaction]:
    """Enabled, confirmed (non-estimate) schedules whose occurrences bring in cash."""
    found: list[ScheduledTransaction] = []
    for sched in db.iter_scheduled():
        if not sched.enabled or not sched.usable or sched.placeholder:
            continue
        representative = _next_unskipped(sched, today) or sched.recurrence.last_occurrence()
        if representative is None:
            continue
        income, outflow = flow_amounts(db, sched, representative)
        if income > 0 and income >= outflow:
            found.append(sched)
    return found


def income_occurrences(
    db: DbSQLite,
    schedules: list[ScheduledTransaction],
    start: date,
    through: date,
    today: date,
) -> Iterator[tuple[date, Money]]:
    """Income occurrences dated after ``start`` through ``through`` that count.

    Future scheduled income counts as expected. Past income counts only when its
    schedule says it was handled or a corresponding ledger transaction exists; a
    missed pay event cannot fund anything, because the cash never arrived.
    """
    for sched in schedules:
        for when in sched.recurrence.occurrences(through, since=start + timedelta(days=1)):
            if when in sched.skipped:
                continue
            income, outflow = flow_amounts(db, sched, when)
            if income <= 0 or income < outflow:
                continue
            happened = when > today
            if not happened and sched.last_posted is not None and when <= sched.last_posted:
                happened = True
            if not happened:
                happened = schedule.already_posted(db, sched.handle, when)
            if happened:
                yield when, income
