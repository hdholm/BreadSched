"""``GET /api/find``: transactions across the book, sorted, grouped, totalled (#382).

Query fields are parsed by ``QueryParams``; the search, groups, and totals are the
shared ``engine.transaction_query`` the desktop's Find Transactions window and the
command line's ``find`` use. A query that cannot run is a 400 naming the problem.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..gen.engine.transaction_query import (
    DIRECTIONS,
    GROUPINGS,
    SORTS,
    QueryResult,
    TransactionQuery,
    run_query,
)
from ..gen.lib import Money

if TYPE_CHECKING:
    from .context import Api
    from .resources import QueryParams

__all__ = ["find", "find_json"]


def _amount(value: Money) -> str:
    return str(value.to_decimal())


def find_json(result: QueryResult) -> dict[str, object]:
    def row(item) -> dict[str, object]:
        return {
            "transaction": item.transaction.handle,
            "split": item.split.handle,
            "date": item.transaction.post_date.isoformat(),
            "num": item.transaction.num,
            "description": item.transaction.description,
            "account": item.split.account,
            "account_name": item.account_name,
            "memo": item.split.memo,
            "amount": _amount(item.value),
            "currency": item.currency,
            "reconcile": item.split.reconcile.value,
            "tags": list(item.transaction.tags),
        }

    return {
        "groups": [
            {
                "key": group.key,
                "rows": [row(item) for item in group.rows],
                "totals": {code: _amount(total) for code, total in group.totals.items()},
            }
            for group in result.groups
        ],
        "totals": {code: _amount(total) for code, total in result.totals.items()},
        "splits": len(result.rows),
        "transactions": result.transactions,
        "options": {"sorts": list(SORTS), "groupings": list(GROUPINGS)},
    }


def find(api: Api, query: QueryParams) -> dict[str, object]:
    accounts = query.texts("account")
    request = TransactionQuery(
        text=query.text("text") or "",
        accounts=accounts,
        subaccounts=not query.flag("no_subaccounts"),
        start=query.iso_date("start"),
        end=query.iso_date("end"),
        amount=query.money("amount"),
        amount_min=query.money("min"),
        amount_max=query.money("max"),
        direction=query.choice("direction", DIRECTIONS, "any"),
        tags=query.texts("tag"),
        reconcile=tuple(item for item in query.texts("reconcile") if item in ("n", "c", "y")),
        sort=query.choice("sort", SORTS, "date"),
        descending=query.flag("descending"),
        group_by=query.choice("group_by", GROUPINGS, "none"),
    )
    query.finish()
    for handle in accounts:
        if api.db.get_account(handle) is None:
            raise KeyError(handle)
    return find_json(run_query(api.db, request))
