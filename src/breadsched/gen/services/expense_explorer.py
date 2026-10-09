"""Read-only expense exploration over the shared Plan calculation."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine.activity import PeriodActivity, PlanMeasure
from ..engine.chart_model import LINE, SHARE, STACKED, ChartMarker, ChartModel, ChartSeries
from ..engine.completeness import Completeness
from ..engine.plan_detail import (
    CategoryActualDetail,
    CategoryPlannedDetail,
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
    actual_to_date: Money | None
    carry_in: Money | None
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
    # An income category's drilldown groups its actuals by payer the same way.
    income: bool = False


@dataclass(frozen=True, slots=True)
class SpendingPoint:
    """One period of total spending (or income) over time, split by top-level category.

    ``categories`` pairs each top-level category's handle with its actual for the
    period; together they equal ``actual`` exactly. ``future`` periods
    start after the as-of date (their actual is only what is already posted), and
    ``partial`` periods contain it. ``currency_incomplete`` marks a period with
    foreign activity that has no applicable quote and is therefore left out.
    """

    start: date
    end: date
    label: str
    planned: Money
    actual: Money
    future: bool
    partial: bool
    currency_incomplete: bool
    categories: tuple[tuple[str, Money], ...]
    #: Partial, with the excluded amounts, when ``currency_incomplete`` (#236).
    completeness: Completeness = field(default_factory=Completeness)


@dataclass(frozen=True, slots=True)
class ExpenseExplorer:
    plan: PlanQueryResult
    categories: tuple[ExpenseCategory, ...]
    totals: tuple[ExpensePeriod, ...]
    drilldown: ExpenseDrilldown | None
    rollover: bool = False
    spending: tuple[SpendingPoint, ...] = ()
    # Income over time uses the same Plan periods and income categories; its
    # points carry income category handles, named by ``income_categories``.
    income: tuple[SpendingPoint, ...] = ()
    income_categories: tuple[ExpenseCategory, ...] = ()


def _merchant_name(description: str, income: bool = False) -> str:
    return description.strip() or ("Unknown payer" if income else "Unknown merchant")


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
        None,
        None if reason else planned - (actual_to_date or Money(0)),
        reason,
    )


def _spending_composition(
    categories: tuple[ExpenseCategory, ...], roots: tuple[ExpenseCategory, ...]
) -> tuple[tuple[str, tuple[Money, ...]], ...]:
    """Top-level expense categories and their actual per period.

    A lone root (usually the book's own "Expenses" account) is replaced by its
    immediate child categories so the split is informative. Anything posted to
    that root itself stays as its own entry under the root's handle, so the
    entries always sum to the total exactly.
    """
    level = list(roots)
    residual: list[tuple[str, tuple[Money, ...]]] = []
    while len(level) == 1:
        parent = level[0]
        prefix = f"{parent.full_name}:"
        below = [row for row in categories if row.full_name.startswith(prefix)]
        children = [
            row
            for row in below
            if not any(
                row.full_name.startswith(f"{other.full_name}:")
                for other in below
                if other is not row
            )
        ]
        if not children:
            break
        own = tuple(
            period.actual - sum((child.periods[i].actual for child in children), Money(0))
            for i, period in enumerate(parent.periods)
        )
        if any(own):
            residual.append((parent.account, own))
        level = children
    entries = [(row.account, tuple(period.actual for period in row.periods)) for row in level]
    return tuple(entries + residual)


def _roots(categories: tuple[ExpenseCategory, ...]) -> tuple[ExpenseCategory, ...]:
    return tuple(
        row
        for row in categories
        if not any(
            row.full_name.startswith(f"{candidate.full_name}:")
            for candidate in categories
            if candidate is not row
        )
    )


def _over_time(
    buckets: tuple[PeriodActivity, ...] | list[PeriodActivity],
    categories: tuple[ExpenseCategory, ...],
    planned: list[Money | None],
    actual: list[Money | None],
    foreign: list[set[str]] | list[frozenset[str]],
    as_of: date,
    what: str,
) -> tuple[SpendingPoint, ...]:
    """Total plan and actual per period, with actual split by top-level category."""
    handles = {row.account for row in categories}
    composition = _spending_composition(categories, _roots(categories))
    points = tuple(
        SpendingPoint(
            bucket.start,
            bucket.end,
            bucket.label,
            planned[index] or Money(0),
            actual[index] or Money(0),
            future=bucket.start > as_of,
            partial=bucket.start <= as_of < bucket.end,
            currency_incomplete=bool(foreign[index] & handles),
            categories=tuple((handle, amounts[index]) for handle, amounts in composition),
            completeness=bucket.completeness.touching(handles),
        )
        for index, bucket in enumerate(buckets)
    )
    for point in points:
        if sum((amount for _account, amount in point.categories), Money(0)) != point.actual:
            raise AssertionError(f"top-level {what} categories do not reconcile to Plan")
    return points


def _apply_rollover(
    periods: tuple[ExpensePeriod, ...], enabled: bool, as_of: date
) -> tuple[ExpensePeriod, ...]:
    """Carry only completed, explainable periods within the selected horizon."""
    if not enabled:
        return periods
    carry: Money | None = Money(0)
    result = []
    for period in periods:
        if period.remaining_reason == "Future period":
            result.append(period)
            continue
        if period.remaining is None:
            result.append(replace(period, carry_in=carry))
            carry = None
            continue
        if carry is None:
            result.append(
                replace(period, remaining=None, remaining_reason="Prior period unavailable")
            )
            continue
        closing = carry + period.remaining
        result.append(replace(period, carry_in=carry, remaining=closing))
        if period.end <= as_of:
            carry = closing
    return tuple(result)


def query_expense_explorer(
    db: DbSQLite,
    request: PlanQuery,
    *,
    account: str | None = None,
    period_index: int | None = None,
    rollover: bool = False,
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
    # Converted foreign activity is already in reporting currency; only categories
    # missing an applicable quote cannot show Remaining.
    expense_accounts = {row.account for row in plan.report.expenses}
    foreign = [accounts & expense_accounts for accounts in plan.report.unconverted_accounts]
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
            _apply_rollover(
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
                rollover,
                plan.report.as_of,
            ),
        )
        for row in plan.report.expenses
    )
    roots = _roots(categories)
    totals = _apply_rollover(
        tuple(
            _expense_period(
                bucket,
                planned or Money(0),
                actual or Money(0),
                variance,
                None
                if bucket.start > plan.report.as_of
                else sum(
                    (row.periods[index].actual_to_date or Money(0) for row in roots), Money(0)
                ),
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
        ),
        rollover,
        plan.report.as_of,
    )
    as_of = plan.report.as_of
    spending = _over_time(
        buckets,
        categories,
        [total.planned for total in totals],
        [total.actual for total in totals],
        plan.report.unconverted_accounts,
        as_of,
        "expense",
    )
    income_categories = tuple(
        ExpenseCategory(
            row.account,
            row.name,
            row.full_name,
            row.depth,
            tuple(
                ExpensePeriod(
                    bucket.start,
                    bucket.end,
                    bucket.label,
                    planned,
                    actual,
                    variance,
                    None,
                    None,
                    None,
                )
                for bucket, planned, actual, variance in zip(
                    buckets, row.planned, row.actual, row.variance, strict=True
                )
            ),
        )
        for row in plan.report.income
    )
    income = _over_time(
        buckets,
        income_categories,
        plan.report.category_totals(AccountClass.INCOME, PlanMeasure.PLANNED),
        plan.report.category_totals(AccountClass.INCOME, PlanMeasure.ACTUAL),
        plan.report.unconverted_accounts,
        as_of,
        "income",
    )
    drilldown = None
    if account is not None or period_index is not None:
        selected = next(
            (item for item in (*categories, *income_categories) if item.account == account),
            None,
        )
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
        is_income = selected in income_categories
        grouped: dict[str, list[CategoryActualDetail]] = {}
        for actual in detail.actual_transactions:
            key = _merchant_name(actual.description, is_income).casefold()
            grouped.setdefault(key, []).append(actual)
        merchants = tuple(
            MerchantGroup(
                min(_merchant_name(item.description, is_income) for item in items),
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
            income=is_income,
        )
    return ServiceResult.success(
        ExpenseExplorer(
            plan, categories, totals, drilldown, rollover, spending, income, income_categories
        )
    )


#: The most top-level categories a category chart names; the rest share one "Other".
_NAMED_CATEGORIES = 7


def _as_of_marker(starts_ends: list[tuple[date, date]], as_of: date) -> tuple[ChartMarker, ...]:
    """A rule at the period holding the as-of date, or the first period after it."""
    index = next(
        (i for i, (start, end) in enumerate(starts_ends) if start <= as_of < end or start > as_of),
        None,
    )
    if index is None:
        return ()
    return (ChartMarker(index, f"As of {as_of.isoformat()}"),)


def _partial(points: tuple[SpendingPoint, ...]) -> tuple[int | None, str]:
    index = next((i for i, point in enumerate(points) if point.currency_incomplete), None)
    if index is None:
        return None, ""
    point = points[index]
    reason = (point.completeness.label or "missing quote").removeprefix("Partial: ")
    return index, f"Partial from {point.label} (shaded): {reason}"


def spending_charts(
    explorer: ExpenseExplorer, *, income: bool = False, currency: str = ""
) -> tuple[ChartModel, ...]:
    """Spending (or income) over the Plan's periods, from the explorer's own values.

    Three charts: total plan and actual as lines, actual stacked by top-level
    category, and each category's share of the period's actual. Categories are
    ranked by actual over the range; past the seventh, the rest are combined as
    "Other", so every period's segments still sum to its actual exactly. No
    charts without periods; without activity, each chart is ``empty``.
    """
    points = explorer.income if income else explorer.spending
    if not points:
        return ()
    what = "income" if income else "spending"
    title = "Income" if income else "Spending"
    names = {
        row.account: row.full_name
        for row in (explorer.income_categories if income else explorer.categories)
    }
    as_of = explorer.plan.report.as_of
    labels = tuple(point.label for point in points)
    actual = tuple(point.actual for point in points)
    partial_from, partial_note = _partial(points)
    markers = _as_of_marker([(point.start, point.end) for point in points], as_of)
    totals = ChartModel(
        what,
        f"{title}: total plan and actual",
        LINE,
        labels,
        (
            ChartSeries("planned", "Plan", tuple(point.planned for point in points), 1),
            ChartSeries("actual", "Actual", actual, 2),
        ),
        currency,
        markers,
        partial_from,
        partial_note,
    )
    handles = [handle for handle, _amount in points[0].categories]
    by_handle = {
        handle: tuple(dict(point.categories)[handle] for point in points) for handle in handles
    }
    ranked = sorted(
        handles,
        key=lambda handle: (
            -sum((abs(v) for v in by_handle[handle]), Money(0)),
            names.get(handle, handle),
        ),
    )
    named = ranked if len(ranked) <= _NAMED_CATEGORIES + 1 else ranked[:_NAMED_CATEGORIES]
    series = [
        ChartSeries(handle, names.get(handle, handle), by_handle[handle], slot)
        for slot, handle in enumerate(named, start=1)
    ]
    rest = [handle for handle in ranked if handle not in named]
    if rest:
        series.append(
            ChartSeries(
                "other",
                "Other",
                tuple(
                    sum((by_handle[handle][index] for handle in rest), Money(0))
                    for index in range(len(points))
                ),
                _NAMED_CATEGORIES + 1,
            )
        )
    for index, point in enumerate(points):
        if sum((item.values[index] or Money(0) for item in series), Money(0)) != point.actual:
            raise AssertionError(f"{what} chart categories do not reconcile to Plan")
    stacked = ChartModel(
        f"{what}_categories",
        f"{title} by category",
        STACKED,
        labels,
        tuple(series),
        currency,
        markers=(),
        partial_from=partial_from,
        partial_note=partial_note,
        totals=actual,
    )
    share = replace(stacked, key=f"{what}_share", title=f"Share of {what} by category", kind=SHARE)
    return totals, stacked, share


def category_trend_chart(category: ExpenseCategory, as_of: date, currency: str = "") -> ChartModel:
    """One category's plan and period actual across the Plan's periods."""
    return ChartModel(
        "category_trend",
        f"{category.full_name}: plan and actual",
        LINE,
        tuple(period.label for period in category.periods),
        (
            ChartSeries("planned", "Plan", tuple(p.planned for p in category.periods), 1),
            ChartSeries("actual", "Period actual", tuple(p.actual for p in category.periods), 2),
        ),
        currency,
        _as_of_marker([(p.start, p.end) for p in category.periods], as_of),
    )
