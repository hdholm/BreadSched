"""Links from a reimbursement record to ordinary ledger splits.

FSA claims and reimbursable expenses (receivables) are the same idea -- money
paid out that someone else is expected to give back -- with different payers
and rules (issue #170). Both point at ledger splits by ``SplitLink`` rather than
copying amounts, so both resolve, total, and check their links here (#340): a
claim and a receivable refuse the same missing splits, the same repeated links,
and the reclassification transactions BreadSched owns, under their own error
codes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from ..db.sqlite import DbSQLite
from ..lib.money import Money
from ..lib.split_link import SplitLink
from ..lib.transaction import Split, Transaction

__all__ = [
    "LinkError",
    "MissingLink",
    "SplitLink",
    "missing_link",
    "owned_postings",
    "refuse_owned_posting",
    "resolve_link",
    "sum_links",
]


class LinkError(ValueError):
    """A stable linked-record failure independent of presentation wording.

    ``FsaClaimError`` and ``ReceivableError`` are its two kinds; each record's
    codes start with its own prefix (``claim.`` or ``receivable.``).
    """

    def __init__(self, code: str, fields: tuple[str, ...], message: str) -> None:
        super().__init__(message)
        self.code = code
        self.fields = fields


#: Builds the caller's own error for ``("transaction" | "split")`` not found.
MissingLink = Callable[[str], Exception]


def missing_link(prefix: str, error: type[LinkError]) -> MissingLink:
    """The ``{prefix}.link.transaction|split.not_found`` error, as ``error``."""

    def build(part: str) -> Exception:
        return error(
            f"{prefix}.link.{part}.not_found",
            ("links",),
            f"linked transaction{' split' if part == 'split' else ''} no longer exists",
        )

    return build


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


def owned_postings(db: DbSQLite) -> set[str]:
    """Handles of every reclassification transaction a receivable owns.

    BreadSched writes and rewrites these from the receivable, so no claim or
    receivable may link one of their splits.
    """
    return {handle for receivable in db.iter_receivables() for handle in receivable.postings}


def refuse_owned_posting(
    transaction: str, owned: set[str], prefix: str, error: type[LinkError]
) -> None:
    """Raise ``{prefix}.split.owned_posting`` when ``transaction`` is BreadSched's."""
    if transaction in owned:
        raise error(
            f"{prefix}.split.owned_posting",
            ("split",),
            "this transaction is maintained by BreadSched for a reimbursable expense",
        )
