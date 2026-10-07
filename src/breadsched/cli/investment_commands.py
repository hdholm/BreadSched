"""Commands over investment holdings: cost basis, lots, and gains."""

from __future__ import annotations

import argparse

from ..gen.engine.cost_basis import holdings_cost_basis, shares_text
from ..presentation import holding_cost_text
from .common import AddCommand, emit, open_book, parse_date, table


def cmd_holdings(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r")
    try:
        holdings = holdings_cost_basis(db, as_of=parse_date(args.as_of))
        rows = [
            {
                **item.as_dict(),
                "name": db.full_name(item.account),
                "text": holding_cost_text(item),
            }
            for item in holdings
        ]
        lines = []
        for item in holdings:
            lines.append(f"{db.full_name(item.account)}: {holding_cost_text(item)}")
            if args.lots:
                for lot in item.lots:
                    lines.append(
                        f"  bought {lot.acquired.isoformat()}: {shares_text(lot.quantity)} "
                        f"shares, cost {lot.cost.format()}"
                    )
                for sale in item.sales:
                    lines.append(
                        f"  sold {sale.sold.isoformat()}: {shares_text(sale.quantity)} "
                        f"shares for {sale.proceeds.format()}, cost {sale.cost.format()}, "
                        f"gain {sale.gain.format(parens_negative=True)}"
                    )
            lines.extend(f"  note: {problem}" for problem in item.problems)
        summary = table(
            [
                [
                    db.full_name(item.account),
                    shares_text(item.quantity),
                    item.cost.format(),
                    item.market_value.format() if item.market_value is not None else "—",
                    item.unrealized_gain.format(parens_negative=True)
                    if item.unrealized_gain is not None
                    else "—",
                ]
                for item in holdings
            ],
            ["holding", "shares", "cost", "market value", "unrealized"],
            right={1, 2, 3, 4},
        )
        emit(
            rows,
            args,
            summary + "\n\n" + "\n".join(lines) if holdings else "No security holdings.",
        )
        return 0
    finally:
        db.close()


def register(add: AddCommand) -> None:
    """Add the investment subcommands."""
    holdings = add(
        "holdings",
        "Each security's shares, cost basis (first in, first out), and gains",
    )
    holdings.add_argument("--as-of", help="holdings as at this date (YYYY-MM-DD)")
    holdings.add_argument("--lots", action="store_true", help="list open lots and each sale")
    holdings.set_defaults(func=cmd_holdings)
