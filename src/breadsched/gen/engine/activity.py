"""Derived plan/actual reporting over dated planning events.

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
from fractions import Fraction

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass, AccountType
from ..lib.money import Money
from ..lib.recurrence import add_months
from ..lib.scenario import Scenario
from ..lib.scheduled import ScheduledTransaction
from ..lib.transaction import PlanningFlowKind, PlanningResolution, Transaction
from . import fsa_flows
from .completeness import Completeness, Policy
from .conversion import (
    CurrencyEvidence,
    ReportingConverter,
    UnconvertedActivity,
    excluded_activity,
)
from .escrow import recognition as escrow_recognition
from .fsa_flows import FsaFlowKind
from .planning import (
    EventStatus,
    PlannedEvent,
    PlannedSplit,
    scenario_events,
    scheduled_events,
)
from .receivables import plan_adjustments

__all__ = [
    "ActualActivity",
    "ActivityReport",
    "CashBridgeKind",
    "CurrencyEvidence",
    "PeriodActivity",
    "PlanMeasure",
    "PlanSettings",
    "UnconvertedActivity",
    "ReportingPeriod",
    "build_activity_report",
    "economic_planning_flow_amounts",
    "escrow_planning_flows",
    "inferred_planning_flow",
    "mortgage_payment",
    "planning_flow_decision",
    "redundant_cash_flow_split",
    "split_totals",
]


class ReportingPeriod(str, Enum):
    """Calendar bucket used only to display event-driven activity."""

    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class PlanMeasure(str, Enum):
    """Which derived amount a Plan table displays."""

    PLANNED = "planned"
    ACTUAL = "actual"
    VARIANCE = "variance"


class CashBridgeKind(str, Enum):
    """One signed, non-overlapping contribution to spendable-cash movement."""

    INCOME = "income"
    EXPENSE = "expense"
    RETIREMENT_DISTRIBUTION = "retirement_distribution"
    RETIREMENT_SAVING = "retirement_saving"
    BENEFIT_FUNDING = "benefit_funding"
    DEBT_PRINCIPAL = "debt_principal"
    ESCROW_FUNDING = "escrow_funding"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            CashBridgeKind.INCOME: "Income received",
            CashBridgeKind.EXPENSE: "Ordinary expenses paid",
            CashBridgeKind.RETIREMENT_DISTRIBUTION: "Retirement distributions",
            CashBridgeKind.RETIREMENT_SAVING: "Retirement saving",
            CashBridgeKind.BENEFIT_FUNDING: "Benefit / FSA funding",
            CashBridgeKind.DEBT_PRINCIPAL: "Debt principal",
            CashBridgeKind.ESCROW_FUNDING: "Escrow funding",
            CashBridgeKind.OTHER: "Other cash timing / financing",
        }[self]


@dataclass(frozen=True, slots=True)
class PlanSettings:
    """Per-book Plan presentation state shared by GTK and web."""

    start: date
    end: date
    period: ReportingPeriod = ReportingPeriod.MONTH
    measure: PlanMeasure = PlanMeasure.PLANNED
    scenario: str | None = None
    compare: str | None = None

    KEY = "plan.view"

    def serialize(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "period": self.period.value,
            "measure": self.measure.value,
            "scenario": self.scenario,
            "compare": self.compare,
        }

    @classmethod
    def load(cls, db: DbSQLite, default_start: date, default_end: date) -> PlanSettings:
        stored = db.get_metadata(cls.KEY, None)
        if not isinstance(stored, dict):
            return cls(default_start, default_end)
        try:
            return cls(
                start=date.fromisoformat(str(stored["start"])),
                end=date.fromisoformat(str(stored["end"])),
                period=ReportingPeriod(str(stored.get("period", "month"))),
                measure=PlanMeasure(str(stored.get("measure", "planned"))),
                scenario=(str(stored["scenario"]) if stored.get("scenario") else None),
                compare=(str(stored["compare"]) if stored.get("compare") else None),
            )
        except (KeyError, TypeError, ValueError):
            return cls(default_start, default_end)

    def save(self, db: DbSQLite) -> None:
        db.set_metadata(self.KEY, self.serialize())


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
    splits: tuple[PlannedSplit, ...] = ()
    converted_from: str | None = None
    #: Splits that separate a reimbursable expense's gross cost from its net cost:
    #: every split of a receivable's reclassification transaction and each linked
    #: reimbursement split, converted like ``splits``.
    reimbursable_splits: tuple[PlannedSplit, ...] = ()

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
            "converted_from": self.converted_from,
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
    unconverted: list[UnconvertedActivity] = field(default_factory=list)
    #: Planned cash change of the expectations dated on or before the report's
    #: as-of date (whole dated events; nothing is prorated).
    planned_cash_through_as_of: Money = field(default_factory=lambda: Money(0))
    #: Whether this period's figures include every foreign amount (#236).
    completeness: Completeness = field(default_factory=Completeness)

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
        return tuple(event for event in self.planned_events if event.status is EventStatus.EXPECTED)

    @property
    def resolved(self) -> tuple[PlannedEvent, ...]:
        return tuple(
            event for event in self.planned_events if event.status is EventStatus.ACTUALIZED
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
            "unconverted": [item.as_dict() for item in self.unconverted],
            "completeness": self.completeness.as_dict(),
        }


@dataclass(slots=True)
class ActivityReport:
    """A chronological set of display buckets over one event/actual horizon."""

    start: date
    end: date
    period: ReportingPeriod
    periods: list[PeriodActivity]
    conversions: tuple[CurrencyEvidence, ...] = ()
    as_of: date | None = None
    #: Plan and actual totals over the horizon are subtotals of what converted.
    completeness: Completeness = field(default_factory=Completeness)
    #: The same for the through-as-of figures: only exclusions dated by ``as_of``.
    completeness_through_as_of: Completeness = field(default_factory=Completeness)

    @property
    def unconverted(self) -> tuple[UnconvertedActivity, ...]:
        """Foreign activity omitted from every total because no quote applies."""
        return tuple(item for period in self.periods for item in period.unconverted)

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
    def through_as_of_applies(self) -> bool:
        """Through-as-of figures exist once the horizon has started by ``as_of``."""
        return self.as_of is not None and self.start <= self.as_of

    @property
    def planned_cash_through_as_of(self) -> Money | None:
        """Planned cash of expectations dated through ``as_of`` (no proration)."""
        if not self.through_as_of_applies:
            return None
        return _sum_money(period.planned_cash_through_as_of for period in self.periods)

    @property
    def actual_cash_through_as_of(self) -> Money | None:
        """Actual cash posted through ``as_of``; later postings are left out."""
        if not self.through_as_of_applies:
            return None
        assert self.as_of is not None
        return _sum_money(
            item.cash_change
            for period in self.periods
            for item in period.actual_transactions
            if item.post_date <= self.as_of
        )

    @property
    def cash_variance_through_as_of(self) -> Money | None:
        """Actual through ``as_of`` less planned through ``as_of`` (#235)."""
        actual = self.actual_cash_through_as_of
        planned = self.planned_cash_through_as_of
        if actual is None or planned is None:
            return None
        return actual - planned

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
            "planned_cash_through_as_of": self.planned_cash_through_as_of,
            "actual_cash_through_as_of": self.actual_cash_through_as_of,
            "cash_variance_through_as_of": self.cash_variance_through_as_of,
            "completeness": self.completeness.as_dict(),
            "completeness_through_as_of": self.completeness_through_as_of.as_dict(),
            "unresolved_count": self.unresolved_count,
            "unresolved_actual_count": self.unresolved_actual_count,
            "unexpected_count": self.unexpected_count,
            "currency_as_of": self.as_of,
            "conversions": [item.as_dict() for item in self.conversions],
            "unconverted": [item.as_dict() for item in self.unconverted],
            "periods": [period.as_dict() for period in self.periods],
        }


