"""What a projection returns: monthly rows, the conservation ledger, and detail.

``projection.project`` produces a ``Projection`` of ``MonthRow`` reports, each with
its ``MonthLedger``; ``projection.explain_month`` fills a ``ProjectionMonthDetail``.
These types only hold and summarize results, never calculate a projection, so
presentation, print, and comparison can depend on them without the engine;
``projection`` re-exports them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import TypedDict

from ..lib.money import Money
from ..lib.recurrence import add_months
from ..lib.scenario import Assumptions, Scenario
from . import planning
from .chart_model import ChartModel
from .completeness import Completeness, Excluded, Policy, combine
from .goal_projection import GoalMilestone
from .reimbursement_outlook import ReimbursementOutlook

__all__ = [
    "projection_balances_chart",
    "projection_chart",
    "CashRunway",
    "ComparisonRow",
    "MonthLedger",
    "MonthRow",
    "Projection",
    "ProjectionAccountDetail",
    "ProjectionMonthDetail",
    "ProjectionProgress",
    "compare",
]


@dataclass(frozen=True, slots=True)
class ProjectionProgress:
    """Progress through the projection horizon, expressed in calendar time."""

    current: date
    end: date
    fraction: float
    phase: str = "Calculating"


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
    holding_movements: dict[str, Money]
    holding_contributions: dict[str, Money]
    holding_withdrawals: dict[str, Money]
    retirement_distributions: dict[str, Money]
    investment_income: dict[str, Money]
    investment_fees: dict[str, Money]
    holding_rollovers: dict[str, Money]
    investment_growth: dict[str, Money]
    liability_interest: dict[str, Money]
    debt_payments: dict[str, Money]
    #: Signed principal movement in natural debt-balance terms. Positive means
    #: more debt, negative means principal was paid down.
    liability_movements: dict[str, Money] = field(default_factory=dict)
    #: User-facing reasons for escrow recognition in this reporting month.
    escrow_explanations: list[str] = field(default_factory=list)
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
            "holding_movements": dict(self.holding_movements),
            "holding_contributions": dict(self.holding_contributions),
            "holding_withdrawals": dict(self.holding_withdrawals),
            "retirement_distributions": dict(self.retirement_distributions),
            "investment_income": dict(self.investment_income),
            "investment_fees": dict(self.investment_fees),
            "holding_rollovers": dict(self.holding_rollovers),
            "investment_growth": dict(self.investment_growth),
            "liability_interest": dict(self.liability_interest),
            "debt_payments": dict(self.debt_payments),
            "liability_movements": dict(self.liability_movements),
            "escrow_explanations": list(self.escrow_explanations),
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
            | set(self.holding_movements)
            | set(self.investment_growth)
            | set(self.closing_holdings)
        )
        for handle in holding_handles:
            expected = (
                self.opening_holdings.get(handle, Money(0))
                + self.holding_movements.get(handle, Money(0))
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
                # Older callers may provide principal reductions without the
                # more general liability-movement field.
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
    withdrawals: Money
    retirement_distributions: Money
    investment_income: Money
    investment_fees: Money
    rollovers: Money
    debt_payments: Money
    interest_earned: Money
    investment_growth: Money
    interest_charged: Money
    cash_close: Money
    holdings: Money
    liabilities: Money
    ledger: MonthLedger
    #: Projected savings-goal earmarks at month end, and the part held from cash.
    goals_set_aside: Money = field(default_factory=lambda: Money(0))
    goals_held: Money = field(default_factory=lambda: Money(0))

    @property
    def cash_after_goals(self) -> Money:
        """Projected cash less what goals in cash accounts have set aside."""
        return self.cash_close - self.goals_held

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
            "cash_open",
            "income",
            "expense",
            "contributions",
            "withdrawals",
            "retirement_distributions",
            "investment_income",
            "investment_fees",
            "rollovers",
            "debt_payments",
            "interest_earned",
            "investment_growth",
            "interest_charged",
            "cash_close",
            "holdings",
            "liabilities",
        ):
            data[name] = getattr(self, name)
        data["goals_set_aside"] = self.goals_set_aside
        data["goals_held"] = self.goals_held
        data["cash_after_goals"] = self.cash_after_goals
        data["net_flow"] = self.net_flow
        data["net_worth"] = self.net_worth
        data["ledger"] = self.ledger.as_dict()
        return data


@dataclass(frozen=True, slots=True)
class ProjectionAccountDetail:
    """One account's contribution to a projected month-end balance."""

    handle: str
    name: str
    opening: Money
    movement: Money
    accrual: Money
    closing: Money
    annual_rate: Decimal
    annual_rate_source: str
    activities: dict[str, Money] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProjectionMonthDetail:
    """Reconciled explanation of one projected reporting month."""

    index: int
    month: date
    label: str
    cash_open: Money
    cash_flow: Money
    cash_interest: Money
    cash_close: Money
    income: Money
    expense: Money
    holdings_open: Money
    holding_movements: Money
    holding_contributions: Money
    holding_withdrawals: Money
    retirement_distributions: Money
    investment_income: Money
    investment_fees: Money
    holding_rollovers: Money
    investment_growth: Money
    holdings_close: Money
    liabilities_open: Money
    liability_movements: Money
    liability_interest: Money
    liabilities_close: Money
    net_worth: Money
    assumptions: Assumptions
    assumption_sources: dict[str, str]
    events: tuple[planning.PlannedEvent, ...]
    holdings: tuple[ProjectionAccountDetail, ...]
    liabilities: tuple[ProjectionAccountDetail, ...]
    escrow_explanations: tuple[str, ...] = ()


