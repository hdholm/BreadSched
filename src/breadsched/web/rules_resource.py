"""HTTP input and output for browser categorization rules and their proposals.

This adapter only parses JSON into the shared categorization service's requests
and translates its results. Validation and every write stay in
``gen/services/categorization``, so a rejected request never changes the book.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..gen.lib.account import AccountClass
from ..gen.services.categorization import (
    AddRule,
    add_rule,
    apply_category_proposals,
    delete_rule,
    list_rules,
    move_rule,
    preview_category_proposals,
)
from .controls import service_error

if TYPE_CHECKING:
    from .context import Api
    from .resources import QueryParams


def _text(payload: Mapping[str, Any], key: str, *, optional: bool = False) -> str | None:
    value = payload.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def _position(payload: Mapping[str, Any], key: str, *, optional: bool = False) -> int | None:
    value = payload.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be a whole number")
    return value


def rules(api: Api, query: QueryParams) -> dict[str, object]:
    """Rules in priority order, current proposals, and the choices a form needs."""
    query.finish()
    db = api.db

    def name(handle: str) -> str:
        return db.full_name(handle) or handle

    payee_names = {payee.handle: payee.name for payee in db.iter_payees()}
    proposals = preview_category_proposals(db).value or ()
    return {
        "rules": [
            {
                "handle": rule.handle,
                "position": position,
                "payee": rule.payee,
                "payee_name": payee_names.get(rule.payee or ""),
                "key": rule.key,
                "category": rule.category,
                "category_name": name(rule.category),
            }
            for position, rule in enumerate(list_rules(db), start=1)
        ],
        "proposals": [
            {
                "transaction": item.transaction,
                "date": item.when.isoformat(),
                "description": item.description,
                "amount": str(item.amount.to_decimal()),
                "category": item.category,
                "category_name": name(item.category),
                "rule_position": item.rule_position,
                "conflicts": [
                    {
                        "rule_position": conflict.rule_position,
                        "category_name": name(conflict.category),
                    }
                    for conflict in item.conflicts
                ],
            }
            for item in proposals
        ],
        "categories": sorted(
            (
                {"handle": account.handle, "name": name(account.handle)}
                for account in db.iter_accounts()
                if not account.placeholder
                and account.account_class in (AccountClass.INCOME, AccountClass.EXPENSE)
            ),
            key=lambda item: item["name"].casefold(),
        ),
        "payees": [{"handle": handle, "name": title} for handle, title in payee_names.items()],
    }


def rule_add(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    result = add_rule(
        api.db,
        AddRule(
            category=_text(payload, "category") or "",
            payee=_text(payload, "payee", optional=True),
            description=_text(payload, "description", optional=True),
            position=_position(payload, "position", optional=True),
        ),
    )
    if result.value is None:
        raise service_error(result.errors[0])
    return {"handle": result.value.handle, "key": result.value.key}


def rule_delete(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    result = delete_rule(api.db, _text(payload, "handle") or "")
    if result.value is None:
        raise service_error(result.errors[0])
    return {"deleted": result.value.handle}


def rule_move(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    result = move_rule(api.db, _text(payload, "handle") or "", _position(payload, "position") or 0)
    if result.value is None:
        raise service_error(result.errors[0])
    return {"moved": result.value.handle}


def rules_accept(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Accept every current proposal, or only the listed ``transactions``."""
    raw = payload.get("transactions")
    if raw is not None and (
        not isinstance(raw, list) or not all(isinstance(item, str) for item in raw)
    ):
        raise ValueError("transactions must be a list of text")
    result = apply_category_proposals(api.db, tuple(raw) if raw is not None else None)
    if result.value is None:
        raise service_error(result.errors[0])
    return {"assigned": result.value.assigned, "unchanged": result.value.unchanged}
