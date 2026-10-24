"""Typed review of scheduled occurrences that are due or missed.

Every presentation lists the same due occurrences, grouped by schedule, and
applies one batch of decisions through :func:`resolve_due`:

* **post** — the occurrence happened; write its transaction;
* **skip** — it did not happen or was recorded another way; mark it dealt with;
* **defer** — undecided; leave it due and ask again.

Besides what is due and missed, the review lists each schedule's occurrences
within its *enter in advance* or *remind in advance* days, marked as such (#352);
like any other they stay undecided until chosen. :func:`resolve_next` enters or skips
a schedule's next occurrence however far ahead it is (#351), through the same
validated write.

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
    "NextOccurrence",
    "PostDue",
    "PostedDue",
    "PostedOccurrence",
    "post_due",
    "ResolveNext",
    "next_occurrence",
    "resolve_next",
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
    #: Why it is listed: ``overdue``, ``due``, ``advance`` (enter in advance), or
    #: ``reminder`` (remind in advance).
    stage: str = "due"


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


def _due_now(
    db: DbSQLite, as_of: date
) -> dict[tuple[str, date], tuple[schedule_engine.Occurrence, schedule_engine.ReviewStage]]:
    return {
        (occurrence.schedule.handle, occurrence.when): (occurrence, stage)
        for occurrence, stage in schedule_engine.review_occurrences(db, as_of=as_of)
    }


def pending_due_review(db: DbSQLite, as_of: date | None = None) -> list[DueScheduleReview]:
    """Due and missed occurrences through ``as_of``, and those its schedules
    enter or remind in advance, grouped by schedule."""
    today = as_of or date.today()
    grouped: dict[str, list[tuple[schedule_engine.Occurrence, str]]] = {}
    for occurrence, stage in _due_now(db, today).values():
        grouped.setdefault(occurrence.schedule.handle, []).append((occurrence, stage.value))
    reviews = []
    for handle, occurrences in grouped.items():
        occurrences.sort(key=lambda item: item[0].when)
        definition = occurrences[0][0].schedule
        reviews.append(
            DueScheduleReview(
                schedule=handle,
                name=definition.name,
                frequency=definition.recurrence.describe(),
                items=tuple(
                    DueItem(handle, item.when, item.amount, item.when < today, stage)
                    for item, stage in occurrences
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
    occurrences = {key: occurrence for key, (occurrence, _stage) in due.items()}
    return _apply(db, occurrences, request.decisions, "Review due scheduled transactions")


def _apply(
    db: DbSQLite,
    due: dict[tuple[str, date], schedule_engine.Occurrence],
    decisions: tuple[tuple[str, date, DueDecision], ...],
    message: str,
) -> ServiceResult[DueResolution]:
    """Write validated decisions as one undoable change."""
    posted: list[str] = []
    skipped = deferred = 0
    touched: dict[str, ScheduledTransaction] = {}
    try:
        with db.transaction(message) as txn:
            for handle, when, decision in decisions:
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


@dataclass(frozen=True, slots=True)
class NextOccurrence:
    """A schedule's next occurrence still to enter or skip (#351)."""

    schedule: str
    name: str
    when: date
    amount: Money


@dataclass(frozen=True, slots=True)
class ResolveNext:
    """Enter or skip ``schedule``'s next occurrence, which must still be ``when``."""

    schedule: str
    decision: DueDecision
    #: The date the user was shown; refused if the next occurrence has changed.
    when: date | None = None
    as_of: date | None = None


def next_occurrence(
    db: DbSQLite, schedule: str, as_of: date | None = None
) -> NextOccurrence | None:
    """The schedule's next occurrence still to enter or skip, however far ahead."""
    definition = db.get_scheduled(schedule)
    if definition is None:
        return None
    occurrence = schedule_engine.next_pending(db, definition, as_of=as_of)
    if occurrence is None:
        return None
    return NextOccurrence(schedule, definition.name, occurrence.when, occurrence.amount)


def resolve_next(db: DbSQLite, request: ResolveNext) -> ServiceResult[DueResolution]:
    """Enter or skip a schedule's next occurrence before it is due (#351)."""
    definition = db.get_scheduled(request.schedule)
    if definition is None:
        return ServiceResult.failure(ServiceError("schedule.due.not_found", ("schedule",)))
    if request.decision is DueDecision.DEFER:
        return ServiceResult.failure(ServiceError("schedule.next.decision", ("decision",)))
    occurrence = schedule_engine.next_pending(db, definition, as_of=request.as_of)
    if occurrence is None:
        return ServiceResult.failure(ServiceError("schedule.next.none", ("schedule",)))
    if request.when is not None and occurrence.when != request.when:
        return ServiceResult.failure(ServiceError("schedule.due.not_pending", ("when",)))
    verb = "Enter" if request.decision is DueDecision.POST else "Skip"
    return _apply(
        db,
        {(definition.handle, occurrence.when): occurrence},
        ((definition.handle, occurrence.when, request.decision),),
        f"{verb} next occurrence of {definition.name}",
    )


@dataclass(frozen=True, slots=True)
class PostDue:
    """Post what is due without a review: automatic schedules only, or every one.

    Plan-only estimates are never posted. ``as_of`` defaults to today.
    """

    as_of: date | None = None
    only_automatic: bool = True


@dataclass(frozen=True, slots=True)
class PostedOccurrence:
    transaction: str
    schedule: str
    when: date
    description: str


@dataclass(frozen=True, slots=True)
class PostedDue:
    posted: tuple[PostedOccurrence, ...]


def post_due(db: DbSQLite, request: PostDue) -> ServiceResult[PostedDue]:
    """Post every due occurrence the request allows as one undoable change."""
    try:
        written = schedule_engine.post_due(
            db, as_of=request.as_of, only_auto=request.only_automatic
        )
    except UnbalancedError:
        return ServiceResult.failure(ServiceError("schedule.due.unbalanced", ("schedule",)))
    return ServiceResult.success(
        PostedDue(
            tuple(
                PostedOccurrence(
                    txn.handle, txn.scheduled_from or "", txn.post_date, txn.description
                )
                for txn in written
            )
        )
    )
