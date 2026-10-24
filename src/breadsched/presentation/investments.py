"""Wording for holdings, cost basis, lots, sales, and lot transfers or share splits."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from ..gen.lib.money import Money

if TYPE_CHECKING:
    from ..gen.engine.cost_basis import HoldingCostBasis, Lot, LotMove, RealizedGain


def holding_cost_text(holding: HoldingCostBasis) -> str:
    """One holding's shares, cost basis, market value, and gains, in plain words."""
    from ..gen.engine.cost_basis import shares_text

    basis = " at average cost" if holding.method == "average" else ""
    parts = [f"{shares_text(holding.quantity)} shares cost {holding.cost.format()}{basis}"]
    market = holding.market_value
    gain = holding.unrealized_gain
    if market is not None and gain is not None:
        parts.append(f"worth {market.format()}")
        parts.append(f"unrealized {'gain' if gain >= 0 else 'loss'} {abs(gain).format()}")
    realized = holding.realized_by_year()
    if realized:
        parts.append(
            "realized "
            + ", ".join(
                f"{year}: {amount.format(parens_negative=True)}"
                for year, amount in realized.items()
            )
        )
    return "; ".join(parts) + "."


def lot_move_text(move: LotMove, other_name: str | None = None) -> str:
    """A transfer of shares or a share split, in plain words (lower case, for a list)."""
    from ..gen.engine.cost_basis import shares_text

    day = move.when.isoformat()
    if move.kind == "split":
        held = move.held if move.held is not None else Money(0)
        return (
            f"share split {day}: {shares_text(held)} shares became "
            f"{shares_text(held + move.quantity)}"
        )
    other = other_name or "another account"
    shares = shares_text(abs(move.quantity))
    if move.kind == "transfer_out":
        return f"moved out {day} to {other}: {shares} shares, cost {move.cost.format()}"
    return f"moved in {day} from {other}: {shares} shares, cost {move.cost.format()}"


def lot_text(lot: Lot) -> str:
    """One lot or lot part: when it was bought, how many shares, and their cost."""
    from ..gen.engine.cost_basis import shares_text

    return (
        f"bought {lot.acquired.isoformat()}: {shares_text(lot.quantity)} shares, "
        f"cost {lot.cost.format()}"
    )


def sale_text(sale: RealizedGain) -> str:
    """One sale and how its lots were chosen (lower case, for a list)."""
    from ..gen.engine.cost_basis import shares_text

    chosen = "named lots" if sale.specific else "the account's method"
    return (
        f"sold {sale.sold.isoformat()}: {shares_text(sale.quantity)} shares for "
        f"{sale.proceeds.format()}, cost {sale.cost.format()}, gain "
        f"{sale.gain.format(parens_negative=True)} ({chosen})"
    )


def price_text(value: Money) -> str:
    """A price with every digit it has, up to ten places, and at least two: 18.4521."""
    text = format(value.to_decimal(10).normalize(), "f")
    whole, _, fraction = text.partition(".")
    return f"{whole}.{fraction.ljust(2, '0')}"


def trade_price_text(price: Decimal | None) -> str:
    """A register's price cell: every digit the price has, and at least two."""
    if price is None:
        return ""
    whole, _, fraction = f"{price:f}".partition(".")
    return f"{whole}.{fraction.ljust(2, '0')}"


def register_share_summary(db, account) -> str | None:
    """The shares a security account holds and what they are worth, for its register.

    The market value names its quote's date and source; without a quote it says
    so and shows the ledger amount instead, never an unquoted conversion (#323).
    ``None`` for an account kept in a currency.
    """
    from ..gen.engine import ledger, valuation

    security = ledger.security_of(db, account)
    if security is None:
        return None
    valued = valuation.account_value(db, account)
    return share_summary_text(
        security.mnemonic,
        valuation.quantity_balance(db, account),
        valued,
        valuation.quote_evidence(db, valued),
    )


def share_summary_text(mnemonic: str, held, valued, evidence: str) -> str:
    """:func:`register_share_summary` from its parts (a register view's holding)."""
    from ..gen.engine.cost_basis import shares_text

    text = f"{shares_text(held)} {mnemonic} shares"
    if valued.source == "market":
        return f"{text} · worth {valued.total.format(parens_negative=True)} · {evidence}"
    return f"{text} · no quote; ledger amount {valued.total.format(parens_negative=True)}"
