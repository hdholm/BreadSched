"""Shared account-statement reconciliation workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from ..db.sqlite import DbSQLite
from ..lib.account import AccountClass
from ..lib.money import Money
from ..lib.reconciliation import Reconciliation, ReconciliationEvent, ReconciliationStatus
from ..lib.transaction import ReconcileState, Split, Transaction

__all__ = [
    "ReconciliationCandidate",
    "ReconciliationSummary",
    "ReconciliationError",
    "cancel",
    "complete",
    "open_for_account",
    "reopen",
    "set_selection",
    "start",
    "summary",
    "update",
    "update_ending_balance",
]


class ReconciliationError(ValueError):
    """A stable reconciliation failure independent of presentation wording."""

    def __init__(self, code: str, fields: tuple[str, ...], message: str) -> None:
        super().__init__(message)
        self.code = code
        self.fields = fields


@dataclass(frozen=True, slots=True)
class ReconciliationCandidate:
    """One account split eligible for the current statement."""

    transaction: str
    split: str
    post_date: date
    description: str
    amount: Money
    state: ReconcileState
    selected: bool


@dataclass(frozen=True, slots=True)
class ReconciliationSummary:
    """Calculated state of a persisted reconciliation session."""

    reconciliation: Reconciliation
    opening_balance: Money
    selected_balance: Money
    difference: Money
    candidates: tuple[ReconciliationCandidate, ...]

    @property
    def balanced(self) -> bool:
        return not self.difference


def open_for_account(db: DbSQLite, account: str) -> Reconciliation | None:
    """Return the sole open session for an account, if any."""
    sessions = [
        item
        for item in db.iter_reconciliations(account)
        if item.status is ReconciliationStatus.OPEN
    ]
    if len(sessions) > 1:
        raise ReconciliationError(
            "reconciliation.open.multiple",
            ("account",),
            "account has more than one open reconciliation",
        )
    return sessions[0] if sessions else None


def _account_split_rows(
    db: DbSQLite, account_handle: str, through: date
) -> list[tuple[Transaction, Split]]:
    rows: list[tuple[Transaction, Split]] = []
    for transaction in db.iter_transactions(account=account_handle, end=through):
        rows.extend(
            (transaction, split) for split in transaction.splits if split.account == account_handle
        )
    return rows


def _candidate_rows(
    db: DbSQLite,
    reconciliation: Reconciliation,
) -> tuple[Money, tuple[ReconciliationCandidate, ...]]:
    account = db.get_account(reconciliation.account)
    if account is None:
        raise KeyError(reconciliation.account)
    opening = Money(0)
    selected = set(reconciliation.selected_splits)
    candidates: list[ReconciliationCandidate] = []
    for transaction, split in _account_split_rows(
        db, reconciliation.account, reconciliation.statement_date
    ):
        amount = split.value * account.sign()
        if split.reconcile in {ReconcileState.RECONCILED, ReconcileState.FROZEN}:
            opening += amount
            continue
        if split.reconcile is ReconcileState.VOID:
            continue
        candidates.append(
            ReconciliationCandidate(
                transaction=transaction.handle,
                split=split.handle,
                post_date=transaction.post_date,
                description=transaction.description,
                amount=amount,
                state=split.reconcile,
                selected=split.handle in selected,
            )
        )
    candidates.sort(key=lambda item: (item.post_date, item.transaction, item.split))
    return opening, tuple(candidates)


def summary(db: DbSQLite, reconciliation: Reconciliation | str) -> ReconciliationSummary:
    """Calculate opening, selected, and difference without mutating the book."""
    if isinstance(reconciliation, str):
        stored = db.get_reconciliation(reconciliation)
        if stored is None:
            raise KeyError(reconciliation)
        reconciliation = stored
    opening, candidates = _candidate_rows(db, reconciliation)
    selected_balance = opening + sum(
        (item.amount for item in candidates if item.selected), Money(0)
    )
    return ReconciliationSummary(
        reconciliation=reconciliation,
        opening_balance=opening,
        selected_balance=selected_balance,
        difference=reconciliation.ending_balance - selected_balance,
        candidates=candidates,
    )


def start(
    db: DbSQLite,
    account: str,
    statement_date: date,
    ending_balance: Money | str | int,
) -> Reconciliation:
    """Persist a new open statement, selecting already-cleared splits by default."""
    account_obj = db.get_account(account)
    if account_obj is None:
        raise KeyError(account)
    if account_obj.placeholder or account_obj.account_class not in {
        AccountClass.ASSET,
        AccountClass.LIABILITY,
    }:
        raise ReconciliationError(
            "reconciliation.account.ineligible",
            ("account",),
            "reconciliation requires an asset or liability posting account",
        )
    if open_for_account(db, account) is not None:
        raise ReconciliationError(
            "reconciliation.open.exists",
            ("account",),
            "finish or cancel the open reconciliation first",
        )
    completed = [
        item
        for item in db.iter_reconciliations(account)
        if item.status is ReconciliationStatus.COMPLETED
    ]
    if completed and statement_date <= max(item.statement_date for item in completed):
        raise ReconciliationError(
            "reconciliation.statement_date.not_after_completed",
            ("statement_date",),
            "new statement date must follow the latest completed statement",
        )

    reconciliation = Reconciliation(
        account=account,
        statement_date=statement_date,
        ending_balance=ending_balance,
    )
    _opening, candidates = _candidate_rows(db, reconciliation)
    reconciliation.selected_splits = [
        item.split for item in candidates if item.state is ReconcileState.CLEARED
    ]
    with db.transaction(f"Start reconciliation for {account_obj.name}") as txn:
        db.add_reconciliation(reconciliation, txn)
    return reconciliation


def set_selection(
    db: DbSQLite,
    handle: str,
    split_handles: list[str],
) -> ReconciliationSummary:
    """Replace the checked candidate set on an open statement."""
    return update(db, handle, split_handles=split_handles)


def update_ending_balance(
    db: DbSQLite,
    handle: str,
    ending_balance: Money | str | int,
) -> ReconciliationSummary:
    """Correct the target balance while retaining the reviewed split set."""
    return update(db, handle, ending_balance=ending_balance)


def update(
    db: DbSQLite,
    handle: str,
    *,
    split_handles: list[str] | None = None,
    ending_balance: Money | str | int | None = None,
) -> ReconciliationSummary:
    """Atomically update either or both editable parts of an open statement."""
    reconciliation = _require_open(db, handle)
    if split_handles is not None:
        candidate_handles = {item.split for item in summary(db, reconciliation).candidates}
        chosen = list(dict.fromkeys(split_handles))
        unknown = set(chosen) - candidate_handles
        if unknown:
            raise ReconciliationError(
                "reconciliation.split.ineligible",
                ("selected_splits",),
                f"split is not eligible for this statement: {sorted(unknown)[0]}",
            )
        reconciliation.selected_splits = chosen
    if ending_balance is not None:
        reconciliation.ending_balance = (
            ending_balance if isinstance(ending_balance, Money) else Money(ending_balance)
        )
    with db.transaction("Update reconciliation") as txn:
        db.commit_reconciliation(reconciliation, txn)
    return summary(db, reconciliation)


def complete(db: DbSQLite, handle: str) -> Reconciliation:
    """Mark the selected splits reconciled when the statement difference is zero."""
    reconciliation = _require_open(db, handle)
    state = summary(db, reconciliation)
    if not state.balanced:
        raise ReconciliationError(
            "reconciliation.unbalanced",
            ("selected_splits", "ending_balance"),
            f"reconciliation is out of balance by {state.difference}",
        )
    selected = set(reconciliation.selected_splits)
    transactions: dict[str, Transaction] = {}
    for candidate in state.candidates:
        if candidate.split not in selected:
            continue
        transaction = transactions.get(candidate.transaction)
        if transaction is None:
            transaction = db.get_transaction(candidate.transaction)
            if transaction is None:  # pragma: no cover - guarded by summary
                raise KeyError(candidate.transaction)
            transactions[candidate.transaction] = transaction
        split = next(item for item in transaction.splits if item.handle == candidate.split)
        split.reconcile = ReconcileState.RECONCILED
        split.reconcile_date = reconciliation.statement_date

    reconciliation.status = ReconciliationStatus.COMPLETED
    reconciliation.completed_at = datetime.now(timezone.utc)
    reconciliation.cancelled_at = None
    reconciliation.audit_events.append(
        ReconciliationEvent("completed", reconciliation.completed_at)
    )
    account = db.get_account(reconciliation.account)
    name = account.name if account is not None else reconciliation.account
    with db.transaction(f"Complete reconciliation for {name}") as txn:
        for transaction in transactions.values():
            db.commit_transaction(transaction, txn)
        db.commit_reconciliation(reconciliation, txn)
    return reconciliation


def cancel(db: DbSQLite, handle: str) -> Reconciliation:
    """Close an unfinished session without changing any ledger split."""
    reconciliation = _require_open(db, handle)
    reconciliation.status = ReconciliationStatus.CANCELLED
    reconciliation.cancelled_at = datetime.now(timezone.utc)
    reconciliation.audit_events.append(
        ReconciliationEvent("cancelled", reconciliation.cancelled_at)
    )
    with db.transaction("Cancel reconciliation") as txn:
        db.commit_reconciliation(reconciliation, txn)
    return reconciliation


def reopen(db: DbSQLite, handle: str) -> Reconciliation:
    """Reopen the latest completed statement and return its splits to Cleared."""
    reconciliation = db.get_reconciliation(handle)
    if reconciliation is None:
        raise KeyError(handle)
    if reconciliation.status is not ReconciliationStatus.COMPLETED:
        raise ReconciliationError(
            "reconciliation.status.not_completed",
            ("handle",),
            "only a completed reconciliation can be reopened",
        )
    if open_for_account(db, reconciliation.account) is not None:
        raise ReconciliationError(
            "reconciliation.open.exists",
            ("handle",),
            "finish or cancel the open reconciliation first",
        )
    later = [
        item
        for item in db.iter_reconciliations(reconciliation.account)
        if item.status is ReconciliationStatus.COMPLETED
        and item.statement_date > reconciliation.statement_date
    ]
    if later:
        raise ReconciliationError(
            "reconciliation.later_completed.exists",
            ("handle",),
            "reopen later statements first",
        )

    selected = set(reconciliation.selected_splits)
    transactions: dict[str, Transaction] = {}
    remaining = set(selected)
    for transaction in db.iter_transactions(account=reconciliation.account):
        for split in transaction.splits:
            if split.handle not in selected:
                continue
            if (
                split.reconcile is not ReconcileState.RECONCILED
                or split.reconcile_date != reconciliation.statement_date
            ):
                raise ReconciliationError(
                    "reconciliation.split.changed",
                    ("handle",),
                    "a reconciled split changed after this statement completed",
                )
            split.reconcile = ReconcileState.CLEARED
            split.reconcile_date = None
            transactions[transaction.handle] = transaction
            remaining.discard(split.handle)
    if remaining:
        raise ReconciliationError(
            "reconciliation.split.missing",
            ("handle",),
            f"reconciled split is missing: {sorted(remaining)[0]}",
        )

    reconciliation.status = ReconciliationStatus.OPEN
    reconciliation.completed_at = None
    reconciliation.cancelled_at = None
    reconciliation.audit_events.append(ReconciliationEvent("reopened", datetime.now(timezone.utc)))
    with db.transaction("Reopen reconciliation") as txn:
        for transaction in transactions.values():
            db.commit_transaction(transaction, txn)
        db.commit_reconciliation(reconciliation, txn)
    return reconciliation


def _require_open(db: DbSQLite, handle: str) -> Reconciliation:
    reconciliation = db.get_reconciliation(handle)
    if reconciliation is None:
        raise KeyError(handle)
    if reconciliation.status is not ReconciliationStatus.OPEN:
        raise ReconciliationError(
            "reconciliation.status.not_open",
            ("handle",),
            "reconciliation is not open",
        )
    return reconciliation
