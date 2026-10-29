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
from ..engine.description_keys import match_key
from ..lib.account import AccountClass
from ..lib.base import create_handle
from .contracts import ServiceError, ServiceResult

__all__ = [
    "AddRule",
    "AppliedCategories",
    "CategorizationRules",
    "CategoryChoice",
    "CategoryProposal",
    "CategoryRule",
    "ProposalLine",
    "RuleLine",
    "add_rule",
    "apply_category_proposals",
    "categorization_rules",
    "delete_rule",
    "list_rules",
    "move_rule",
    "preview_category_proposals",
]


@dataclass(frozen=True, slots=True)
class AddRule:
    category: str
    #: Match transactions whose description normalizes to the same key as this text.
    description: str
    #: 1-based position; ``None`` appends (lowest priority).
    position: int | None = None


@dataclass(frozen=True, slots=True)
class AppliedCategories:
    assigned: int
    #: Requested transactions that no longer have a proposal.
    unchanged: int


def list_rules(db: DbSQLite) -> list[CategoryRule]:
    """The rules in priority order (first match wins)."""
    return load_rules(db)


def _store(db: DbSQLite, rules: list[CategoryRule], txn: DbTxn) -> None:
    db.set_metadata(RULES_KEY, [rule.serialize() for rule in rules], txn)


def add_rule(db: DbSQLite, request: AddRule) -> ServiceResult[CategoryRule]:
    """Insert a rule at ``position`` (default: last)."""
    key = match_key(request.description)
    if not key:
        return ServiceResult.failure(ServiceError("rule.match.empty", ("description",)))
    category = db.get_account(request.category)
    if (
        category is None
        or category.placeholder
        or category.account_class not in (AccountClass.INCOME, AccountClass.EXPENSE)
    ):
        return ServiceResult.failure(ServiceError("rule.category.invalid", ("category",)))
    rules = load_rules(db)
    if any(rule.key == key for rule in rules):
        return ServiceResult.failure(ServiceError("rule.match.duplicate", ("description",)))
    position = len(rules) + 1 if request.position is None else request.position
    if not 1 <= position <= len(rules) + 1:
        return ServiceResult.failure(ServiceError("rule.position.invalid", ("position",)))
    rule = CategoryRule(create_handle(), category.handle, key)
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
    if accepted:
        with db.transaction(f"Categorize {len(accepted)} transaction(s)") as txn:
            for proposal in accepted:
                transaction = db.get_transaction(proposal.transaction)
                assert transaction is not None
                for split in transaction.splits:
                    if split.account == proposal.placeholder:
                        split.account = proposal.category
                db.commit_transaction(transaction, txn)
    return ServiceResult.success(AppliedCategories(len(accepted), len(wanted) - len(accepted)))


@dataclass(frozen=True, slots=True)
class RuleLine:
    #: 1-based priority; earlier rules win.
    position: int
    rule: CategoryRule
    category_name: str


@dataclass(frozen=True, slots=True)
class ProposalLine:
    proposal: CategoryProposal
    category_name: str
    #: The category name of each of the proposal's conflicts, in order.
    conflict_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CategoryChoice:
    """An account a rule may choose: a non-placeholder income or expense account."""

    handle: str
    name: str


@dataclass(frozen=True, slots=True)
class CategorizationRules:
    """Everything a rules screen shows: the rules, their proposals, and the choices."""

    rules: tuple[RuleLine, ...]
    proposals: tuple[ProposalLine, ...]
    categories: tuple[CategoryChoice, ...]


def categorization_rules(db: DbSQLite) -> CategorizationRules:
    """The rules in priority order, the current proposals, and the category choices,
    each with its account's full name; writes nothing."""

    def name(handle: str) -> str:
        return db.full_name(handle) or handle

    return CategorizationRules(
        tuple(
            RuleLine(position, rule, name(rule.category))
            for position, rule in enumerate(load_rules(db), start=1)
        ),
        tuple(
            ProposalLine(
                proposal,
                name(proposal.category),
                tuple(name(conflict.category) for conflict in proposal.conflicts),
            )
            for proposal in propose_categories(db)
        ),
        tuple(
            sorted(
                (
                    CategoryChoice(account.handle, name(account.handle))
                    for account in db.iter_accounts()
                    if not account.placeholder
                    and account.account_class in (AccountClass.INCOME, AccountClass.EXPENSE)
                ),
                key=lambda choice: choice.name.casefold(),
            )
        ),
    )