def _inferred_planning_flow(
    split: PlannedSplit,
    splits: Iterable[PlannedSplit],
    accounts: dict[str, Account],
) -> PlanningFlowKind | None:
    """Return explicit split purpose or infer one from account type and context."""
    return planning_flow_decision(split, splits, accounts)[0]


def inferred_planning_flow(
    split: PlannedSplit,
    splits: Iterable[PlannedSplit],
    accounts: dict[str, Account],
) -> PlanningFlowKind | None:
    """Return the shared explicit-or-inferred purpose for an expected split."""
    return _inferred_planning_flow(split, splits, accounts)


def planning_flow_decision(
    split: PlannedSplit,
    splits: Iterable[PlannedSplit],
    accounts: dict[str, Account],
) -> tuple[PlanningFlowKind | None, str]:
    """Return a planning-flow kind and a user-facing reason for the decision."""
    if split.planning_flow is not None:
        return (
            split.planning_flow,
            f"Explicit split planning purpose: {split.planning_flow.label}.",
        )
    account = accounts.get(split.account)
    if account is None:
        return None, "No planning flow was inferred because the split account is unavailable."
    peers = [accounts.get(item.account) for item in splits if item.account != split.account]
    if account.atype is AccountType.RETIREMENT:
        if any(peer and peer.atype is AccountType.RETIREMENT for peer in peers):
            return (
                None,
                "Retirement-to-retirement movement is an ordinary transfer unless a split "
                "purpose explicitly classifies it.",
            )
        if split.amount > 0:
            return (
                PlanningFlowKind.RETIREMENT_SAVING,
                f"Inferred Retirement saving from a positive split to Retirement account "
                f"{account.name}.",
            )
        if split.amount < 0:
            return (
                PlanningFlowKind.RETIREMENT_INCOME,
                f"Inferred Retirement distributions from a negative split from Retirement "
                f"account {account.name}.",
            )
    if account.atype is AccountType.FSA:
        flow = fsa_flows.classify_parts(split.amount, peers)
        if flow is FsaFlowKind.TRANSFER:
            return (
                None,
                "Money moved between FSA accounts is an ordinary transfer unless a split "
                "purpose explicitly classifies it.",
            )
        # Every other FSA movement is part of the household's net benefit
        # funding: payroll puts money in, and direct payments and reimbursements
        # take it out to cover medical costs that are counted once, as expense,
        # where they were incurred.
        return (
            PlanningFlowKind.BENEFIT_FUNDING,
            f"Inferred Benefit / FSA funding from an FSA {flow.label.lower()} "
            f"{'into' if split.amount > 0 else 'out of'} FSA account {account.name}.",
        )
    if account.atype is AccountType.LOAN and split.amount > 0:
        if any(peer and peer.atype is AccountType.LOAN for peer in peers):
            return (
                None,
                "Loan-to-loan movement is an ordinary transfer unless a split purpose "
                "explicitly classifies it.",
            )
        return (
            PlanningFlowKind.DEBT_PRINCIPAL,
            f"Inferred Debt principal from a positive split reducing Loan account {account.name}.",
        )
    return (
        None,
        f"No planning flow is inferred for a {account.atype.value} account with this "
        "split direction; it remains ordinary ledger activity.",
    )


