"""Read register typing against an open book: accounts, check numbers, amounts, dates.

The rules are the pure ones in ``engine/entry_input``; this service adds what only
the book knows: which accounts may take a new split, the register's last check
number, and the currency's smallest unit. GTK calls it directly and the web
through ``GET /api/entry/*``, so both registers read the same text the same way.
Nothing here writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from ..db.sqlite import DbSQLite
from ..engine.currency import reporting_currency_handle
from ..engine.entry_input import (
    EntryInputError,
    complete_account,
    parse_entry_amount,
    parse_entry_date,
    step_num,
)
from ..utils.amount_input import NumberFormat
from .contracts import ServiceError, ServiceResult

__all__ = [
    "EntryAccount",
    "EntryAmount",
    "complete_entry_account",
    "latest_num",
    "next_entry_num",
    "read_entry_amount",
    "read_entry_date",
]

#: Completion offers at most this many accounts.
COMPLETION_LIMIT = 20


@dataclass(frozen=True, slots=True)
class EntryAmount:
    #: ``None`` for a blank field.
    value: Decimal | None


@dataclass(frozen=True, slots=True)
class EntryAccount:
    handle: str
    full_name: str


def complete_entry_account(
    db: DbSQLite, text: str, *, exclude: str | None = None, limit: int = COMPLETION_LIMIT
) -> ServiceResult[tuple[EntryAccount, ...]]:
    """Visible, postable accounts ``text`` completes to (``Ex:Gr`` → Expenses:Groceries)."""
    names: dict[str, str] = {}
    for account in db.iter_accounts():
        if account.hidden or account.placeholder or account.handle == exclude:
            continue
        if account.parent is None:
            continue  # the root is never a split's account
        names.setdefault(db.full_name(account), account.handle)
    matches = complete_account(text, names)[: max(0, limit)]
    return ServiceResult.success(tuple(EntryAccount(names[name], name) for name in matches))


def latest_num(db: DbSQLite, account: str) -> str:
    """The number on the register's most recent numbered transaction, or ``""``."""
    latest = ""
    for transaction in db.iter_transactions(account):
        if transaction.num.strip():
            latest = transaction.num.strip()
    return latest


def next_entry_num(db: DbSQLite, account: str, text: str, step: int) -> ServiceResult[str]:
    """``text`` moved ``step`` numbers on; an empty field continues the register."""
    if db.get_account(account) is None:
        return ServiceResult.failure(ServiceError("entry.account.not_found", ("account",)))
    latest = latest_num(db, account) if not text.strip() else ""
    return ServiceResult.success(step_num(text, step, latest))


def read_entry_amount(
    db: DbSQLite,
    text: str,
    *,
    currency: str | None = None,
    number_format: NumberFormat | Literal["auto"] = "auto",
) -> ServiceResult[EntryAmount]:
    """An amount, arithmetic evaluated and rounded to the currency's smallest unit."""
    commodity = db.get_commodity(currency or reporting_currency_handle(db))
    if commodity is None:
        return ServiceResult.failure(ServiceError("entry.currency.not_found", ("currency",)))
    try:
        value = parse_entry_amount(text, number_format, commodity.fraction)
    except EntryInputError:
        return ServiceResult.failure(ServiceError("entry.amount.invalid", ("text",)))
    return ServiceResult.success(EntryAmount(value))


def read_entry_date(text: str, base: date, today: date | None = None) -> ServiceResult[date]:
    """A date typed or keyed relative to ``base``, the date the field held."""
    try:
        return ServiceResult.success(parse_entry_date(text, base, today))
    except EntryInputError:
        return ServiceResult.failure(ServiceError("entry.date.invalid", ("text",)))
