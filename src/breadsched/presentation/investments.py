"""Wording for holdings, cost basis, and lot transfers or share splits."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..gen.lib.money import Money

if TYPE_CHECKING:
    from ..gen.engine.cost_basis import HoldingCostBasis, LotMove


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


def price_text(value: Money) -> str:
    """A price with every digit it has, up to ten places, and at least two: 18.4521."""
    text = format(value.to_decimal(10).normalize(), "f")
    whole, _, fraction = text.partition(".")
    return f"{whole}.{fraction.ljust(2, '0')}"
