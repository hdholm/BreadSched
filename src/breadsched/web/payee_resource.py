"""HTTP input and output for browser payee management and proposal review.

This adapter only parses JSON into the shared payee service's requests and
translates its results. Validation and every write stay in
``gen/services/payees``, so a rejected request never changes the book.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..gen.services.payees import (
    SavePayee,
    apply_payee_proposals,
    delete_payee,
    preview_payee_proposals,
    save_payee,
)

if TYPE_CHECKING:
    from .resources import QueryParams
    from .server import Api


def _text(payload: Mapping[str, Any], key: str, *, optional: bool = False) -> str | None:
    value = payload.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def _texts(payload: Mapping[str, Any], key: str) -> tuple[str, ...] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of text")
    return tuple(value)


def payees(api: Api, query: QueryParams) -> dict[str, object]:
    """Every payee with its transaction count, and the current proposals."""
    query.finish()
    counts: dict[str, int] = {}
    for transaction in api.db.iter_transactions():
        if transaction.payee is not None:
            counts[transaction.payee] = counts.get(transaction.payee, 0) + 1
    proposals = preview_payee_proposals(api.db).value or ()
    return {
        "payees": [
            {
                "handle": payee.handle,
                "name": payee.name,
                "match_keys": list(payee.match_keys),
                "transactions": counts.get(payee.handle, 0),
            }
            for payee in api.db.iter_payees()
        ],
        "proposals": [
            {
                "transaction": item.transaction,
                "date": item.when.isoformat(),
                "description": item.description,
                "payee": item.payee,
                "payee_name": item.payee_name,
                "key": item.key,
            }
            for item in proposals
        ],
    }


def payee_save(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    request = SavePayee(
        name=_text(payload, "name") or "",
        matches=_texts(payload, "matches") or (),
        handle=_text(payload, "handle", optional=True),
    )
    result = save_payee(api.db, request)
    if result.value is None:
        raise api._service_resource_error(result.errors[0])
    return {
        "handle": result.value.handle,
        "name": result.value.name,
        "match_keys": list(result.value.match_keys),
    }


def payee_delete(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    result = delete_payee(api.db, _text(payload, "handle") or "")
    if result.value is None:
        raise api._service_resource_error(result.errors[0])
    return {"cleared": result.value}


def payee_accept(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Accept every current proposal, or only the listed ``transactions``."""
    result = apply_payee_proposals(api.db, _texts(payload, "transactions"))
    if result.value is None:
        raise api._service_resource_error(result.errors[0])
    return {"assigned": result.value.assigned, "unchanged": result.value.unchanged}
