"""HTTP input and output for importing a local file and reviewing held changes.

The import itself and every held-change decision go through the shared import
services; this adapter only parses the request and translates the result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..gen.services import (
    HeldImportDecision,
    ImportBook,
    ResolveHeldImports,
)
from ..presentation import claim_link_notice, reimbursement_notice

if TYPE_CHECKING:
    from .context import Api


def import_local(api: Api, payload: dict) -> dict:
    path = str(payload.get("path") or "").strip()
    include_scheduled = payload.get("include_scheduled", True)
    if not isinstance(include_scheduled, bool):
        raise ValueError("include_scheduled must be true or false")
    include_duplicates = payload.get("include_duplicates", False)
    if not isinstance(include_duplicates, bool):
        raise ValueError("include_duplicates must be true or false")
    imported = api.book.import_book(
        ImportBook(
            source=path,
            format=(str(payload["format"]) if payload.get("format") else None),
            include_scheduled=include_scheduled,
            number_format=str(payload.get("number_format") or "auto"),
            date_format=str(payload.get("date_format") or "auto"),
            include_duplicates=include_duplicates,
        ),
    )
    return {
        "format": imported.format_name,
        "detail": imported.result.detail(limit=50),
        "problems": [problem.as_dict() for problem in imported.result.problems()],
        "held": imported.result.transactions_held,
        "possible_duplicates": imported.result.possible_duplicates,
        "reimbursement_notice": reimbursement_notice(len(api.book.reimbursement_proposals())),
        "claim_link_notice": claim_link_notice(len(api.book.claim_link_proposals())),
    }


def import_review(api: Api) -> dict:
    """GnuCash changes held back from transactions reconciled in BreadSched."""
    return {
        "changes": [
            {
                "transaction": item.transaction,
                "date": item.post_date,
                "description": item.description,
                "source": item.source,
                "detected": item.detected,
                "changes": list(item.changes),
                "blocked_by": list(item.blocked_by),
                "can_use_gnucash": item.can_use_source,
                "deleted": item.deleted,
            }
            for item in api.book.held_imports()
        ]
    }


def import_review_resolve(api: Api, payload: dict) -> dict:
    raw = payload.get("decisions")
    if not isinstance(raw, list):
        raise ValueError("decisions must be a list")
    decisions: list[tuple[str, HeldImportDecision]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each decision must be an object")
        decisions.append(
            (str(item.get("transaction", "")), HeldImportDecision(str(item.get("decision"))))
        )
    outcome = api.book.resolve_held_imports(ResolveHeldImports(tuple(decisions)))
    return {"kept": outcome.kept, "applied": outcome.applied, "deferred": outcome.deferred}


def import_defaults(api: Api) -> dict:
    """Return per-book presentation state without initiating an import."""
    return {"path": api.book.import_source() or ""}
