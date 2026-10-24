"""Read-only plan versus actuals over a horizon, grouped only for display.

The report is derived in ``engine/activity``; the view carries it whole with the
currency notes that disclose its quotes and exclusions, so an interface shows both
without reading the book itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import category_report
from ..engine.activity import ActivityReport, ReportingPeriod, build_activity_report
from .contracts import ServiceError, ServiceResult

__all__ = ["ActivityView", "activity_view"]


@dataclass(frozen=True, slots=True)
class ActivityView:
    report: ActivityReport
    #: The quotes the totals used and the foreign activity they left out.
    currency_notes: tuple[str, ...]


def activity_view(
    db: DbSQLite,
    start: date,
    end: date,
    period: ReportingPeriod | str = ReportingPeriod.MONTH,
    as_of: date | None = None,
) -> ServiceResult[ActivityView]:
    """Planned occurrences and actual ledger activity from ``start`` through ``end``."""
    if end < start:
        return ServiceResult.failure(ServiceError("activity.range.invalid", ("start", "end")))
    try:
        grouping = ReportingPeriod(period)
    except ValueError:
        return ServiceResult.failure(ServiceError("activity.period.invalid", ("period",)))
    report = build_activity_report(db, start, end, period=grouping, as_of=as_of)
    return ServiceResult.success(ActivityView(report, category_report.currency_notes(db, report)))