def escrow_planning_flows(
    splits: tuple[PlannedSplit, ...], accounts: dict[str, Account]
) -> dict[str, Money]:
    """Return exact signed escrow Plan rows, honoring explicit split overrides."""
    result = escrow_recognition(
        ((split.account, split.amount) for split in splits), accounts
    ).planning_flows
    explicit: dict[str, Money] = {}
    for split in splits:
        if split.planning_flow is PlanningFlowKind.ESCROW_FUNDING:
            explicit[split.account] = explicit.get(split.account, Money(0)) + split.amount
    for handle, amount in explicit.items():
        result[handle] = amount
    return result


def mortgage_payment(
    splits: tuple[PlannedSplit, ...], accounts: dict[str, Account]
) -> tuple[str, Money] | None:
    """Return the one Loan account and whole spendable-cash outflow for a payment.

    Origination and refinancing do not qualify: a payment must reduce exactly one
    Loan account and must have an actual outflow from spendable cash.
    """
    loans = {
        split.account
        for split in splits
        if split.amount > 0
        and (account := accounts.get(split.account)) is not None
        and account.atype is AccountType.LOAN
    }
    if len(loans) != 1:
        return None
    if any(
        split.amount < 0
        and (account := accounts.get(split.account)) is not None
        and account.atype is AccountType.LOAN
        for split in splits
    ):
        # A new or increased loan identifies origination/refinancing rather than
        # ordinary servicing, even when cash also pays closing costs.
        return None
    cash_required = _sum_money(
        -split.amount
        for split in splits
        if split.amount < 0
        and (account := accounts.get(split.account)) is not None
        and account.is_spendable_cash
    )
    if cash_required <= 0:
        return None
    return next(iter(loans)), cash_required


