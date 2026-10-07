"""Statistics over one category's dated history, for historical estimates.

Pure functions of dates, amounts, and the estimate rules: which months are a
robust sample, the typical amount, the recurrence a run of dates implies and when
it would next fall, the trend, seasonality, and how confident a proposal can be.
``estimates`` combines these with the book's planned and actual activity into
reviewable proposals; nothing here reads the book.
"""

from __future__ import annotations

from calendar import monthrange
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from ..lib.money import Money
from ..lib.recurrence import PeriodType, Recurrence
from ..lib.scheduled import ScheduledMonthAmount
from .estimate_rules import HistoricalEstimateRules

__all__ = [
    "EstimateCadenceEvidence",
    "EstimateConfidenceEvidence",
    "EstimateSeasonalityEvidence",
    "EstimateTrendEvidence",
    "add_months",
    "cadence_evidence",
    "calendar_month_interval",
    "confidence",
    "corresponding_future_month",
    "has_seasonality",
    "infer_recurrence",
    "month_start",
    "monthly_category_start",
    "next_monthly_start",
    "next_weekly_start",
    "next_yearly_start",
    "robust_sample",
    "seasonal_amounts",
    "trend_summary",
    "typical_amount",
    "variability_label",
]


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
            "monthly_amounts": [
                {"month": item.month, "amount": str(item.amount.to_decimal())}
                for item in self.monthly_amounts
            ],
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


def month_start(when: date) -> date:
    return when.replace(day=1)


def add_months(when: date, months: int) -> date:
    index = when.year * 12 + when.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def typical_amount(values: list[Money], fraction: int) -> Money:
    decimals = sorted(value.to_decimal() for value in values)
    return Money(median(decimals)).quantize(fraction)


def robust_sample(
    values: list[Money], rules: HistoricalEstimateRules
) -> tuple[list[Money], tuple[int, ...], Decimal]:
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
    policy = rules.anomaly
    if len(values) < policy.minimum_observations:
        return values, (), relative_variability

    # When most months are identical MAD is zero, use the policy's relative and
    # absolute floors so a single annual purchase does not become ordinary history.
    threshold = (
        mad * policy.median_deviation_multiplier
        if mad
        else max(
            magnitude * policy.zero_mad_relative_threshold,
            policy.zero_mad_absolute_floor,
        )
    )
    excluded = tuple(index for index, deviation in enumerate(deviations) if deviation > threshold)
    retained = [value for index, value in enumerate(values) if index not in excluded]
    # Never let anomaly handling erase the evidence required to make a proposal.
    if len(retained) < policy.minimum_retained_observations:
        return values, (), relative_variability
    retained_decimals = [value.to_decimal() for value in retained]
    retained_center = median(retained_decimals)
    retained_mad = median(abs(value - retained_center) for value in retained_decimals)
    retained_magnitude = abs(retained_center)
    variability = retained_mad / retained_magnitude if retained_magnitude else Decimal(0)
    return retained, excluded, variability


def corresponding_future_month(historical: date, future_start: date) -> date:
    """Map a historical calendar month into the exact rolling future year."""
    year = future_start.year + (historical.month < future_start.month)
    return date(year, historical.month, 1)


def next_yearly_start(last: date, interval: int, floor: date) -> date:
    """Advance an observed annual cadence to its first future occurrence."""
    year = last.year + interval
    candidate = date(year, last.month, min(last.day, monthrange(year, last.month)[1]))
    while candidate < floor:
        year += interval
        candidate = date(year, last.month, min(last.day, monthrange(year, last.month)[1]))
    return candidate


def next_monthly_start(last: date, interval: int, floor: date) -> date:
    """Advance a calendar-month cadence without losing its observed day."""
    candidate = last
    while candidate < floor:
        index = candidate.year * 12 + candidate.month - 1 + interval
        year, zero_month = divmod(index, 12)
        month = zero_month + 1
        candidate = date(year, month, min(last.day, monthrange(year, month)[1]))
    return candidate


def calendar_month_interval(dates: list[date], rules: HistoricalEstimateRules) -> int | None:
    """Recognize stable every-N-month activity despite varying day-of-month."""
    policy = rules.cadence
    if len(dates) < policy.calendar_interval_minimum_observations:
        return None
    gaps = [
        (later.year - earlier.year) * 12 + later.month - earlier.month
        for earlier, later in zip(dates[:-1], dates[1:], strict=True)
    ]
    positive = [gap for gap in gaps if gap > 0]
    if len(positive) < policy.calendar_interval_minimum_positive_gaps:
        return None
    interval = int(median(positive))
    if not policy.calendar_month_interval_min <= interval <= policy.calendar_month_interval_max:
        return None
    supporting = sum(abs(gap - interval) <= policy.calendar_month_gap_tolerance for gap in positive)
    return (
        interval
        if supporting * policy.support_denominator >= len(positive) * policy.support_numerator
        else None
    )


