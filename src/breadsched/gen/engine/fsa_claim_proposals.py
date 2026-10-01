"""Claim links proposed for FSA transactions that arrive from outside.

An FSA administrator's statement, imported or reconciled, brings FSA movements the
household did not enter by hand: payments from the FSA card, reimbursements paid
out to a bank account, and provider refunds credited back to the card. Each one
belongs on a claim. This proposes the link when the evidence singles one claim out,
as ``receivables.propose_reimbursements`` does for receivables, and writes nothing.

A movement is proposed only when all of these hold:

- its FSA split is not already linked to any claim;
- its flow (:mod:`fsa_flows`) is a direct payment, a reimbursement, or a provider
  refund (funding, repayments, and transfers need no claim);
- exactly one claim suggests it in the role that flow takes, with a matching
  amount (:func:`fsa_claims.suggest_claims_for_transaction`);
- that claim has not already been proposed for an earlier movement in this batch.

Anything else is left to Review, which explains it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..lib.account import AccountType
from ..lib.money import Money
from . import fsa_flows
from .fsa_claims import iter_claims, suggest_claims_for_transaction
from .fsa_flows import FsaFlowKind

__all__ = ["FsaClaimProposal", "propose_claim_links"]

#: The claim role each flow that needs a claim takes.
_ROLE = {
    FsaFlowKind.DIRECT_PAYMENT: "direct_payment",
    FsaFlowKind.REIMBURSEMENT: "reimbursement",
    FsaFlowKind.PROVIDER_REFUND: "direct_refund",
}


@dataclass(frozen=True, slots=True)
class FsaClaimProposal:
    """An unlinked FSA movement that clearly belongs on one claim."""

    claim: str
    claim_label: str
    transaction: str
    #: The FSA side of the movement (the split the role is attached through).
    split: str
    role: str
    when: date
    description: str
    amount: Money
    reason: str


def _linked_splits(db: DbSQLite) -> set[tuple[str, str]]:
    linked: set[tuple[str, str]] = set()
    for claim in iter_claims(db):
        links = [*claim.payments, *claim.refunds]
        for allocation in claim.allocations:
            links.extend(allocation.reimbursements)
            links.extend(allocation.repayments)
        linked.update((link.transaction, link.split) for link in links)
    return linked


def _claim_label(claim) -> str:
    name = claim.provider or claim.description or "FSA claim"
    return f"{claim.service_date.isoformat()} {name}"


def propose_claim_links(db: DbSQLite, *, account: str | None = None) -> list[FsaClaimProposal]:
    """Proposed claim links for unlinked FSA movements, oldest first.

    ``account`` keeps only movements touching that account, such as the FSA or
    bank account being reconciled.
    """
    accounts = {item.handle: item for item in db.iter_accounts()}
    if not any(item.atype is AccountType.FSA for item in accounts.values()):
        return []
    linked = _linked_splits(db)
    movements = []
    for transaction in db.iter_transactions():
        if account is not None and not any(s.account == account for s in transaction.splits):
            continue
        for split in transaction.splits:
            owner = accounts.get(split.account)
            if owner is None or owner.atype is not AccountType.FSA:
                continue
            if (transaction.handle, split.handle) in linked:
                continue
            kind = fsa_flows.classify(transaction, split, accounts)
            if kind in _ROLE:
                movements.append((transaction, split, _ROLE[kind]))
    movements.sort(key=lambda item: (item[0].post_date, item[0].handle, item[1].handle))
    proposals: list[FsaClaimProposal] = []
    taken: set[str] = set()
    for transaction, split, role in movements:
        fits = [
            suggestion
            for suggestion in suggest_claims_for_transaction(db, transaction)
            if suggestion.role == role
            and suggestion.split_handle == split.handle
            and "amount match" in suggestion.reason
            and suggestion.claim.handle not in taken
        ]
        if len(fits) != 1:
            continue
        chosen = fits[0]
        taken.add(chosen.claim.handle)
        proposals.append(
            FsaClaimProposal(
                claim=chosen.claim.handle,
                claim_label=_claim_label(chosen.claim),
                transaction=transaction.handle,
                split=split.handle,
                role=role,
                when=transaction.post_date,
                description=transaction.description,
                amount=abs(split.value),
                reason=chosen.reason,
            )
        )
    return proposals
