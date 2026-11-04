"""Matching actuals to planned occurrences by split and amount (decision 0005).

The Match windows read an occurrence with what counts toward it and the splits that
could (:func:`occurrence_matching`), or a transaction with each of its plan legs and
the occurrences each could count toward (:func:`transaction_matching`). One write
owns every change: allocate part or all of a split to an occurrence, change or
remove that allocation (:func:`allocate`), mark a split *not planned*
(:func:`mark_split_not_planned`), and close or reopen an occurrence
(:func:`close_occurrence`, :func:`reopen_occurrence`). Each validates the split, the
occurrence, its currency, and that a split's allocations never pass its value, and
a refusal leaves the book as it was under a stable code.

A transaction's resolution follows from its plan legs: *matched* when every one is
fully allocated or not planned and at least one is allocated, *unexpected* when
every one is not planned, and *unresolved* (Review still lists it) otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..db.sqlite import DbSQLite
from ..engine import planning
from ..engine.plan_allocations import account_classes, plan_legs
from ..lib.account import AccountClass
from ..lib.money import Money
from ..lib.transaction import PlanAllocation, PlanningResolution, Split, Transaction
from .contracts import ServiceError, ServiceResult

__all__ = [
    "AllocateSplit",
    "AllocatedSplit",
    "CandidateOccurrence",
    "CandidateSplit",
    "MatchingLeg",
    "OccurrenceMatching",
    "OccurrenceRequest",
    "SplitNotPlanned",
    "TransactionMatching",
    "allocate",
    "close_occurrence",
    "mark_split_not_planned",
    "occurrence_matching",
    "reopen_occurrence",
    "transaction_matching",
]

#: A commitment's candidates lie this many days either side of its date.
_WINDOW_DAYS = 7


# ---------------------------------------------------------------------- requests


@dataclass(frozen=True, slots=True)
class AllocateSplit:
    """Count ``amount`` of a split toward an occurrence; ``None`` is all that is left
    of the split, and zero removes the allocation."""

    transaction: str
    split: str
    occurrence: str
    amount: Money | None = None


@dataclass(frozen=True, slots=True)
class SplitNotPlanned:
    transaction: str
    split: str
    not_planned: bool = True


@dataclass(frozen=True, slots=True)
class OccurrenceRequest:
    occurrence: str


# ------------------------------------------------------------------------- views


@dataclass(frozen=True, slots=True)
class AllocatedSplit:
    """One split's part in an occurrence."""

    transaction: str
    split: str
    date: date
    description: str
    account_name: str
    amount: Money


@dataclass(frozen=True, slots=True)
class CandidateSplit:
    """A split that could count toward an occurrence, as a reconcile row is."""

    transaction: str
    split: str
    date: date
    num: str
    description: str
    account_name: str
    #: The accounts on the transaction's other side.
    other_accounts: tuple[str, ...]
    notes: str
    memo: str
    amount: Money
    #: What of the split no occurrence has yet, the amount a check allocates.
    unallocated: Money
    #: Already counting toward this occurrence, for this much.
    allocated: Money


@dataclass(frozen=True, slots=True)
class OccurrenceMatching:
    """An occurrence with its expected, applied, and remaining amounts."""

    key: str
    description: str
    planned_date: date
    period_end: date | None
    estimate: bool
    expected: Money
    applied: Money
    remaining: Money
    closed: bool
    closed_early: bool
    currency: str | None
    allocated: tuple[AllocatedSplit, ...]
    candidates: tuple[CandidateSplit, ...]


@dataclass(frozen=True, slots=True)
class CandidateOccurrence:
    key: str
    description: str
    planned_date: date
    estimate: bool
    expected: Money
    remaining: Money
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MatchingLeg:
    """One plan leg of a transaction, what it counts toward, and what it could."""

    split: str
    account_name: str
    memo: str
    amount: Money
    unallocated: Money
    not_planned: bool
    allocations: tuple[PlanAllocation, ...]
    candidates: tuple[CandidateOccurrence, ...]


@dataclass(frozen=True, slots=True)
class TransactionMatching:
    transaction: str
    date: date
    description: str
    notes: str
    resolution: PlanningResolution
    legs: tuple[MatchingLeg, ...]


# ------------------------------------------------------------------------ shared


def _unallocated(split: Split) -> Money:
    used = sum((abs(item.amount) for item in split.plan_allocations), Money(0))
    left = abs(split.value) - used
    if left <= 0:
        return Money(0)
    return left if split.value > 0 else -left


def _allocated_to(split: Split, key: str) -> Money:
    return sum((item.amount for item in split.plan_allocations if item.occurrence == key), Money(0))


