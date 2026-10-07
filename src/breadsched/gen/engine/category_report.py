"""The Plan's category report: income and expense rows, cash bridge, and flows.

``activity.build_activity_report`` dates planned occurrences and actual ledger
activity into display periods; ``build_category_report`` turns that into the rows
the Plan shows (per income or expense account, the cash bridge, planning flows,
and mortgage payments), the projected spendable-cash position, and its currency
notes and completeness. It reads ``activity``; ``activity`` never imports it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass
from ..lib.money import Money
from ..lib.scenario import Scenario
from ..lib.transaction import PlanningFlowKind
from . import ledger
from .activity import (
    ActivityReport,
    CashBridgeKind,
    PeriodActivity,
    PlanMeasure,
    ReportingPeriod,
    build_activity_report,
    economic_planning_flow_amounts,
    escrow_planning_flows,
    inferred_planning_flow,
    mortgage_payment,
    redundant_cash_flow_split,
    split_totals,
)
from .completeness import Completeness, Policy
from .conversion import (
    ReportingConverter,
    UnconvertedActivity,
    conversion_notes,
    excluded_activity,
)
from .escrow import recognition as escrow_recognition
from .planning import (
    PlannedSplit,
    scenario_events,
    scheduled_events,
)

__all__ = [
    "CashBridgeActivity",
    "CashPosition",
    "CategoryActivity",
    "CategoryReport",
    "MortgagePaymentActivity",
    "PlanningFlowActivity",
    "build_category_report",
    "currency_notes",
]


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


def _cash_bridge_contributions(
    splits: tuple[PlannedSplit, ...],
    accounts: dict[str, Account],
    *,
    funded_from_cash: bool = False,
) -> dict[CashBridgeKind, Money]:
    """Allocate every spendable-cash dollar once to an explainable signed row."""
    cash, income, expense = split_totals(
        splits,
        accounts,
        funded_from_cash=funded_from_cash,
    )
    escrow = _sum_money(escrow_planning_flows(splits, accounts).values())
    flows = economic_planning_flow_amounts(splits, accounts)
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
                cash, _income, _expense = split_totals(
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
        cash, _income, _expense = split_totals(
            event.expected_splits,
            accounts,
            funded_from_cash=event.funded_from_cash,
        )
        current = current + cash
        if current < minimum:
            minimum = current
            minimum_date = event.planned_date
    return CashPosition(opening, current, minimum, minimum_date, tuple(skipped))


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
                flow_kind = inferred_planning_flow(planned_split, event.expected_splits, accounts)
                if flow_kind is PlanningFlowKind.ESCROW_FUNDING:
                    continue
                if flow_kind is not None:
                    if redundant_cash_flow_split(
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
                flow_kind = inferred_planning_flow(actual_split, actual_planned_splits, accounts)
                if flow_kind is PlanningFlowKind.ESCROW_FUNDING:
                    continue
                if flow_kind is not None:
                    if redundant_cash_flow_split(
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
