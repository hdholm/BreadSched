"""Web holdings: each security's cost basis, lots, and gains, read-only.

The lots and gains are derived in ``gen/engine/cost_basis``; this adapter only
parses the optional as-of date and serializes, with the same sentence every
interface shows.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, cast

from ..gen.engine.cost_basis import holdings_cost_basis
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
