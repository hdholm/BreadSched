"""Shared account-statement reconciliation workflow."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from ..db.sqlite import DbSQLite
from ..lib.account import AccountClass
from ..lib.money import Money
from ..lib.reconciliation import Reconciliation, ReconciliationEvent, ReconciliationStatus
from ..lib.transaction import ReconcileState, Split, Transaction

__all__ = [
    "CANDIDATE_SORTS",
    "ReconciliationCandidate",
    "ReconciliationSummary",
    "ReconciliationError",
    "cancel",
    "complete",
    "open_for_account",
    "reconciled_balance",
    "reopen",
    "set_selection",
    "sort_candidates",
    "start",
    "summary",
    "toggle_cleared",
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
    #: Dated after the statement: listed in case its date is wrong (#362), never
    #: checked by default, but reconciled with the statement if checked.
    after_statement: bool = False

    #: A debit: money into an asset, or a payment on a liability. GnuCash's
    #: reconcile window lists these as Funds In and the credits as Funds Out.
    funds_in: bool = False
    #: The transaction's number (a check number, say): sometimes all a statement
    #: gives to tell its entries apart.
    num: str = ""


@dataclass(frozen=True, slots=True)
class ReconciliationSummary:
    """Calculated state of a persisted reconciliation session."""

    reconciliation: Reconciliation
    opening_balance: Money
    selected_balance: Money
    difference: Money
    candidates: tuple[ReconciliationCandidate, ...]
    #: The checked Funds In and Funds Out, each as a positive total (#362).
    selected_in: Money = Money(0)
    selected_out: Money = Money(0)

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


def _account_split_rows(db: DbSQLite, account_handle: str) -> list[tuple[Transaction, Split]]:
    rows: list[tuple[Transaction, Split]] = []
    for transaction in db.iter_transactions(account=account_handle):
        rows.extend(
            (transaction, split) for split in transaction.splits if split.account == account_handle
        )
    return rows


def _reconciled_through(transaction: Transaction, split: Split, through: date) -> bool:
    """Whether a reconciled split belongs to the balance as of a statement date.

    A split dated later belongs too when an earlier statement reconciled it,
    which happens when its date was wrong (#362).
    """
    if transaction.post_date <= through:
        return True
    return split.reconcile_date is not None and split.reconcile_date <= through


def _candidate_rows(
    db: DbSQLite,
    reconciliation: Reconciliation,
) -> tuple[Money, tuple[ReconciliationCandidate, ...]]:
    account = db.get_account(reconciliation.account)
    if account is None:
        raise KeyError(reconciliation.account)
    opening = Money(0)
    through = reconciliation.statement_date
    selected = set(reconciliation.selected_splits)
    candidates: list[ReconciliationCandidate] = []
    for transaction, split in _account_split_rows(db, reconciliation.account):
        amount = split.value * account.sign()
        if split.reconcile in {ReconcileState.RECONCILED, ReconcileState.FROZEN}:
            if _reconciled_through(transaction, split, through):
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
                after_statement=transaction.post_date > through,
                funds_in=split.value > 0,
                num=transaction.num,
            )
        )
    candidates.sort(key=lambda item: (item.post_date, item.transaction, item.split))
    return opening, tuple(candidates)


def reconciled_balance(db: DbSQLite, account: str, through: date) -> Money:
    """The account's reconciled balance as of a statement date (#362).

    This is the starting balance a new statement on that date would begin from,
    to compare with the statement's own previous balance before reconciling.
    """
    account_obj = db.get_account(account)
    if account_obj is None:
        raise KeyError(account)
    total = Money(0)
    for transaction, split in _account_split_rows(db, account):
        if split.reconcile in {
            ReconcileState.RECONCILED,
            ReconcileState.FROZEN,
        } and _reconciled_through(transaction, split, through):
            total += split.value * account_obj.sign()
    return total


#: How a statement's lists can be ordered (#377); ``date`` is the order they start in.
CANDIDATE_SORTS = ("date", "num", "description", "amount", "checked")


def sort_candidates(
    candidates: Iterable[ReconciliationCandidate], sort: str = "date", descending: bool = False
) -> list[ReconciliationCandidate]:
    """``candidates`` in a column's order, so a missing entry is easier to find (#377).

    ``num`` orders numbers by value, then other text, then entries without one;
    ``amount`` orders by size, as both lists show it; ``checked`` puts the checked
    entries first (last when descending). Entries dated after the statement stay
    below the others in either direction, since they are listed only in case a date
    is wrong. Ties keep date order.
    """
    if sort not in CANDIDATE_SORTS:
        raise ValueError(f"sort must be one of {', '.join(CANDIDATE_SORTS)}")

    def key(item: ReconciliationCandidate):
        if sort == "num":
            text = item.num.strip()
            if not text:
                return (2, 0, "")
            return (0, int(text), "") if text.isdigit() else (1, 0, text.casefold())
        if sort == "description":
            return item.description.casefold()
        if sort == "amount":
            return abs(item.amount)
        if sort == "checked":
            return not item.selected
        return item.post_date

    ordered = sorted(candidates, key=lambda item: (item.post_date, item.transaction, item.split))
    ordered.sort(key=key, reverse=descending)
    ordered.sort(key=lambda item: item.after_statement)
    return ordered


def summary(db: DbSQLite, reconciliation: Reconciliation | str) -> ReconciliationSummary:
    """Calculate opening, selected, and difference without mutating the book."""
    if isinstance(reconciliation, str):
        stored = db.get_reconciliation(reconciliation)
        if stored is None:
            raise KeyError(reconciliation)
        reconciliation = stored
    opening, candidates = _candidate_rows(db, reconciliation)
    checked = [item for item in candidates if item.selected]
    selected_balance = opening + sum((item.amount for item in checked), Money(0))
    return ReconciliationSummary(
        reconciliation=reconciliation,
        opening_balance=opening,
        selected_balance=selected_balance,
        difference=reconciliation.ending_balance - selected_balance,
        candidates=candidates,
        selected_in=sum((abs(item.amount) for item in checked if item.funds_in), Money(0)),
        selected_out=sum((abs(item.amount) for item in checked if not item.funds_in), Money(0)),
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
        item.split
        for item in candidates
        if item.state is ReconcileState.CLEARED and not item.after_statement
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
    reconciliation.completed_at = datetime.now(UTC)
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
    reconciliation.cancelled_at = datetime.now(UTC)
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
    reconciliation.audit_events.append(ReconciliationEvent("reopened", datetime.now(UTC)))
    with db.transaction("Reopen reconciliation") as txn:
        for transaction in transactions.values():
            db.commit_transaction(transaction, txn)
        db.commit_reconciliation(reconciliation, txn)
    return reconciliation


def toggle_cleared(db: DbSQLite, transaction: str, split: str) -> ReconcileState:
    """Mark one split cleared, or not cleared again, from a register's R column.

    Only ``n`` and ``c`` change here: ``y`` comes from completing a statement and
    goes back only by reopening it, so the statement's record stays true. While the
    account has an open statement, a newly cleared split that is a candidate joins
    its selection and an uncleared one leaves it, in the same change.
    """
    stored = db.get_transaction(transaction)
    if stored is None:
        raise KeyError(transaction)
    target = next((item for item in stored.splits if item.handle == split), None)
    if target is None:
        raise ReconciliationError(
            "reconciliation.split.missing", ("split",), "the split no longer exists"
        )
    if target.reconcile not in {ReconcileState.NOT_RECONCILED, ReconcileState.CLEARED}:
        raise ReconciliationError(
            "reconciliation.split.locked",
            ("split",),
            "a reconciled, frozen, or void entry changes only through its statement",
        )
    cleared = target.reconcile is ReconcileState.NOT_RECONCILED
    target.reconcile = ReconcileState.CLEARED if cleared else ReconcileState.NOT_RECONCILED
    target.reconcile_date = None
    session = open_for_account(db, target.account)
    if session is not None:
        selected = [item for item in session.selected_splits if item != split]
        if cleared and session.statement_date >= stored.post_date:
            selected.append(split)
        changed = selected != session.selected_splits
        session.selected_splits = selected
    label = "Mark cleared" if cleared else "Mark not cleared"
    with db.transaction(f"{label}: {stored.description}") as txn:
        db.commit_transaction(stored, txn)
        if session is not None and changed:
            db.commit_reconciliation(session, txn)
    return target.reconcile


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
