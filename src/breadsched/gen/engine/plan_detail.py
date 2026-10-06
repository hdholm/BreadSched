"""Explain one Plan cell from the same exact-dated activity stream.

The Plan grid (`activity.build_category_report`) sums dated planned occurrences and
actual ledger transactions into display periods. A drill-down asks why one cell has
its value: these functions rebuild that period's activity for one category, planning
flow, or mortgage payment and return each contributing planned occurrence and actual
transaction with the reasons it counted, so the explanation can never disagree with
the grid.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass, AccountType
from ..lib.money import Money
from ..lib.scenario import Scenario
from ..lib.transaction import PlanningFlowKind, PlanningResolution
from .activity import (
    ActualActivity,
    ReportingPeriod,
    build_activity_report,
    escrow_planning_flows,
    inferred_planning_flow,
    mortgage_payment,
    planning_flow_decision,
)
from .conversion import (
    ReportingConverter,
)
from .escrow import recognition as escrow_recognition
from .planning import (
    EventStatus,
    PlannedEvent,
    PlannedSplit,
    event_by_key,
)

__all__ = [
    "CategoryActualDetail",
    "CategoryPeriodDetail",
    "CategoryPlannedDetail",
    "MortgagePaymentPeriodDetail",
    "PlanningFlowPeriodDetail",
    "explain_category_period",
    "explain_mortgage_payment_period",
    "explain_planning_flow_period",
]


def _sum_money(values: Iterable[Money]) -> Money:
    total = Money(0)
    for value in values:
        total = total + value
    return total


@dataclass(frozen=True, slots=True)
class CategoryPlannedDetail:
    """One planned occurrence contributing to a category/period cell."""

    occurrence: str
    planned_date: date
    description: str
    source: str
    status: str
    expected: Money
    actual: Money | None
    variance: Money | None
    actual_transaction: str | None
    actual_date: date | None
    explanation: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CategoryActualDetail:
    """One actual transaction contributing to a category/period cell."""

    transaction: str
    post_date: date
    description: str
    amount: Money
    resolution: PlanningResolution
    planned_occurrence: str | None
    planned_for: date | None
    expected: Money | None
    variance: Money | None
    date_variance_days: int | None
    explanation: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CategoryPeriodDetail:
    """Explanation of one derived Plan category/period value."""

    account: str
    name: str
    full_name: str
    account_class: AccountClass
    start: date
    end: date
    planned: Money
    actual: Money
    planned_events: tuple[CategoryPlannedDetail, ...]
    actual_transactions: tuple[CategoryActualDetail, ...]
    as_of: date
    #: What reimbursements took or are expected to take off this expense
    #: category's cost in the period, less write-offs; ``actual`` is the net
    #: household cost.
    reimbursable: Money = field(default_factory=lambda: Money(0))

    @property
    def variance(self) -> Money | None:
        if self.start > self.as_of:
            return None
        return self.actual - self.planned

    @property
    def gross(self) -> Money:
        """The cost before reimbursement: the net actual plus ``reimbursable``."""
        return self.actual + self.reimbursable


@dataclass(frozen=True, slots=True)
class PlanningFlowPeriodDetail:
    """Explanation of one classified planning-flow value for one period."""

    kind: PlanningFlowKind
    account: str
    name: str
    full_name: str
    start: date
    end: date
    planned: Money
    actual: Money
    planned_events: tuple[CategoryPlannedDetail, ...]
    actual_transactions: tuple[CategoryActualDetail, ...]
    as_of: date

    @property
    def variance(self) -> Money | None:
        if self.start > self.as_of:
            return None
        return self.actual - self.planned


@dataclass(frozen=True, slots=True)
class MortgagePaymentPeriodDetail:
    """Explanation of one non-additive whole-payment Plan cell."""

    account: str
    name: str
    full_name: str
    start: date
    end: date
    planned: Money
    actual: Money
    planned_events: tuple[CategoryPlannedDetail, ...]
    actual_transactions: tuple[CategoryActualDetail, ...]
    as_of: date

    @property
    def variance(self) -> Money | None:
        if self.start > self.as_of:
            return None
        return self.actual - self.planned


def explain_category_period(
    db: DbSQLite,
    account_handle: str,
    start: date,
    end: date,
    *,
    scenario: Scenario | None = None,
    as_of: date | None = None,
) -> CategoryPeriodDetail:
    """Explain one category-period value from the same exact-dated activity stream."""
    if end < start:
        raise ValueError("Plan detail end date precedes its start date.")
    account = db.get_account(account_handle)
    if account is None:
        raise KeyError(account_handle)
    if account.account_class not in (AccountClass.INCOME, AccountClass.EXPENSE):
        raise ValueError("Plan detail requires an income or expense account.")

    accounts = {item.handle: item for item in db.iter_accounts()}
    children: dict[str, list[str]] = {}
    for item in accounts.values():
        if item.parent is not None:
            children.setdefault(item.parent, []).append(item.handle)
    included: set[str] = set()

    def include(handle: str) -> None:
        item = accounts.get(handle)
        if item is None or item.account_class is not account.account_class:
            return
        included.add(handle)
        for child in children.get(handle, []):
            include(child)

    include(account.handle)

    def category_amount(splits: Iterable[PlannedSplit]) -> Money:
        splits = tuple(splits)
        total = Money(0)
        for split in splits:
            if split.account not in included:
                continue
            if account.account_class is AccountClass.INCOME:
                total = total - split.amount
            else:
                total = total + split.amount
        if account.account_class is AccountClass.EXPENSE:
            escrow = escrow_recognition(
                ((split.account, split.amount) for split in splits), accounts
            )
            total = total - _sum_money(
                amount for handle, amount in escrow.covered_expenses.items() if handle in included
            )
            total = total + _sum_money(
                amount for handle, amount in escrow.restored_expenses.items() if handle in included
            )
        return total

    report = build_activity_report(
        db, start, end, period=ReportingPeriod.MONTH, scenario=scenario, as_of=as_of
    )
    converter = ReportingConverter(db, as_of or date.today())
    planned_rows: list[CategoryPlannedDetail] = []
    actual_rows: list[CategoryActualDetail] = []
    planned_total = Money(0)
    actual_total = Money(0)
    reimbursable_total = Money(0)

    for bucket in report.periods:
        for event in bucket.planned_events:
            expected = category_amount(event.expected_splits)
            if expected == Money(0):
                continue
            actual_value = (
                category_amount(event.actual_splits) if converter.reporting_actual(event) else None
            )
            planned_total = planned_total + expected
            planned_rows.append(
                CategoryPlannedDetail(
                    occurrence=event.key,
                    planned_date=event.planned_date,
                    description=event.description,
                    source=event.source.value,
                    status=event.status.value,
                    expected=expected,
                    actual=actual_value,
                    variance=actual_value - expected if actual_value is not None else None,
                    actual_transaction=event.actual_transaction,
                    actual_date=event.actual_date,
                    explanation=_unique_explanations(
                        _planned_resolution_explanation(event),
                        _category_explanations(
                            event.expected_splits,
                            included,
                            account.account_class,
                            accounts,
                        ),
                        _amount_source_explanations(
                            event.expected_splits, accounts, included=included
                        ),
                        escrow_recognition(
                            ((split.account, split.amount) for split in event.expected_splits),
                            accounts,
                        ).explanations(accounts),
                    ),
                )
            )

        for actual in bucket.actual_transactions:
            if account.account_class is AccountClass.EXPENSE:
                reimbursable_total = reimbursable_total - _sum_money(
                    split.amount
                    for split in actual.reimbursable_splits
                    if split.account in included
                )
            splits = actual.splits
            value = category_amount(splits)
            if value == Money(0):
                continue
            actual_total = actual_total + value
            matched_expected: Money | None = None
            if actual.planned_occurrence:
                matched_event = converter.event(event_by_key(db, actual.planned_occurrence))
                if matched_event is not None:
                    matched_expected = category_amount(matched_event.expected_splits)
            actual_rows.append(
                CategoryActualDetail(
                    transaction=actual.transaction,
                    post_date=actual.post_date,
                    description=actual.description,
                    amount=value,
                    resolution=actual.planning_resolution,
                    planned_occurrence=actual.planned_occurrence,
                    planned_for=actual.planned_for,
                    expected=matched_expected,
                    variance=value - matched_expected if matched_expected is not None else None,
                    date_variance_days=actual.date_variance_days,
                    explanation=_unique_explanations(
                        _actual_resolution_explanation(actual, as_of or date.today()),
                        _category_explanations(
                            splits,
                            included,
                            account.account_class,
                            accounts,
                        ),
                        escrow_recognition(
                            ((split.account, split.amount) for split in splits),
                            accounts,
                        ).explanations(accounts),
                    ),
                )
            )

    return CategoryPeriodDetail(
        account=account.handle,
        name=account.name,
        full_name=db.full_name(account),
        account_class=account.account_class,
        start=start,
        end=end,
        planned=planned_total,
        actual=actual_total,
        planned_events=tuple(planned_rows),
        actual_transactions=tuple(actual_rows),
        as_of=as_of or date.today(),
        reimbursable=reimbursable_total,
    )


def explain_planning_flow_period(
    db: DbSQLite,
    kind: PlanningFlowKind,
    account_handle: str,
    start: date,
    end: date,
    *,
    scenario: Scenario | None = None,
    as_of: date | None = None,
) -> PlanningFlowPeriodDetail:
    """Explain one classified balance-sheet Plan cell from exact-dated activity."""
    if end < start:
        raise ValueError("Plan detail end date precedes its start date.")
    account = db.get_account(account_handle)
    if account is None:
        raise KeyError(account_handle)
    accounts = {item.handle: item for item in db.iter_accounts()}

    def flow_amount(splits: Iterable[PlannedSplit]) -> Money:
        split_tuple = tuple(splits)
        if kind is PlanningFlowKind.ESCROW_FUNDING:
            return escrow_planning_flows(split_tuple, accounts).get(account_handle, Money(0))
        total = Money(0)
        for split in split_tuple:
            if split.account != account_handle:
                continue
            inferred = inferred_planning_flow(split, split_tuple, accounts)
            if inferred is kind:
                total = total + kind.plan_amount(split.amount)
        return total

    def flow_explanations(splits: Iterable[PlannedSplit]) -> tuple[str, ...]:
        split_tuple = tuple(splits)
        found: list[str] = []
        if kind is PlanningFlowKind.ESCROW_FUNDING:
            explicit = [
                split
                for split in split_tuple
                if split.account == account_handle
                and split.planning_flow is PlanningFlowKind.ESCROW_FUNDING
            ]
            if explicit:
                found.append("Explicit split planning purpose: Escrow funding.")
            elif escrow_planning_flows(split_tuple, accounts).get(account_handle, Money(0)):
                found.append(
                    f"Inferred Escrow funding from cash or income entering Escrow account "
                    f"{account.name}; the funding is recognized when reserved."
                )
            return tuple(found)
        for split in split_tuple:
            if split.account != account_handle:
                continue
            decision, explanation = planning_flow_decision(split, split_tuple, accounts)
            if decision is kind:
                found.append(explanation)
        return _unique_explanations(found)

    report = build_activity_report(
        db, start, end, period=ReportingPeriod.MONTH, scenario=scenario, as_of=as_of
    )
    converter = ReportingConverter(db, as_of or date.today())
    planned_rows: list[CategoryPlannedDetail] = []
    actual_rows: list[CategoryActualDetail] = []
    planned_total = Money(0)
    actual_total = Money(0)

    for bucket in report.periods:
        for event in bucket.planned_events:
            expected = flow_amount(event.expected_splits)
            if expected == Money(0):
                continue
            actual_value = (
                flow_amount(event.actual_splits) if converter.reporting_actual(event) else None
            )
            planned_total = planned_total + expected
            planned_rows.append(
                CategoryPlannedDetail(
                    occurrence=event.key,
                    planned_date=event.planned_date,
                    description=event.description,
                    source=event.source.value,
                    status=event.status.value,
                    expected=expected,
                    actual=actual_value,
                    variance=actual_value - expected if actual_value is not None else None,
                    actual_transaction=event.actual_transaction,
                    actual_date=event.actual_date,
                    explanation=_unique_explanations(
                        _planned_resolution_explanation(event),
                        flow_explanations(event.expected_splits),
                        _amount_source_explanations(
                            event.expected_splits,
                            accounts,
                            included={account_handle},
                        ),
                        escrow_recognition(
                            ((split.account, split.amount) for split in event.expected_splits),
                            accounts,
                        ).explanations(accounts),
                    ),
                )
            )

        for actual in bucket.actual_transactions:
            splits = actual.splits
            value = flow_amount(splits)
            if value == Money(0):
                continue
            actual_total = actual_total + value
            matched_expected: Money | None = None
            if actual.planned_occurrence:
                matched = converter.event(event_by_key(db, actual.planned_occurrence))
                if matched is not None:
                    matched_expected = flow_amount(matched.expected_splits)
            actual_rows.append(
                CategoryActualDetail(
                    transaction=actual.transaction,
                    post_date=actual.post_date,
                    description=actual.description,
                    amount=value,
                    resolution=actual.planning_resolution,
                    planned_occurrence=actual.planned_occurrence,
                    planned_for=actual.planned_for,
                    expected=matched_expected,
                    variance=value - matched_expected if matched_expected is not None else None,
                    date_variance_days=actual.date_variance_days,
                    explanation=_unique_explanations(
                        _actual_resolution_explanation(actual, as_of or date.today()),
                        flow_explanations(splits),
                        escrow_recognition(
                            ((split.account, split.amount) for split in splits),
                            accounts,
                        ).explanations(accounts),
                    ),
                )
            )

    return PlanningFlowPeriodDetail(
        kind=kind,
        account=account.handle,
        name=f"{kind.label} — {db.full_name(account)}",
        full_name=db.full_name(account),
        start=start,
        end=end,
        planned=planned_total,
        actual=actual_total,
        planned_events=tuple(planned_rows),
        actual_transactions=tuple(actual_rows),
        as_of=as_of or date.today(),
    )


def explain_mortgage_payment_period(
    db: DbSQLite,
    account_handle: str,
    start: date,
    end: date,
    *,
    scenario: Scenario | None = None,
    as_of: date | None = None,
) -> MortgagePaymentPeriodDetail:
    """Explain one whole-payment row without adding it to component totals."""
    if end < start:
        raise ValueError("Plan detail end date precedes its start date.")
    account = db.get_account(account_handle)
    if account is None:
        raise KeyError(account_handle)
    if account.atype is not AccountType.LOAN:
        raise ValueError("Mortgage payment detail requires a Loan account.")
    accounts = {item.handle: item for item in db.iter_accounts()}

    def payment_amount(splits: Iterable[PlannedSplit]) -> Money:
        payment = mortgage_payment(tuple(splits), accounts)
        if payment is None or payment[0] != account_handle:
            return Money(0)
        return payment[1]

    report = build_activity_report(
        db, start, end, period=ReportingPeriod.MONTH, scenario=scenario, as_of=as_of
    )
    converter = ReportingConverter(db, as_of or date.today())
    planned_rows: list[CategoryPlannedDetail] = []
    actual_rows: list[CategoryActualDetail] = []
    planned_total = Money(0)
    actual_total = Money(0)

    for bucket in report.periods:
        for event in bucket.planned_events:
            expected = payment_amount(event.expected_splits)
            if expected == Money(0):
                continue
            actual_value = (
                payment_amount(event.actual_splits) if converter.reporting_actual(event) else None
            )
            planned_total = planned_total + expected
            planned_rows.append(
                CategoryPlannedDetail(
                    occurrence=event.key,
                    planned_date=event.planned_date,
                    description=event.description,
                    source=event.source.value,
                    status=event.status.value,
                    expected=expected,
                    actual=actual_value,
                    variance=actual_value - expected if actual_value is not None else None,
                    actual_transaction=event.actual_transaction,
                    actual_date=event.actual_date,
                    explanation=_unique_explanations(
                        _planned_resolution_explanation(event),
                        _mortgage_payment_explanations(event.expected_splits, accounts),
                        _amount_source_explanations(event.expected_splits, accounts),
                    ),
                )
            )

        for actual in bucket.actual_transactions:
            splits = actual.splits
            value = payment_amount(splits)
            if value == Money(0):
                continue
            actual_total = actual_total + value
            matched_expected: Money | None = None
            if actual.planned_occurrence:
                matched = converter.event(event_by_key(db, actual.planned_occurrence))
                if matched is not None:
                    matched_expected = payment_amount(matched.expected_splits)
            actual_rows.append(
                CategoryActualDetail(
                    transaction=actual.transaction,
                    post_date=actual.post_date,
                    description=actual.description,
                    amount=value,
                    resolution=actual.planning_resolution,
                    planned_occurrence=actual.planned_occurrence,
                    planned_for=actual.planned_for,
                    expected=matched_expected,
                    variance=(value - matched_expected if matched_expected is not None else None),
                    date_variance_days=actual.date_variance_days,
                    explanation=_unique_explanations(
                        _actual_resolution_explanation(actual, as_of or date.today()),
                        _mortgage_payment_explanations(splits, accounts),
                    ),
                )
            )

    full_name = db.full_name(account)
    return MortgagePaymentPeriodDetail(
        account=account.handle,
        name=f"Mortgage payment — {full_name}",
        full_name=full_name,
        start=start,
        end=end,
        planned=planned_total,
        actual=actual_total,
        planned_events=tuple(planned_rows),
        actual_transactions=tuple(actual_rows),
        as_of=as_of or date.today(),
    )


def _unique_explanations(*groups: Iterable[str]) -> tuple[str, ...]:
    """Combine explanations in stable order without repeating identical decisions."""
    found: list[str] = []
    for group in groups:
        for explanation in group:
            if explanation and explanation not in found:
                found.append(explanation)
    return tuple(found)


def _amount_source_explanations(
    splits: Iterable[PlannedSplit],
    accounts: dict[str, Account],
    *,
    included: set[str] | None = None,
) -> tuple[str, ...]:
    """Explain non-default amount provenance for relevant planned legs."""
    found: list[str] = []
    for split in splits:
        if included is not None and split.account not in included:
            continue
        if not split.amount_source or split.amount_source == "fixed template amount":
            continue
        account = accounts.get(split.account)
        name = account.name if account is not None else split.account
        found.append(f"Amount for {name}: {split.amount_source}.")
    return _unique_explanations(found)


def _category_explanations(
    splits: Iterable[PlannedSplit],
    included: set[str],
    account_class: AccountClass,
    accounts: dict[str, Account],
) -> tuple[str, ...]:
    """Explain the account-type decisions contributing direct category activity."""
    found: list[str] = []
    for split in splits:
        account = accounts.get(split.account)
        if account is None or split.account not in included:
            continue
        amount = -split.amount if account_class is AccountClass.INCOME else split.amount
        found.append(
            f"{account.name} is type {account.atype.value}, which is an "
            f"{account_class.value} account; this split contributes "
            f"{amount.format(parens_negative=True)}."
        )
    return _unique_explanations(found)


def _planned_resolution_explanation(event: PlannedEvent) -> tuple[str, ...]:
    if event.status is EventStatus.EXPECTED:
        return (
            "Resolution: no actual transaction is linked, so this expected occurrence "
            "remains pending.",
        )
    posted = event.actual_date.isoformat() if event.actual_date is not None else "an unknown date"
    return (f"Resolution: linked to an actual transaction posted {posted}.",)


def _actual_resolution_explanation(actual: ActualActivity, as_of: date) -> tuple[str, ...]:
    return _resolution_explanation(actual) + _future_dated_explanation(actual, as_of)


def _future_dated_explanation(actual: ActualActivity, as_of: date) -> tuple[str, ...]:
    """Say when a posting counts in the period actual but not through as-of (#235)."""
    if actual.post_date <= as_of:
        return ()
    return (
        f"Dated after the as-of date ({as_of.isoformat()}): counted in the period "
        "actual and period variance, but not in actual through as-of or Remaining.",
    )


def _resolution_explanation(actual: ActualActivity) -> tuple[str, ...]:
    if actual.planning_resolution is PlanningResolution.UNRESOLVED:
        return (
            "Resolution: this actual has not been matched to a planned occurrence. Use "
            "Resolve actuals to match it or mark it unexpected.",
        )
    if actual.planning_resolution is PlanningResolution.UNEXPECTED:
        return (
            "Resolution: explicitly marked unexpected. It remains actual activity but "
            "does not resolve a planned occurrence.",
        )
    if actual.planning_resolution is PlanningResolution.HISTORICAL:
        return (
            "Resolution: historical actual. It is reported as actual activity and does "
            "not enter the unresolved review queue.",
        )
    planned = actual.planned_for.isoformat() if actual.planned_for is not None else "unknown"
    return (f"Resolution: matched to the planned occurrence dated {planned}.",)


def _mortgage_payment_explanations(
    splits: tuple[PlannedSplit, ...], accounts: dict[str, Account]
) -> tuple[str, ...]:
    payment = mortgage_payment(splits, accounts)
    if payment is None:
        return ()
    loan_handle, cash_required = payment
    principal = _sum_money(
        split.amount for split in splits if split.account == loan_handle and split.amount > 0
    )
    escrow = _sum_money(
        amount for amount in escrow_planning_flows(splits, accounts).values() if amount > 0
    )
    recognition = escrow_recognition(((split.account, split.amount) for split in splits), accounts)
    expense = _sum_money(
        split.amount
        for split in splits
        if (account := accounts.get(split.account)) is not None
        and account.account_class is AccountClass.EXPENSE
    )
    expense = expense - _sum_money(recognition.covered_expenses.values())
    expense = expense + _sum_money(recognition.restored_expenses.values())
    components = [f"Debt principal {principal.format('$', parens_negative=True)}"]
    if expense:
        components.append(f"ordinary expense {expense.format('$', parens_negative=True)}")
    if escrow:
        components.append(f"Escrow funding {escrow.format('$', parens_negative=True)}")
    loan = accounts[loan_handle]
    return (
        f"Whole mortgage payment: {cash_required.format('$', parens_negative=True)} leaves "
        f"spendable cash while reducing Loan account {loan.name}.",
        "Classified components (non-additive): " + ", ".join(components) + ".",
        "The whole payment is a liquidity requirement; its components supply the "
        "expense and planning-flow classifications and are not added to it again.",
    )
