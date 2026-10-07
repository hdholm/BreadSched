"""Multi-year cash-flow projection over a chronological planning-event stream.

Scheduled transactions and one-off plan items change state on their actual planned
dates; interest and investment assumptions accrue over the exact interval between
events. Months remain reporting buckets only.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass, AccountType
from ..lib.money import Money, Rate
from ..lib.recurrence import add_months
from ..lib.scenario import Assumptions, Drawdown, Scenario, ScenarioSchedule
from ..lib.scheduled import ScheduledTransaction, ScheduleGrowthPolicy
from ..lib.transaction import InvestmentActivityKind
from . import investment, planning, valuation
from .conversion import (
    ReportingConverter,
    UnconvertedActivity,
    conversion_notes,
    excluded_activity,
)
from .currency import reporting_fraction
from .escrow import recognition as escrow_recognition
from .goal_projection import project_goals
from .projection_result import (
    CashRunway,
    ComparisonRow,
    MonthLedger,
    MonthRow,
    Projection,
    ProjectionAccountDetail,
    ProjectionMonthDetail,
    ProjectionProgress,
    compare,
)
from .reimbursement_outlook import reimbursement_outlook

__all__ = [
    "CashRunway",
    "ComparisonRow",
    "MonthLedger",
    "MonthRow",
    "Projection",
    "ProjectionProgress",
    "ProjectionAccountDetail",
    "ProjectionMonthDetail",
    "compare",
    "explain_month",
    "project",
]

_ONE = Decimal(1)
# Projection growth rates originate as finite-precision Decimals. Converting them
# directly to exact Money rationals and compounding forever lets denominators grow
# without bound, eventually making bigint gcd/multiplication dominate runtime.
# Keep eight decimal places of a currency unit internally (one hundred-thousandth
# of a cent) after each accrual step. This is far below display precision while
# bounding rational sizes for long event streams.
_PROJECTION_MONEY_DENOMINATOR = 100_000_000


ProgressCallback = Callable[[ProjectionProgress], None]


@dataclass(slots=True)
class _AssumptionTimeline:
    """Projection-local cache for dated assumptions and annual escalation.

    Scenario.assumptions_for() intentionally favors a simple domain API, but a
    projection can ask for assumptions thousands of times.  Build the change
    points once, then use binary search for O(log changes) lookups and O(1) annual
    escalation factors.
    """

    scenario: Scenario
    start: date
    end: date
    _points: list[date] = field(init=False, default_factory=list)
    _values: list[Assumptions] = field(init=False, default_factory=list)
    _income_factors: list[Decimal] = field(init=False, default_factory=list)
    _expense_factors: list[Decimal] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        points = {self.start}
        for period in self.scenario.assumption_periods:
            if self.start < period.start <= self.end:
                points.add(period.start)
            if period.end is not None:
                after = period.end + timedelta(days=1)
                if self.start < after <= self.end:
                    points.add(after)
        self._points = sorted(points)
        # The expensive overlay/sort happens once per actual change point, not once
        # per event or balance-accrual interval.
        self._values = [self.scenario.assumptions_for(point) for point in self._points]

        months = (self.end.year - self.start.year) * 12 + self.end.month - self.start.month
        max_years = max(0, months // 12)
        self._income_factors = [_ONE]
        self._expense_factors = [_ONE]
        for year in range(1, max_years + 1):
            anniversary = add_months(self.start.replace(day=1), year * 12, day=1)
            assumptions = self.at(anniversary)
            self._income_factors.append(
                self._income_factors[-1] * (_ONE + assumptions.income_growth.decimal)
            )
            self._expense_factors.append(
                self._expense_factors[-1] * (_ONE + assumptions.expense_inflation.decimal)
            )

    def at(self, when: date) -> Assumptions:
        index = bisect_right(self._points, when) - 1
        return self._values[max(0, index)]

    def boundaries_between(self, start: date, end: date) -> list[date]:
        left = bisect_right(self._points, start)
        right = bisect_right(self._points, end - timedelta(days=1))
        return self._points[left:right]

    def escalation(
        self, field_name: Literal["income_growth", "expense_inflation"], completed_years: int
    ) -> Decimal:
        factors = self._income_factors if field_name == "income_growth" else self._expense_factors
        return factors[min(max(0, completed_years), len(factors) - 1)]


def _report_progress(
    callback: ProgressCallback | None, current: date, start: date, end: date, phase: str
) -> None:
    if callback is None:
        return
    span = max(1, (end - start).days)
    elapsed = min(span, max(0, (current - start).days))
    callback(ProjectionProgress(current=current, end=end, fraction=elapsed / span, phase=phase))


def explain_month(db: DbSQLite, result: Projection, index: int) -> ProjectionMonthDetail:
    """Explain the exact events, accruals, and account changes behind one month."""
    if index < 0 or index >= len(result.rows):
        raise IndexError(index)
    row = result.rows[index]
    ledger = row.ledger
    assumptions = result.scenario.assumptions_for(row.month)
    assumption_sources = result.scenario.assumption_sources(row.month)
    account_sources = result.scenario.account_assumption_sources(row.month)

    def rate_source(account: Account) -> str:
        if account.handle in assumptions.per_account:
            return account_sources.get(account.handle, result.scenario.name or "Scenario")
        if account.account_class is AccountClass.LIABILITY:
            return (
                "Account" if account.annual_interest else assumption_sources["liability_interest"]
            )
        if account.annual_return:
            return "Account"
        if account.atype.is_investment:
            return assumption_sources["investment_return"]
        return "Not applicable"

    def account_name(handle: str) -> str:
        account = db.get_account(handle)
        return db.full_name(account) if account is not None else handle

    holding_handles = sorted(
        set(ledger.opening_holdings)
        | set(ledger.holding_movements)
        | set(ledger.holding_contributions)
        | set(ledger.investment_growth)
        | set(ledger.closing_holdings),
        key=account_name,
    )
    holdings: list[ProjectionAccountDetail] = []
    for handle in holding_handles:
        account = db.get_account(handle)
        rate = _resolve_rate(assumptions, account).decimal if account is not None else Decimal(0)
        detail = ProjectionAccountDetail(
            handle=handle,
            name=account_name(handle),
            opening=ledger.opening_holdings.get(handle, Money(0)),
            movement=ledger.holding_movements.get(handle, Money(0)),
            accrual=ledger.investment_growth.get(handle, Money(0)),
            closing=ledger.closing_holdings.get(handle, Money(0)),
            annual_rate=rate,
            annual_rate_source=rate_source(account) if account is not None else "Unavailable",
            activities={
                "contributions": ledger.holding_contributions.get(handle, Money(0)),
                "withdrawals": ledger.holding_withdrawals.get(handle, Money(0)),
                "retirement_distributions": ledger.retirement_distributions.get(handle, Money(0)),
                "investment_income": ledger.investment_income.get(handle, Money(0)),
                "fees": ledger.investment_fees.get(handle, Money(0)),
                "rollovers": ledger.holding_rollovers.get(handle, Money(0)),
            },
        )
        if any((detail.opening, detail.movement, detail.accrual, detail.closing)):
            holdings.append(detail)

    liability_handles = sorted(
        set(ledger.opening_liabilities)
        | set(ledger.liability_movements)
        | set(ledger.liability_interest)
        | set(ledger.closing_liabilities),
        key=account_name,
    )
    liabilities: list[ProjectionAccountDetail] = []
    for handle in liability_handles:
        account = db.get_account(handle)
        rate = _resolve_rate(assumptions, account).decimal if account is not None else Decimal(0)
        movement = ledger.liability_movements.get(handle)
        if movement is None:
            movement = -ledger.debt_payments.get(handle, Money(0))
        detail = ProjectionAccountDetail(
            handle=handle,
            name=account_name(handle),
            opening=ledger.opening_liabilities.get(handle, Money(0)),
            movement=movement,
            accrual=ledger.liability_interest.get(handle, Money(0)),
            closing=ledger.closing_liabilities.get(handle, Money(0)),
            annual_rate=rate,
            annual_rate_source=rate_source(account) if account is not None else "Unavailable",
        )
        if any((detail.opening, detail.movement, detail.accrual, detail.closing)):
            liabilities.append(detail)

    return ProjectionMonthDetail(
        index=index,
        month=row.month,
        label=row.label,
        cash_open=ledger.opening_cash,
        cash_flow=ledger.cash_flow,
        cash_interest=ledger.cash_interest,
        cash_close=ledger.closing_cash,
        income=row.income,
        expense=row.expense,
        holdings_open=ledger.holdings_open,
        holding_movements=_sum(ledger.holding_movements.values()),
        holding_contributions=_sum(ledger.holding_contributions.values()),
        holding_withdrawals=_sum(ledger.holding_withdrawals.values()),
        retirement_distributions=_sum(ledger.retirement_distributions.values()),
        investment_income=_sum(ledger.investment_income.values()),
        investment_fees=_sum(ledger.investment_fees.values()),
        holding_rollovers=_sum(
            amount for amount in ledger.holding_rollovers.values() if amount > 0
        ),
        investment_growth=_sum(ledger.investment_growth.values()),
        holdings_close=ledger.holdings_close,
        liabilities_open=ledger.liabilities_open,
        liability_movements=_sum(item.movement for item in liabilities),
        liability_interest=_sum(ledger.liability_interest.values()),
        liabilities_close=ledger.liabilities_close,
        net_worth=row.net_worth,
        assumptions=assumptions,
        assumption_sources=assumption_sources,
        events=tuple(ledger.events),
        holdings=tuple(holdings),
        liabilities=tuple(liabilities),
        escrow_explanations=tuple(ledger.escrow_explanations),
    )


# ---------------------------------------------------------------------- rates


def _resolve_rate(
    assumptions: Assumptions,
    account: Account,
    *,
    schedule_driven_liabilities: set[str] | None = None,
) -> Rate:
    """Resolve one account's projection rate.

    A liability whose interest is already represented by a formula schedule must
    not also accrue the scenario's generic liability rate.  This matters both for
    loans created by BreadSched and imported GnuCash loans.
    """
    if (
        account.account_class is AccountClass.LIABILITY
        and schedule_driven_liabilities
        and account.handle in schedule_driven_liabilities
    ):
        return Rate(0)
    if account.handle in assumptions.per_account:
        return assumptions.per_account[account.handle]
    if account.account_class is AccountClass.LIABILITY:
        return (
            Rate(account.annual_interest)
            if account.annual_interest
            else assumptions.liability_interest
        )
    if account.annual_return:
        return Rate(account.annual_return)
    if account.atype.is_investment:
        return assumptions.investment_return
    return Rate(0)


@dataclass(slots=True)
class _EventMonthFlows:
    """Effects accumulated while exact dated events are processed."""

    income: Money = field(default_factory=lambda: Money(0))
    expense: Money = field(default_factory=lambda: Money(0))
    cash_flow: Money = field(default_factory=lambda: Money(0))
    cash_interest: Money = field(default_factory=lambda: Money(0))
    holding_movements: dict[str, Money] = field(default_factory=dict)
    holding_contributions: dict[str, Money] = field(default_factory=dict)
    holding_withdrawals: dict[str, Money] = field(default_factory=dict)
    retirement_distributions: dict[str, Money] = field(default_factory=dict)
    investment_income: dict[str, Money] = field(default_factory=dict)
    investment_fees: dict[str, Money] = field(default_factory=dict)
    holding_rollovers: dict[str, Money] = field(default_factory=dict)
    investment_growth: dict[str, Money] = field(default_factory=dict)
    liability_interest: dict[str, Money] = field(default_factory=dict)
    liability_movements: dict[str, Money] = field(default_factory=dict)
    debt_payments: dict[str, Money] = field(default_factory=dict)
    events: list[planning.PlannedEvent] = field(default_factory=list)
    escrow_explanations: list[str] = field(default_factory=list)


def _period_growth_rate(annual: Rate | Decimal, days: int) -> Rate:
    """Effective growth over ``days`` using an actual/365 convention."""
    value = annual.decimal if isinstance(annual, Rate) else annual
    if days <= 0 or value == 0:
        return Rate(0)
    if value <= -1:
        raise ValueError("annual rate must be greater than -100%")
    return Rate((_ONE + value) ** (Decimal(days) / Decimal(365)) - _ONE)


def _advance_event_state(
    timeline: _AssumptionTimeline,
    accounts: dict[str, Account],
    start: date,
    end: date,
    cash: Money,
    holdings: dict[str, Money],
    debts: dict[str, Money],
    flows: _EventMonthFlows,
    schedule_driven_liabilities: set[str],
) -> Money:
    """Accrue state from one event date to the next without inventing cash events."""
    points = [start, *timeline.boundaries_between(start, end), end]
    for left, right in zip(points, points[1:], strict=False):
        days = (right - left).days
        if days <= 0:
            continue
        assumptions = timeline.at(left)

        cash_rate = _period_growth_rate(assumptions.cash_interest, days)
        cash_growth = (
            (cash * cash_rate).quantize(_PROJECTION_MONEY_DENOMINATOR) if cash_rate else Money(0)
        )
        cash = cash + cash_growth
        flows.cash_interest = flows.cash_interest + cash_growth

        for handle, balance in list(holdings.items()):
            account = accounts[handle]
            rate = _period_growth_rate(
                _resolve_rate(
                    assumptions,
                    account,
                    schedule_driven_liabilities=schedule_driven_liabilities,
                ),
                days,
            )
            growth = (balance * rate).quantize(_PROJECTION_MONEY_DENOMINATOR) if rate else Money(0)
            holdings[handle] = balance + growth
            flows.investment_growth[handle] = flows.investment_growth.get(handle, Money(0)) + growth

        for handle, owed in list(debts.items()):
            account = accounts[handle]
            rate = _period_growth_rate(
                _resolve_rate(
                    assumptions,
                    account,
                    schedule_driven_liabilities=schedule_driven_liabilities,
                ),
                days,
            )
            charge = (
                (owed * rate).quantize(_PROJECTION_MONEY_DENOMINATOR)
                if (rate and owed > 0)
                else Money(0)
            )
            debts[handle] = owed + charge
            flows.liability_interest[handle] = (
                flows.liability_interest.get(handle, Money(0)) + charge
            )
    return cash


def _event_escalation_factor(
    scenario: Scenario,
    timeline: _AssumptionTimeline,
    event: planning.PlannedEvent,
    accounts: dict[str, Account],
    schedule_growth_policies: dict[str, ScheduleGrowthPolicy],
    formula_schedule_handles: set[str],
) -> Decimal:
    """Return the scenario escalation applied to one unresolved schedule event.

    Scheduled transactions are balanced financial events, so income growth or
    expense inflation must scale the whole transaction rather than only the
    category leg.  Actualized events are ledger facts and one-off scenario events
    already state their intended amount, so neither is escalated.
    """
    if (
        event.source not in (planning.EventSource.SCHEDULED, planning.EventSource.SCENARIO_SCHEDULE)
        or event.status is planning.EventStatus.ACTUALIZED
    ):
        return _ONE

    source_handle = event.source_handle
    if source_handle is None:
        return _ONE

    policy = schedule_growth_policies.get(source_handle, ScheduleGrowthPolicy.AUTO)
    if policy is ScheduleGrowthPolicy.NONE:
        return _ONE
    if policy is ScheduleGrowthPolicy.AUTO and source_handle in formula_schedule_handles:
        return _ONE

    classes = {
        account.account_class
        for split in event.expected_splits
        if (account := accounts.get(split.account)) is not None
    }
    if policy is ScheduleGrowthPolicy.INCOME:
        field: Literal["income_growth", "expense_inflation"] = "income_growth"
    elif policy is ScheduleGrowthPolicy.INFLATION:
        field = "expense_inflation"
    elif AccountClass.INCOME in classes:
        # Gross-to-net payroll has both income and expense legs.  In automatic
        # mode the presence of income makes the whole balanced event grow with
        # income, including withholding and the net deposit.
        field = "income_growth"
    elif AccountClass.EXPENSE in classes:
        field = "expense_inflation"
    else:
        return _ONE

    start = scenario.start.replace(day=1)
    months = (event.when.year - start.year) * 12 + (event.when.month - start.month)
    completed_years = max(0, months // 12)
    return timeline.escalation(field, completed_years)


def _record_holding_activity(
    db: DbSQLite,
    account: Account,
    amount: Money,
    kind: InvestmentActivityKind | None,
    flows: _EventMonthFlows,
) -> None:
    """Attribute a signed holding movement without changing its ledger effect."""
    flows.holding_movements[account.handle] = (
        flows.holding_movements.get(account.handle, Money(0)) + amount
    )
    if not account.atype.is_investment:
        return
    if kind is None:
        if amount > 0:
            kind = InvestmentActivityKind.CONTRIBUTION
        elif investment.retirement_context(db, account):
            kind = InvestmentActivityKind.RETIREMENT_DISTRIBUTION
        else:
            kind = InvestmentActivityKind.WITHDRAWAL

    if kind is InvestmentActivityKind.CONTRIBUTION:
        target, reported = flows.holding_contributions, amount
    elif kind is InvestmentActivityKind.WITHDRAWAL:
        target, reported = flows.holding_withdrawals, -amount
    elif kind is InvestmentActivityKind.RETIREMENT_DISTRIBUTION:
        target, reported = flows.retirement_distributions, -amount
    elif kind in {InvestmentActivityKind.DIVIDEND, InvestmentActivityKind.INTEREST}:
        target, reported = flows.investment_income, amount
    elif kind is InvestmentActivityKind.FEE:
        target, reported = flows.investment_fees, -amount
    else:
        target, reported = flows.holding_rollovers, amount
    target[account.handle] = target.get(account.handle, Money(0)) + reported


def _apply_event(
    db: DbSQLite,
    result: Projection,
    scenario: Scenario,
    timeline: _AssumptionTimeline,
    event: planning.PlannedEvent,
    accounts: dict[str, Account],
    cash: Money,
    holdings: dict[str, Money],
    debts: dict[str, Money],
    flows: _EventMonthFlows,
    schedule_growth_policies: dict[str, ScheduleGrowthPolicy],
    formula_schedule_handles: set[str],
    fraction: int,
) -> Money:
    """Apply one event's effective splits to financial state on its exact date."""
    flows.events.append(event)
    factor = _event_escalation_factor(
        scenario, timeline, event, accounts, schedule_growth_policies, formula_schedule_handles
    )
    escrow_before = {
        split.account: holdings.get(split.account, Money(0))
        for split in event.splits
        if (account := accounts.get(split.account)) is not None
        and account.atype is AccountType.ESCROW
    }
    for planned_split in event.splits:
        account = accounts.get(planned_split.account)
        if account is None or account.exclude_from_projection:
            continue
        amount = (planned_split.amount * factor).quantize(fraction)
        cls = account.account_class

        if cls is AccountClass.INCOME:
            flows.income = flows.income + (amount if event.funded_from_cash else -amount)
            if event.funded_from_cash:
                cash = cash + amount
                flows.cash_flow = flows.cash_flow + amount
        elif cls is AccountClass.EXPENSE:
            flows.expense = flows.expense + amount
            if event.funded_from_cash:
                cash = cash - amount
                flows.cash_flow = flows.cash_flow - amount
        elif account.is_spendable_cash:
            cash = cash + amount
            flows.cash_flow = flows.cash_flow + amount
        elif cls is AccountClass.ASSET:
            holdings[account.handle] = holdings.get(account.handle, Money(0)) + amount
            _record_holding_activity(
                db,
                account,
                amount,
                planned_split.investment_activity,
                flows,
            )
            if event.funded_from_cash:
                cash = cash - amount
                flows.cash_flow = flows.cash_flow - amount
        elif cls is AccountClass.LIABILITY:
            # Ledger split sign is opposite the natural debt balance: a positive
            # split pays principal down; a negative split borrows more.
            movement = -amount
            debts[account.handle] = debts.get(account.handle, Money(0)) + movement
            flows.liability_movements[account.handle] = (
                flows.liability_movements.get(account.handle, Money(0)) + movement
            )
            if amount > 0:
                flows.debt_payments[account.handle] = (
                    flows.debt_payments.get(account.handle, Money(0)) + amount
                )
            if event.funded_from_cash:
                cash = cash - amount
                flows.cash_flow = flows.cash_flow - amount
    effective_legs = (
        (split.account, (split.amount * factor).quantize(fraction)) for split in event.splits
    )
    escrow = escrow_recognition(effective_legs, accounts)
    flows.expense = flows.expense + escrow.planning_expense_adjustment
    for message in escrow.explanations(accounts):
        if message not in flows.escrow_explanations:
            flows.escrow_explanations.append(message)
    for handle in escrow.movements:
        balance = holdings.get(handle, Money(0))
        before = escrow_before.get(handle, Money(0))
        if balance < 0 and (before >= 0 or balance < before):
            account = accounts[handle]
            _warn_once(
                result,
                f"escrow account {account.name!r} falls below zero by "
                f"{(-balance).format()} after {event.description!r} on "
                f"{event.when:%Y-%m-%d}; the event remains included and the "
                "shortfall is visible in projected holdings",
            )
    return cash


