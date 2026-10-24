"""HTTP input and output for browser categorization rules and their proposals.

This adapter only parses JSON into the book server's categorization requests and
translates its results. Validation and every write stay in
``gen/services/categorization``, behind the book server (``Api.book``), so a
rejected request never changes the book.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..gen.services.categorization import AddRule

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
    view = api.book.categorization_rules()
    return {
        "rules": [
            {
                "handle": line.rule.handle,
                "position": line.position,
                "key": line.rule.key,
                "category": line.rule.category,
                "category_name": line.category_name,
            }
            for line in view.rules
        ],
        "proposals": [
            {
                "transaction": line.proposal.transaction,
                "date": line.proposal.when.isoformat(),
                "description": line.proposal.description,
                "amount": str(line.proposal.amount.to_decimal()),
                "category": line.proposal.category,
                "category_name": line.category_name,
                "rule_position": line.proposal.rule_position,
                "conflicts": [
                    {"rule_position": conflict.rule_position, "category_name": name}
                    for conflict, name in zip(
                        line.proposal.conflicts, line.conflict_names, strict=True
                    )
                ],
            }
            for line in view.proposals
        ],
        "categories": [{"handle": item.handle, "name": item.name} for item in view.categories],
    }


def rule_add(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    rule = api.book.add_rule(
        AddRule(
            category=_text(payload, "category") or "",
            description=_text(payload, "description") or "",
            position=_position(payload, "position", optional=True),
        ),
    )
    return {"handle": rule.handle, "key": rule.key}


def rule_delete(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    return {"deleted": api.book.delete_rule(_text(payload, "handle") or "").handle}


def rule_move(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    moved = api.book.move_rule(_text(payload, "handle") or "", _position(payload, "position") or 0)
    return {"moved": moved.handle}


def rules_accept(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Accept every current proposal, or only the listed ``transactions``."""
    raw = payload.get("transactions")
    if raw is not None and (
        not isinstance(raw, list) or not all(isinstance(item, str) for item in raw)
    ):
        raise ValueError("transactions must be a list of text")
    applied = api.book.apply_category_proposals(tuple(raw) if raw is not None else None)
    return {"assigned": applied.assigned, "unchanged": applied.unchanged}
