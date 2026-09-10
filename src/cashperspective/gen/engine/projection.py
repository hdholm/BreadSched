"""Multi-year cash-flow projection.

New schedule-driven scenarios are calculated as a chronological event stream.
Scheduled transactions and one-off plan items change state on their actual planned
dates; interest and investment assumptions accrue over the exact interval between
events. Months remain reporting buckets only.

Legacy budget/combined scenarios keep the period engine temporarily so existing
books remain readable while the planning UI migrates to scheduled-event budgeting.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass
from ..lib.money import Money
from ..lib.recurrence import add_months
from ..lib.scenario import Assumptions, ProjectionBasis, Scenario
from . import ledger, planning, schedule

__all__ = [
    "MonthLedger",
    "MonthRow",
    "Projection",
    "ProjectionProgress",
    "compare",
    "project",
]

_ONE = Decimal(1)
_TWELFTH = Decimal(1) / Decimal(12)
# Projection growth rates originate as finite-precision Decimals. Converting them
# directly to exact Money rationals and compounding forever lets denominators grow
# without bound, eventually making bigint gcd/multiplication dominate runtime.
# Keep eight decimal places of a currency unit internally (one hundred-thousandth
# of a cent) after each accrual step. This is far below display precision while
# bounding rational sizes for long event streams.
_PROJECTION_MONEY_DENOMINATOR = 100_000_000


@dataclass(frozen=True, slots=True)
class ProjectionProgress:
    """Progress through the projection horizon, expressed in calendar time."""

    current: date
    end: date
    fraction: float
    phase: str = "Calculating"


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
                self._income_factors[-1] * (_ONE + assumptions.income_growth)
            )
            self._expense_factors.append(
                self._expense_factors[-1] * (_ONE + assumptions.expense_inflation)
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
        factors = (
            self._income_factors
            if field_name == "income_growth"
            else self._expense_factors
        )
        return factors[min(max(0, completed_years), len(factors) - 1)]


def _report_progress(
    callback: ProgressCallback | None, current: date, start: date, end: date, phase: str
) -> None:
    if callback is None:
        return
    span = max(1, (end - start).days)
    elapsed = min(span, max(0, (current - start).days))
    callback(ProjectionProgress(current=current, end=end, fraction=elapsed / span, phase=phase))


def monthly_rate(annual: Decimal) -> Decimal:
    """Convert an annual nominal rate to the equivalent monthly compounding rate."""
    if annual == 0:
        return Decimal(0)
    if annual <= -1:
        raise ValueError("annual rate must be greater than -100%")
    return (_ONE + annual) ** _TWELFTH - _ONE


@dataclass(slots=True)
class MonthLedger:
    """Explain one month's projected state transition.

    Stock balances are captured per account at the beginning and end of the
    month.  Flow/effect maps contain the exact deltas that bridge those states,
    allowing callers to explain a forecast without reverse-engineering aggregate
    ``MonthRow`` values.
    """

    opening_cash: Money
    opening_holdings: dict[str, Money]
    opening_liabilities: dict[str, Money]
    cash_flow: Money
    cash_interest: Money
    holding_contributions: dict[str, Money]
    investment_growth: dict[str, Money]
    liability_interest: dict[str, Money]
    debt_payments: dict[str, Money]
    #: Signed principal movement in natural debt-balance terms. Positive means
    #: more debt, negative means principal was paid down.
    liability_movements: dict[str, Money] = field(default_factory=dict)
    #: Exact dated plan events that contributed to this reporting month.
    events: list[planning.PlannedEvent] = field(default_factory=list)
    closing_cash: Money = field(default_factory=lambda: Money(0))
    closing_holdings: dict[str, Money] = field(default_factory=dict)
    closing_liabilities: dict[str, Money] = field(default_factory=dict)

    @property
    def holdings_open(self) -> Money:
        return _sum(self.opening_holdings.values())

    @property
    def holdings_close(self) -> Money:
        return _sum(self.closing_holdings.values())

    @property
    def liabilities_open(self) -> Money:
        return _sum(self.opening_liabilities.values())

    @property
    def liabilities_close(self) -> Money:
        return _sum(self.closing_liabilities.values())

    def as_dict(self) -> dict[str, object]:
        """Structured representation suitable for CLI JSON and future UI detail."""
        return {
            "opening_cash": self.opening_cash,
            "opening_holdings": dict(self.opening_holdings),
            "opening_liabilities": dict(self.opening_liabilities),
            "cash_flow": self.cash_flow,
            "cash_interest": self.cash_interest,
            "holding_contributions": dict(self.holding_contributions),
            "investment_growth": dict(self.investment_growth),
            "liability_interest": dict(self.liability_interest),
            "debt_payments": dict(self.debt_payments),
            "liability_movements": dict(self.liability_movements),
            "events": [event.as_dict() for event in self.events],
            "closing_cash": self.closing_cash,
            "closing_holdings": dict(self.closing_holdings),
            "closing_liabilities": dict(self.closing_liabilities),
        }

    def reconciles(self) -> bool:
        """Return whether every closing stock is explained by recorded effects."""
        if self.closing_cash != self.opening_cash + self.cash_flow + self.cash_interest:
            return False

        holding_handles = (
            set(self.opening_holdings)
            | set(self.holding_contributions)
            | set(self.investment_growth)
            | set(self.closing_holdings)
        )
        for handle in holding_handles:
            expected = (
                self.opening_holdings.get(handle, Money(0))
                + self.holding_contributions.get(handle, Money(0))
                + self.investment_growth.get(handle, Money(0))
            )
            if self.closing_holdings.get(handle, Money(0)) != expected:
                return False

        liability_handles = (
            set(self.opening_liabilities)
            | set(self.liability_interest)
            | set(self.debt_payments)
            | set(self.liability_movements)
            | set(self.closing_liabilities)
        )
        for handle in liability_handles:
            movement = self.liability_movements.get(handle)
            if movement is None:
                # Backward-compatible month ledgers produced by the legacy period
                # engine recorded only principal reductions as debt payments.
                movement = -self.debt_payments.get(handle, Money(0))
            expected = (
                self.opening_liabilities.get(handle, Money(0))
                + self.liability_interest.get(handle, Money(0))
                + movement
            )
            if self.closing_liabilities.get(handle, Money(0)) != expected:
                return False
        return True


@dataclass(slots=True)
class MonthRow:
    """One month of the forecast."""

    index: int
    month: date
    cash_open: Money
    income: Money
    expense: Money
    contributions: Money
    debt_payments: Money
    interest_earned: Money
    investment_growth: Money
    interest_charged: Money
    cash_close: Money
    holdings: Money
    liabilities: Money
    ledger: MonthLedger

    @property
    def net_flow(self) -> Money:
        """Operating cash flow: what came in less what went out."""
        return self.income - self.expense

    @property
    def net_worth(self) -> Money:
        return self.cash_close + self.holdings - self.liabilities

    @property
    def label(self) -> str:
        return f"{self.month:%b %Y}"

    @property
    def is_shortfall(self) -> bool:
        return self.cash_close < 0

    def as_dict(self) -> dict[str, object]:
        """Flat mapping for CSV and JSON output.  Slots means no ``__dict__``."""
        data: dict[str, object] = {"month": self.month, "label": self.label}
        for name in (
            "cash_open", "income", "expense", "contributions", "debt_payments",
            "interest_earned", "investment_growth", "interest_charged",
            "cash_close", "holdings", "liabilities",
        ):
            data[name] = getattr(self, name)
        data["net_flow"] = self.net_flow
        data["net_worth"] = self.net_worth
        data["ledger"] = self.ledger.as_dict()
        return data


@dataclass(slots=True)
class Projection:
    """The result of running one scenario against the current ledger."""

    scenario: Scenario
    rows: list[MonthRow] = field(default_factory=list)
    #: Assumptions that could not be applied, e.g. a budget that was deleted.
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ series

    def series(self, attribute: str) -> list[Money]:
        return [getattr(row, attribute) for row in self.rows]

    def annual(self, attribute: str) -> list[Money]:
        """Sum a flow attribute into calendar years of the projection."""
        out: list[Money] = []
        for start in range(0, len(self.rows), 12):
            total = Money(0)
            for row in self.rows[start:start + 12]:
                total = total + getattr(row, attribute)
            out.append(total)
        return out

    def year_end(self, attribute: str) -> list[Money]:
        """Sample a stock attribute at each December of the projection."""
        return [
            getattr(self.rows[min(i + 11, len(self.rows) - 1)], attribute)
            for i in range(0, len(self.rows), 12)
        ]

    # ----------------------------------------------------------------- summary

    @property
    def ending_net_worth(self) -> Money:
        return self.rows[-1].net_worth if self.rows else Money(0)

    @property
    def ending_cash(self) -> Money:
        return self.rows[-1].cash_close if self.rows else Money(0)

    @property
    def minimum_cash(self) -> Money:
        return min((row.cash_close for row in self.rows), default=Money(0))

    def first_shortfall(self) -> MonthRow | None:
        """The month the current account first goes negative, if it ever does."""
        for row in self.rows:
            if row.is_shortfall:
                return row
        return None

    def total(self, attribute: str) -> Money:
        result = Money(0)
        for row in self.rows:
            result = result + getattr(row, attribute)
        return result

    def summary(self) -> dict[str, object]:
        shortfall = self.first_shortfall()
        return {
            "scenario": self.scenario.name,
            "months": len(self.rows),
            "ending_cash": self.ending_cash,
            "ending_net_worth": self.ending_net_worth,
            "minimum_cash": self.minimum_cash,
            "total_income": self.total("income"),
            "total_expense": self.total("expense"),
            "total_growth": self.total("investment_growth"),
            "first_shortfall": shortfall.label if shortfall else None,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------- rates


def _resolve_rate(assumptions: Assumptions, account: Account) -> Decimal:
    """Per-account override, then the account's own rate, then the global default."""
    if account.handle in assumptions.per_account:
        return assumptions.per_account[account.handle]
    if account.account_class is AccountClass.LIABILITY:
        return account.annual_interest or assumptions.liability_interest
    if account.annual_return:
        return account.annual_return
    if account.atype.is_investment:
        return assumptions.investment_return
    return Decimal(0)