@dataclass(slots=True)
class Projection:
    """The result of running one scenario against the current ledger."""

    scenario: Scenario
    rows: list[MonthRow] = field(default_factory=list)
    #: Events or assumptions that could not be applied.
    warnings: list[str] = field(default_factory=list)
    #: Each savings goal's target date in this scenario (pinned unless overridden).
    goal_milestones: list[GoalMilestone] = field(default_factory=list)
    #: Each reimbursement expected in the range, with its gross and net cost.
    reimbursements: list[ReimbursementOutlook] = field(default_factory=list)
    #: Accounts a drawdown emptied, with the first withdrawal it could not make whole.
    depletions: list[tuple[str, date]] = field(default_factory=list)
    #: Opening balances and events left out for lack of an exchange rate (#236).
    excluded: tuple[Excluded, ...] = ()

    @property
    def completeness(self) -> Completeness:
        """The projection is a labelled subtotal of what converted."""
        start = self.rows[0].month if self.rows else None
        return Completeness.of(self.excluded, policy=Policy.SUBTOTAL, as_of=start)

    def month_completeness(self, index: int) -> Completeness:
        """Coverage of month ``index``: balances carry exclusions forward.

        An excluded opening balance affects every month; an excluded event
        affects its own month and, through the balances it would have moved,
        every later one.
        """
        if not 0 <= index < len(self.rows):
            return Completeness()
        end = add_months(self.rows[index].month, 1, day=1) - timedelta(days=1)
        return Completeness.of(
            (item for item in self.excluded if item.when is None or item.when <= end),
            policy=Policy.SUBTOTAL,
            as_of=self.rows[index].month,
        )

    # ------------------------------------------------------------------ series

    def series(self, attribute: str) -> list[Money]:
        return [getattr(row, attribute) for row in self.rows]

    def annual(self, attribute: str) -> list[Money]:
        """Sum a flow attribute into calendar years of the projection."""
        out: list[Money] = []
        for start in range(0, len(self.rows), 12):
            total = Money(0)
            for row in self.rows[start : start + 12]:
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

    def first_goal_shortfall(self) -> MonthRow | None:
        """The first month cash covers bills but not what goals have set aside."""
        for row in self.rows:
            if row.cash_close >= 0 and row.cash_after_goals < 0:
                return row
        return None

    def runway(self) -> CashRunway:
        """How long projected cash lasts, its low point, and what runs out when."""
        shortfall = self.first_shortfall()
        goal_shortfall = self.first_goal_shortfall()
        lowest = min(self.rows, key=lambda row: row.cash_close, default=None)
        return CashRunway(
            months=len(self.rows),
            months_covered=shortfall.index if shortfall is not None else len(self.rows),
            first_shortfall=shortfall.month if shortfall is not None else None,
            lowest_cash=lowest.cash_close if lowest is not None else Money(0),
            lowest_month=lowest.month if lowest is not None else None,
            first_goal_shortfall=goal_shortfall.month if goal_shortfall is not None else None,
            depletions=tuple(self.depletions),
        )

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
            "total_contributions": self.total("contributions"),
            "total_withdrawals": self.total("withdrawals"),
            "total_retirement_distributions": self.total("retirement_distributions"),
            "total_investment_income": self.total("investment_income"),
            "total_investment_fees": self.total("investment_fees"),
            "total_rollovers": self.total("rollovers"),
            "total_growth": self.total("investment_growth"),
            "first_shortfall": shortfall.label if shortfall else None,
            "ending_goals_set_aside": self.rows[-1].goals_set_aside if self.rows else Money(0),
            "first_goal_shortfall": (
                goal_shortfall.label
                if (goal_shortfall := self.first_goal_shortfall()) is not None
                else None
            ),
            "warnings": list(self.warnings),
        }


