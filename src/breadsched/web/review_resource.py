"""HTTP input and output for Review: unresolved actuals and their decisions.

The candidate list, its reasons, and the FSA hint come from
``engine.review_explain``; every decision goes through the shared review service, so
a rejected request never changes the book.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import fsa_claims, review_explain
from ..gen.lib import Money, PlanningResolution, Transaction
from ..gen.services import (
    ReviewClaimAttachment,
    ReviewOccurrence,
    ReviewTransaction,
    attach_review_claim,
    mark_review_unexpected,
    match_review,
    reject_review,
    skip_review,
)
from ..presentation import REVIEW_ACTION_HELP, claim_role_label
from .controls import service_error

if TYPE_CHECKING:
    from .context import Api


def gross_amount(transaction: Transaction) -> Money:
    total = Money(0)
    for split in transaction.splits:
        if split.value > 0:
            total = total + split.value
    return total


def review_fsa_options(db: DbSQLite, transaction: Transaction) -> dict:
    roles: list[dict[str, object]] = [
        {
            "role": item.role,
            "label": claim_role_label(item.role),
            "split": item.split,
            "account": db.full_name(item.account),
            "years": [year.isoformat() for year in item.years],
        }
        for item in fsa_claims.attachment_roles(db, transaction)
    ]
    claims = []
    for suggestion in fsa_claims.suggest_claims_for_transaction(db, transaction):
        claim = suggestion.claim
        summary = fsa_claims.claim_summary(db, claim)
        claims.append(
            {
                "handle": claim.handle,
                "label": (
                    f"{claim.service_date.isoformat()} "
                    f"{claim.provider or claim.description or 'FSA claim'}"
                ),
                "remaining": summary.remaining_reimbursable,
                "score": suggestion.score,
                "reason": suggestion.reason,
                "suggested_role": suggestion.role,
                "suggested_split": suggestion.split_handle,
            }
        )
    return {"roles": roles, "claims": claims}


def review(api: Api, transaction_handle: str | None = None) -> dict:
    """Unresolved actuals and candidate plan occurrences for Review."""
    transactions = sorted(
        (
            transaction
            for transaction in api.db.iter_transactions()
            if transaction.planning_resolution is PlanningResolution.UNRESOLVED
        ),
        key=lambda transaction: (transaction.post_date, transaction.handle),
    )
    actuals = [
        {
            "handle": transaction.handle,
            "date": transaction.post_date,
            "description": transaction.description,
            "amount": gross_amount(transaction),
        }
        for transaction in transactions
    ]

    selected = None
    candidates: list[dict] = []
    if transaction_handle is not None:
        transaction = api.db.get_transaction(transaction_handle)
        if transaction is None:
            raise KeyError(transaction_handle)
        if transaction.planning_resolution is not PlanningResolution.UNRESOLVED:
            raise ValueError("transaction is no longer awaiting review")
        actual_amount = gross_amount(transaction)
        selected = {
            "handle": transaction.handle,
            "date": transaction.post_date,
            "description": transaction.description,
            "amount": actual_amount,
            "fsa": review_fsa_options(api.db, transaction),
            "fsa_hint": review_explain.fsa_hint(api.db, transaction),
            "no_candidate_reason": None,
        }
        for explained in review_explain.explain_candidates(api.db, transaction):
            candidate = explained.candidate
            event = candidate.event
            candidates.append(
                {
                    "key": event.key,
                    "date": event.planned_date,
                    "description": event.description,
                    "expected_amount": event.expected_amount,
                    "date_distance_days": candidate.date_distance,
                    "date_variance_days": (transaction.post_date - event.planned_date).days,
                    "amount_difference": candidate.amount_difference,
                    "amount_variance": actual_amount - event.expected_amount,
                    "common_accounts": candidate.common_accounts,
                    **explained.as_dict(),
                }
            )
        if not candidates:
            selected["no_candidate_reason"] = review_explain.no_candidate_reason(
                api.db, transaction
            )

    return {
        "actuals": actuals,
        "selected": selected,
        "candidates": candidates,
        "action_help": dict(REVIEW_ACTION_HELP),
    }


def review_match(api: Api, payload: dict) -> dict:
    result = match_review(
        api.db,
        ReviewOccurrence(str(payload["transaction"]), str(payload["occurrence"])),
    )
    if not result.ok:
        raise service_error(result.errors[0])
    mutation = result.value
    assert mutation is not None and mutation.resolution is not None
    return {
        "transaction": mutation.transaction,
        "resolution": mutation.resolution.value,
        "occurrence": mutation.occurrence,
    }


def review_reject(api: Api, payload: dict) -> dict:
    result = reject_review(
        api.db,
        ReviewOccurrence(str(payload["transaction"]), str(payload["occurrence"])),
    )
    if not result.ok:
        raise service_error(result.errors[0])
    mutation = result.value
    assert mutation is not None
    return {
        "transaction": mutation.transaction,
        "rejected": mutation.occurrence,
    }


def review_skip(api: Api, payload: dict) -> dict:
    result = skip_review(
        api.db,
        ReviewOccurrence(str(payload["transaction"]), str(payload["occurrence"])),
    )
    if not result.ok:
        raise service_error(result.errors[0])
    mutation = result.value
    assert mutation is not None
    return {"transaction": mutation.transaction, "skipped": mutation.occurrence}


def review_fsa_attach(api: Api, payload: dict) -> dict:
    funding_year = str(payload.get("funding_year") or "").strip()
    result = attach_review_claim(
        api.db,
        ReviewClaimAttachment(
            str(payload["transaction"]),
            str(payload["claim"]),
            str(payload["role"]),
            str(payload.get("split") or "") or None,
            date.fromisoformat(funding_year) if funding_year else None,
        ),
    )
    if not result.ok:
        raise service_error(result.errors[0])
    mutation = result.value
    assert mutation is not None
    return {"claim": mutation.claim, "transaction": mutation.transaction}


def review_unexpected(api: Api, payload: dict) -> dict:
    result = mark_review_unexpected(api.db, ReviewTransaction(str(payload["transaction"])))
    if not result.ok:
        raise service_error(result.errors[0])
    mutation = result.value
    assert mutation is not None and mutation.resolution is not None
    return {
        "transaction": mutation.transaction,
        "resolution": mutation.resolution.value,
    }
