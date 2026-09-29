"""Ownership rules that protect locally reconciled imported transactions.

A GnuCash re-import is authoritative for ledger facts, but a statement the user
has reconciled in BreadSched is a durable local assertion about those facts. The
importer therefore distinguishes three cases for a matching transaction:

* No split is reconciled locally: GnuCash facts replace the local ones.
* A split is reconciled locally and no *protected* fact changed: the refresh is
  applied, and each reconciled split keeps its local state and statement date.
* A protected fact changed: the transaction is left untouched and the incoming
  source version is held for an explicit batched decision.

Protected facts are the transaction's date, description, number, and currency,
plus each locally reconciled split's account, value, quantity, memo, and action,
or that split's disappearance. Changes confined to splits that are not reconciled
locally, and read-only source notes, never require review.

A transaction deleted in GnuCash is held the same way when any of its splits is
reconciled, whether it was reconciled in GnuCash or in BreadSched: the deletion
is recorded as a held change with ``deleted`` set and no incoming version, and the
transaction stays until the user chooses to apply the deletion. (A transaction a
BreadSched reconciliation, FSA claim, or receivable still refers to is retained
outright; see ``deletion_references``.)

Held versions live in book metadata written inside the import's database
transaction, so they are undoable, survive restart, and are shared by every
presentation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

from ..db.base import DbTxn
from ..db.sqlite import DbSQLite
from ..lib.reconciliation import Reconciliation, ReconciliationStatus
from ..lib.transaction import ReconcileState, Split, Transaction

__all__ = [
    "REVIEW_KEY",
    "HeldChange",
    "HeldStatus",
    "DELETION_FINGERPRINT",
    "blocking_reconciliations",
    "deletion_references",
    "describe_changes",
    "held_changes",
    "is_reconciled",
    "is_protected_change",
    "locally_reconciled",
    "merge_local_state",
    "source_fingerprint",
    "split_source_facts",
    "store_held_changes",
    "transaction_source_facts",
]

REVIEW_KEY = "import.reconciled_review"
#: The fingerprint of a held deletion; the deleted source has no version to digest.
DELETION_FINGERPRINT = "deleted"


class HeldStatus(str, Enum):
    """Whether a held source version still awaits a decision."""

    PENDING = "pending"
    #: The user kept the BreadSched version of this exact source version.
    KEPT = "kept"


@dataclass(frozen=True, slots=True)
class HeldChange:
    """One incoming source version withheld from a locally reconciled transaction."""

    transaction: str
    status: HeldStatus
    fingerprint: str
    source: str
    detected: date
    changes: tuple[str, ...]
    incoming: dict[str, Any]
    #: The transaction was deleted in the source; ``incoming`` is then empty.
    deleted: bool = False

    def serialize(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "status": self.status.value,
            "fingerprint": self.fingerprint,
            "source": self.source,
            "detected": self.detected.isoformat(),
            "changes": list(self.changes),
            "incoming": self.incoming,
        }
        if self.deleted:
            data["deleted"] = True
        return data

    @classmethod
    def from_dict(cls, handle: str, data: dict[str, Any]) -> HeldChange:
        return cls(
            transaction=handle,
            status=HeldStatus(data.get("status", HeldStatus.PENDING.value)),
            fingerprint=str(data["fingerprint"]),
            source=str(data.get("source", "")),
            detected=date.fromisoformat(str(data["detected"])),
            changes=tuple(str(item) for item in data.get("changes", [])),
            incoming=dict(data["incoming"]),
            deleted=bool(data.get("deleted", False)),
        )

    def incoming_transaction(self) -> Transaction:
        return Transaction.from_dict(self.incoming)


# ------------------------------------------------------------------ source facts


def split_source_facts(split: Split) -> tuple[object, ...]:
    """Fields for which an imported ledger source remains authoritative."""
    return (
        split.account,
        split.value,
        split.quantity,
        split.memo,
        split.action,
        split.reconcile,
    )


def transaction_source_facts(transaction: Transaction) -> tuple[object, ...]:
    facts: tuple[object, ...] = (
        transaction.post_date,
        transaction.description,
        transaction.currency,
        transaction.num,
        transaction.source_notes,
        tuple(sorted((split_source_facts(split) for split in transaction.splits), key=repr)),
    )
    # Appended only when present, so fingerprints of books without source links
    # (every book before links were imported) stay exactly as they were.
    return (*facts, transaction.source_link) if transaction.source_link else facts


def source_fingerprint(transaction: Transaction) -> str:
    """A stable digest of one source version, used to avoid asking twice."""
    return hashlib.sha256(repr(transaction_source_facts(transaction)).encode()).hexdigest()


# --------------------------------------------------------------- classification


def locally_reconciled(transaction: Transaction) -> set[str]:
    """Handles of the splits whose local state is Reconciled."""
    return {
        split.handle for split in transaction.splits if split.reconcile is ReconcileState.RECONCILED
    }


def _protected_split_facts(split: Split | None) -> tuple[object, ...] | None:
    if split is None:
        return None
    return (split.account, split.value, split.quantity, split.memo, split.action)


def _protected_facts(transaction: Transaction, reconciled: Iterable[str]) -> tuple[object, ...]:
    by_handle = {split.handle: split for split in transaction.splits}
    return (
        transaction.post_date,
        transaction.description,
        transaction.num,
        transaction.currency,
        tuple(
            (handle, _protected_split_facts(by_handle.get(handle))) for handle in sorted(reconciled)
        ),
    )


def is_protected_change(existing: Transaction, incoming: Transaction) -> bool:
    """Whether applying ``incoming`` would alter a locally reconciled record."""
    reconciled = locally_reconciled(existing)
    if not reconciled:
        return False
    return _protected_facts(existing, reconciled) != _protected_facts(incoming, reconciled)


def describe_changes(
    existing: Transaction,
    incoming: Transaction,
    account_name: Callable[[str], str] = lambda handle: handle,
) -> tuple[str, ...]:
    """Plain-language differences in the protected facts, for review."""
    changes: list[str] = []
    for label, before, after in (
        ("Date", existing.post_date, incoming.post_date),
        ("Description", existing.description, incoming.description),
        ("Number", existing.num, incoming.num),
        ("Currency", existing.currency, incoming.currency),
    ):
        if before != after:
            changes.append(f"{label}: {_shown(before)} -> {_shown(after)}")
    incoming_by_handle = {split.handle: split for split in incoming.splits}
    for split in existing.splits:
        if split.reconcile is not ReconcileState.RECONCILED:
            continue
        name = account_name(split.account)
        other = incoming_by_handle.get(split.handle)
        if other is None:
            changes.append(f"Reconciled split in {name} {split.value} is removed")
            continue
        if other.account != split.account:
            changes.append(f"Reconciled split account: {name} -> {account_name(other.account)}")
        split_fields: tuple[tuple[str, object, object], ...] = (
            ("amount", split.value, other.value),
            ("quantity", split.quantity, other.quantity),
            ("memo", split.memo, other.memo),
            ("action", split.action, other.action),
        )
        for field_label, old, new in split_fields:
            if old != new:
                changes.append(
                    f"Reconciled split in {name} {field_label}: {_shown(old)} -> {_shown(new)}"
                )
    return tuple(changes)


def _shown(value: object) -> str:
    if value is None or value == "":
        return "(blank)"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------- merging


def merge_local_state(incoming: Transaction, existing: Transaction) -> None:
    """Carry BreadSched-owned state from ``existing`` onto an incoming version.

    GnuCash remains authoritative for ledger facts such as dates, amounts,
    accounts, memos, and actions. BreadSched owns planning/review annotations, payees,
    FSA classifications, and the reconcile state of a split it reconciled while
    that split's account, value, and quantity are unchanged. Split annotations
    are preserved only when the source split GUID still exists, so a materially
    replaced source split cannot inherit stale state.
    """
    incoming.notes = existing.notes
    incoming.scheduled_from = existing.scheduled_from
    incoming.planned_occurrence = existing.planned_occurrence
    incoming.planned_for = existing.planned_for
    incoming.planned_amount = existing.planned_amount
    incoming.planning_resolution = existing.planning_resolution
    incoming.rejected_plan_occurrences = list(existing.rejected_plan_occurrences)
    incoming.payee = existing.payee
    incoming.tags = list(existing.tags)
    incoming.attachments = list(existing.attachments)

    existing_splits = {split.handle: split for split in existing.splits}
    for split in incoming.splits:
        prior = existing_splits.get(split.handle)
        if prior is None:
            continue
        split.planning_flow = prior.planning_flow
        split.investment_activity = prior.investment_activity
        split.fsa_year_start = prior.fsa_year_start
        if prior.reconcile is ReconcileState.RECONCILED and (
            split.account,
            split.value,
            split.quantity,
        ) == (prior.account, prior.value, prior.quantity):
            split.reconcile = ReconcileState.RECONCILED
            split.reconcile_date = prior.reconcile_date


def blocking_reconciliations(
    db: DbSQLite, existing: Transaction, incoming: Transaction
) -> list[Reconciliation]:
    """Completed statements that ``incoming`` would stop balancing."""
    incoming_by_handle = {split.handle: split for split in incoming.splits}
    changed: set[str] = set()
    for split in existing.splits:
        if split.reconcile is not ReconcileState.RECONCILED:
            continue
        other = incoming_by_handle.get(split.handle)
        if other is None or (other.account, other.value, other.quantity) != (
            split.account,
            split.value,
            split.quantity,
        ):
            changed.add(split.handle)
    if not changed:
        return []
    return [
        reconciliation
        for reconciliation in db.iter_reconciliations()
        if reconciliation.status is ReconciliationStatus.COMPLETED
        and changed.intersection(reconciliation.selected_splits)
    ]


def deletion_references(db: DbSQLite, transaction: Transaction) -> list[str]:
    """Name BreadSched-owned objects that make deleting ``transaction`` unsafe."""
    split_handles = {split.handle for split in transaction.splits}
    reconciliation_count = sum(
        bool(split_handles.intersection(item.selected_splits)) for item in db.iter_reconciliations()
    )
    claim_count = 0
    for claim in db.iter_fsa_claims():
        links = [*claim.payments, *claim.refunds]
        links.extend(link for allocation in claim.allocations for link in allocation.reimbursements)
        if any(
            link.transaction == transaction.handle or link.split in split_handles for link in links
        ):
            claim_count += 1
    receivable_count = 0
    for receivable in db.iter_receivables():
        receivable_links = [*receivable.expenses, *receivable.reimbursements]
        if any(
            link.transaction == transaction.handle or link.split in split_handles
            for link in receivable_links
        ):
            receivable_count += 1

    references: list[str] = []
    if reconciliation_count:
        references.append(
            f"{reconciliation_count} reconciliation{'s' if reconciliation_count != 1 else ''}"
        )
    if claim_count:
        references.append(f"{claim_count} FSA claim{'s' if claim_count != 1 else ''}")
    if receivable_count:
        references.append(f"{receivable_count} receivable{'s' if receivable_count != 1 else ''}")
    return references


def is_reconciled(transaction: Transaction) -> bool:
    """Whether any split is reconciled, in GnuCash or in BreadSched."""
    return bool(locally_reconciled(transaction))


# ---------------------------------------------------------------------- storage


def held_changes(db: DbSQLite) -> dict[str, HeldChange]:
    """Every retained held version, pending or kept, keyed by transaction."""
    raw = db.get_metadata(REVIEW_KEY, {})
    if not isinstance(raw, dict):
        return {}
    held: dict[str, HeldChange] = {}
    for handle, data in raw.items():
        if not isinstance(data, dict):
            continue
        try:
            held[str(handle)] = HeldChange.from_dict(str(handle), data)
        except (KeyError, TypeError, ValueError):
            continue
    return held


def store_held_changes(db: DbSQLite, held: dict[str, HeldChange], txn: DbTxn) -> None:
    db.set_metadata(
        REVIEW_KEY,
        {handle: change.serialize() for handle, change in sorted(held.items())},
        txn,
    )
