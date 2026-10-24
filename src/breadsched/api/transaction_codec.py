"""The JSON form of the transaction editors' typed request (#356, #357).

**Experimental.** Every editor (GTK's register and transaction editor, the
browser's register and entry form, the command line's ``add`` and ``edit``) saves
through ``SaveTransaction``. This module gives that request a lossless JSON form for
HTTP and decodes it strictly: a value of the wrong type is a ``ValueError`` (a
400). An amount travels as its decimal value and commodity, so a split's value and
its account-commodity quantity stay distinct; the transaction an edit is based on
travels in its stored form.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from ..gen.lib import Transaction
from ..gen.lib.amount import Amount
from ..gen.lib.money import Money
from ..gen.lib.transaction import InvestmentActivityKind, PlanningFlowKind
from ..gen.services import (
    ClaimAttachment,
    SaveTransaction,
    TransactionInput,
    TransactionSplitInput,
)
from .contract import to_json

__all__ = [
    "save_transaction_from_json",
    "save_transaction_json",
    "transaction_input_from_json",
    "transaction_input_json",
]


def _amount(amount: Amount | None) -> dict[str, str] | None:
    if amount is None:
        return None
    return {"value": to_json(amount.value), "commodity": amount.commodity}


def _enum(value: Any) -> str | None:
    return None if value is None else str(value.value)


def transaction_input_json(item: TransactionInput) -> dict[str, Any]:
    """An editor's transaction fields as JSON data."""
    return {
        "post_date": item.post_date.isoformat(),
        "description": item.description,
        "num": item.num,
        "notes": item.notes,
        "currency": item.currency,
        "investment_activity": _enum(item.investment_activity),
        "tags": None if item.tags is None else list(item.tags),
        "splits": [
            {
                "account": split.account,
                "value": _amount(split.value),
                "quantity": _amount(split.quantity),
                "handle": split.handle,
                "memo": split.memo,
                "planning_flow": _enum(split.planning_flow),
                "investment_activity": _enum(split.investment_activity),
            }
            for split in item.splits
        ],
    }


def save_transaction_json(request: SaveTransaction) -> dict[str, Any]:
    """``request`` as JSON data, every field included."""
    claim = request.claim_attachment
    return {
        "existing_handle": request.existing_handle,
        "source": None if request.source is None else request.source.serialize(),
        "claim_attachment": None
        if claim is None
        else {
            "claim": claim.claim,
            "role": claim.role,
            "funding_year_start": to_json(claim.funding_year_start),
        },
        "definition": transaction_input_json(request.definition),
    }


# ------------------------------------------------------------------ decoding


def _object(data: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{name} must be an object")
    return data


def _text(data: Mapping[str, Any], key: str, *, optional: bool = False) -> Any:
    value = data.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def _date(raw: Any, key: str) -> date:
    if not isinstance(raw, str):
        raise ValueError(f"{key} must be a date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"{key} must be a date (YYYY-MM-DD)") from None


def _amount_from(raw: Any, key: str, *, optional: bool = False) -> Amount | None:
    if raw is None and optional:
        return None
    item = _object(raw, key)
    value = item.get("value")
    if not isinstance(value, str):
        raise ValueError(f"{key} value must be a decimal amount in text")
    try:
        return Amount(Money(value), _text(item, "commodity"))
    except (ArithmeticError, TypeError, ValueError):
        raise ValueError(f"{key} is not an amount") from None


def _choice(kind: Any, raw: Any, key: str) -> Any:
    if raw is None:
        return None
    try:
        return kind(raw)
    except ValueError:
        raise ValueError(f"{key} is not one of the known values") from None


def _split(raw: Any) -> TransactionSplitInput:
    item = _object(raw, "split")
    value = _amount_from(item.get("value"), "value")
    assert value is not None
    return TransactionSplitInput(
        account=_text(item, "account"),
        value=value,
        quantity=_amount_from(item.get("quantity"), "quantity", optional=True),
        handle=_text(item, "handle", optional=True),
        memo=_text(item, "memo"),
        planning_flow=_choice(PlanningFlowKind, item.get("planning_flow"), "planning_flow"),
        investment_activity=_choice(
            InvestmentActivityKind, item.get("investment_activity"), "investment_activity"
        ),
    )


def transaction_input_from_json(data: Any) -> TransactionInput:
    """An editor's transaction fields from untrusted JSON data, or a ``ValueError``."""
    item = _object(data, "definition")
    splits = item.get("splits")
    if not isinstance(splits, list):
        raise ValueError("splits must be a list")
    tags = item.get("tags")
    if tags is not None and (
        not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags)
    ):
        raise ValueError("tags must be a list of text")
    return TransactionInput(
        post_date=_date(item.get("post_date"), "post_date"),
        description=_text(item, "description"),
        splits=tuple(_split(split) for split in splits),
        num=_text(item, "num"),
        notes=_text(item, "notes"),
        currency=_text(item, "currency", optional=True),
        investment_activity=_choice(
            InvestmentActivityKind, item.get("investment_activity"), "investment_activity"
        ),
        tags=None if tags is None else tuple(tags),
    )


def save_transaction_from_json(data: Any) -> SaveTransaction:
    """A ``SaveTransaction`` from untrusted JSON data, or a ``ValueError``."""
    outer = _object(data, "request")
    definition = transaction_input_from_json(outer.get("definition"))
    claim = outer.get("claim_attachment")
    attachment = None
    if claim is not None:
        claim_item = _object(claim, "claim_attachment")
        start = claim_item.get("funding_year_start")
        attachment = ClaimAttachment(
            _text(claim_item, "claim"),
            _text(claim_item, "role"),
            None if start is None else _date(start, "funding_year_start"),
        )
    raw_source = outer.get("source")
    source = None
    if raw_source is not None:
        try:
            source = Transaction.from_dict(dict(_object(raw_source, "source")))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"source is not valid: {exc}") from None
    return SaveTransaction(
        definition,
        existing_handle=_text(outer, "existing_handle", optional=True),
        claim_attachment=attachment,
        source=source,
    )
