"""What one movement of money into or out of an FSA account means.

The custodial ledger of an FSA mixes several flows that must be counted
differently: payroll *funding* never changes benefit availability, a *direct
payment* from the FSA card and a *reimbursement* to a bank account both use the
election, a *provider refund* credited back to the card restores it, and a
*repayment* of an over-reimbursement gives it back to the funding year the claim
tagged. A move between two FSA accounts is none of these.

The medical expense itself is counted where it was incurred (the expense split),
whichever path paid it, so neither the reimbursement nor a later card payment is
ever a second expense.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from enum import Enum

from ..lib.account import Account, AccountClass, AccountType
from ..lib.money import Money
from ..lib.transaction import Split, Transaction

__all__ = ["FsaFlowKind", "classify", "classify_parts"]


class FsaFlowKind(str, Enum):
    FUNDING = "funding"
    DIRECT_PAYMENT = "direct_payment"
    REIMBURSEMENT = "reimbursement"
    PROVIDER_REFUND = "provider_refund"
    REPAYMENT = "repayment"
    TRANSFER = "transfer"

    @property
    def label(self) -> str:
        return {
            FsaFlowKind.FUNDING: "Funding",
            FsaFlowKind.DIRECT_PAYMENT: "Direct payment",
            FsaFlowKind.REIMBURSEMENT: "Reimbursement",
            FsaFlowKind.PROVIDER_REFUND: "Provider refund",
            FsaFlowKind.REPAYMENT: "Repayment",
            FsaFlowKind.TRANSFER: "Transfer between FSA accounts",
        }[self]


def classify_parts(
    amount: Money,
    peers: Iterable[Account | None],
    *,
    fsa_year_start: date | None = None,
) -> FsaFlowKind:
    """Classify one FSA split from its signed amount and the other splits' accounts.

    Positive amounts flow into the FSA: tagged with a funding year they are a
    claim's repayment; from an expense account they are a provider refund;
    otherwise (payroll, a bank contribution) they are funding. Negative amounts
    flow out: to an expense account they are a direct payment; anywhere else
    (usually a bank account) they are a reimbursement. Money that only moves
    between FSA accounts is a transfer.
    """
    others = [peer for peer in peers if peer is not None]
    if not amount or (others and all(peer.atype is AccountType.FSA for peer in others)):
        return FsaFlowKind.TRANSFER
    to_expense = any(peer.account_class is AccountClass.EXPENSE for peer in others)
    if amount > 0:
        if fsa_year_start is not None:
            return FsaFlowKind.REPAYMENT
        from_income = any(peer.account_class is AccountClass.INCOME for peer in others)
        if to_expense and not from_income:
            return FsaFlowKind.PROVIDER_REFUND
        return FsaFlowKind.FUNDING
    if to_expense:
        return FsaFlowKind.DIRECT_PAYMENT
    return FsaFlowKind.REIMBURSEMENT


def classify(
    transaction: Transaction, split: Split, accounts: Mapping[str, Account]
) -> FsaFlowKind:
    """Classify ``split`` (a split on an FSA account) within its transaction."""
    peers = [
        accounts.get(item.account) for item in transaction.splits if item.account != split.account
    ]
    return classify_parts(split.value, peers, fsa_year_start=split.fsa_year_start)
