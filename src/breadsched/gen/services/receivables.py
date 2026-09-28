"""Create, link, and resolve reimbursable expenses for CLI, GTK, and web.

Every write is one undoable database transaction. A receivable never rewrites
or removes the expense splits it references, and recording a dispute or a
write-off never posts anything to the ledger by itself -- see
``lib.receivable`` and ``engine.receivables`` for the underlying model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine.receivables import (
    ReceivableStatus,
    ReceivableSummary,
    ReimbursementProposal,
    iter_receivables,
    propose_reimbursements,
    receivable_summary,
)
from ..lib.account import AccountClass
from ..lib.money import Money
from ..lib.receivable import Receivable, ReceivableSplitLink, ReceivableWriteOff
from .contracts import ServiceError, ServiceResult

__all__ = [
    "AcceptedReimbursements",
    "ReceivableCandidate",
    "ReimbursementProposal",
    "RecordWriteOff",
    "ReceivableStatus",
    "ReceivableSummary",
    "SaveReceivable",
    "attach_expense_split",
    "attach_reimbursement_split",
    "clear_dispute",
    "delete_receivable",
    "detach_split",
    "list_receivables",
    "mark_disputed",
    "accept_reimbursements",
    "receivable_candidates",
    "record_write_off",
    "reimbursement_proposals",
    "save_receivable",
]


@dataclass(frozen=True, slots=True)
class SaveReceivable:
    incurred_date: date
    payer: str
    description: str = ""
    expected_amount: Money | None = None
    expected_cash_date: date | None = None
    #: The receivable to update; ``None`` creates one.
    handle: str | None = None


@dataclass(frozen=True, slots=True)
class RecordWriteOff:
    receivable: str
    amount: Money
    written_off_on: date
    reason: str = ""


def save_receivable(db: DbSQLite, request: SaveReceivable) -> ServiceResult[Receivable]:
    """Create a receivable or replace one's payer, description, and estimates."""
    payer = " ".join(request.payer.split())
    if not payer:
        return ServiceResult.failure(ServiceError("receivable.payer.required", ("payer",)))
    existing = db.get_receivable(request.handle) if request.handle is not None else None
    if request.handle is not None and existing is None:
        return ServiceResult.failure(ServiceError("receivable.not_found", ("handle",)))
    if request.expected_amount is not None and request.expected_amount < 0:
        return ServiceResult.failure(
            ServiceError("receivable.expected_amount.negative", ("expected_amount",))
        )
    receivable = existing or Receivable()
    receivable.incurred_date = request.incurred_date
    receivable.payer = payer
    receivable.description = " ".join(request.description.split())
    receivable.expected_amount = request.expected_amount
    receivable.expected_cash_date = request.expected_cash_date
    with db.transaction(f"Save receivable {payer}") as txn:
        if existing is None:
            db.add_receivable(receivable, txn)
        else:
            db.commit_receivable(receivable, txn)
    return ServiceResult.success(receivable)


def _link_split(
    db: DbSQLite, receivable_handle: str, transaction_handle: str, split_handle: str, *, role: str
) -> ServiceResult[Receivable]:
    receivable = db.get_receivable(receivable_handle)
    if receivable is None:
        return ServiceResult.failure(ServiceError("receivable.not_found", ("receivable",)))
    transaction = db.get_transaction(transaction_handle)
    if transaction is None:
        return ServiceResult.failure(
            ServiceError("receivable.transaction.not_found", ("transaction",))
        )
    split = next((item for item in transaction.splits if item.handle == split_handle), None)
    if split is None:
        return ServiceResult.failure(ServiceError("receivable.split.not_found", ("split",)))
    account = db.get_account(split.account)
    if account is None or account.account_class is not AccountClass.EXPENSE:
        return ServiceResult.failure(ServiceError("receivable.split.not_expense", ("split",)))
    if role == "expense" and split.value <= 0:
        return ServiceResult.failure(ServiceError("receivable.split.not_a_cost", ("split",)))
    if role == "reimbursement" and split.value >= 0:
        return ServiceResult.failure(ServiceError("receivable.split.not_a_credit", ("split",)))
    link = ReceivableSplitLink(transaction_handle, split_handle)
    if link in receivable.expenses or link in receivable.reimbursements:
        return ServiceResult.failure(ServiceError("receivable.split.duplicate", ("split",)))
    links = receivable.expenses if role == "expense" else receivable.reimbursements
    links.append(link)
    with db.transaction(f"Link {role} to receivable") as txn:
        db.commit_receivable(receivable, txn)
    return ServiceResult.success(receivable)


