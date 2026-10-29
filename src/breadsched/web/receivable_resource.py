"""HTTP input and output for reimbursable expenses (receivables).

This adapter only parses JSON into the book server's receivable requests and
translates its results. Validation, status arithmetic, and every write stay in
``gen/services/receivables``, behind the book server (``Api.book``), so a rejected
request never changes the book. The
service, not this adapter, keeps the Receivable-account reclassifications (#170).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any

from ..api.contract import ApiError
from ..gen.services.receivables import RecordWriteOff, SaveReceivable
from ..gen.services.scenarios import SetReimbursementOverride
from ..presentation import (
    receivable_account_note,
    reimbursement_override_text,
    shared_cost_text,
)
from .controls import input_money

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


def _linked(links) -> list[dict[str, object]]:
    return [
        {
            "transaction": link.transaction,
            "split": link.split,
            "date": link.when,
            "description": link.description,
            "account": link.account_name,
            "value": link.value,
        }
        for link in links
    ]


def _candidate(line) -> dict[str, object]:
    item = line.candidate
    return {
        "transaction": item.transaction,
        "split": item.split,
        "date": item.when,
        "description": item.description,
        "account": line.account_name,
        "account_handle": item.account,
        "value": item.value,
    }


def receivables(api: Api, query: QueryParams) -> dict[str, object]:
    """Every receivable's recomputed standing and links, plus linkable splits."""
    query.finish()
    view = api.book.receivables_view(date.today())
    return {
        "proposals": [
            {
                "receivable": line.proposal.receivable,
                "payer": line.proposal.payer,
                "transaction": line.proposal.transaction,
                "split": line.proposal.split,
                "date": line.proposal.when,
                "description": line.proposal.description,
                "account": line.account_name,
                "amount": line.proposal.amount,
                "remaining_after": line.proposal.remaining_after,
                "reason": line.proposal.reason,
            }
            for line in view.proposals
        ],
        "receivables": [
            {
                "handle": line.summary.receivable.handle,
                "payer": line.summary.receivable.payer,
                "description": line.summary.receivable.description,
                "incurred_date": line.summary.receivable.incurred_date,
                "expected_amount": line.summary.receivable.expected_amount,
                "expected_cash_date": line.summary.receivable.expected_cash_date,
                "disputed_on": line.summary.receivable.disputed_on,
                "dispute_note": line.summary.receivable.dispute_note,
                "expense_total": line.summary.expense_total,
                "reimbursed": line.summary.reimbursed,
                "written_off": line.summary.written_off,
                "remaining": line.summary.remaining,
                "age_days": line.summary.age_days,
                "status": line.summary.status.value,
                "status_label": line.summary.status.label,
                "owed": line.summary.owed,
                "direct": line.summary.direct,
                "account": line.summary.receivable.account,
                "account_name": line.account_name,
                "fsa_claims": list(line.summary.fsa_claims),
                "shared_costs": [shared_cost_json(item) for item in line.shared_costs],
                "expenses": _linked(line.expenses),
                "reimbursements": _linked(line.reimbursements),
                "scenario_changes": [
                    {
                        "scenario": change.scenario,
                        "amount": change.override.amount,
                        "on": change.override.on,
                        "text": reimbursement_override_text(change.scenario_name, change.override),
                    }
                    for change in line.scenario_changes
                ],
            }
            for line in view.receivables
        ],
        "scenarios": [{"handle": handle, "name": name} for handle, name in view.scenarios],
        "costs": [_candidate(line) for line in view.costs],
        "credits": [_candidate(line) for line in view.credits],
        "accounts": [{"handle": handle, "name": name} for handle, name in view.accounts],
        "write_off_accounts": [
            {"handle": handle, "name": name} for handle, name in view.write_off_accounts
        ],
        "account_note": receivable_account_note(
            [name for _handle, name in view.unoffered_accounts]
        ),
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
    saved = api.book.save_receivable(request)
    response: dict[str, object] = {"handle": saved.handle, "payer": saved.payer}
    expense = payload.get("link_expense")
    if request.handle is None and isinstance(expense, dict):
        try:
            api.book.link_receivable_split(
                saved.handle,
                _text(expense, "transaction") or "",
                _text(expense, "split") or "",
                "expense",
            )
            response["linked"] = True
        except ApiError:
            response["linked"] = False
    return response


def receivable_link(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    role = _text(payload, "role")
    if role not in {"expense", "reimbursement"}:
        raise ValueError("role must be expense or reimbursement")
    saved = api.book.link_receivable_split(
        _text(payload, "receivable") or "",
        _text(payload, "transaction") or "",
        _text(payload, "split") or "",
        role,
    )
    return {"handle": saved.handle}


def receivable_unlink(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    saved = api.book.detach_receivable_split(
        _text(payload, "receivable") or "",
        _text(payload, "transaction") or "",
        _text(payload, "split") or "",
    )
    return {"handle": saved.handle}


def receivable_dispute(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    handle = _text(payload, "handle") or ""
    if payload.get("clear") is True:
        saved = api.book.clear_dispute(handle)
    else:
        saved = api.book.mark_disputed(
            handle,
            _date(payload, "disputed_on") or date.today(),
            _text(payload, "note", optional=True) or "",
        )
    return {"handle": saved.handle}


def receivable_scenario(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Change what one scenario expects back for a receivable; blank fields clear it."""
    raw_amount = (_text(payload, "amount", optional=True) or "").strip()
    receivable = _text(payload, "receivable") or ""
    scenario = api.book.set_reimbursement_override(
        SetReimbursementOverride(
            _text(payload, "scenario") or "",
            receivable,
            input_money(dict(payload), raw_amount) if raw_amount else None,
            _date(payload, "on", optional=True),
        )
    )
    override = scenario.reimbursement_overrides.get(receivable)
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
    saved = api.book.record_write_off(
        RecordWriteOff(
            _text(payload, "receivable") or "",
            input_money(dict(payload), raw_amount),
            _date(payload, "written_off_on") or date.today(),
            _text(payload, "reason", optional=True) or "",
            _text(payload, "account", optional=True) or None,
        )
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
    accepted = api.book.accept_reimbursements(tuple((item[0], item[1], item[2]) for item in raw))
    return {"linked": accepted.linked, "unchanged": accepted.unchanged}


def receivable_delete(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    return {"handle": api.book.delete_receivable(_text(payload, "handle") or "")}
