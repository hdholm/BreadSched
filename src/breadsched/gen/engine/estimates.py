"""Historical category activity turned into reviewable planning estimates.

The analyzer deliberately produces suggestions rather than silently changing the
book. Saved suggestions become ordinary scheduled estimates, so Plan,
Projection, Review, and scenarios continue to consume one event model.
"""

from __future__ import annotations

from calendar import monthrange
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
from .escrow import recognition as escrow_recognition

__all__ = [
    "HistoricalEstimateProposal",
    "accept_historical_estimate",
    "draft_historical_estimate",
    "draft_scenario_estimate",
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


def _planned_category_profiles(
    db: DbSQLite,
    start: date,
    scenario_handle: str | None,
) -> dict[tuple[str, date], Money]:
    """Existing future coverage by category and calendar month.

    Historical ledger activity is the gross need, including transactions resolved
    to older schedules. Sample the selected *future* plan to find what already
    covers that need. Historical schedules may have ended or changed amounts;
    future commitments and accepted estimates may have begun only recently.
    The next twelve months supply one occurrence of each calendar month without
    treating a past occurrence and its future replacement as separate coverage.
    """
    end = _add_months(start, 12) - timedelta(days=1)
    totals: dict[tuple[str, date], Money] = {}
    accounts = {account.handle: account for account in db.iter_accounts()}
    for event in _target_events(db, start, end, scenario_handle):
        _, covered = escrow_recognition(
            ((split.account, split.amount) for split in event.expected_splits), accounts
        )
        for split in event.expected_splits:
            account = accounts.get(split.account)
            if account is None or account.account_class not in (
                AccountClass.INCOME,
                AccountClass.EXPENSE,
            ):
                continue
            key = (account.handle, _month_start(event.planned_date))
            amount = split.amount * account.sign() - covered.get(account.handle, Money(0))
            totals[key] = totals.get(key, Money(0)) + amount
    return totals


def _corresponding_future_month(historical: date, future_start: date) -> date:
    """Map a historical calendar month into the exact rolling future year."""
    year = future_start.year + (historical.month < future_start.month)
    return date(year, historical.month, 1)


def _residual_after_scheduled(actual: Money, scheduled: Money) -> tuple[Money, Money]:
    """Return residual history and the scheduled amount that actually covered it."""
    if actual > 0 and scheduled > 0:
        residual = max(actual - scheduled, Money(0))
    elif actual < 0 and scheduled < 0:
        residual = min(actual - scheduled, Money(0))
    else:
        residual = actual
    return residual, actual - residual


def _bridge_end_before_continuing_coverage(
    account: str,
    future_start: date,
    planned: dict[tuple[str, date], Money],
    gross_by_month: dict[int, list[Money]],
    gross_default: Money,
) -> date | None:
    """End a monthly bridge before sustained full coverage begins.

    Two or more fully covered months through the end of the rolling future year
    are required. That distinguishes a continuing replacement from an isolated
    annual or one-time event, which must not truncate otherwise uncovered months.
    """
    covered: list[bool] = []
    for offset in range(12):
        month = _add_months(future_start, offset)
        samples = gross_by_month.get(month.month, [])
        need = _typical_amount(samples) if samples else gross_default
        residual, _applied = _residual_after_scheduled(
            need, planned.get((account, month), Money(0))
        )
        covered.append(not residual)
    for offset in range(1, 11):
        if not any(not value for value in covered[:offset]):
            continue
        if all(covered[offset:]):
            return _add_months(future_start, offset) - timedelta(days=1)
    return None


def _unscheduled_dates(db: DbSQLite, category: str, start: date, end: date) -> list[date]:
    dates: list[date] = []
    for txn in db.iter_transactions(account=category, start=start, end=end):
        if txn.planned_occurrence:
            continue
        if txn.value_for(category):
            dates.append(txn.post_date)
    return sorted(dates)


def _next_yearly_start(last: date, interval: int, floor: date) -> date:
    """Advance an observed annual cadence to its first future occurrence."""
    year = last.year + interval
    candidate = date(year, last.month, min(last.day, monthrange(year, last.month)[1]))
    while candidate < floor:
        year += interval
        candidate = date(year, last.month, min(last.day, monthrange(year, last.month)[1]))
    return candidate


def _infer_recurrence(dates: list[date], start: date) -> tuple[Recurrence | None, str, Decimal]:
    if len(dates) < 2:
        return Recurrence(PeriodType.ONCE, start=start), "once (single observation)", Decimal("1")
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
    for years in range(1, 4):
        if abs(typical_gap - 365.2425 * years) <= 70:
            cadence = "annual" if years == 1 else f"every {years} years"
            return (
                Recurrence(
                    PeriodType.YEAR,
                    interval=years,
                    start=_next_yearly_start(dates[-1], years, start),
                ),
                cadence,
                Decimal("1"),
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
    planned_profiles = _planned_category_profiles(db, current_month, scenario_handle)
    accounts_by_handle = {account.handle: account for account in db.iter_accounts()}

    for account in db.iter_accounts():
        if account.is_root or account.placeholder:
            continue
        if account.account_class not in (AccountClass.INCOME, AccountClass.EXPENSE):
            continue

        monthly: list[Money] = []
        monthly_by_month: dict[int, list[Money]] = {}
        gross_monthly: list[Money] = []
        gross_by_month: dict[int, list[Money]] = {}
        txn_count = 0
        applied_scheduled_total = Money(0)
        for offset in range(months):
            start = _add_months(history_start, offset)
            end = _add_months(start, 1) - timedelta(days=1)
            total = Money(0)
            for txn in db.iter_transactions(account=account.handle, start=start, end=end):
                value = txn.value_for(account.handle) * account.sign()
                _, covered = escrow_recognition(
                    ((split.account, split.value) for split in txn.splits),
                    accounts_by_handle,
                )
                value = value - covered.get(account.handle, Money(0))
                if value:
                    total = total + value
                    txn_count += 1
            if total:
                gross_monthly.append(total)
                gross_by_month.setdefault(start.month, []).append(total)
            future_month = _corresponding_future_month(start, current_month)
            scheduled = planned_profiles.get((account.handle, future_month), Money(0))
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
        if recurrence is None:
            continue
        if recurrence.period is PeriodType.MONTH and recurrence.interval == 1:
            recurrence.end = _bridge_end_before_continuing_coverage(
                account.handle,
                current_month,
                planned_profiles,
                gross_by_month,
                _typical_amount(gross_monthly),
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
        gross_median = _typical_amount(gross_monthly)
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
                    f"{cadence}; historical median {gross_median.format()} across "
                    f"{len(gross_monthly)} active month(s); {scheduled_total.format()} "
                    f"of selected future plan applied across {months} completed month(s); "
                    f"median uncovered {monthly_residual.format()} across "
                    f"{len(monthly)} month(s)"
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


def _proposal_splits(db: DbSQLite, proposal: HistoricalEstimateProposal) -> list[ScheduledSplit]:
    category = db.get_account(proposal.category)
    funding = db.get_account(proposal.funding)
    if category is None or funding is None:
        raise ValueError("proposal accounts no longer exist")
    signed = proposal.amount * category.sign()
    return [
        ScheduledSplit(category.handle, signed),
        ScheduledSplit(funding.handle, -signed),
    ]


def draft_historical_estimate(
    db: DbSQLite, proposal: HistoricalEstimateProposal
) -> ScheduledTransaction:
    """Build an editable, unsaved Base estimate from one analyzer proposal."""
    name = f"Estimated {proposal.category_name}"
    draft = ScheduledTransaction(
        name=name,
        recurrence=Recurrence.from_dict(proposal.recurrence.serialize()),
        splits=_proposal_splits(db, proposal),
        auto_create=False,
        seasonal_amounts=[
            ScheduledMonthAmount.from_dict(item.serialize()) for item in proposal.seasonal_amounts
        ],
    )
    draft.placeholder = True
    return draft


def draft_scenario_estimate(db: DbSQLite, proposal: HistoricalEstimateProposal) -> ScenarioSchedule:
    """Build an editable, unsaved scenario estimate from one analyzer proposal."""
    return ScenarioSchedule(
        name=f"Estimated {proposal.category_name}",
        recurrence=Recurrence.from_dict(proposal.recurrence.serialize()),
        splits=_proposal_splits(db, proposal),
        placeholder=True,
        seasonal_amounts=[
            ScheduledMonthAmount.from_dict(item.serialize()) for item in proposal.seasonal_amounts
        ],
    )


def accept_historical_estimate(
    db: DbSQLite,
    proposal: HistoricalEstimateProposal,
    *,
    scenario_handle: str | None = None,
) -> str:
    """Persist a proposal as an ordinary Base or scenario estimate."""

    if scenario_handle is None:
        baseline_schedule = draft_historical_estimate(db, proposal)
        with db.transaction(f"Add historical estimate {proposal.category_name}") as txn:
            db.add_scheduled(baseline_schedule, txn)
        return baseline_schedule.handle

    scenario = db.get_scenario(scenario_handle)
    if scenario is None:
        raise ValueError("saved scenario no longer exists")
    scenario_schedule = draft_scenario_estimate(db, proposal)
    scenario.schedule_overrides.append(scenario_schedule)
    with db.transaction(f"Add historical estimate to {scenario.name}") as txn:
        db.commit_scenario(scenario, txn)
    return scenario_schedule.handle
