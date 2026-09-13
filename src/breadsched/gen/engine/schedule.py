"""Turning schedules into ledger entries.

Posting is idempotent: an occurrence is identified by (schedule handle, date), and
``post_due`` skips any occurrence already present in the ledger.  That matters
because the natural trigger for posting is "whenever the book is opened", which on
a laptop that sleeps through a month boundary can easily happen twice in a minute.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..db.sqlite import DbSQLite
from ..lib.base import create_handle
from ..lib.money import Money
from ..lib.recurrence import PeriodType, Recurrence
from ..lib.scheduled import ScheduledSplit, ScheduledTransaction
from ..lib.transaction import Transaction

__all__ = [
    "Occurrence",
    "already_posted",
    "delete_definition",
    "due_occurrences",
    "duplicate_definition",
    "duplicate_saved_definition",
    "forecast_occurrences",
    "from_transaction",
    "post_due",
    "post_occurrences",
    "skip_occurrences",
]


@dataclass(slots=True)
class Occurrence:
    """A single firing of a schedule, real or hypothetical."""

    schedule: ScheduledTransaction
    when: date
    amount: Money

    @property
    def name(self) -> str:
        return self.schedule.name

    def instantiate(self) -> Transaction:
        return self.schedule.instantiate(self.when)


def duplicate_definition(source: ScheduledTransaction) -> ScheduledTransaction:
    """Copy a definition with independent identity and no completed-occurrence state."""
    duplicate = ScheduledTransaction.from_dict(source.serialize())
    duplicate.handle = create_handle()
    duplicate.gid = ""
    duplicate.change = 0
    duplicate.name = f"{source.name} copy"
    if duplicate.description == source.name:
        duplicate.description = duplicate.name
    duplicate.last_posted = None
    duplicate.skipped = []
    return duplicate


def duplicate_saved_definition(
    db: DbSQLite, handle: str, *, name: str | None = None
) -> ScheduledTransaction:
    """Persist an exact independent copy, including protected custom structure."""
    source = db.get_scheduled(handle)
    if source is None:
        raise KeyError(handle)
    duplicate = duplicate_definition(source)
    if name is not None:
        chosen = name.strip()
        if not chosen:
            raise ValueError("give the copied schedule a name")
        old_name = duplicate.name
        duplicate.name = chosen
        if duplicate.description == old_name:
            duplicate.description = chosen
    with db.transaction(f"Duplicate scheduled {source.name}") as txn:
        db.add_scheduled(duplicate, txn)
    return duplicate


def from_transaction(transaction: Transaction) -> ScheduledTransaction:
    """Build an unsaved one-time template from every financial leg of an actual."""
    return ScheduledTransaction(
        name=transaction.description or "Scheduled transaction",
        description=transaction.description,
        recurrence=Recurrence(PeriodType.ONCE, start=transaction.post_date),
        splits=[
            ScheduledSplit(
                split.account,
                split.value,
                memo=split.memo,
                planning_flow=split.planning_flow,
            )
            for split in transaction.splits
        ],
        currency=transaction.currency,
    )


def delete_definition(db: DbSQLite, handle: str) -> ScheduledTransaction:
    """Delete a baseline schedule without leaving live scenario references behind."""
    source = db.get_scheduled(handle)
    if source is None:
        raise KeyError(handle)
    scenarios = [
        scenario.name
        for scenario in db.iter_scenarios()
        if any(item.source_schedule == handle for item in scenario.schedule_overrides)
    ]
    if scenarios:
        names = ", ".join(sorted(scenarios))
        raise ValueError(
            f"remove this schedule's override from scenario(s) {names} before deleting it"
        )
    with db.transaction(f"Delete scheduled {source.name}") as txn:
        db.remove_scheduled(handle, txn)
    return source


def already_posted(db: DbSQLite, schedule_handle: str, when: date) -> bool:
    for txn in db.iter_transactions(start=when, end=when):
        if txn.scheduled_from == schedule_handle:
            return True
    return False


def due_occurrences(
    db: DbSQLite,
    as_of: date | None = None,
    horizon_days: int | None = None,
) -> list[Occurrence]:
    """Occurrences that should already have been posted, plus any within advance days.

    ``horizon_days`` overrides each schedule's own ``advance_days``, which is what
    the "show me the next 30 days" filter in the UI uses.
    """
    today = as_of or date.today()
    found: list[Occurrence] = []
    for sched in db.iter_scheduled():
        if not sched.usable:
            continue
        if not sched.enabled:
            continue
        advance = horizon_days if horizon_days is not None else sched.advance_days
        until = today + timedelta(days=advance)
        since = sched.last_posted + timedelta(days=1) if sched.last_posted else None
        for when in sched.upcoming(until, since=since):
            if already_posted(db, sched.handle, when):
                continue
            if when in sched.skipped:
                continue
            found.append(Occurrence(sched, when, sched.amount(when=when)))
    return sorted(found, key=lambda o: (o.when, o.name))


def post_due(
    db: DbSQLite,
    as_of: date | None = None,
    only_auto: bool = True,
    message: str = "Post scheduled transactions",
) -> list[Transaction]:
    """Write due occurrences into the ledger.  Returns what was actually posted."""
    today = as_of or date.today()
    posted: list[Transaction] = []
    candidates = [
        occ
        for occ in due_occurrences(db, as_of=today, horizon_days=0)
        # A placeholder describes a planning expectation, not a transaction that
        # will happen; posting one would invent a ledger entry.
        if occ.when <= today
        and not occ.schedule.placeholder
        and (occ.schedule.auto_create or not only_auto)
    ]
    if not candidates:
        return posted

    with db.transaction(message) as txn:
        touched: dict[str, ScheduledTransaction] = {}
        for occ in candidates:
            real = occ.instantiate()
            db.add_transaction(real, txn)
            posted.append(real)
            sched = touched.setdefault(occ.schedule.handle, occ.schedule)
            if sched.last_posted is None or occ.when > sched.last_posted:
                sched.last_posted = occ.when
        for sched in touched.values():
            db.commit_scheduled(sched, txn)
    return posted


def post_occurrences(
    db: DbSQLite,
    chosen: list[Occurrence],
    message: str = "Post scheduled transactions",
) -> list[Transaction]:
    """Post exactly the occurrences given, and nothing else.

    The user chose these one at a time, so the engine posts them one at a time
    rather than re-deriving what is due — which could differ by the time they
    answered.
    """
    posted: list[Transaction] = []
    if not chosen:
        return posted
    with db.transaction(message) as txn:
        touched: dict[str, ScheduledTransaction] = {}
        for occurrence in chosen:
            real = occurrence.instantiate()
            db.add_transaction(real, txn)
            posted.append(real)
            sched = touched.setdefault(occurrence.schedule.handle, occurrence.schedule)
            if sched.last_posted is None or occurrence.when > sched.last_posted:
                sched.last_posted = occurrence.when
        for sched in touched.values():
            db.commit_scheduled(sched, txn)
    return posted


def skip_occurrences(
    db: DbSQLite,
    chosen: list[Occurrence],
    message: str = "Skip scheduled transactions",
) -> int:
    """Mark occurrences as dealt with without posting them."""
    if not chosen:
        return 0
    with db.transaction(message) as txn:
        touched: dict[str, ScheduledTransaction] = {}
        for occurrence in chosen:
            sched = touched.setdefault(occurrence.schedule.handle, occurrence.schedule)
            sched.skip(occurrence.when)
        for sched in touched.values():
            db.commit_scheduled(sched, txn)
    return len(chosen)


def forecast_occurrences(
    db: DbSQLite,
    start: date,
    end: date,
    include_disabled: bool = False,
) -> list[Occurrence]:
    """Every firing in a window, ignoring whether it has been posted.

    This is the projection view of schedules: it answers "what will happen", where
    :func:`due_occurrences` answers "what still needs recording".
    """
    found: list[Occurrence] = []
    for sched in db.iter_scheduled():
        if not sched.usable:
            continue
        if not sched.enabled and not include_disabled:
            continue
        for when in sched.recurrence.occurrences(end, since=start):
            if when in sched.skipped:
                continue
            found.append(Occurrence(sched, when, sched.amount(when=when)))
    return sorted(found, key=lambda o: (o.when, o.name))