def _sum_money(values: Iterable[Money]) -> Money:
    total = Money(0)
    for value in values:
        total = total + value
    return total


def _sum_optional(values: Iterable[Money | None]) -> Money | None:
    present = [value for value in values if value is not None]
    return _sum_money(present) if present else None


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


def reporting_periods(
    start: date, end: date, period: ReportingPeriod
) -> list[tuple[date, date, str]]:
    """The ``(start, end, label)`` display buckets Plan uses for this range."""
    return [(item.start, item.end, item.label) for item in _make_periods(start, end, period)]


def _index_for(periods: list[PeriodActivity], when: date) -> PeriodActivity | None:
    for period in periods:
        if period.start <= when <= period.end:
            return period
    return None


def _gross(splits: tuple[PlannedSplit, ...]) -> Money:
    return _sum_money(split.amount for split in splits if split.amount > 0)


def split_totals(
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
        if account.is_spendable_cash:
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
    escrow = escrow_recognition(((split.account, split.amount) for split in splits), accounts)
    return cash, income, expense + escrow.planning_expense_adjustment


def _actual_activity(
    transaction: Transaction,
    accounts: dict[str, Account],
    factor: Fraction = Fraction(1),
    converted_from: str | None = None,
    adjustments: tuple[frozenset[str], frozenset[str]] = (frozenset(), frozenset()),
) -> ActualActivity:
    splits = ReportingConverter.splits(
        tuple(
            PlannedSplit(
                split.account,
                split.value,
                split.planning_flow,
                split.investment_activity,
            )
            for split in transaction.splits
        ),
        factor,
    )
    postings, reimbursements = adjustments
    owned = transaction.handle in postings
    reimbursable = ReportingConverter.splits(
        tuple(
            PlannedSplit(split.account, split.value)
            for split in transaction.splits
            if owned or split.handle in reimbursements
        ),
        factor,
    )
    cash, income, expense = split_totals(splits, accounts)
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
        planned_amount=(
            None if transaction.planned_amount is None else transaction.planned_amount * factor
        ),
        planning_resolution=transaction.planning_resolution,
        splits=splits,
        converted_from=converted_from,
        reimbursable_splits=reimbursable,
    )


def economic_planning_flow_amounts(
    splits: tuple[PlannedSplit, ...], accounts: dict[str, Account]
) -> dict[PlanningFlowKind, Money]:
    """Return each logical planning purpose once, excluding its cash counterpart."""
    totals: dict[PlanningFlowKind, Money] = {}
    for split in splits:
        account = accounts.get(split.account)
        if account is None:
            continue
        kind = _inferred_planning_flow(split, splits, accounts)
        if kind is None or kind is PlanningFlowKind.ESCROW_FUNDING:
            continue
        if redundant_cash_flow_split(split, splits, accounts, kind):
            continue
        amount = kind.plan_amount(split.amount)
        totals[kind] = totals.get(kind, Money(0)) + amount
    return totals


def redundant_cash_flow_split(
    split: PlannedSplit,
    splits: tuple[PlannedSplit, ...],
    accounts: dict[str, Account],
    kind: PlanningFlowKind,
) -> bool:
    """Whether a cash leg merely repeats a retirement distribution's source leg."""
    account = accounts.get(split.account)
    if (
        account is None
        or not account.is_spendable_cash
        or kind is not PlanningFlowKind.RETIREMENT_INCOME
    ):
        return False
    return any(
        peer is not split
        and (peer_account := accounts.get(peer.account)) is not None
        and not peer_account.is_spendable_cash
        and _inferred_planning_flow(peer, splits, accounts) is kind
        for peer in splits
    )


