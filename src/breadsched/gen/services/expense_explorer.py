"""Read-only expense exploration over the shared Plan calculation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine.activity import (
    CategoryActualDetail,
    CategoryPlannedDetail,
    PlanMeasure,
    explain_category_period,
)
from ..lib.account import AccountClass
from ..lib.money import Money
from .contracts import ServiceError, ServiceResult
from .plan import PlanQuery, PlanQueryResult, _baseline, query_plan


@dataclass(frozen=True, slots=True)
class ExpensePeriod:
    start: date
    end: date
    label: str
    planned: Money
    actual: Money
    variance: Money | None


@dataclass(frozen=True, slots=True)
class ExpenseCategory:
    account: str
    name: str
    full_name: str
    depth: int
    periods: tuple[ExpensePeriod, ...]


@dataclass(frozen=True, slots=True)
class MerchantActual:
    """One contribution; keep transaction identity for the drilldown."""

    transaction: str
    post_date: date
    description: str
    amount: Money


@dataclass(frozen=True, slots=True)
class MerchantGroup:
    name: str
    amount: Money
    transactions: tuple[MerchantActual, ...]


@dataclass(frozen=True, slots=True)
class ExpenseDrilldown:
    account: str
    period: ExpensePeriod
    planned_events: tuple[CategoryPlannedDetail, ...]
    actual_transactions: tuple[CategoryActualDetail, ...]
    merchants: tuple[MerchantGroup, ...]


@dataclass(frozen=True, slots=True)
class ExpenseExplorer:
    plan: PlanQueryResult
    categories: tuple[ExpenseCategory, ...]
    totals: tuple[ExpensePeriod, ...]
    drilldown: ExpenseDrilldown | None


def _merchant_name(description: str) -> str:
    return description.strip() or "Unknown merchant"


def query_expense_explorer(
    db: DbSQLite,
    request: PlanQuery,
    *,
    account: str | None = None,
    period_index: int | None = None,
) -> ServiceResult[ExpenseExplorer]:
    """Reuse Plan periods and category values; optionally explain one category cell.

    Merchant groups exist only in this result. Their totals are actual amounts;
    the category plan is deliberately never apportioned between merchants.
    """
    result = query_plan(db, request)
    if result.value is None:
        return ServiceResult.failure(*result.errors)
    plan = result.value
    buckets = plan.report.activity.periods
    categories = tuple(
        ExpenseCategory(
            row.account,
            row.name,
            row.full_name,
            row.depth,
            tuple(
                ExpensePeriod(bucket.start, bucket.end, bucket.label, planned, actual, variance)
                for bucket, planned, actual, variance in zip(
                    buckets, row.planned, row.actual, row.variance, strict=True
                )
            ),
        )
        for row in plan.report.expenses
    )
    totals = tuple(
        ExpensePeriod(
            bucket.start,
            bucket.end,
            bucket.label,
            planned or Money(0),
            actual or Money(0),
            variance,
        )
        for bucket, planned, actual, variance in zip(
            buckets,
            plan.report.category_totals(AccountClass.EXPENSE, PlanMeasure.PLANNED),
            plan.report.category_totals(AccountClass.EXPENSE, PlanMeasure.ACTUAL),
            plan.report.category_totals(AccountClass.EXPENSE, PlanMeasure.VARIANCE),
            strict=True,
        )
    )
    drilldown = None
    if account is not None or period_index is not None:
        selected = next((item for item in categories if item.account == account), None)
        if selected is None or period_index is None or not 0 <= period_index < len(buckets):
            return ServiceResult.failure(
                ServiceError("expense.selection.invalid", ("account", "period_index"))
            )
        bucket = buckets[period_index]
        scenario = (
            next(
                (item for item in db.iter_scenarios() if item.handle == plan.scenario.handle), None
            )
            if plan.scenario.handle is not None
            else (request.baseline or _baseline(db, plan.start, plan.end))
        )
        detail = explain_category_period(
            db,
            selected.account,
            bucket.start,
            bucket.end,
            scenario=scenario,
            as_of=plan.report.as_of,
        )
        grouped: dict[str, list[CategoryActualDetail]] = {}
        for actual in detail.actual_transactions:
            grouped.setdefault(_merchant_name(actual.description).casefold(), []).append(actual)
        merchants = tuple(
            MerchantGroup(
                min(_merchant_name(item.description) for item in items),
                sum((item.amount for item in items), Money(0)),
                tuple(
                    MerchantActual(item.transaction, item.post_date, item.description, item.amount)
                    for item in items
                ),
            )
            for _, items in sorted(grouped.items())
        )
        if (
            sum((item.amount for item in merchants), Money(0))
            != selected.periods[period_index].actual
        ):
            raise AssertionError("merchant actuals do not reconcile to Plan")
        if (detail.planned, detail.actual) != (
            selected.periods[period_index].planned,
            selected.periods[period_index].actual,
        ):
            raise AssertionError("expense detail does not reconcile to Plan")
        drilldown = ExpenseDrilldown(
            selected.account,
            selected.periods[period_index],
            detail.planned_events,
            detail.actual_transactions,
            merchants,
        )
    return ServiceResult.success(ExpenseExplorer(plan, categories, totals, drilldown))
