"""Typed review of scheduled occurrences that are due or missed.

Every presentation lists the same due occurrences, grouped by schedule, and
applies one batch of decisions through :func:`resolve_due`:

* **post** — the occurrence happened; write its transaction;
* **skip** — it did not happen or was recorded another way; mark it dealt with;
* **defer** — undecided; leave it due and ask again.

A decision names a schedule and a date rather than carrying a prepared
transaction. The batch is revalidated against the book immediately before
writing, so an occurrence posted or skipped elsewhere since the list was shown
(another window, the browser, the command line) is refused instead of being
posted twice. Plan-only estimates are never due and so are never posted. The
whole batch commits as one undoable database transaction, or nothing is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from ..db.sqlite import DbSQLite
from ..engine import schedule as schedule_engine
from ..engine.currency import commodity_fraction
from ..lib.money import Money
from ..lib.scheduled import ScheduledTransaction
from ..lib.transaction import UnbalancedError
from .contracts import ServiceError, ServiceResult

__all__ = [
    "DueDecision",
    "DueItem",
    "DueResolution",
    "DueScheduleReview",
    "ResolveDue",
    "pending_due_review",
    "resolve_due",
]


class DueDecision(str, Enum):
    POST = "post"
    SKIP = "skip"
    DEFER = "defer"


@dataclass(frozen=True, slots=True)
class DueItem:
    """One due or missed occurrence with its resolved amount."""

    schedule: str
    when: date
    amount: Money
    overdue: bool


@dataclass(frozen=True, slots=True)
class DueScheduleReview:
    """Every due occurrence of one schedule, oldest first."""

    schedule: str
    name: str
    frequency: str
    items: tuple[DueItem, ...]

    @property
    def total(self) -> Money:
        return sum((item.amount for item in self.items), Money(0))


@dataclass(frozen=True, slots=True)
class ResolveDue:
    decisions: tuple[tuple[str, date, DueDecision], ...]
    as_of: date | None = None


@dataclass(frozen=True, slots=True)
class DueResolution:
    posted: int
    skipped: int
    deferred: int
    transactions: tuple[str, ...]


def _due_now(db: DbSQLite, as_of: date) -> dict[tuple[str, date], schedule_engine.Occurrence]:
    return {
        (occurrence.schedule.handle, occurrence.when): occurrence
        for occurrence in schedule_engine.due_occurrences(db, as_of=as_of, horizon_days=0)
        if occurrence.when <= as_of
    }


def pending_due_review(db: DbSQLite, as_of: date | None = None) -> list[DueScheduleReview]:
    """Due and missed occurrences through ``as_of``, grouped by schedule."""
    today = as_of or date.today()
    grouped: dict[str, list[schedule_engine.Occurrence]] = {}
    for occurrence in _due_now(db, today).values():
        grouped.setdefault(occurrence.schedule.handle, []).append(occurrence)
    reviews = []
    for handle, occurrences in grouped.items():
        occurrences.sort(key=lambda item: item.when)
        definition = occurrences[0].schedule
        reviews.append(
            DueScheduleReview(
                schedule=handle,
                name=definition.name,
                frequency=definition.recurrence.describe(),
                items=tuple(
                    DueItem(handle, item.when, item.amount, item.when < today)
                    for item in occurrences
                ),
            )
        )
    reviews.sort(key=lambda review: (review.items[0].when, review.name.casefold()))
    return reviews


def resolve_due(db: DbSQLite, request: ResolveDue) -> ServiceResult[DueResolution]:
    """Apply one reviewed batch of post, skip, and defer decisions atomically."""
    today = request.as_of or date.today()
    due = _due_now(db, today)
    errors: list[ServiceError] = []
    seen: set[tuple[str, date]] = set()
    for handle, when, _decision in request.decisions:
        key = (handle, when)
        field = f"{handle}@{when.isoformat()}"
        if key in seen:
            errors.append(ServiceError("schedule.due.duplicate", ("decisions", field)))
            continue
        seen.add(key)
        if key not in due:
            code = (
                "schedule.due.not_found"
                if db.get_scheduled(handle) is None
                else "schedule.due.not_pending"
            )
            errors.append(ServiceError(code, ("decisions", field)))
    if errors:
        return ServiceResult.failure(*errors)

    posted: list[str] = []
    skipped = deferred = 0
    touched: dict[str, ScheduledTransaction] = {}
    try:
        with db.transaction("Review due scheduled transactions") as txn:
            for handle, when, decision in request.decisions:
                if decision is DueDecision.DEFER:
                    deferred += 1
                    continue
                occurrence = due[(handle, when)]
                definition = touched.setdefault(handle, occurrence.schedule)
                if decision is DueDecision.SKIP:
                    definition.skip(when)
                    skipped += 1
                    continue
                real = definition.instantiate(
                    when, fraction=commodity_fraction(db, definition.currency)
                )
                db.add_transaction(real, txn)
                posted.append(real.handle)
                if definition.last_posted is None or when > definition.last_posted:
                    definition.last_posted = when
            for definition in touched.values():
                db.commit_scheduled(definition, txn)
    except UnbalancedError:
        return ServiceResult.failure(ServiceError("schedule.due.unbalanced", ("decisions",)))
    return ServiceResult.success(DueResolution(len(posted), skipped, deferred, tuple(posted)))
