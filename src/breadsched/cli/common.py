"""Helpers every command module shares: errors, dates, output, and book lookups."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

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
