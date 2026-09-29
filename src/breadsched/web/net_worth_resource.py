"""Web net worth history: typed query parsing over the shared read-only service.

The calculation, valuation, and missing-quote handling stay in
``gen/services/net_worth``; this adapter only parses months and serializes.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from typing import TYPE_CHECKING

from ..gen.lib.recurrence import add_months
from ..gen.services import query_net_worth_history

if TYPE_CHECKING:
    from .resources import QueryParams
    from .server import Api


def _month(text: str | None, field: str) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(f"{text}-01")
    except ValueError:
        from .resources import QueryError  # resources imports this module

        raise QueryError("query.invalid", (field,)) from None


def net_worth_history(api: Api, query: QueryParams) -> dict[str, object]:
    """Net worth at each period end, by default for the twelve months through now."""
    start = _month(query.text("from"), "from")
    through = _month(query.text("through"), "through")
    period = query.text("period") or "month"
    query.finish()
    today = date.today()
    through = through or today.replace(day=1)
    start = start or add_months(through, -11, day=1)
    end = date(through.year, through.month, monthrange(through.year, through.month)[1])
    result = query_net_worth_history(api.db, start, end, period, today)
    if result.value is None:
        raise api._service_resource_error(result.errors[0])
    history = result.value
    return {
        "start": history.start,
        "end": history.end,
        "period": history.period.value,
        "as_of": history.as_of,
        "points": [
            {
                "label": point.label,
                "start": point.start,
                "end": point.end,
                "valued_on": point.valued_on,
                "partial": point.partial,
                "assets": point.assets,
                "debts": point.debts,
                "net_worth": point.net_worth,
                "change": point.change,
                "missing": list(point.missing),
                "lines": [
                    {
                        "account": line.account,
                        "name": line.name,
                        "kind": line.kind,
                        "value": line.value,
                    }
                    for line in point.lines
                ],
            }
            for point in history.points
        ],
    }
