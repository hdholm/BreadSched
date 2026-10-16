"""HTTP output for reading register typing: dates, amounts, accounts, check numbers.

The browser register sends what was typed and shows what comes back, so a
shortcut or an arithmetic amount means the same thing as in the desktop
register. Every rule stays in ``gen/services/entry_input``; nothing here writes.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Literal, cast

from ..gen.services.entry_input import (
    complete_entry_account,
    next_entry_num,
    read_entry_amount,
    read_entry_date,
    read_entry_trade,
)
from .controls import service_error

if TYPE_CHECKING:
    from .context import Api
    from .resources import QueryParams

_FORMATS = ("auto", "dot", "comma")


def entry_date(api: Api, query: QueryParams) -> dict[str, object]:
    text = query.text("text") or ""
    base = query.iso_date("base") or date.today()
    query.finish()
    result = read_entry_date(text, base)
    if result.value is None:
        raise service_error(result.errors[0])
    return {"date": result.value.isoformat()}


def entry_amount(api: Api, query: QueryParams) -> dict[str, object]:
    text = query.text("text") or ""
    currency = query.text("currency")
    number_format = query.choice("number_format", _FORMATS, "auto")
    query.finish()
    result = read_entry_amount(
        api.db,
        text,
        currency=currency,
        number_format=cast(Literal["auto", "dot", "comma"], number_format),
    )
    if result.value is None:
        raise service_error(result.errors[0])
    value = result.value.value
    return {"amount": None if value is None else str(value)}


def entry_trade(api: Api, query: QueryParams) -> dict[str, object]:
    """Shares, price, and amount of a trade with the third worked out (#323)."""
    account = query.text("account", required=True) or ""
    shares = query.text("shares") or ""
    price = query.text("price") or ""
    amount = query.text("amount") or ""
    changed = query.choice("changed", ("shares", "price", "amount"), "amount")
    currency = query.text("currency")
    number_format = query.choice("number_format", _FORMATS, "auto")
    query.finish()
    result = read_entry_trade(
        api.db,
        account,
        shares=shares,
        price=price,
        amount=amount,
        changed=cast(Literal["shares", "price", "amount"], changed),
        currency=currency,
        number_format=cast(Literal["auto", "dot", "comma"], number_format),
    )
    if result.value is None:
        raise service_error(result.errors[0])
    filled = result.value
    return {
        "shares": None if filled.shares is None else f"{filled.shares:f}",
        "price": None if filled.price is None else f"{filled.price:f}",
        "amount": None if filled.amount is None else f"{filled.amount:f}",
    }


def entry_accounts(api: Api, query: QueryParams) -> dict[str, object]:
    text = query.text("text") or ""
    exclude = query.text("exclude")
    limit = query.integer("limit", default=20, minimum=1, maximum=200)
    query.finish()
    result = complete_entry_account(api.db, text, exclude=exclude, limit=limit or 20)
    assert result.value is not None
    return {
        "accounts": [
            {"handle": account.handle, "full_name": account.full_name} for account in result.value
        ]
    }


def entry_num(api: Api, query: QueryParams) -> dict[str, object]:
    account = query.text("account", required=True) or ""
    text = query.text("text") or ""
    step = query.integer("step", default=1, minimum=-1000, maximum=1000)
    query.finish()
    result = next_entry_num(api.db, account, text, step or 0)
    if result.value is None:
        raise service_error(result.errors[0])
    return {"num": result.value}
