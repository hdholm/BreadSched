"""Create, link, and resolve reimbursable expenses for CLI, GTK, and web.

Every write is one undoable database transaction. A receivable never rewrites
or removes the expense splits it references. Each write also brings the
receivable's BreadSched-owned reclassification transactions up to date in the
same database transaction (issue #170), so what is owed always sits in the
receivable account; a dispute posts nothing. See ``lib.receivable`` and
``engine.receivables`` for the underlying model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.base import DbTxn
from ..db.sqlite import DbSQLite
from ..engine import fsa_claims
from ..engine.currency import reporting_currency_handle
from ..engine.receivables import (
    ReceivableError,
    ReceivableStatus,
    ReceivableSummary,
    ReimbursementProposal,
    iter_receivables,
    owned_postings,
    planned_postings,
    posting_currency,
    propose_reimbursements,
    receivable_summary,
)
from ..lib.account import Account, AccountClass, AccountType
from ..lib.money import Money
from ..lib.receivable import Receivable, ReceivableSplitLink, ReceivableWriteOff
from ..lib.transaction import Transaction
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
    "default_receivable_account",
    "receivable_accounts",
    "sync_all_receivables",
    "sync_linked_receivables",
    "attach_reimbursement_split",
    "is_owned_posting",
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
    "shared_costs",
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
    #: The Receivable account holding what is owed; ``None`` keeps the current
    #: one, or uses the default for the linked splits' currency.
    account: str | None = None


@dataclass(frozen=True, slots=True)
class RecordWriteOff:
    receivable: str
    amount: Money
    written_off_on: date
    reason: str = ""


#: Name of the receivable account BreadSched creates when a book has none.
DEFAULT_ACCOUNT_NAME = "Reimbursements Receivable"


def receivable_accounts(db: DbSQLite) -> tuple[Account, ...]:
    """Every Receivable account a receivable can be held in, by full name."""
    found = [
        account
        for account in db.iter_accounts()
        if account.atype is AccountType.RECEIVABLE and not account.placeholder
    ]
    return tuple(sorted(found, key=lambda account: db.full_name(account) or account.name))


def default_receivable_account(db: DbSQLite, currency: str, txn: DbTxn) -> str:
    """The first visible Receivable account in ``currency``, created when none exists.

    Only BreadSched's own accounts are chosen by default: an imported GnuCash
    receivable (often business invoices) is used only when picked explicitly. A
    created account sits under the top-level Assets account when the book has
    one, else under the root.
    """
    book = reporting_currency_handle(db)
    for account in receivable_accounts(db):
        native = not account.source_guid
        if native and not account.hidden and (account.commodity or book) == currency:
            return account.handle
    root = db.root_account()
    assets = next(
        (
            account
            for account in db.child_accounts(root.handle if root is not None else None)
            if account.account_class is AccountClass.ASSET and account.name == "Assets"
        ),
        None,
    )
    name = DEFAULT_ACCOUNT_NAME
    if currency != book:
        commodity = db.get_commodity(currency)
        name = f"{name} {commodity.mnemonic if commodity is not None else currency}"
    parent = assets or root
    account = Account(
        name=name,
        atype=AccountType.RECEIVABLE,
        parent=parent.handle if parent is not None else None,
        commodity=currency,
        description="Money others owe back on reimbursable expenses",
    )
    db.add_account(account, txn)
    return account.handle


def _same(planned: Transaction, existing: Transaction) -> bool:
    """Whether ``planned`` matches the stored posting, apart from write bookkeeping."""
    planned.enter_date = existing.enter_date
    planned.change = existing.change
    return planned.serialize() == existing.serialize()


def _sync_postings(db: DbSQLite, receivable: Receivable, txn: DbTxn) -> None:
    """Bring the receivable's owned reclassification transactions up to date.

    Raises ``ReceivableError`` (leaving the caller to abort the database
    transaction) when the postings cannot be made.
    """
    if receivable.account is None and receivable.expenses:
        currency = posting_currency(db, receivable)
        if currency is not None:
            receivable.account = default_receivable_account(db, currency, txn)
    planned = planned_postings(db, receivable)
    wanted = {transaction.handle for transaction in planned}
    for handle in receivable.postings:
        if handle not in wanted and db.get_transaction(handle) is not None:
            db.remove_transaction(handle, txn)
    for transaction in planned:
        existing = db.get_transaction(transaction.handle)
        if existing is None:
            db.add_transaction(transaction, txn)
            continue
        if not _same(transaction, existing):
            db.commit_transaction(transaction, txn)
    receivable.postings = [transaction.handle for transaction in planned]


def _write(
    db: DbSQLite, receivable: Receivable, message: str, *, new: bool = False
) -> ServiceResult[Receivable]:
    """Store ``receivable`` and its postings as one undoable change."""
    try:
        with db.transaction(message) as txn:
            _sync_postings(db, receivable, txn)
            if new:
                db.add_receivable(receivable, txn)
            else:
                db.commit_receivable(receivable, txn)
    except ReceivableError as exc:
        return ServiceResult.failure(ServiceError(exc.code, exc.fields))
    return ServiceResult.success(receivable)


def sync_linked_receivables(db: DbSQLite, transaction: str, txn: DbTxn) -> None:
    """Recompute the postings of every receivable linked to ``transaction``.

    Called inside the transaction service's write when a linked expense or
    reimbursement is edited, so the receivable account follows the ledger.
    """
    for receivable in db.iter_receivables():
        links = (*receivable.expenses, *receivable.reimbursements)
        if any(link.transaction == transaction for link in links):
            _sync_postings(db, receivable, txn)
            db.commit_receivable(receivable, txn)


def _stale(db: DbSQLite, receivable: Receivable) -> bool:
    """Whether the receivable's stored postings differ from what it should own."""
    try:
        planned = planned_postings(db, receivable)
    except ReceivableError:
        return False  # reported by its summary; nothing is guessed around it
    if [transaction.handle for transaction in planned] != receivable.postings:
        return True
    for transaction in planned:
        existing = db.get_transaction(transaction.handle)
        if existing is None:
            return True
        if not _same(transaction, existing):
            return True
    return False


