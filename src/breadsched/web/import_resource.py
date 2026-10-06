"""HTTP input and output for importing a local file and reviewing held changes.

The import itself and every held-change decision go through the shared import
services; this adapter only parses the request and translates the result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..gen.plug import remembered_import_source
from ..gen.services import (
    HeldImportDecision,
    ImportBook,
    ResolveHeldImports,
    import_book,
    pending_import_changes,
    resolve_import_changes,
)
from ..gen.services.claims import claim_link_proposals
from ..gen.services.receivables import reimbursement_proposals
from ..presentation import claim_link_notice, reimbursement_notice
from .controls import service_error

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
    result = import_book(
        api.db,
        ImportBook(
            source=path,
            format=(str(payload["format"]) if payload.get("format") else None),
            include_scheduled=include_scheduled,
            number_format=str(payload.get("number_format") or "auto"),
            date_format=str(payload.get("date_format") or "auto"),
            include_duplicates=include_duplicates,
        ),
    )
    if not result.ok:
        raise service_error(result.errors[0])
    imported = result.value
    assert imported is not None
    return {
        "format": imported.format_name,
        "detail": imported.result.detail(limit=50),
        "held": imported.result.transactions_held,
        "possible_duplicates": imported.result.possible_duplicates,
        "reimbursement_notice": reimbursement_notice(
            len(reimbursement_proposals(api.db).value or ())
        ),
        "claim_link_notice": claim_link_notice(len(claim_link_proposals(api.db).value or ())),
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
            for item in pending_import_changes(api.db)
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
    result = resolve_import_changes(api.db, ResolveHeldImports(tuple(decisions)))
    if not result.ok:
        raise service_error(result.errors[0])
    outcome = result.value
    assert outcome is not None
    return {"kept": outcome.kept, "applied": outcome.applied, "deferred": outcome.deferred}


def import_defaults(api: Api) -> dict:
    """Return per-book presentation state without initiating an import."""
    return {"path": remembered_import_source(api.db) or ""}
