"""Typed transaction construction and atomic persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import fsa_claims, investment
from ..lib.money import Money
from ..lib.transaction import (
    InvestmentActivityKind,
    PlanningFlowKind,
    Split,
    Transaction,
)
from .contracts import ServiceError, ServiceResult


@dataclass(frozen=True, slots=True)
class TransactionSplitInput:
    """One editable ledger leg, using exact transaction-currency value."""

    account: str
    value: Money
    handle: str | None = None
    memo: str = ""
    planning_flow: PlanningFlowKind | None = None
    investment_activity: InvestmentActivityKind | None = None


@dataclass(frozen=True, slots=True)
class TransactionInput:
    """Presentation-neutral editable fields for one ledger transaction."""

    post_date: date
    description: str
    splits: tuple[TransactionSplitInput, ...]
    num: str = ""
    notes: str = ""
    currency: str | None = None
    investment_activity: InvestmentActivityKind | None = None


@dataclass(frozen=True, slots=True)
class ClaimAttachment:
    """Optional claim link committed with a newly posted transaction."""

    claim: str
    role: str
    funding_year_start: date | None = None


@dataclass(frozen=True, slots=True)
class SaveTransaction:
    definition: TransactionInput
    existing_handle: str | None = None
    claim_attachment: ClaimAttachment | None = None
    source: Transaction | None = None


@dataclass(frozen=True, slots=True)
class SavedTransaction:
    handle: str
    post_date: date
    description: str


def build_transaction(
    db: DbSQLite,
    request: SaveTransaction,
) -> ServiceResult[Transaction]:
    """Build and validate a candidate without changing the book."""
    if request.source is not None and request.existing_handle != request.source.handle:
        return ServiceResult.failure(ServiceError("transaction.source.mismatch", ("handle",)))
    existing = request.source
    if existing is None and request.existing_handle is not None:
        existing = db.get_transaction(request.existing_handle)
    if request.existing_handle is not None and existing is None:
        return ServiceResult.failure(ServiceError("transaction.not_found", ("handle",)))

    definition = request.definition
    errors: list[ServiceError] = []
    if not definition.description.strip():
        errors.append(ServiceError("transaction.description.required", ("description",)))
    if len(definition.splits) < 2:
        errors.append(ServiceError("transaction.splits.too_few", ("splits",)))
    if len({split.account for split in definition.splits}) < 2:
        errors.append(ServiceError("transaction.accounts.same", ("splits",)))

    source_splits = {split.handle: split for split in existing.splits} if existing else {}
    seen_handles: set[str] = set()
    candidate_splits: list[Split] = []
    for index, item in enumerate(definition.splits):
        path = f"splits.{index}"
        account = db.get_account(item.account)
        if account is None:
            errors.append(ServiceError("transaction.account.not_found", (f"{path}.account",)))
        source = source_splits.get(item.handle or "")
        if item.handle is not None:
            if item.handle in seen_handles:
                errors.append(ServiceError("transaction.split.duplicate", (f"{path}.handle",)))
            seen_handles.add(item.handle)
            if existing is not None and source is None:
                errors.append(ServiceError("transaction.split.not_found", (f"{path}.handle",)))
        if (
            account is not None
            and account.hidden
            and (source is None or source.account != item.account)
        ):
            errors.append(ServiceError("transaction.account.hidden", (f"{path}.account",)))

        if source is not None:
            split = Split.from_dict(source.serialize())
            if split.account != item.account:
                split.quantity = item.value
        else:
            split = Split(item.account, item.value, handle=item.handle)
        split.account = item.account
        split.value = item.value
        split.memo = item.memo.strip()
        split.planning_flow = item.planning_flow
        split.investment_activity = item.investment_activity
        default_activity = definition.investment_activity
        if split.investment_activity is None and default_activity is not None:
            direction = default_activity.direction
            if (direction >= 0 and item.value > 0) or (direction <= 0 and item.value < 0):
                split.investment_activity = default_activity
        candidate_splits.append(split)

    if sum((split.value for split in definition.splits), Money(0)):
        errors.append(ServiceError("transaction.unbalanced", ("splits",)))
    if errors:
        return ServiceResult.failure(*errors)

    if existing is not None:
        candidate = Transaction.from_dict(existing.serialize())
    else:
        candidate = Transaction()
    candidate.post_date = definition.post_date
    candidate.description = definition.description.strip()
    candidate.num = definition.num.strip()
    candidate.notes = definition.notes.strip()
    if existing is None:
        candidate.currency = definition.currency
    candidate.splits = candidate_splits

    if investment.activity_problems(db, candidate.splits):
        return ServiceResult.failure(ServiceError("transaction.investment.invalid", ("splits",)))
    return ServiceResult.success(candidate)


def save_transaction(
    db: DbSQLite,
    request: SaveTransaction,
) -> ServiceResult[SavedTransaction]:
    """Validate and persist a transaction, plus an optional claim link, atomically."""
    if request.existing_handle is not None and db.get_transaction(request.existing_handle) is None:
        return ServiceResult.failure(ServiceError("transaction.not_found", ("handle",)))
    built = build_transaction(db, request)
    if built.value is None:
        return ServiceResult.failure(*built.errors)
    candidate = built.value
    action = "Edit" if request.existing_handle is not None else "Add"

    class ClaimNotFound(Exception):
        pass

    class ClaimInvalid(Exception):
        pass

    try:
        with db.transaction(f"{action} {candidate.description}") as txn:
            if request.existing_handle is None:
                db.add_transaction(candidate, txn)
            else:
                db.commit_transaction(candidate, txn)
            attachment = request.claim_attachment
            if attachment is not None:
                try:
                    fsa_claims.attach_transaction_to_claim(
                        db,
                        attachment.claim,
                        candidate.handle,
                        role=attachment.role,
                        funding_year_start=attachment.funding_year_start,
                        txn=txn,
                    )
                except KeyError as exc:
                    raise ClaimNotFound from exc
                except ValueError as exc:
                    raise ClaimInvalid from exc
    except ClaimNotFound:
        return ServiceResult.failure(ServiceError("transaction.claim.not_found", ("claim",)))
    except ClaimInvalid:
        return ServiceResult.failure(ServiceError("transaction.claim.invalid", ("claim",)))
    return ServiceResult.success(
        SavedTransaction(candidate.handle, candidate.post_date, candidate.description)
    )
