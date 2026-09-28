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
from .currency import reporting_currency_handle
from .payees import match_key

__all__ = [
    "ReceivableError",
    "ReceivableStatus",
    "ReceivableSummary",
    "ReimbursementProposal",
    "iter_receivables",
    "propose_reimbursements",
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


@dataclass(frozen=True, slots=True)
class ReimbursementProposal:
    """An unlinked credit that looks like money back on one open receivable."""

    receivable: str
    payer: str
    transaction: str
    split: str
    when: date
    description: str
    account: str
    #: What the credit gives back (positive).
    amount: Money
    #: The receivable's remaining balance once this and earlier proposals land.
    remaining_after: Money
    reason: str


_CLOSED = {ReceivableStatus.SETTLED, ReceivableStatus.WRITTEN_OFF}


def propose_reimbursements(
    db: DbSQLite, *, as_of: date | None = None
) -> list[ReimbursementProposal]:
    """Propose reimbursement links for credits that clearly belong to one receivable.

    A credit (a negative split) qualifies for a receivable that is still owed
    something when all of these hold:

    - it posts to an expense account one of the receivable's expense splits used;
    - it is dated on or after the receivable's incurred date;
    - it is in the same transaction currency as that expense;
    - it is no larger than what remains;
    - it is not already linked to any receivable.

    Where a credit fits several receivables, it is proposed only when the payer's
    name (by ``payees.match_key``) appears in its description and singles one out.
    Otherwise it is left alone. Credits are allocated oldest first, so proposals
    never promise more than a receivable's remaining balance. Nothing is written.
    """
    receivables = iter_receivables(db)
    linked: set[tuple[str, str]] = set()
    for receivable in receivables:
        for link in (*receivable.expenses, *receivable.reimbursements):
            linked.add((link.transaction, link.split))
    # A transaction with no recorded currency is in the book's reporting currency,
    # as the transaction service reads it.
    book_currency = reporting_currency_handle(db)

    def currency_of(transaction: Transaction) -> str:
        return transaction.currency or book_currency

    open_items: list[tuple[Receivable, set[str], set[str], str]] = []
    remaining: dict[str, Money] = {}
    for receivable in receivables:
        try:
            summary = receivable_summary(db, receivable, as_of=as_of)
            expenses = [_resolve_link(db, link) for link in receivable.expenses]
        except ReceivableError:
            continue  # a broken link is reported by the summary, not guessed around
        if summary.status in _CLOSED or summary.remaining <= 0 or not expenses:
            continue
        accounts = {split.account for _transaction, split in expenses}
        currencies = {currency_of(transaction) for transaction, _split in expenses}
        open_items.append((receivable, accounts, currencies, match_key(receivable.payer)))
        remaining[receivable.handle] = summary.remaining
    if not open_items:
        return []
    credits: list[tuple[Transaction, Split]] = []
    wanted = set().union(*(accounts for _r, accounts, _c, _k in open_items))
    for transaction in db.iter_transactions():
        for split in transaction.splits:
            if split.value < 0 and split.account in wanted:
                if (transaction.handle, split.handle) not in linked:
                    credits.append((transaction, split))
    credits.sort(key=lambda item: (item[0].post_date, item[0].handle, item[1].handle))
    proposals: list[ReimbursementProposal] = []
    for transaction, split in credits:
        amount = -split.value
        eligible = [
            item
            for item in open_items
            if split.account in item[1]
            and currency_of(transaction) in item[2]
            and transaction.post_date >= item[0].incurred_date
            and amount <= remaining[item[0].handle]
        ]
        if not eligible:
            continue
        # Whole words only: "acme insurance" is named in "acme insurance payment",
        # but "acme" is not named in "macme".
        key = f" {match_key(transaction.description)} "
        named = [item for item in eligible if item[3] and f" {item[3]} " in key]
        if len(eligible) == 1:
            chosen = eligible[0]
            reason = (
                "payer named in the description; only open receivable for this account"
                if named
                else "only open receivable for this account"
            )
        elif len(named) == 1:
            chosen = named[0]
            reason = "payer named in the description"
        else:
            continue
        receivable = chosen[0]
        remaining[receivable.handle] = remaining[receivable.handle] - amount
        proposals.append(
            ReimbursementProposal(
                receivable=receivable.handle,
                payer=receivable.payer,
                transaction=transaction.handle,
                split=split.handle,
                when=transaction.post_date,
                description=transaction.description,
                account=split.account,
                amount=amount,
                remaining_after=remaining[receivable.handle],
                reason=reason,
            )
        )
    return proposals
