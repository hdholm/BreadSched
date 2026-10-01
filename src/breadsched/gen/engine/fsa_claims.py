"""FSA healthcare claims linked to ordinary transactions and funding years."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum

from ..db.base import DbTxn
from ..db.sqlite import DbSQLite
from ..lib.account import AccountClass, AccountType, FsaFundingYear
from ..lib.fsa_claim import FsaClaim, FsaClaimAllocation, FsaClaimEvent, FsaClaimSplitLink
from ..lib.money import Money
from ..lib.receivable import Receivable
from ..lib.transaction import Split, Transaction
from . import fsa, receivables, split_links

__all__ = [
    "FsaClaimError",
    "FsaClaimStatus",
    "FsaClaimSuggestion",
    "FsaClaimSummary",
    "SharedCost",
    "allocation_year",
    "attach_transaction_to_claim",
    "claim_summary",
    "claim_year_window",
    "close_claim",
    "delete_claim",
    "iter_claims",
    "reopen_claim",
    "save_claim",
    "shared_costs_for_receivable",
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
            year for year in account.fsa_years if year.start <= service_date <= year.service_through
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
        if account.atype is AccountType.FSA and split.value > 0:
            eligible.append(("repayment", split, account.handle))
    if not eligible:
        return []
    # One transaction on both sides is a paired role (see PAIRED_ROLES).
    for paired, (first, second) in PAIRED_ROLES.items():
        firsts = [item for item in eligible if item[0] == first]
        seconds = [item for item in eligible if item[0] == second]
        if len(firsts) == 1 and len(seconds) == 1:
            eligible.append((paired, seconds[0][1], seconds[0][2]))

    txn_words = _transaction_words(transaction)
    suggestions: list[FsaClaimSuggestion] = []
    for claim in iter_claims(db):
        summary = claim_summary(db, claim)
        over = summary.status is FsaClaimStatus.OVER_REIMBURSED
        if summary.status is FsaClaimStatus.CLOSED:
            continue
        # A fully reimbursed claim takes no more payments or reimbursements, but a
        # provider refund often arrives after the FSA has paid.
        settled = summary.status is FsaClaimStatus.FULLY_REIMBURSED

        best: FsaClaimSuggestion | None = None
        for role, split, reimbursement_account in eligible:
            if settled and role not in {"refund", "direct_refund"}:
                continue
            if role == "refund" and summary.net_paid <= 0:
                continue
            # Only money owed back to the FSA is a repayment.
            if role == "repayment" and not over:
                continue
            if role == "direct_refund" and summary.net_paid <= 0:
                continue
            if role in {"repayment", "direct_refund"}:
                # Money goes back to an FSA the claim was reimbursed from.
                if not any(item.account == reimbursement_account for item in claim.allocations):
                    continue
            elif role in {"reimbursement", "direct_payment"}:
                account = db.get_account(reimbursement_account or "")
                if account is None:
                    continue
                compatible = any(
                    year.start <= claim.service_date <= year.service_through
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
            elif role == "direct_payment":
                target = claim.eob_responsibility or summary.remaining_reimbursable
                role_label = "provider payment from the FSA card"
                score += 25
                reasons.append("paid from the FSA card")
            elif role == "direct_refund":
                target = summary.net_paid
                role_label = "provider refund to the FSA card"
                score += 20
                reasons.append("refunded to the FSA card")
            elif role == "repayment":
                target = summary.over_reimbursed
                role_label = "repayment to the FSA"
                score += 20
                reasons.append("claim over-reimbursed")
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
    OVER_REIMBURSED = "over_reimbursed"
    CLOSED = "closed"

    @property
    def settled(self) -> bool:
        """Nothing more is pursued: fully reimbursed, or closed by the household."""
        return self in {FsaClaimStatus.FULLY_REIMBURSED, FsaClaimStatus.CLOSED}

    @property
    def label(self) -> str:
        return {
            FsaClaimStatus.WAITING_EOB: "Waiting for EOB",
            FsaClaimStatus.OPEN: "Open",
            FsaClaimStatus.PARTIAL: "Partially reimbursed",
            FsaClaimStatus.FULLY_REIMBURSED: "Fully reimbursed",
            FsaClaimStatus.CLOSED_NO_FUNDS: "Closed — no funds",
            FsaClaimStatus.NEEDS_REVIEW: "Needs review",
            FsaClaimStatus.OVER_REIMBURSED: "Over-reimbursed",
            FsaClaimStatus.CLOSED: "Closed",
        }[self]


@dataclass(frozen=True)
class SharedCost:
    """One expense split between a payer's receivable and an FSA claim (issue #192).

    The three shares always add up to ``expense``: ``your_share`` goes negative
    when the payer and the EOB together claim more than was paid, and then
    ``over_allocated`` says by how much and the claim needs review. Nothing is
    refused or rewritten, and no posting depends on this allocation.
    """

    claim: str
    receivable: str
    payer: str
    #: The claim's net paid amount: what the provider was paid, less refunds.
    expense: Money
    #: What the receivable is owed, or what the payer actually paid once that
    #: is more; less any amount written off.
    payer_share: Money
    #: What the FSA is expected to reimburse: zero until an EOB responsibility
    #: is entered, then that responsibility, never more than the remainder.
    fsa_share: Money
    your_share: Money
    waiting_eob: bool
    #: How far the payer share plus the EOB responsibility exceed the expense.
    over_allocated: Money

    @property
    def needs_review(self) -> bool:
        return self.over_allocated > 0


@dataclass(frozen=True)
class FsaClaimSummary:
    claim: FsaClaim
    paid: Money
    refunds: Money
    net_paid: Money
    eob_responsibility: Money | None
    reimbursable: Money
    #: Reimbursed by the FSA, less what was paid back to it.
    reimbursed: Money
    rejected: Money
    remaining_reimbursable: Money
    available_fsa: Money
    status: FsaClaimStatus
    #: The payer/FSA/you allocation when the claim is linked to a receivable.
    shared: SharedCost | None = None
    #: Paid back to the FSA; already taken off ``reimbursed``.
    repaid: Money = Money(0)
    #: How much more the FSA reimbursed than the claim allows, still to repay.
    over_reimbursed: Money = Money(0)
    #: What a closed claim gave up: it was never reimbursed.
    forgone: Money = Money(0)
    #: The correction that reopened this claim while money is still to come:
    #: an EOB raised after reimbursement, or the claim reopened by hand.
    reopened_by: FsaClaimEvent | None = None


def iter_claims(db: DbSQLite) -> list[FsaClaim]:
    return list(db.iter_fsa_claims())


def _missing_link(part: str) -> FsaClaimError:
    return FsaClaimError(
        f"claim.link.{part}.not_found",
        ("links",),
        f"linked transaction{' split' if part == 'split' else ''} no longer exists",
    )


def _resolve_link(db: DbSQLite, link: FsaClaimSplitLink) -> tuple[Transaction, Split]:
    return split_links.resolve_link(db, link, _missing_link)


def allocation_year(db: DbSQLite, allocation: FsaClaimAllocation) -> FsaFundingYear:
    """The FSA funding year a claim allocation draws on."""
    return _allocation_year(db, allocation)


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


def save_claim(
    db: DbSQLite,
    claim: FsaClaim,
    *,
    txn: DbTxn | None = None,
    today: date | None = None,
    eob_note: str = "",
) -> FsaClaim:
    """Persist a claim and linked split classifications as one atomic edit.

    A saved claim keeps its stored closing and history: those change only
    through :func:`close_claim` and :func:`reopen_claim`. Changing an EOB that
    was already entered records the change, dated ``today``, with ``eob_note``.
    """
    seen_reimbursements: set[tuple[str, str]] = set()
    assignments: dict[str, list[tuple[str, date]]] = {}
    if claim.receivable is not None:
        _linked_receivable(db, claim)
        if any(
            other.receivable == claim.receivable and other.handle != claim.handle
            for other in iter_claims(db)
        ):
            raise FsaClaimError(
                "claim.receivable.taken",
                ("receivable",),
                "another FSA claim already covers the rest of this receivable",
            )
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
        for link in allocation.repayments:
            key = (link.transaction, link.split)
            if key in seen_reimbursements:
                raise FsaClaimError(
                    "claim.repayment.duplicate",
                    ("allocations",),
                    "an FSA repayment split can only be allocated once",
                )
            seen_reimbursements.add(key)
            transaction, split = _resolve_link(db, link)
            if split.account != allocation.account:
                raise FsaClaimError(
                    "claim.repayment.account.mismatch",
                    ("allocations",),
                    "repayment split does not belong to allocation FSA",
                )
            if split.value <= 0:
                raise FsaClaimError(
                    "claim.repayment.direction",
                    ("allocations",),
                    "a repayment must pay money into the FSA",
                )
            # Tagging the split with its year keeps it out of payroll funding
            # and gives the year's election back (engine.fsa.year_status).
            if split.fsa_year_start != year.start:
                assignments.setdefault(transaction.handle, []).append((split.handle, year.start))

    existing = db.get_fsa_claim(claim.handle)
    if existing is not None:
        claim.closed_on = existing.closed_on
        claim.close_reason = existing.close_reason
        claim.events = list(existing.events)
        if (
            existing.eob_responsibility is not None
            and claim.eob_responsibility != existing.eob_responsibility
        ):
            claim.events.append(
                FsaClaimEvent(
                    FsaClaimEvent.EOB_CHANGED,
                    today or date.today(),
                    existing.eob_responsibility,
                    claim.eob_responsibility,
                    eob_note.strip(),
                )
            )

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


#: Roles one split can play on a claim, and the two that link a pair of splits
#: from a single transaction: a provider paid straight from the FSA card is both
#: the claim's payment and its reimbursement, and a provider refund credited
#: back to the card is both its refund and its repayment to the FSA.
SINGLE_ROLES = ("payment", "refund", "reimbursement", "repayment")
PAIRED_ROLES = {
    "direct_payment": ("payment", "reimbursement"),
    "direct_refund": ("refund", "repayment"),
}
ATTACHMENT_ROLES = (*SINGLE_ROLES, *PAIRED_ROLES)


@dataclass(frozen=True)
class AttachmentRole:
    """One way a transaction can attach to a claim, for review and entry screens."""

    role: str
    split: str
    account: str
    #: Funding years a reimbursement could draw on (empty: decided by the claim).
    years: tuple[date, ...] = ()


def _role_eligible(role: str, account, split: Split) -> bool:
    expense = account.account_class is AccountClass.EXPENSE
    fsa = account.atype is AccountType.FSA
    return (
        (role == "payment" and expense and split.value > 0)
        or (role == "refund" and expense and split.value < 0)
        or (role == "reimbursement" and fsa and split.value < 0)
        or (role == "repayment" and fsa and split.value > 0)
    )


def attachment_roles(db: DbSQLite, transaction: Transaction) -> list[AttachmentRole]:
    """Every role this transaction's splits can play, paired roles first."""
    single: list[AttachmentRole] = []
    by_role: dict[str, list[AttachmentRole]] = {}
    for split in transaction.splits:
        account = db.get_account(split.account)
        if account is None:
            continue
        for role in SINGLE_ROLES:
            if not _role_eligible(role, account, split):
                continue
            years: tuple[date, ...] = ()
            if role == "reimbursement":
                years = tuple(
                    year.start
                    for year in account.fsa_years
                    if transaction.post_date <= (year.runout_through or year.through)
                )
            option = AttachmentRole(role, split.handle, account.handle, years)
            single.append(option)
            by_role.setdefault(role, []).append(option)
    paired: list[AttachmentRole] = []
    for role, (first, second) in PAIRED_ROLES.items():
        if len(by_role.get(first, ())) == 1 and len(by_role.get(second, ())) == 1:
            fsa_side = by_role[second][0]
            paired.append(AttachmentRole(role, fsa_side.split, fsa_side.account, fsa_side.years))
    return paired + single


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
    """Attach one ledger split (or a paired role's two splits) to an existing claim.

    A paired role links both sides of one transaction in a single save:
    ``direct_payment`` (the expense paid and the FSA money that paid it) and
    ``direct_refund`` (the expense credited and the FSA money it returned).
    ``split_handle`` then names the FSA split, if given.
    """
    claim = next((item for item in iter_claims(db) if item.handle == claim_handle), None)
    if claim is None:
        raise KeyError(claim_handle)
    transaction = db.get_transaction(transaction_handle)
    if transaction is None:
        raise KeyError(transaction_handle)
    if role not in ATTACHMENT_ROLES:
        raise FsaClaimError(
            "claim.attachment.role.invalid",
            ("role",),
            "unknown FSA claim attachment role",
        )
    parts = PAIRED_ROLES.get(role, (role,))
    for part in parts:
        fsa_side = part in {"reimbursement", "repayment"}
        chosen = split_handle if (len(parts) == 1 or fsa_side) else None
        candidates = []
        for split in transaction.splits:
            account = db.get_account(split.account)
            if account is None or not _role_eligible(part, account, split):
                continue
            if chosen is None or split.handle == chosen:
                candidates.append((split, account))
        if len(candidates) != 1:
            raise FsaClaimError(
                "claim.attachment.split.ineligible",
                ("split",),
                "choose exactly one eligible transaction split",
            )
        split, account = candidates[0]
        _link(claim, part, transaction, split, account, funding_year_start)
    return save_claim(db, claim, txn=txn)


