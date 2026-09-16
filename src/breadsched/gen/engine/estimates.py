"""Historical category activity turned into reviewable planning estimates.

The analyzer deliberately produces suggestions rather than silently changing the
book. Saved suggestions become ordinary scheduled estimates, so Plan,
Projection, Review, and scenarios continue to consume one event model.
"""

from __future__ import annotations

from calendar import monthrange
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass
from ..lib.money import Money
from ..lib.recurrence import PeriodType, Recurrence
from ..lib.scenario import ScenarioSchedule
from ..lib.scheduled import ScheduledMonthAmount, ScheduledSplit, ScheduledTransaction
from ..lib.transaction import InvestmentActivityKind, PlanningFlowKind, Split
from . import activity, planning
from .escrow import recognition as escrow_recognition

__all__ = [
    "EstimateEvidence",
    "EstimateHistoryEvidence",
    "HistoricalEstimateProposal",
    "accept_historical_estimate",
    "draft_historical_estimate",
    "draft_scenario_estimate",
    "propose_historical_estimates",
]


@dataclass(frozen=True, slots=True)
class EstimateHistoryEvidence:
    """One completed historical month and how it affected the estimate."""

    month: date
    gross: Money
    planned: Money
    residual: Money
    selected: bool
    exclusion: str | None = None

    def serialize(self) -> dict[str, object]:
        return {
            "month": self.month.isoformat(),
            "gross": self.gross,
            "planned": self.planned,
            "residual": self.residual,
            "selected": self.selected,
            "exclusion": self.exclusion,
        }


@dataclass(frozen=True, slots=True)
class EstimateCadenceEvidence:
    """Observed dates and the deterministic cadence selected from them."""

    label: str
    observed_dates: tuple[date, ...]
    typical_gap_days: float | None
    occurrences_per_month: Decimal
    explanation: str

    def serialize(self) -> dict[str, object]:
        return {
            "label": self.label,
            "observed_dates": [value.isoformat() for value in self.observed_dates],
            "typical_gap_days": self.typical_gap_days,
            "occurrences_per_month": str(self.occurrences_per_month),
            "explanation": self.explanation,
        }


@dataclass(frozen=True, slots=True)
class EstimateTrendEvidence:
    """Conservative comparison between the earlier and later sample."""

    direction: str
    change_percent: Decimal
    used_recent_months: int
    explanation: str

    def serialize(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "change_percent": str(self.change_percent),
            "used_recent_months": self.used_recent_months,
            "explanation": self.explanation,
        }


@dataclass(frozen=True, slots=True)
class EstimateSeasonalityEvidence:
    """Calendar-month pattern and the month-specific amounts it produced."""

    detected: bool
    variability: str
    monthly_amounts: tuple[ScheduledMonthAmount, ...]
    explanation: str

    def serialize(self) -> dict[str, object]:
        return {
            "detected": self.detected,
            "variability": self.variability,
            "monthly_amounts": [item.serialize() for item in self.monthly_amounts],
            "explanation": self.explanation,
        }


@dataclass(frozen=True, slots=True)
class EstimateConfidenceEvidence:
    """Named inputs to the proposal confidence score."""

    score: float
    coverage: Decimal
    depth: Decimal
    retained_ratio: Decimal
    consistency: Decimal
    explanation: str

    def serialize(self) -> dict[str, object]:
        return {
            "score": self.score,
            "coverage": str(self.coverage),
            "depth": str(self.depth),
            "retained_ratio": str(self.retained_ratio),
            "consistency": str(self.consistency),
            "explanation": self.explanation,
        }


@dataclass(frozen=True, slots=True)
class EstimateFundingCandidate:
    """One observed non-flow counterpart considered as the funding account."""

    account: str
    account_name: str
    transaction_count: int
    spendable_cash: bool

    def serialize(self) -> dict[str, object]:
        return {
            "account": self.account,
            "account_name": self.account_name,
            "transaction_count": self.transaction_count,
            "spendable_cash": self.spendable_cash,
        }


