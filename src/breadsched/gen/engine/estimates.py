"""Historical category activity turned into reviewable planning estimates.

The analyzer deliberately produces suggestions rather than silently changing the
book. Saved suggestions become ordinary scheduled estimates, so Plan,
Projection, Review, and scenarios continue to consume one event model.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass
from ..lib.money import Money
from ..lib.recurrence import PeriodType, Recurrence
from ..lib.scenario import ScenarioSchedule
from ..lib.scheduled import ScheduledMonthAmount, ScheduledSplit, ScheduledTransaction
from ..lib.transaction import InvestmentActivityKind, PlanningFlowKind, Split
from . import activity, planning
from . import estimate_history as estimate_stats
from .currency import reporting_fraction
from .escrow import recognition as escrow_recognition
from .estimate_history import (
    EstimateCadenceEvidence,
    EstimateConfidenceEvidence,
    EstimateSeasonalityEvidence,
    EstimateTrendEvidence,
)
from .estimate_rules import DEFAULT_HISTORICAL_ESTIMATE_RULES, HistoricalEstimateRules

__all__ = [
    "EstimateEvidence",
    "EstimateHistoryEvidence",
    "HistoricalEstimateProposal",
    "HistoricalEstimateRules",
    "accept_historical_estimate",
    "draft_historical_estimate",
    "draft_scenario_estimate",
    "propose_historical_estimates",
    "validate_historical_estimate_adjustment",
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
            "gross": str(self.gross.to_decimal()),
            "planned": str(self.planned.to_decimal()),
            "residual": str(self.residual.to_decimal()),
            "selected": self.selected,
            "exclusion": self.exclusion,
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
    rules: HistoricalEstimateRules

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
            "rules": self.rules.serialize(),
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
            f"Rules: {self.rules.version}.",
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


def _funding_evidence(
    db: DbSQLite,
    category: str,
    start: date,
    end: date,
    rules: HistoricalEstimateRules,
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
    return _funding_evidence_from_counts(db, counts, rules)


def _funding_evidence_from_counts(
    db: DbSQLite,
    counts: Counter[str],
    rules: HistoricalEstimateRules,
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
    funding_keys = {
        "transaction_count": lambda item: item.transaction_count,
        "spendable_cash": lambda item: item.spendable_cash,
        "account_handle": lambda item: item.account,
    }
    for criterion in reversed(rules.funding.tie_break_order):
        try:
            key = funding_keys[criterion]
        except KeyError as exc:
            raise ValueError(
                f"unsupported historical-estimate funding tie-break {criterion!r}"
            ) from exc
        candidates.sort(key=key, reverse=True)
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
    end = estimate_stats.add_months(start, 12) - timedelta(days=1)
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
            key = (account.handle, estimate_stats.month_start(event.planned_date))
            amount = (
                split.amount * account.sign()
                - escrow.covered_expenses.get(account.handle, Money(0))
                + escrow.restored_expenses.get(account.handle, Money(0))
            )
            totals[key] = totals.get(key, Money(0)) + amount
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


def _bridge_end_before_continuing_coverage(
    account: str,
    future_start: date,
    planned: dict[tuple[str, date], Money],
    gross_by_month: dict[int, list[Money]],
    gross_default: Money,
    fraction: int,
) -> date | None:
    """End a monthly bridge before sustained full coverage begins.

    Two or more fully covered months through the end of the rolling future year
    are required. That distinguishes a continuing replacement from an isolated
    annual or one-time event, which must not truncate otherwise uncovered months.
    """
    covered: list[bool] = []
    for offset in range(12):
        month = estimate_stats.add_months(future_start, offset)
        samples = gross_by_month.get(month.month, [])
        need = estimate_stats.typical_amount(samples, fraction) if samples else gross_default
        residual, _applied = _residual_after_scheduled(
            need, planned.get((account, month), Money(0))
        )
        covered.append(not residual)
    for offset in range(1, 11):
        if not any(not value for value in covered[:offset]):
            continue
        if all(covered[offset:]):
            return estimate_stats.add_months(future_start, offset) - timedelta(days=1)
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


def _planned_flow_profiles(
    db: DbSQLite,
    start: date,
    scenario_handle: str | None,
    accounts: dict[str, Account],
) -> dict[tuple[_FlowSignature, date], Money]:
    """Return matching future coverage for each classified economic purpose."""
    end = estimate_stats.add_months(start, 12) - timedelta(days=1)
    totals: dict[tuple[_FlowSignature, date], Money] = {}
    for event in _target_events(db, start, end, scenario_handle):
        for split, flow, investment in _classified_flow_splits(event.expected_splits, accounts):
            amount = _economic_flow_amount(split, flow, investment)
            if amount <= 0:
                continue
            signature = (split.account, flow, investment)
            key = (signature, estimate_stats.month_start(event.planned_date))
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
    fraction: int,
    rules: HistoricalEstimateRules,
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
            key = (signature, estimate_stats.month_start(txn.post_date))
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
            historical_month = estimate_stats.add_months(history_start, offset)
            actual = monthly.get((signature, historical_month), Money(0))
            future_month = estimate_stats.corresponding_future_month(historical_month, future_start)
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
            db, counterpart_counts.get(signature, Counter()), rules
        )
        if funding_evidence is None:
            continue
        funding_handle = funding_evidence.selected
        observed_dates = sorted(dates.get(signature, []))
        recurrence, cadence, occurrences_per_month = estimate_stats.infer_recurrence(
            observed_dates, future_start, rules
        )
        if recurrence is None:
            continue
        robust, excluded_indexes, variability_value = estimate_stats.robust_sample(
            residual_monthly, rules
        )
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
        trend_sample, trend_evidence = estimate_stats.trend_summary(robust, fraction, rules)
        monthly_residual = estimate_stats.typical_amount(trend_sample, fraction)
        amount = (monthly_residual / occurrences_per_month).quantize(fraction)
        if amount <= 0:
            continue
        funding_account = accounts[funding_handle]
        ledger_amount = (
            flow.ledger_amount(amount)
            if flow is not None
            else amount * (investment.direction if investment is not None else 0)
        )
        variability = estimate_stats.variability_label(variability_value, rules)
        confidence_evidence = estimate_stats.confidence(
            sample_months=months,
            active_months=len(residual_monthly),
            retained_months=len(robust),
            variability=variability_value,
            rules=rules,
        )
        cadence_evidence = estimate_stats.cadence_evidence(
            observed_dates, cadence, occurrences_per_month, recurrence, rules
        )
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
            rules,
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
                    f"{estimate_stats.typical_amount(gross_values, fraction).format()}; "
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


@dataclass(frozen=True, slots=True)
class _CategoryHistory:
    residual_monthly: list[Money]
    residual_row_indexes: list[int]
    rows: list[tuple[date, Money, Money, Money]]
    residual_by_month: dict[int, list[Money]]
    gross_monthly: list[Money]
    gross_by_month: dict[int, list[Money]]
    transaction_count: int
    applied_scheduled_total: Money


def _category_history(
    db: DbSQLite,
    account: Account,
    accounts: dict[str, Account],
    history_start: date,
    future_start: date,
    months: int,
    planned_profiles: dict[tuple[str, date], Money],
) -> _CategoryHistory:
    residual_monthly: list[Money] = []
    residual_row_indexes: list[int] = []
    rows: list[tuple[date, Money, Money, Money]] = []
    residual_by_month: dict[int, list[Money]] = {}
    gross_monthly: list[Money] = []
    gross_by_month: dict[int, list[Money]] = {}
    transaction_count = 0
    applied_scheduled_total = Money(0)
    for offset in range(months):
        start = estimate_stats.add_months(history_start, offset)
        end = estimate_stats.add_months(start, 1) - timedelta(days=1)
        total = Money(0)
        for txn in db.iter_transactions(account=account.handle, start=start, end=end):
            if _is_investment_performance(txn.splits):
                continue
            value = txn.value_for(account.handle) * account.sign()
            escrow = escrow_recognition(
                ((split.account, split.value) for split in txn.splits),
                accounts,
            )
            value = (
                value
                - escrow.covered_expenses.get(account.handle, Money(0))
                + escrow.restored_expenses.get(account.handle, Money(0))
            )
            if value:
                total = total + value
                transaction_count += 1
        if total:
            gross_monthly.append(total)
            gross_by_month.setdefault(start.month, []).append(total)
        future_month = estimate_stats.corresponding_future_month(start, future_start)
        scheduled = planned_profiles.get((account.handle, future_month), Money(0))
        residual, applied_scheduled = _residual_after_scheduled(total, scheduled)
        applied_scheduled_total = applied_scheduled_total + applied_scheduled
        rows.append((start, total, applied_scheduled, residual))
        if residual:
            residual_row_indexes.append(len(rows) - 1)
            residual_monthly.append(residual)
            residual_by_month.setdefault(start.month, []).append(residual)
    return _CategoryHistory(
        residual_monthly,
        residual_row_indexes,
        rows,
        residual_by_month,
        gross_monthly,
        gross_by_month,
        transaction_count,
        applied_scheduled_total,
    )


def propose_historical_estimates(
    db: DbSQLite,
    *,
    as_of: date | None = None,
    months: int = 12,
    min_active_months: int = 3,
    scenario_handle: str | None = None,
    rules: HistoricalEstimateRules = DEFAULT_HISTORICAL_ESTIMATE_RULES,
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
    current_month = estimate_stats.month_start(today)
    history_start = estimate_stats.add_months(current_month, -months)
    history_end = current_month - timedelta(days=1)
    proposals: list[HistoricalEstimateProposal] = []
    planned_profiles = _planned_category_profiles(db, current_month, scenario_handle)
    accounts_by_handle = {account.handle: account for account in db.iter_accounts()}
    fraction = reporting_fraction(db)

    for account in db.iter_accounts():
        if account.is_root or account.placeholder:
            continue
        if account.account_class not in (AccountClass.INCOME, AccountClass.EXPENSE):
            continue

        category_history = _category_history(
            db,
            account,
            accounts_by_handle,
            history_start,
            current_month,
            months,
            planned_profiles,
        )
        monthly = category_history.residual_monthly
        monthly_by_month = category_history.residual_by_month
        gross_monthly = category_history.gross_monthly
        gross_by_month = category_history.gross_by_month

        if len(monthly) < min_active_months:
            continue
        funding_evidence = _funding_evidence(db, account.handle, history_start, history_end, rules)
        if funding_evidence is None:
            continue
        funding_account = db.get_account(funding_evidence.selected)
        if funding_account is None:
            continue

        seasonal, seasonality_explanation = estimate_stats.has_seasonality(
            monthly_by_month, fraction, rules
        )
        if seasonal:
            # Repeated winter/summer peaks are signal, not global outliers. The
            # month-specific profile below preserves them explicitly.
            robust_monthly = monthly
            excluded_indexes: tuple[int, ...] = ()
            relative_variability = Decimal(0)
        else:
            robust_monthly, excluded_indexes, relative_variability = estimate_stats.robust_sample(
                monthly, rules
            )
        excluded_rows = {category_history.residual_row_indexes[index] for index in excluded_indexes}
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
            for index, (month, gross, applied, residual) in enumerate(category_history.rows)
        )
        trend_sample, trend_evidence = estimate_stats.trend_summary(robust_monthly, fraction, rules)
        monthly_residual = estimate_stats.typical_amount(trend_sample, fraction)
        observed_dates = _unscheduled_dates(db, account.handle, history_start, history_end)
        recurrence, cadence, occurrences_per_month = estimate_stats.infer_recurrence(
            observed_dates,
            current_month,
            rules,
        )
        if recurrence is None:
            continue
        if recurrence.period is PeriodType.MONTH and recurrence.interval == 1:
            recurrence.end = _bridge_end_before_continuing_coverage(
                account.handle,
                current_month,
                planned_profiles,
                gross_by_month,
                estimate_stats.typical_amount(gross_monthly, fraction),
                fraction,
            )
        amount = (monthly_residual / occurrences_per_month).quantize(fraction)
        seasonal_amounts = (
            estimate_stats.seasonal_amounts(monthly_by_month, fraction, rules)
            if seasonal and recurrence.period is PeriodType.MONTH
            else ()
        )
        variability = (
            "seasonal by calendar month"
            if seasonal
            else estimate_stats.variability_label(relative_variability, rules)
        )
        confidence_evidence = estimate_stats.confidence(
            sample_months=months,
            active_months=len(monthly),
            retained_months=len(robust_monthly),
            variability=relative_variability,
            rules=rules,
        )
        scheduled_total = category_history.applied_scheduled_total
        gross_median = estimate_stats.typical_amount(gross_monthly, fraction)
        cadence_evidence = estimate_stats.cadence_evidence(
            observed_dates, cadence, occurrences_per_month, recurrence, rules
        )
        seasonality_evidence = EstimateSeasonalityEvidence(
            seasonal,
            variability,
            seasonal_amounts,
            (
                f"{seasonality_explanation} {len(seasonal_amounts)} "
                "month-specific amount(s) will be retained."
                if seasonal
                else f"{seasonality_explanation} Amounts are {variability}."
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
            rules,
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
                transaction_count=category_history.transaction_count,
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
            fraction=fraction,
            rules=rules,
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


def validate_historical_estimate_adjustment(
    db: DbSQLite, adjusted: ScheduledTransaction | ScenarioSchedule
) -> None:
    """Shared acceptance guard for GTK, web, and direct engine callers."""
    if adjusted.estimate_evidence is None:
        return
    if not adjusted.placeholder:
        raise ValueError("a historical estimate must remain a planning estimate")
    if not adjusted.name.strip():
        raise ValueError("a historical estimate needs a name")
    if len(adjusted.splits) < 2:
        raise ValueError("a historical estimate needs a category and funding split")
    missing = [split.account for split in adjusted.splits if db.get_account(split.account) is None]
    if missing:
        raise ValueError("a historical estimate references an account that no longer exists")
    when = adjusted.recurrence.start
    resolved = adjusted.resolved_splits(when=when)
    if sum((value for _account, value in resolved), Money(0)):
        raise ValueError("historical estimate adjustments must remain balanced")
    if not any(value > 0 for _account, value in resolved):
        raise ValueError("historical estimate amount must be greater than zero")
    if not any(
        (account := db.get_account(split.account)) is not None
        and (
            account.account_class in {AccountClass.INCOME, AccountClass.EXPENSE}
            or split.planning_flow is not None
            or split.investment_activity is not None
        )
        for split in adjusted.splits
    ):
        raise ValueError(
            "choose an income/expense category, planning purpose, or investment activity"
        )
    if len({item.month for item in adjusted.seasonal_amounts}) != len(adjusted.seasonal_amounts):
        raise ValueError("seasonal estimate months must be unique")


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
    draft.estimate_evidence = proposal.evidence.serialize()
    validate_historical_estimate_adjustment(db, draft)
    return draft


def draft_scenario_estimate(db: DbSQLite, proposal: HistoricalEstimateProposal) -> ScenarioSchedule:
    """Build an editable, unsaved scenario estimate from one analyzer proposal."""
    draft = ScenarioSchedule(
        name=proposal.estimate_name,
        recurrence=Recurrence.from_dict(proposal.recurrence.serialize()),
        splits=_proposal_splits(db, proposal),
        placeholder=True,
        seasonal_amounts=[
            ScheduledMonthAmount.from_dict(item.serialize()) for item in proposal.seasonal_amounts
        ],
        estimate_evidence=proposal.evidence.serialize(),
    )
    validate_historical_estimate_adjustment(db, draft)
    return draft


def accept_historical_estimate(
    db: DbSQLite,
    proposal: HistoricalEstimateProposal,
    *,
    scenario_handle: str | None = None,
) -> str:
    """Persist a proposal as an ordinary Base or scenario estimate."""

    if scenario_handle is None:
        baseline_schedule = draft_historical_estimate(db, proposal)
        validate_historical_estimate_adjustment(db, baseline_schedule)
        with db.transaction(f"Add historical estimate {proposal.category_name}") as txn:
            db.add_scheduled(baseline_schedule, txn)
        return baseline_schedule.handle

    scenario = db.get_scenario(scenario_handle)
    if scenario is None:
        raise ValueError("saved scenario no longer exists")
    scenario_schedule = draft_scenario_estimate(db, proposal)
    validate_historical_estimate_adjustment(db, scenario_schedule)
    scenario.schedule_overrides.append(scenario_schedule)
    with db.transaction(f"Add historical estimate to {scenario.name}") as txn:
        db.commit_scenario(scenario, txn)
    return scenario_schedule.handle
