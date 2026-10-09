"""Web holdings: each security's cost basis, lots, and gains; sale lots; realized gains.

The lots and gains are derived in ``gen/engine/cost_basis`` and
``gen/engine/realized_gains``; choosing a sale's lots goes through
``gen/services/lots``. This adapter only parses queries and payloads and
serializes, with the same sentences every interface shows.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any, cast

from ..gen.engine.cost_basis import holdings_charts, holdings_cost_basis
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
    found = holdings_cost_basis(api.db, as_of=as_of)
    db = api.db

    def other_name(handle: str | None) -> str | None:
        account = db.get_account(handle) if handle else None
        return db.full_name(account) if account is not None else None

    return {
        "as_of": as_of,
        "charts": [chart.as_dict() for chart in holdings_charts(db, found)],
        "holdings": [
            {
                **item.as_dict(),
                "name": db.full_name(item.account),
                "text": holding_cost_text(item),
                "moves": [
                    {**data, "text": lot_move_text(move, other_name(move.other_account))}
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
    from ..gen.engine.realized_gains import realized_gains
    from ..presentation import sale_text

    year = query.integer("year", minimum=1900, maximum=9999)
    account = query.text("account")
    query.finish()
    report = realized_gains(api.db, year=year, account=account)
    data = report.as_dict(api.db)
    sales = cast("list[dict[str, object]]", data["sales"])
    for row, item in zip(sales, report.sales, strict=True):
        row["text"] = sale_text(item.sale)
        row["lots"] = [_lot_data(lot) for lot in item.lots]
    years = sorted({sale.sold.year for sale in realized_gains(api.db, account=account).sales})
    return {**data, "year": year, "account": account, "available_years": years}


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
    from ..gen.services import sale_lots
    from .controls import service_error

    transaction = query.text("transaction", required=True)
    split = query.text("split", required=True)
    query.finish()
    result = sale_lots(api.db, str(transaction), str(split))
    if result.value is None:
        raise service_error(result.errors[0])
    return _sale_lots_data(result.value)


def sale_lots_save(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Name the lots a sale sells; an empty list returns it to the account's method."""
    from ..gen.services import SetSaleLots, set_sale_lots
    from .controls import service_error

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
    result = set_sale_lots(api.db, SetSaleLots(transaction, split, picks))
    if result.value is None:
        raise service_error(result.errors[0])
    return _sale_lots_data(result.value)
