"""Reimbursable expenses: status derived from linked ledger splits.

A receivable's status is never stored; it is always recomputed from the
expense and reimbursement splits it references, plus any recorded write-off
or dispute, so it can never drift from the ledger it describes. A
reimbursement split is never counted as income and an expense split is never
rewritten -- see ``lib.receivable`` for why both are ordinary expense-class
splits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from ..db.sqlite import DbSQLite
from ..lib.money import Money
from ..lib.receivable import Receivable, ReceivableSplitLink
from ..lib.transaction import Split, Transaction

__all__ = [
    "ReceivableError",
    "ReceivableStatus",
    "ReceivableSummary",
    "iter_receivables",
    "receivable_summary",
]


class ReceivableError(ValueError):
    """A stable receivable failure independent of presentation wording."""

    def __init__(self, code: str, fields: tuple[str, ...], message: str) -> None:
        super().__init__(message)
        self.code = code
        self.fields = fields


class ReceivableStatus(str, Enum):
    OPEN = "open"
    PARTIAL = "partial"
    DISPUTED = "disputed"
    WRITTEN_OFF = "written_off"
    SETTLED = "settled"

    @property
    def label(self) -> str:
        return {
            ReceivableStatus.OPEN: "Open",
            ReceivableStatus.PARTIAL: "Partially reimbursed",
            ReceivableStatus.DISPUTED: "Disputed",
            ReceivableStatus.WRITTEN_OFF: "Written off",
            ReceivableStatus.SETTLED: "Settled",
        }[self]


@dataclass(frozen=True)
class ReceivableSummary:
    receivable: Receivable
    #: Sum of the linked expense splits (always >= 0).
    expense_total: Money
    #: Sum credited back by the linked reimbursement splits (always >= 0).
    reimbursed: Money
    #: Sum recorded as given up (always >= 0).
    written_off: Money
    #: What is still expected; negative when reimbursed more than the expense.
    remaining: Money
    age_days: int
    status: ReceivableStatus


def iter_receivables(db: DbSQLite) -> list[Receivable]:
    return list(db.iter_receivables())


def _resolve_link(db: DbSQLite, link: ReceivableSplitLink) -> tuple[Transaction, Split]:
    transaction = db.get_transaction(link.transaction)
    if transaction is None:
        raise ReceivableError(
            "receivable.link.transaction.not_found",
            ("links",),
            "linked transaction no longer exists",
        )
    split = next((item for item in transaction.splits if item.handle == link.split), None)
    if split is None:
        raise ReceivableError(
            "receivable.link.split.not_found",
            ("links",),
            "linked transaction split no longer exists",
        )
    return transaction, split


def _sum_links(db: DbSQLite, links: list[ReceivableSplitLink]) -> Money:
    total = Money(0)
    for link in links:
        _transaction, split = _resolve_link(db, link)
        total = total + abs(split.value)
    return total


def receivable_summary(
    db: DbSQLite, receivable: Receivable, *, as_of: date | None = None
) -> ReceivableSummary:
    """The receivable's current standing, always recomputed from linked splits."""
    expense_total = _sum_links(db, receivable.expenses)
    reimbursed = _sum_links(db, receivable.reimbursements)
    written_off = Money(0)
    for item in receivable.write_offs:
        written_off = written_off + item.amount
    remaining = expense_total - reimbursed - written_off
    today = as_of or date.today()
    age_days = (today - receivable.incurred_date).days

    # A write-off is a deliberate, terminal decision to stop collecting, so its
    # presence decides the closed state even when some money already came
    # back; only its absence lets a full reimbursement read as "settled".
    if written_off > 0 and remaining <= 0:
        status = ReceivableStatus.WRITTEN_OFF
    elif remaining <= 0 and expense_total > 0:
        status = ReceivableStatus.SETTLED
    elif receivable.disputed_on is not None and remaining > 0:
        status = ReceivableStatus.DISPUTED
    elif reimbursed > 0:
        status = ReceivableStatus.PARTIAL
    else:
        status = ReceivableStatus.OPEN

    return ReceivableSummary(
        receivable=receivable,
        expense_total=expense_total,
        reimbursed=reimbursed,
        written_off=written_off,
        remaining=remaining,
        age_days=age_days,
        status=status,
    )
