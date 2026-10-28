"""HTTP input and output for statement reconciliation.

This adapter only parses request fields into the book server's typed
reconciliation requests and shapes their results; opening, selection, balancing,
completion, cancellation, and reopening rules all stay in
``gen/services/reconciliations``, behind the book server (``Api.book``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any

from ..api.contract import ApiError
from ..gen.engine import reconciliation as reconciliation_engine
from ..gen.services.reconciliations import StartReconciliation, UpdateReconciliation
from ..presentation import claim_link_notice, reimbursement_notice
from .controls import input_money

if TYPE_CHECKING:
    from .context import Api
    from .resources import QueryParams

__all__ = [
    "reconciliation",
    "reconciliation_cancel",
    "reconciliation_complete",
    "reconciliation_reopen",
    "reconciliation_start",
    "reconciliation_update",
]


def reconciliation(api: Api, query: QueryParams) -> dict[str, object]:
    """Return the current statement workflow and its auditable history.

    ``sort`` (one of ``CANDIDATE_SORTS``) and ``descending`` order the statement's
    entries, as the desktop's lists do (#377).
    """
    account_handle = query.text("account", required=True) or ""
    sort = query.choice("sort", reconciliation_engine.CANDIDATE_SORTS, "date")
    descending = query.flag("descending")
    query.finish()
    try:
        view = api.book.reconciliation(account_handle)
    except ApiError as exc:
        if exc.status == 404:
            raise KeyError(account_handle) from None
        raise
    current_state = view.current
    current = current_state.reconciliation if current_state is not None else None
    return {
        "account": {"handle": view.account, "name": view.account_name},
        # A deposit being reconciled may be money back on a reimbursable expense.
        "reimbursement_notice": reimbursement_notice(view.reimbursement_proposals),
        # An FSA statement line may belong on a claim.
        "claim_link_notice": claim_link_notice(view.claim_link_proposals),
        "open": (
            {
                "handle": current.handle,
                "statement_date": current.statement_date,
                "ending_balance": current.ending_balance,
                "opening_balance": current_state.opening_balance,
                "selected_balance": current_state.selected_balance,
                "difference": current_state.difference,
                "balanced": current_state.balanced,
                "selected_in": current_state.selected_in,
                "selected_out": current_state.selected_out,
                "selected_splits": list(current.selected_splits),
                "candidates": [
                    {
                        "transaction": item.transaction,
                        "split": item.split,
                        "date": item.post_date,
                        "description": item.description,
                        "amount": item.amount,
                        "state": item.state.value,
                        "selected": item.selected,
                        "after_statement": item.after_statement,
                        "funds_in": item.funds_in,
                    }
                    for item in reconciliation_engine.sort_candidates(
                        current_state.candidates, sort, descending
                    )
                ],
                "sort": sort,
                "descending": descending,
                "sorts": list(reconciliation_engine.CANDIDATE_SORTS),
            }
            if current is not None and current_state is not None
            else None
        ),
        "history": [
            {
                "handle": item.handle,
                "statement_date": item.statement_date,
                "ending_balance": item.ending_balance,
                "status": item.status.value,
                "completed_at": item.completed_at,
                "cancelled_at": item.cancelled_at,
                "events": [
                    {"action": event.action, "at": event.occurred_at} for event in item.audit_events
                ],
            }
            for item in view.history
        ],
    }


def reconciliation_start(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    account = str(payload.get("account") or "")
    try:
        statement_date = date.fromisoformat(str(payload.get("statement_date") or ""))
    except ValueError as exc:
        raise ValueError("enter a valid statement date") from exc
    ending = input_money(dict(payload), payload.get("ending_balance", ""))
    started = api.book.start_reconciliation(
        StartReconciliation(account, statement_date, ending)
    ).reconciliation
    return {"handle": started.handle, "status": started.status.value}


def reconciliation_update(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    handle = str(payload.get("handle") or "")
    raw_splits = payload.get("selected_splits", [])
    if not isinstance(raw_splits, list) or not all(isinstance(item, str) for item in raw_splits):
        raise ValueError("selected_splits must be a list of split handles")
    ending = (
        input_money(dict(payload), payload["ending_balance"])
        if "ending_balance" in payload
        else None
    )
    state = api.book.update_reconciliation(
        UpdateReconciliation(
            handle,
            selected_splits=tuple(raw_splits),
            ending_balance=ending,
        ),
    )
    return {"handle": handle, "difference": state.difference, "balanced": state.balanced}


def reconciliation_complete(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    completed = api.book.complete_reconciliation(str(payload.get("handle") or ""))
    return {"handle": completed.handle, "status": completed.status.value}


def reconciliation_cancel(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    cancelled = api.book.cancel_reconciliation(str(payload.get("handle") or ""))
    return {"handle": cancelled.handle, "status": cancelled.status.value}


def reconciliation_reopen(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    reopened = api.book.reopen_reconciliation(str(payload.get("handle") or ""))
    return {"handle": reopened.handle, "status": reopened.status.value}
