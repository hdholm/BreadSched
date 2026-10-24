"""Read-only Budget jars: each jar's fills, draws, and level by period and account.

The jars are derived in ``engine/budget_jars``. The view carries that report whole
with the mnemonics of its currencies, so an interface lays it out (and draws its
charts) without reading the book itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import budget_jars as jars_engine
from ..engine.activity import ReportingPeriod
from ..engine.budget_jars import JarsReport
from ..engine.chart_model import ChartModel
from .contracts import ServiceError, ServiceResult
from .investment_reports import commodity_mnemonics

__all__ = ["JarsView", "jars_view"]


@dataclass(frozen=True, slots=True)
class JarsView:
    report: JarsReport
    #: Mnemonics of the report's currencies, by handle.
    mnemonics: Mapping[str, str]

    def labels(self) -> dict[str | None, str]:
        return jars_engine.currency_labels(self.report, self.mnemonics)

    def charts(self) -> tuple[ChartModel, ...]:
        return jars_engine.jar_charts(self.report, self.labels())


def jars_view(
    db: DbSQLite,
    start: date,
    end: date,
    period: ReportingPeriod | str = ReportingPeriod.MONTH,
    today: date | None = None,
) -> ServiceResult[JarsView]:
    """Every jar from ``start`` through ``end``, grouped by ``period``."""
    if end < start:
        return ServiceResult.failure(ServiceError("jars.range.invalid", ("start", "end")))
    try:
        grouping = ReportingPeriod(period)
    except ValueError:
        return ServiceResult.failure(ServiceError("jars.period.invalid", ("period",)))
    report = jars_engine.budget_jars(db, start, end, period=grouping, today=today)
    handles = {bundle.currency for bundle in report.accounts if bundle.currency}
    return ServiceResult.success(JarsView(report, commodity_mnemonics(db, handles)))
