"""Helpers every command module shares: errors, dates, output, and book lookups."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..api.connect import open_book_api
from ..api.http_client import HttpBookClient
from ..api.protocol import BookApi
from ..gen.db.sqlite import DbSQLite
from ..gen.engine.completeness import Completeness
from ..gen.lib import (
    Account,
    Money,
    Rate,
    Transaction,
)

#: The ``add(name, help_text, needs_book=True)`` function ``build_parser`` passes to
#: each command module's ``register``; it returns the new subcommand's parser.
AddCommand = Callable[..., argparse.ArgumentParser]


class CommandError(Exception):
    """A problem the user can fix, reported without a traceback."""


def boolean(text: str) -> bool:
    return str(text).strip().lower() in ("1", "true", "yes", "y", "on")


def parse_date(text: str | None) -> date | None:
    if not text:
        return None
    if text == "today":
        return date.today()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise CommandError(f"{text!r} is not a date; use YYYY-MM-DD") from exc


def emit(payload: Any, args: argparse.Namespace, text: str | None = None) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, default=encode))
    elif text is not None:
        print(text)


def encode(value: Any) -> Any:
    if isinstance(value, Money):
        return str(value.to_decimal())
    if isinstance(value, Rate):
        return str(value.decimal)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Completeness):
        return value.as_dict()
    raise TypeError(f"cannot serialise {type(value).__name__}")


def table(
    rows: Sequence[Sequence[Any]],
    headers: Sequence[str],
    right: frozenset[int] | set[int] | None = None,
) -> str:
    """Render a fixed-width table.  No dependencies, aligned numeric columns."""
    right = right or frozenset()
    body = [[str(cell) for cell in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in body:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def line(cells: Sequence[str]) -> str:
        return "  ".join(
            cell.rjust(widths[i]) if i in right else cell.ljust(widths[i])
            for i, cell in enumerate(cells)
        ).rstrip()

    out = [line(headers), "  ".join("-" * w for w in widths)]
    out.extend(line(row) for row in body)
    return "\n".join(out)


def open_book(path: str, mode: str = "w") -> DbSQLite:
    if mode == "r" and not Path(path).exists():
        raise CommandError(f"no book at {path}")
    db = DbSQLite()
    db.load(path, mode=mode)
    return db


def resolve_account(db: DbSQLite, reference: str) -> Account:
    account = db.get_account(reference) or db.get_account_by_name(reference)
    if account is None:
        raise CommandError(f"no account matches {reference!r}")
    return account


def resolve_currency(db: DbSQLite, reference: str) -> str:
    """Accept an exact handle or an unambiguous currency mnemonic."""
    direct = db.get_commodity(reference)
    if direct is not None:
        if direct.is_currency:
            return direct.handle
        raise CommandError(f"{reference!r} is not a currency")
    matches = [
        item
        for item in db.iter_commodities()
        if item.is_currency and item.mnemonic.upper() == reference.upper()
    ]
    if len(matches) != 1:
        reason = "ambiguous currency" if matches else "no currency matches"
        raise CommandError(f"{reason} {reference!r}; use an exact currency handle")
    return matches[0].handle


def find_transaction(db: DbSQLite, reference: str) -> Transaction:
    """Locate a transaction by handle, or by a unique prefix of one."""
    exact = db.get_transaction(reference)
    if exact is not None:
        return exact
    matches = [t for t in db.iter_transactions() if t.handle.startswith(reference)]
    if not matches:
        raise CommandError(f"no transaction matches {reference!r}")
    if len(matches) > 1:
        raise CommandError(
            f"{reference!r} matches {len(matches)} transactions; use more characters"
        )
    return matches[0]


#: Environment variable holding the token for ``--server``.
SERVER_TOKEN_ENV = "BREADSCHED_SERVER_TOKEN"


def add_server_options(parser: argparse.ArgumentParser) -> None:
    """``--server`` and ``--token-file`` for commands that use the book server (#358)."""
    parser.add_argument(
        "--server",
        metavar="URL",
        help="use the book server at URL (default: the one holding this book, if any)",
    )
    parser.add_argument(
        "--token-file",
        metavar="PATH",
        help=f"file holding the --server token (default: ${SERVER_TOKEN_ENV})",
    )
    parser.add_argument(
        "--book-id",
        metavar="ID",
        help="the book to use on a --server that holds several (GET /api/v1/books lists them)",
    )
    parser.add_argument(
        "--ca-file",
        metavar="PATH",
        help="certificate authority for an https:// --server (default: the system's)",
    )
    parser.add_argument(
        "--client-cert",
        metavar="PATH",
        help="client certificate (PEM) for an https:// --server that asks for one",
    )
    parser.add_argument(
        "--client-key", metavar="PATH", help="the client certificate's key, if kept apart"
    )


def book_api(args: argparse.Namespace, mode: str) -> AbstractContextManager[BookApi]:
    """The book API for ``args.book``: ``--server``, the server holding it, or here."""
    url = getattr(args, "server", None)
    if url is None:
        return open_book_api(args.book, mode)
    path = getattr(args, "token_file", None)
    if path:
        try:
            token = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CommandError(f"could not read the token file: {exc}") from exc
    else:
        token = os.environ.get(SERVER_TOKEN_ENV, "")
    if not token:
        raise CommandError(f"--server needs a token: --token-file or ${SERVER_TOKEN_ENV}")
    try:
        client = HttpBookClient(
            url,
            token,
            ca_file=getattr(args, "ca_file", None),
            client_cert=getattr(args, "client_cert", None),
            client_key=getattr(args, "client_key", None),
            book_id=getattr(args, "book_id", None),
        )
    except (OSError, ValueError) as exc:
        raise CommandError(f"could not use --server {url}: {exc}") from exc
    return nullcontext(client)