def sync_all_receivables(db: DbSQLite) -> int:
    """Bring every stale receivable's postings up to date; the number updated.

    Run after an import, which can change a linked expense without going through
    the transaction service. Writes nothing (and adds no undo step) when every
    receivable is already current.
    """
    stale = [receivable for receivable in db.iter_receivables() if _stale(db, receivable)]
    if not stale:
        return 0
    with db.transaction("Update reimbursable expense reclassifications") as txn:
        for receivable in stale:
            _sync_postings(db, receivable, txn)
            db.commit_receivable(receivable, txn)
    return len(stale)


def is_owned_posting(db: DbSQLite, transaction: str) -> bool:
    """Whether ``transaction`` is a reclassification a receivable owns."""
    return transaction in owned_postings(db)


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
    if request.account is not None:
        account = db.get_account(request.account)
        if account is None:
            return ServiceResult.failure(ServiceError("receivable.account.not_found", ("account",)))
        if account.atype is not AccountType.RECEIVABLE:
            return ServiceResult.failure(
                ServiceError("receivable.account.not_receivable", ("account",))
            )
    receivable = existing or Receivable()
    receivable.incurred_date = request.incurred_date
    receivable.payer = payer
    receivable.description = " ".join(request.description.split())
    receivable.expected_amount = request.expected_amount
    receivable.expected_cash_date = request.expected_cash_date
    if request.account is not None:
        receivable.account = request.account
    return _write(db, receivable, f"Save receivable {payer}", new=existing is None)


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
    if transaction_handle in owned_postings(db):
        return ServiceResult.failure(ServiceError("receivable.split.owned_posting", ("split",)))
    link = ReceivableSplitLink(transaction_handle, split_handle)
    if link in receivable.expenses or link in receivable.reimbursements:
        return ServiceResult.failure(ServiceError("receivable.split.duplicate", ("split",)))
    links = receivable.expenses if role == "expense" else receivable.reimbursements
    links.append(link)
    return _write(db, receivable, f"Link {role} to receivable")


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
    return _write(db, receivable, "Unlink receivable split")


