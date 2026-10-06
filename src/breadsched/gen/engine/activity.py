"""Derived plan/actual reporting over dated planning events.

The planning engine is event driven.  This module deliberately introduces display
periods only after planned occurrences and actual ledger transactions already have
their real dates.  Changing a report from month to quarter or year therefore never
changes the underlying plan or projection.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
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
from . import fsa_flows, ledger
from .completeness import Completeness, Policy
from .conversion import (
    CurrencyEvidence,
    ReportingConverter,
    UnconvertedActivity,
    conversion_notes,
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
    "CategoryActivity",
    "CategoryReport",
    "CashBridgeActivity",
    "CashBridgeKind",
    "CashPosition",
    "CurrencyEvidence",
    "MortgagePaymentActivity",
    "PlanningFlowActivity",
    "PeriodActivity",
    "PlanMeasure",
    "PlanSettings",
    "UnconvertedActivity",
    "ReportingPeriod",
    "build_activity_report",
    "build_category_report",
    "currency_notes",
    "escrow_planning_flows",
    "inferred_planning_flow",
    "mortgage_payment",
    "planning_flow_decision",
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
    variance: list[Money | None]
    #: Per period, what reimbursements took or are expected to take off this expense
    #: category's gross cost (``actual`` is the net household cost), less write-offs.
    reimbursable: list[Money] = field(default_factory=list)
    #: ``reimbursable`` over the range for this account alone, without children.
    own_reimbursable: Money = field(default_factory=lambda: Money(0))

    @property
    def gross(self) -> list[Money]:
        """Per period, the cost before reimbursement: net actual plus reimbursable."""
        if not self.reimbursable:
            return list(self.actual)
        return [net + back for net, back in zip(self.actual, self.reimbursable, strict=True)]

    @property
    def reimbursable_total(self) -> Money:
        return _sum_money(self.reimbursable)

    def values(self, measure: PlanMeasure) -> Sequence[Money | None]:
        if measure is PlanMeasure.PLANNED:
            return self.planned
        if measure is PlanMeasure.ACTUAL:
            return self.actual
        return self.variance

    def total(self, measure: PlanMeasure) -> Money | None:
        return _sum_optional(self.values(measure))


@dataclass(slots=True)
class CashBridgeActivity:
    """One signed source or use of spendable cash across display periods."""

    kind: CashBridgeKind
    name: str
    planned: list[Money]
    actual: list[Money]
    variance: list[Money | None]

    def values(self, measure: PlanMeasure) -> Sequence[Money | None]:
        if measure is PlanMeasure.PLANNED:
            return self.planned
        if measure is PlanMeasure.ACTUAL:
            return self.actual
        return self.variance

    def total(self, measure: PlanMeasure) -> Money | None:
        return _sum_optional(self.values(measure))


@dataclass(frozen=True, slots=True)
class CashPosition:
    """Projected spendable-cash positions over the selected Plan horizon."""

    opening: Money
    closing: Money
    minimum: Money
    minimum_date: date
    # Foreign events between the as-of date and the horizon start that no quote
    # converts; they are left out of the opening position rather than mixed in.
    unconverted_before: tuple[UnconvertedActivity, ...] = ()


@dataclass(slots=True)
class PlanningFlowActivity:
    """One economically meaningful balance-sheet flow across display periods."""

    kind: PlanningFlowKind
    account: str
    account_name: str
    full_name: str
    name: str
    planned: list[Money]
    actual: list[Money]
    variance: list[Money | None]

    def values(self, measure: PlanMeasure) -> Sequence[Money | None]:
        if measure is PlanMeasure.PLANNED:
            return self.planned
        if measure is PlanMeasure.ACTUAL:
            return self.actual
        return self.variance

    def total(self, measure: PlanMeasure) -> Money | None:
        return _sum_optional(self.values(measure))


@dataclass(slots=True)
class MortgagePaymentActivity:
    """A whole mortgage cash requirement, separate from its classified components."""

    account: str
    account_name: str
    full_name: str
    name: str
    planned: list[Money]
    actual: list[Money]
    variance: list[Money | None]

    def values(self, measure: PlanMeasure) -> Sequence[Money | None]:
        if measure is PlanMeasure.PLANNED:
            return self.planned
        if measure is PlanMeasure.ACTUAL:
            return self.actual
        return self.variance

    def total(self, measure: PlanMeasure) -> Money | None:
        return _sum_optional(self.values(measure))


@dataclass(slots=True)
class CategoryReport:
    """Income/expense hierarchy plus classified balance-sheet planning flows."""

    activity: ActivityReport
    categories: list[CategoryActivity]
    cash_bridge: list[CashBridgeActivity]
    cash_position: CashPosition
    mortgage_payments: list[MortgagePaymentActivity]
    planning_flows: list[PlanningFlowActivity]
    as_of: date
    unconverted_accounts: list[frozenset[str]] = field(default_factory=list)
    """Per period, income/expense categories (with ancestors) missing foreign activity."""
    currency_notes: tuple[str, ...] = ()
    #: Horizon totals, including the opening cash position (#236).
    completeness: Completeness = field(default_factory=Completeness)

    @property
    def completeness_through_as_of(self) -> Completeness:
        """Coverage of the through-as-of summary figures."""
        return self.activity.completeness_through_as_of

    @property
    def period_completeness(self) -> tuple[Completeness, ...]:
        return tuple(period.completeness for period in self.activity.periods)

    def cell_complete(self, account: str, index: int) -> bool:
        """False when this category's value in period ``index`` omits foreign activity."""
        return not (
            index < len(self.unconverted_accounts) and account in self.unconverted_accounts[index]
        )

    @property
    def cash_variance(self) -> Money:
        """Period variance summed over periods that have started by ``as_of``.

        Each period compares everything posted in it, future-dated postings
        included, with its whole plan. For how the household is doing *so far*,
        use :attr:`cash_variance_through_as_of`.
        """
        return _sum_money(
            period.cash_variance for period in self.activity.periods if period.start <= self.as_of
        )

    @property
    def planned_cash_through_as_of(self) -> Money | None:
        """Planned cash of expectations dated through ``as_of``; future-only is N/A."""
        return self.activity.planned_cash_through_as_of

    @property
    def actual_cash_through_as_of(self) -> Money | None:
        """Actual cash inside the horizon through ``as_of``; future-only is N/A."""
        return self.activity.actual_cash_through_as_of

    @property
    def cash_variance_through_as_of(self) -> Money | None:
        """Actual through ``as_of`` less planned through ``as_of`` (#235).

        Both operands stop at the same date: postings dated later, even inside
        the current period, and expectations dated later are left out, and no
        planned amount is prorated.
        """
        return self.activity.cash_variance_through_as_of

    @property
    def income(self) -> tuple[CategoryActivity, ...]:
        return tuple(row for row in self.categories if row.account_class is AccountClass.INCOME)

    @property
    def expenses(self) -> tuple[CategoryActivity, ...]:
        return tuple(row for row in self.categories if row.account_class is AccountClass.EXPENSE)

    @property
    def reimbursable_categories(self) -> tuple[CategoryActivity, ...]:
        """Expense categories whose own cost was partly reimbursed or expected back."""
        return tuple(row for row in self.expenses if row.own_reimbursable != Money(0))

    def category_totals(
        self, account_class: AccountClass, measure: PlanMeasure
    ) -> list[Money | None]:
        """Column totals using only outermost rollups of one category class."""
        rows = [row for row in self.categories if row.account_class is account_class]
        roots = [
            row
            for row in rows
            if not any(
                row.full_name.startswith(f"{candidate.full_name}:")
                for candidate in rows
                if candidate is not row
            )
        ]
        return _sum_columns([row.values(measure) for row in roots], len(self.activity.periods))

    def category_grand_total(
        self, account_class: AccountClass, measure: PlanMeasure
    ) -> Money | None:
        return _sum_optional(self.category_totals(account_class, measure))

    def operating_net_totals(self, measure: PlanMeasure) -> list[Money | None]:
        """Signed income less expense, kept separate from balance-sheet movements."""
        result: list[Money | None] = []
        for income, expense in zip(
            self.category_totals(AccountClass.INCOME, measure),
            self.category_totals(AccountClass.EXPENSE, measure),
            strict=True,
        ):
            if income is None and expense is None:
                result.append(None)
            else:
                result.append((income or Money(0)) - (expense or Money(0)))
        return result

    def operating_net_grand_total(self, measure: PlanMeasure) -> Money | None:
        return _sum_optional(self.operating_net_totals(measure))

    def cash_bridge_totals(self, measure: PlanMeasure) -> list[Money | None]:
        if not self.cash_bridge:
            return [Money(0) for _ in self.activity.periods]
        return _sum_columns(
            [row.values(measure) for row in self.cash_bridge],
            len(self.activity.periods),
        )

    def cash_bridge_grand_total(self, measure: PlanMeasure) -> Money | None:
        return _sum_optional(self.cash_bridge_totals(measure))

    def planning_flow_totals(self, measure: PlanMeasure) -> list[Money | None]:
        return _sum_columns(
            [row.values(measure) for row in self.planning_flows],
            len(self.activity.periods),
        )

    def planning_flow_grand_total(self, measure: PlanMeasure) -> Money | None:
        return _sum_optional(self.planning_flow_totals(measure))

    def mortgage_payment_totals(self, measure: PlanMeasure) -> list[Money | None]:
        """Informational whole-payment totals; never add these to component rows."""
        return _sum_columns(
            [row.values(measure) for row in self.mortgage_payments],
            len(self.activity.periods),
        )

    def mortgage_payment_grand_total(self, measure: PlanMeasure) -> Money | None:
        return _sum_optional(self.mortgage_payment_totals(measure))

    def cash_totals(self, measure: PlanMeasure) -> list[Money | None]:
        if measure is PlanMeasure.PLANNED:
            return [period.planned_cash_change for period in self.activity.periods]
        if measure is PlanMeasure.ACTUAL:
            return [period.actual_cash_change for period in self.activity.periods]
        return [
            period.cash_variance if period.start <= self.as_of else None
            for period in self.activity.periods
        ]

    def grand_total(self, measure: PlanMeasure) -> Money | None:
        return _sum_optional(self.cash_totals(measure))


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


