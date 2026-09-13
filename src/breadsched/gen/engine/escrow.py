"""Planning recognition and explanations for restricted escrow assets.

Ledger balances and split values are never changed. Cash/income-funded deposits
are planning expense at funding time; later escrow draws suppress the portion of
expense already recognized. Refunds and manual balance corrections need more
careful treatment so an escrow balance change is not automatically called expense.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum

from ..lib.account import Account, AccountClass, AccountType
from ..lib.money import Money

__all__ = ["EscrowEffect", "EscrowEffectKind", "EscrowRecognition", "recognition"]


class EscrowEffectKind(str, Enum):
    """Economic meaning of one escrow-account movement."""

    FUNDING = "funding"
    DRAW = "draw"
    REFUND = "refund"
    RESTORATION = "restoration"
    TRANSFER = "transfer"
    ADJUSTMENT = "adjustment"


@dataclass(frozen=True, slots=True)
class EscrowEffect:
    """One explained portion of an escrow movement."""

    account: str
    kind: EscrowEffectKind
    amount: Money


@dataclass(frozen=True, slots=True)
class EscrowRecognition:
    """Non-destructive planning interpretation of one balanced ledger event."""

    movements: dict[str, Money] = field(default_factory=dict)
    funding: dict[str, Money] = field(default_factory=dict)
    draws: dict[str, Money] = field(default_factory=dict)
    refunds: dict[str, Money] = field(default_factory=dict)
    restorations: dict[str, Money] = field(default_factory=dict)
    transfers: dict[str, Money] = field(default_factory=dict)
    adjustments: dict[str, Money] = field(default_factory=dict)
    covered_expenses: dict[str, Money] = field(default_factory=dict)
    restored_expenses: dict[str, Money] = field(default_factory=dict)
    combined_debt_principal: Money = field(default_factory=lambda: Money(0))
    combined_expense: Money = field(default_factory=lambda: Money(0))

    @property
    def planning_expense_adjustment(self) -> Money:
        """Amount added to ordinary expense-account legs for planning."""
        return (
            _sum(self.funding.values())
            - _sum(self.covered_expenses.values())
            + _sum(self.restored_expenses.values())
            - _sum(self.refunds.values())
        )

    @property
    def planning_flows(self) -> dict[str, Money]:
        """Signed Escrow-funding rows: deposits positive, cash refunds negative."""
        handles = set(self.funding) | set(self.refunds)
        return {
            handle: self.funding.get(handle, Money(0)) - self.refunds.get(handle, Money(0))
            for handle in handles
            if self.funding.get(handle, Money(0)) != self.refunds.get(handle, Money(0))
        }

    @property
    def effects(self) -> tuple[EscrowEffect, ...]:
        """Structured effects in a stable explanatory order."""
        found: list[EscrowEffect] = []
        for kind, values in (
            (EscrowEffectKind.FUNDING, self.funding),
            (EscrowEffectKind.DRAW, self.draws),
            (EscrowEffectKind.REFUND, self.refunds),
            (EscrowEffectKind.RESTORATION, self.restorations),
            (EscrowEffectKind.TRANSFER, self.transfers),
            (EscrowEffectKind.ADJUSTMENT, self.adjustments),
        ):
            found.extend(
                EscrowEffect(handle, kind, amount)
                for handle, amount in sorted(values.items())
                if amount
            )
        return tuple(found)

    def explanations(self, accounts: Mapping[str, Account]) -> tuple[str, ...]:
        """User-facing reasons for this event's escrow treatment."""
        messages: list[str] = []
        for effect in self.effects:
            account = accounts.get(effect.account)
            name = account.name if account is not None else effect.account
            amount = effect.amount.format(parens_negative=True)
            if effect.kind is EscrowEffectKind.FUNDING:
                text = f"{name}: {amount} funded from household cash/income is recognized now."
            elif effect.kind is EscrowEffectKind.DRAW:
                text = f"{name}: {amount} draw covers expense recognized when escrow was funded."
            elif effect.kind is EscrowEffectKind.REFUND:
                text = f"{name}: {amount} returned to spendable cash reverses prior expense."
            elif effect.kind is EscrowEffectKind.RESTORATION:
                text = f"{name}: {amount} vendor credit restores escrow without new expense."
            elif effect.kind is EscrowEffectKind.TRANSFER:
                text = f"{name}: {amount} escrow transfer changes location, not expense."
            else:
                text = f"{name}: {amount} manual balance adjustment is not planning expense."
            messages.append(text)
        covered = _sum(self.covered_expenses.values())
        if covered and self.draws:
            amount = covered.format(parens_negative=True)
            messages.append(f"Expense-account charges covered by escrow: {amount}.")
        restored = _sum(self.restored_expenses.values())
        if restored and self.restorations:
            amount = restored.format(parens_negative=True)
            messages.append(f"Expense-account credits restored to escrow: {amount}.")
        if self.funding and self.combined_debt_principal:
            messages.append(
                "Combined loan/escrow payment: "
                f"{_sum(self.funding.values()).format(parens_negative=True)} escrow funding "
                f"and {self.combined_expense.format(parens_negative=True)} interest/expense "
                "are recognized as household expense; "
                f"{self.combined_debt_principal.format(parens_negative=True)} principal "
                "only reduces the liability."
            )
        return tuple(messages)


