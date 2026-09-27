"""Deterministic payee proposals from transaction descriptions.

Matching is exact on a normalized description key, never fuzzy: two
descriptions propose the same payee only when their keys are identical, so every
proposal can be explained by the key that produced it. A description key belongs
to at most one payee. Transactions that already have a payee are never proposed
again, so an accepted or manually chosen payee is never silently replaced.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from ..db.base import DbBase
from ..lib.payee import Payee

__all__ = ["PayeeProposal", "match_key", "payee_index", "propose_payees"]

_SEPARATORS = re.compile(r"[^\w]+")


def match_key(description: str) -> str:
    """The comparison key for one description.

    Case, accents' compatibility forms, punctuation, and spacing are ignored, and
    any word containing a digit is dropped, because statements append changing
    store numbers, card suffixes, and reference numbers to the same merchant.
    ``"CORNER GROCER #1234"`` and ``"Corner Grocer 0987"`` share ``"corner grocer"``.
    """
    text = unicodedata.normalize("NFKC", description).casefold().replace("_", " ")
    words = [
        word for word in _SEPARATORS.split(text) if word and not any(c.isdigit() for c in word)
    ]
    return " ".join(words)


@dataclass(frozen=True, slots=True)
class PayeeProposal:
    """One unassigned transaction whose description key names a payee."""

    transaction: str
    when: date
    description: str
    payee: str
    payee_name: str
    key: str


def payee_index(payees: list[Payee]) -> dict[str, Payee]:
    """Description key -> payee; the service keeps keys unique across payees."""
    index: dict[str, Payee] = {}
    for payee in payees:
        for key in payee.match_keys:
            index.setdefault(key, payee)
    return index


def propose_payees(db: DbBase) -> list[PayeeProposal]:
    """Proposals for every transaction without a payee, newest first."""
    index = payee_index(list(db.iter_payees()))
    if not index:
        return []
    proposals = []
    for transaction in db.iter_transactions():
        if transaction.payee is not None:
            continue
        key = match_key(transaction.description)
        payee = index.get(key)
        if payee is None:
            continue
        proposals.append(
            PayeeProposal(
                transaction.handle,
                transaction.post_date,
                transaction.description,
                payee.handle,
                payee.name,
                key,
            )
        )
    proposals.sort(key=lambda item: (item.when, item.transaction), reverse=True)
    return proposals