@dataclass(frozen=True, slots=True)
class EstimateFundingEvidence:
    """Candidates and tie-break used to infer the proposal counterpart."""

    selected: str
    selected_name: str
    candidates: tuple[EstimateFundingCandidate, ...]
    explanation: str

    def serialize(self) -> dict[str, object]:
        return {
            "selected": self.selected,
            "selected_name": self.selected_name,
            "candidates": [item.serialize() for item in self.candidates],
            "explanation": self.explanation,
        }


@dataclass(frozen=True, slots=True)
class EstimateEvidence:
    """Shared, structured explanation for one historical estimate proposal."""

    history_start: date
    history_end: date
    history: tuple[EstimateHistoryEvidence, ...]
    cadence: EstimateCadenceEvidence
    trend: EstimateTrendEvidence | None
    seasonality: EstimateSeasonalityEvidence
    confidence: EstimateConfidenceEvidence
    funding: EstimateFundingEvidence
    residual_explanation: str

    @property
    def selected_months(self) -> tuple[EstimateHistoryEvidence, ...]:
        return tuple(item for item in self.history if item.selected)

    @property
    def exclusions(self) -> tuple[EstimateHistoryEvidence, ...]:
        return tuple(item for item in self.history if item.exclusion is not None)

    def serialize(self) -> dict[str, object]:
        return {
            "history_start": self.history_start.isoformat(),
            "history_end": self.history_end.isoformat(),
            "history": [item.serialize() for item in self.history],
            "selected_months": len(self.selected_months),
            "exclusions": [item.serialize() for item in self.exclusions],
            "cadence": self.cadence.serialize(),
            "trend": self.trend.serialize() if self.trend else None,
            "seasonality": self.seasonality.serialize(),
            "confidence": self.confidence.serialize(),
            "funding": self.funding.serialize(),
            "residual_explanation": self.residual_explanation,
        }

    def summary_lines(self) -> tuple[str, ...]:
        trend = self.trend.explanation if self.trend else "No material trend detected."
        return (
            f"History: {len(self.selected_months)} selected month(s), "
            f"{len(self.exclusions)} explained exclusion(s).",
            f"Cadence: {self.cadence.explanation}",
            f"Residuals: {self.residual_explanation}",
            f"Trend: {trend}",
            f"Seasonality: {self.seasonality.explanation}",
            f"Funding: {self.funding.explanation}",
            f"Confidence: {self.confidence.explanation}",
        )


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
    evidence: EstimateEvidence
    scheduled_amount: Money = Money(0)
    seasonal: bool = False
    trend: str | None = None
    seasonal_amounts: tuple[ScheduledMonthAmount, ...] = ()
    outlier_months: int = 0
    variability: str = "unknown"
    planning_flow: PlanningFlowKind | None = None
    investment_activity: InvestmentActivityKind | None = None
    ledger_amount: Money | None = None

    @property
    def key(self) -> str:
        """Stable review identity, including classifications that may share an account."""
        flow = self.planning_flow.value if self.planning_flow else ""
        investment = self.investment_activity.value if self.investment_activity else ""
        return f"{self.category}:{self.funding}:{flow}:{investment}"

    @property
    def purpose_name(self) -> str:
        """User-facing purpose without disguising a balance movement as a category."""
        if self.planning_flow is not None:
            return self.planning_flow.label
        if self.investment_activity is not None:
            return self.investment_activity.label
        return self.category_name

    @property
    def estimate_name(self) -> str:
        """Schedule name that distinguishes classified purposes on one account."""
        if self.planning_flow is None and self.investment_activity is None:
            return f"Estimated {self.category_name}"
        return f"Estimated {self.purpose_name} — {self.category_name}"

    @property
    def source_name(self) -> str:
        """Display name of the account money historically flowed from."""
        if self.ledger_amount is not None:
            return self.funding_name if self.ledger_amount >= 0 else self.category_name
        return self.funding_name if self.amount >= 0 else self.category_name

    @property
    def destination_name(self) -> str:
        """Display name of the account money historically flowed to."""
        if self.ledger_amount is not None:
            return self.category_name if self.ledger_amount >= 0 else self.funding_name
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


