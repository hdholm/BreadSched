"""HTTP input and output for reimbursable expenses (receivables).

This adapter only parses JSON into the shared receivables service's requests and
translates its results. Validation, status arithmetic, and every write stay in
``gen/services/receivables``, so a rejected request never changes the book, and
no request here posts anything to the ledger.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any

from ..gen.services.receivables import (
    RecordWriteOff,
    SaveReceivable,
    accept_reimbursements,
    attach_expense_split,
    attach_reimbursement_split,
    clear_dispute,
    delete_receivable,
    detach_split,
    list_receivables,
    mark_disputed,
    receivable_candidates,
    record_write_off,
    reimbursement_proposals,
    save_receivable,
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


def _date(payload: Mapping[str, Any], key: str, *, optional: bool = False) -> date | None:
    text = (_text(payload, key, optional=optional) or "").strip()
    if not text and optional:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{key} must be YYYY-MM-DD") from None


def _result(api: Api, result) -> Any:
    if result.value is None:
        raise api._service_resource_error(result.errors[0])
    return result.value


def _linked(api: Api, links) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for link in links:
        transaction = api.db.get_transaction(link.transaction)
        split = (
            next((item for item in transaction.splits if item.handle == link.split), None)
            if transaction is not None
            else None
        )
        rows.append(
            {
                "transaction": link.transaction,
                "split": link.split,
                "date": transaction.post_date if transaction is not None else None,
                "description": transaction.description if transaction is not None else None,
                "account": api.db.full_name(split.account) if split is not None else None,
                "value": split.value if split is not None else None,
            }
        )
    return rows


def _candidate(api: Api, item) -> dict[str, object]:
    return {
        "transaction": item.transaction,
        "split": item.split,
        "date": item.when,
        "description": item.description,
        "account": api.db.full_name(item.account),
        "value": item.value,
    }


def receivables(api: Api, query: QueryParams) -> dict[str, object]:
    """Every receivable's recomputed standing and links, plus linkable splits."""
    query.finish()
    summaries = _result(api, list_receivables(api.db, as_of=date.today()))
    costs, credits = _result(api, receivable_candidates(api.db))
    proposals = _result(api, reimbursement_proposals(api.db, as_of=date.today()))
    return {
        "proposals": [
            {
                "receivable": item.receivable,
                "payer": item.payer,
                "transaction": item.transaction,
                "split": item.split,
                "date": item.when,
                "description": item.description,
                "account": api.db.full_name(item.account),
                "amount": item.amount,
                "remaining_after": item.remaining_after,
                "reason": item.reason,
            }
            for item in proposals
        ],
        "receivables": [
            {
                "handle": item.receivable.handle,
                "payer": item.receivable.payer,
                "description": item.receivable.description,
                "incurred_date": item.receivable.incurred_date,
                "expected_amount": item.receivable.expected_amount,
                "expected_cash_date": item.receivable.expected_cash_date,
                "disputed_on": item.receivable.disputed_on,
                "dispute_note": item.receivable.dispute_note,
                "expense_total": item.expense_total,
                "reimbursed": item.reimbursed,
                "written_off": item.written_off,
                "remaining": item.remaining,
                "age_days": item.age_days,
                "status": item.status.value,
                "status_label": item.status.label,
                "expenses": _linked(api, item.receivable.expenses),
                "reimbursements": _linked(api, item.receivable.reimbursements),
            }
            for item in summaries
        ],
        "costs": [_candidate(api, item) for item in costs],
        "credits": [_candidate(api, item) for item in credits],
    }


def receivable_save(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Create or update a receivable, optionally linking a first expense split."""
    raw_expected = payload.get("expected_amount")
    expected = (
        api._input_money(dict(payload), raw_expected) if raw_expected not in (None, "") else None
    )
    request = SaveReceivable(
        incurred_date=_date(payload, "incurred_date") or date.today(),
        payer=_text(payload, "payer") or "",
        description=_text(payload, "description", optional=True) or "",
        expected_amount=expected,
        expected_cash_date=_date(payload, "expected_cash_date", optional=True),
        handle=_text(payload, "handle", optional=True) or None,
    )
    saved = _result(api, save_receivable(api.db, request))
    response: dict[str, object] = {"handle": saved.handle, "payer": saved.payer}
    expense = payload.get("link_expense")
    if request.handle is None and isinstance(expense, dict):
        linked = attach_expense_split(
            api.db,
            saved.handle,
            _text(expense, "transaction") or "",
            _text(expense, "split") or "",
        )
        response["linked"] = linked.value is not None
    return response


def receivable_link(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    role = _text(payload, "role")
    if role not in {"expense", "reimbursement"}:
        raise ValueError("role must be expense or reimbursement")
    attach = attach_expense_split if role == "expense" else attach_reimbursement_split
    saved = _result(
        api,
        attach(
            api.db,
            _text(payload, "receivable") or "",
            _text(payload, "transaction") or "",
            _text(payload, "split") or "",
        ),
    )
    return {"handle": saved.handle}


def receivable_unlink(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    saved = _result(
        api,
        detach_split(
            api.db,
            _text(payload, "receivable") or "",
            _text(payload, "transaction") or "",
            _text(payload, "split") or "",
        ),
    )
    return {"handle": saved.handle}


def receivable_dispute(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    handle = _text(payload, "handle") or ""
    if payload.get("clear") is True:
        saved = _result(api, clear_dispute(api.db, handle))
    else:
        saved = _result(
            api,
            mark_disputed(
                api.db,
                handle,
                _date(payload, "disputed_on") or date.today(),
                _text(payload, "note", optional=True) or "",
            ),
        )
    return {"handle": saved.handle}


def receivable_write_off(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    raw_amount = payload.get("amount")
    if not isinstance(raw_amount, str):
        raise ValueError("amount must be text")
    saved = _result(
        api,
        record_write_off(
            api.db,
            RecordWriteOff(
                _text(payload, "receivable") or "",
                api._input_money(dict(payload), raw_amount),
                _date(payload, "written_off_on") or date.today(),
                _text(payload, "reason", optional=True) or "",
            ),
        ),
    )
    return {"handle": saved.handle}


def receivable_accept(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Link the chosen proposals that are still on offer."""
    raw = payload.get("links")
    if not isinstance(raw, list) or not all(
        isinstance(item, list) and len(item) == 3 and all(isinstance(part, str) for part in item)
        for item in raw
    ):
        raise ValueError("links must be a list of [receivable, transaction, split]")
    accepted = _result(
        api, accept_reimbursements(api.db, tuple((item[0], item[1], item[2]) for item in raw))
    )
    return {"linked": accepted.linked, "unchanged": accepted.unchanged}


def receivable_delete(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    return {"handle": _result(api, delete_receivable(api.db, _text(payload, "handle") or ""))}