def _link(
    claim: FsaClaim,
    role: str,
    transaction: Transaction,
    split: Split,
    account,
    funding_year_start: date | None,
) -> None:
    link = FsaClaimSplitLink(transaction.handle, split.handle)
    if role == "payment":
        if link not in claim.payments:
            claim.payments.append(link)
        return
    if role == "refund":
        if link not in claim.refunds:
            claim.refunds.append(link)
        return
    if role == "repayment":
        # Money goes back to a year the claim was reimbursed from, whenever it
        # is repaid.
        claimed = {
            item.funding_year_start for item in claim.allocations if item.account == account.handle
        }
        eligible_years = [year for year in account.fsa_years if year.start in claimed]
    else:
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
            if year.start <= claim.service_date <= year.service_through
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
    links = allocation.reimbursements if role == "reimbursement" else allocation.repayments
    if link not in links:
        links.append(link)


def close_claim(db: DbSQLite, handle: str, *, on: date, reason: str = "") -> FsaClaim:
    """Stop pursuing what is left to reimburse; the claim keeps its links and history."""
    claim = db.get_fsa_claim(handle)
    if claim is None:
        raise KeyError(handle)
    if claim.closed_on is not None:
        raise FsaClaimError(
            "claim.close.already_closed", ("handle",), "the claim is already closed"
        )
    if on < claim.service_date:
        raise FsaClaimError(
            "claim.close.before_service", ("on",), "a claim cannot close before its service date"
        )
    claim.closed_on = on
    claim.close_reason = reason.strip()
    claim.events.append(FsaClaimEvent(FsaClaimEvent.CLOSED, on, note=claim.close_reason))
    with db.transaction("Close FSA claim") as txn:
        db.commit_fsa_claim(claim, txn)
    return claim


