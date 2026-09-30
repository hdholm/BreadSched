"""Typed FSA claim construction and mutation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import fsa_claims
from ..lib.base import create_handle
from ..lib.fsa_claim import (
    FsaClaim,
    FsaClaimAllocation,
    FsaClaimRejection,
    FsaClaimSplitLink,
)
from ..lib.money import Money
from .contracts import ServiceError, ServiceResult


@dataclass(frozen=True, slots=True)
class ClaimLinkInput:
    transaction: str
    split: str


@dataclass(frozen=True, slots=True)
class ClaimRejectionInput:
    attempted_on: date
    amount: Money
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ClaimAllocationInput:
    account: str
    funding_year_start: date
    target: Money | None = None
    reimbursements: tuple[ClaimLinkInput, ...] = ()
    rejections: tuple[ClaimRejectionInput, ...] = ()
    #: Splits paying money back into the FSA for this funding year.
    repayments: tuple[ClaimLinkInput, ...] = ()


@dataclass(frozen=True, slots=True)
class ClaimInput:
    service_date: date
    provider: str = ""
    description: str = ""
    eob_responsibility: Money | None = None
    payments: tuple[ClaimLinkInput, ...] = ()
    refunds: tuple[ClaimLinkInput, ...] = ()
    allocations: tuple[ClaimAllocationInput, ...] = ()
    #: A receivable whose payer covers part of this expense (issue #192).
    receivable: str | None = None
    #: Why an EOB already entered changed (a corrected or late EOB); recorded
    #: in the claim's history only when the EOB does change.
    eob_note: str = ""


@dataclass(frozen=True, slots=True)
class SaveClaim:
    definition: ClaimInput
    existing_handle: str | None = None
    #: The date an EOB change is recorded on; today when omitted.
    changed_on: date | None = None


@dataclass(frozen=True, slots=True)
class SavedClaim:
    handle: str


@dataclass(frozen=True, slots=True)
class DeleteClaim:
    handle: str


@dataclass(frozen=True, slots=True)
class CloseClaim:
    """Stop pursuing what is left to reimburse on ``on``."""

    handle: str
    on: date
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ReopenClaim:
    """Pursue a closed claim again from ``on``."""

    handle: str
    on: date
    note: str = ""


def build_claim(request: SaveClaim) -> FsaClaim:
    definition = request.definition
    return FsaClaim(
        handle=request.existing_handle or create_handle(),
        service_date=definition.service_date,
        provider=definition.provider.strip(),
        description=definition.description.strip(),
        eob_responsibility=definition.eob_responsibility,
        payments=[FsaClaimSplitLink(item.transaction, item.split) for item in definition.payments],
        refunds=[FsaClaimSplitLink(item.transaction, item.split) for item in definition.refunds],
        allocations=[
            FsaClaimAllocation(
                item.account,
                item.funding_year_start,
                item.target,
                [FsaClaimSplitLink(link.transaction, link.split) for link in item.reimbursements],
                [
                    FsaClaimRejection(rejection.attempted_on, rejection.amount, rejection.reason)
                    for rejection in item.rejections
                ],
                [FsaClaimSplitLink(link.transaction, link.split) for link in item.repayments],
            )
            for item in definition.allocations
        ],
        receivable=definition.receivable or None,
    )


def save_claim(db: DbSQLite, request: SaveClaim) -> ServiceResult[SavedClaim]:
    """Validate and persist a complete claim through one atomic service boundary."""
    if request.existing_handle is not None and db.get_fsa_claim(request.existing_handle) is None:
        return ServiceResult.failure(ServiceError("claim.not_found", ("handle",)))
    candidate = build_claim(request)
    try:
        fsa_claims.save_claim(
            db, candidate, today=request.changed_on, eob_note=request.definition.eob_note
        )
    except fsa_claims.FsaClaimError as exc:
        return ServiceResult.failure(ServiceError(exc.code, exc.fields))
    return ServiceResult.success(SavedClaim(candidate.handle))


def delete_claim(db: DbSQLite, request: DeleteClaim) -> ServiceResult[SavedClaim]:
    try:
        fsa_claims.delete_claim(db, request.handle)
    except KeyError:
        return ServiceResult.failure(ServiceError("claim.not_found", ("handle",)))
    return ServiceResult.success(SavedClaim(request.handle))


def close_claim(db: DbSQLite, request: CloseClaim) -> ServiceResult[SavedClaim]:
    try:
        fsa_claims.close_claim(db, request.handle, on=request.on, reason=request.reason)
    except KeyError:
        return ServiceResult.failure(ServiceError("claim.not_found", ("handle",)))
    except fsa_claims.FsaClaimError as exc:
        return ServiceResult.failure(ServiceError(exc.code, exc.fields))
    return ServiceResult.success(SavedClaim(request.handle))


def reopen_claim(db: DbSQLite, request: ReopenClaim) -> ServiceResult[SavedClaim]:
    try:
        fsa_claims.reopen_claim(db, request.handle, on=request.on, note=request.note)
    except KeyError:
        return ServiceResult.failure(ServiceError("claim.not_found", ("handle",)))
    except fsa_claims.FsaClaimError as exc:
        return ServiceResult.failure(ServiceError(exc.code, exc.fields))
    return ServiceResult.success(SavedClaim(request.handle))