def _drawdown_date(drawdown: Drawdown, month: date) -> date | None:
    """The day in ``month`` a drawdown withdraws on, or ``None`` if it is inactive."""
    last = (add_months(month, 1, day=1) - timedelta(days=1)).day
    when = month.replace(day=min(drawdown.start.day, last))
    return when if drawdown.active(when) else None


class _Drawdowns:
    """Sizes each scenario drawdown from the projected state on its withdrawal date."""

    def __init__(
        self,
        db: DbSQLite,
        result: Projection,
        scenario: Scenario,
        timeline: _AssumptionTimeline,
        accounts: dict[str, Account],
        fraction: int,
    ) -> None:
        self.db = db
        self.result = result
        self.scenario = scenario
        self.timeline = timeline
        self.accounts = accounts
        self.fraction = fraction
        # Escalation applied so far: (anniversaries counted, factor).
        self._escalation: dict[str, tuple[int, Decimal]] = {}
        self._depleted: set[str] = set()

    def due(self, month: date) -> list[tuple[date, Drawdown]]:
        found: list[tuple[date, Drawdown]] = []
        for drawdown in self.scenario.drawdowns:
            when = _drawdown_date(drawdown, month)
            if when is not None:
                found.append((when, drawdown))
        return found

    def _factor(self, drawdown: Drawdown, when: date) -> Decimal:
        years = (when.year - drawdown.start.year) - (
            (when.month, when.day) < (drawdown.start.month, drawdown.start.day)
        )
        counted, factor = self._escalation.get(drawdown.handle, (0, _ONE))
        while counted < years:
            counted += 1
            anniversary = add_months(drawdown.start, counted * 12)
            rate = self.timeline.at(max(anniversary, self.timeline.start)).expense_inflation
            factor = factor * (_ONE + rate.decimal)
        self._escalation[drawdown.handle] = (counted, factor)
        return factor

    def event(
        self, drawdown: Drawdown, when: date, holdings: dict[str, Money]
    ) -> planning.PlannedEvent | None:
        account = self.accounts.get(drawdown.account)
        into = self.accounts.get(drawdown.into)
        if account is None or account.handle not in holdings:
            _warn_once(
                self.result,
                "a drawdown's account is not a projected holding; its withdrawals are left out",
            )
            return None
        if into is None or not into.is_spendable_cash:
            _warn_once(
                self.result,
                f"a drawdown from {account.name!r} pays into an account that is not "
                "projected spendable cash; its withdrawals are left out",
            )
            return None
        balance = holdings[account.handle]
        if drawdown.annual_rate is not None:
            wanted = balance * (drawdown.annual_rate / 12)
        elif drawdown.annual_amount is not None:
            factor = self._factor(drawdown, when) if drawdown.escalate else _ONE
            wanted = (drawdown.annual_amount / 12) * factor
        else:
            return None
        wanted = wanted.quantize(self.fraction)
        amount = min(wanted, balance) if balance > 0 else Money(0)
        if amount < wanted:
            if drawdown.handle not in self._depleted:
                self._depleted.add(drawdown.handle)
                self.result.depletions.append((account.name, when))
            _warn_once(
                self.result,
                f"the drawdown from {account.name!r} runs out of money on "
                f"{when:%Y-%m-%d}; later withdrawals take only what is left",
            )
        if amount <= 0:
            return None
        return planning.PlannedEvent(
            key=f"drawdown:{self.scenario.handle}:{drawdown.handle}:{when.isoformat()}",
            planned_date=when,
            source=planning.EventSource.ONE_OFF,
            description=f"Drawdown from {account.name}",
            expected_splits=(
                planning.PlannedSplit(into.handle, amount),
                planning.PlannedSplit(account.handle, -amount),
            ),
            expected_amount=amount,
            placeholder=True,
        )


