"""Plan expense recognition for restricted escrow assets.

Ledger balances and split values are never changed. Funding is a planning expense;
the expense leg of an escrow payout has already been planned at funding time.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..lib.account import Account, AccountClass, AccountKind
from ..lib.money import Money

__all__ = ["recognition"]


def recognition(
    legs: Iterable[tuple[str, Money]], accounts: Mapping[str, Account]
) -> tuple[Money, dict[str, Money]]:
    """Return escrow funding and expense-account amounts already covered by draws.

    Allocate a partial escrow payout across positive expense legs proportionally;
    ledger refunds (negative expense legs) remain visible as reversals. Escrow-to-
    escrow transfers are neutral because only the net escrow movement is counted.
    """
    net_escrow = Money(0)
    expenses: dict[str, Money] = {}
    for handle, amount in legs:
        account = accounts.get(handle)
        if account is None:
            continue
        if account.kind is AccountKind.ESCROW:
            net_escrow = net_escrow + amount
        elif account.account_class is AccountClass.EXPENSE and amount > 0:
            expenses[handle] = expenses.get(handle, Money(0)) + amount
    if net_escrow >= 0:
        return net_escrow, {}
    total = sum(expenses.values(), Money(0))
    if total <= 0:
        return Money(0), {}
    covered = min(-net_escrow, total)
    return Money(0), {handle: covered * (amount / total) for handle, amount in expenses.items()}
