"""The projection's conservation identity, term by term.

Every projected month is built so that each closing stock equals its opening
stock plus dated flows plus interest, performance, and other assumption effects;
``projection.MonthLedger.reconciles`` checks it and the engine refuses a month
that does not. This module states that identity for people: for one month or any
run of months it lists, for **Cash**, **Investments**, **Debts**, and **Net
worth**, the opening balance, each term, the closing balance, and what the terms
leave unexplained (always zero for a projection the engine accepted).

Net worth is cash plus investments less debts, so its bridge takes the planned
events net of transfers between those three (a contribution leaves cash and
enters investments; a principal payment leaves cash and reduces a debt) and the
three assumption effects: cash interest, investment performance, and debt
interest. A bridge over several months sums each month's terms; the opening is the
first month's and the closing the last month's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..lib.money import Money
from .projection import MonthLedger, Projection

__all__ = [
    "BridgeTerm",
    "StockBridge",
    "month_bridges",
    "projection_bridges",
    "range_bridges",
]

#: Term kinds: the opening and closing stocks, dated plan flows, and effects
#: that come from rate assumptions.
OPENING = "opening"
FLOW = "flow"
EFFECT = "effect"
CLOSING = "closing"


@dataclass(frozen=True, slots=True)
class BridgeTerm:
    key: str
    label: str
    kind: str
    amount: Money
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "amount": self.amount,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class StockBridge:
    """Opening + terms = closing for one stock."""

    key: str
    label: str
    terms: tuple[BridgeTerm, ...]

    def _one(self, kind: str) -> Money:
        return next(term.amount for term in self.terms if term.kind == kind)

    @property
    def opening(self) -> Money:
        return self._one(OPENING)

    @property
    def closing(self) -> Money:
        return self._one(CLOSING)

    @property
    def changes(self) -> tuple[BridgeTerm, ...]:
        return tuple(term for term in self.terms if term.kind in (FLOW, EFFECT))

    @property
    def explained(self) -> Money:
        total = self.opening
        for term in self.changes:
            total = total + term.amount
        return total

    @property
    def unexplained(self) -> Money:
        return self.closing - self.explained

    @property
    def reconciles(self) -> bool:
        return not self.unexplained

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "terms": [term.as_dict() for term in self.terms],
            "opening": self.opening,
            "closing": self.closing,
            "explained": self.explained,
            "unexplained": self.unexplained,
            "reconciles": self.reconciles,
        }


def _total(values) -> Money:
    total = Money(0)
    for value in values:
        total = total + value
    return total


def _liability_movement(ledger: MonthLedger) -> Money:
    handles = set(ledger.opening_liabilities) | set(ledger.closing_liabilities)
    handles |= set(ledger.liability_movements) | set(ledger.debt_payments)
    total = Money(0)
    for handle in handles:
        movement = ledger.liability_movements.get(handle)
        if movement is None:
            movement = -ledger.debt_payments.get(handle, Money(0))
        total = total + movement
    return total


def range_bridges(ledgers: Sequence[MonthLedger]) -> tuple[StockBridge, ...]:
    """Bridges from the first ledger's opening to the last ledger's closing."""
    if not ledgers:
        raise ValueError("a bridge needs at least one month")
    first, last = ledgers[0], ledgers[-1]
    cash_flow = _total(item.cash_flow for item in ledgers)
    cash_interest = _total(item.cash_interest for item in ledgers)
    holding_flow = _total(_total(item.holding_movements.values()) for item in ledgers)
    growth = _total(_total(item.investment_growth.values()) for item in ledgers)
    contributions = _total(_total(item.holding_contributions.values()) for item in ledgers)
    withdrawals = _total(_total(item.holding_withdrawals.values()) for item in ledgers)
    debt_movement = _total(_liability_movement(item) for item in ledgers)
    debt_interest = _total(_total(item.liability_interest.values()) for item in ledgers)
    open_worth = first.opening_cash + first.holdings_open - first.liabilities_open
    close_worth = last.closing_cash + last.holdings_close - last.liabilities_close
    cash = StockBridge(
        "cash",
        "Cash",
        (
            BridgeTerm("opening", "Opening cash", OPENING, first.opening_cash),
            BridgeTerm(
                "planned",
                "Planned events into and out of cash",
                FLOW,
                cash_flow,
                "Income, spending, transfers, and debt payments dated in these months",
            ),
            BridgeTerm(
                "interest",
                "Cash interest",
                EFFECT,
                cash_interest,
                "From the cash interest assumption",
            ),
            BridgeTerm("closing", "Closing cash", CLOSING, last.closing_cash),
        ),
    )
    investments = StockBridge(
        "investments",
        "Investments",
        (
            BridgeTerm("opening", "Opening investments", OPENING, first.holdings_open),
            BridgeTerm(
                "planned",
                "Planned movements",
                FLOW,
                holding_flow,
                f"Contributions {contributions.format()}, withdrawals and distributions "
                f"{withdrawals.format()}, and reinvested income, fees, and rollovers",
            ),
            BridgeTerm(
                "performance",
                "Investment performance",
                EFFECT,
                growth,
                "From each account's return or the investment return assumption",
            ),
            BridgeTerm("closing", "Closing investments", CLOSING, last.holdings_close),
        ),
    )
    debts = StockBridge(
        "debts",
        "Debts",
        (
            BridgeTerm("opening", "Opening debts", OPENING, first.liabilities_open),
            BridgeTerm(
                "principal",
                "Principal borrowed less repaid",
                FLOW,
                debt_movement,
                "Planned borrowing and principal payments",
            ),
            BridgeTerm(
                "interest",
                "Debt interest",
                EFFECT,
                debt_interest,
                "From each account's rate or the liability interest assumption",
            ),
            BridgeTerm("closing", "Closing debts", CLOSING, last.liabilities_close),
        ),
    )
    worth = StockBridge(
        "net_worth",
        "Net worth",
        (
            BridgeTerm("opening", "Opening net worth", OPENING, open_worth),
            BridgeTerm(
                "planned",
                "Planned events, net of transfers",
                FLOW,
                cash_flow + holding_flow - debt_movement,
                "Cash events plus investment movements less principal borrowed; moves "
                "between cash, investments, and debts cancel",
            ),
            BridgeTerm("cash_interest", "Cash interest", EFFECT, cash_interest),
            BridgeTerm("performance", "Investment performance", EFFECT, growth),
            BridgeTerm("debt_interest", "Debt interest", EFFECT, -debt_interest),
            BridgeTerm("closing", "Closing net worth", CLOSING, close_worth),
        ),
    )
    return (cash, investments, debts, worth)


def month_bridges(result: Projection, index: int) -> tuple[StockBridge, ...]:
    """The bridges for one projected reporting month."""
    if index < 0 or index >= len(result.rows):
        raise IndexError(index)
    return range_bridges([result.rows[index].ledger])


def projection_bridges(result: Projection) -> tuple[StockBridge, ...] | None:
    """The bridges across the whole projection, or ``None`` when it has no months."""
    if not result.rows:
        return None
    return range_bridges([row.ledger for row in result.rows])
