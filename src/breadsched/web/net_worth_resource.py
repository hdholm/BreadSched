"""Web net worth history: typed query parsing over the shared read-only service.

The calculation, valuation, and missing-quote handling stay in
``gen/services/net_worth``; this adapter only parses months and serializes.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from typing import TYPE_CHECKING

from ..gen.engine.currency import reporting_currency_label
from ..gen.lib.recurrence import add_months
from ..gen.services import net_worth_charts, query_net_worth_change, query_net_worth_history
from ..plugins.export.csv_export import net_worth_change_csv
from .controls import service_error

if TYPE_CHECKING:
    from .context import Api
    from .resources import QueryParams


def _month(text: str | None, field: str) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(f"{text}-01")
    except ValueError:
        from .resources import QueryError  # resources imports this module

        raise QueryError("query.invalid", (field,)) from None


def _day(text: str | None, field: str) -> date:
    try:
        return date.fromisoformat(text or "")
    except ValueError:
        from .resources import QueryError

        raise QueryError("query.invalid", (field,)) from None


def net_worth_change(api: Api, query: QueryParams) -> dict[str, object]:
    """The postings behind one net worth change, with the CSV the page downloads."""
    start = _day(query.text("from", required=True), "from")
    through = _day(query.text("through", required=True), "through")
    query.finish()
    result = query_net_worth_change(api.db, start, through, date.today())
    if result.value is None:
        raise service_error(result.errors[0])
    change = result.value
    return {
        "start": change.start,
        "end": change.end,
        "opening_on": change.opening_on,
        "closing_on": change.closing_on,
        "partial": change.partial,
        "opening": change.opening,
        "closing": change.closing,
        "change": change.change,
        "posted": change.posted,
        "revaluation": change.revaluation,
        "transfers": change.transfers,
        "missing": list(change.missing),
        "completeness": change.completeness.as_dict(),
        "postings": [
            {
                "transaction": posting.transaction,
                "posted": posting.posted,
                "description": posting.description,
                "accounts": list(posting.accounts),
                "currency": posting.currency,
                "effect": posting.effect,
            }
            for posting in change.postings
        ],
        "csv": net_worth_change_csv(change),
    }


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
        raise service_error(result.errors[0])
    history = result.value
    currency = reporting_currency_label(api.db)
    return {
        "currency": currency,
        "charts": [chart.as_dict() for chart in net_worth_charts(history, currency)],
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
                "completeness": point.completeness.as_dict(),
                "lines": [
                    {
                        "account": line.account,
                        "name": line.name,
                        "kind": line.kind,
                        "value": line.value,
                    }
                    for line in point.lines
                ],
                "groups": [
                    {
                        "account": line.account,
                        "name": line.name,
                        "kind": line.kind,
                        "value": line.value,
                    }
                    for line in point.groups
                ],
            }
            for point in history.points
        ],
    }