def build_activity_report(
    db: DbSQLite,
    start: date,
    end: date,
    *,
    period: ReportingPeriod | str = ReportingPeriod.MONTH,
    scenario: Scenario | None = None,
    as_of: date | None = None,
) -> ActivityReport:
    """Aggregate exact-dated planned and actual activity for display.

    Expectations are placed in the period containing their planned date.  Actual
    ledger transactions are placed in the period containing their posting date.
    Consequently an event planned for 31 January but posted on 1 February produces
    a January expectation and a February actual, faithfully exposing cash timing.

    Foreign-currency values are converted to the reporting currency with the one
    quote applicable on ``as_of`` (default today) for each currency, recorded in
    ``conversions``. Values with no applicable quote are excluded from every total
    and listed in their period's ``unconverted``; they are never added as
    reporting-currency units.
    """
    if end < start:
        raise ValueError("activity report end date precedes start date")
    grouping = period if isinstance(period, ReportingPeriod) else ReportingPeriod(period)
    periods = _make_periods(start, end, grouping)
    accounts = {account.handle: account for account in db.iter_accounts()}
    effective_as_of = as_of or date.today()
    converter = ReportingConverter(db, effective_as_of)

    planned = (
        scenario_events(db, scenario, start, end)
        if scenario is not None
        else scheduled_events(db, start, end, include_actualized=True)
    )
    for event in planned:
        bucket = _index_for(periods, event.planned_date)
        if bucket is None:
            continue
        converted = converter.event(event)
        if converted is None:
            bucket.unconverted.append(
                UnconvertedActivity(
                    "planned",
                    event.planned_date,
                    event.description,
                    event.expected_amount,
                    event.expected_currency or converter.target,
                    tuple(split.account for split in event.expected_splits),
                )
            )
            continue
        event = converted
        cash, income, expense = split_totals(
            event.expected_splits,
            accounts,
            funded_from_cash=event.funded_from_cash,
        )
        bucket.planned_events.append(event)
        bucket.planned_amount = bucket.planned_amount + event.expected_amount
        bucket.planned_cash_change = bucket.planned_cash_change + cash
        if event.planned_date <= effective_as_of:
            bucket.planned_cash_through_as_of = bucket.planned_cash_through_as_of + cash
        bucket.planned_income = bucket.planned_income + income
        bucket.planned_expense = bucket.planned_expense + expense

    adjustments = plan_adjustments(db)
    for transaction in db.iter_transactions(start=start, end=end):
        bucket = _index_for(periods, transaction.post_date)
        if bucket is None:
            continue
        currency = transaction.currency
        factor = converter.factor(currency)
        if factor is None:
            assert currency is not None
            bucket.unconverted.append(
                UnconvertedActivity(
                    "actual",
                    transaction.post_date,
                    transaction.description,
                    _gross(
                        tuple(PlannedSplit(item.account, item.value) for item in transaction.splits)
                    ),
                    currency,
                    tuple(item.account for item in transaction.splits),
                )
            )
            continue
        actual = _actual_activity(
            transaction,
            accounts,
            factor,
            currency if factor != 1 else None,
            adjustments,
        )
        bucket.actual_transactions.append(actual)
        bucket.actual_amount = bucket.actual_amount + actual.amount
        bucket.actual_cash_change = bucket.actual_cash_change + actual.cash_change
        bucket.actual_income = bucket.actual_income + actual.income
        bucket.actual_expense = bucket.actual_expense + actual.expense

    for bucket in periods:
        bucket.completeness = Completeness.of(
            excluded_activity(db, bucket.unconverted), policy=Policy.SUBTOTAL, as_of=effective_as_of
        )
    unconverted = [item for bucket in periods for item in bucket.unconverted]
    return ActivityReport(
        start=start,
        end=end,
        period=grouping,
        periods=periods,
        conversions=tuple(converter.used[key] for key in sorted(converter.used)),
        as_of=effective_as_of,
        completeness=Completeness.of(
            excluded_activity(db, unconverted), policy=Policy.SUBTOTAL, as_of=effective_as_of
        ),
        completeness_through_as_of=Completeness.of(
            excluded_activity(db, (item for item in unconverted if item.when <= effective_as_of)),
            policy=Policy.SUBTOTAL,
            as_of=effective_as_of,
        ),
    )