def _dated_growth_factor(
    scenario: Scenario,
    field: Literal["income_growth", "expense_inflation"],
    month: date,
    completed_years: int,
) -> Decimal:
    """Compound annual budget escalation using assumptions at each anniversary.

    Income and expense budget growth has historically stepped once per projection
    year. Preserve that behavior while allowing the rate used for each future
    annual step to change over time. A period beginning mid-year therefore affects
    the next annual budget escalation; interest/return rates take effect monthly.
    """
    if completed_years <= 0:
        return _ONE
    factor = _ONE
    for year in range(1, completed_years + 1):
        anniversary = add_months(scenario.start.replace(day=1), year * 12, day=1)
        if anniversary > month:
            break
        assumptions = scenario.assumptions_for(anniversary)
        rate = (
            assumptions.income_growth
            if field == "income_growth"
            else assumptions.expense_inflation
        )
        factor *= _ONE + rate
    return factor


# ------------------------------------------------------------------ the model


@dataclass(slots=True)
class _MonthFlows:
    """Accumulates one month's movements, routed by account class.

    A budget line names only one side of a movement, so the other side is inferred
    to be cash (``funded_from_cash``). A scheduled transaction names both sides, so
    nothing is inferred and each split is booked exactly where it falls.
    """

    income: Money = field(default_factory=lambda: Money(0))
    expense: Money = field(default_factory=lambda: Money(0))
    cash_delta: Money = field(default_factory=lambda: Money(0))
    debt_payments: Money = field(default_factory=lambda: Money(0))
    contributions: dict = field(default_factory=dict)

    def apply(
        self, account: Account, amount: Money, funded_from_cash: bool = True
    ) -> None:
        cls = account.account_class
        if cls is AccountClass.INCOME:
            # Income accounts carry credit balances, so a pay cheque posts a
            # negative split value against the income account.
            self.income = self.income + (amount if funded_from_cash else -amount)
            if funded_from_cash:
                self.cash_delta = self.cash_delta + amount
        elif cls is AccountClass.EXPENSE:
            self.expense = self.expense + amount
            if funded_from_cash:
                self.cash_delta = self.cash_delta - amount
        elif account.atype.is_cash_like:
            self.cash_delta = self.cash_delta + amount
        elif cls is AccountClass.ASSET:
            self.contributions[account.handle] = (
                self.contributions.get(account.handle, Money(0)) + amount
            )
            if funded_from_cash:
                self.cash_delta = self.cash_delta - amount
        elif cls is AccountClass.LIABILITY:
            self.debt_payments = self.debt_payments + amount
            if funded_from_cash:
                self.cash_delta = self.cash_delta - amount