def mark_disputed(
    db: DbSQLite, handle: str, disputed_on: date, note: str = ""
) -> ServiceResult[Receivable]:
    """Record that the payer is contesting this receivable."""
    receivable = db.get_receivable(handle)
    if receivable is None:
        return ServiceResult.failure(ServiceError("receivable.not_found", ("handle",)))
    receivable.disputed_on = disputed_on
    receivable.dispute_note = " ".join(note.split())
    return _write(db, receivable, "Dispute receivable")


def clear_dispute(db: DbSQLite, handle: str) -> ServiceResult[Receivable]:
    """Withdraw a dispute; the receivable falls back to its reimbursed amount."""
    receivable = db.get_receivable(handle)
    if receivable is None:
        return ServiceResult.failure(ServiceError("receivable.not_found", ("handle",)))
    receivable.disputed_on = None
    receivable.dispute_note = ""
    return _write(db, receivable, "Clear receivable dispute")


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
    return _write(db, receivable, "Write off receivable balance")


def delete_receivable(db: DbSQLite, handle: str) -> ServiceResult[str]:
    """Delete a receivable and its reclassifications; linked transactions are untouched.

    An FSA claim covering the rest of its expense is unlinked in the same edit,
    so the claim again expects the whole EOB responsibility (issue #192).
    """
    receivable = db.get_receivable(handle)
    if receivable is None:
        return ServiceResult.failure(ServiceError("receivable.not_found", ("handle",)))
    with db.transaction(f"Delete receivable {receivable.payer}") as txn:
        for claim in list(db.iter_fsa_claims()):
            if claim.receivable == handle:
                claim.receivable = None
                db.commit_fsa_claim(claim, txn)
        for posting in receivable.postings:
            if db.get_transaction(posting) is not None:
                db.remove_transaction(posting, txn)
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
    owned = owned_postings(db)
    costs: list[ReceivableCandidate] = []
    credits: list[ReceivableCandidate] = []
    for transaction in db.iter_transactions():
        if transaction.handle in owned:
            continue
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


def shared_costs(
    db: DbSQLite, handle: str, *, as_of: date | None = None
) -> ServiceResult[tuple[fsa_claims.SharedCost, ...]]:
    """How each FSA claim covering the rest of this receivable's expense allocates it."""
    receivable = db.get_receivable(handle)
    if receivable is None:
        return ServiceResult.failure(ServiceError("receivable.not_found", ("handle",)))
    try:
        found = fsa_claims.shared_costs_for_receivable(db, receivable, as_of=as_of)
    except fsa_claims.FsaClaimError as exc:
        return ServiceResult.failure(ServiceError(exc.code, exc.fields))
    return ServiceResult.success(tuple(found))


def reimbursement_proposals(
    db: DbSQLite, *, as_of: date | None = None, account: str | None = None
) -> ServiceResult[tuple[ReimbursementProposal, ...]]:
    """Credits that clearly reimburse one open receivable; writes nothing.

    ``account`` keeps only proposals whose transaction touches that account, such
    as the bank account being reconciled.
    """
    proposals = propose_reimbursements(db, as_of=as_of)
    if account is not None:
        touching = set()
        for item in proposals:
            transaction = db.get_transaction(item.transaction)
            if transaction is not None and any(s.account == account for s in transaction.splits):
                touching.add(item.transaction)
        proposals = [item for item in proposals if item.transaction in touching]
    return ServiceResult.success(tuple(proposals))


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
