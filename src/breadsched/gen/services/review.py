"""Typed mutations for resolving actual transactions against the plan."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import fsa_claims, planning, review_explain
from ..lib.money import Money
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
    if request.role not in fsa_claims.ATTACHMENT_ROLES:
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


# --------------------------------------------------------------------- reads


@dataclass(frozen=True, slots=True)
class PendingActual:
    """A real transaction not yet resolved against the plan."""

    handle: str
    date: date
    description: str
    #: The transaction's positive legs added up.
    amount: Money


@dataclass(frozen=True, slots=True)
class ReviewRole:
    """One way the transaction could attach to an FSA claim."""

    role: str
    split: str
    account: str
    account_name: str
    #: Funding years a reimbursement could draw on (empty: decided by the claim).
    years: tuple[date, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewClaimChoice:
    """A claim the transaction may belong on, ranked, with the likely role."""

    claim: str
    label: str
    remaining: Money
    score: int
    reason: str
    role: str
    split: str


@dataclass(frozen=True, slots=True)
class ReviewDetail:
    """One pending actual with the planned items it could be, and why."""

    actual: PendingActual
    candidates: tuple[review_explain.CandidateExplanation, ...]
    roles: tuple[ReviewRole, ...]
    claims: tuple[ReviewClaimChoice, ...]
    fsa_hint: str | None
    #: Why nothing is offered, when no candidate is.
    no_candidate_reason: str | None


def _gross(transaction: Transaction) -> Money:
    total = Money(0)
    for split in transaction.splits:
        if split.value > 0:
            total = total + split.value
    return total


def _pending(transaction: Transaction) -> PendingActual:
    return PendingActual(
        transaction.handle, transaction.post_date, transaction.description, _gross(transaction)
    )


def review_queue(db: DbSQLite) -> tuple[PendingActual, ...]:
    """Unresolved actuals, oldest first."""
    pending = sorted(
        (
            transaction
            for transaction in db.iter_transactions()
            if transaction.planning_resolution is PlanningResolution.UNRESOLVED
        ),
        key=lambda transaction: (transaction.post_date, transaction.handle),
    )
    return tuple(_pending(transaction) for transaction in pending)


def review_detail(db: DbSQLite, handle: str) -> ServiceResult[ReviewDetail]:
    """What Review offers for one unresolved actual; writes nothing."""
    transaction, error = _pending_transaction(db, handle)
    if transaction is None:
        assert error is not None
        return ServiceResult.failure(error)
    candidates = tuple(review_explain.explain_candidates(db, transaction))
    claims = []
    for suggestion in fsa_claims.suggest_claims_for_transaction(db, transaction):
        claim = suggestion.claim
        claims.append(
            ReviewClaimChoice(
                claim.handle,
                f"{claim.service_date.isoformat()} "
                f"{claim.provider or claim.description or 'FSA claim'}",
                fsa_claims.claim_summary(db, claim).remaining_reimbursable,
                suggestion.score,
                suggestion.reason,
                suggestion.role,
                suggestion.split_handle,
            )
        )
    return ServiceResult.success(
        ReviewDetail(
            _pending(transaction),
            candidates,
            tuple(
                ReviewRole(
                    item.role,
                    item.split,
                    item.account,
                    db.full_name(item.account) or item.account,
                    tuple(item.years),
                )
                for item in fsa_claims.attachment_roles(db, transaction)
            ),
            tuple(claims),
            review_explain.fsa_hint(db, transaction),
            None if candidates else review_explain.no_candidate_reason(db, transaction),
        )
    )


def plan_occurrence(db: DbSQLite, key: str) -> ServiceResult[planning.PlannedEvent]:
    """One planned occurrence by its stable key."""
    event = planning.event_by_key(db, key)
    if event is None:
        return ServiceResult.failure(ServiceError("review.occurrence.not_found", ("occurrence",)))
    return ServiceResult.success(event)


def unresolved_occurrences(
    db: DbSQLite, start: date, end: date
) -> ServiceResult[tuple[planning.PlannedEvent, ...]]:
    """Planned occurrences in ``start``..``end`` still waiting for an actual."""
    if end < start:
        return ServiceResult.failure(ServiceError("review.range.invalid", ("end",)))
    return ServiceResult.success(tuple(planning.unresolved_events(db, start, end)))


def match_candidates(
    db: DbSQLite, handle: str, window_days: int = 7
) -> ServiceResult[tuple[planning.MatchCandidate, ...]]:
    """Unresolved occurrences that could explain one actual, best first."""
    transaction = db.get_transaction(handle)
    if transaction is None:
        return ServiceResult.failure(ServiceError("review.transaction.not_found", ("transaction",)))
    if window_days < 0:
        return ServiceResult.failure(ServiceError("review.window.invalid", ("window_days",)))
    found = planning.match_candidates(db, transaction, window_days=window_days)
    return ServiceResult.success(tuple(found))
