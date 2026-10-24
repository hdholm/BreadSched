"""HTTP input and output for FSA claims.

This adapter only parses JSON into the shared claim service's requests and
translates its results. Validation, the claim arithmetic in
``gen/engine/fsa_claims``, and every write stay in ``gen/services/claims``, so a
rejected request never changes the stored claim. On the experimental book-server
branch (#354) every read and write goes through the book server, ``api.book``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any

from ..gen.lib.fsa_claim import FsaClaimEvent
from ..gen.services import (
    ClaimAllocationInput,
    ClaimInput,
    ClaimLinkInput,
    ClaimRejectionInput,
    CloseClaim,
    ReopenClaim,
    SaveClaim,
)
from ..gen.services.claims import ClaimCandidate, ClaimCandidates
from .controls import input_money
from .receivable_resource import shared_cost_json

if TYPE_CHECKING:
    from .context import Api
    from .resources import QueryParams


def _decimal(value) -> str:
    return str(value.to_decimal())


def _event(event: FsaClaimEvent) -> dict[str, object]:
    return {
        "kind": event.kind,
        "on": event.on.isoformat(),
        "previous": _decimal(event.previous) if event.previous is not None else None,
        "current": _decimal(event.current) if event.current is not None else None,
        "note": event.note,
    }


def fsa_claims(api: Api, query: QueryParams) -> dict[str, object]:
    """Every claim's stored links and recomputed standing, plus linkable splits."""
    query.finish()
    view = api.book.fsa_claims()
    rows = []
    for summary in view.claims:
        claim = summary.claim
        rows.append(
            {
                "handle": claim.handle,
                "service_date": claim.service_date.isoformat(),
                "provider": claim.provider,
                "description": claim.description,
                "eob_responsibility": (
                    _decimal(claim.eob_responsibility)
                    if claim.eob_responsibility is not None
                    else None
                ),
                "paid": _decimal(summary.paid),
                "provider_refunds": _decimal(summary.refunds),
                "net_paid": _decimal(summary.net_paid),
                "reimbursed": _decimal(summary.reimbursed),
                "repaid": _decimal(summary.repaid),
                "over_reimbursed": _decimal(summary.over_reimbursed),
                "rejected": _decimal(summary.rejected),
                "remaining": _decimal(summary.remaining_reimbursable),
                "forgone": _decimal(summary.forgone),
                "status": summary.status.value,
                "status_label": summary.status.label,
                "closed_on": claim.closed_on.isoformat() if claim.closed_on else None,
                "close_reason": claim.close_reason,
                "events": [_event(event) for event in claim.events],
                "payments": [link.serialize() for link in claim.payments],
                "refunds": [link.serialize() for link in claim.refunds],
                "receivable": claim.receivable,
                "shared": (
                    shared_cost_json(summary.shared) if summary.shared is not None else None
                ),
                "allocations": [
                    {**allocation.serialize(), "account_name": view.name(allocation.account)}
                    for allocation in claim.allocations
                ],
            }
        )
    return {"claims": rows, "candidates": _candidates(view.candidates)}


def _candidate(item: ClaimCandidate) -> dict[str, object]:
    return {
        "transaction": item.transaction,
        "split": item.split,
        "date": item.date.isoformat(),
        "description": item.description,
        "account": item.account,
        "account_name": item.account_name,
        "amount": _decimal(item.amount),
    }


def _candidates(candidates: ClaimCandidates) -> dict[str, object]:
    return {
        "payments": [_candidate(item) for item in candidates.payments],
        "refunds": [_candidate(item) for item in candidates.refunds],
        "reimbursements": [_candidate(item) for item in candidates.reimbursements],
        "repayments": [_candidate(item) for item in candidates.repayments],
        "fsa_accounts": [
            {
                "handle": item.handle,
                "name": item.name,
                "years": [year.serialize() for year in item.years],
            }
            for item in candidates.fsa_accounts
        ],
        # A payer covering part of a claim's expense (issue #192).
        "receivables": [
            {"handle": item.handle, "label": item.name} for item in candidates.receivables
        ],
    }


def _text(payload: Mapping[str, Any], key: str) -> str:
    return str(payload.get(key) or "").strip()


def _date(payload: Mapping[str, Any], key: str) -> date | None:
    text = _text(payload, key)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{key} must be YYYY-MM-DD") from None


def _link(item: Mapping[str, Any]) -> ClaimLinkInput:
    return ClaimLinkInput(str(item["transaction"]), str(item["split"]))


def fsa_claim_save(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Create or replace one claim; a changed EOB is recorded with ``eob_note``."""
    body = dict(payload)
    eob = _text(payload, "eob_responsibility")

    def money(raw: object):
        return input_money(body, raw)

    definition = ClaimInput(
        service_date=date.fromisoformat(str(payload["service_date"])),
        provider=_text(payload, "provider"),
        description=_text(payload, "description"),
        eob_responsibility=money(eob) if eob else None,
        payments=tuple(_link(item) for item in payload.get("payments", [])),
        refunds=tuple(_link(item) for item in payload.get("refunds", [])),
        allocations=tuple(
            ClaimAllocationInput(
                account=str(item["account"]),
                funding_year_start=date.fromisoformat(str(item["funding_year_start"])),
                target=(money(item["target"]) if item.get("target") not in (None, "") else None),
                reimbursements=tuple(_link(link) for link in item.get("reimbursements", [])),
                rejections=tuple(
                    ClaimRejectionInput(
                        attempted_on=date.fromisoformat(str(rejection["attempted_on"])),
                        amount=money(rejection["amount"]),
                        reason=str(rejection.get("reason", "")),
                    )
                    for rejection in item.get("rejections", [])
                ),
                repayments=tuple(_link(link) for link in item.get("repayments", [])),
            )
            for item in payload.get("allocations", [])
        ),
        receivable=_text(payload, "receivable") or None,
        eob_note=_text(payload, "eob_note"),
    )
    saved = api.book.save_fsa_claim(
        SaveClaim(definition, existing_handle=_text(payload, "handle") or None)
    )
    return {"handle": saved.handle}


def fsa_claim_delete(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    handle = str(payload.get("handle", ""))
    api.book.delete_fsa_claim(handle)
    return {"handle": handle}


def fsa_claim_close(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Stop pursuing what is left to reimburse on a claim."""
    request = CloseClaim(
        _text(payload, "handle"),
        _date(payload, "on") or date.today(),
        _text(payload, "reason"),
    )
    saved = api.book.close_fsa_claim(request)
    return {"handle": saved.handle}


def fsa_claim_reopen(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Pursue a closed claim again."""
    request = ReopenClaim(
        _text(payload, "handle"),
        _date(payload, "on") or date.today(),
        _text(payload, "note"),
    )
    saved = api.book.reopen_fsa_claim(request)
    return {"handle": saved.handle}