def _sum(values) -> Money:
    total = Money(0)
    for value in values:
        total = total + value
    return total


@dataclass(frozen=True, slots=True)
class CashRunway:
    """How long one projection's spendable cash lasts.

    ``months_covered`` counts the months before the first one that closes with
    negative cash (all of them when none does). The low point, the first month
    cash no longer covers what savings goals set aside, and each drawdown account
    that runs out complete the picture.
    """

    months: int
    months_covered: int
    first_shortfall: date | None
    lowest_cash: Money
    lowest_month: date | None
    first_goal_shortfall: date | None
    #: (account name, date) for each drawdown account that ran out.
    depletions: tuple[tuple[str, date], ...] = ()

    @property
    def lasts(self) -> bool:
        return self.first_shortfall is None

    def as_dict(self) -> dict[str, object]:
        return {
            "months": self.months,
            "months_covered": self.months_covered,
            "lasts": self.lasts,
            "first_shortfall": self.first_shortfall,
            "lowest_cash": self.lowest_cash,
            "lowest_month": self.lowest_month,
            "first_goal_shortfall": self.first_goal_shortfall,
            "depletions": [{"account": name, "date": when} for name, when in self.depletions],
        }


class ComparisonRow(TypedDict):
    index: int
    label: str
    cash_delta: Money
    net_worth_delta: Money
    base_net_worth: Money
    other_net_worth: Money
    #: Coverage of both inputs: a delta between subtotals is itself partial (#236).
    completeness: Completeness


def compare(base: Projection, other: Projection) -> list[ComparisonRow]:
    """Row-by-row difference between two scenarios, for the comparison view."""
    rows: list[ComparisonRow] = []
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
                "completeness": combine(
                    base.month_completeness(index), other.month_completeness(index)
                ),
            }
        )
    return rows


def projection_chart(
    result: Projection, comparison: Projection | None = None, currency: str = ""
) -> ChartModel:
    """Projected cash, investments, and net worth by month, as a line chart.

    The first cash shortfall and the first month goals' earmarks exceed cash are
    marked; with a comparison, its net worth is overlaid (slot 4). Months whose
    values leave out an unconverted amount are flagged as partial from the first.
    """
    from .chart_model import LINE, ChartMarker, ChartModel, ChartSeries

    rows = result.rows
    series = [
        ChartSeries("cash", "Cash", tuple(row.cash_close for row in rows), 1),
        ChartSeries("holdings", "Investments", tuple(row.holdings for row in rows), 2),
        ChartSeries("net_worth", "Net worth", tuple(row.net_worth for row in rows), 3),
    ]
    if comparison is not None:
        compared = [row.net_worth for row in comparison.rows]
        series.append(
            ChartSeries(
                "comparison_net_worth",
                f"{comparison.scenario.name} net worth",
                tuple(
                    compared[index] if index < len(compared) else None for index in range(len(rows))
                ),
                4,
            )
        )
    markers = []
    shortfall = result.first_shortfall()
    if shortfall is not None:
        markers.append(ChartMarker(shortfall.index, f"First shortfall: {shortfall.label}"))
    goal_shortfall = result.first_goal_shortfall()
    if goal_shortfall is not None and (shortfall is None or goal_shortfall.index < shortfall.index):
        markers.append(
            ChartMarker(goal_shortfall.index, f"Goals exceed cash: {goal_shortfall.label}")
        )
    partial = next(
        (index for index in range(len(rows)) if not result.month_completeness(index).complete),
        None,
    )
    return ChartModel(
        "projection",
        "Projected cash, investments, and net worth",
        LINE,
        tuple(row.label for row in rows),
        tuple(series),
        currency,
        tuple(markers),
        partial,
        (
            f"Partial from {rows[partial].label} (shaded): "
            + result.month_completeness(partial).label.removeprefix("Partial: ")
            if partial is not None
            else ""
        ),
    )


