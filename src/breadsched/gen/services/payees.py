"""Create, preview, and accept payees for CLI, GTK, and web adapters.

Every write is one undoable database transaction, and a rejected request leaves
the book unchanged. Accepting proposals assigns a payee only to transactions that
still have none and whose description still produces the proposed key, so a
stale preview cannot overwrite a choice made since. Descriptions are never
rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..db.sqlite import DbSQLite
from ..engine.payees import PayeeProposal, match_key, payee_index, propose_payees
from ..lib.payee import Payee
from ..lib.transaction import Transaction
from .contracts import ServiceError, ServiceResult

__all__ = [
    "AppliedPayees",
    "PayeeProposal",
    "SavePayee",
    "apply_payee_proposals",
    "assign_payee",
    "delete_payee",
    "match_key",
    "preview_payee_proposals",
    "save_payee",
]


@dataclass(frozen=True, slots=True)
class SavePayee:
    name: str
    #: Example descriptions or keys; each is normalized with ``match_key``.
    matches: tuple[str, ...] = ()
    #: The payee to rename or re-key; ``None`` creates one.
    handle: str | None = None


@dataclass(frozen=True, slots=True)
class AppliedPayees:
    assigned: int
    #: Requested transactions that already had a payee or no longer match.
    unchanged: int


def save_payee(db: DbSQLite, request: SavePayee) -> ServiceResult[Payee]:
    """Create a payee or replace one's name and description keys."""
    name = " ".join(request.name.split())
    if not name:
        return ServiceResult.failure(ServiceError("payee.name.required", ("name",)))
    existing = db.get_payee(request.handle) if request.handle is not None else None
    if request.handle is not None and existing is None:
        return ServiceResult.failure(ServiceError("payee.not_found", ("handle",)))
    others = [payee for payee in db.iter_payees() if payee.handle != request.handle]
    if any(payee.name.casefold() == name.casefold() for payee in others):
        return ServiceResult.failure(ServiceError("payee.name.duplicate", ("name",)))
    keys: list[str] = []
    for raw in request.matches:
        key = match_key(raw)
        if not key:
            return ServiceResult.failure(ServiceError("payee.match.empty", ("matches",)))
        if key not in keys:
            keys.append(key)
    claimed = payee_index(others)
    if any(key in claimed for key in keys):
        return ServiceResult.failure(ServiceError("payee.match.conflict", ("matches",)))
    payee = existing or Payee()
    payee.name = name
    payee.match_keys = keys
    with db.transaction(f"Save payee {name}") as txn:
        if existing is None:
            db.add_payee(payee, txn)
        else:
            db.commit_payee(payee, txn)
    return ServiceResult.success(payee)


def delete_payee(db: DbSQLite, handle: str) -> ServiceResult[int]:
    """Delete a payee and clear it from its transactions; returns how many."""
    payee = db.get_payee(handle)
    if payee is None:
        return ServiceResult.failure(ServiceError("payee.not_found", ("handle",)))
    cleared = 0
    with db.transaction(f"Delete payee {payee.name}") as txn:
        for transaction in db.iter_transactions():
            if transaction.payee == handle:
                transaction.payee = None
                db.commit_transaction(transaction, txn)
                cleared += 1
        db.remove_payee(handle, txn)
    return ServiceResult.success(cleared)


def preview_payee_proposals(db: DbSQLite) -> ServiceResult[tuple[PayeeProposal, ...]]:
    """Proposed payees for unassigned transactions; writes nothing."""
    return ServiceResult.success(tuple(propose_payees(db)))


def apply_payee_proposals(
    db: DbSQLite, transactions: tuple[str, ...] | None = None
) -> ServiceResult[AppliedPayees]:
    """Accept the current proposals (all of them, or only ``transactions``)."""
    current = {proposal.transaction: proposal for proposal in propose_payees(db)}
    wanted = list(current) if transactions is None else list(dict.fromkeys(transactions))
    if any(db.get_transaction(handle) is None for handle in wanted):
        return ServiceResult.failure(ServiceError("payee.transaction.not_found", ("transactions",)))
    accepted = [current[handle] for handle in wanted if handle in current]
    if accepted:
        with db.transaction(f"Accept {len(accepted)} payee(s)") as txn:
            for proposal in accepted:
                transaction = db.get_transaction(proposal.transaction)
                assert transaction is not None
                transaction.payee = proposal.payee
                db.commit_transaction(transaction, txn)
    return ServiceResult.success(AppliedPayees(len(accepted), len(wanted) - len(accepted)))


def assign_payee(db: DbSQLite, transaction: str, payee: str | None) -> ServiceResult[Transaction]:
    """Explicitly set or clear (``payee=None``) one transaction's payee."""
    stored = db.get_transaction(transaction)
    if stored is None:
        return ServiceResult.failure(ServiceError("payee.transaction.not_found", ("transaction",)))
    if payee is not None and db.get_payee(payee) is None:
        return ServiceResult.failure(ServiceError("payee.not_found", ("payee",)))
    if stored.payee != payee:
        stored.payee = payee
        with db.transaction("Set payee") as txn:
            db.commit_transaction(stored, txn)
    return ServiceResult.success(stored)
