"""Typed Plan query shared by GTK, web, CLI, and contract tests."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine.activity import (
    CategoryReport,
    PlanMeasure,
    PlanSettings,
    ReportingPeriod,
    build_category_report,
)
from ..lib.scenario import Assumptions, Scenario
from .assumptions import BASE_ASSUMPTIONS_KEY
from .contracts import ServiceError, ServiceResult

BASE_SCENARIO = "__base__"


@dataclass(frozen=True, slots=True)
class PlanQuery:
    """Typed inputs for one complete Plan calculation."""

    start: date | None = None
    end: date | None = None
    period: ReportingPeriod | None = None
    measure: PlanMeasure | None = None
    scenario: str | None = None
    compare: str | None = None
    use_saved: bool = False
    baseline: Scenario | None = None
    today: date | None = None


@dataclass(frozen=True, slots=True)
class ScenarioChoice:
    handle: str | None
    name: str


@dataclass(frozen=True, slots=True)
class PlanComparison:
    scenario: ScenarioChoice
    assumption_sources: dict[str, str]
    report: CategoryReport


@dataclass(frozen=True, slots=True)
class PlanQueryResult:
    start: date
    end: date
    minimum: date
    maximum: date
    period: ReportingPeriod
    measure: PlanMeasure
    scenario: ScenarioChoice
    compare_handle: str | None
    scenarios: tuple[ScenarioChoice, ...]
    assumption_sources: dict[str, str]
    report: CategoryReport
    comparison: PlanComparison | None = None


def _month_end(when: date) -> date:
    return date(when.year, when.month, monthrange(when.year, when.month)[1])


def _previous_month(when: date) -> date:
    if when.month == 1:
        return date(when.year - 1, 12, 1)
    return date(when.year, when.month - 1, 1)


def _bounds(db: DbSQLite, today: date) -> tuple[date, date]:
    dates: list[date] = []
    dates.extend(transaction.post_date for transaction in db.iter_transactions())
    dates.extend(schedule.recurrence.start for schedule in db.iter_scheduled())
    for scenario in db.iter_scenarios():
        dates.extend(item.recurrence.start for item in scenario.schedule_overrides)
        dates.extend(item.when for item in scenario.one_offs)
    minimum = min(dates, default=date(today.year, 1, 1)).replace(day=1)
    anniversary_year = today.year + 150
    anniversary = date(
        anniversary_year,
        today.month,
        min(today.day, monthrange(anniversary_year, today.month)[1]),
    )
    maximum = anniversary.replace(day=1)
    if _month_end(maximum) > anniversary:
        maximum = _previous_month(maximum)
    return minimum, maximum


def _baseline(db: DbSQLite, start: date, end: date) -> Scenario:
    scenario = Scenario(
        name="Base scenario",
        start=start,
        years=max(1, end.year - start.year + 1),
    )
    stored = db.get_metadata(BASE_ASSUMPTIONS_KEY, None)
    if isinstance(stored, dict):
        scenario.assumptions = Assumptions.from_dict(stored)
    return scenario


def query_plan(db: DbSQLite, request: PlanQuery) -> ServiceResult[PlanQueryResult]:
    """Validate selection state and calculate the primary and comparison reports."""
    today = request.today or date.today()
    minimum, maximum_month = _bounds(db, today)
    maximum = _month_end(maximum_month)
    defaults = PlanSettings.load(
        db,
        date(today.year, 1, 1),
        date(today.year + 1, 12, 31),
    )
    if request.use_saved:
        start = defaults.start
        end = defaults.end
        period = defaults.period
        measure = defaults.measure
        scenario_handle = defaults.scenario
        compare_handle = defaults.compare
    else:
        start = request.start or max(date(today.year, 1, 1), minimum)
        end = request.end or min(date(today.year + 1, 12, 31), maximum)
        period = request.period or ReportingPeriod.MONTH
        measure = request.measure or PlanMeasure.PLANNED
        scenario_handle = request.scenario
        compare_handle = request.compare

    if request.use_saved:
        start = max(start, minimum)
        end = min(end, maximum)
        if end < start:
            end = _month_end(start)

    errors: list[ServiceError] = []
    if start < minimum:
        errors.append(ServiceError("plan.start.before_book_data", ("start",)))
    if end < start:
        errors.append(ServiceError("plan.end.before_start", ("end", "start")))
    if end > maximum:
        errors.append(ServiceError("plan.end.after_maximum", ("end",)))
    if errors:
        return ServiceResult.failure(*errors)

    saved_scenarios = tuple(db.iter_scenarios())
    choices = (
        ScenarioChoice(None, "Base scenario"),
        *(ScenarioChoice(item.handle, item.name) for item in saved_scenarios),
    )
    selected = next(
        (item for item in saved_scenarios if item.handle == scenario_handle),
        None,
    )
    if scenario_handle and selected is None and not request.use_saved:
        return ServiceResult.failure(ServiceError("plan.scenario.not_found", ("scenario",)))
    if selected is None:
        scenario_handle = None
        scenario = request.baseline or _baseline(db, start, end)
        scenario_choice = choices[0]
    else:
        scenario = selected
        scenario_choice = ScenarioChoice(selected.handle, selected.name)

    report = build_category_report(db, start, end, period=period, scenario=scenario)
    if (
        request.use_saved
        and compare_handle not in {None, BASE_SCENARIO}
        and not any(item.handle == compare_handle for item in saved_scenarios)
    ):
        compare_handle = None

    comparison = None
    if compare_handle is not None:
        if compare_handle == BASE_SCENARIO:
            compare_scenario = request.baseline or _baseline(db, start, end)
            compare_choice = choices[0]
        else:
            selected_compare = next(
                (item for item in saved_scenarios if item.handle == compare_handle),
                None,
            )
            if selected_compare is None:
                return ServiceResult.failure(
                    ServiceError("plan.comparison.not_found", ("compare",))
                )
            compare_scenario = selected_compare
            compare_choice = ScenarioChoice(selected_compare.handle, selected_compare.name)
        if compare_choice.handle == scenario_handle:
            return ServiceResult.failure(ServiceError("plan.comparison.same", ("compare",)))
        compare_report = build_category_report(
            db,
            start,
            end,
            period=period,
            scenario=compare_scenario,
        )
        comparison = PlanComparison(
            compare_choice,
            compare_scenario.assumption_sources(start),
            compare_report,
        )

    return ServiceResult.success(
        PlanQueryResult(
            start=start,
            end=end,
            minimum=minimum,
            maximum=maximum_month,
            period=period,
            measure=measure,
            scenario=scenario_choice,
            compare_handle=compare_handle,
            scenarios=choices,
            assumption_sources=scenario.assumption_sources(start),
            report=report,
            comparison=comparison,
        )
    )
