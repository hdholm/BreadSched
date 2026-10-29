"""HTTP input and output for Review: unresolved actuals and their decisions.

The candidate list, its reasons, and the FSA hint come from
``engine.review_explain``; every decision goes through the shared review service, so
a rejected request never changes the book. The queue, each actual's detail, and
every decision go through the book server, ``api.book`` (#354).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from ..gen.services import ReviewClaimAttachment, ReviewOccurrence
from ..gen.services.review import ReviewDetail
from ..presentation import REVIEW_ACTION_HELP, claim_role_label

if TYPE_CHECKING:
    from .context import Api


def review_fsa_options(detail: ReviewDetail) -> dict:
    roles: list[dict[str, object]] = [
        {
            "role": item.role,
            "label": claim_role_label(item.role),
            "split": item.split,
            "account": item.account_name,
            "years": [year.isoformat() for year in item.years],
        }
        for item in detail.roles
    ]
    claims = [
        {
            "handle": item.claim,
            "label": item.label,
            "remaining": item.remaining,
            "score": item.score,
            "reason": item.reason,
            "suggested_role": item.role,
            "suggested_split": item.split,
        }
        for item in detail.claims
    ]
    return {"roles": roles, "claims": claims}


def review(api: Api, transaction_handle: str | None = None) -> dict:
    """Unresolved actuals and candidate plan occurrences for Review."""
    actuals = [
        {
            "handle": item.handle,
            "date": item.date,
            "description": item.description,
            "amount": item.amount,
        }
        for item in api.book.review_queue()
    ]

    selected = None
    candidates: list[dict] = []
    if transaction_handle is not None:
        detail = api.book.review_detail(transaction_handle)
        actual = detail.actual
        selected = {
            "handle": actual.handle,
            "date": actual.date,
            "description": actual.description,
            "amount": actual.amount,
            "fsa": review_fsa_options(detail),
            "fsa_hint": detail.fsa_hint,
            "no_candidate_reason": detail.no_candidate_reason,
        }
        for explained in detail.candidates:
            candidate = explained.candidate
            event = candidate.event
            candidates.append(
                {
                    "key": event.key,
                    "date": event.planned_date,
                    "description": event.description,
                    "expected_amount": event.expected_amount,
                    "date_distance_days": candidate.date_distance,
                    "date_variance_days": (actual.date - event.planned_date).days,
                    "amount_difference": candidate.amount_difference,
                    "amount_variance": actual.amount - event.expected_amount,
                    "common_accounts": candidate.common_accounts,
                    **explained.as_dict(),
                }
            )

    return {
        "actuals": actuals,
        "selected": selected,
        "candidates": candidates,
        "action_help": dict(REVIEW_ACTION_HELP),
    }


def review_match(api: Api, payload: dict) -> dict:
    mutation = api.book.match_review(
        ReviewOccurrence(str(payload["transaction"]), str(payload["occurrence"]))
    )
    assert mutation.resolution is not None
    return {
        "transaction": mutation.transaction,
        "resolution": mutation.resolution.value,
        "occurrence": mutation.occurrence,
    }


def review_reject(api: Api, payload: dict) -> dict:
    mutation = api.book.reject_review(
        ReviewOccurrence(str(payload["transaction"]), str(payload["occurrence"]))
    )
    return {
        "transaction": mutation.transaction,
        "rejected": mutation.occurrence,
    }


def review_skip(api: Api, payload: dict) -> dict:
    mutation = api.book.skip_review(
        ReviewOccurrence(str(payload["transaction"]), str(payload["occurrence"]))
    )
    return {"transaction": mutation.transaction, "skipped": mutation.occurrence}


def review_fsa_attach(api: Api, payload: dict) -> dict:
    funding_year = str(payload.get("funding_year") or "").strip()
    mutation = api.book.attach_review_claim(
        ReviewClaimAttachment(
            str(payload["transaction"]),
            str(payload["claim"]),
            str(payload["role"]),
            str(payload.get("split") or "") or None,
            date.fromisoformat(funding_year) if funding_year else None,
        )
    )
    return {"claim": mutation.claim, "transaction": mutation.transaction}


def review_unexpected(api: Api, payload: dict) -> dict:
    mutation = api.book.mark_review_unexpected(str(payload["transaction"]))
    assert mutation.resolution is not None
    return {
        "transaction": mutation.transaction,
        "resolution": mutation.resolution.value,
    }
