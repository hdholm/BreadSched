"""Budget versus actual, on a cash basis.

Variance is reported with a *direction*, not just a number.  Spending 50 less than
budgeted and earning 50 less than budgeted are the same arithmetic and opposite
news, and a report that shows both as "-50" makes the reader do the sign reasoning
every single row.  :attr:`PeriodResult.favourable` does it once, here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..db.sqlite import DbSQLite
from ..lib.account import AccountClass
from ..lib.budget import Budget
from ..lib.money import Money
from . import ledger

__all__ = ["PeriodResult", "LineResult", "BudgetReport", "build_report"]


@dataclass(slots=True)
class PeriodResult:
    """One account, one period."""

    period: int
    budgeted: Money
    actual: Money
    is_income: bool

    @property
    def variance(self) -> Money:
        return self.actual - self.budgeted

    @property
    def favourable(self) -> bool:
        """More income than planned, or less spending than planned."""
        return self.variance >= 0 if self.is_income else self.variance <= 0

    @property
    def used(self) -> float:
        """Fraction of the budget consumed, for a progress bar.  0.0 if unbudgeted."""
        if not self.budgeted:
            return 0.0
        return float(self.actual.rate() / self.budgeted.rate())


@dataclass(slots=True)
class LineResult:
    """One account across every period of the budget."""

    account: str
    name: str
    account_class: AccountClass
    periods: list[PeriodResult] = field(default_factory=list)

    @property
    def budgeted_total(self) -> Money:
        total = Money(0)
        for period in self.periods:
            total = total + period.budgeted
        return total

    @property
    def actual_total(self) -> Money:
        total = Money(0)
        for period in self.periods:
            total = total + period.actual
        return total

    @property
    def variance_total(self) -> Money:
        return self.actual_total - self.budgeted_total


@dataclass(slots=True)
class BudgetReport:
    """Everything a budget view needs, computed once."""

    budget: Budget
    lines: list[LineResult]
    labels: list[str]

    def by_class(self, cls: AccountClass) -> list[LineResult]:
        return [line for line in self.lines if line.account_class is cls]

    def class_total(self, cls: AccountClass, period: int, actual: bool = False) -> Money:
        total = Money(0)
        for line in self.by_class(cls):
            entry = line.periods[period]
            total = total + (entry.actual if actual else entry.budgeted)
        return total

    def net_cash_flow(self, period: int, actual: bool = False) -> Money:
        """Income less expenses for the period.  Negative means dipping into savings."""
        income = self.class_total(AccountClass.INCOME, period, actual)
        expense = self.class_total(AccountClass.EXPENSE, period, actual)
        return income - expense

    def cumulative_cash_flow(self, actual: bool = False) -> list[Money]:
        running = Money(0)
        out: list[Money] = []
        for period in range(self.budget.periods):
            running = running + self.net_cash_flow(period, actual)
            out.append(running)
        return out

    def shortfall_periods(self, actual: bool = False) -> list[int]:
        """Periods where more goes out than comes in -- the point of the exercise."""
        return [
            period
            for period in range(self.budget.periods)
            if self.net_cash_flow(period, actual) < 0
        ]

    def net_cash_flow_total(self, actual: bool = False) -> Money:
        """Net cash flow across the whole budget: what the year comes to.

        The figure people actually want from a budget. Per-period rows say whether
        each month works; this says whether the year does, and the two can differ
        -- a year that ends ahead can still have run dry in March.
        """
        total = Money(0)
        for period in range(self.budget.periods):
            total = total + self.net_cash_flow(period, actual)
        return total

    def class_grand_total(self, cls: AccountClass, actual: bool = False) -> Money:
        total = Money(0)
        for period in range(self.budget.periods):
            total = total + self.class_total(cls, period, actual)
        return total

    def annual_summary(self, actual: bool = False) -> dict[str, Money]:
        """Income, expenses and the net for the whole budget period."""
        income = self.class_grand_total(AccountClass.INCOME, actual)
        expense = self.class_grand_total(AccountClass.EXPENSE, actual)
        return {"income": income, "expense": expense, "net": income - expense}

    def totals_row(self, actual: bool = False) -> list[Money]:
        return [self.net_cash_flow(p, actual) for p in range(self.budget.periods)]


def actual_for(
    db: DbSQLite,
    account: str,
    start: date,
    end: date,
    include_children: bool = True,
) -> Money:
    """Natural-sign cash movement on an account during a window."""
    if include_children:
        return ledger.balance_recursive(db, account, as_of=end, since=start)
    return ledger.balance(db, account, as_of=end, since=start)


def build_report(
    db: DbSQLite,
    budget: Budget,
    include_unbudgeted: bool = True,
) -> BudgetReport:
    """Compare a budget against what actually happened.

    ``include_unbudgeted`` adds rows for income and expense accounts that saw real
    money but were never budgeted -- the category that quietly eats a plan.
    """
    handles: list[str] = list(budget.lines.keys())
    if include_unbudgeted:
        for account in db.iter_accounts():
            if account.is_root or account.placeholder or account.handle in handles:
                continue
            if not account.atype.is_flow:
                continue
            moved = any(
                db.split_rows(
                    account.handle, budget.period_start(0), budget.period_end(budget.periods - 1)
                )
            )
            if moved:
                handles.append(account.handle)

    lines: list[LineResult] = []
    for handle in handles:
        flow_account = db.get_account(handle)
        if flow_account is None:
            continue
        is_income = flow_account.account_class is AccountClass.INCOME
        result = LineResult(
            account=handle,
            name=db.full_name(flow_account) or flow_account.name,
            account_class=flow_account.account_class,
        )
        for period in range(budget.periods):
            result.periods.append(
                PeriodResult(
                    period=period,
                    budgeted=budget.amount(handle, period),
                    actual=actual_for(
                        db,
                        handle,
                        budget.period_start(period),
                        budget.period_end(period),
                        include_children=False,
                    ),
                    is_income=is_income,
                )
            )
        lines.append(result)

    lines.sort(key=lambda line: (line.account_class.value, line.name))
    labels = [budget.period_label(p) for p in range(budget.periods)]
    return BudgetReport(budget=budget, lines=lines, labels=labels)
