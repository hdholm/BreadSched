"""Web budget jars: each schedule and goal, filled from income and drawn by actuals.

The jars are derived in ``gen/engine/budget_jars``; this adapter parses the month
range and grouping and serializes the report.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from typing import TYPE_CHECKING

from ..gen.engine.activity import ReportingPeriod
from ..gen.engine.budget_jars import JarPeriod, budget_jars, currency_labels, jar_kind_label

if TYPE_CHECKING:
    from .context import Api
    from .resources import QueryParams


def _month(raw: str | None, name: str) -> date | None:
    if raw is None:
        return None
    try:
        return date.fromisoformat(f"{raw}-01")
    except ValueError:
        from .resources import QueryError  # resources imports this module

        raise QueryError("query.invalid", (name,)) from None


def _period(item: JarPeriod) -> dict[str, object]:
    return {
        "start": item.start,
        "end": item.end,
        "label": item.label,
        "filled": item.filled,
        "planned": item.planned,
        "actual": item.actual,
        "variance": item.variance,
        "level": item.level,
    }


def jars_report(api: Api, query: QueryParams) -> dict[str, object]:
    """Jars from ``from`` through ``through`` (months), grouped by ``period``."""
    from .resources import QueryError

    first = _month(query.text("from"), "from")
    last = _month(query.text("through"), "through")
    raw_period = query.text("period") or ReportingPeriod.MONTH.value
    query.finish()
    try:
        period = ReportingPeriod(raw_period)
    except ValueError:
        raise QueryError("query.invalid", ("period",)) from None
    today = date.today()
    start = first or date(today.year, today.month, 1)
    through = last or start
    end = date(through.year, through.month, monthrange(through.year, through.month)[1])
    if end < start:
        raise QueryError("query.invalid", ("through",))
    report = budget_jars(api.db, start, end, period=period, today=today)
    labels = currency_labels(api.db, report)
    return {
        "from": start.strftime("%Y-%m"),
        "through": end.strftime("%Y-%m"),
        "period": period.value,
        "labels": [label for _first, _last, label in report.labels],
        "accounts": [
            {
                "account": bundle.account,
                "name": bundle.account_name,
                "currency": labels.get(bundle.currency, ""),
                "periods": [_period(item) for item in bundle.periods],
                "jars": [
                    {
                        "key": jar.key,
                        "kind": jar.kind,
                        "kind_label": jar_kind_label(jar.kind),
                        "name": jar.name,
                        "opening": jar.opening,
                        "periods": [_period(item) for item in jar.periods],
                    }
                    for jar in bundle.jars
                ],
            }
            for bundle in report.accounts
        ],
        "totals": [
            {
                "currency": labels.get(total.currency, ""),
                "periods": [_period(item) for item in total.periods],
            }
            for total in report.totals
        ],
        "problems": list(report.problems),
    }