def _robust_sample(values: list[Money]) -> tuple[list[Money], tuple[int, ...], Decimal]:
    """Exclude isolated monthly spikes and return robust relative variability.

    A median already prevents one large value from moving the estimate very far,
    but naming excluded anomalies makes confidence meaningful and prevents an
    even-sized sample from averaging a normal value with a one-off event. Six
    observations are required before excluding anything; shorter histories remain
    visible exactly as recorded.
    """
    if not values:
        return [], (), Decimal(0)
    decimals = [value.to_decimal() for value in values]
    center = median(decimals)
    deviations = [abs(value - center) for value in decimals]
    mad = median(deviations)
    magnitude = abs(center)
    relative_variability = mad / magnitude if magnitude else Decimal(0)
    if len(values) < 6:
        return values, (), relative_variability

    # Three median absolute deviations is intentionally conservative. When most
    # months are identical MAD is zero, use a generous relative threshold so a
    # single annual purchase does not become the ordinary monthly estimate.
    threshold = mad * Decimal(3) if mad else max(magnitude * Decimal("0.75"), Decimal("1"))
    excluded = tuple(index for index, deviation in enumerate(deviations) if deviation > threshold)
    retained = [value for index, value in enumerate(values) if index not in excluded]
    # Never let anomaly handling erase the evidence required to make a proposal.
    if len(retained) < 3:
        return values, (), relative_variability
    retained_decimals = [value.to_decimal() for value in retained]
    retained_center = median(retained_decimals)
    retained_mad = median(abs(value - retained_center) for value in retained_decimals)
    retained_magnitude = abs(retained_center)
    variability = retained_mad / retained_magnitude if retained_magnitude else Decimal(0)
    return retained, excluded, variability


def _funding_evidence(
    db: DbSQLite, category: str, start: date, end: date
) -> EstimateFundingEvidence | None:
    counts: Counter[str] = Counter()
    for txn in db.iter_transactions(account=category, start=start, end=end):
        for split in txn.splits:
            if split.account == category:
                continue
            account = db.get_account(split.account)
            if account is None or account.atype.is_flow or account.is_root:
                continue
            counts[split.account] += 1
    return _funding_evidence_from_counts(db, counts)


def _funding_evidence_from_counts(
    db: DbSQLite, counts: Counter[str]
) -> EstimateFundingEvidence | None:
    candidates = []
    for handle, transaction_count in counts.items():
        account = db.get_account(handle)
        if account is None:
            continue
        candidates.append(
            EstimateFundingCandidate(
                handle,
                db.full_name(account),
                transaction_count,
                account.is_spendable_cash,
            )
        )
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (item.transaction_count, item.spendable_cash, item.account),
        reverse=True,
    )
    selected = candidates[0]
    explanation = (
        f"{selected.account_name} appeared as the counterpart in "
        f"{selected.transaction_count} qualifying transaction(s)"
    )
    if len(candidates) > 1:
        explanation += "; transaction count wins, with spendable cash as the tie-break"
    else:
        explanation += "; it was the only qualifying counterpart"
    return EstimateFundingEvidence(
        selected.account,
        selected.account_name,
        tuple(candidates),
        explanation + ".",
    )


_INVESTMENT_PERFORMANCE_ACTIVITY = {
    InvestmentActivityKind.DIVIDEND,
    InvestmentActivityKind.INTEREST,
    InvestmentActivityKind.FEE,
    InvestmentActivityKind.ROLLOVER,
}

_PLANNABLE_INVESTMENT_ACTIVITY = {
    InvestmentActivityKind.CONTRIBUTION,
    InvestmentActivityKind.WITHDRAWAL,
    InvestmentActivityKind.RETIREMENT_DISTRIBUTION,
}

_FlowSignature = tuple[
    str,
    PlanningFlowKind | None,
    InvestmentActivityKind | None,
]


def _is_investment_performance(splits: Iterable[Split | planning.PlannedSplit]) -> bool:
    """Whether flow-account legs are investment bookkeeping, not household cash need."""
    return any(split.investment_activity in _INVESTMENT_PERFORMANCE_ACTIVITY for split in splits)


