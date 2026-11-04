"""``GET /api/find``: transactions across the book, sorted, grouped, totalled (#382).

Query fields are parsed by ``QueryParams``; the search, groups, and totals are the
book server's ``find`` (``services.transaction_search``), which the desktop's Find
Transactions window and the command line's ``find`` also use. A query that cannot
run is a 400 with the service's code naming the problem.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..gen.engine.transaction_query import DIRECTIONS, GROUPINGS, SORTS, TransactionQuery
from ..gen.lib import Money
from ..gen.services.transaction_search import FindResult

if TYPE_CHECKING:
    from .context import Api
    from .resources import QueryParams

__all__ = ["find", "find_json"]


def _amount(value: Money) -> str:
    return str(value.to_decimal())


def find_json(result: FindResult) -> dict[str, object]:
    def row(item) -> dict[str, object]:
        return {
            "transaction": item.transaction,
            "split": item.split,
            "date": item.date.isoformat(),
            "num": item.num,
            "description": item.description,
            "account": item.account,
            "account_name": item.account_name,
            "memo": item.memo,
            "amount": _amount(item.amount),
            "currency": item.currency,
            "reconcile": item.reconcile,
            "tags": list(item.tags),
        }

    def group(item) -> dict[str, object]:
        return {
            "key": item.key,
            "depth": item.depth,
            "rows": [row(entry) for entry in item.rows],
            "totals": {code: _amount(total) for code, total in item.totals.items()},
            # The next subtotal's groups within this one (#408).
            "children": [group(child) for child in item.children],
        }

    return {
        "groups": [group(item) for item in result.groups],
        "subtotals": list(result.query.levels),
        "totals": {code: _amount(total) for code, total in result.totals.items()},
        "splits": result.splits,
        "transactions": result.transactions,
        "options": {"sorts": list(SORTS), "groupings": list(GROUPINGS)},
    }


def find(api: Api, query: QueryParams) -> dict[str, object]:
    request = TransactionQuery(
        text=query.text("text") or "",
        accounts=query.texts("account"),
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
        # Repeated, outermost first (#408); the service refuses a repeat.
        subtotals=query.texts("subtotal"),
    )
    query.finish()
    return find_json(api.book.find(request))