def _resolve(transaction: Transaction, db: DbSQLite) -> None:
    """Set the resolution its plan legs imply (see the module notes)."""
    legs = plan_legs(transaction, account_classes(db))
    if not legs:
        return
    done = [split.not_planned or not _unallocated(split) for split in legs]
    allocated = any(split.plan_allocations for split in legs)
    if all(done) and allocated:
        transaction.planning_resolution = PlanningResolution.MATCHED
    elif all(split.not_planned for split in legs):
        transaction.planning_resolution = PlanningResolution.UNEXPECTED
    else:
        transaction.planning_resolution = PlanningResolution.UNRESOLVED
    # A whole-transaction link stays only while one occurrence holds every leg.
    named = {item.occurrence for split in legs for item in split.plan_allocations}
    whole = (
        len(named) == 1
        and all(not _unallocated(split) for split in legs)
        and transaction.planned_occurrence in named
    )
    if not whole and transaction.planned_occurrence is not None:
        transaction.planned_occurrence = None
        transaction.planned_for = None
        transaction.planned_amount = None


def _names(db: DbSQLite, handles: list[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for handle in handles:
        seen.setdefault(db.full_name(handle) or handle, None)
    return tuple(seen)


def _event(db: DbSQLite, key: str) -> planning.PlannedEvent | None:
    return planning.event_by_key(db, key)


# ------------------------------------------------------------------------- reads


def occurrence_matching(db: DbSQLite, key: str) -> ServiceResult[OccurrenceMatching]:
    """An occurrence, what counts toward it, and the splits that could, best first.

    Candidates are the splits on the occurrence's category accounts (its income and
    expense legs, else its legs with a planning purpose, else all its non-cash legs)
    in its period, or within seven days of a commitment's date, with something left
    to allocate or already allocated here. None starts chosen (question 3).
    """
    event = _event(db, key)
    if event is None:
        return ServiceResult.failure(ServiceError("matching.occurrence.not_found", ("occurrence",)))
    schedule = db.get_scheduled(event.source_handle or "")
    period_end = (
        planning.period_end(schedule, event.planned_date)
        if schedule is not None and event.placeholder
        else None
    )
    class_of = account_classes(db)
    categories = {
        split.account
        for split in event.expected_splits
        if class_of(split.account) in (AccountClass.INCOME, AccountClass.EXPENSE)
    } or {split.account for split in event.expected_splits if split.planning_flow is not None}
    start = (
        event.planned_date
        if period_end is not None
        else event.planned_date - timedelta(days=_WINDOW_DAYS)
    )
    end = period_end or event.planned_date + timedelta(days=_WINDOW_DAYS)
    allocated: list[AllocatedSplit] = []
    for item in event.actuals:
        for split in item.transaction.splits:
            amount = _allocated_to(split, key)
            if item.whole and not split.plan_allocations:
                amount = split.value if split.account in categories else Money(0)
            if amount:
                allocated.append(
                    AllocatedSplit(
                        item.transaction.handle,
                        split.handle,
                        item.transaction.post_date,
                        item.transaction.description,
                        db.full_name(split.account) or split.account,
                        amount,
                    )
                )
    reporting = event.expected_currency
    candidates: list[tuple[tuple[object, ...], CandidateSplit]] = []
    for transaction in db.iter_transactions(start=start, end=end):
        if (transaction.currency or reporting) != reporting:
            continue
        for split in transaction.splits:
            if split.account not in categories or split.not_planned or not split.value:
                continue
            left = _unallocated(split)
            here = _allocated_to(split, key)
            if not left and not here:
                continue
            others = [other.account for other in transaction.splits if other is not split]
            row = CandidateSplit(
                transaction.handle,
                split.handle,
                transaction.post_date,
                transaction.num,
                transaction.description,
                db.full_name(split.account) or split.account,
                _names(db, others),
                transaction.notes,
                split.memo,
                split.value,
                left,
                here,
            )
            distance = abs((transaction.post_date - event.planned_date).days)
            fits = abs(left) <= event.remaining_amount
            candidates.append(((not fits, distance, transaction.post_date, split.handle), row))
    candidates.sort(key=lambda pair: pair[0])
    return ServiceResult.success(
        OccurrenceMatching(
            event.key,
            event.description,
            event.planned_date,
            period_end,
            event.placeholder,
            event.expected_amount,
            event.applied_amount,
            event.remaining_amount,
            event.closed,
            event.closed_early,
            event.expected_currency,
            tuple(allocated),
            tuple(row for _key, row in candidates),
        )
    )


def transaction_matching(db: DbSQLite, handle: str) -> ServiceResult[TransactionMatching]:
    """A transaction's plan legs, each with its allocations and the open occurrences
    it could count toward, ranked as Review ranks them."""
    from ..engine import review_explain

    transaction = db.get_transaction(handle)
    if transaction is None:
        return ServiceResult.failure(
            ServiceError("matching.transaction.not_found", ("transaction",))
        )
    explained = review_explain.explain_candidates(db, transaction)
    legs = []
    for split in plan_legs(transaction, account_classes(db)):
        offers = tuple(
            CandidateOccurrence(
                item.candidate.event.key,
                item.candidate.event.description,
                item.candidate.event.planned_date,
                item.candidate.event.placeholder,
                item.candidate.event.expected_amount,
                item.candidate.event.remaining_amount,
                item.reasons,
            )
            for item in explained
            if split.account in {leg.account for leg in item.candidate.event.expected_splits}
        )
        legs.append(
            MatchingLeg(
                split.handle,
                db.full_name(split.account) or split.account,
                split.memo,
                split.value,
                _unallocated(split),
                split.not_planned,
                tuple(split.plan_allocations),
                offers,
            )
        )
    return ServiceResult.success(
        TransactionMatching(
            transaction.handle,
            transaction.post_date,
            transaction.description,
            transaction.notes,
            transaction.planning_resolution,
            tuple(legs),
        )
    )


# ------------------------------------------------------------------------ writes


def allocate(db: DbSQLite, request: AllocateSplit) -> ServiceResult[TransactionMatching]:
    """Set what one split counts toward one occurrence (zero removes it)."""
    transaction = db.get_transaction(request.transaction)
    if transaction is None:
        return ServiceResult.failure(
            ServiceError("matching.transaction.not_found", ("transaction",))
        )
    split = next((item for item in transaction.splits if item.handle == request.split), None)
    if split is None or not split.value:
        return ServiceResult.failure(ServiceError("matching.split.not_found", ("split",)))
    event = _event(db, request.occurrence)
    if event is None:
        return ServiceResult.failure(ServiceError("matching.occurrence.not_found", ("occurrence",)))
    if (transaction.currency or event.expected_currency) != event.expected_currency:
        return ServiceResult.failure(ServiceError("matching.currency.mismatch", ("occurrence",)))
    if not event.matchable and not any(
        item.occurrence == event.key for item in split.plan_allocations
    ):
        return ServiceResult.failure(ServiceError("matching.occurrence.closed", ("occurrence",)))
    others = [item for item in split.plan_allocations if item.occurrence != event.key]
    room = abs(split.value) - sum((abs(item.amount) for item in others), Money(0))
    amount = request.amount
    if amount is None:
        amount = room if split.value > 0 else -room
    if amount and (amount > 0) != (split.value > 0):
        return ServiceResult.failure(ServiceError("matching.amount.sign", ("amount",)))
    if abs(amount) > room:
        return ServiceResult.failure(ServiceError("matching.amount.too_large", ("amount",)))
    split.plan_allocations = others + (
        [PlanAllocation(event.key, amount, event.planned_date, event.expected_amount)]
        if amount
        else []
    )
    if amount:
        split.not_planned = False
    _resolve(transaction, db)
    message = "Match split to planned occurrence" if amount else "Remove a plan match"
    with db.transaction(message) as txn:
        db.commit_transaction(transaction, txn)
    return transaction_matching(db, transaction.handle)


def mark_split_not_planned(
    db: DbSQLite, request: SplitNotPlanned
) -> ServiceResult[TransactionMatching]:
    """Mark one split as needing no plan (or undo that); it keeps no allocation."""
    transaction = db.get_transaction(request.transaction)
    if transaction is None:
        return ServiceResult.failure(
            ServiceError("matching.transaction.not_found", ("transaction",))
        )
    split = next((item for item in transaction.splits if item.handle == request.split), None)
    if split is None:
        return ServiceResult.failure(ServiceError("matching.split.not_found", ("split",)))
    split.not_planned = request.not_planned
    if request.not_planned:
        split.plan_allocations.clear()
    _resolve(transaction, db)
    with db.transaction("Mark split not planned" if request.not_planned else "Plan a split") as txn:
        db.commit_transaction(transaction, txn)
    return transaction_matching(db, transaction.handle)


def _set_closed(
    db: DbSQLite, request: OccurrenceRequest, closed: bool
) -> ServiceResult[OccurrenceMatching]:
    event = _event(db, request.occurrence)
    if event is None or event.source is not planning.EventSource.SCHEDULED:
        return ServiceResult.failure(ServiceError("matching.occurrence.not_found", ("occurrence",)))
    schedule = db.get_scheduled(event.source_handle or "")
    if schedule is None:
        return ServiceResult.failure(ServiceError("matching.occurrence.not_found", ("occurrence",)))
    when = event.planned_date
    if closed and when not in schedule.closed_occurrences:
        schedule.closed_occurrences = sorted([*schedule.closed_occurrences, when])
    elif not closed and when in schedule.closed_occurrences:
        schedule.closed_occurrences = [item for item in schedule.closed_occurrences if item != when]
    else:
        return occurrence_matching(db, event.key)
    with db.transaction("Close occurrence" if closed else "Reopen occurrence") as txn:
        db.commit_scheduled(schedule, txn)
    return occurrence_matching(db, event.key)


def close_occurrence(db: DbSQLite, request: OccurrenceRequest) -> ServiceResult[OccurrenceMatching]:
    """Close an open occurrence early: what remains of it is no longer expected."""
    return _set_closed(db, request, True)


def reopen_occurrence(
    db: DbSQLite, request: OccurrenceRequest
) -> ServiceResult[OccurrenceMatching]:
    """Undo :func:`close_occurrence`. An estimate whose period has ended stays closed."""
    return _set_closed(db, request, False)
