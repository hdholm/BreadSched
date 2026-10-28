"""Find transactions as one typed result every interface reads alike (#382, #358).

``engine.transaction_query`` finds the splits; this service checks the query,
refusing one that cannot run with stable codes, and returns the rows flattened to
what is shown (dates, names, amounts, and currencies), so the book server can carry
the result to the browser, the command line, and the desktop unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine.transaction_query import (
    DIRECTIONS,
    GROUPINGS,
    SORTS,
    QueryRow,
    TransactionQuery,
    run_query,
)
from ..lib import Money
from .contracts import ServiceError, ServiceResult

__all__ = ["FindResult", "FoundGroup", "FoundSplit", "find_transactions"]


@dataclass(frozen=True, slots=True)
class FoundSplit:
    """One found split, as a row shows it."""

    transaction: str
    split: str
    date: date
    num: str
    description: str
    account: str
    account_name: str
    memo: str
    amount: Money
    #: The transaction's currency mnemonic, such as ``USD``.
    currency: str
    reconcile: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FoundGroup:
    #: An account name, ``2026-03``, ``2026``, a tag, or "" when not grouped.
    key: str
    rows: tuple[FoundSplit, ...] = ()
    #: Signed totals by currency mnemonic.
    totals: dict[str, Money] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FindResult:
    query: TransactionQuery
    groups: tuple[FoundGroup, ...] = ()
    totals: dict[str, Money] = field(default_factory=dict)
    #: How many splits, and how many transactions they belong to.
    splits: int = 0
    transactions: int = 0

    @property
    def rows(self) -> list[FoundSplit]:
        return [row for group in self.groups for row in group.rows]


def _found(row: QueryRow) -> FoundSplit:
    return FoundSplit(
        transaction=row.transaction.handle,
        split=row.split.handle,
        date=row.transaction.post_date,
        num=row.transaction.num,
        description=row.transaction.description,
        account=row.split.account,
        account_name=row.account_name,
        memo=row.split.memo,
        amount=row.value,
        currency=row.currency,
        reconcile=row.split.reconcile.value,
        tags=tuple(row.transaction.tags),
    )


def _errors(db: DbSQLite, query: TransactionQuery) -> list[ServiceError]:
    found = []
    if query.direction not in DIRECTIONS:
        found.append(ServiceError("find.direction.invalid", ("direction",)))
    if query.sort not in SORTS:
        found.append(ServiceError("find.sort.invalid", ("sort",)))
    if query.group_by not in GROUPINGS:
        found.append(ServiceError("find.group_by.invalid", ("group_by",)))
    if query.start and query.end and query.start > query.end:
        found.append(ServiceError("find.dates.order", ("start", "end")))
    if query.amount_min is not None and query.amount_max is not None:
        if query.amount_min > query.amount_max:
            found.append(ServiceError("find.amounts.order", ("min", "max")))
    if any(db.get_account(handle) is None for handle in query.accounts):
        found.append(ServiceError("find.account.missing", ("account",)))
    return found


def find_transactions(db: DbSQLite, query: TransactionQuery) -> ServiceResult[FindResult]:
    """The splits ``query`` finds, grouped, with per-currency totals; never writes."""
    errors = _errors(db, query)
    if errors:
        return ServiceResult.failure(*errors)
    result = run_query(db, query)
    groups = tuple(
        FoundGroup(group.key, tuple(_found(row) for row in group.rows), dict(group.totals))
        for group in result.groups
    )
    return ServiceResult.success(
        FindResult(
            query=query,
            groups=groups,
            totals=dict(result.totals),
            splits=len(result.rows),
            transactions=result.transactions,
        )
    )
