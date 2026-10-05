"""HTTP input and output for statement reconciliation.

This adapter only parses request fields into the typed reconciliation service
requests and shapes their results; opening, selection, balancing, completion,
cancellation, and reopening rules all stay in ``gen/services/reconciliations``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any

from ..gen.engine import reconciliation as reconciliation_engine
from ..gen.services.claims import claim_link_proposals
from ..gen.services.receivables import reimbursement_proposals
from ..gen.services.reconciliations import (
    ReconciliationAction,
    StartReconciliation,
    UpdateReconciliation,
    cancel_reconciliation,
    complete_reconciliation,
    reopen_reconciliation,
    start_reconciliation,
    update_reconciliation,
)
from ..presentation import claim_link_notice, reimbursement_notice
from .controls import input_money, service_error

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
    account_handle = query.text("account", required=True) or ""
    query.finish()
    """Return the current statement workflow and its auditable history."""
    account = api.db.get_account(account_handle)
    if account is None:
        raise KeyError(account_handle)
    current = reconciliation_engine.open_for_account(api.db, account_handle)
    current_state = reconciliation_engine.summary(api.db, current) if current else None
    proposals = reimbursement_proposals(api.db, account=account_handle).value or ()
    return {
        "account": {"handle": account.handle, "name": api.db.full_name(account)},
        # A deposit being reconciled may be money back on a reimbursable expense.
        "reimbursement_notice": reimbursement_notice(len(proposals)),
        # An FSA statement line may belong on a claim.
        "claim_link_notice": claim_link_notice(
            len(claim_link_proposals(api.db, account=account_handle).value or ())
        ),
        "open": (
            {
                "handle": current.handle,
                "statement_date": current.statement_date,
                "ending_balance": current.ending_balance,
                "opening_balance": current_state.opening_balance,
                "selected_balance": current_state.selected_balance,
                "difference": current_state.difference,
                "balanced": current_state.balanced,
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
                    }
                    for item in current_state.candidates
                ],
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
            for item in reversed(list(api.db.iter_reconciliations(account_handle)))
        ],
    }


def reconciliation_start(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    account = str(payload.get("account") or "")
    try:
        statement_date = date.fromisoformat(str(payload.get("statement_date") or ""))
    except ValueError as exc:
        raise ValueError("enter a valid statement date") from exc
    ending = input_money(dict(payload), payload.get("ending_balance", ""))
    result = start_reconciliation(
        api.db,
        StartReconciliation(account, statement_date, ending),
    )
    if result.value is None:
        raise service_error(result.errors[0])
    started = result.value.reconciliation
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
    result = update_reconciliation(
        api.db,
        UpdateReconciliation(
            handle,
            selected_splits=tuple(raw_splits),
            ending_balance=ending,
        ),
    )
    if result.value is None:
        raise service_error(result.errors[0])
    state = result.value
    return {"handle": handle, "difference": state.difference, "balanced": state.balanced}


def reconciliation_complete(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    result = complete_reconciliation(
        api.db,
        ReconciliationAction(str(payload.get("handle") or "")),
    )
    if result.value is None:
        raise service_error(result.errors[0])
    completed = result.value
    return {"handle": completed.handle, "status": completed.status.value}


def reconciliation_cancel(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    result = cancel_reconciliation(
        api.db,
        ReconciliationAction(str(payload.get("handle") or "")),
    )
    if result.value is None:
        raise service_error(result.errors[0])
    cancelled = result.value
    return {"handle": cancelled.handle, "status": cancelled.status.value}


def reconciliation_reopen(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    result = reopen_reconciliation(
        api.db,
        ReconciliationAction(str(payload.get("handle") or "")),
    )
    if result.value is None:
        raise service_error(result.errors[0])
    reopened = result.value
    return {"handle": reopened.handle, "status": reopened.status.value}
