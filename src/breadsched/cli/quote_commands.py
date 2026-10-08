"""Commands for online quotes: fetch them, and choose where each commodity's come from."""

from __future__ import annotations

import argparse

from ..gen.services.quotes import SetQuoteSource, quote_requests, set_quote_source, update_quotes
from ..gen.utils.settings import Settings
from ..plugins.quotes import OnlineQuotes, alphavantage_key, finance_quote_status
from ..presentation import price_text, service_error_message
from .common import AddCommand, CommandError, emit, open_book, table


def _fetcher() -> OnlineQuotes:
    return OnlineQuotes(alphavantage_key=alphavantage_key(Settings()) or None)


def cmd_quotes(args: argparse.Namespace) -> int:
    db = open_book(args.book, "r" if args.dry_run else "w")
    try:
        if args.list:
            requests = quote_requests(db)
            available, reason = finance_quote_status()
            listed = [{"symbol": item.symbol, "source": item.source} for item in requests]
            text = (
                table([[item.symbol, item.source] for item in requests], ["symbol", "source"])
                if requests
                else "No commodity has an online quote source."
            )
            emit(
                {"requests": listed, "finance_quote": {"available": available, "reason": reason}},
                args,
                f"{text}\n\n{reason}.",
            )
            return 0
        result = update_quotes(db, _fetcher(), dry_run=args.dry_run)
        if result.value is None:
            raise CommandError(service_error_message(result.errors[0]))
        update = result.value
        rows = [
            [
                item.symbol,
                price_text(item.value),
                item.currency,
                item.when.isoformat(),
                item.source,
                "stored"
                if item.changed and not update.dry_run
                else ("would store" if item.changed else "unchanged"),
            ]  # fmt: skip
            for item in update.stored
        ]
        lines = []
        if rows:
            lines.append(table(rows, ["symbol", "price", "in", "date", "source", ""], right={1}))
        lines.extend(f"{item.symbol} ({item.source}): {item.reason}" for item in update.failures)
        emit(
            {
                "dry_run": update.dry_run,
                "stored": [
                    {
                        "symbol": item.symbol,
                        "price": price_text(item.value),
                        "currency": item.currency,
                        "date": item.when.isoformat(),
                        "source": item.source,
                        "changed": item.changed,
                    }
                    for item in update.stored
                ],
                "failures": [
                    {"symbol": item.symbol, "source": item.source, "reason": item.reason}
                    for item in update.failures
                ],
            },
            args,
            "\n".join(lines) or "No commodity has an online quote source.",
        )
        return 1 if update.failures and not update.stored else 0
    finally:
        db.close()


def cmd_quote_source(args: argparse.Namespace) -> int:
    db = open_book(args.book)
    try:
        wanted = args.commodity.strip().upper()
        matches = [item for item in db.iter_commodities() if item.mnemonic.upper() == wanted]
        if not matches:
            raise CommandError(f"no commodity {args.commodity!r}")
        if len(matches) > 1:
            raise CommandError(f"{args.commodity!r} names {len(matches)} commodities")
        result = set_quote_source(db, SetQuoteSource(matches[0].handle, args.source or ""))
        if result.value is None:
            raise CommandError(service_error_message(result.errors[0]))
        emit(
            {"commodity": matches[0].mnemonic, "source": result.value},
            args,
            f"{matches[0].mnemonic}: "
            + (f"online quotes from {result.value}" if result.value else "no online quotes"),
        )
        return 0
    finally:
        db.close()


def register(add: AddCommand) -> None:
    """Add the online quote subcommands."""
    quotes = add("quotes", "Fetch online quotes for every commodity that has a quote source")
    quotes.add_argument(
        "--dry-run", action="store_true", help="fetch and show the quotes without storing them"
    )
    quotes.add_argument(
        "--list", action="store_true", help="list the commodities and their sources only"
    )
    quotes.set_defaults(func=cmd_quotes)
    source = add(
        "quote-source",
        "Choose where a commodity's online quotes come from (tsp, alphavantage, currency, "
        "or a Finance::Quote method); leave it out to stop online quotes",
    )
    source.add_argument("commodity", help="the commodity's symbol, such as G or VTSAX")
    source.add_argument("source", nargs="?", default="", help="the quote source")
    source.set_defaults(func=cmd_quote_source)
