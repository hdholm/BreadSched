"""``find``: transactions across the whole book, sorted, grouped, and totalled (#382)."""

from __future__ import annotations

import argparse
from decimal import InvalidOperation

from ..gen.engine.transaction_query import (
    DIRECTIONS,
    GROUPINGS,
    SORTS,
    QueryResult,
    TransactionQuery,
    run_query,
)
from ..gen.lib import Money
from .common import AddCommand, CommandError, emit, open_book, parse_date, resolve_account, table

__all__ = ["cmd_find", "register", "result_json"]


def _money(text: str | None, flag: str) -> Money | None:
    if text is None:
        return None
    try:
        return Money(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        raise CommandError(f"{flag}: {text!r} is not an amount") from None


def _totals_text(totals: dict[str, Money]) -> str:
    return (
        ", ".join(
            f"{amount.format(parens_negative=True)} {currency}".strip()
            for currency, amount in totals.items()
        )
        or "0.00"
    )


def result_json(result: QueryResult) -> dict[str, object]:
    """The rows, groups, and totals as ``--json`` (and the browser) read them."""

    def row(item) -> dict[str, object]:
        return {
            "transaction": item.transaction.handle,
            "split": item.split.handle,
            "date": item.transaction.post_date,
            "num": item.transaction.num,
            "description": item.transaction.description,
            "account": item.split.account,
            "account_name": item.account_name,
            "memo": item.split.memo,
            "amount": item.value,
            "currency": item.currency,
            "reconcile": item.split.reconcile.value,
            "tags": list(item.transaction.tags),
        }

    return {
        "groups": [
            {"key": group.key, "rows": [row(item) for item in group.rows], "totals": group.totals}
            for group in result.groups
        ],
        "totals": result.totals,
        "splits": len(result.rows),
        "transactions": result.transactions,
    }


def cmd_find(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r")
    try:
        query = TransactionQuery(
            text=args.text or "",
            accounts=tuple(resolve_account(db, name).handle for name in args.account or ()),
            subaccounts=not args.no_subaccounts,
            start=parse_date(args.start),
            end=parse_date(args.end),
            amount=_money(args.amount, "--amount"),
            amount_min=_money(args.min, "--min"),
            amount_max=_money(args.max, "--max"),
            direction=args.direction,
            tags=tuple(args.tag or ()),
            reconcile=tuple(args.reconcile or ()),
            sort=args.sort,
            descending=args.descending,
            group_by=args.group_by,
        )
        try:
            result = run_query(db, query)
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        if args.json:
            emit(result_json(result), args)
            return 0
        grouped = query.group_by != "none"
        for group in result.groups:
            if grouped:
                print(f"{group.key}")
            print(
                table(
                    [
                        [
                            item.transaction.post_date.isoformat(),
                            item.transaction.num[:8],
                            item.transaction.description[:36],
                            item.account_name[:36],
                            item.value.format(parens_negative=True),
                        ]
                        for item in group.rows
                    ],
                    ["date", "num", "description", "account", "amount"],
                    right={4},
                )
            )
            if grouped:
                print(f"  subtotal: {_totals_text(group.totals)}\n")
        print(
            f"{len(result.rows)} split(s) in {result.transactions} transaction(s); "
            f"total {_totals_text(result.totals)}"
        )
        return 0
    finally:
        db.close()


def register(add: AddCommand) -> None:
    """Add the ``find`` subcommand."""
    find = add("find", "Find transactions across the book, with totals and subtotals")
    find.add_argument("--text", help="anything a transaction holds, as the register filter")
    find.add_argument(
        "--account", action="append", help="only splits in this account (repeat for more)"
    )
    find.add_argument(
        "--no-subaccounts", action="store_true", help="leave out the accounts' subaccounts"
    )
    find.add_argument("--start", help="first date (YYYY-MM-DD)")
    find.add_argument("--end", help="last date (YYYY-MM-DD)")
    find.add_argument("--amount", help="a split of exactly this size, either direction")
    find.add_argument("--min", help="splits at least this large")
    find.add_argument("--max", help="splits at most this large")
    find.add_argument(
        "--direction", choices=DIRECTIONS, default="any", help="debit (in) or credit (out)"
    )
    find.add_argument("--tag", action="append", help="only transactions with this tag")
    find.add_argument(
        "--reconcile", action="append", choices=("n", "c", "y"), help="only splits in this state"
    )
    find.add_argument("--sort", choices=SORTS, default="date")
    find.add_argument("--descending", action="store_true")
    find.add_argument("--group-by", choices=GROUPINGS, default="none", help="subtotal groups")
    find.set_defaults(func=cmd_find)