@dataclass(slots=True)
class _EventMonthFlows:
    """Effects accumulated while exact dated events are processed."""

    income: Money = field(default_factory=lambda: Money(0))
    expense: Money = field(default_factory=lambda: Money(0))
    cash_flow: Money = field(default_factory=lambda: Money(0))
    cash_interest: Money = field(default_factory=lambda: Money(0))
    holding_contributions: dict[str, Money] = field(default_factory=dict)
    investment_growth: dict[str, Money] = field(default_factory=dict)
    liability_interest: dict[str, Money] = field(default_factory=dict)
    liability_movements: dict[str, Money] = field(default_factory=dict)
    debt_payments: dict[str, Money] = field(default_factory=dict)
    events: list[planning.PlannedEvent] = field(default_factory=list)


def _period_growth_rate(annual: Decimal, days: int) -> Decimal:
    """Effective growth over ``days`` using an actual/365 convention."""
    if days <= 0 or annual == 0:
        return Decimal(0)
    if annual <= -1:
        raise ValueError("annual rate must be greater than -100%")
    return (_ONE + annual) ** (Decimal(days) / Decimal(365)) - _ONE


def _advance_event_state(
    timeline: _AssumptionTimeline,
    accounts: dict[str, Account],
    start: date,
    end: date,
    cash: Money,
    holdings: dict[str, Money],
    debts: dict[str, Money],
    flows: _EventMonthFlows,
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
            (cash * Money(cash_rate)).quantize(_PROJECTION_MONEY_DENOMINATOR)
            if cash_rate
            else Money(0)
        )
        cash = cash + cash_growth
        flows.cash_interest = flows.cash_interest + cash_growth

        for handle, balance in list(holdings.items()):
            account = accounts[handle]
            rate = _period_growth_rate(_resolve_rate(assumptions, account), days)
            growth = (
                (balance * Money(rate)).quantize(_PROJECTION_MONEY_DENOMINATOR)
                if rate
                else Money(0)
            )
            holdings[handle] = balance + growth
            flows.investment_growth[handle] = (
                flows.investment_growth.get(handle, Money(0)) + growth
            )

        for handle, owed in list(debts.items()):
            account = accounts[handle]
            rate = _period_growth_rate(_resolve_rate(assumptions, account), days)
            charge = (
                (owed * Money(rate)).quantize(_PROJECTION_MONEY_DENOMINATOR)
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
) -> Decimal:
    """Return the scenario escalation applied to one unresolved schedule event.

    Scheduled transactions are balanced financial events, so income growth or
    expense inflation must scale the whole transaction rather than only the
    category leg.  Actualized events are ledger facts and one-off scenario events
    already state their intended amount, so neither is escalated.
    """
    if (
        event.source is not planning.EventSource.SCHEDULED
        or event.status is planning.EventStatus.ACTUALIZED
    ):
        return _ONE

    classes = {
        account.account_class
        for split in event.expected_splits
        if (account := accounts.get(split.account)) is not None
    }
    if AccountClass.INCOME in classes and AccountClass.EXPENSE not in classes:
        field: Literal["income_growth", "expense_inflation"] = "income_growth"
    elif AccountClass.EXPENSE in classes and AccountClass.INCOME not in classes:
        field = "expense_inflation"
    else:
        return _ONE

    start = scenario.start.replace(day=1)
    months = (event.when.year - start.year) * 12 + (event.when.month - start.month)
    completed_years = max(0, months // 12)
    return timeline.escalation(field, completed_years)


def _apply_event(
    scenario: Scenario,
    timeline: _AssumptionTimeline,
    event: planning.PlannedEvent,
    accounts: dict[str, Account],
    cash: Money,
    holdings: dict[str, Money],
    debts: dict[str, Money],
    flows: _EventMonthFlows,
) -> Money:
    """Apply one event's effective splits to financial state on its exact date."""
    flows.events.append(event)
    factor = _event_escalation_factor(scenario, timeline, event, accounts)
    for planned_split in event.splits:
        account = accounts.get(planned_split.account)
        if account is None or account.exclude_from_projection:
            continue
        amount = (planned_split.amount * factor).quantize(100)
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
        elif account.atype.is_cash_like:
            cash = cash + amount
            flows.cash_flow = flows.cash_flow + amount
        elif cls is AccountClass.ASSET:
            holdings[account.handle] = holdings.get(account.handle, Money(0)) + amount
            flows.holding_contributions[account.handle] = (
                flows.holding_contributions.get(account.handle, Money(0)) + amount
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
    return cash


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
            opening = ledger.balance(db, account.handle, as_of=day_before)
        if account.atype.is_cash_like:
            cash = cash + opening
        elif account.account_class is AccountClass.ASSET:
            holdings[account.handle] = opening
        elif account.account_class is AccountClass.LIABILITY:
            debts[account.handle] = opening

    _report_progress(progress, start, start, end, "Preparing events")
    all_events = planning.scenario_events(db, scenario, start, end)
    for event in all_events:
        if event.source is not planning.EventSource.SCHEDULED:
            continue
        residual = _sum(split.amount for split in event.expected_splits)
        if residual.quantize(100):
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

    for index in range(scenario.months):
        month = add_months(start, index, day=1)
        next_month = add_months(start, index + 1, day=1)
        cash_open = cash
        holdings_open = dict(holdings)
        liabilities_open = dict(debts)
        flows = _EventMonthFlows()
        cursor = month

        for event in events_by_month.get((month.year, month.month), []):
            _report_progress(progress, event.when, start, end, "Applying scheduled events")
            cash = _advance_event_state(
                timeline, accounts, cursor, event.when, cash, holdings, debts, flows
            )
            cash = _apply_event(
                scenario, timeline, event, accounts, cash, holdings, debts, flows
            )
            cursor = event.when

        cash = _advance_event_state(
            timeline, accounts, cursor, next_month, cash, holdings, debts, flows
        )

        month_ledger = MonthLedger(
            opening_cash=cash_open,
            opening_holdings=holdings_open,
            opening_liabilities=liabilities_open,
            cash_flow=flows.cash_flow,
            cash_interest=flows.cash_interest,
            holding_contributions=dict(flows.holding_contributions),
            investment_growth=dict(flows.investment_growth),
            liability_interest=dict(flows.liability_interest),
            debt_payments=dict(flows.debt_payments),
            liability_movements=dict(flows.liability_movements),
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
    _report_progress(progress, end, start, end, "Complete")
    return result


def project(
    db: DbSQLite, scenario: Scenario, progress: ProgressCallback | None = None
) -> Projection:
    """Run a scenario, optionally reporting calendar progress through its horizon."""
    if scenario.basis is ProjectionBasis.SCHEDULED:
        return _project_events(db, scenario, progress)
    return _project_periodic(db, scenario, progress)


def _project_periodic(
    db: DbSQLite, scenario: Scenario, progress: ProgressCallback | None = None
) -> Projection:
    """Run ``scenario`` against the ledger and return month-by-month results."""
    result = Projection(scenario=scenario)
    start = scenario.start.replace(day=1)
    day_before = date.fromordinal(start.toordinal() - 1)
    end = add_months(start, scenario.months, day=1) - timedelta(days=1)
    _report_progress(progress, start, start, end, "Preparing projection")

    budget = db.get_budget(scenario.budget) if scenario.budget else None
    if scenario.budget and budget is None:
        result.warnings.append("the scenario's budget no longer exists; using schedules only")
    use_budget = budget is not None and scenario.basis in (
        ProjectionBasis.BUDGET, ProjectionBasis.COMBINED
    )
    use_schedules = scenario.basis in (ProjectionBasis.SCHEDULED, ProjectionBasis.COMBINED)

    # Accounts driven by a schedule are excluded from the budget under COMBINED,
    # so rent stated in both places is charged once.
    scheduled_accounts: set[str] = set()
    if use_schedules and scenario.basis is ProjectionBasis.COMBINED:
        for sched in db.iter_scheduled():
            if sched.enabled and sched.in_budget(scenario.budget):
                scheduled_accounts.update(split.account for split in sched.splits)

    # --- opening position ---------------------------------------------------
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
            opening = ledger.balance(db, account.handle, as_of=day_before)
        if account.atype.is_cash_like:
            cash = cash + opening
        elif account.account_class is AccountClass.ASSET:
            holdings[account.handle] = opening
        elif account.account_class is AccountClass.LIABILITY:
            debts[account.handle] = opening


    one_offs_by_month: dict[int, list] = {}
    for item in scenario.one_offs:
        index = (item.when.year - start.year) * 12 + (item.when.month - start.month)
        if 0 <= index < scenario.months:
            one_offs_by_month.setdefault(index, []).append(item)

    # --- month loop ---------------------------------------------------------
    for index in range(scenario.months):
        month = add_months(start, index, day=1)
        month_end = date.fromordinal(add_months(start, index + 1, day=1).toordinal() - 1)
        _report_progress(progress, month, start, end, "Calculating reporting period")
        year = index // 12
        assumptions = scenario.assumptions_for(month)
        income_factor = _dated_growth_factor(scenario, "income_growth", month, year)
        expense_factor = _dated_growth_factor(
            scenario, "expense_inflation", month, year
        )
        cash_rate = monthly_rate(assumptions.cash_interest)

        cash_open = cash
        holdings_open = dict(holdings)
        liabilities_open = dict(debts)
        flows = _MonthFlows()

        # Budget lines ------------------------------------------------------
        if use_budget and budget is not None:
            period = _budget_period(budget, month, scenario.extend_budget)
            if period is not None:
                for handle, line in budget.lines.items():
                    if handle in scheduled_accounts:
                        continue
                    budget_account = accounts.get(handle) or db.get_account(handle)
                    if budget_account is None or budget_account.exclude_from_projection:
                        continue
                    amount = line.amount(period)
                    if not amount:
                        continue
                    factor = (
                        income_factor
                        if budget_account.account_class is AccountClass.INCOME
                        else expense_factor
                    )
                    flows.apply(budget_account, (amount * factor).quantize(100))

        # Scheduled transactions --------------------------------------------
        if use_schedules:
            for occurrence in schedule.forecast_occurrences(db, month, month_end):
                sched = occurrence.schedule
                # A projection belongs to a budget, so it only sees the flows that
                # budget includes. Without this every projection is the same one.
                if not sched.in_budget(scenario.budget):
                    continue
                # strict=False: a template whose calculated legs disagree is a
                # problem with that schedule, not a reason to abandon a forty-year
                # forecast. The residual is reported once, below, naming it.
                try:
                    legs = sched.resolved_splits(when=occurrence.when)
                except Exception as exc:  # noqa: BLE001 - one schedule, not the run
                    _warn_once(
                        result,
                        f"scheduled transaction {sched.name!r} could not be "
                        f"calculated ({exc}); it is left out of this projection",
                    )
                    continue

                residual = Money(0)
                for _account, amount in legs:
                    residual = residual + amount
                # Formula legs balance to full precision, not to the cent, so a
                # sub-cent residue is arithmetic noise rather than a broken
                # schedule and must not be reported as one.
                if residual.quantize(100):
                    _warn_once(
                        result,
                        f"scheduled transaction {sched.name!r} does not balance: its "
                        f"calculated legs differ by {residual}. The forecast uses "
                        f"them as they stand, so its totals carry that difference.",
                    )

                for handle, amount in legs:
                    scheduled_account = accounts.get(handle) or db.get_account(handle)
                    if scheduled_account is None or scheduled_account.exclude_from_projection:
                        continue
                    # A schedule states both sides of the movement itself, so each
                    # split is booked where it lands and nothing is inferred.
                    flows.apply(scheduled_account, amount, funded_from_cash=False)

        # One-off events ----------------------------------------------------
        for item in one_offs_by_month.get(index, []):
            event_account = accounts.get(item.account) or db.get_account(item.account)
            if event_account is None:
                flows.cash_delta = flows.cash_delta + item.amount
            else:
                flows.apply(event_account, item.amount)

        # Compounding -------------------------------------------------------
        interest_earned = (cash_open * cash_rate).quantize(100) if cash_rate else Money(0)

        investment_growth = Money(0)
        investment_growth_by_account: dict[str, Money] = {}
        for handle, balance in list(holdings.items()):
            account = accounts[handle]
            rate = monthly_rate(_resolve_rate(assumptions, account))
            growth = (balance * rate).quantize(100) if rate else Money(0)
            investment_growth = investment_growth + growth
            investment_growth_by_account[handle] = growth
            holdings[handle] = balance + growth + flows.contributions.get(handle, Money(0))
        for handle, amount in flows.contributions.items():
            if handle not in holdings:
                holdings[handle] = amount

        interest_charged = Money(0)
        liability_interest_by_account: dict[str, Money] = {}
        for handle, owed in list(debts.items()):
            account = accounts[handle]
            rate = monthly_rate(_resolve_rate(assumptions, account))
            charge = (owed * rate).quantize(100) if (rate and owed > 0) else Money(0)
            interest_charged = interest_charged + charge
            liability_interest_by_account[handle] = charge
            debts[handle] = owed + charge

        applied_debt_payments: dict[str, Money] = {}
        if flows.debt_payments:
            applied_debt_payments = _apply_payments(debts, flows.debt_payments)

        cash = cash_open + flows.cash_delta + interest_earned
        month_ledger = MonthLedger(
            opening_cash=cash_open,
            opening_holdings=holdings_open,
            opening_liabilities=liabilities_open,
            cash_flow=flows.cash_delta,
            cash_interest=interest_earned,
            holding_contributions=dict(flows.contributions),
            investment_growth=investment_growth_by_account,
            liability_interest=liability_interest_by_account,
            debt_payments=applied_debt_payments,
            closing_cash=cash,
            closing_holdings=dict(holdings),
            closing_liabilities=dict(debts),
        )
        if not month_ledger.reconciles():
            raise RuntimeError(f"projection month {month:%Y-%m} does not reconcile")

        result.rows.append(
            MonthRow(
                index=index,
                month=month,
                cash_open=cash_open,
                income=flows.income,
                expense=flows.expense,
                contributions=_sum(flows.contributions.values()),
                debt_payments=flows.debt_payments,
                interest_earned=interest_earned,
                investment_growth=investment_growth,
                interest_charged=interest_charged,
                cash_close=cash,
                holdings=_sum(holdings.values()),
                liabilities=_sum(debts.values()),
                ledger=month_ledger,
            )
        )

    _report_progress(progress, end, start, end, "Complete")
    return result


def _warn_once(result: Projection, message: str) -> None:
    """Record a warning, without repeating it for every month it recurs in."""
    if message not in result.warnings:
        result.warnings.append(message)


def _sum(values) -> Money:
    total = Money(0)
    for value in values:
        total = total + value
    return total


def _apply_payments(debts: dict[str, Money], payment: Money) -> dict[str, Money]:
    """Spread a payment across debts and return the amount applied per account."""
    remaining = payment
    applied_by_account: dict[str, Money] = {}
    for handle in sorted(debts, key=lambda h: debts[h], reverse=True):
        if remaining <= 0:
            break
        owed = debts[handle]
        if owed <= 0:
            continue
        applied = owed if owed < remaining else remaining
        debts[handle] = owed - applied
        applied_by_account[handle] = applied
        remaining = remaining - applied
    return applied_by_account


def _budget_period(budget, month: date, extend: bool) -> int | None:
    """Which budget period covers ``month``, wrapping past the end if allowed.

    Wrapping repeats the budget's seasonal shape rather than flat-lining it, so a
    December that always costs more keeps costing more in year four.
    """
    period = budget.period_for(month)
    if period is not None:
        return period
    if not extend or budget.periods == 0:
        return None
    months_in = (month.year - budget.start.year) * 12 + (month.month - budget.start.month)
    if months_in < 0:
        return None
    index = months_in // budget.kind.months
    return index % budget.periods


def compare(base: Projection, other: Projection) -> list[dict[str, object]]:
    """Row-by-row difference between two scenarios, for the comparison view."""
    rows: list[dict[str, object]] = []
    for index in range(min(len(base.rows), len(other.rows))):
        left, right = base.rows[index], other.rows[index]
        rows.append(
            {
                "index": index,
                "label": left.label,
                "cash_delta": right.cash_close - left.cash_close,
                "net_worth_delta": right.net_worth - left.net_worth,
                "base_net_worth": left.net_worth,
                "other_net_worth": right.net_worth,
            }
        )
    return rows
