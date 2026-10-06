"""Ordered, reviewed categorization rules and the proposals they make.

A rule maps a payee, or a normalized description key (``payees.match_key``), to an
income or expense category. Rules are kept in book metadata as one ordered list:
the first rule that matches a transaction proposes its category, and every later
matching rule that would choose a different category is reported as a conflict,
so the outcome is always explainable by position.

Only a transaction that still has exactly one split on an import placeholder
(Uncategorized CSV or Uncategorized OFX) is proposed. A category the user or the
source already chose is therefore never replaced, and a split transaction is never
given a guessed category. Proposals are read-only; the service applies accepted
ones.

A description rule may also name a payee (``set_payee``): its proposal then sets
that payee too, but only on a transaction that has none, so a payee the user or a
payee match already chose is never replaced. A payee rule needs no such field; it
matched because the payee was already set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from ..db.base import DbBase
from ..lib.money import Money
from .payees import match_key

__all__ = [
    "RULES_KEY",
    "CategoryConflict",
    "CategoryProposal",
    "CategoryRule",
    "load_rules",
    "placeholder_handles",
    "propose_categories",
]

#: Book metadata key holding the ordered rule list.
RULES_KEY = "categorization_rules"


def placeholder_handles() -> tuple[tuple[tuple[str, str], str], ...]:
    """The uncategorized accounts statement imports post to, keyed by (format, type).

    Only a split in one of these is an unreviewed guess that a transfer link or a
    categorization rule may replace; any other account is a category the user or
    the source chose.
    """
    return tuple(
        ((fmt, atype), uuid5(NAMESPACE_URL, f"breadsched:{fmt}:category:{atype}|{name}").hex)
        for fmt, name in (("csv", "Uncategorized CSV"), ("ofx", "Uncategorized OFX"))
        for atype in ("EXPENSE", "INCOME")
    )


@dataclass(frozen=True, slots=True)
class CategoryRule:
    """Match exactly one of ``payee`` or ``key``; propose ``category``.

    A ``key`` rule may also propose ``set_payee`` for a transaction with no payee.
    """

    handle: str
    category: str
    payee: str | None = None
    key: str | None = None
    set_payee: str | None = None

    def serialize(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "category": self.category,
            "payee": self.payee,
            "key": self.key,
            "set_payee": self.set_payee,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CategoryRule:
        return cls(
            handle=str(data["handle"]),
            category=str(data["category"]),
            payee=data.get("payee"),
            key=data.get("key"),
            set_payee=data.get("set_payee"),
        )

    def matches(self, payee: str | None, key: str) -> bool:
        if self.payee is not None:
            return payee == self.payee
        return bool(key) and key == self.key


@dataclass(frozen=True, slots=True)
class CategoryConflict:
    """A later matching rule that would have chosen a different category."""

    rule_position: int
    category: str


@dataclass(frozen=True, slots=True)
class CategoryProposal:
    transaction: str
    when: date
    description: str
    #: Value of the placeholder split the proposal would recategorize.
    amount: Money
    placeholder: str
    category: str
    #: 1-based position of the deciding rule.
    rule_position: int
    rule: CategoryRule
    conflicts: tuple[CategoryConflict, ...] = ()
    #: The payee accepting also sets: the rule's ``set_payee`` when the
    #: transaction has no payee yet, else ``None``.
    payee: str | None = None


def load_rules(db: DbBase) -> list[CategoryRule]:
    raw = db.get_metadata(RULES_KEY, [])
    if not isinstance(raw, list):
        return []
    return [CategoryRule.from_dict(item) for item in raw if isinstance(item, dict)]


def propose_categories(db: DbBase) -> list[CategoryProposal]:
    """Proposals for transactions with one placeholder split, newest first."""
    rules = load_rules(db)
    if not rules:
        return []
    placeholders = {handle for _kind, handle in placeholder_handles()}
    proposals: list[CategoryProposal] = []
    for transaction in db.iter_transactions():
        guesses = [split for split in transaction.splits if split.account in placeholders]
        if len(guesses) != 1:
            continue
        key = match_key(transaction.description)
        matched = [
            (position, rule)
            for position, rule in enumerate(rules, start=1)
            if rule.matches(transaction.payee, key)
        ]
        if not matched:
            continue
        position, rule = matched[0]
        conflicts = tuple(
            CategoryConflict(other_position, other.category)
            for other_position, other in matched[1:]
            if other.category != rule.category
        )
        proposals.append(
            CategoryProposal(
                transaction.handle,
                transaction.post_date,
                transaction.description,
                guesses[0].value,
                guesses[0].account,
                rule.category,
                position,
                rule,
                conflicts,
                rule.set_payee if transaction.payee is None else None,
            )
        )
    proposals.sort(key=lambda item: (item.when, item.transaction), reverse=True)
    return proposals
