"""Derived budget/actual reporting over dated planning events.

The planning engine is event driven.  This module deliberately introduces display
periods only after planned occurrences and actual ledger transactions already have
their real dates.  Changing a report from month to quarter or year therefore never
changes the underlying plan or projection.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass
from ..lib.money import Money
from ..lib.recurrence import add_months
from ..lib.scenario import Scenario
from ..lib.scheduled import ScheduledTransaction
from ..lib.transaction import PlanningResolution, Transaction
from .planning import (
    EventStatus,
    PlannedEvent,
    PlannedSplit,
    event_by_key,
    scenario_events,
    scheduled_events,
)

__all__ = [
    "ActualActivity",
    "ActivityReport",
    "CategoryActivity",
    "CategoryActualDetail",
    "CategoryPeriodDetail",
    "CategoryPlannedDetail",
    "CategoryReport",
    "PeriodActivity",
    "ReportingPeriod",
    "build_activity_report",
    "build_category_report",
    "explain_category_period",
]


class ReportingPeriod(str, Enum):
    """Calendar bucket used only to display event-driven activity."""

    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


@dataclass(frozen=True, slots=True)
class ActualActivity:
    """One ledger transaction as it appears in a plan-vs-actual report."""

    transaction: str
    post_date: date
    description: str
    amount: Money
    cash_change: Money
    income: Money
    expense: Money
    planned_occurrence: str | None = None
    planned_for: date | None = None
    planned_amount: Money | None = None
    planning_resolution: PlanningResolution = PlanningResolution.UNRESOLVED

    @property
    def unresolved(self) -> bool:
        return self.planning_resolution is PlanningResolution.UNRESOLVED

    @property
    def unexpected(self) -> bool:
        return self.planning_resolution is PlanningResolution.UNEXPECTED

    @property
    def variance(self) -> Money | None:
        if self.planned_amount is None:
            return None
        return self.amount - self.planned_amount

    @property
    def date_variance_days(self) -> int | None:
        if self.planned_for is None:
            return None
        return (self.post_date - self.planned_for).days

    def as_dict(self) -> dict[str, object]:
        return {
            "transaction": self.transaction,
            "post_date": self.post_date,
            "description": self.description,
            "amount": self.amount,
            "cash_change": self.cash_change,
            "income": self.income,
            "expense": self.expense,
            "planned_occurrence": self.planned_occurrence,
            "planned_for": self.planned_for,
            "planned_amount": self.planned_amount,
            "variance": self.variance,
            "date_variance_days": self.date_variance_days,
            "planning_resolution": self.planning_resolution.value,
            "unresolved": self.unresolved,
            "unexpected": self.unexpected,
        }


@dataclass(slots=True)
class PeriodActivity:
    """One display bucket assembled from exact-dated expectations and actuals."""

    start: date
    end: date
    label: str
    planned_amount: Money = field(default_factory=lambda: Money(0))
    actual_amount: Money = field(default_factory=lambda: Money(0))
    planned_cash_change: Money = field(default_factory=lambda: Money(0))
    actual_cash_change: Money = field(default_factory=lambda: Money(0))
    planned_income: Money = field(default_factory=lambda: Money(0))
    actual_income: Money = field(default_factory=lambda: Money(0))
    planned_expense: Money = field(default_factory=lambda: Money(0))
    actual_expense: Money = field(default_factory=lambda: Money(0))
    planned_events: list[PlannedEvent] = field(default_factory=list)
    actual_transactions: list[ActualActivity] = field(default_factory=list)

    @property
    def amount_variance(self) -> Money:
        return self.actual_amount - self.planned_amount

    @property
    def cash_variance(self) -> Money:
        return self.actual_cash_change - self.planned_cash_change

    @property
    def income_variance(self) -> Money:
        return self.actual_income - self.planned_income

    @property
    def expense_variance(self) -> Money:
        return self.actual_expense - self.planned_expense

    @property
    def unresolved(self) -> tuple[PlannedEvent, ...]:
        return tuple(
            event for event in self.planned_events
            if event.status is EventStatus.EXPECTED
        )

    @property
    def resolved(self) -> tuple[PlannedEvent, ...]:
        return tuple(
            event for event in self.planned_events
            if event.status is EventStatus.ACTUALIZED
        )

    @property
    def unresolved_actuals(self) -> tuple[ActualActivity, ...]:
        return tuple(item for item in self.actual_transactions if item.unresolved)

    @property
    def unexpected(self) -> tuple[ActualActivity, ...]:
        return tuple(item for item in self.actual_transactions if item.unexpected)

    def as_dict(self) -> dict[str, object]:
        return {
            "start": self.start,
            "end": self.end,
            "label": self.label,
            "planned_amount": self.planned_amount,
            "actual_amount": self.actual_amount,
            "amount_variance": self.amount_variance,
            "planned_cash_change": self.planned_cash_change,
            "actual_cash_change": self.actual_cash_change,
            "cash_variance": self.cash_variance,
            "planned_income": self.planned_income,
            "actual_income": self.actual_income,
            "income_variance": self.income_variance,
            "planned_expense": self.planned_expense,
            "actual_expense": self.actual_expense,
            "expense_variance": self.expense_variance,
            "unresolved_count": len(self.unresolved),
            "resolved_count": len(self.resolved),
            "unresolved_actual_count": len(self.unresolved_actuals),
            "unexpected_count": len(self.unexpected),
            "planned_events": [event.as_dict() for event in self.planned_events],
            "actual_transactions": [item.as_dict() for item in self.actual_transactions],
        }


@dataclass(slots=True)
class ActivityReport:
    """A chronological set of display buckets over one event/actual horizon."""

    start: date
    end: date
    period: ReportingPeriod
    periods: list[PeriodActivity]

    @property
    def planned_amount(self) -> Money:
        return _sum_money(period.planned_amount for period in self.periods)

    @property
    def actual_amount(self) -> Money:
        return _sum_money(period.actual_amount for period in self.periods)

    @property
    def amount_variance(self) -> Money:
        return self.actual_amount - self.planned_amount

    @property
    def planned_cash_change(self) -> Money:
        return _sum_money(period.planned_cash_change for period in self.periods)

    @property
    def actual_cash_change(self) -> Money:
        return _sum_money(period.actual_cash_change for period in self.periods)

    @property
    def cash_variance(self) -> Money:
        return self.actual_cash_change - self.planned_cash_change

    @property
    def unresolved_count(self) -> int:
        return sum(len(period.unresolved) for period in self.periods)

    @property
    def unresolved_actual_count(self) -> int:
        return sum(len(period.unresolved_actuals) for period in self.periods)

    @property
    def unexpected_count(self) -> int:
        return sum(len(period.unexpected) for period in self.periods)

    def as_dict(self) -> dict[str, object]:
        return {
            "start": self.start,
            "end": self.end,
            "period": self.period.value,
            "planned_amount": self.planned_amount,
            "actual_amount": self.actual_amount,
            "amount_variance": self.amount_variance,
            "planned_cash_change": self.planned_cash_change,
            "actual_cash_change": self.actual_cash_change,
            "cash_variance": self.cash_variance,
            "unresolved_count": self.unresolved_count,
            "unresolved_actual_count": self.unresolved_actual_count,
            "unexpected_count": self.unexpected_count,
            "periods": [period.as_dict() for period in self.periods],
        }



@dataclass(slots=True)
class CategoryActivity:
    """One income/expense account across the selected display periods."""

    account: str
    name: str
    full_name: str
    account_class: AccountClass
    depth: int
    planned: list[Money]
    actual: list[Money]

    @property
    def variance(self) -> list[Money]:
        return [actual - planned for planned, actual in zip(self.planned, self.actual, strict=True)]




@dataclass(frozen=True, slots=True)
class CategoryPlannedDetail:
    """One planned occurrence contributing to a category/period cell."""

    occurrence: str
    planned_date: date
    description: str
    source: str
    status: str
    expected: Money
    actual: Money | None
    variance: Money | None
    actual_transaction: str | None
    actual_date: date | None


@dataclass(frozen=True, slots=True)
class CategoryActualDetail:
    """One actual transaction contributing to a category/period cell."""

    transaction: str
    post_date: date
    description: str
    amount: Money
    resolution: PlanningResolution
    planned_occurrence: str | None
    planned_for: date | None
    expected: Money | None
    variance: Money | None
    date_variance_days: int | None


@dataclass(frozen=True, slots=True)
class CategoryPeriodDetail:
    """Explanation of one derived Plan category/period value."""

    account: str
    name: str
    full_name: str
    account_class: AccountClass
    start: date
    end: date
    planned: Money
    actual: Money
    planned_events: tuple[CategoryPlannedDetail, ...]
    actual_transactions: tuple[CategoryActualDetail, ...]

    @property
    def variance(self) -> Money:
        return self.actual - self.planned


@dataclass(slots=True)
class CategoryReport:
    """Income/expense hierarchy derived from exact-dated events and actuals."""

    activity: ActivityReport
    categories: list[CategoryActivity]

    @property
    def income(self) -> tuple[CategoryActivity, ...]:
        return tuple(
            row for row in self.categories if row.account_class is AccountClass.INCOME
        )

    @property
    def expenses(self) -> tuple[CategoryActivity, ...]:
        return tuple(
            row for row in self.categories if row.account_class is AccountClass.EXPENSE
        )


def explain_category_period(
    db: DbSQLite,
    account_handle: str,
    start: date,
    end: date,
    *,
    scenario: Scenario | None = None,
) -> CategoryPeriodDetail:
    """Explain one category-period value from the same exact-dated activity stream."""
    if end < start:
        raise ValueError("Plan detail end date precedes its start date.")
    account = db.get_account(account_handle)
    if account is None:
        raise KeyError(account_handle)
    if account.account_class not in (AccountClass.INCOME, AccountClass.EXPENSE):
        raise ValueError("Plan detail requires an income or expense account.")

    accounts = {item.handle: item for item in db.iter_accounts()}
    children: dict[str, list[str]] = {}
    for item in accounts.values():
        if item.parent is not None:
            children.setdefault(item.parent, []).append(item.handle)
    included: set[str] = set()

    def include(handle: str) -> None:
        item = accounts.get(handle)
        if item is None or item.account_class is not account.account_class:
            return
        included.add(handle)
        for child in children.get(handle, []):
            include(child)

    include(account.handle)

    def category_amount(splits: Iterable[PlannedSplit]) -> Money:
        total = Money(0)
        for split in splits:
            if split.account not in included:
                continue
            if account.account_class is AccountClass.INCOME:
                total = total - split.amount
            else:
                total = total + split.amount
        return total

    report = build_activity_report(db, start, end, period=ReportingPeriod.MONTH, scenario=scenario)
    planned_rows: list[CategoryPlannedDetail] = []
    actual_rows: list[CategoryActualDetail] = []
    planned_total = Money(0)
    actual_total = Money(0)

    for bucket in report.periods:
        for event in bucket.planned_events:
            expected = category_amount(event.expected_splits)
            if expected == Money(0):
                continue
            actual_value = (
                category_amount(event.actual_splits)
                if event.actual_transaction is not None
                else None
            )
            planned_total = planned_total + expected
            planned_rows.append(CategoryPlannedDetail(
                occurrence=event.key, planned_date=event.planned_date,
                description=event.description, source=event.source.value,
                status=event.status.value, expected=expected, actual=actual_value,
                variance=actual_value - expected if actual_value is not None else None,
                actual_transaction=event.actual_transaction, actual_date=event.actual_date,
            ))

        for actual in bucket.actual_transactions:
            transaction = db.get_transaction(actual.transaction)
            if transaction is None:
                continue
            value = category_amount(
                PlannedSplit(split.account, split.value) for split in transaction.splits
            )
            if value == Money(0):
                continue
            actual_total = actual_total + value
            expected = None
            if actual.planned_occurrence:
                event = event_by_key(db, actual.planned_occurrence)
                if event is not None:
                    expected = category_amount(event.expected_splits)
            actual_rows.append(CategoryActualDetail(
                transaction=actual.transaction, post_date=actual.post_date,
                description=actual.description, amount=value,
                resolution=actual.planning_resolution,
                planned_occurrence=actual.planned_occurrence, planned_for=actual.planned_for,
                expected=expected, variance=value - expected if expected is not None else None,
                date_variance_days=actual.date_variance_days,
            ))

    return CategoryPeriodDetail(
        account=account.handle, name=account.name, full_name=db.full_name(account),
        account_class=account.account_class, start=start, end=end,
        planned=planned_total, actual=actual_total, planned_events=tuple(planned_rows),
        actual_transactions=tuple(actual_rows),
    )


def _sum_money(values: Iterable[Money]) -> Money:
    total = Money(0)
    for value in values:
        total = total + value
    return total


def _period_start(when: date, period: ReportingPeriod) -> date:
    if period is ReportingPeriod.MONTH:
        return when.replace(day=1)
    if period is ReportingPeriod.QUARTER:
        month = ((when.month - 1) // 3) * 3 + 1
        return date(when.year, month, 1)
    return date(when.year, 1, 1)


def _next_period(start: date, period: ReportingPeriod) -> date:
    months = {
        ReportingPeriod.MONTH: 1,
        ReportingPeriod.QUARTER: 3,
        ReportingPeriod.YEAR: 12,
    }[period]
    return add_months(start, months, day=1)


def _period_label(start: date, period: ReportingPeriod) -> str:
    if period is ReportingPeriod.MONTH:
        return f"{start:%b %Y}"
    if period is ReportingPeriod.QUARTER:
        return f"Q{(start.month - 1) // 3 + 1} {start.year}"
    return str(start.year)


def _make_periods(
    start: date,
    end: date,
    period: ReportingPeriod,
) -> list[PeriodActivity]:
    found: list[PeriodActivity] = []
    cursor = _period_start(start, period)
    while cursor <= end:
        nxt = _next_period(cursor, period)
        found.append(
            PeriodActivity(
                start=max(start, cursor),
                end=min(end, nxt - timedelta(days=1)),
                label=_period_label(cursor, period),
            )
        )
        cursor = nxt
    return found


def _index_for(periods: list[PeriodActivity], when: date) -> PeriodActivity | None:
    for period in periods:
        if period.start <= when <= period.end:
            return period
    return None


def _gross(splits: tuple[PlannedSplit, ...]) -> Money:
    return _sum_money(split.amount for split in splits if split.amount > 0)


def _split_totals(
    splits: tuple[PlannedSplit, ...],
    accounts: dict[str, Account],
    *,
    funded_from_cash: bool = False,
) -> tuple[Money, Money, Money]:
    """Return ``(cash_change, income, expense)`` for a set of split values."""
    cash = Money(0)
    income = Money(0)
    expense = Money(0)
    for split in splits:
        account = accounts.get(split.account)
        if account is None:
            continue
        if account.atype.is_cash_like:
            cash = cash + split.amount
        elif account.account_class is AccountClass.INCOME:
            value = split.amount if funded_from_cash else -split.amount
            income = income + value
            if funded_from_cash:
                cash = cash + split.amount
        elif account.account_class is AccountClass.EXPENSE:
            expense = expense + split.amount
            if funded_from_cash:
                cash = cash - split.amount
        elif funded_from_cash and account.account_class in (
            AccountClass.ASSET,
            AccountClass.LIABILITY,
        ):
            cash = cash - split.amount
    return cash, income, expense


def _actual_activity(
    transaction: Transaction,
    accounts: dict[str, Account],
) -> ActualActivity:
    splits = tuple(PlannedSplit(split.account, split.value) for split in transaction.splits)
    cash, income, expense = _split_totals(splits, accounts)
    planned_for = transaction.planned_for
    planned_occurrence = transaction.planned_occurrence
    if planned_occurrence is None and transaction.scheduled_from is not None:
        # Books created before occurrence metadata existed still know which schedule
        # posted the transaction.  Treat that as resolved plan activity rather than
        # incorrectly reporting it as an unexpected purchase.
        planned_for = planned_for or transaction.post_date
        planned_occurrence = ScheduledTransaction.occurrence_key_for(
            transaction.scheduled_from, planned_for
        )
    return ActualActivity(
        transaction=transaction.handle,
        post_date=transaction.post_date,
        description=transaction.description,
        amount=_gross(splits),
        cash_change=cash,
        income=income,
        expense=expense,
        planned_occurrence=planned_occurrence,
        planned_for=planned_for,
        planned_amount=transaction.planned_amount,
        planning_resolution=transaction.planning_resolution,
    )


def build_activity_report(
    db: DbSQLite,
    start: date,
    end: date,
    *,
    period: ReportingPeriod | str = ReportingPeriod.MONTH,
    budget_handle: str | None = None,
    scenario: Scenario | None = None,
) -> ActivityReport:
    """Aggregate exact-dated planned and actual activity for display.

    Expectations are placed in the period containing their planned date.  Actual
    ledger transactions are placed in the period containing their posting date.
    Consequently an event planned for 31 January but posted on 1 February produces
    a January expectation and a February actual, faithfully exposing cash timing.
    """
    if end < start:
        raise ValueError("activity report end date precedes start date")
    grouping = period if isinstance(period, ReportingPeriod) else ReportingPeriod(period)
    periods = _make_periods(start, end, grouping)
    accounts = {account.handle: account for account in db.iter_accounts()}

    planned = (
        scenario_events(db, scenario, start, end)
        if scenario is not None
        else scheduled_events(
            db,
            start,
            end,
            budget_handle=budget_handle,
            include_actualized=True,
        )
    )
    for event in planned:
        bucket = _index_for(periods, event.planned_date)
        if bucket is None:
            continue
        cash, income, expense = _split_totals(
            event.expected_splits,
            accounts,
            funded_from_cash=event.funded_from_cash,
        )
        bucket.planned_events.append(event)
        bucket.planned_amount = bucket.planned_amount + event.expected_amount
        bucket.planned_cash_change = bucket.planned_cash_change + cash
        bucket.planned_income = bucket.planned_income + income
        bucket.planned_expense = bucket.planned_expense + expense

    for transaction in db.iter_transactions(start=start, end=end):
        bucket = _index_for(periods, transaction.post_date)
        if bucket is None:
            continue
        actual = _actual_activity(transaction, accounts)
        bucket.actual_transactions.append(actual)
        bucket.actual_amount = bucket.actual_amount + actual.amount
        bucket.actual_cash_change = bucket.actual_cash_change + actual.cash_change
        bucket.actual_income = bucket.actual_income + actual.income
        bucket.actual_expense = bucket.actual_expense + actual.expense

    return ActivityReport(start=start, end=end, period=grouping, periods=periods)


def build_category_report(
    db: DbSQLite,
    start: date,
    end: date,
    *,
    period: ReportingPeriod | str = ReportingPeriod.MONTH,
    scenario: Scenario | None = None,
) -> CategoryReport:
    """Derive category-period values from planned occurrences and actual splits.

    Income and expense accounts are the reporting dimension. Asset/liability
    transfers therefore affect projection state but never become budget expense.
    Parent category rows are roll-ups of their descendants.
    """
    activity = build_activity_report(db, start, end, period=period, scenario=scenario)
    accounts = {account.handle: account for account in db.iter_accounts()}
    periods = activity.periods
    direct_planned: dict[str, list[Money]] = {}
    direct_actual: dict[str, list[Money]] = {}

    def amounts(store: dict[str, list[Money]], handle: str) -> list[Money]:
        return store.setdefault(handle, [Money(0) for _ in periods])

    for period_index, bucket in enumerate(periods):
        for event in bucket.planned_events:
            for planned_split in event.expected_splits:
                account = accounts.get(planned_split.account)
                if account is None:
                    continue
                values = amounts(direct_planned, account.handle)
                if account.account_class is AccountClass.INCOME:
                    values[period_index] = values[period_index] - planned_split.amount
                elif account.account_class is AccountClass.EXPENSE:
                    values[period_index] = values[period_index] + planned_split.amount
        for actual in bucket.actual_transactions:
            transaction = db.get_transaction(actual.transaction)
            if transaction is None:
                continue
            for actual_split in transaction.splits:
                account = accounts.get(actual_split.account)
                if account is None:
                    continue
                values = amounts(direct_actual, account.handle)
                if account.account_class is AccountClass.INCOME:
                    values[period_index] = values[period_index] - actual_split.value
                elif account.account_class is AccountClass.EXPENSE:
                    values[period_index] = values[period_index] + actual_split.value

    active = set(direct_planned) | set(direct_actual)
    for handle in list(active):
        account = accounts.get(handle)
        while account is not None and account.parent is not None:
            parent = accounts.get(account.parent)
            if parent is None or parent.account_class is not account.account_class:
                break
            active.add(parent.handle)
            account = parent

    children: dict[str, list[str]] = {}
    for account in accounts.values():
        if account.parent is not None:
            children.setdefault(account.parent, []).append(account.handle)

    def rolled(handle: str, store: dict[str, list[Money]]) -> list[Money]:
        result = list(store.get(handle, [Money(0) for _ in periods]))
        for child in children.get(handle, []):
            child_account = accounts.get(child)
            account = accounts.get(handle)
            if (
                child_account is None
                or account is None
                or child_account.account_class is not account.account_class
            ):
                continue
            values = rolled(child, store)
            result = [left + right for left, right in zip(result, values, strict=True)]
        return result

    rows: list[CategoryActivity] = []
    for handle in active:
        account = accounts[handle]
        if account.account_class not in (AccountClass.INCOME, AccountClass.EXPENSE):
            continue
        full_name = db.full_name(account)
        # Hide a conventional top-level Income/Expenses root from indentation.
        depth = max(0, full_name.count(":"))
        rows.append(
            CategoryActivity(
                account=handle,
                name=account.name,
                full_name=full_name,
                account_class=account.account_class,
                depth=depth,
                planned=rolled(handle, direct_planned),
                actual=rolled(handle, direct_actual),
            )
        )
    rows.sort(key=lambda row: (row.account_class.value, row.full_name.casefold()))
    return CategoryReport(activity=activity, categories=rows)
