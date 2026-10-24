"""HTTP input for the web register's blank entry row and in-place edits (#158).

This adapter only parses the browser's JSON into the shared transaction service's
request and translates the result. Validation and the complete database write stay
in ``gen/services/transactions``, so a rejected request never changes the book.
Details the register row does not show (notes, each split's planning purpose and
investment activity, reconciliation) are carried from the stored transaction, the
same way the GTK row keeps them.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any

from ..gen.engine.cost_basis import shares_text
from ..gen.lib.amount import Amount
from ..gen.lib.money import Money
from ..gen.services import (
    SaveTransaction,
    ToggleCleared,
    TransactionInput,
    TransactionSplitInput,
    transaction_currency,
)
from .controls import input_money

if TYPE_CHECKING:
    from .context import Api


def _text(payload: Mapping[str, Any], key: str, *, optional: bool = False) -> str | None:
    value = payload.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def _quantity(db, account: str, raw: object, source) -> Amount | None:
    """A split's typed shares as an amount of its account's security, or None."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ValueError("quantity must be decimal text")
    obj = db.get_account(account)
    if obj is None or not obj.commodity:
        raise ValueError("quantity is only for a split in an account that holds a security")
    text = raw.strip()
    if source is not None and source.account == account and text == shares_text(source.quantity):
        return Amount(source.quantity, obj.commodity)
    try:
        return Amount(Money(text), obj.commodity)
    except (ValueError, ArithmeticError):
        raise ValueError("quantity must be decimal text") from None


def register_entry_save(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Create a transaction from the blank row, or save an in-place edit.

    ``handle`` names the stored transaction for an edit and is absent for a new
    entry. Each split gives its ``account`` handle, a signed ``value`` in the
    transaction currency (decimal text in the browser's number format, or an
    exact ``[numerator, denominator]`` pair of integer strings), an optional
    ``memo``, and for an edit the stored split ``handle`` it changes. A split in
    an account that holds a security also gives its signed share ``quantity``
    as decimal text (#323); left as the stored split's shares, it keeps them exactly.
    """
    db = api.db
    handle = _text(payload, "handle", optional=True) or None
    existing = db.get_transaction(handle) if handle is not None else None
    raw_date = _text(payload, "date") or ""
    try:
        when = date.fromisoformat(raw_date.strip())
    except ValueError:
        raise ValueError("date must be YYYY-MM-DD") from None
    raw_splits = payload.get("splits")
    if not isinstance(raw_splits, list) or not all(isinstance(item, dict) for item in raw_splits):
        raise ValueError("splits must be a list of objects")
    currency = transaction_currency(db, existing.currency if existing is not None else None)
    sources = {split.handle: split for split in existing.splits} if existing is not None else {}
    splits: list[TransactionSplitInput] = []
    for item in raw_splits:
        account = _text(item, "account") or ""
        split_handle = _text(item, "handle", optional=True) or None
        source = sources.get(split_handle or "")
        raw_value = item.get("value")
        if isinstance(raw_value, list):
            # An exact [numerator, denominator] pair, as the register row sends.
            if (
                len(raw_value) != 2
                or not all(
                    isinstance(part, str) and part.lstrip("-").isdigit() for part in raw_value
                )
                or int(raw_value[1]) <= 0
            ):
                raise ValueError("value must be decimal text or [numerator, denominator]")
        elif not isinstance(raw_value, str):
            raise ValueError("value must be decimal text or [numerator, denominator]")
        value = input_money(dict(payload), raw_value)
        quantity = _quantity(db, account, item.get("quantity"), source)
        splits.append(
            TransactionSplitInput(
                account,
                Amount(value, currency),
                handle=split_handle if existing is not None else None,
                memo=(_text(item, "memo", optional=True) or "").strip(),
                planning_flow=source.planning_flow if source is not None else None,
                investment_activity=source.investment_activity if source is not None else None,
                quantity=quantity,
            )
        )
    request = SaveTransaction(
        TransactionInput(
            post_date=when,
            description=(_text(payload, "description") or "").strip(),
            num=(_text(payload, "num", optional=True) or "").strip(),
            notes=existing.notes if existing is not None else "",
            currency=currency,
            splits=tuple(splits),
        ),
        existing_handle=handle,
        source=existing,
    )
    saved = api.book.save_transaction(request)
    return {"handle": saved.handle, "date": saved.post_date}


def register_cleared_toggle(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """The R column: mark one split cleared or not cleared through the book server."""
    request = ToggleCleared(
        transaction=_text(payload, "transaction") or "",
        split=_text(payload, "split") or "",
    )
    return {"reconcile": api.book.toggle_cleared(request).value}