def _project_events(
    db: DbSQLite, scenario: Scenario, progress: ProgressCallback | None = None
) -> Projection:
    """Project scheduled/one-off events chronologically; months are display buckets."""
    result = Projection(scenario=scenario)
    start = scenario.start.replace(day=1)
    end_exclusive = add_months(start, scenario.months, day=1)
    end = end_exclusive - timedelta(days=1)
    day_before = start - timedelta(days=1)
    timeline = _AssumptionTimeline(scenario, start, end)
    fraction = reporting_fraction(db)
    # Foreign values use the quote known on the opening valuation date, once.
    converter = ReportingConverter(db, day_before)
    unconverted: list[UnconvertedActivity] = []

    cash = Money(0)
    holdings: dict[str, Money] = {}
    debts: dict[str, Money] = {}
    accounts: dict[str, Account] = {}
    for account in db.iter_accounts():
        if account.is_root or account.placeholder or account.exclude_from_projection:
            continue
        accounts[account.handle] = account
        opening = scenario.opening_overrides.get(account.handle)
        if opening is None:
            valued = valuation.account_value(db, account, as_of=day_before)
            opening = valued.total
            if valued.missing_quote and valued.currency is not None:
                # A foreign-currency balance without a quote is not reporting money.
                unconverted.append(
                    UnconvertedActivity(
                        "balance",
                        day_before,
                        db.full_name(account),
                        opening,
                        valued.currency.handle,
                        (account.handle,),
                    )
                )
                opening = Money(0)
        if account.is_spendable_cash:
            cash = cash + opening
        elif account.account_class is AccountClass.ASSET:
            holdings[account.handle] = opening
        elif account.account_class is AccountClass.LIABILITY:
            debts[account.handle] = opening

    formula_schedule_handles: set[str] = set()
    schedule_growth_policies: dict[str, ScheduleGrowthPolicy] = {}
    schedule_driven_liabilities: set[str] = set()

    def record_formula_schedule(
        scheduled_tx: ScheduledTransaction | ScenarioSchedule,
    ) -> None:
        schedule_growth_policies[scheduled_tx.handle] = scheduled_tx.growth_policy
        if not any(split.formula for split in scheduled_tx.splits):
            return
        formula_schedule_handles.add(scheduled_tx.handle)
        for split in scheduled_tx.splits:
            liability_account = accounts.get(split.account)
            if (
                liability_account is not None
                and liability_account.account_class is AccountClass.LIABILITY
            ):
                schedule_driven_liabilities.add(liability_account.handle)

    for scheduled_tx in db.iter_scheduled():
        if not scheduled_tx.usable:
            reason = scheduled_tx.unsupported_reason or scheduled_tx.formula_problem()
            _warn_once(
                result,
                f"scheduled transaction {scheduled_tx.name!r} is preserved but excluded "
                f"from projection: {reason}",
            )
            continue
        record_formula_schedule(scheduled_tx)
    for scenario_schedule in scenario.schedule_overrides:
        record_formula_schedule(scenario_schedule)

    _report_progress(progress, start, start, end, "Preparing events")
    all_events = []
    for original in planning.scenario_events(db, scenario, start, end):
        event = converter.event(original)
        if event is not None and event.actual_transaction is not None:
            if not converter.reporting_actual(event):
                event = None
        if event is None:
            unconverted.append(
                UnconvertedActivity(
                    "planned",
                    original.when,
                    original.description,
                    original.amount,
                    (
                        original.actual_currency
                        if original.actual_transaction is not None
                        and converter.factor(original.actual_currency) is None
                        else original.expected_currency
                    )
                    or converter.target,
                    tuple(split.account for split in original.splits),
                )
            )
            continue
        all_events.append(event)
    for note in conversion_notes(
        db,
        (converter.used[key] for key in sorted(converter.used)),
        unconverted,
        day_before,
    ):
        _warn_once(result, note)
    result.excluded = excluded_activity(db, unconverted)
    for event in all_events:
        if event.source not in (
            planning.EventSource.SCHEDULED,
            planning.EventSource.SCENARIO_SCHEDULE,
        ):
            continue
        residual = _sum(split.amount for split in event.expected_splits)
        if residual.quantize(fraction):
            _warn_once(
                result,
                f"scheduled transaction {event.description!r} does not balance: its "
                f"calculated legs differ by {residual}. The forecast uses them as "
                f"they stand, so its totals carry that difference.",
            )

    events_by_month: dict[tuple[int, int], list[planning.PlannedEvent]] = {}
    for event in all_events:
        if start <= event.when <= end:
            events_by_month.setdefault((event.when.year, event.when.month), []).append(event)

    drawdowns = _Drawdowns(db, result, scenario, timeline, accounts, fraction)
    for index in range(scenario.months):
        month = add_months(start, index, day=1)
        next_month = add_months(start, index + 1, day=1)
        cash_open = cash
        holdings_open = dict(holdings)
        liabilities_open = dict(debts)
        flows = _EventMonthFlows()
        cursor = month

        # A drawdown is sized from the state on its own date, after that day's events.
        scheduled: list[tuple[date, int, planning.PlannedEvent | Drawdown]] = [
            (event.when, 0, event) for event in events_by_month.get((month.year, month.month), [])
        ]
        scheduled.extend((when, 1, item) for when, item in drawdowns.due(month))
        scheduled.sort(key=lambda entry: (entry[0], entry[1]))
        for when, _order, item in scheduled:
            if isinstance(item, Drawdown):
                cash = _advance_event_state(
                    timeline,
                    accounts,
                    cursor,
                    when,
                    cash,
                    holdings,
                    debts,
                    flows,
                    schedule_driven_liabilities,
                )
                cursor = when
                drawn = drawdowns.event(item, when, holdings)
                if drawn is None:
                    continue
                event = drawn
            else:
                event = item
            _report_progress(progress, event.when, start, end, "Applying scheduled events")
            cash = _advance_event_state(
                timeline,
                accounts,
                cursor,
                event.when,
                cash,
                holdings,
                debts,
                flows,
                schedule_driven_liabilities,
            )
            cash = _apply_event(
                db,
                result,
                scenario,
                timeline,
                event,
                accounts,
                cash,
                holdings,
                debts,
                flows,
                schedule_growth_policies,
                formula_schedule_handles,
                fraction,
            )
            cursor = event.when

        cash = _advance_event_state(
            timeline,
            accounts,
            cursor,
            next_month,
            cash,
            holdings,
            debts,
            flows,
            schedule_driven_liabilities,
        )

        month_ledger = MonthLedger(
            opening_cash=cash_open,
            opening_holdings=holdings_open,
            opening_liabilities=liabilities_open,
            cash_flow=flows.cash_flow,
            cash_interest=flows.cash_interest,
            holding_movements=dict(flows.holding_movements),
            holding_contributions=dict(flows.holding_contributions),
            holding_withdrawals=dict(flows.holding_withdrawals),
            retirement_distributions=dict(flows.retirement_distributions),
            investment_income=dict(flows.investment_income),
            investment_fees=dict(flows.investment_fees),
            holding_rollovers=dict(flows.holding_rollovers),
            investment_growth=dict(flows.investment_growth),
            liability_interest=dict(flows.liability_interest),
            debt_payments=dict(flows.debt_payments),
            liability_movements=dict(flows.liability_movements),
            escrow_explanations=list(flows.escrow_explanations),
            events=list(flows.events),
            closing_cash=cash,
            closing_holdings=dict(holdings),
            closing_liabilities=dict(debts),
        )
        if not month_ledger.reconciles():
            raise RuntimeError(f"projection month {month:%Y-%m} does not reconcile")

        _report_progress(
            progress,
            min(next_month - timedelta(days=1), end),
            start,
            end,
            "Closing reporting period",
        )
        result.rows.append(
            MonthRow(
                index=index,
                month=month,
                cash_open=cash_open,
                income=flows.income,
                expense=flows.expense,
                contributions=_sum(flows.holding_contributions.values()),
                withdrawals=_sum(flows.holding_withdrawals.values()),
                retirement_distributions=_sum(flows.retirement_distributions.values()),
                investment_income=_sum(flows.investment_income.values()),
                investment_fees=_sum(flows.investment_fees.values()),
                rollovers=_sum(amount for amount in flows.holding_rollovers.values() if amount > 0),
                debt_payments=_sum(flows.debt_payments.values()),
                interest_earned=flows.cash_interest,
                investment_growth=_sum(flows.investment_growth.values()),
                interest_charged=_sum(flows.liability_interest.values()),
                cash_close=cash,
                holdings=_sum(holdings.values()),
                liabilities=_sum(debts.values()),
                ledger=month_ledger,
            )
        )
    goal_months, result.goal_milestones = project_goals(
        db,
        scenario,
        [row.month for row in result.rows],
        [row.income for row in result.rows],
        [row.cash_close for row in result.rows],
        [row.ledger.closing_holdings for row in result.rows],
    )
    for row, goals in zip(result.rows, goal_months, strict=True):
        row.goals_set_aside = goals.set_aside
        row.goals_held = goals.held
    result.reimbursements = reimbursement_outlook(db, scenario, start, end)
    _report_progress(progress, end, start, end, "Complete")
    return result


def project(
    db: DbSQLite, scenario: Scenario, progress: ProgressCallback | None = None
) -> Projection:
    """Run a scenario, optionally reporting calendar progress through its horizon."""
    return _project_events(db, scenario, progress)


def _warn_once(result: Projection, message: str) -> None:
    """Record a warning, without repeating it for every month it recurs in."""
    if message not in result.warnings:
        result.warnings.append(message)


def _sum(values) -> Money:
    total = Money(0)
    for value in values:
        total = total + value
    return total
