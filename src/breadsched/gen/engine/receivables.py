"""Reimbursable expenses: status derived from linked ledger splits.

A receivable's status is never stored; it is always recomputed from the
expense and reimbursement splits it references, plus any recorded write-off
or dispute, so it can never drift from the ledger it describes. A
reimbursement split is never counted as income and an expense split is never
rewritten -- see ``lib.receivable`` for why both are ordinary expense-class
splits.

What is owed is the expected amount, or the whole linked expense when none is
given, and never more than the linked expense. ``planned_postings`` derives the
BreadSched-owned reclassification transactions that hold it in the receivable
account (issue #170):

- tracking: receivable +owed, expense -owed, on the incurred date;
- each reimbursement: expense +amount, receivable -amount, on its date;
- each write-off: expense +amount, receivable -amount, on its date.

Reimbursements and write-offs are applied oldest first and never take the
receivable below zero; money back beyond what was owed simply stays a refund in
the expense account. So the receivable account holds exactly what is still
owed, which counts toward net worth but never toward liquidity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from uuid import NAMESPACE_URL, uuid5

from ..db.sqlite import DbSQLite
from ..lib.account import AccountType
from ..lib.money import Money
from ..lib.receivable import Receivable, ReceivableSplitLink, ReceivableWriteOff
from ..lib.transaction import Split, Transaction
from . import split_links
from .currency import commodity_fraction, reporting_currency_handle
from .payees import match_key

__all__ = [
    "ReceivableError",
    "ReceivableStatus",
    "ReceivableSummary",
    "ReimbursementProposal",
    "fsa_overlaps",
    "iter_receivables",
    "owned_postings",
    "planned_postings",
    "posting_currency",
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
    #: What the payer owes: the expected amount, or the whole linked expense
    #: when none is given, never more than the linked expense.
    owed: Money
    #: Sum credited back by the linked reimbursement splits (always >= 0).
    reimbursed: Money
    #: Sum recorded as given up (always >= 0).
    written_off: Money
    #: What is still expected; negative when reimbursed more than was owed.
    remaining: Money
    age_days: int
    status: ReceivableStatus
    #: FSA claims that also claim one of the linked expense splits (a warning).
    fsa_claims: tuple[str, ...] = ()


def iter_receivables(db: DbSQLite) -> list[Receivable]:
    return list(db.iter_receivables())


def _missing_link(part: str) -> ReceivableError:
    return ReceivableError(
        f"receivable.link.{part}.not_found",
        ("links",),
        f"linked transaction{' split' if part == 'split' else ''} no longer exists",
    )


def _resolve_link(db: DbSQLite, link: ReceivableSplitLink) -> tuple[Transaction, Split]:
    return split_links.resolve_link(db, link, _missing_link)


def _sum_links(db: DbSQLite, links: list[ReceivableSplitLink]) -> Money:
    return split_links.sum_links(db, links, _missing_link)


def _owed(receivable: Receivable, expense_total: Money) -> Money:
    """The expected amount (or the whole expense), never more than the expense."""
    if receivable.expected_amount is None:
        return expense_total
    return min(receivable.expected_amount, expense_total)


def receivable_summary(
    db: DbSQLite, receivable: Receivable, *, as_of: date | None = None
) -> ReceivableSummary:
    """The receivable's current standing, always recomputed from linked splits."""
    expense_total = _sum_links(db, receivable.expenses)
    reimbursed = _sum_links(db, receivable.reimbursements)
    written_off = Money(0)
    for item in receivable.write_offs:
        written_off = written_off + item.amount
    owed = _owed(receivable, expense_total)
    remaining = owed - reimbursed - written_off
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
        owed=owed,
        reimbursed=reimbursed,
        written_off=written_off,
        remaining=remaining,
        age_days=age_days,
        status=status,
        fsa_claims=fsa_overlaps(db, receivable),
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
    owned = owned_postings(db)
    wanted = set().union(*(accounts for _r, accounts, _c, _k in open_items))
    for transaction in db.iter_transactions():
        if transaction.handle in owned:
            continue  # BreadSched's own reclassification, never a reimbursement
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


# ---------------------------------------------------------------- postings

#: The note on every BreadSched-owned reclassification transaction.
POSTING_NOTE = (
    "Maintained by BreadSched for a reimbursable expense. Change the receivable "
    "instead; this transaction is recomputed from it."
)


def owned_postings(db: DbSQLite) -> set[str]:
    """Handles of every reclassification transaction a receivable owns."""
    return {handle for receivable in db.iter_receivables() for handle in receivable.postings}


def posting_currency(db: DbSQLite, receivable: Receivable) -> str | None:
    """The one transaction currency of the linked splits; ``None`` if nothing is linked.

    Raises when the linked splits are in different currencies, because a
    receivable account holds one currency and no conversion is guessed.
    """
    book = reporting_currency_handle(db)
    found: set[str] = set()
    for link in (*receivable.expenses, *receivable.reimbursements):
        transaction, _split = _resolve_link(db, link)
        found.add(transaction.currency or book)
    if len(found) > 1:
        raise ReceivableError(
            "receivable.currency.mixed",
            ("links",),
            "linked expense and reimbursement splits are in different currencies",
        )
    return next(iter(found), None)


