"""Historical category activity turned into reviewable planning estimates.

The analyzer deliberately produces suggestions rather than silently changing the
book. Accepted suggestions become ordinary scheduled estimates, so Plan,
Projection, Review, and scenarios continue to consume one event model.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from ..db.sqlite import DbSQLite
from ..lib.account import AccountClass
from ..lib.money import Money
from ..lib.recurrence import PeriodType, Recurrence
from ..lib.scenario import ScenarioSchedule
from ..lib.scheduled import ScheduledMonthAmount, ScheduledSplit, ScheduledTransaction
from . import planning

__all__ = [
    "HistoricalEstimateProposal",
    "accept_historical_estimate",
    "propose_historical_estimates",
]


@dataclass(frozen=True, slots=True)
class HistoricalEstimateProposal:
    """One category estimate inferred from closed historical months."""

    category: str
    category_name: str
    funding: str
    funding_name: str
    amount: Money
    recurrence: Recurrence
    sample_months: int
    active_months: int
    transaction_count: int
    confidence: float
    reason: str
    scheduled_amount: Money = Money(0)
    seasonal: bool = False
    trend: str | None = None
    seasonal_amounts: tuple[ScheduledMonthAmount, ...] = ()

    @property
    def source_name(self) -> str:
        """Display name of the account money historically flowed from."""
        return self.funding_name if self.amount >= 0 else self.category_name

    @property
    def destination_name(self) -> str:
        """Display name of the account money historically flowed to."""
        return self.category_name if self.amount >= 0 else self.funding_name

    @property
    def display_amount(self) -> Money:
        """Unsigned amount for UIs that display direction separately."""
        return abs(self.amount)


def _month_start(when: date) -> date:
    return when.replace(day=1)


def _add_months(when: date, months: int) -> date:
    index = when.year * 12 + when.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def _typical_amount(values: list[Money]) -> Money:
    decimals = sorted(value.to_decimal() for value in values)
    return Money(median(decimals)).quantize(100)


def _funding_account(db: DbSQLite, category: str, start: date, end: date) -> str | None:
    counts: Counter[str] = Counter()
    for txn in db.iter_transactions(account=category, start=start, end=end):
        for split in txn.splits:
            if split.account == category:
                continue
            account = db.get_account(split.account)
            if account is None or account.atype.is_flow or account.is_root:
                continue
            counts[split.account] += 1
    return counts.most_common(1)[0][0] if counts else None


def _target_events(
    db: DbSQLite,
    start: date,
    end: date,
    scenario_handle: str | None,
) -> list[planning.PlannedEvent]:
    if scenario_handle is None:
        return planning.scheduled_events(db, start, end)
    scenario = db.get_scenario(scenario_handle)
    if scenario is None:
        raise ValueError("saved scenario no longer exists")
    return planning.scenario_events(db, scenario, start, end)


def _scheduled_category_totals(
    db: DbSQLite,
    start: date,
    end: date,
    scenario_handle: str | None,
) -> dict[tuple[str, date], Money]:
    """Historical committed activity, excluding planning-only estimates."""
    totals: dict[tuple[str, date], Money] = {}
    for event in _target_events(db, start, end, scenario_handle):
        if event.placeholder:
            continue
        month = _month_start(event.planned_date)
        for split in event.expected_splits:
            account = db.get_account(split.account)
            if account is None or account.account_class not in (
                AccountClass.INCOME,
                AccountClass.EXPENSE,
            ):
                continue
            key = (account.handle, month)
            totals[key] = totals.get(key, Money(0)) + split.amount * account.sign()
    return totals


def _planned_estimate_profiles(
    db: DbSQLite,
    start: date,
    scenario_handle: str | None,
) -> dict[tuple[str, int], Money]:
    """Expected estimate amount by category and month-of-year.

    Historical suggestions create schedules beginning in the current planning
    period.  Looking only for occurrences inside the historical sample therefore
    forgets an estimate immediately after it is accepted.  Instead, sample the
    next twelve planning months and use that future plan as the amount already
    accounted for when re-analysing history.
    """
    end = _add_months(start, 12) - timedelta(days=1)
    totals: dict[tuple[str, int], Money] = {}
    for event in _target_events(db, start, end, scenario_handle):
        if not event.placeholder:
            continue
        for split in event.expected_splits:
            account = db.get_account(split.account)
            if account is None or account.account_class not in (
                AccountClass.INCOME,
                AccountClass.EXPENSE,
            ):
                continue
            key = (account.handle, event.planned_date.month)
            totals[key] = totals.get(key, Money(0)) + split.amount * account.sign()
    return totals


def _residual_after_scheduled(actual: Money, scheduled: Money) -> tuple[Money, Money]:
    """Return residual history and the scheduled amount that actually covered it."""
    if actual > 0 and scheduled > 0:
        residual = max(actual - scheduled, Money(0))
    elif actual < 0 and scheduled < 0:
        residual = min(actual - scheduled, Money(0))
    else:
        residual = actual
    return residual, actual - residual


def _unscheduled_dates(db: DbSQLite, category: str, start: date, end: date) -> list[date]:
    dates: list[date] = []
    for txn in db.iter_transactions(account=category, start=start, end=end):
        if txn.planned_occurrence:
            continue
        if txn.value_for(category):
            dates.append(txn.post_date)
    return sorted(dates)


def _infer_recurrence(dates: list[date], start: date) -> tuple[Recurrence, str, Decimal]:
    if len(dates) < 2:
        return Recurrence(PeriodType.MONTH, start=start), "monthly", Decimal("1")
    gaps = [(later - earlier).days for earlier, later in zip(dates[:-1], dates[1:], strict=True)]
    typical_gap = float(median(gaps))
    if 5 <= typical_gap <= 9:
        return (
            Recurrence(PeriodType.WEEK, start=start),
            "weekly",
            Decimal(52) / Decimal(12),
        )
    if 11 <= typical_gap <= 17:
        return (
            Recurrence(PeriodType.WEEK, interval=2, start=start),
            "fortnightly",
            Decimal(26) / Decimal(12),
        )
    if 300 <= typical_gap <= 430:
        return (
            Recurrence(PeriodType.YEAR, start=start),
            "annual",
            Decimal(1) / Decimal(12),
        )
    return Recurrence(PeriodType.MONTH, start=start), "monthly", Decimal("1")


def _trend_summary(values: list[Money]) -> tuple[list[Money], str | None]:
    """Return the sample to use and a conservative trend label."""
    if len(values) < 6:
        return values, None
    split = len(values) // 2
    earlier = _typical_amount(values[:split])
    later = _typical_amount(values[split:])
    if not earlier or (earlier > 0) != (later > 0):
        return values, None
    change = (later.to_decimal() - earlier.to_decimal()) / abs(earlier.to_decimal())
    if abs(change) < Decimal("0.10"):
        return values, None
    recent = values[-min(3, len(values)) :]
    direction = "upward" if change > 0 else "downward"
    return recent, f"{direction} trend ({abs(change) * Decimal(100):.1f}%)"


def _has_seasonality(monthly_by_month: dict[int, list[Money]]) -> bool:
    """Detect a repeated month-of-year pattern without overfitting one year."""
    medians = [
        _typical_amount(values)
        for values in monthly_by_month.values()
        if len(values) >= 2 and any(values)
    ]
    if len(medians) < 4:
        return False
    magnitudes = [abs(value.to_decimal()) for value in medians if value]
    if len(magnitudes) < 4:
        return False
    middle = median(magnitudes)
    if not middle:
        return False
    return max(magnitudes) >= middle * Decimal("1.35")


def _seasonal_amounts(
    monthly_by_month: dict[int, list[Money]],
) -> tuple[ScheduledMonthAmount, ...]:
    """Return repeated month-of-year magnitudes supported by at least two years."""
    result: list[ScheduledMonthAmount] = []
    for month in range(1, 13):
        values = monthly_by_month.get(month, [])
        if len(values) < 2:
            continue
        typical = _typical_amount(values)
        if typical:
            result.append(ScheduledMonthAmount(month, abs(typical)))
    return tuple(result)


def propose_historical_estimates(
    db: DbSQLite,
    *,
    as_of: date | None = None,
    months: int = 12,
    min_active_months: int = 3,
    scenario_handle: str | None = None,
) -> list[HistoricalEstimateProposal]:
    """Propose residual category estimates from completed historical months.

    Existing planned activity for the selected target is subtracted before a
    proposal is formed. The remaining history is summarized with a median and a
    conservative cadence detector; users may refine the result in the normal
    schedule editor before relying on it.
    """
    if months < 1:
        raise ValueError("months of history must be positive")
    if min_active_months < 1:
        raise ValueError("minimum active months must be positive")

    today = as_of or date.today()
    current_month = _month_start(today)
    history_start = _add_months(current_month, -months)
    history_end = current_month - timedelta(days=1)
    proposals: list[HistoricalEstimateProposal] = []
    scheduled_totals = _scheduled_category_totals(db, history_start, history_end, scenario_handle)
    estimate_profiles = _planned_estimate_profiles(db, current_month, scenario_handle)

    for account in db.iter_accounts():
        if account.is_root or account.placeholder:
            continue
        if account.account_class not in (AccountClass.INCOME, AccountClass.EXPENSE):
            continue

        monthly: list[Money] = []
        monthly_by_month: dict[int, list[Money]] = {}
        txn_count = 0
        applied_scheduled_total = Money(0)
        for offset in range(months):
            start = _add_months(history_start, offset)
            end = _add_months(start, 1) - timedelta(days=1)
            total = Money(0)
            for txn in db.iter_transactions(account=account.handle, start=start, end=end):
                value = txn.value_for(account.handle) * account.sign()
                if value:
                    total = total + value
                    txn_count += 1
            scheduled = scheduled_totals.get((account.handle, start), Money(0))
            scheduled = scheduled + estimate_profiles.get((account.handle, start.month), Money(0))
            residual, applied_scheduled = _residual_after_scheduled(total, scheduled)
            applied_scheduled_total = applied_scheduled_total + applied_scheduled
            if residual:
                monthly.append(residual)
                monthly_by_month.setdefault(start.month, []).append(residual)

        if len(monthly) < min_active_months:
            continue
        funding = _funding_account(db, account.handle, history_start, history_end)
        funding_account = db.get_account(funding) if funding else None
        if funding_account is None:
            continue

        trend_sample, trend = _trend_summary(monthly)
        monthly_residual = _typical_amount(trend_sample)
        seasonal = _has_seasonality(monthly_by_month)
        recurrence, cadence, occurrences_per_month = _infer_recurrence(
            _unscheduled_dates(db, account.handle, history_start, history_end),
            current_month,
        )
        amount = (monthly_residual / occurrences_per_month).quantize(100)
        seasonal_amounts = (
            _seasonal_amounts(monthly_by_month)
            if seasonal and recurrence.period is PeriodType.MONTH
            else ()
        )
        active_ratio = len(monthly) / months
        confidence = min(0.95, 0.45 + active_ratio * 0.5)
        scheduled_total = applied_scheduled_total
        proposals.append(
            HistoricalEstimateProposal(
                category=account.handle,
                category_name=db.full_name(account),
                funding=funding_account.handle,
                funding_name=db.full_name(funding_account),
                amount=amount,
                recurrence=recurrence,
                sample_months=months,
                active_months=len(monthly),
                transaction_count=txn_count,
                confidence=confidence,
                reason=(
                    f"{cadence}; median residual of {len(monthly)} active month(s) "
                    f"across {months} completed month(s) after subtracting "
                    f"{scheduled_total.format()} of scheduled category activity"
                    + (f"; {trend}, using recent median" if trend else "")
                    + ("; recurring seasonal variation detected" if seasonal else "")
                ),
                scheduled_amount=scheduled_total,
                seasonal=seasonal,
                trend=trend,
                seasonal_amounts=seasonal_amounts,
            )
        )

    return sorted(proposals, key=lambda item: item.category_name)


def accept_historical_estimate(
    db: DbSQLite,
    proposal: HistoricalEstimateProposal,
    *,
    scenario_handle: str | None = None,
) -> str:
    """Persist a proposal as an ordinary Base or scenario estimate."""
    category = db.get_account(proposal.category)
    funding = db.get_account(proposal.funding)
    if category is None or funding is None:
        raise ValueError("proposal accounts no longer exist")
    signed = proposal.amount * category.sign()
    splits = [
        ScheduledSplit(category.handle, signed),
        ScheduledSplit(funding.handle, -signed),
    ]
    name = f"Estimated {proposal.category_name}"

    if scenario_handle is None:
        baseline_schedule = ScheduledTransaction(
            name=name,
            recurrence=proposal.recurrence,
            splits=splits,
            auto_create=False,
            seasonal_amounts=list(proposal.seasonal_amounts),
        )
        baseline_schedule.placeholder = True
        with db.transaction(f"Add historical estimate {proposal.category_name}") as txn:
            db.add_scheduled(baseline_schedule, txn)
        return baseline_schedule.handle

    scenario = db.get_scenario(scenario_handle)
    if scenario is None:
        raise ValueError("saved scenario no longer exists")
    scenario_schedule = ScenarioSchedule(
        name=name,
        recurrence=proposal.recurrence,
        splits=splits,
        placeholder=True,
        seasonal_amounts=list(proposal.seasonal_amounts),
    )
    scenario.schedule_overrides.append(scenario_schedule)
    with db.transaction(f"Add historical estimate to {scenario.name}") as txn:
        db.commit_scenario(scenario, txn)
    return scenario_schedule.handle
