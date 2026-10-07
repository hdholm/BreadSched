"""Manage ordered categorization rules and accept their proposals.

CLI, GTK, and web adapters call these functions. Every write (a rule change or an
accepted batch) is one undoable database transaction, and a rejected request
leaves the book unchanged. Accepting recomputes the proposals first and changes
only a transaction whose single placeholder split still gets that category, so a
stale preview cannot overwrite a category chosen since.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..db.base import DbTxn
from ..db.sqlite import DbSQLite
from ..engine.categorization import (
    RULES_KEY,
    CategoryProposal,
    CategoryRule,
    load_rules,
    propose_categories,
)
from ..engine.payees import match_key
from ..lib.account import AccountClass
from ..lib.base import create_handle
from .contracts import ServiceError, ServiceResult

__all__ = [
    "AddRule",
    "AppliedCategories",
    "CategoryProposal",
    "CategoryRule",
    "add_rule",
    "apply_category_proposals",
    "delete_rule",
    "list_rules",
    "move_rule",
    "preview_category_proposals",
]


@dataclass(frozen=True, slots=True)
class AddRule:
    category: str
    #: Match transactions with this payee ...
    payee: str | None = None
    #: ... or whose description normalizes to the same key as this text.
    description: str | None = None
    #: 1-based position; ``None`` appends (lowest priority).
    position: int | None = None
    #: A description rule may also set this payee on a transaction without one.
    set_payee: str | None = None


@dataclass(frozen=True, slots=True)
class AppliedCategories:
    assigned: int
    #: Requested transactions that no longer have a proposal.
    unchanged: int
    #: Accepted transactions that also received the rule's payee.
    payees_set: int = 0


def list_rules(db: DbSQLite) -> list[CategoryRule]:
    """The rules in priority order (first match wins)."""
    return load_rules(db)


def _store(db: DbSQLite, rules: list[CategoryRule], txn: DbTxn) -> None:
    db.set_metadata(RULES_KEY, [rule.serialize() for rule in rules], txn)


def add_rule(db: DbSQLite, request: AddRule) -> ServiceResult[CategoryRule]:
    """Insert a rule at ``position`` (default: last)."""
    if (request.payee is None) == (request.description is None):
        return ServiceResult.failure(ServiceError("rule.match.required", ("payee", "description")))
    key: str | None = None
    if request.description is not None:
        key = match_key(request.description)
        if not key:
            return ServiceResult.failure(ServiceError("rule.match.empty", ("description",)))
    elif db.get_payee(request.payee or "") is None:
        return ServiceResult.failure(ServiceError("rule.payee.not_found", ("payee",)))
    if request.set_payee is not None:
        if request.payee is not None:
            return ServiceResult.failure(
                ServiceError("rule.set_payee.payee_match", ("set_payee", "payee"))
            )
        if db.get_payee(request.set_payee) is None:
            return ServiceResult.failure(ServiceError("rule.set_payee.not_found", ("set_payee",)))
    category = db.get_account(request.category)
    if (
        category is None
        or category.placeholder
        or category.account_class not in (AccountClass.INCOME, AccountClass.EXPENSE)
    ):
        return ServiceResult.failure(ServiceError("rule.category.invalid", ("category",)))
    rules = load_rules(db)
    if any(rule.payee == request.payee and rule.key == key for rule in rules):
        return ServiceResult.failure(ServiceError("rule.match.duplicate", ("payee", "description")))
    position = len(rules) + 1 if request.position is None else request.position
    if not 1 <= position <= len(rules) + 1:
        return ServiceResult.failure(ServiceError("rule.position.invalid", ("position",)))
    rule = CategoryRule(create_handle(), category.handle, request.payee, key, request.set_payee)
    rules.insert(position - 1, rule)
    with db.transaction("Add categorization rule") as txn:
        _store(db, rules, txn)
    return ServiceResult.success(rule)


def delete_rule(db: DbSQLite, handle: str) -> ServiceResult[CategoryRule]:
    rules = load_rules(db)
    found = [rule for rule in rules if rule.handle == handle]
    if not found:
        return ServiceResult.failure(ServiceError("rule.not_found", ("rule",)))
    with db.transaction("Delete categorization rule") as txn:
        _store(db, [rule for rule in rules if rule.handle != handle], txn)
    return ServiceResult.success(found[0])


def move_rule(db: DbSQLite, handle: str, position: int) -> ServiceResult[CategoryRule]:
    """Move a rule to a 1-based ``position``; earlier rules win."""
    rules = load_rules(db)
    found = [rule for rule in rules if rule.handle == handle]
    if not found:
        return ServiceResult.failure(ServiceError("rule.not_found", ("rule",)))
    if not 1 <= position <= len(rules):
        return ServiceResult.failure(ServiceError("rule.position.invalid", ("position",)))
    reordered = [rule for rule in rules if rule.handle != handle]
    reordered.insert(position - 1, found[0])
    if reordered != rules:
        with db.transaction("Reorder categorization rules") as txn:
            _store(db, reordered, txn)
    return ServiceResult.success(found[0])


def preview_category_proposals(db: DbSQLite) -> ServiceResult[tuple[CategoryProposal, ...]]:
    """Proposed categories for placeholder-posted transactions; writes nothing."""
    return ServiceResult.success(tuple(propose_categories(db)))


def apply_category_proposals(
    db: DbSQLite, transactions: tuple[str, ...] | None = None
) -> ServiceResult[AppliedCategories]:
    """Accept the current proposals (all of them, or only ``transactions``)."""
    current = {proposal.transaction: proposal for proposal in propose_categories(db)}
    wanted = list(current) if transactions is None else list(dict.fromkeys(transactions))
    if any(db.get_transaction(handle) is None for handle in wanted):
        return ServiceResult.failure(ServiceError("rule.transaction.not_found", ("transactions",)))
    accepted = [current[handle] for handle in wanted if handle in current]
    payees_set = 0
    if accepted:
        with db.transaction(f"Categorize {len(accepted)} transaction(s)") as txn:
            for proposal in accepted:
                transaction = db.get_transaction(proposal.transaction)
                assert transaction is not None
                for split in transaction.splits:
                    if split.account == proposal.placeholder:
                        split.account = proposal.category
                # A payee chosen since the preview is never replaced.
                if (
                    proposal.payee is not None
                    and transaction.payee is None
                    and db.get_payee(proposal.payee) is not None
                ):
                    transaction.payee = proposal.payee
                    payees_set += 1
                db.commit_transaction(transaction, txn)
    return ServiceResult.success(
        AppliedCategories(len(accepted), len(wanted) - len(accepted), payees_set)
    )
