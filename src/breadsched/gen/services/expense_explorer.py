"""Read-only expense exploration over the shared Plan calculation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine.activity import (
    CategoryActualDetail,
    CategoryPlannedDetail,
    PeriodActivity,
    PlanMeasure,
    explain_category_period,
)
from ..engine.currency import reporting_currency_handle
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
    actual_to_date: Money | None
    remaining: Money | None
    remaining_reason: str | None = None


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


def _foreign_expense_accounts(
    db: DbSQLite, buckets: Sequence[PeriodActivity], reporting: str, as_of: date
) -> list[set[str]]:
    """Find category ancestors touched by expense events needing FX conversion."""
    accounts = {item.handle: item for item in db.iter_accounts()}
    affected: list[set[str]] = []
    for bucket in buckets:
        handles: set[str] = set()
        for event in bucket.planned_events:
            if (event.expected_currency or reporting) != reporting:
                handles.update(split.account for split in event.expected_splits)
        for actual in bucket.actual_transactions:
            if actual.post_date > as_of:
                continue
            transaction = db.get_transaction(actual.transaction)
            if transaction is not None and (transaction.currency or reporting) != reporting:
                handles.update(split.account for split in transaction.splits)
        expanded: set[str] = set()
        for handle in handles:
            seen: set[str] = set()
            while handle not in seen:
                seen.add(handle)
                account = accounts.get(handle)
                if account is None or account.account_class is not AccountClass.EXPENSE:
                    break
                expanded.add(handle)
                if account.parent is None:
                    break
                handle = account.parent
        affected.append(expanded)
    return affected


def _expense_period(
    bucket: PeriodActivity,
    planned: Money,
    actual: Money,
    variance: Money | None,
    actual_to_date: Money | None,
    *,
    foreign: bool,
) -> ExpensePeriod:
    reason = (
        "Future period"
        if variance is None
        else "Currency conversion unavailable"
        if foreign
        else None
    )
    return ExpensePeriod(
        bucket.start,
        bucket.end,
        bucket.label,
        planned,
        actual,
        variance,
        actual_to_date,
        None if reason else planned - (actual_to_date or Money(0)),
        reason,
    )


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
    foreign = _foreign_expense_accounts(
        db, buckets, reporting_currency_handle(db), plan.report.as_of
    )
    scenario = (
        next((item for item in db.iter_scenarios() if item.handle == plan.scenario.handle), None)
        if plan.scenario.handle is not None
        else (request.baseline or _baseline(db, plan.start, plan.end))
    )

    def to_date(account_handle: str, index: int, actual: Money) -> Money | None:
        bucket = buckets[index]
        if bucket.start > plan.report.as_of:
            return None
        if bucket.end <= plan.report.as_of:
            return actual
        detail = explain_category_period(
            db,
            account_handle,
            bucket.start,
            bucket.end,
            scenario=scenario,
            as_of=plan.report.as_of,
        )
        return sum(
            (
                item.amount
                for item in detail.actual_transactions
                if item.post_date <= plan.report.as_of
            ),
            Money(0),
        )

    categories = tuple(
        ExpenseCategory(
            row.account,
            row.name,
            row.full_name,
            row.depth,
            tuple(
                _expense_period(
                    bucket,
                    planned,
                    actual,
                    variance,
                    to_date(row.account, index, actual),
                    foreign=row.account in foreign[index],
                )
                for index, (bucket, planned, actual, variance) in enumerate(
                    zip(buckets, row.planned, row.actual, row.variance, strict=True)
                )
            ),
        )
        for row in plan.report.expenses
    )
    roots = tuple(
        row
        for row in categories
        if not any(
            row.full_name.startswith(f"{candidate.full_name}:")
            for candidate in categories
            if candidate is not row
        )
    )
    totals = tuple(
        _expense_period(
            bucket,
            planned or Money(0),
            actual or Money(0),
            variance,
            None
            if bucket.start > plan.report.as_of
            else sum((row.periods[index].actual_to_date or Money(0) for row in roots), Money(0)),
            foreign=bool(foreign[index]),
        )
        for index, (bucket, planned, actual, variance) in enumerate(
            zip(
                buckets,
                plan.report.category_totals(AccountClass.EXPENSE, PlanMeasure.PLANNED),
                plan.report.category_totals(AccountClass.EXPENSE, PlanMeasure.ACTUAL),
                plan.report.category_totals(AccountClass.EXPENSE, PlanMeasure.VARIANCE),
                strict=True,
            )
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