def attach_expense_split(
    db: DbSQLite, receivable: str, transaction: str, split: str
) -> ServiceResult[Receivable]:
    """Link the split recording the original out-of-pocket cost."""
    return _link_split(db, receivable, transaction, split, role="expense")


def attach_reimbursement_split(
    db: DbSQLite, receivable: str, transaction: str, split: str
) -> ServiceResult[Receivable]:
    """Link the split crediting money back, like an ordinary refund."""
    return _link_split(db, receivable, transaction, split, role="reimbursement")


def detach_split(
    db: DbSQLite, receivable_handle: str, transaction: str, split: str
) -> ServiceResult[Receivable]:
    """Unlink one split without touching the ledger transaction itself."""
    receivable = db.get_receivable(receivable_handle)
    if receivable is None:
        return ServiceResult.failure(ServiceError("receivable.not_found", ("receivable",)))
    link = ReceivableSplitLink(transaction, split)
    if link in receivable.expenses:
        receivable.expenses.remove(link)
    elif link in receivable.reimbursements:
        receivable.reimbursements.remove(link)
    else:
        return ServiceResult.failure(ServiceError("receivable.link.not_found", ("split",)))
    with db.transaction("Unlink receivable split") as txn:
        db.commit_receivable(receivable, txn)
    return ServiceResult.success(receivable)


def mark_disputed(
    db: DbSQLite, handle: str, disputed_on: date, note: str = ""
) -> ServiceResult[Receivable]:
    """Record that the payer is contesting this receivable."""
    receivable = db.get_receivable(handle)
    if receivable is None:
        return ServiceResult.failure(ServiceError("receivable.not_found", ("handle",)))
    receivable.disputed_on = disputed_on
    receivable.dispute_note = " ".join(note.split())
    with db.transaction("Dispute receivable") as txn:
        db.commit_receivable(receivable, txn)
    return ServiceResult.success(receivable)


def clear_dispute(db: DbSQLite, handle: str) -> ServiceResult[Receivable]:
    """Withdraw a dispute; the receivable falls back to its reimbursed amount."""
    receivable = db.get_receivable(handle)
    if receivable is None:
        return ServiceResult.failure(ServiceError("receivable.not_found", ("handle",)))
    receivable.disputed_on = None
    receivable.dispute_note = ""
    with db.transaction("Clear receivable dispute") as txn:
        db.commit_receivable(receivable, txn)
    return ServiceResult.success(receivable)


def record_write_off(db: DbSQLite, request: RecordWriteOff) -> ServiceResult[Receivable]:
    """Give up on collecting part or all of the remaining balance."""
    receivable = db.get_receivable(request.receivable)
    if receivable is None:
        return ServiceResult.failure(ServiceError("receivable.not_found", ("receivable",)))
    if request.amount <= 0:
        return ServiceResult.failure(
            ServiceError("receivable.write_off.amount_not_positive", ("amount",))
        )
    receivable.write_offs.append(
        ReceivableWriteOff(request.written_off_on, request.amount, " ".join(request.reason.split()))
    )
    with db.transaction("Write off receivable balance") as txn:
        db.commit_receivable(receivable, txn)
    return ServiceResult.success(receivable)