def _planned_views(
    splits: Iterable[Split | planning.PlannedSplit],
) -> tuple[planning.PlannedSplit, ...]:
    """Normalize ledger and expected splits for shared classification."""
    return tuple(
        split
        if isinstance(split, planning.PlannedSplit)
        else planning.PlannedSplit(
            split.account,
            split.value,
            split.planning_flow,
            split.investment_activity,
        )
        for split in splits
    )


def _classified_flow_splits(
    splits: Iterable[Split | planning.PlannedSplit],
    accounts: dict[str, Account],
) -> list[tuple[planning.PlannedSplit, PlanningFlowKind | None, InvestmentActivityKind | None]]:
    """Return each planning-relevant balance leg once with shared classifications."""
    views = _planned_views(splits)
    classified: list[
        tuple[planning.PlannedSplit, PlanningFlowKind | None, InvestmentActivityKind | None]
    ] = []
    for split in views:
        account = accounts.get(split.account)
        if account is None or account.atype.is_flow or account.is_root:
            continue
        flow = activity.inferred_planning_flow(split, views, accounts)
        investment = split.investment_activity
        if flow is PlanningFlowKind.ESCROW_FUNDING:
            continue
        if flow is None and investment not in _PLANNABLE_INVESTMENT_ACTIVITY:
            continue
        # Some importers annotate both sides of a distribution. The holding leg
        # is the economic event; its cash counterpart must not become a duplicate.
        if account.is_spendable_cash and any(
            peer.account != split.account
            and (peer_account := accounts.get(peer.account)) is not None
            and not peer_account.is_spendable_cash
            and (
                activity.inferred_planning_flow(peer, views, accounts) is flow
                or (investment is not None and peer.investment_activity is investment)
            )
            for peer in views
        ):
            continue
        classified.append((split, flow, investment))
    return classified


def _economic_flow_amount(
    split: planning.PlannedSplit,
    flow: PlanningFlowKind | None,
    investment: InvestmentActivityKind | None,
) -> Money:
    if flow is not None:
        return flow.plan_amount(split.amount)
    if investment is not None:
        return split.amount * investment.direction
    return Money(0)