def _sum_columns(rows: Iterable[Sequence[Money | None]], width: int) -> list[Money | None]:
    materialized = list(rows)
    return [_sum_optional(row[index] for row in materialized) for index in range(width)]


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
        planned_amount=(
            None if transaction.planned_amount is None else transaction.planned_amount * factor
        ),
        planning_resolution=transaction.planning_resolution,
        splits=splits,
        converted_from=converted_from,
        reimbursable_splits=reimbursable,
    )


def _economic_planning_flow_amounts(
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
        if _redundant_cash_flow_split(split, splits, accounts, kind):
            continue
        amount = kind.plan_amount(split.amount)
        totals[kind] = totals.get(kind, Money(0)) + amount
    return totals


def _redundant_cash_flow_split(
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


def _cash_bridge_contributions(
    splits: tuple[PlannedSplit, ...],
    accounts: dict[str, Account],
    *,
    funded_from_cash: bool = False,
) -> dict[CashBridgeKind, Money]:
    """Allocate every spendable-cash dollar once to an explainable signed row."""
    cash, income, expense = _split_totals(
        splits,
        accounts,
        funded_from_cash=funded_from_cash,
    )
    escrow = _sum_money(escrow_planning_flows(splits, accounts).values())
    flows = _economic_planning_flow_amounts(splits, accounts)
    contributions = {
        CashBridgeKind.INCOME: income,
        CashBridgeKind.EXPENSE: -(expense - escrow),
        CashBridgeKind.RETIREMENT_DISTRIBUTION: flows.get(
            PlanningFlowKind.RETIREMENT_INCOME, Money(0)
        ),
        CashBridgeKind.RETIREMENT_SAVING: -flows.get(PlanningFlowKind.RETIREMENT_SAVING, Money(0)),
        CashBridgeKind.BENEFIT_FUNDING: -flows.get(PlanningFlowKind.BENEFIT_FUNDING, Money(0)),
        CashBridgeKind.DEBT_PRINCIPAL: -flows.get(PlanningFlowKind.DEBT_PRINCIPAL, Money(0)),
        CashBridgeKind.ESCROW_FUNDING: -escrow,
    }
    classified = _sum_money(contributions.values())
    contributions[CashBridgeKind.OTHER] = cash - classified
    return contributions


def _spendable_cash_balance(db: DbSQLite, accounts: dict[str, Account], when: date) -> Money:
    return _sum_money(
        ledger.balance(db, account, as_of=when)
        for account in accounts.values()
        if account.is_spendable_cash and not account.placeholder
    )


def _planned_cash_position(
    db: DbSQLite,
    start: date,
    end: date,
    accounts: dict[str, Account],
    scenario: Scenario | None,
    as_of: date,
) -> CashPosition:
    """Project exact-event spendable cash from the latest known ledger position.

    Event values use the same as-of currency conversion as the activity report;
    an event without an applicable quote does not move the projected position.
    """
    converter = ReportingConverter(db, as_of)
    skipped: list[UnconvertedActivity] = []
    if start > as_of:
        opening = _spendable_cash_balance(db, accounts, as_of)
        if as_of < start - timedelta(days=1):
            prior_events = (
                scenario_events(db, scenario, as_of + timedelta(days=1), start - timedelta(days=1))
                if scenario is not None
                else scheduled_events(
                    db,
                    as_of + timedelta(days=1),
                    start - timedelta(days=1),
                    include_actualized=True,
                )
            )
            for original in prior_events:
                event = converter.event(original)
                if event is None:
                    skipped.append(
                        UnconvertedActivity(
                            "planned",
                            original.planned_date,
                            original.description,
                            original.expected_amount,
                            original.expected_currency or converter.target,
                        )
                    )
                    continue
                cash, _income, _expense = _split_totals(
                    event.expected_splits,
                    accounts,
                    funded_from_cash=event.funded_from_cash,
                )
                opening = opening + cash
    else:
        opening = _spendable_cash_balance(db, accounts, start - timedelta(days=1))

    current = opening
    minimum = opening
    minimum_date = start
    events = (
        scenario_events(db, scenario, start, end)
        if scenario is not None
        else scheduled_events(db, start, end, include_actualized=True)
    )
    for original in sorted(events, key=lambda item: (item.planned_date, item.key)):
        event = converter.event(original)
        if event is None:
            continue  # listed with the horizon's unconverted activity
        cash, _income, _expense = _split_totals(
            event.expected_splits,
            accounts,
            funded_from_cash=event.funded_from_cash,
        )
        current = current + cash
        if current < minimum:
            minimum = current
            minimum_date = event.planned_date
    return CashPosition(opening, current, minimum, minimum_date, tuple(skipped))


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
        cash, income, expense = _split_totals(
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


def currency_notes(
    db: DbSQLite,
    report: ActivityReport,
    *,
    before: Sequence[UnconvertedActivity] = (),
) -> tuple[str, ...]:
    """Disclose the report's quotes and exclusions (see :func:`conversion_notes`)."""
    return conversion_notes(db, report.conversions, (*before, *report.unconverted), report.as_of)


def _period_variances(
    planned_values: Sequence[Money],
    actual_values: Sequence[Money],
    periods: Sequence[PeriodActivity],
    as_of: date,
) -> list[Money | None]:
    return [
        actual - planned if bucket.start <= as_of else None
        for planned, actual, bucket in zip(planned_values, actual_values, periods, strict=True)
    ]


def _category_rows(
    db: DbSQLite,
    accounts: dict[str, Account],
    periods: Sequence[PeriodActivity],
    direct_planned: dict[str, list[Money]],
    direct_actual: dict[str, list[Money]],
    as_of: date,
    direct_reimbursable: dict[str, list[Money]] | None = None,
) -> list[CategoryActivity]:
    direct_reimbursable = direct_reimbursable or {}
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
        planned_values = rolled(handle, direct_planned)
        actual_values = rolled(handle, direct_actual)
        reimbursable_values = rolled(handle, direct_reimbursable)
        rows.append(
            CategoryActivity(
                account=handle,
                name=account.name,
                full_name=full_name,
                account_class=account.account_class,
                # Hide a conventional top-level Income/Expenses root from indentation.
                depth=max(0, full_name.count(":")),
                planned=planned_values,
                actual=actual_values,
                variance=_period_variances(planned_values, actual_values, periods, as_of),
                reimbursable=reimbursable_values,
                own_reimbursable=_sum_money(direct_reimbursable.get(handle, ())),
            )
        )
    rows.sort(key=lambda row: (row.account_class.value, row.full_name.casefold()))
    return rows


def _cash_bridge_rows(
    periods: Sequence[PeriodActivity],
    planned: dict[CashBridgeKind, list[Money]],
    actual: dict[CashBridgeKind, list[Money]],
    as_of: date,
) -> list[CashBridgeActivity]:
    rows: list[CashBridgeActivity] = []
    for kind in CashBridgeKind:
        planned_values = planned.get(kind, [Money(0) for _ in periods])
        actual_values = actual.get(kind, [Money(0) for _ in periods])
        if not any(planned_values) and not any(actual_values):
            continue
        rows.append(
            CashBridgeActivity(
                kind=kind,
                name=kind.label,
                planned=list(planned_values),
                actual=list(actual_values),
                variance=_period_variances(planned_values, actual_values, periods, as_of),
            )
        )
    return rows


def _planning_flow_rows(
    db: DbSQLite,
    accounts: dict[str, Account],
    periods: Sequence[PeriodActivity],
    planned: dict[tuple[PlanningFlowKind, str], list[Money]],
    actual: dict[tuple[PlanningFlowKind, str], list[Money]],
    as_of: date,
) -> list[PlanningFlowActivity]:
    rows: list[PlanningFlowActivity] = []
    for flow_kind, handle in set(planned) | set(actual):
        account = accounts.get(handle)
        if account is None:
            continue
        planned_values = planned.get((flow_kind, handle), [Money(0) for _ in periods])
        actual_values = actual.get((flow_kind, handle), [Money(0) for _ in periods])
        full_name = db.full_name(account)
        rows.append(
            PlanningFlowActivity(
                kind=flow_kind,
                account=handle,
                account_name=account.name,
                full_name=full_name,
                name=f"{flow_kind.label} — {full_name}",
                planned=list(planned_values),
                actual=list(actual_values),
                variance=_period_variances(planned_values, actual_values, periods, as_of),
            )
        )
    rows.sort(key=lambda row: (row.kind.value, row.full_name.casefold()))
    return rows


def _mortgage_rows(
    db: DbSQLite,
    accounts: dict[str, Account],
    periods: Sequence[PeriodActivity],
    planned: dict[str, list[Money]],
    actual: dict[str, list[Money]],
    as_of: date,
) -> list[MortgagePaymentActivity]:
    rows: list[MortgagePaymentActivity] = []
    for handle in set(planned) | set(actual):
        account = accounts.get(handle)
        if account is None:
            continue
        planned_values = planned.get(handle, [Money(0) for _ in periods])
        actual_values = actual.get(handle, [Money(0) for _ in periods])
        full_name = db.full_name(account)
        rows.append(
            MortgagePaymentActivity(
                account=handle,
                account_name=account.name,
                full_name=full_name,
                name=f"Mortgage payment — {full_name}",
                planned=list(planned_values),
                actual=list(actual_values),
                variance=_period_variances(planned_values, actual_values, periods, as_of),
            )
        )
    rows.sort(key=lambda row: row.full_name.casefold())
    return rows


def build_category_report(
    db: DbSQLite,
    start: date,
    end: date,
    *,
    period: ReportingPeriod | str = ReportingPeriod.MONTH,
    scenario: Scenario | None = None,
    as_of: date | None = None,
) -> CategoryReport:
    """Derive category-period values from planned occurrences and actual splits.

    Income and expense accounts are the reporting dimension. Asset/liability
    transfers therefore affect projection state but never become planning expense.
    Parent category rows are roll-ups of their descendants.
    """
    effective_as_of = as_of or date.today()
    activity = build_activity_report(
        db, start, end, period=period, scenario=scenario, as_of=effective_as_of
    )
    accounts = {account.handle: account for account in db.iter_accounts()}
    periods = activity.periods
    direct_planned: dict[str, list[Money]] = {}
    direct_actual: dict[str, list[Money]] = {}
    direct_reimbursable: dict[str, list[Money]] = {}
    bridge_planned: dict[CashBridgeKind, list[Money]] = {}
    bridge_actual: dict[CashBridgeKind, list[Money]] = {}
    flow_planned: dict[tuple[PlanningFlowKind, str], list[Money]] = {}
    flow_actual: dict[tuple[PlanningFlowKind, str], list[Money]] = {}
    mortgage_planned: dict[str, list[Money]] = {}
    mortgage_actual: dict[str, list[Money]] = {}

    def amounts(store: dict[str, list[Money]], handle: str) -> list[Money]:
        return store.setdefault(handle, [Money(0) for _ in periods])

    def flow_amounts(
        store: dict[tuple[PlanningFlowKind, str], list[Money]],
        kind: PlanningFlowKind,
        account: str,
    ) -> list[Money]:
        return store.setdefault((kind, account), [Money(0) for _ in periods])

    def bridge_amounts(
        store: dict[CashBridgeKind, list[Money]], kind: CashBridgeKind
    ) -> list[Money]:
        return store.setdefault(kind, [Money(0) for _ in periods])

    unconverted_accounts: list[frozenset[str]] = []
    for period_index, bucket in enumerate(periods):
        missing: set[str] = set()
        for item in bucket.unconverted:
            for handle in item.accounts:
                account = accounts.get(handle)
                if account is None or account.account_class not in (
                    AccountClass.INCOME,
                    AccountClass.EXPENSE,
                ):
                    continue
                # Keep the category visible so its incomplete value can be flagged.
                amounts(direct_planned if item.kind == "planned" else direct_actual, handle)
                while account is not None and account.handle not in missing:
                    missing.add(account.handle)
                    parent = accounts.get(account.parent) if account.parent else None
                    if parent is None or parent.account_class is not account.account_class:
                        break
                    account = parent
        unconverted_accounts.append(frozenset(missing))
        for event in bucket.planned_events:
            for kind, value in _cash_bridge_contributions(
                event.expected_splits,
                accounts,
                funded_from_cash=event.funded_from_cash,
            ).items():
                values = bridge_amounts(bridge_planned, kind)
                values[period_index] = values[period_index] + value
            mortgage = mortgage_payment(event.expected_splits, accounts)
            if mortgage is not None:
                handle, cash_required = mortgage
                values = amounts(mortgage_planned, handle)
                values[period_index] = values[period_index] + cash_required
            escrow = escrow_recognition(
                ((split.account, split.amount) for split in event.expected_splits), accounts
            )
            for handle, value in escrow_planning_flows(event.expected_splits, accounts).items():
                values = flow_amounts(flow_planned, PlanningFlowKind.ESCROW_FUNDING, handle)
                values[period_index] = values[period_index] + value
            for planned_split in event.expected_splits:
                flow_kind = _inferred_planning_flow(planned_split, event.expected_splits, accounts)
                if flow_kind is PlanningFlowKind.ESCROW_FUNDING:
                    continue
                if flow_kind is not None:
                    if _redundant_cash_flow_split(
                        planned_split,
                        event.expected_splits,
                        accounts,
                        flow_kind,
                    ):
                        continue
                    values = flow_amounts(flow_planned, flow_kind, planned_split.account)
                    values[period_index] = values[period_index] + flow_kind.plan_amount(
                        planned_split.amount
                    )
                account = accounts.get(planned_split.account)
                if account is None:
                    continue
                values = amounts(direct_planned, account.handle)
                if account.account_class is AccountClass.INCOME:
                    values[period_index] = values[period_index] - planned_split.amount
                elif account.account_class is AccountClass.EXPENSE:
                    values[period_index] = values[period_index] + planned_split.amount
            for handle, amount in escrow.covered_expenses.items():
                values = amounts(direct_planned, handle)
                values[period_index] = values[period_index] - amount
            for handle, amount in escrow.restored_expenses.items():
                values = amounts(direct_planned, handle)
                values[period_index] = values[period_index] + amount
        for actual in bucket.actual_transactions:
            # Reporting-currency split values, converted once by the activity report.
            actual_planned_splits = actual.splits
            for kind, value in _cash_bridge_contributions(
                actual_planned_splits,
                accounts,
            ).items():
                values = bridge_amounts(bridge_actual, kind)
                values[period_index] = values[period_index] + value
            mortgage = mortgage_payment(actual_planned_splits, accounts)
            if mortgage is not None:
                handle, cash_required = mortgage
                values = amounts(mortgage_actual, handle)
                values[period_index] = values[period_index] + cash_required
            escrow = escrow_recognition(
                ((split.account, split.amount) for split in actual_planned_splits), accounts
            )
            for handle, value in escrow_planning_flows(actual_planned_splits, accounts).items():
                values = flow_amounts(flow_actual, PlanningFlowKind.ESCROW_FUNDING, handle)
                values[period_index] = values[period_index] + value
            for actual_split in actual_planned_splits:
                flow_kind = _inferred_planning_flow(actual_split, actual_planned_splits, accounts)
                if flow_kind is PlanningFlowKind.ESCROW_FUNDING:
                    continue
                if flow_kind is not None:
                    if _redundant_cash_flow_split(
                        actual_split,
                        actual_planned_splits,
                        accounts,
                        flow_kind,
                    ):
                        continue
                    values = flow_amounts(flow_actual, flow_kind, actual_split.account)
                    values[period_index] = values[period_index] + flow_kind.plan_amount(
                        actual_split.amount
                    )
                account = accounts.get(actual_split.account)
                if account is None:
                    continue
                values = amounts(direct_actual, account.handle)
                if account.account_class is AccountClass.INCOME:
                    values[period_index] = values[period_index] - actual_split.amount
                elif account.account_class is AccountClass.EXPENSE:
                    values[period_index] = values[period_index] + actual_split.amount
            for handle, amount in escrow.covered_expenses.items():
                values = amounts(direct_actual, handle)
                values[period_index] = values[period_index] - amount
            for handle, amount in escrow.restored_expenses.items():
                values = amounts(direct_actual, handle)
                values[period_index] = values[period_index] + amount
            for adjustment in actual.reimbursable_splits:
                account = accounts.get(adjustment.account)
                if account is not None and account.account_class is AccountClass.EXPENSE:
                    values = amounts(direct_reimbursable, account.handle)
                    values[period_index] = values[period_index] - adjustment.amount

    rows = _category_rows(
        db,
        accounts,
        periods,
        direct_planned,
        direct_actual,
        effective_as_of,
        direct_reimbursable,
    )
    position = _planned_cash_position(db, start, end, accounts, scenario, effective_as_of)
    bridge_rows = _cash_bridge_rows(periods, bridge_planned, bridge_actual, effective_as_of)
    flow_rows = _planning_flow_rows(
        db,
        accounts,
        periods,
        flow_planned,
        flow_actual,
        effective_as_of,
    )
    mortgage_rows = _mortgage_rows(
        db,
        accounts,
        periods,
        mortgage_planned,
        mortgage_actual,
        effective_as_of,
    )
    report = CategoryReport(
        activity=activity,
        categories=rows,
        cash_bridge=bridge_rows,
        cash_position=position,
        mortgage_payments=mortgage_rows,
        planning_flows=flow_rows,
        as_of=effective_as_of,
        unconverted_accounts=unconverted_accounts,
        currency_notes=currency_notes(db, activity, before=position.unconverted_before),
        completeness=Completeness.of(
            excluded_activity(db, (*position.unconverted_before, *activity.unconverted)),
            policy=Policy.SUBTOTAL,
            as_of=effective_as_of,
        ),
    )
    for measure in (PlanMeasure.PLANNED, PlanMeasure.ACTUAL):
        if report.cash_bridge_totals(measure) != report.cash_totals(measure):
            raise AssertionError(f"cash bridge does not reconcile for {measure.value}")
    return report