def _posting_handle(receivable: Receivable, *parts: object) -> str:
    text = "|".join(str(part) for part in (receivable.handle, *parts))
    return uuid5(NAMESPACE_URL, f"breadsched:receivable-posting:{text}").hex


def _quantity(value: Money, source: Split) -> Money:
    """``value`` in the account's commodity, at the rate ``source`` recorded."""
    if source.quantity == source.value or not source.value:
        return value
    return value * (source.quantity / source.value)


def planned_postings(db: DbSQLite, receivable: Receivable) -> list[Transaction]:
    """The reclassification transactions ``receivable`` should own right now.

    Nothing is posted for a receivable without an account (recorded before
    issue #170) or without a linked expense. Raises ``ReceivableError`` when the
    account is missing, is not a receivable account, or holds another currency.
    """
    if receivable.account is None or not receivable.expenses:
        return []
    account = db.get_account(receivable.account)
    if account is None:
        raise ReceivableError("receivable.account.not_found", ("account",), "no such account")
    if account.atype is not AccountType.RECEIVABLE:
        raise ReceivableError(
            "receivable.account.not_receivable",
            ("account",),
            "a receivable is held in a Receivable account",
        )
    currency = posting_currency(db, receivable)
    book = reporting_currency_handle(db)
    if (account.commodity or book) != currency:
        raise ReceivableError(
            "receivable.account.currency_mismatch",
            ("account",),
            "the receivable account holds another currency than the linked splits",
        )
    expenses = [_resolve_link(db, link) for link in receivable.expenses]
    expense_total = Money(0)
    for _transaction, split in expenses:
        expense_total = expense_total + split.value
    owed = _owed(receivable, expense_total)
    raw_currency = expenses[0][0].currency
    fraction = commodity_fraction(db, currency)
    largest = max(expenses, key=lambda item: item[1].value)[1]

    def posting(
        key: object, when: date, description: str, lines: list[tuple[str, Money, Split | None]]
    ) -> Transaction:
        transaction = Transaction(
            handle=_posting_handle(receivable, key),
            post_date=when,
            description=description,
            currency=raw_currency,
        )
        for index, (target, value, source) in enumerate(lines):
            transaction.add_split(
                Split(
                    target,
                    value,
                    quantity=_quantity(value, source) if source is not None else None,
                    memo=receivable.description,
                    handle=_posting_handle(receivable, key, index),
                )
            )
        transaction.notes = POSTING_NOTE
        return transaction

    postings: list[Transaction] = []
    if owed > 0:
        shares: list[tuple[str, Money, Split | None]] = []
        allocated = Money(0)
        for index, (_transaction, split) in enumerate(expenses):
            if owed == expense_total:
                share = split.value
            elif index == len(expenses) - 1:
                share = owed - allocated
            else:
                share = (owed * (split.value / expense_total)).quantize(fraction)
            allocated = allocated + share
            if share:
                shares.append((split.account, -share, split))
        postings.append(
            posting(
                "tracking",
                receivable.incurred_date,
                f"Reimbursable from {receivable.payer}",
                [(receivable.account, owed, None), *shares],
            )
        )

    events: list[tuple[date, int, int, object]] = []
    for index, link in enumerate(receivable.reimbursements):
        transaction, _split = _resolve_link(db, link)
        events.append((transaction.post_date, 0, index, link))
    for index, item in enumerate(receivable.write_offs):
        events.append((item.written_off_on, 1, index, item))
    outstanding = owed
    for when, kind, index, event in sorted(events, key=lambda item: item[:3]):
        if outstanding <= 0:
            break
        if kind == 0:
            assert isinstance(event, ReceivableSplitLink)
            _transaction, split = _resolve_link(db, event)
            amount = min(-split.value, outstanding)
            expense_account, source = split.account, split
            key: object = ("reimbursement", event.transaction, event.split)
            description = f"Reimbursement from {receivable.payer} received"
        else:
            assert isinstance(event, ReceivableWriteOff)
            amount = min(event.amount, outstanding)
            expense_account, source = largest.account, largest
            key = ("write-off", index)
            description = f"Reimbursement from {receivable.payer} written off"
        if amount <= 0:
            continue
        outstanding = outstanding - amount
        postings.append(
            posting(
                key,
                when,
                description,
                [(expense_account, amount, source), (receivable.account, -amount, None)],
            )
        )
    return postings


def fsa_overlaps(db: DbSQLite, receivable: Receivable) -> tuple[str, ...]:
    """FSA claims that also claim one of the receivable's expense splits.

    The same cost expected back from both a payer and the FSA is usually a
    mistake, but not always (a partial claim on each), so this only warns. A
    claim linked to this receivable to cover the rest of the expense (issue
    #192) is an explicit allocation, not an overlap.
    """
    linked = {(link.transaction, link.split) for link in receivable.expenses}
    if not linked:
        return ()
    return tuple(
        claim.handle
        for claim in db.iter_fsa_claims()
        if claim.receivable != receivable.handle
        and any((link.transaction, link.split) in linked for link in claim.payments)
    )