def _flow_counterpart(
    split: planning.PlannedSplit,
    splits: Iterable[Split | planning.PlannedSplit],
    accounts: dict[str, Account],
) -> Account | None:
    """Choose the account that makes the proposed event useful for cash planning."""
    candidates = [
        account
        for item in _planned_views(splits)
        if item.account != split.account
        and (account := accounts.get(item.account)) is not None
        and not account.is_root
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda account: (
            account.is_spendable_cash,
            not account.atype.is_flow,
            account.handle,
        ),
    )


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
        if _is_investment_performance(event.expected_splits):
            continue
        escrow = escrow_recognition(
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
            amount = (
                split.amount * account.sign()
                - escrow.covered_expenses.get(account.handle, Money(0))
                + escrow.restored_expenses.get(account.handle, Money(0))
            )
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
        if _is_investment_performance(txn.splits):
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


def _next_monthly_start(last: date, interval: int, floor: date) -> date:
    """Advance a calendar-month cadence without losing its observed day."""
    candidate = last
    while candidate < floor:
        index = candidate.year * 12 + candidate.month - 1 + interval
        year, zero_month = divmod(index, 12)
        month = zero_month + 1
        candidate = date(year, month, min(last.day, monthrange(year, month)[1]))
    return candidate


def _calendar_month_interval(dates: list[date]) -> int | None:
    """Recognize stable every-N-month activity despite varying day-of-month."""
    if len(dates) < 3:
        return None
    gaps = [
        (later.year - earlier.year) * 12 + later.month - earlier.month
        for earlier, later in zip(dates[:-1], dates[1:], strict=True)
    ]
    positive = [gap for gap in gaps if gap > 0]
    if len(positive) < 2:
        return None
    interval = int(median(positive))
    if not 2 <= interval <= 11:
        return None
    supporting = sum(abs(gap - interval) <= 1 for gap in positive)
    return interval if supporting * 4 >= len(positive) * 3 else None


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
    month_interval = _calendar_month_interval(dates)
    if month_interval is not None:
        return (
            Recurrence(
                PeriodType.MONTH,
                interval=month_interval,
                start=_next_monthly_start(dates[-1], month_interval, start),
            ),
            f"every {month_interval} months",
            Decimal("1"),
        )
    return Recurrence(PeriodType.MONTH, start=start), "monthly", Decimal("1")


def _cadence_evidence(
    dates: list[date], label: str, occurrences_per_month: Decimal
) -> EstimateCadenceEvidence:
    gaps = [(later - earlier).days for earlier, later in zip(dates[:-1], dates[1:], strict=True)]
    typical_gap = float(median(gaps)) if gaps else None
    if len(dates) < 2:
        explanation = "one observation supports a one-time proposal."
    elif label in {"weekly", "fortnightly"}:
        explanation = f"{label} from a typical {typical_gap:g}-day gap across {len(dates)} dates."
    elif label.startswith("every ") and label.endswith(" months"):
        explanation = (
            f"{label} from stable calendar-month spacing across {len(dates)} dates, "
            "allowing posting-day drift."
        )
    elif label == "annual" or label.endswith(" years"):
        explanation = f"{label} from the typical {typical_gap:g}-day gap."
    else:
        explanation = (
            f"monthly fallback because {len(dates)} observed date(s) did not form a "
            "stable weekly, multi-month, or annual cadence."
        )
    return EstimateCadenceEvidence(
        label,
        tuple(dates),
        typical_gap,
        occurrences_per_month,
        explanation,
    )


def _confidence(
    *, sample_months: int, active_months: int, retained_months: int, variability: Decimal
) -> EstimateConfidenceEvidence:
    """Score evidence coverage, sample depth, anomalies, and amount stability."""
    coverage = Decimal(active_months) / Decimal(sample_months)
    depth = min(Decimal(1), Decimal(retained_months) / Decimal(12))
    retained_ratio = Decimal(retained_months) / Decimal(active_months)
    consistency = max(Decimal(0), Decimal(1) - min(variability, Decimal(1)))
    score = (
        Decimal("0.20")
        + coverage * Decimal("0.30")
        + depth * Decimal("0.20")
        + retained_ratio * Decimal("0.10")
        + consistency * Decimal("0.20")
    )
    bounded = float(min(Decimal("0.95"), score))
    return EstimateConfidenceEvidence(
        score=bounded,
        coverage=coverage,
        depth=depth,
        retained_ratio=retained_ratio,
        consistency=consistency,
        explanation=(
            f"{bounded:.0%}: {active_months}/{sample_months} months active, "
            f"{retained_months}/{active_months} active months retained, "
            f"amount consistency {consistency:.0%}."
        ),
    )


def _variability_label(value: Decimal) -> str:
    if value <= Decimal("0.10"):
        return "stable"
    if value <= Decimal("0.25"):
        return "moderately variable"
    return "highly variable"


def _trend_summary(values: list[Money]) -> tuple[list[Money], EstimateTrendEvidence | None]:
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
    percent = abs(change) * Decimal(100)
    explanation = (
        f"{direction} trend ({percent:.1f}%); using the recent {len(recent)}-month median."
    )
    return recent, EstimateTrendEvidence(direction, percent, len(recent), explanation)


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


def _planned_flow_profiles(
    db: DbSQLite,
    start: date,
    scenario_handle: str | None,
    accounts: dict[str, Account],
) -> dict[tuple[_FlowSignature, date], Money]:
    """Return matching future coverage for each classified economic purpose."""
    end = _add_months(start, 12) - timedelta(days=1)
    totals: dict[tuple[_FlowSignature, date], Money] = {}
    for event in _target_events(db, start, end, scenario_handle):
        for split, flow, investment in _classified_flow_splits(event.expected_splits, accounts):
            amount = _economic_flow_amount(split, flow, investment)
            if amount <= 0:
                continue
            signature = (split.account, flow, investment)
            key = (signature, _month_start(event.planned_date))
            totals[key] = totals.get(key, Money(0)) + amount
    return totals


def _propose_classified_flows(
    db: DbSQLite,
    *,
    history_start: date,
    history_end: date,
    future_start: date,
    months: int,
    min_active_months: int,
    scenario_handle: str | None,
    accounts: dict[str, Account],
) -> list[HistoricalEstimateProposal]:
    """Infer recurring balance-sheet purposes without relabeling them as categories."""
    monthly: dict[tuple[_FlowSignature, date], Money] = {}
    dates: dict[_FlowSignature, list[date]] = {}
    counterpart_counts: dict[_FlowSignature, Counter[str]] = {}
    transaction_counts: Counter[_FlowSignature] = Counter()

    for txn in db.iter_transactions(start=history_start, end=history_end):
        transaction_amounts: dict[_FlowSignature, Money] = {}
        representatives: dict[_FlowSignature, planning.PlannedSplit] = {}
        for split, flow, investment in _classified_flow_splits(txn.splits, accounts):
            amount = _economic_flow_amount(split, flow, investment)
            if amount <= 0:
                continue
            signature = (split.account, flow, investment)
            transaction_amounts[signature] = transaction_amounts.get(signature, Money(0)) + amount
            representatives.setdefault(signature, split)
        for signature, amount in transaction_amounts.items():
            key = (signature, _month_start(txn.post_date))
            monthly[key] = monthly.get(key, Money(0)) + amount
            transaction_counts[signature] += 1
            if not txn.planned_occurrence:
                dates.setdefault(signature, []).append(txn.post_date)
            counterpart = _flow_counterpart(representatives[signature], txn.splits, accounts)
            if counterpart is not None:
                counterpart_counts.setdefault(signature, Counter())[counterpart.handle] += 1

    planned = _planned_flow_profiles(db, future_start, scenario_handle, accounts)
    proposals: list[HistoricalEstimateProposal] = []
    signatures = {signature for signature, _month in monthly}
    for signature in sorted(
        signatures, key=lambda item: (db.full_name(accounts[item[0]]), str(item))
    ):
        target, flow, investment = signature
        target_account = accounts[target]
        residual_monthly: list[Money] = []
        residual_row_indexes: list[int] = []
        history_inputs: list[tuple[date, Money, Money, Money]] = []
        scheduled_total = Money(0)
        for offset in range(months):
            historical_month = _add_months(history_start, offset)
            actual = monthly.get((signature, historical_month), Money(0))
            future_month = _corresponding_future_month(historical_month, future_start)
            residual, applied = _residual_after_scheduled(
                actual, planned.get((signature, future_month), Money(0))
            )
            scheduled_total = scheduled_total + applied
            history_inputs.append((historical_month, actual, applied, residual))
            if residual:
                residual_row_indexes.append(len(history_inputs) - 1)
                residual_monthly.append(residual)
        if len(residual_monthly) < min_active_months:
            continue
        funding_evidence = _funding_evidence_from_counts(
            db, counterpart_counts.get(signature, Counter())
        )
        if funding_evidence is None:
            continue
        funding_handle = funding_evidence.selected
        observed_dates = sorted(dates.get(signature, []))
        recurrence, cadence, occurrences_per_month = _infer_recurrence(observed_dates, future_start)
        if recurrence is None:
            continue
        robust, excluded_indexes, variability_value = _robust_sample(residual_monthly)
        excluded_rows = {residual_row_indexes[index] for index in excluded_indexes}
        history = tuple(
            EstimateHistoryEvidence(
                month,
                actual,
                applied,
                residual,
                bool(residual) and index not in excluded_rows,
                (
                    "isolated amount outlier"
                    if index in excluded_rows
                    else (
                        "fully covered by the selected future plan"
                        if actual and not residual
                        else ("no qualifying activity" if not actual else None)
                    )
                ),
            )
            for index, (month, actual, applied, residual) in enumerate(history_inputs)
        )
        trend_sample, trend_evidence = _trend_summary(robust)
        monthly_residual = _typical_amount(trend_sample)
        amount = (monthly_residual / occurrences_per_month).quantize(100)
        if amount <= 0:
            continue
        funding_account = accounts[funding_handle]
        ledger_amount = (
            flow.ledger_amount(amount)
            if flow is not None
            else amount * (investment.direction if investment is not None else 0)
        )
        variability = _variability_label(variability_value)
        confidence_evidence = _confidence(
            sample_months=months,
            active_months=len(residual_monthly),
            retained_months=len(robust),
            variability=variability_value,
        )
        cadence_evidence = _cadence_evidence(observed_dates, cadence, occurrences_per_month)
        seasonality_evidence = EstimateSeasonalityEvidence(
            False,
            variability,
            (),
            f"No repeated calendar-month profile was applied; amounts are {variability}.",
        )
        if flow is not None:
            purpose = flow.label
        elif investment is not None:
            purpose = investment.label
        else:  # pragma: no cover - signatures are created only for classified splits
            continue
        gross_values = [
            monthly_value
            for (candidate, _month), monthly_value in monthly.items()
            if candidate == signature
        ]
        evidence = EstimateEvidence(
            history_start,
            history_end,
            history,
            cadence_evidence,
            trend_evidence,
            seasonality_evidence,
            confidence_evidence,
            funding_evidence,
            (
                f"Matching future classified plan contributed {scheduled_total.format()}; "
                f"the retained median uncovered amount is {monthly_residual.format()}."
            ),
        )
        trend = trend_evidence.explanation.rstrip(".") if trend_evidence else None
        outlier_months = len(excluded_indexes)
        proposals.append(
            HistoricalEstimateProposal(
                category=target,
                category_name=db.full_name(target_account),
                funding=funding_handle,
                funding_name=db.full_name(funding_account),
                amount=amount,
                recurrence=recurrence,
                sample_months=months,
                active_months=len(residual_monthly),
                transaction_count=transaction_counts[signature],
                confidence=confidence_evidence.score,
                reason=(
                    f"{cadence}; classified as {purpose}; historical median "
                    f"{_typical_amount(gross_values).format()}; "
                    f"{scheduled_total.format()} of matching future classified plan applied; "
                    f"median uncovered {monthly_residual.format()} across "
                    f"{len(residual_monthly)} month(s); {variability} amounts"
                    + (
                        f"; excluded {outlier_months} isolated outlier month(s)"
                        if outlier_months
                        else ""
                    )
                    + (f"; {trend}, using recent median" if trend else "")
                ),
                evidence=evidence,
                scheduled_amount=scheduled_total,
                trend=trend,
                outlier_months=outlier_months,
                variability=variability,
                planning_flow=flow,
                investment_activity=investment,
                ledger_amount=ledger_amount,
            )
        )
    return proposals


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
        residual_row_indexes: list[int] = []
        history_inputs: list[tuple[date, Money, Money, Money]] = []
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
                if _is_investment_performance(txn.splits):
                    continue
                value = txn.value_for(account.handle) * account.sign()
                escrow = escrow_recognition(
                    ((split.account, split.value) for split in txn.splits),
                    accounts_by_handle,
                )
                value = (
                    value
                    - escrow.covered_expenses.get(account.handle, Money(0))
                    + escrow.restored_expenses.get(account.handle, Money(0))
                )
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
            history_inputs.append((start, total, applied_scheduled, residual))
            if residual:
                residual_row_indexes.append(len(history_inputs) - 1)
                monthly.append(residual)
                monthly_by_month.setdefault(start.month, []).append(residual)

        if len(monthly) < min_active_months:
            continue
        funding_evidence = _funding_evidence(db, account.handle, history_start, history_end)
        if funding_evidence is None:
            continue
        funding_account = db.get_account(funding_evidence.selected)
        if funding_account is None:
            continue

        seasonal = _has_seasonality(monthly_by_month)
        if seasonal:
            # Repeated winter/summer peaks are signal, not global outliers. The
            # month-specific profile below preserves them explicitly.
            robust_monthly = monthly
            excluded_indexes: tuple[int, ...] = ()
            relative_variability = Decimal(0)
        else:
            robust_monthly, excluded_indexes, relative_variability = _robust_sample(monthly)
        excluded_rows = {residual_row_indexes[index] for index in excluded_indexes}
        history = tuple(
            EstimateHistoryEvidence(
                month,
                gross,
                applied,
                residual,
                bool(residual) and index not in excluded_rows,
                (
                    "isolated amount outlier"
                    if index in excluded_rows
                    else (
                        "fully covered by the selected future plan"
                        if gross and not residual
                        else ("no qualifying activity" if not gross else None)
                    )
                ),
            )
            for index, (month, gross, applied, residual) in enumerate(history_inputs)
        )
        trend_sample, trend_evidence = _trend_summary(robust_monthly)
        monthly_residual = _typical_amount(trend_sample)
        observed_dates = _unscheduled_dates(db, account.handle, history_start, history_end)
        recurrence, cadence, occurrences_per_month = _infer_recurrence(
            observed_dates,
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
        variability = (
            "seasonal by calendar month" if seasonal else _variability_label(relative_variability)
        )
        confidence_evidence = _confidence(
            sample_months=months,
            active_months=len(monthly),
            retained_months=len(robust_monthly),
            variability=relative_variability,
        )
        scheduled_total = applied_scheduled_total
        gross_median = _typical_amount(gross_monthly)
        cadence_evidence = _cadence_evidence(observed_dates, cadence, occurrences_per_month)
        seasonality_evidence = EstimateSeasonalityEvidence(
            seasonal,
            variability,
            seasonal_amounts,
            (
                f"Repeated calendar-month variation detected; {len(seasonal_amounts)} "
                "month-specific amount(s) will be retained."
                if seasonal
                else f"No repeated calendar-month pattern detected; amounts are {variability}."
            ),
        )
        evidence = EstimateEvidence(
            history_start,
            history_end,
            history,
            cadence_evidence,
            trend_evidence,
            seasonality_evidence,
            confidence_evidence,
            funding_evidence,
            (
                f"Selected future plan contributed {scheduled_total.format()}; "
                f"the retained median uncovered amount is {monthly_residual.format()}."
            ),
        )
        trend = trend_evidence.explanation.rstrip(".") if trend_evidence else None
        outlier_months = len(excluded_indexes)
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
                confidence=confidence_evidence.score,
                reason=(
                    f"{cadence}; historical median {gross_median.format()} across "
                    f"{len(gross_monthly)} active month(s); {scheduled_total.format()} "
                    f"of selected future plan applied across {months} completed month(s); "
                    f"median uncovered {monthly_residual.format()} across "
                    f"{len(monthly)} month(s); {variability} amounts"
                    + (
                        f"; excluded {outlier_months} isolated outlier month(s)"
                        if outlier_months
                        else ""
                    )
                    + (f"; {trend}, using recent median" if trend else "")
                    + ("; recurring seasonal variation detected" if seasonal else "")
                ),
                evidence=evidence,
                scheduled_amount=scheduled_total,
                seasonal=seasonal,
                trend=trend,
                seasonal_amounts=seasonal_amounts,
                outlier_months=outlier_months,
                variability=variability,
            )
        )

    proposals.extend(
        _propose_classified_flows(
            db,
            history_start=history_start,
            history_end=history_end,
            future_start=current_month,
            months=months,
            min_active_months=min_active_months,
            scenario_handle=scenario_handle,
            accounts=accounts_by_handle,
        )
    )
    return sorted(proposals, key=lambda item: (item.purpose_name, item.category_name))


def _proposal_splits(db: DbSQLite, proposal: HistoricalEstimateProposal) -> list[ScheduledSplit]:
    category = db.get_account(proposal.category)
    funding = db.get_account(proposal.funding)
    if category is None or funding is None:
        raise ValueError("proposal accounts no longer exist")
    signed = (
        proposal.ledger_amount
        if proposal.ledger_amount is not None
        else proposal.amount * category.sign()
    )
    return [
        ScheduledSplit(
            category.handle,
            signed,
            planning_flow=proposal.planning_flow,
            investment_activity=proposal.investment_activity,
        ),
        ScheduledSplit(funding.handle, -signed),
    ]


def draft_historical_estimate(
    db: DbSQLite, proposal: HistoricalEstimateProposal
) -> ScheduledTransaction:
    """Build an editable, unsaved Base estimate from one analyzer proposal."""
    name = proposal.estimate_name
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
        name=proposal.estimate_name,
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
