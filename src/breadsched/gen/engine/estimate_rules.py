"""Versioned policy for historical-estimate inference.

The estimator records the complete applied rule set with every proposal.  Changing
one of these values is therefore an explicit, reviewable policy change rather than
an unlabelled numeric edit inside the inference algorithm.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

__all__ = [
    "DEFAULT_HISTORICAL_ESTIMATE_RULES",
    "HistoricalEstimateRules",
]


@dataclass(frozen=True, slots=True)
class AnomalyRules:
    """Conservative median-absolute-deviation filtering."""

    minimum_observations: int
    median_deviation_multiplier: Decimal
    zero_mad_relative_threshold: Decimal
    zero_mad_absolute_floor: Decimal
    minimum_retained_observations: int


@dataclass(frozen=True, slots=True)
class ConfidenceRules:
    """Named components of the non-probabilistic confidence score."""

    base_weight: Decimal
    coverage_weight: Decimal
    depth_weight: Decimal
    retained_weight: Decimal
    consistency_weight: Decimal
    full_depth_months: int
    maximum_score: Decimal


@dataclass(frozen=True, slots=True)
class CadenceRules:
    """Evidence thresholds for recurrence and monthly-anchor inference."""

    minimum_short_cadence_observations: int
    support_numerator: int
    support_denominator: int
    weekly_gap_min_days: int
    weekly_gap_max_days: int
    fortnightly_gap_min_days: int
    fortnightly_gap_max_days: int
    maximum_year_interval: int
    mean_year_days: Decimal
    annual_tolerance_days: int
    calendar_interval_minimum_observations: int
    calendar_interval_minimum_positive_gaps: int
    calendar_month_interval_min: int
    calendar_month_interval_max: int
    calendar_month_gap_tolerance: int
    monthly_anchor_minimum_months: int
    monthly_anchor_day_tolerance: int


@dataclass(frozen=True, slots=True)
class TrendRules:
    """Conservative recent-median selection after a material trend."""

    minimum_observations: int
    minimum_relative_change: Decimal
    recent_observations: int


@dataclass(frozen=True, slots=True)
class SeasonalityRules:
    """Repeated calendar-month evidence required for a seasonal profile."""

    minimum_samples_per_month: int
    minimum_repeated_months: int
    maximum_within_month_variability: Decimal
    stable_month_support_numerator: int
    stable_month_support_denominator: int
    minimum_peak_to_median_ratio: Decimal


@dataclass(frozen=True, slots=True)
class VariabilityRules:
    """Human-readable bands for residual amount variability."""

    stable_maximum: Decimal
    moderate_maximum: Decimal


@dataclass(frozen=True, slots=True)
class FundingRules:
    """Deterministic funding-candidate ordering, highest value first."""

    tie_break_order: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HistoricalEstimateRules:
    """One immutable, serializable historical-estimation policy."""

    version: str
    anomaly: AnomalyRules
    confidence: ConfidenceRules
    cadence: CadenceRules
    trend: TrendRules
    seasonality: SeasonalityRules
    variability: VariabilityRules
    funding: FundingRules

    def serialize(self) -> dict[str, object]:
        """Return JSON-compatible evidence for the exact applied policy."""

        def normalize(value: object) -> object:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, dict):
                return {str(key): normalize(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [normalize(item) for item in value]
            return value

        serialized = normalize(asdict(self))
        if not isinstance(serialized, dict):  # pragma: no cover - asdict is always a mapping
            raise TypeError("historical-estimate rules did not serialize as an object")
        return serialized


DEFAULT_HISTORICAL_ESTIMATE_RULES = HistoricalEstimateRules(
    version="historical-estimates-v1",
    anomaly=AnomalyRules(
        minimum_observations=6,
        median_deviation_multiplier=Decimal("3"),
        zero_mad_relative_threshold=Decimal("0.75"),
        zero_mad_absolute_floor=Decimal("1"),
        minimum_retained_observations=3,
    ),
    confidence=ConfidenceRules(
        base_weight=Decimal("0.20"),
        coverage_weight=Decimal("0.30"),
        depth_weight=Decimal("0.20"),
        retained_weight=Decimal("0.10"),
        consistency_weight=Decimal("0.20"),
        full_depth_months=12,
        maximum_score=Decimal("0.95"),
    ),
    cadence=CadenceRules(
        minimum_short_cadence_observations=4,
        support_numerator=3,
        support_denominator=4,
        weekly_gap_min_days=5,
        weekly_gap_max_days=9,
        fortnightly_gap_min_days=11,
        fortnightly_gap_max_days=17,
        maximum_year_interval=3,
        mean_year_days=Decimal("365.2425"),
        annual_tolerance_days=70,
        calendar_interval_minimum_observations=3,
        calendar_interval_minimum_positive_gaps=2,
        calendar_month_interval_min=2,
        calendar_month_interval_max=11,
        calendar_month_gap_tolerance=1,
        monthly_anchor_minimum_months=3,
        monthly_anchor_day_tolerance=3,
    ),
    trend=TrendRules(
        minimum_observations=6,
        minimum_relative_change=Decimal("0.10"),
        recent_observations=3,
    ),
    seasonality=SeasonalityRules(
        minimum_samples_per_month=2,
        minimum_repeated_months=6,
        maximum_within_month_variability=Decimal("0.25"),
        stable_month_support_numerator=3,
        stable_month_support_denominator=4,
        minimum_peak_to_median_ratio=Decimal("1.35"),
    ),
    variability=VariabilityRules(
        stable_maximum=Decimal("0.10"),
        moderate_maximum=Decimal("0.25"),
    ),
    funding=FundingRules(
        tie_break_order=("transaction_count", "spendable_cash", "account_handle"),
    ),
)
