"""Web holdings: each security's cost basis, lots, and gains; sale lots; realized gains.

The lots and gains are derived in ``gen/engine/cost_basis`` and
``gen/engine/realized_gains`` and read through the book server (``holdings``,
``realized_gains``); choosing a sale's lots goes through it too (``sale_lots``,
``set_sale_lots``). This adapter only parses queries and payloads and serializes,
with the same sentences every interface shows.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any, cast

from ..presentation import holding_cost_text, lot_move_text

if TYPE_CHECKING:
    from .context import Api
    from .resources import QueryParams


def holdings(api: Api, query: QueryParams) -> dict[str, object]:
    """Every security holding with its open lots, sales, and cost-basis notes."""
    raw = query.text("as_of")
    query.finish()
    try:
        as_of = date.fromisoformat(raw) if raw else None
    except ValueError:
        from .resources import QueryError  # resources imports this module

        raise QueryError("query.invalid", ("as_of",)) from None
    view = api.book.holdings(as_of)
    found = view.holdings
    return {
        "as_of": as_of,
        "charts": [chart.as_dict() for chart in view.charts],
        "holdings": [
            {
                **item.as_dict(),
                "name": view.name(item.account.handle),
                "text": holding_cost_text(item),
                "moves": [
                    {**data, "text": lot_move_text(move, view.other_name(move.other_account))}
                    for move, data in zip(
                        item.moves,
                        cast("list[dict[str, object]]", item.as_dict()["moves"]),
                        strict=True,
                    )
                ],
            }
            for item in found
        ],
    }


def _lot_data(lot) -> dict[str, object]:
    from ..gen.engine.cost_basis import shares_text
    from ..presentation import lot_text

    return {
        "lot": lot.transaction,
        "acquired": lot.acquired,
        "quantity": shares_text(lot.quantity),
        "cost": lot.cost,
        "text": lot_text(lot),
    }


def realized_gains_report(api: Api, query: QueryParams) -> dict[str, object]:
    """Every sale with the lots it took, and totals by year, for a year or all years."""
    from ..presentation import sale_text

    year = query.integer("year", minimum=1900, maximum=9999)
    account = query.text("account")
    query.finish()
    view = api.book.realized_gains(year, account)
    report = view.report
    data = report.as_dict(view.mnemonics)
    sales = cast("list[dict[str, object]]", data["sales"])
    for row, item in zip(sales, report.sales, strict=True):
        row["text"] = sale_text(item.sale)
        row["lots"] = [_lot_data(lot) for lot in item.lots]
    return {**data, "year": year, "account": account, "available_years": list(view.available_years)}


def _sale_lots_data(chosen) -> dict[str, object]:
    from ..gen.engine.cost_basis import shares_text
    from ..presentation import sale_text

    return {
        "transaction": chosen.transaction.handle,
        "split": chosen.split.handle,
        "account": chosen.account.handle,
        "method": chosen.method,
        "sale": sale_text(chosen.sale),
        "specific": chosen.sale.specific,
        "quantity": shares_text(chosen.sale.quantity),
        "picks": [
            {"lot": pick.lot, "quantity": shares_text(pick.quantity)} for pick in chosen.picks
        ],
        "offered": [_lot_data(lot) for lot in chosen.offered],
        "taken": [_lot_data(lot) for lot in chosen.sale.lots],
    }


def sale_lots_view(api: Api, query: QueryParams) -> dict[str, object]:
    """The lots a sale could name and the ones it names now."""
    transaction = query.text("transaction", required=True)
    split = query.text("split", required=True)
    query.finish()
    return _sale_lots_data(api.book.sale_lots(str(transaction), str(split)))


def sale_lots_save(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Name the lots a sale sells; an empty list returns it to the account's method."""
    from ..gen.services import SetSaleLots

    transaction = payload.get("transaction")
    split = payload.get("split")
    raw = payload.get("picks", [])
    if not isinstance(transaction, str) or not isinstance(split, str):
        raise ValueError("transaction and split are required")
    if not isinstance(raw, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("lot"), str)
        and isinstance(item.get("quantity"), (str, int))
        for item in raw
    ):
        raise ValueError("picks must be a list of {lot, quantity}")
    picks = tuple((item["lot"], str(item["quantity"])) for item in raw)
    return _sale_lots_data(api.book.set_sale_lots(SetSaleLots(transaction, split, picks)))
