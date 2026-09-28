"""Links from a reimbursement record to ordinary ledger splits.

FSA claims and reimbursable expenses (receivables) are the same idea -- money
paid out that someone else is expected to give back -- with different payers
and rules (issue #170). Both point at ledger splits by (transaction, split)
handle rather than copying amounts, so both resolve and total their links here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol

from ..db.sqlite import DbSQLite
from ..lib.money import Money
from ..lib.transaction import Split, Transaction

__all__ = ["SplitLink", "resolve_link", "sum_links"]


class SplitLink(Protocol):
    @property
    def transaction(self) -> str: ...

    @property
    def split(self) -> str: ...


#: Builds the caller's own error for ``("transaction" | "split")`` not found.
MissingLink = Callable[[str], Exception]


def resolve_link(db: DbSQLite, link: SplitLink, missing: MissingLink) -> tuple[Transaction, Split]:
    """The linked transaction and split, or ``missing(...)`` raised when either is gone."""
    transaction = db.get_transaction(link.transaction)
    if transaction is None:
        raise missing("transaction")
    split = next((item for item in transaction.splits if item.handle == link.split), None)
    if split is None:
        raise missing("split")
    return transaction, split


def sum_links(db: DbSQLite, links: Iterable[SplitLink], missing: MissingLink) -> Money:
    """The total magnitude of the linked splits."""
    total = Money(0)
    for link in links:
        _transaction, split = resolve_link(db, link, missing)
        total = total + abs(split.value)
    return total
