"""Find transactions across every register, sorted, grouped, and totalled (#382).

A register's filter searches one account. Finding things across the whole book,
and the reports built on that ("every grocery split last quarter, by month, with
totals"), use this query instead. Its rows are *splits*, since an amount, an account,
and a direction belong to one leg of a transaction, not to the whole:

- ``text`` matches anything the transaction holds, as the register filter does
  (``ledger.transaction_matches``); ``start``/``end`` bound its date and ``tags``
  require each tag. These choose transactions.
- ``accounts`` (with their subaccounts unless ``subaccounts`` is false),
  ``amount`` or ``amount_min``/``amount_max`` (on the split's size, its sign
  ignored), ``direction`` (``debit`` for money in, ``credit`` for money out, as the
  split's sign records it), and ``reconcile`` choose the splits shown.

Amounts are totalled per currency and never converted: a group of splits in two
currencies has two totals.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from ..db.sqlite import DbSQLite
from ..lib import Money, Split, Transaction
from . import ledger
from .currency import reporting_currency_handle

__all__ = [
    "DIRECTIONS",
    "GROUPINGS",
    "SORTS",
    "QueryGroup",
    "QueryResult",
    "QueryRow",
    "TransactionQuery",
    "run_query",
]

#: What a split's sign can be asked to be.
DIRECTIONS = ("any", "debit", "credit")
#: How rows can be ordered.
SORTS = ("date", "amount", "account", "description")
#: How rows can be grouped for subtotals; ``none`` gives one list and a grand total.
GROUPINGS = ("none", "account", "month", "year", "tag")


@dataclass(frozen=True, slots=True)
class TransactionQuery:
    text: str = ""
    accounts: tuple[str, ...] = ()
    subaccounts: bool = True
    start: date | None = None
    end: date | None = None
    amount: Money | None = None
    amount_min: Money | None = None
    amount_max: Money | None = None
    direction: str = "any"
    tags: tuple[str, ...] = ()
    #: Reconcile states (``n``, ``c``, ``y``) a split must have; empty for any.
    reconcile: tuple[str, ...] = ()
    sort: str = "date"
    descending: bool = False
    group_by: str = "none"

    def problems(self) -> list[str]:
        """What is wrong with the query, in words; empty when it can run."""
        found = []
        if self.direction not in DIRECTIONS:
            found.append(f"direction must be one of {', '.join(DIRECTIONS)}")
        if self.sort not in SORTS:
            found.append(f"sort must be one of {', '.join(SORTS)}")
        if self.group_by not in GROUPINGS:
            found.append(f"group by must be one of {', '.join(GROUPINGS)}")
        if self.start and self.end and self.start > self.end:
            found.append("the start date is after the end date")
        if self.amount_min is not None and self.amount_max is not None:
            if self.amount_min > self.amount_max:
                found.append("the smallest amount is larger than the largest")
        return found


@dataclass(frozen=True, slots=True)
class QueryRow:
    transaction: Transaction
    split: Split
    account_name: str
    #: The currency the split's value is in (a mnemonic such as ``USD``).
    currency: str

    @property
    def value(self) -> Money:
        return self.split.value


@dataclass(slots=True)
class QueryGroup:
    #: The group's key as shown: an account name, ``2026-03``, ``2026``, a tag, or "".
    key: str
    rows: list[QueryRow] = field(default_factory=list)
    #: Signed totals by currency mnemonic, in the order first met.
    totals: dict[str, Money] = field(default_factory=dict)

    def add(self, row: QueryRow) -> None:
        self.rows.append(row)
        self.totals[row.currency] = self.totals.get(row.currency, Money(0)) + row.value


@dataclass(slots=True)
class QueryResult:
    query: TransactionQuery
    groups: list[QueryGroup]
    totals: dict[str, Money]

    @property
    def rows(self) -> list[QueryRow]:
        return [row for group in self.groups for row in group.rows]

    @property
    def transactions(self) -> int:
        return len({row.transaction.handle for row in self.rows})


def _account_set(db: DbSQLite, query: TransactionQuery) -> set[str] | None:
    if not query.accounts:
        return None
    chosen = set(query.accounts)
    if query.subaccounts:
        for handle in query.accounts:
            chosen.update(account.handle for account in db.descendants(handle))
    return chosen


def _split_matches(split: Split, query: TransactionQuery, accounts: set[str] | None) -> bool:
    if accounts is not None and split.account not in accounts:
        return False
    size = abs(split.value)
    if query.amount is not None and size != abs(query.amount):
        return False
    if query.amount_min is not None and size < abs(query.amount_min):
        return False
    if query.amount_max is not None and size > abs(query.amount_max):
        return False
    if query.direction == "debit" and split.value <= 0:
        return False
    if query.direction == "credit" and split.value >= 0:
        return False
    return not (query.reconcile and split.reconcile.value not in query.reconcile)


def _transaction_matches(db: DbSQLite, transaction: Transaction, query: TransactionQuery) -> bool:
    if query.start is not None and transaction.post_date < query.start:
        return False
    if query.end is not None and transaction.post_date > query.end:
        return False
    if query.tags:
        held = {tag.casefold() for tag in transaction.tags}
        if not all(tag.casefold() in held for tag in query.tags):
            return False
    return ledger.transaction_matches(db, transaction, query.text)


def _sort_key(query: TransactionQuery):
    def key(row: QueryRow):
        handle = (row.transaction.post_date, row.transaction.handle, row.split.handle)
        if query.sort == "amount":
            return (row.value, *handle)
        if query.sort == "account":
            return (row.account_name.casefold(), *handle)
        if query.sort == "description":
            return (row.transaction.description.casefold(), *handle)
        return handle

    return key


def _group_keys(row: QueryRow, group_by: str) -> list[str]:
    when = row.transaction.post_date
    if group_by == "account":
        return [row.account_name]
    if group_by == "month":
        return [f"{when.year:04d}-{when.month:02d}"]
    if group_by == "year":
        return [f"{when.year:04d}"]
    if group_by == "tag":
        # A split appears under each of its transaction's tags, or once untagged.
        return sorted(row.transaction.tags, key=str.casefold) or ["(untagged)"]
    return [""]


def run_query(db: DbSQLite, query: TransactionQuery) -> QueryResult:
    """The splits ``query`` finds, grouped and sorted, with per-currency totals.

    Raises ``ValueError`` naming what is wrong with a query that cannot run.
    """
    problems = query.problems()
    if problems:
        raise ValueError("; ".join(problems))
    accounts = _account_set(db, query)
    names: dict[str, str] = {}
    mnemonics: dict[str | None, str] = {}
    reporting = reporting_currency_handle(db)

    def name(handle: str) -> str:
        if handle not in names:
            account = db.get_account(handle)
            names[handle] = db.full_name(account) if account is not None else handle
        return names[handle]

    def currency(transaction: Transaction) -> str:
        handle = transaction.currency or reporting
        if handle not in mnemonics:
            commodity = db.get_commodity(handle)
            mnemonics[handle] = commodity.mnemonic if commodity is not None else ""
        return mnemonics[handle]

    rows: list[QueryRow] = []
    for transaction in db.iter_transactions():
        if not _transaction_matches(db, transaction, query):
            continue
        for split in transaction.splits:
            if _split_matches(split, query, accounts):
                rows.append(
                    QueryRow(transaction, split, name(split.account), currency(transaction))
                )
    rows.sort(key=_sort_key(query), reverse=query.descending)

    groups: dict[str, QueryGroup] = {}
    totals: dict[str, Money] = defaultdict(lambda: Money(0))
    for row in rows:
        for key in _group_keys(row, query.group_by):
            groups.setdefault(key, QueryGroup(key)).add(row)
        totals[row.currency] = totals[row.currency] + row.value
    ordered = sorted(groups.values(), key=lambda group: group.key.casefold())
    return QueryResult(query, ordered, dict(totals))
