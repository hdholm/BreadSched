"""Multi-year cash-flow projection.

The model steps one month at a time.  A month is the right granularity because it
is the period on which households actually run out of money: an annual model shows
a comfortable surplus for a year in which the current account was overdrawn twice.

Three rules keep the numbers defensible.

**Rates compound, they do not divide.**  A 6% annual assumption becomes
``(1.06 ** (1/12)) - 1`` per month, so twelve months of it is 6.00%, not 6.17%.

**Cash is a pool; holdings are per account.**  Spendable money is fungible, so all
bank and cash accounts collapse into one balance.  Investments and debts compound
at their own rates and are tracked separately.

**Nothing is counted twice.**  Under :attr:`ProjectionBasis.COMBINED` a scheduled
transaction wins over a budget line for the same account, because the schedule is
the more specific statement of intent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountClass
from ..lib.money import Money
from ..lib.recurrence import add_months
from ..lib.scenario import Assumptions, ProjectionBasis, Scenario
from . import ledger, schedule

__all__ = ["MonthRow", "Projection", "project", "compare"]

_ONE = Decimal(1)
_TWELFTH = Decimal(1) / Decimal(12)


def monthly_rate(annual: Decimal) -> Decimal:
    """Convert an annual nominal rate to the equivalent monthly compounding rate."""
    if annual == 0:
        return Decimal(0)
    if annual <= -1:
        raise ValueError("annual rate must be greater than -100%")
    return (_ONE + annual) ** _TWELFTH - _ONE


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


def project(db: DbSQLite, scenario: Scenario) -> Projection:
    """Run ``scenario`` against the ledger and return month-by-month results."""
    result = Projection(scenario=scenario)
    start = scenario.start.replace(day=1)
    day_before = date.fromordinal(start.toordinal() - 1)

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
        year = index // 12
        assumptions = scenario.assumptions_for(month)
        income_factor = _dated_growth_factor(scenario, "income_growth", month, year)
        expense_factor = _dated_growth_factor(
            scenario, "expense_inflation", month, year
        )
        cash_rate = monthly_rate(assumptions.cash_interest)

        cash_open = cash
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
        for handle, balance in list(holdings.items()):
            account = accounts[handle]
            rate = monthly_rate(_resolve_rate(assumptions, account))
            growth = (balance * rate).quantize(100) if rate else Money(0)
            investment_growth = investment_growth + growth
            holdings[handle] = balance + growth + flows.contributions.get(handle, Money(0))
        for handle, amount in flows.contributions.items():
            if handle not in holdings:
                holdings[handle] = amount

        interest_charged = Money(0)
        for handle, owed in list(debts.items()):
            account = accounts[handle]
            rate = monthly_rate(_resolve_rate(assumptions, account))
            charge = (owed * rate).quantize(100) if (rate and owed > 0) else Money(0)
            interest_charged = interest_charged + charge
            debts[handle] = owed + charge

        if flows.debt_payments:
            _apply_payments(debts, flows.debt_payments)

        cash = cash_open + flows.cash_delta + interest_earned

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
            )
        )

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


def _apply_payments(debts: dict[str, Money], payment: Money) -> None:
    """Spread a payment across debts, highest balance first, never below zero."""
    remaining = payment
    for handle in sorted(debts, key=lambda h: debts[h], reverse=True):
        if remaining <= 0:
            break
        owed = debts[handle]
        if owed <= 0:
            continue
        applied = owed if owed < remaining else remaining
        debts[handle] = owed - applied
        remaining = remaining - applied


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