#: The most accounts the balances chart names; the rest share one "Other".
_NAMED_BALANCES = 7


def projection_balances_chart(
    result: Projection, names: Mapping[str, str], currency: str = ""
) -> ChartModel:
    """Each account's projected balance at every year end, stacked.

    Cash (one pool, slot 1) and each investment account stack up from zero and
    each debt stacks down, so a column sums exactly to that year end's net worth
    (its ``totals``). Accounts are ranked by their largest balance; past the
    seventh, the rest are combined as "Other". Years whose values leave out an
    unconverted amount are shaded, as in ``projection_chart``.
    """
    from .chart_model import STACKED, ChartModel, ChartSeries

    indices = [min(i + 11, len(result.rows) - 1) for i in range(0, len(result.rows), 12)]
    rows = [result.rows[index] for index in indices]
    columns: dict[str, list[Money]] = {"cash": [row.ledger.closing_cash for row in rows]}
    labels: dict[str, str] = {"cash": "Cash"}
    for kind, sign in (("holding", 1), ("debt", -1)):
        for position, row in enumerate(rows):
            balances = (
                row.ledger.closing_holdings if kind == "holding" else row.ledger.closing_liabilities
            )
            for handle, balance in balances.items():
                key = f"{kind}:{handle}"
                labels[key] = names.get(handle, handle) + (" (debt)" if kind == "debt" else "")
                columns.setdefault(key, [Money(0)] * len(rows))[position] = balance * sign
    largest = {
        key: max((abs(value) for value in values), default=Money(0))
        for key, values in columns.items()
    }
    accounts = [key for key in columns if key != "cash" and largest[key]]
    ranked = sorted(accounts, key=lambda key: (-largest[key], labels[key].casefold()))
    named = set(ranked if len(ranked) <= _NAMED_BALANCES else ranked[: _NAMED_BALANCES - 1])
    # Cash first, then investments, then debts, each largest first.
    order = [
        "cash",
        *(key for key in ranked if key in named and key.startswith("holding:")),
        *(key for key in ranked if key in named and key.startswith("debt:")),
    ]
    series = [
        ChartSeries(key, labels[key], tuple(columns[key]), slot)
        for slot, key in enumerate(order, start=1)
    ]
    rest = [key for key in ranked if key not in named]
    if rest:
        series.append(
            ChartSeries(
                "other",
                "Other",
                tuple(
                    sum((columns[key][position] for key in rest), Money(0))
                    for position in range(len(rows))
                ),
                _NAMED_BALANCES + 1,
            )
        )
    for position, row in enumerate(rows):
        column = sum((item.values[position] or Money(0) for item in series), Money(0))
        if column != row.net_worth:
            raise AssertionError("projected account balances do not reconcile to net worth")
    partial = next(
        (
            position
            for position, index in enumerate(indices)
            if not result.month_completeness(index).complete
        ),
        None,
    )
    return ChartModel(
        "projection_balances",
        "Year-end balances by account",
        STACKED,
        tuple(row.label for row in rows),
        tuple(series),
        currency,
        partial_from=partial,
        partial_note=(
            f"Partial from {rows[partial].label} (shaded): "
            + result.month_completeness(indices[partial]).label.removeprefix("Partial: ")
            if partial is not None
            else ""
        ),
        totals=tuple(row.net_worth for row in rows),
    )
