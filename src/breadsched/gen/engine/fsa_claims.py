"""FSA healthcare claims linked to ordinary transactions and funding years."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from ..db.sqlite import DbSQLite
from ..lib.account import AccountClass, AccountPlanningRole, FsaFundingYear
from ..lib.fsa_claim import FsaClaim, FsaClaimAllocation, FsaClaimSplitLink
from ..lib.money import Money
from . import fsa

__all__ = [
    "FsaClaimStatus",
    "FsaClaimSummary",
    "attach_transaction_to_claim",
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
    refunds: Money
    net_paid: Money
    eob_responsibility: Money | None
    reimbursable: Money
    reimbursed: Money
    rejected: Money
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
    for link in claim.refunds:
        _resolve_link(db, link)
    for allocation in claim.allocations:
        year = _allocation_year(db, allocation)
        if allocation.target is not None and allocation.target < 0:
            raise ValueError("FSA allocation target must not be negative")
        runout = year.runout_through or year.through
        for rejection in allocation.rejections:
            if rejection.amount < 0:
                raise ValueError("rejected reimbursement amount must not be negative")
            if rejection.attempted_on > runout:
                raise ValueError(
                    "rejected reimbursement is after the funding year's run-out window"
                )
        for link in allocation.reimbursements:
            key = (link.transaction, link.split)
            if key in seen_reimbursements:
                raise ValueError("an FSA reimbursement split can only be allocated once")
            seen_reimbursements.add(key)
            transaction, split = _resolve_link(db, link)
            if split.account != allocation.account:
                raise ValueError("reimbursement split does not belong to allocation FSA")
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



def attach_transaction_to_claim(
    db: DbSQLite,
    claim_handle: str,
    transaction_handle: str,
    *,
    role: str,
    split_handle: str | None = None,
    funding_year_start: date | None = None,
) -> FsaClaim:
    """Attach one ledger split to an existing claim from entry/review workflows."""
    claim = next((item for item in iter_claims(db) if item.handle == claim_handle), None)
    if claim is None:
        raise KeyError(claim_handle)
    transaction = db.get_transaction(transaction_handle)
    if transaction is None:
        raise KeyError(transaction_handle)

    candidates = []
    for split in transaction.splits:
        account = db.get_account(split.account)
        if account is None:
            continue
        eligible = (
            role == "payment"
            and account.account_class is AccountClass.EXPENSE
            and split.value > 0
        ) or (
            role == "refund"
            and account.account_class is AccountClass.EXPENSE
            and split.value < 0
        ) or (
            role == "reimbursement"
            and account.planning_role is AccountPlanningRole.FSA
            and split.value < 0
        )
        if eligible and (split_handle is None or split.handle == split_handle):
            candidates.append((split, account))
    if len(candidates) != 1:
        raise ValueError("choose exactly one eligible transaction split")
    split, account = candidates[0]
    link = FsaClaimSplitLink(transaction.handle, split.handle)

    if role == "payment":
        if link not in claim.payments:
            claim.payments.append(link)
    elif role == "refund":
        if link not in claim.refunds:
            claim.refunds.append(link)
    elif role == "reimbursement":
        eligible_years = [
            year
            for year in account.fsa_years
            if transaction.post_date <= (year.runout_through or year.through)
        ]
        if funding_year_start is not None:
            eligible_years = [year for year in eligible_years if year.start == funding_year_start]
        else:
            service_years = [
                year
                for year in eligible_years
                if year.start <= claim.service_date <= year.through
            ]
            if len(service_years) == 1:
                eligible_years = service_years
        if len(eligible_years) != 1:
            raise ValueError("choose an FSA funding year for this reimbursement")
        year = eligible_years[0]
        allocation = next(
            (
                item
                for item in claim.allocations
                if item.account == account.handle
                and item.funding_year_start == year.start
            ),
            None,
        )
        if allocation is None:
            allocation = FsaClaimAllocation(account.handle, year.start)
            claim.allocations.append(allocation)
        if link not in allocation.reimbursements:
            allocation.reimbursements.append(link)
    else:
        raise ValueError("unknown FSA claim attachment role")

    return save_claim(db, claim)

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
    refunds = _sum_links(db, claim.refunds)
    net_paid = paid - refunds
    reimbursed = Money(0)
    rejected = Money(0)
    available = Money(0)
    target_total = Money(0)
    has_targets = False
    for allocation in claim.allocations:
        account = db.get_account(allocation.account)
        if account is None:
            raise ValueError("claim allocation account no longer exists")
        year = _allocation_year(db, allocation)
        year_status = fsa.year_status(db, account, year, as_of=when)
        available = available + year_status.remaining
        reimbursed = reimbursed + _sum_links(db, allocation.reimbursements)
        for rejection in allocation.rejections:
            if rejection.amount < 0:
                raise ValueError("rejected reimbursement amount must not be negative")
            rejected = rejected + rejection.amount
        if allocation.target is not None:
            has_targets = True
            target_total = target_total + allocation.target

    if net_paid < 0:
        reimbursable = Money(0)
        status = FsaClaimStatus.NEEDS_REVIEW
    elif claim.eob_responsibility is None:
        reimbursable = net_paid
        status = FsaClaimStatus.WAITING_EOB
    else:
        reimbursable = min(net_paid, claim.eob_responsibility)
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
        refunds=refunds,
        net_paid=net_paid,
        eob_responsibility=claim.eob_responsibility,
        reimbursable=reimbursable,
        reimbursed=reimbursed,
        rejected=rejected,
        remaining_reimbursable=remaining,
        available_fsa=available,
        status=status,
    )
