"""HTTP input and output for reimbursable expenses (receivables).

This adapter only parses JSON into the shared receivables service's requests and
translates its results. Validation, status arithmetic, and every write stay in
``gen/services/receivables``, so a rejected request never changes the book. The
service, not this adapter, keeps the Receivable-account reclassifications (#170).
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
    receivable_accounts,
    receivable_candidates,
    record_write_off,
    reimbursement_proposals,
    save_receivable,
    shared_costs,
)
from ..gen.services.scenarios import SetReimbursementOverride, set_reimbursement_override
from ..presentation import reimbursement_override_text, shared_cost_text
from .controls import input_money, service_error

if TYPE_CHECKING:
    from ..gen.engine.fsa_claims import SharedCost
    from .context import Api
    from .resources import QueryParams


def shared_cost_json(shared: SharedCost) -> dict[str, object]:
    """One payer/FSA/you allocation (issue #192), with the shared wording."""
    return {
        "claim": shared.claim,
        "receivable": shared.receivable,
        "payer": shared.payer,
        "expense": shared.expense,
        "payer_share": shared.payer_share,
        "fsa_share": shared.fsa_share,
        "your_share": shared.your_share,
        "waiting_eob": shared.waiting_eob,
        "needs_review": shared.needs_review,
        "over_allocated": shared.over_allocated,
        "text": shared_cost_text(shared),
    }


def _shared(api: Api, handle: str) -> list[dict[str, object]]:
    result = shared_costs(api.db, handle)
    if result.value is None:
        raise service_error(result.errors[0])
    return [shared_cost_json(item) for item in result.value]


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
        raise service_error(result.errors[0])
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
    scenarios = sorted(api.db.iter_scenarios(), key=lambda item: item.name.casefold())
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
                "owed": item.owed,
                "account": item.receivable.account,
                "account_name": (
                    api.db.full_name(item.receivable.account) if item.receivable.account else None
                ),
                "fsa_claims": list(item.fsa_claims),
                "shared_costs": _shared(api, item.receivable.handle),
                "expenses": _linked(api, item.receivable.expenses),
                "reimbursements": _linked(api, item.receivable.reimbursements),
                "scenario_changes": [
                    {
                        "scenario": scenario.handle,
                        "amount": scenario.reimbursement_overrides[item.receivable.handle].amount,
                        "on": scenario.reimbursement_overrides[item.receivable.handle].on,
                        "text": reimbursement_override_text(
                            scenario.name,
                            scenario.reimbursement_overrides[item.receivable.handle],
                        ),
                    }
                    for scenario in scenarios
                    if item.receivable.handle in scenario.reimbursement_overrides
                ],
            }
            for item in summaries
        ],
        "scenarios": [{"handle": item.handle, "name": item.name} for item in scenarios],
        "costs": [_candidate(api, item) for item in costs],
        "credits": [_candidate(api, item) for item in credits],
        "accounts": [
            {"handle": account.handle, "name": api.db.full_name(account) or account.name}
            for account in receivable_accounts(api.db)
        ],
    }


def receivable_save(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Create or update a receivable, optionally linking a first expense split."""
    raw_expected = payload.get("expected_amount")
    expected = input_money(dict(payload), raw_expected) if raw_expected not in (None, "") else None
    request = SaveReceivable(
        incurred_date=_date(payload, "incurred_date") or date.today(),
        payer=_text(payload, "payer") or "",
        description=_text(payload, "description", optional=True) or "",
        expected_amount=expected,
        expected_cash_date=_date(payload, "expected_cash_date", optional=True),
        handle=_text(payload, "handle", optional=True) or None,
        account=_text(payload, "account", optional=True) or None,
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


def receivable_scenario(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Change what one scenario expects back for a receivable; blank fields clear it."""
    raw_amount = (_text(payload, "amount", optional=True) or "").strip()
    scenario = _result(
        api,
        set_reimbursement_override(
            api.db,
            SetReimbursementOverride(
                _text(payload, "scenario") or "",
                _text(payload, "receivable") or "",
                input_money(dict(payload), raw_amount) if raw_amount else None,
                _date(payload, "on", optional=True),
            ),
        ),
    )
    override = scenario.reimbursement_overrides.get(_text(payload, "receivable"))
    return {
        "scenario": scenario.handle,
        "text": (
            reimbursement_override_text(scenario.name, override)
            if override is not None
            else f"{scenario.name} expects what the receivable says"
        ),
    }


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
                input_money(dict(payload), raw_amount),
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