def next_weekly_start(dates: list[date], interval: int, floor: date) -> date:
    """Keep a category's observed weekday and, for fortnightly data, its phase."""
    if interval == 2:
        candidate = dates[-1]
        while candidate < floor:
            candidate += timedelta(weeks=2)
        return candidate
    weekday = Counter(item.weekday() for item in dates).most_common(1)[0][0]
    return floor + timedelta(days=(weekday - floor.weekday()) % 7)


def monthly_category_start(dates: list[date], floor: date, rules: HistoricalEstimateRules) -> date:
    """Use a stable once-per-month posting day; otherwise keep the neutral first."""
    by_month: Counter[tuple[int, int]] = Counter((item.year, item.month) for item in dates)
    policy = rules.cadence
    if len(by_month) < policy.monthly_anchor_minimum_months or any(
        count != 1 for count in by_month.values()
    ):
        return floor
    typical_day = int(median(item.day for item in dates))
    if (
        sum(abs(item.day - typical_day) <= policy.monthly_anchor_day_tolerance for item in dates)
        * policy.support_denominator
        < len(dates) * policy.support_numerator
    ):
        return floor
    return date(floor.year, floor.month, min(typical_day, monthrange(floor.year, floor.month)[1]))


def infer_recurrence(
    dates: list[date], start: date, rules: HistoricalEstimateRules
) -> tuple[Recurrence | None, str, Decimal]:
    if len(dates) < 2:
        return Recurrence(PeriodType.ONCE, start=start), "once (single observation)", Decimal("1")
    gaps = [(later - earlier).days for earlier, later in zip(dates[:-1], dates[1:], strict=True)]
    typical_gap = float(median(gaps))
    policy = rules.cadence
    weekly_support = sum(
        policy.weekly_gap_min_days <= gap <= policy.weekly_gap_max_days for gap in gaps
    )
    if (
        len(dates) >= policy.minimum_short_cadence_observations
        and policy.weekly_gap_min_days <= typical_gap <= policy.weekly_gap_max_days
        and weekly_support * policy.support_denominator >= len(gaps) * policy.support_numerator
    ):
        return (
            Recurrence(PeriodType.WEEK, start=next_weekly_start(dates, 1, start)),
            "weekly",
            Decimal(52) / Decimal(12),
        )
    fortnightly_support = sum(
        policy.fortnightly_gap_min_days <= gap <= policy.fortnightly_gap_max_days for gap in gaps
    )
    if (
        len(dates) >= policy.minimum_short_cadence_observations
        and policy.fortnightly_gap_min_days <= typical_gap <= policy.fortnightly_gap_max_days
        and fortnightly_support * policy.support_denominator >= len(gaps) * policy.support_numerator
    ):
        return (
            Recurrence(PeriodType.WEEK, interval=2, start=next_weekly_start(dates, 2, start)),
            "fortnightly",
            Decimal(26) / Decimal(12),
        )
    for years in range(1, policy.maximum_year_interval + 1):
        if abs(Decimal(str(typical_gap)) - policy.mean_year_days * years) <= Decimal(
            policy.annual_tolerance_days
        ):
            cadence = "annual" if years == 1 else f"every {years} years"
            return (
                Recurrence(
                    PeriodType.YEAR,
                    interval=years,
                    start=next_yearly_start(dates[-1], years, start),
                ),
                cadence,
                Decimal("1"),
            )
    month_interval = calendar_month_interval(dates, rules)
    if month_interval is not None:
        return (
            Recurrence(
                PeriodType.MONTH,
                interval=month_interval,
                start=next_monthly_start(dates[-1], month_interval, start),
            ),
            f"every {month_interval} months",
            Decimal("1"),
        )
    return (
        Recurrence(PeriodType.MONTH, start=monthly_category_start(dates, start, rules)),
        "monthly",
        Decimal("1"),
    )


