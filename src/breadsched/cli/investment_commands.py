"""Commands over investment holdings: cost basis, lots, and gains."""

from __future__ import annotations

import argparse

from ..gen.engine.cost_basis import holdings_cost_basis, shares_text
from ..gen.engine.realized_gains import realized_gains
from ..gen.services import SetSaleLots, sale_lots, set_sale_lots
from ..presentation import (
    holding_cost_text,
    lot_move_text,
    lot_text,
    sale_text,
    service_error_message,
)
from .common import (
    AddCommand,
    CommandError,
    emit,
    find_transaction,
    open_book,
    parse_date,
    resolve_account,
    table,
)


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
                    lines.append(f"  {sale_text(sale)} [{sale.transaction[:8]}]")
                for move in item.moves:
                    other = db.get_account(move.other_account) if move.other_account else None
                    name = db.full_name(other) if other is not None else None
                    lines.append(f"  {lot_move_text(move, name)}")
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


def _mnemonic(db, handle: str | None) -> str:
    commodity = db.get_commodity(handle) if handle else None
    return commodity.mnemonic if commodity is not None else "—"


def cmd_realized_gains(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r")
    try:
        account = resolve_account(db, args.account).handle if args.account else None
        report = realized_gains(db, year=args.year, account=account)
        data = report.as_dict(db)
        lines = []
        for sale in report.sales:
            lines.append(
                f"{sale.account_name}: {sale_text(sale.sale)} [{sale.sale.transaction[:8]}]"
            )
            lines.extend(f"  {lot_text(lot)}" for lot in sale.lots)
        totals = table(
            [
                [
                    str(total.year),
                    _mnemonic(db, total.currency),
                    str(total.sales),
                    total.proceeds.format(),
                    total.cost.format(),
                    total.gain.format(parens_negative=True),
                ]
                for total in report.years
            ],
            ["year", "currency", "sales", "proceeds", "cost", "gain"],
            right={2, 3, 4, 5},
        )
        notes = [f"note: {problem}" for problem in report.problems]
        text = (
            "\n\n".join(part for part in (totals, "\n".join(lines), "\n".join(notes)) if part)
            if report.sales
            else "No sales."
        )
        emit(data, args, text)
        return 0
    finally:
        db.close()


def cmd_sale_lots(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r" if not (args.lot or args.clear) else "w")
    try:
        txn = find_transaction(db, args.transaction)
        sales = [
            split
            for split in txn.splits
            if (args.split is None or split.handle.startswith(args.split))
            and sale_lots(db, txn.handle, split.handle).value is not None
        ]
        if len(sales) != 1:
            raise CommandError(
                "that transaction sells no shares"
                if not sales
                else "that transaction has more than one sale; choose one with --split"
            )
        split = sales[0]
        if args.lot or args.clear:
            picks = []
            for text in args.lot or ():
                lot, separator, shares = text.partition("=")
                if not separator:
                    raise CommandError(f"{text!r} is not LOT=SHARES")
                picks.append((find_transaction(db, lot.strip()).handle, shares.strip()))
            result = set_sale_lots(db, SetSaleLots(txn.handle, split.handle, tuple(picks)))
        else:
            result = sale_lots(db, txn.handle, split.handle)
        if result.value is None:
            raise CommandError("; ".join(service_error_message(error) for error in result.errors))
        chosen = result.value
        payload = {
            "transaction": txn.handle,
            "split": split.handle,
            "account": chosen.account.handle,
            "method": chosen.method,
            "specific": chosen.sale.specific,
            "picks": [
                {"lot": pick.lot, "quantity": shares_text(pick.quantity)} for pick in chosen.picks
            ],
            "offered": [
                {
                    "lot": lot.transaction,
                    "acquired": lot.acquired,
                    "quantity": shares_text(lot.quantity),
                    "cost": lot.cost,
                }
                for lot in chosen.offered
            ],
            "taken": [
                {
                    "lot": lot.transaction,
                    "acquired": lot.acquired,
                    "quantity": shares_text(lot.quantity),
                    "cost": lot.cost,
                }
                for lot in chosen.sale.lots
            ],
            "gain": chosen.sale.gain,
        }
        lines = [
            f"{db.full_name(chosen.account)}: {sale_text(chosen.sale)}",
            "Lots held before the sale:",
            *(f"  [{lot.transaction[:8]}] {lot_text(lot)}" for lot in chosen.offered),
            "Lots sold:",
            *(f"  [{lot.transaction[:8]}] {lot_text(lot)}" for lot in chosen.sale.lots),
        ]
        emit(payload, args, "\n".join(lines))
        return 0
    finally:
        db.close()


def register(add: AddCommand) -> None:
    """Add the investment subcommands."""
    holdings = add(
        "holdings",
        "Each security's shares, cost basis, and gains",
    )
    holdings.add_argument("--as-of", help="holdings as at this date (YYYY-MM-DD)")
    holdings.add_argument(
        "--lots",
        action="store_true",
        help="list open lots, each sale, and each transfer or share split",
    )
    holdings.set_defaults(func=cmd_holdings)

    gains = add("realized-gains", "Realized gains of every sale, with its lots, by year")
    gains.add_argument("--year", type=int, help="only sales in this calendar year")
    gains.add_argument("--account", help="only this security account (name or handle)")
    gains.set_defaults(func=cmd_realized_gains)

    lots = add(
        "sale-lots",
        "Show the lots a sale could sell, or name the lots it sells (specific identification)",
    )
    lots.add_argument("transaction", help="the sale's transaction (handle or unique prefix)")
    lots.add_argument("--split", help="the selling split, when one transaction sells twice")
    lots.add_argument(
        "--lot",
        action="append",
        metavar="LOT=SHARES",
        help="sell SHARES from the lot bought by transaction LOT (repeatable)",
    )
    lots.add_argument(
        "--clear",
        action="store_true",
        help="sell by the account's method again (first-in, first-out or average cost)",
    )
    lots.set_defaults(func=cmd_sale_lots)