def reopen_claim(db: DbSQLite, handle: str, *, on: date, note: str = "") -> FsaClaim:
    """Pursue a closed claim again, for example after a corrected EOB."""
    claim = db.get_fsa_claim(handle)
    if claim is None:
        raise KeyError(handle)
    if claim.closed_on is None:
        raise FsaClaimError("claim.reopen.not_closed", ("handle",), "the claim is not closed")
    if on < claim.closed_on:
        raise FsaClaimError(
            "claim.reopen.before_close", ("on",), "a claim cannot reopen before it was closed"
        )
    claim.closed_on = None
    claim.close_reason = ""
    claim.events.append(FsaClaimEvent(FsaClaimEvent.REOPENED, on, note=note.strip()))
    with db.transaction("Reopen FSA claim") as txn:
        db.commit_fsa_claim(claim, txn)
    return claim


def delete_claim(db: DbSQLite, handle: str) -> None:
    if db.get_fsa_claim(handle) is None:
        raise KeyError(handle)
    with db.transaction("Delete FSA claim") as txn:
        db.remove_fsa_claim(handle, txn)


def _sum_links(db: DbSQLite, links: list[FsaClaimSplitLink]) -> Money:
    return split_links.sum_links(db, links, _missing_link)


def _linked_receivable(db: DbSQLite, claim: FsaClaim) -> Receivable:
    receivable = db.get_receivable(claim.receivable or "")
    if receivable is None:
        raise FsaClaimError(
            "claim.receivable.not_found",
            ("receivable",),
            "the linked receivable no longer exists",
        )
    return receivable


