"""FSA healthcare claims linked to ordinary transactions and funding years."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from ..db.sqlite import DbSQLite
from ..lib.account import AccountPlanningRole, FsaFundingYear
from ..lib.fsa_claim import FsaClaim, FsaClaimAllocation, FsaClaimSplitLink
from ..lib.money import Money
from . import fsa

__all__ = [
    "FsaClaimStatus",
    "FsaClaimSummary",
    "claim_summary",
    "delete_claim",
    "iter_claims",
    "save_claim",
]

_METADATA_KEY = "fsa_claims"


class FsaClaimStatus(str, Enum):
    WAITING_EOB = "waiting_eob"
    OPEN = "open"
    PARTIAL = "partial"
    FULLY_REIMBURSED = "fully_reimbursed"
    CLOSED_NO_FUNDS = "closed_no_funds"
    NEEDS_REVIEW = "needs_review"

    @property
    def label(self) -> str:
        return {
            FsaClaimStatus.WAITING_EOB: "Waiting for EOB",
            FsaClaimStatus.OPEN: "Open",
            FsaClaimStatus.PARTIAL: "Partially reimbursed",
            FsaClaimStatus.FULLY_REIMBURSED: "Fully reimbursed",
            FsaClaimStatus.CLOSED_NO_FUNDS: "Closed — no funds",
            FsaClaimStatus.NEEDS_REVIEW: "Needs review",
        }[self]


@dataclass(frozen=True)
class FsaClaimSummary:
    claim: FsaClaim
    paid: Money
    eob_responsibility: Money | None
    reimbursable: Money
    reimbursed: Money
    remaining_reimbursable: Money
    available_fsa: Money
    status: FsaClaimStatus


def iter_claims(db: DbSQLite) -> list[FsaClaim]:
    raw = db.get_metadata(_METADATA_KEY, [])
    return [FsaClaim.from_dict(item) for item in raw]


def _store(db: DbSQLite, claims: list[FsaClaim]) -> None:
    db.set_metadata(_METADATA_KEY, [claim.serialize() for claim in claims])


def _resolve_link(db: DbSQLite, link: FsaClaimSplitLink):
    transaction = db.get_transaction(link.transaction)
    if transaction is None:
        raise ValueError("linked transaction no longer exists")
    split = next((item for item in transaction.splits if item.handle == link.split), None)
    if split is None:
        raise ValueError("linked transaction split no longer exists")
    return transaction, split


def _allocation_year(db: DbSQLite, allocation: FsaClaimAllocation) -> FsaFundingYear:
    account = db.get_account(allocation.account)
    if account is None or account.planning_role is not AccountPlanningRole.FSA:
        raise ValueError("claim allocation must reference an FSA account")
    year = next(
        (item for item in account.fsa_years if item.start == allocation.funding_year_start),
        None,
    )
    if year is None:
        raise ValueError("claim allocation references an unknown FSA funding year")
    return year


def save_claim(db: DbSQLite, claim: FsaClaim) -> FsaClaim:
    """Persist a claim and attribute linked FSA reimbursements to their plan years."""
    seen_reimbursements: set[tuple[str, str]] = set()
    for link in claim.payments:
        _resolve_link(db, link)
    for allocation in claim.allocations:
        year = _allocation_year(db, allocation)
        if allocation.target is not None and allocation.target < 0:
            raise ValueError("FSA allocation target must not be negative")
        for link in allocation.reimbursements:
            key = (link.transaction, link.split)
            if key in seen_reimbursements:
                raise ValueError("an FSA reimbursement split can only be allocated once")
            seen_reimbursements.add(key)
            transaction, split = _resolve_link(db, link)
            if split.account != allocation.account:
                raise ValueError("reimbursement split does not belong to allocation FSA")
            runout = year.runout_through or year.through
            if transaction.post_date > runout:
                raise ValueError("reimbursement is after the funding year's run-out window")
            if split.fsa_year_start != year.start:
                split.fsa_year_start = year.start
                with db.transaction("Assign FSA reimbursement funding year") as txn:
                    db.commit_transaction(transaction, txn)
    claims = iter_claims(db)
    claims = [item for item in claims if item.handle != claim.handle]
    claims.append(claim)
    claims.sort(key=lambda item: (item.service_date, item.handle))
    _store(db, claims)
    return claim


def delete_claim(db: DbSQLite, handle: str) -> None:
    claims = iter_claims(db)
    updated = [claim for claim in claims if claim.handle != handle]
    if len(updated) == len(claims):
        raise KeyError(handle)
    _store(db, updated)


def _sum_links(db: DbSQLite, links: list[FsaClaimSplitLink]) -> Money:
    total = Money(0)
    for link in links:
        _transaction, split = _resolve_link(db, link)
        total = total + abs(split.value)
    return total


def claim_summary(
    db: DbSQLite,
    claim: FsaClaim,
    *,
    as_of: date | None = None,
) -> FsaClaimSummary:
    when = as_of or date.today()
    paid = _sum_links(db, claim.payments)
    reimbursed = Money(0)
    available = Money(0)
    target_total = Money(0)
    has_targets = False
    for allocation in claim.allocations:
        account = db.get_account(allocation.account)
        if account is None:
            raise ValueError("claim allocation account no longer exists")
        year = _allocation_year(db, allocation)
        status = fsa.year_status(db, account, year, as_of=when)
        available = available + status.remaining
        reimbursed = reimbursed + _sum_links(db, allocation.reimbursements)
        if allocation.target is not None:
            has_targets = True
            target_total = target_total + allocation.target

    if claim.eob_responsibility is None:
        reimbursable = paid
        status = FsaClaimStatus.WAITING_EOB
    else:
        reimbursable = min(paid, claim.eob_responsibility)
        if has_targets and target_total > reimbursable:
            status = FsaClaimStatus.NEEDS_REVIEW
        elif reimbursed > reimbursable:
            status = FsaClaimStatus.NEEDS_REVIEW
        elif reimbursable > 0 and reimbursed >= reimbursable:
            status = FsaClaimStatus.FULLY_REIMBURSED
        elif available <= 0:
            status = FsaClaimStatus.CLOSED_NO_FUNDS
        elif reimbursed > 0:
            status = FsaClaimStatus.PARTIAL
        else:
            status = FsaClaimStatus.OPEN
    remaining = max(reimbursable - reimbursed, Money(0))
    return FsaClaimSummary(
        claim=claim,
        paid=paid,
        eob_responsibility=claim.eob_responsibility,
        reimbursable=reimbursable,
        reimbursed=reimbursed,
        remaining_reimbursable=remaining,
        available_fsa=available,
        status=status,
    )