def delete_receivable(db: DbSQLite, handle: str) -> ServiceResult[str]:
    """Delete a receivable; the expense and reimbursement transactions are untouched."""
    receivable = db.get_receivable(handle)
    if receivable is None:
        return ServiceResult.failure(ServiceError("receivable.not_found", ("handle",)))
    with db.transaction(f"Delete receivable {receivable.payer}") as txn:
        db.remove_receivable(handle, txn)
    return ServiceResult.success(handle)


@dataclass(frozen=True, slots=True)
class ReceivableCandidate:
    """An expense-account split that could be linked to a receivable."""

    transaction: str
    split: str
    when: date
    description: str
    account: str
    value: Money


def receivable_candidates(
    db: DbSQLite, receivable: str | None = None, *, limit: int = 300
) -> ServiceResult[tuple[tuple[ReceivableCandidate, ...], tuple[ReceivableCandidate, ...]]]:
    """The most recent linkable splits: (costs, credits), newest first; writes nothing.

    Costs are positive expense-account splits (for an expense link) and credits
    negative ones (for a reimbursement link), the same rules ``attach_*`` checks.
    Splits already linked to ``receivable`` are left out.
    """
    linked: set[tuple[str, str]] = set()
    if receivable is not None:
        existing = db.get_receivable(receivable)
        if existing is None:
            return ServiceResult.failure(ServiceError("receivable.not_found", ("receivable",)))
        linked = {(link.transaction, link.split) for link in existing.expenses} | {
            (link.transaction, link.split) for link in existing.reimbursements
        }
    expense_accounts = {
        account.handle
        for account in db.iter_accounts()
        if account.account_class is AccountClass.EXPENSE
    }
    costs: list[ReceivableCandidate] = []
    credits: list[ReceivableCandidate] = []
    for transaction in db.iter_transactions():
        for split in transaction.splits:
            if split.account not in expense_accounts or not split.value:
                continue
            if (transaction.handle, split.handle) in linked:
                continue
            candidate = ReceivableCandidate(
                transaction.handle,
                split.handle,
                transaction.post_date,
                transaction.description,
                split.account,
                split.value,
            )
            (costs if split.value > 0 else credits).append(candidate)
    newest = sorted(costs, key=lambda item: item.when, reverse=True)[:limit]
    newest_credits = sorted(credits, key=lambda item: item.when, reverse=True)[:limit]
    return ServiceResult.success((tuple(newest), tuple(newest_credits)))


def list_receivables(
    db: DbSQLite, *, as_of: date | None = None
) -> ServiceResult[tuple[ReceivableSummary, ...]]:
    """Every receivable with its current standing; writes nothing."""
    return ServiceResult.success(
        tuple(receivable_summary(db, item, as_of=as_of) for item in iter_receivables(db))
    )


def reimbursement_proposals(
    db: DbSQLite, *, as_of: date | None = None
) -> ServiceResult[tuple[ReimbursementProposal, ...]]:
    """Credits that clearly reimburse one open receivable; writes nothing."""
    return ServiceResult.success(tuple(propose_reimbursements(db, as_of=as_of)))


@dataclass(frozen=True, slots=True)
class AcceptedReimbursements:
    linked: int
    #: Chosen links that are no longer proposed (already linked, or changed).
    unchanged: int


def accept_reimbursements(
    db: DbSQLite, chosen: tuple[tuple[str, str, str], ...]
) -> ServiceResult[AcceptedReimbursements]:
    """Link the chosen (receivable, transaction, split) proposals still on offer.

    Proposals are recomputed first, so a stale choice is skipped rather than
    linked on outdated evidence. Each link uses ``attach_reimbursement_split``,
    whose checks still apply.
    """
    current = {
        (item.receivable, item.transaction, item.split) for item in propose_reimbursements(db)
    }
    linked = 0
    for receivable, transaction, split in chosen:
        if (receivable, transaction, split) not in current:
            continue
        if attach_reimbursement_split(db, receivable, transaction, split).value is not None:
            linked += 1
    return ServiceResult.success(AcceptedReimbursements(linked, len(chosen) - linked))