def _payer_share(db: DbSQLite, receivable: Receivable, when: date) -> Money:
    summary = receivables.receivable_summary(db, receivable, as_of=when)
    return max(summary.owed - summary.written_off, summary.reimbursed, Money(0))


def shared_costs_for_receivable(
    db: DbSQLite, receivable: Receivable, *, as_of: date | None = None
) -> list[SharedCost]:
    """The allocation of each FSA claim that covers the rest of ``receivable``'s expense."""
    found: list[SharedCost] = []
    for claim in iter_claims(db):
        if claim.receivable == receivable.handle:
            shared = claim_summary(db, claim, as_of=as_of).shared
            if shared is not None:
                found.append(shared)
    return found


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
    repaid = Money(0)
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
        repaid = repaid + _sum_links(db, allocation.repayments)
        for rejection in allocation.rejections:
            if rejection.amount < 0:
                raise ValueError("rejected reimbursement amount must not be negative")
            rejected = rejected + rejection.amount
        if allocation.target is not None:
            has_targets = True
            target_total = target_total + allocation.target

    # A payer covering part of this expense leaves the FSA only the remainder,
    # and nothing at all until the EOB says what the patient owes (issue #192).
    payer: Receivable | None = None
    payer_share = over_allocated = Money(0)
    if claim.receivable is not None:
        payer = _linked_receivable(db, claim)
        payer_share = _payer_share(db, payer, when)
        eob_part = Money(0)
        if claim.eob_responsibility is not None:
            eob_part = min(max(net_paid, Money(0)), claim.eob_responsibility)
        over_allocated = max(payer_share + eob_part - net_paid, Money(0))

    # Money paid back to the FSA undoes that much reimbursement.
    reimbursed = reimbursed - repaid
    if net_paid < 0:
        reimbursable = Money(0)
        status = FsaClaimStatus.NEEDS_REVIEW
    elif claim.eob_responsibility is None:
        reimbursable = net_paid if payer is None else Money(0)
        status = FsaClaimStatus.NEEDS_REVIEW if over_allocated > 0 else FsaClaimStatus.WAITING_EOB
    else:
        reimbursable = min(net_paid, claim.eob_responsibility)
        if payer is not None:
            reimbursable = min(reimbursable, max(net_paid - payer_share, Money(0)))
        if over_allocated > 0:
            status = FsaClaimStatus.NEEDS_REVIEW
        elif has_targets and target_total > reimbursable:
            status = FsaClaimStatus.NEEDS_REVIEW
        elif reimbursed > reimbursable:
            status = FsaClaimStatus.OVER_REIMBURSED
        elif reimbursable > 0 and reimbursed >= reimbursable:
            status = FsaClaimStatus.FULLY_REIMBURSED
        elif available <= 0:
            status = FsaClaimStatus.CLOSED_NO_FUNDS
        elif reimbursed > 0:
            status = FsaClaimStatus.PARTIAL
        else:
            status = FsaClaimStatus.OPEN
    over_reimbursed = max(reimbursed - reimbursable, Money(0))
    remaining = max(reimbursable - reimbursed, Money(0))
    forgone = Money(0)
    # A closed claim pursues nothing more; what it still owed the household is
    # given up. Figures that disagree, or money owed back to the FSA, still
    # need the household whether or not the claim is closed.
    if claim.closed_on is not None and status not in {
        FsaClaimStatus.NEEDS_REVIEW,
        FsaClaimStatus.OVER_REIMBURSED,
    }:
        status = FsaClaimStatus.CLOSED
        forgone = remaining
        remaining = Money(0)
    reopened_by = None
    if remaining > 0 and status in {FsaClaimStatus.OPEN, FsaClaimStatus.PARTIAL}:
        reopened_by = _reopening(claim, reimbursed)
    shared = None
    if payer is not None:
        shared = SharedCost(
            claim=claim.handle,
            receivable=payer.handle,
            payer=payer.payer,
            expense=net_paid,
            payer_share=payer_share,
            fsa_share=reimbursable,
            your_share=net_paid - payer_share - reimbursable,
            waiting_eob=claim.eob_responsibility is None,
            over_allocated=over_allocated,
        )
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
        shared=shared,
        repaid=repaid,
        over_reimbursed=over_reimbursed,
        forgone=forgone,
        reopened_by=reopened_by,
    )


def _reopening(claim: FsaClaim, reimbursed: Money) -> FsaClaimEvent | None:
    """The latest correction that reopened the claim, if its money is still to come.

    A claim reopens when it is reopened by hand, or when an EOB is raised after
    the FSA has reimbursed some of it.
    """
    for event in reversed(claim.events):
        if event.kind == FsaClaimEvent.REOPENED:
            return event
        if event.kind == FsaClaimEvent.CLOSED:
            return None
        if event.kind == FsaClaimEvent.EOB_CHANGED:
            raised = (
                event.previous is not None
                and event.current is not None
                and event.current > event.previous
            )
            return event if raised and reimbursed > 0 else None
    return None
