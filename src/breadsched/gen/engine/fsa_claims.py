"""FSA healthcare claims linked to ordinary transactions and funding years."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum

from ..db.base import DbTxn
from ..db.sqlite import DbSQLite
from ..lib.account import AccountClass, AccountType, FsaFundingYear
from ..lib.fsa_claim import FsaClaim, FsaClaimAllocation, FsaClaimSplitLink
from ..lib.money import Money
from ..lib.transaction import Split, Transaction
from . import fsa

__all__ = [
    "FsaClaimError",
    "FsaClaimStatus",
    "FsaClaimSuggestion",
    "FsaClaimSummary",
    "attach_transaction_to_claim",
    "claim_summary",
    "claim_year_window",
    "delete_claim",
    "iter_claims",
    "save_claim",
    "suggest_claims_for_transaction",
]


class FsaClaimError(ValueError):
    """A stable claim failure independent of presentation wording."""

    def __init__(self, code: str, fields: tuple[str, ...], message: str) -> None:
        super().__init__(message)
        self.code = code
        self.fields = fields


@dataclass(frozen=True)
class FsaClaimSuggestion:
    claim: FsaClaim
    role: str
    split_handle: str
    score: int
    reason: str


def claim_year_window(db: DbSQLite, service_date: date) -> tuple[date, date] | None:
    """Return the combined FSA plan-year/run-out window containing a service date."""
    matches: list[FsaFundingYear] = []
    for account in db.iter_accounts():
        if account.atype is not AccountType.FSA:
            continue
        matches.extend(
            year for year in account.fsa_years if year.start <= service_date <= year.through
        )
    if not matches:
        return None
    return (
        min(year.start for year in matches),
        max((year.runout_through or year.through) for year in matches),
    )


def _claim_words(claim: FsaClaim) -> set[str]:
    text = f"{claim.provider} {claim.description}".lower()
    return {word for word in re.findall(r"[a-z0-9]+", text) if len(word) > 2}


def _transaction_words(transaction: Transaction) -> set[str]:
    return {
        word for word in re.findall(r"[a-z0-9]+", transaction.description.lower()) if len(word) > 2
    }


def suggest_claims_for_transaction(
    db: DbSQLite, transaction: Transaction
) -> list[FsaClaimSuggestion]:
    """Rank claims and explain the most likely attachment role for one transaction."""
    eligible: list[tuple[str, Split, str | None]] = []
    for split in transaction.splits:
        account = db.get_account(split.account)
        if account is None:
            continue
        if account.account_class is AccountClass.EXPENSE and split.value > 0:
            eligible.append(("payment", split, None))
        elif account.account_class is AccountClass.EXPENSE and split.value < 0:
            eligible.append(("refund", split, None))
        if account.atype is AccountType.FSA and split.value < 0:
            eligible.append(("reimbursement", split, account.handle))
    if not eligible:
        return []

    txn_words = _transaction_words(transaction)
    suggestions: list[FsaClaimSuggestion] = []
    for claim in iter_claims(db):
        summary = claim_summary(db, claim)
        if summary.status is FsaClaimStatus.FULLY_REIMBURSED:
            continue

        best: FsaClaimSuggestion | None = None
        for role, split, reimbursement_account in eligible:
            if role == "refund" and summary.net_paid <= 0:
                continue
            if role == "reimbursement":
                account = db.get_account(reimbursement_account or "")
                if account is None:
                    continue
                compatible = any(
                    year.start <= claim.service_date <= year.through
                    and transaction.post_date <= (year.runout_through or year.through)
                    for year in account.fsa_years
                )
                if not compatible:
                    continue

            score = 0
            reasons: list[str] = []
            distance = abs((transaction.post_date - claim.service_date).days)
            if distance <= 14:
                score += 35
                reasons.append("near service date")
            elif distance <= 60:
                score += 25
                reasons.append("close to service date")
            elif distance <= 180:
                score += 10
            if transaction.post_date < claim.service_date:
                score -= 5

            overlap = txn_words & _claim_words(claim)
            if overlap:
                score += min(30, 10 * len(overlap))
                reasons.append("description match")

            amount = abs(split.value)
            if role == "payment":
                target = claim.eob_responsibility or summary.remaining_reimbursable
                role_label = "provider payment"
            elif role == "refund":
                target = summary.net_paid
                role_label = "provider refund"
                if {"refund", "credit"} & txn_words:
                    score += 15
                    reasons.append("refund/credit description")
            else:
                target = summary.remaining_reimbursable
                role_label = "FSA reimbursement"
                score += 20
                reasons.append("compatible FSA year")

            if amount > 0 and target > 0:
                difference = abs(amount - target)
                if difference <= Money("1.00"):
                    score += 25
                    reasons.append("amount match")
                elif difference <= target / 10:
                    score += 15
                    reasons.append("similar amount")

            reason = f"likely {role_label}"
            if reasons:
                reason += ": " + ", ".join(reasons)
            candidate = FsaClaimSuggestion(
                claim=claim,
                role=role,
                split_handle=split.handle,
                score=score,
                reason=reason,
            )
            if best is None or candidate.score > best.score:
                best = candidate
        if best is not None:
            suggestions.append(best)

    suggestions.sort(
        key=lambda item: (-item.score, -item.claim.service_date.toordinal(), item.claim.handle)
    )
    return suggestions


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
    return list(db.iter_fsa_claims())


def _resolve_link(db: DbSQLite, link: FsaClaimSplitLink):
    transaction = db.get_transaction(link.transaction)
    if transaction is None:
        raise FsaClaimError(
            "claim.link.transaction.not_found",
            ("links",),
            "linked transaction no longer exists",
        )
    split = next((item for item in transaction.splits if item.handle == link.split), None)
    if split is None:
        raise FsaClaimError(
            "claim.link.split.not_found",
            ("links",),
            "linked transaction split no longer exists",
        )
    return transaction, split


def _allocation_year(db: DbSQLite, allocation: FsaClaimAllocation) -> FsaFundingYear:
    account = db.get_account(allocation.account)
    if account is None or account.atype is not AccountType.FSA:
        raise FsaClaimError(
            "claim.allocation.account.invalid",
            ("allocations",),
            "claim allocation must reference an FSA account",
        )
    year = next(
        (item for item in account.fsa_years if item.start == allocation.funding_year_start),
        None,
    )
    if year is None:
        raise FsaClaimError(
            "claim.allocation.year.not_found",
            ("allocations",),
            "claim allocation references an unknown FSA funding year",
        )
    return year


def save_claim(db: DbSQLite, claim: FsaClaim, *, txn: DbTxn | None = None) -> FsaClaim:
    """Persist a claim and linked split classifications as one atomic edit."""
    seen_reimbursements: set[tuple[str, str]] = set()
    assignments: dict[str, list[tuple[str, date]]] = {}
    for link in claim.payments:
        _resolve_link(db, link)
    for link in claim.refunds:
        _resolve_link(db, link)
    for allocation in claim.allocations:
        year = _allocation_year(db, allocation)
        if allocation.target is not None and allocation.target < 0:
            raise FsaClaimError(
                "claim.allocation.target.negative",
                ("allocations",),
                "FSA allocation target must not be negative",
            )
        runout = year.runout_through or year.through
        for rejection in allocation.rejections:
            if rejection.amount < 0:
                raise FsaClaimError(
                    "claim.rejection.amount.negative",
                    ("allocations",),
                    "rejected reimbursement amount must not be negative",
                )
            if rejection.attempted_on > runout:
                raise FsaClaimError(
                    "claim.rejection.after_runout",
                    ("allocations",),
                    "rejected reimbursement is after the funding year's run-out window",
                )
        for link in allocation.reimbursements:
            key = (link.transaction, link.split)
            if key in seen_reimbursements:
                raise FsaClaimError(
                    "claim.reimbursement.duplicate",
                    ("allocations",),
                    "an FSA reimbursement split can only be allocated once",
                )
            seen_reimbursements.add(key)
            transaction, split = _resolve_link(db, link)
            if split.account != allocation.account:
                raise FsaClaimError(
                    "claim.reimbursement.account.mismatch",
                    ("allocations",),
                    "reimbursement split does not belong to allocation FSA",
                )
            if transaction.post_date > runout:
                raise FsaClaimError(
                    "claim.reimbursement.after_runout",
                    ("allocations",),
                    "reimbursement is after the funding year's run-out window",
                )
            if split.fsa_year_start != year.start:
                assignments.setdefault(transaction.handle, []).append((split.handle, year.start))

    existing = db.get_fsa_claim(claim.handle)

    def persist(active: DbTxn) -> None:
        for transaction_handle, split_assignments in assignments.items():
            transaction = db.get_transaction(transaction_handle)
            if transaction is None:
                raise FsaClaimError(
                    "claim.link.transaction.not_found",
                    ("links",),
                    "linked transaction no longer exists",
                )
            by_handle = {split.handle: split for split in transaction.splits}
            for split_handle, funding_year_start in split_assignments:
                split = by_handle.get(split_handle)
                if split is None:
                    raise FsaClaimError(
                        "claim.link.split.not_found",
                        ("links",),
                        "linked transaction split no longer exists",
                    )
                split.fsa_year_start = funding_year_start
            db.commit_transaction(transaction, active)
        if existing is None:
            db.add_fsa_claim(claim, active)
        else:
            db.commit_fsa_claim(claim, active)

    if txn is None:
        with db.transaction("Save FSA claim") as active:
            persist(active)
    else:
        persist(txn)
    return claim


def attach_transaction_to_claim(
    db: DbSQLite,
    claim_handle: str,
    transaction_handle: str,
    *,
    role: str,
    split_handle: str | None = None,
    funding_year_start: date | None = None,
    txn: DbTxn | None = None,
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
            (
                role == "payment"
                and account.account_class is AccountClass.EXPENSE
                and split.value > 0
            )
            or (
                role == "refund"
                and account.account_class is AccountClass.EXPENSE
                and split.value < 0
            )
            or (role == "reimbursement" and account.atype is AccountType.FSA and split.value < 0)
        )
        if eligible and (split_handle is None or split.handle == split_handle):
            candidates.append((split, account))
    if len(candidates) != 1:
        raise FsaClaimError(
            "claim.attachment.split.ineligible",
            ("split",),
            "choose exactly one eligible transaction split",
        )
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
                year for year in eligible_years if year.start <= claim.service_date <= year.through
            ]
            if len(service_years) == 1:
                eligible_years = service_years
        if len(eligible_years) != 1:
            raise FsaClaimError(
                "claim.attachment.funding_year.required",
                ("funding_year",),
                "choose an FSA funding year for this reimbursement",
            )
        year = eligible_years[0]
        allocation = next(
            (
                item
                for item in claim.allocations
                if item.account == account.handle and item.funding_year_start == year.start
            ),
            None,
        )
        if allocation is None:
            allocation = FsaClaimAllocation(account.handle, year.start)
            claim.allocations.append(allocation)
        if link not in allocation.reimbursements:
            allocation.reimbursements.append(link)
    else:
        raise FsaClaimError(
            "claim.attachment.role.invalid",
            ("role",),
            "unknown FSA claim attachment role",
        )

    return save_claim(db, claim, txn=txn)


def delete_claim(db: DbSQLite, handle: str) -> None:
    if db.get_fsa_claim(handle) is None:
        raise KeyError(handle)
    with db.transaction("Delete FSA claim") as txn:
        db.remove_fsa_claim(handle, txn)


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