def recognition(
    legs: Iterable[tuple[str, Money]], accounts: Mapping[str, Account]
) -> EscrowRecognition:
    """Explain escrow movements and return their exact planning adjustments.

    Allocation is proportional when an event touches multiple escrow or expense
    accounts. Internal escrow transfers are removed first. Draws cover positive
    expense legs; negative expense legs deposited back into escrow are restorations.
    Remaining deposits are funding only when the event is sourced from spendable
    cash or income. Remaining draws are refunds only when they reach spendable cash.
    Everything else is an explicit balance-sheet adjustment, not household expense.
    """
    movements: dict[str, Money] = {}
    positive_expenses: dict[str, Money] = {}
    negative_expenses: dict[str, Money] = {}
    has_cash_out = False
    has_cash_in = False
    has_income_source = False
    debt_principal = Money(0)
    ordinary_expense = Money(0)
    for handle, amount in legs:
        account = accounts.get(handle)
        if account is None:
            continue
        if account.atype is AccountType.ESCROW:
            movements[handle] = movements.get(handle, Money(0)) + amount
        elif account.account_class is AccountClass.EXPENSE:
            if amount > 0:
                positive_expenses[handle] = positive_expenses.get(handle, Money(0)) + amount
                ordinary_expense = ordinary_expense + amount
            elif amount < 0:
                negative_expenses[handle] = negative_expenses.get(handle, Money(0)) - amount
        elif account.is_spendable_cash:
            has_cash_out = has_cash_out or amount < 0
            has_cash_in = has_cash_in or amount > 0
        elif account.account_class is AccountClass.INCOME and amount < 0:
            has_income_source = True
        elif account.atype is AccountType.LOAN and amount > 0:
            debt_principal = debt_principal + amount

    positive = {handle: amount for handle, amount in movements.items() if amount > 0}
    negative = {handle: -amount for handle, amount in movements.items() if amount < 0}

    transfer_total = min(_sum(positive.values()), _sum(negative.values()))
    positive_transfers = _allocate(transfer_total, positive)
    negative_transfers = _allocate(transfer_total, negative)
    transfers = {
        handle: positive_transfers.get(handle, Money(0)) - negative_transfers.get(handle, Money(0))
        for handle in set(positive_transfers) | set(negative_transfers)
    }
    positive = _subtract_allocations(positive, positive_transfers)
    negative = _subtract_allocations(negative, negative_transfers)

    restoration_total = min(_sum(positive.values()), _sum(negative_expenses.values()))
    restorations = _allocate(restoration_total, positive)
    restored_expenses = _allocate(restoration_total, negative_expenses)
    positive = _subtract_allocations(positive, restorations)

    funding = dict(positive) if has_cash_out or has_income_source else {}
    positive = {} if funding else positive
    positive_adjustments = dict(positive)

    draw_total = min(_sum(negative.values()), _sum(positive_expenses.values()))
    draws = _allocate(draw_total, negative)
    covered_expenses = _allocate(draw_total, positive_expenses)
    negative = _subtract_allocations(negative, draws)

    refunds = dict(negative) if has_cash_in else {}
    negative = {} if refunds else negative
    adjustments = dict(positive_adjustments)
    for handle, amount in negative.items():
        adjustments[handle] = adjustments.get(handle, Money(0)) - amount

    return EscrowRecognition(
        movements=movements,
        funding=funding,
        draws=draws,
        refunds=refunds,
        restorations=restorations,
        transfers=transfers,
        adjustments=adjustments,
        covered_expenses=covered_expenses,
        restored_expenses=restored_expenses,
        combined_debt_principal=debt_principal,
        combined_expense=ordinary_expense,
    )


def _allocate(total: Money, values: Mapping[str, Money]) -> dict[str, Money]:
    available = _sum(values.values())
    if total <= 0 or available <= 0:
        return {}
    if total >= available:
        return dict(values)
    return {handle: total * (amount / available) for handle, amount in values.items()}


def _subtract_allocations(
    values: Mapping[str, Money], allocations: Mapping[str, Money]
) -> dict[str, Money]:
    return {
        handle: remaining
        for handle, amount in values.items()
        if (remaining := amount - allocations.get(handle, Money(0)))
    }


def _sum(values: Iterable[Money]) -> Money:
    return sum(values, Money(0))
