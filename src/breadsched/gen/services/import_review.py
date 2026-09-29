"""Typed review of GnuCash changes held back from locally reconciled transactions.

Re-import never silently replaces a transaction the user reconciled in
BreadSched. The importer holds the incoming version instead, and this service lets
every presentation list those held versions and apply one batch of decisions:

* keep the BreadSched version (not asked again until GnuCash changes it again);
* use the GnuCash version, preserving BreadSched annotations; or
* decide later.

A held deletion (the transaction was deleted in GnuCash while reconciled) offers
the same choices: keeping it keeps the transaction and is not asked again, and
using the GnuCash version deletes it here too, unless a BreadSched
reconciliation, FSA claim, or receivable has come to refer to it since.

A batch is validated completely before anything is written and then commits as
one undoable database transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import Enum

from ..db.sqlite import DbSQLite
from ..engine import import_review
from ..lib.transaction import Transaction, UnbalancedError
from .contracts import ServiceError, ServiceResult

__all__ = [
    "HeldImportChange",
    "HeldImportDecision",
    "HeldImportResolution",
    "ResolveHeldImports",
    "pending_import_changes",
    "resolve_import_changes",
]


class HeldImportDecision(str, Enum):
    KEEP_LOCAL = "keep"
    USE_SOURCE = "use-source"
    LATER = "later"


@dataclass(frozen=True, slots=True)
class HeldImportChange:
    """One held GnuCash change, described for review."""

    transaction: str
    post_date: date
    description: str
    source: str
    detected: date
    changes: tuple[str, ...]
    #: Completed statements that must be reopened before the GnuCash version applies,
    #: or, for a deletion, what still refers to the transaction.
    blocked_by: tuple[str, ...]
    #: The transaction was deleted in GnuCash; using GnuCash deletes it here.
    deleted: bool = False

    @property
    def can_use_source(self) -> bool:
        return not self.blocked_by


@dataclass(frozen=True, slots=True)
class ResolveHeldImports:
    decisions: tuple[tuple[str, HeldImportDecision], ...]


@dataclass(frozen=True, slots=True)
class HeldImportResolution:
    kept: int
    applied: int
    deferred: int


def pending_import_changes(db: DbSQLite) -> list[HeldImportChange]:
    """Held changes still awaiting a decision, oldest transaction first."""
    items: list[HeldImportChange] = []
    for handle, held in import_review.held_changes(db).items():
        if held.status is not import_review.HeldStatus.PENDING:
            continue
        existing = db.get_transaction(handle)
        if existing is None:
            continue
        items.append(
            HeldImportChange(
                transaction=handle,
                post_date=existing.post_date,
                description=existing.description,
                source=held.source,
                detected=held.detected,
                changes=held.changes,
                blocked_by=_blockers(db, existing, held),
                deleted=held.deleted,
            )
        )
    items.sort(key=lambda item: (item.post_date, item.description, item.transaction))
    return items


def resolve_import_changes(
    db: DbSQLite, request: ResolveHeldImports
) -> ServiceResult[HeldImportResolution]:
    """Apply one reviewed batch of decisions atomically."""
    held = import_review.held_changes(db)
    seen: set[str] = set()
    errors: list[ServiceError] = []
    for handle, decision in request.decisions:
        change = held.get(handle)
        if handle in seen:
            errors.append(ServiceError("import.review.duplicate", ("decisions", handle)))
            continue
        seen.add(handle)
        if change is None or change.status is not import_review.HeldStatus.PENDING:
            errors.append(ServiceError("import.review.not_pending", ("decisions", handle)))
            continue
        existing = db.get_transaction(handle)
        if existing is None:
            errors.append(ServiceError("import.review.missing", ("decisions", handle)))
            continue
        if decision is HeldImportDecision.USE_SOURCE and _blockers(db, existing, change):
            code = (
                "import.review.deletion_referenced"
                if change.deleted
                else "import.review.reconciliation_blocks"
            )
            errors.append(ServiceError(code, ("decisions", handle)))
    if errors:
        return ServiceResult.failure(*errors)

    kept = applied = deferred = 0
    updated = dict(held)
    try:
        with db.transaction("Review held GnuCash changes") as txn:
            for handle, decision in request.decisions:
                change = held[handle]
                if decision is HeldImportDecision.LATER:
                    deferred += 1
                elif decision is HeldImportDecision.KEEP_LOCAL:
                    updated[handle] = replace(change, status=import_review.HeldStatus.KEPT)
                    kept += 1
                elif change.deleted:
                    db.remove_transaction(handle, txn)
                    del updated[handle]
                    applied += 1
                else:
                    existing = db.get_transaction(handle)
                    assert existing is not None
                    incoming = change.incoming_transaction()
                    import_review.merge_local_state(incoming, existing)
                    db.commit_transaction(incoming, txn)
                    del updated[handle]
                    applied += 1
            if updated != held:
                import_review.store_held_changes(db, updated, txn)
    except UnbalancedError:
        return ServiceResult.failure(ServiceError("import.review.unbalanced", ("decisions",)))
    return ServiceResult.success(HeldImportResolution(kept, applied, deferred))


def _blockers(
    db: DbSQLite, existing: Transaction, held: import_review.HeldChange
) -> tuple[str, ...]:
    if held.deleted:
        return tuple(import_review.deletion_references(db, existing))
    statements = import_review.blocking_reconciliations(db, existing, held.incoming_transaction())
    return tuple(_statement_label(db, item) for item in statements)


def _statement_label(db: DbSQLite, reconciliation) -> str:
    account = db.get_account(reconciliation.account)
    name = account.name if account is not None else reconciliation.account
    return f"{name} statement {reconciliation.statement_date.isoformat()}"
