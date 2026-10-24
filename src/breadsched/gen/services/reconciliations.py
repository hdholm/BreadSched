"""Typed statement-reconciliation contracts: the workflow's read and its writes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TypeVar

from ..db.sqlite import DbSQLite
from ..engine import reconciliation
from ..lib.amount import Amount
from ..lib.money import Money
from ..lib.reconciliation import Reconciliation
from ..lib.transaction import ReconcileState
from .claims import claim_link_proposals
from .contracts import ServiceError, ServiceResult
from .receivables import reimbursement_proposals
from .transactions import (
    SavedTransaction,
    SaveTransaction,
    TransactionInput,
    TransactionSplitInput,
    build_transaction,
    transaction_currency,
)


@dataclass(frozen=True, slots=True)
class StartReconciliation:
    account: str
    statement_date: date
    ending_balance: Money


@dataclass(frozen=True, slots=True)
class UpdateReconciliation:
    handle: str
    selected_splits: tuple[str, ...] | None = None
    ending_balance: Money | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationAction:
    handle: str


T = TypeVar("T")


def _result(call: Callable[[], T]) -> ServiceResult[T]:
    try:
        return ServiceResult.success(call())
    except reconciliation.ReconciliationError as exc:
        return ServiceResult.failure(ServiceError(exc.code, exc.fields))
    except KeyError:
        return ServiceResult.failure(ServiceError("reconciliation.not_found", ("handle",)))


def start_reconciliation(
    db: DbSQLite,
    request: StartReconciliation,
) -> ServiceResult[reconciliation.ReconciliationSummary]:
    """Start a statement session and return its calculated initial state."""
    if db.get_account(request.account) is None:
        return ServiceResult.failure(ServiceError("reconciliation.account.not_found", ("account",)))

    def start() -> reconciliation.ReconciliationSummary:
        session = reconciliation.start(
            db,
            request.account,
            request.statement_date,
            request.ending_balance,
        )
        return reconciliation.summary(db, session)

    return _result(start)


def update_reconciliation(
    db: DbSQLite,
    request: UpdateReconciliation,
) -> ServiceResult[reconciliation.ReconciliationSummary]:
    """Replace selected splits and/or the exact ending balance atomically."""
    return _result(
        lambda: reconciliation.update(
            db,
            request.handle,
            split_handles=(
                list(request.selected_splits) if request.selected_splits is not None else None
            ),
            ending_balance=request.ending_balance,
        )
    )


def complete_reconciliation(
    db: DbSQLite,
    request: ReconciliationAction,
) -> ServiceResult[Reconciliation]:
    return _result(lambda: reconciliation.complete(db, request.handle))


def cancel_reconciliation(
    db: DbSQLite,
    request: ReconciliationAction,
) -> ServiceResult[Reconciliation]:
    return _result(lambda: reconciliation.cancel(db, request.handle))


def reopen_reconciliation(
    db: DbSQLite,
    request: ReconciliationAction,
) -> ServiceResult[Reconciliation]:
    return _result(lambda: reconciliation.reopen(db, request.handle))


@dataclass(frozen=True, slots=True)
class ToggleCleared:
    transaction: str
    split: str


def toggle_cleared(db: DbSQLite, request: ToggleCleared) -> ServiceResult[ReconcileState]:
    """Cycle one split between not cleared and cleared; reconciled entries refuse."""
    if db.get_transaction(request.transaction) is None:
        return ServiceResult.failure(ServiceError("transaction.not_found", ("transaction",)))
    return _result(lambda: reconciliation.toggle_cleared(db, request.transaction, request.split))


@dataclass(frozen=True, slots=True)
class ReconciliationView:
    """One account's statement workflow: the open session and the history."""

    account: str
    account_name: str
    #: The open session's calculated state, or None when none is open.
    current: reconciliation.ReconciliationSummary | None
    #: Every session for the account, newest first.
    history: tuple[Reconciliation, ...]
    #: Waiting proposals a deposit or an FSA statement line on this account may answer.
    reimbursement_proposals: int
    claim_link_proposals: int


def reconciliation_view(db: DbSQLite, account: str) -> ServiceResult[ReconciliationView]:
    found = db.get_account(account)
    if found is None:
        return ServiceResult.failure(ServiceError("reconciliation.account.not_found", ("account",)))
    current = reconciliation.open_for_account(db, account)
    return ServiceResult.success(
        ReconciliationView(
            account,
            db.full_name(found),
            reconciliation.summary(db, current) if current is not None else None,
            tuple(reversed(list(db.iter_reconciliations(account)))),
            len(reimbursement_proposals(db, account=account).value or ()),
            len(claim_link_proposals(db, account=account).value or ()),
        )
    )


def reconciled_balance(db: DbSQLite, account: str, through: date) -> ServiceResult[Money]:
    """The account's reconciled balance on a statement date (#362)."""
    if db.get_account(account) is None:
        return ServiceResult.failure(ServiceError("reconciliation.account.not_found", ("account",)))
    return ServiceResult.success(reconciliation.reconciled_balance(db, account, through))


@dataclass(frozen=True, slots=True)
class AddStatementEntry:
    """A charge or credit found on the statement but not yet in the book (#362).

    ``amount`` is in the account's own terms: positive raises its balance, as
    interest charged raises what a card owes or interest earned raises savings.
    """

    account: str
    other_account: str
    post_date: date
    description: str
    amount: Money


def add_statement_entry(
    db: DbSQLite, request: AddStatementEntry
) -> ServiceResult[SavedTransaction]:
    """Add a statement line, such as interest, already cleared and checked.

    The transaction, its cleared mark, and its place in the account's open
    statement are one change, so a single Undo removes the entry.
    """
    account = db.get_account(request.account)
    if account is None:
        return ServiceResult.failure(ServiceError("reconciliation.account.not_found", ("account",)))
    if not request.amount:
        return ServiceResult.failure(ServiceError("reconciliation.entry.amount", ("amount",)))
    currency = transaction_currency(db)
    value = request.amount * account.sign()
    built = build_transaction(
        db,
        SaveTransaction(
            TransactionInput(
                post_date=request.post_date,
                description=request.description,
                splits=(
                    TransactionSplitInput(request.account, Amount(value, currency)),
                    TransactionSplitInput(request.other_account, Amount(-value, currency)),
                ),
                currency=currency,
            )
        ),
    )
    if built.value is None:
        return ServiceResult.failure(*built.errors)
    transaction = built.value
    own = next(split for split in transaction.splits if split.account == request.account)
    own.reconcile = ReconcileState.CLEARED
    try:
        session = reconciliation.open_for_account(db, request.account)
    except reconciliation.ReconciliationError as exc:
        return ServiceResult.failure(ServiceError(exc.code, exc.fields))
    if session is not None and request.post_date <= session.statement_date:
        session.selected_splits = [*session.selected_splits, own.handle]
    with db.transaction(f"Add {transaction.description}") as txn:
        db.add_transaction(transaction, txn)
        if session is not None and own.handle in session.selected_splits:
            db.commit_reconciliation(session, txn)
    return ServiceResult.success(
        SavedTransaction(transaction.handle, transaction.post_date, transaction.description)
    )