def cadence_evidence(
    dates: list[date],
    label: str,
    occurrences_per_month: Decimal,
    recurrence: Recurrence,
    rules: HistoricalEstimateRules,
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
        policy = rules.cadence
        if (
            len(dates) < policy.minimum_short_cadence_observations
            and typical_gap is not None
            and policy.weekly_gap_min_days <= typical_gap <= policy.fortnightly_gap_max_days
        ):
            explanation = (
                f"monthly fallback: {len(dates)} date(s) are too sparse to prove a "
                "weekly or fortnightly cadence; at least "
                f"{policy.minimum_short_cadence_observations} are required."
            )
        elif recurrence.start.day != 1:
            explanation = (
                f"monthly with a category-specific day-{recurrence.start.day} anchor, "
                f"supported by {len(dates)} stable once-per-month dates."
            )
        else:
            explanation = (
                f"monthly fallback because {len(dates)} observed date(s) did not form a "
                "stable weekly, multi-month, or annual cadence."
            )
    explanation = explanation.rstrip(".") + f" First proposed occurrence: {recurrence.start}."
    return EstimateCadenceEvidence(
        label,
        tuple(dates),
        typical_gap,
        occurrences_per_month,
        explanation,
    )


def confidence(
    *,
    sample_months: int,
    active_months: int,
    retained_months: int,
    variability: Decimal,
    rules: HistoricalEstimateRules,
) -> EstimateConfidenceEvidence:
    """Score evidence coverage, sample depth, anomalies, and amount stability."""
    coverage = Decimal(active_months) / Decimal(sample_months)
    policy = rules.confidence
    depth = min(Decimal(1), Decimal(retained_months) / Decimal(policy.full_depth_months))
    retained_ratio = Decimal(retained_months) / Decimal(active_months)
    consistency = max(Decimal(0), Decimal(1) - min(variability, Decimal(1)))
    score = (
        policy.base_weight
        + coverage * policy.coverage_weight
        + depth * policy.depth_weight
        + retained_ratio * policy.retained_weight
        + consistency * policy.consistency_weight
    )
    bounded = float(min(policy.maximum_score, score))
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


def variability_label(value: Decimal, rules: HistoricalEstimateRules) -> str:
    if value <= rules.variability.stable_maximum:
        return "stable"
    if value <= rules.variability.moderate_maximum:
        return "moderately variable"
    return "highly variable"


def trend_summary(
    values: list[Money], fraction: int, rules: HistoricalEstimateRules
) -> tuple[list[Money], EstimateTrendEvidence | None]:
    """Return the sample to use and a conservative trend label."""
    policy = rules.trend
    if len(values) < policy.minimum_observations:
        return values, None
    split = len(values) // 2
    earlier = typical_amount(values[:split], fraction)
    later = typical_amount(values[split:], fraction)
    if not earlier or (earlier > 0) != (later > 0):
        return values, None
    change = (later.to_decimal() - earlier.to_decimal()) / abs(earlier.to_decimal())
    if abs(change) < policy.minimum_relative_change:
        return values, None
    recent = values[-min(policy.recent_observations, len(values)) :]
    direction = "upward" if change > 0 else "downward"
    percent = abs(change) * Decimal(100)
    explanation = (
        f"{direction} trend ({percent:.1f}%); using the recent {len(recent)}-month median."
    )
    return recent, EstimateTrendEvidence(direction, percent, len(recent), explanation)


def has_seasonality(
    monthly_by_month: dict[int, list[Money]],
    fraction: int,
    rules: HistoricalEstimateRules,
) -> tuple[bool, str]:
    """Detect a repeated month-of-year pattern without overfitting one year."""
    policy = rules.seasonality
    repeated = {
        month: values
        for month, values in monthly_by_month.items()
        if len(values) >= policy.minimum_samples_per_month and any(values)
    }
    medians = [typical_amount(values, fraction) for values in repeated.values()]
    if len(medians) < policy.minimum_repeated_months:
        return (
            False,
            f"Only {len(medians)} calendar month(s) repeat across years; at least "
            f"{policy.minimum_repeated_months} "
            "are required before applying a seasonal profile.",
        )
    magnitudes = [abs(value.to_decimal()) for value in medians if value]
    if len(magnitudes) < policy.minimum_repeated_months:
        return False, "Too few non-zero repeated calendar months support seasonality."
    middle = median(magnitudes)
    if not middle:
        return False, "Repeated calendar-month amounts have no non-zero seasonal baseline."
    stable_months = 0
    for values in repeated.values():
        center = abs(typical_amount(values, fraction).to_decimal())
        deviations = [abs(abs(value.to_decimal()) - center) for value in values]
        relative = median(deviations) / center if center else Decimal(0)
        stable_months += relative <= policy.maximum_within_month_variability
    if (
        stable_months * policy.stable_month_support_denominator
        < len(repeated) * policy.stable_month_support_numerator
    ):
        return (
            False,
            f"Only {stable_months}/{len(repeated)} repeated calendar months are stable; "
            "the apparent pattern is treated as noise.",
        )
    ratio = max(magnitudes) / middle
    if ratio < policy.minimum_peak_to_median_ratio:
        return (
            False,
            f"Repeated month medians vary by only {ratio:.2f}×; "
            f"{policy.minimum_peak_to_median_ratio}× is required.",
        )
    return (
        True,
        f"{len(repeated)} repeated calendar months are stable and peak at "
        f"{ratio:.2f}× the median month.",
    )


def seasonal_amounts(
    monthly_by_month: dict[int, list[Money]],
    fraction: int,
    rules: HistoricalEstimateRules,
) -> tuple[ScheduledMonthAmount, ...]:
    """Return repeated month-of-year magnitudes supported by at least two years."""
    result: list[ScheduledMonthAmount] = []
    for month in range(1, 13):
        values = monthly_by_month.get(month, [])
        if len(values) < rules.seasonality.minimum_samples_per_month:
            continue
        typical = typical_amount(values, fraction)
        if typical:
            result.append(ScheduledMonthAmount(month, abs(typical)))
    return tuple(result)
