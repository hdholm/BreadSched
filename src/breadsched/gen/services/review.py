"""Typed mutations for resolving actual transactions against the plan."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import fsa_claims, planning
from ..lib.transaction import PlanningResolution, Transaction
from .contracts import ServiceError, ServiceResult


@dataclass(frozen=True, slots=True)
class ReviewOccurrence:
    transaction: str
    occurrence: str


@dataclass(frozen=True, slots=True)
class ReviewTransaction:
    transaction: str


@dataclass(frozen=True, slots=True)
class ReviewClaimAttachment:
    transaction: str
    claim: str
    role: str
    split: str | None = None
    funding_year_start: date | None = None


@dataclass(frozen=True, slots=True)
class ReviewMutation:
    transaction: str
    resolution: PlanningResolution | None = None
    occurrence: str | None = None
    claim: str | None = None


def match_review(db: DbSQLite, request: ReviewOccurrence) -> ServiceResult[ReviewMutation]:
    transaction, error = _pending_transaction(db, request.transaction)
    if error is not None:
        return ServiceResult.failure(error)
    event, error = _available_event(db, request.occurrence)
    if error is not None:
        return ServiceResult.failure(error)
    assert transaction is not None and event is not None
    planning.actualize_transaction(transaction, event)
    with db.transaction("Match transaction to planned occurrence") as txn:
        db.commit_transaction(transaction, txn)
    return ServiceResult.success(
        ReviewMutation(transaction.handle, transaction.planning_resolution, event.key)
    )


def reject_review(db: DbSQLite, request: ReviewOccurrence) -> ServiceResult[ReviewMutation]:
    transaction, error = _pending_transaction(db, request.transaction)
    if error is not None:
        return ServiceResult.failure(error)
    event, error = _available_event(db, request.occurrence)
    if error is not None:
        return ServiceResult.failure(error)
    assert transaction is not None and event is not None
    planning.reject_candidate(transaction, event)
    with db.transaction("Reject planned occurrence candidate") as txn:
        db.commit_transaction(transaction, txn)
    return ServiceResult.success(ReviewMutation(transaction.handle, occurrence=event.key))


def skip_review(db: DbSQLite, request: ReviewOccurrence) -> ServiceResult[ReviewMutation]:
    transaction, error = _pending_transaction(db, request.transaction)
    if error is not None:
        return ServiceResult.failure(error)
    event, error = _available_event(db, request.occurrence)
    if error is not None:
        return ServiceResult.failure(error)
    assert transaction is not None and event is not None
    try:
        planning.skip_occurrence(db, event)
    except ValueError:
        return ServiceResult.failure(
            ServiceError("review.occurrence.not_skippable", ("occurrence",))
        )
    return ServiceResult.success(ReviewMutation(transaction.handle, occurrence=event.key))


def mark_review_unexpected(
    db: DbSQLite, request: ReviewTransaction
) -> ServiceResult[ReviewMutation]:
    transaction, error = _pending_transaction(db, request.transaction)
    if error is not None:
        return ServiceResult.failure(error)
    assert transaction is not None
    planning.mark_unexpected(transaction)
    with db.transaction("Mark transaction as unexpected") as txn:
        db.commit_transaction(transaction, txn)
    return ServiceResult.success(
        ReviewMutation(transaction.handle, transaction.planning_resolution)
    )


def attach_review_claim(
    db: DbSQLite, request: ReviewClaimAttachment
) -> ServiceResult[ReviewMutation]:
    if db.get_transaction(request.transaction) is None:
        return ServiceResult.failure(ServiceError("review.transaction.not_found", ("transaction",)))
    if db.get_fsa_claim(request.claim) is None:
        return ServiceResult.failure(ServiceError("review.claim.not_found", ("claim",)))
    if request.role not in {"payment", "refund", "reimbursement"}:
        return ServiceResult.failure(ServiceError("claim.attachment.role.invalid", ("role",)))
    try:
        claim = fsa_claims.attach_transaction_to_claim(
            db,
            request.claim,
            request.transaction,
            role=request.role,
            split_handle=request.split,
            funding_year_start=request.funding_year_start,
        )
    except fsa_claims.FsaClaimError as exc:
        return ServiceResult.failure(ServiceError(exc.code, exc.fields))
    return ServiceResult.success(ReviewMutation(request.transaction, claim=claim.handle))


def _pending_transaction(
    db: DbSQLite, handle: str
) -> tuple[Transaction | None, ServiceError | None]:
    transaction = db.get_transaction(handle)
    if transaction is None:
        return None, ServiceError("review.transaction.not_found", ("transaction",))
    if transaction.planning_resolution is not PlanningResolution.UNRESOLVED:
        return None, ServiceError("review.transaction.not_unresolved", ("transaction",))
    return transaction, None


def _available_event(
    db: DbSQLite, key: str
) -> tuple[planning.PlannedEvent | None, ServiceError | None]:
    event = planning.event_by_key(db, key)
    if event is None:
        return None, ServiceError("review.occurrence.not_found", ("occurrence",))
    if event.status is planning.EventStatus.ACTUALIZED:
        return None, ServiceError("review.occurrence.resolved", ("occurrence",))
    return event, None
