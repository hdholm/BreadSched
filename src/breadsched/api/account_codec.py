"""The JSON form of an account save (#356, #357).

**Experimental.** ``SaveAccount`` carries a whole account, so its JSON form carries
the account (and the account an edit is based on) in their stored forms, with the
opening balance and its date. Decoding is strict: a value of the wrong type, or an
account the stored form cannot rebuild, is a ``ValueError`` (a 400).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..gen.lib import Account
from ..gen.lib.money import Money
from ..gen.services import SaveAccount
from .contract import to_json

__all__ = ["save_account_from_json", "save_account_json"]

_FIELDS = {"account", "existing_handle", "source", "opening_balance", "opening_date"}


def save_account_json(request: SaveAccount) -> dict[str, Any]:
    """``request`` as JSON data, every field included."""
    return {
        "account": request.account.serialize(),
        "existing_handle": request.existing_handle,
        "source": None if request.source is None else request.source.serialize(),
        "opening_balance": to_json(request.opening_balance),
        "opening_date": to_json(request.opening_date),
    }


def _account(raw: Any, key: str) -> Account:
    if not isinstance(raw, dict):
        raise ValueError(f"{key} must be an object")
    try:
        account = Account.from_dict(raw)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{key} is not a valid account: {exc}") from None
    if not isinstance(account, Account):
        raise ValueError(f"{key} is not a valid account")
    return account


def save_account_from_json(data: Any) -> SaveAccount:
    """A ``SaveAccount`` from untrusted JSON data, or a ``ValueError``."""
    if not isinstance(data, dict):
        raise ValueError("request must be an object")
    unknown = sorted(set(data) - _FIELDS)
    if unknown:
        raise ValueError(f"unknown field: {', '.join(unknown)}")
    existing = data.get("existing_handle")
    if existing is not None and not isinstance(existing, str):
        raise ValueError("existing_handle must be text")
    raw_balance = data.get("opening_balance")
    if raw_balance is not None and not isinstance(raw_balance, str):
        raise ValueError("opening_balance must be a decimal amount in text")
    try:
        balance = None if raw_balance is None else Money(raw_balance)
    except (ArithmeticError, TypeError, ValueError):
        raise ValueError("opening_balance is not an amount") from None
    raw_date = data.get("opening_date")
    if raw_date is not None and not isinstance(raw_date, str):
        raise ValueError("opening_date must be a date (YYYY-MM-DD)")
    try:
        opening_date = None if raw_date is None else date.fromisoformat(raw_date)
    except ValueError:
        raise ValueError("opening_date must be a date (YYYY-MM-DD)") from None
    source = data.get("source")
    return SaveAccount(
        _account(data.get("account"), "account"),
        existing_handle=existing,
        opening_balance=balance,
        opening_date=opening_date,
        source=None if source is None else _account(source, "source"),
    )
