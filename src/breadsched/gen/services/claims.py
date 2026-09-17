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


@dataclass(frozen=True, slots=True)
class ClaimInput:
    service_date: date
    provider: str = ""
    description: str = ""
    eob_responsibility: Money | None = None
    payments: tuple[ClaimLinkInput, ...] = ()
    refunds: tuple[ClaimLinkInput, ...] = ()
    allocations: tuple[ClaimAllocationInput, ...] = ()


@dataclass(frozen=True, slots=True)
class SaveClaim:
    definition: ClaimInput
    existing_handle: str | None = None


@dataclass(frozen=True, slots=True)
class SavedClaim:
    handle: str


@dataclass(frozen=True, slots=True)
class DeleteClaim:
    handle: str


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
            )
            for item in definition.allocations
        ],
    )


def save_claim(db: DbSQLite, request: SaveClaim) -> ServiceResult[SavedClaim]:
    """Validate and persist a complete claim through one atomic service boundary."""
    if request.existing_handle is not None and db.get_fsa_claim(request.existing_handle) is None:
        return ServiceResult.failure(ServiceError("claim.not_found", ("handle",)))
    candidate = build_claim(request)
    try:
        fsa_claims.save_claim(db, candidate)
    except fsa_claims.FsaClaimError as exc:
        return ServiceResult.failure(ServiceError(exc.code, exc.fields))
    return ServiceResult.success(SavedClaim(candidate.handle))


def delete_claim(db: DbSQLite, request: DeleteClaim) -> ServiceResult[SavedClaim]:
    try:
        fsa_claims.delete_claim(db, request.handle)
    except KeyError:
        return ServiceResult.failure(ServiceError("claim.not_found", ("handle",)))
    return ServiceResult.success(SavedClaim(request.handle))
