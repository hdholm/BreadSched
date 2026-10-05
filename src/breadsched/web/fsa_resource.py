"""Web FSA Dashboard: benefit years, open claims, and the claim report.

Availability comes from ``gen/engine/fsa`` and the claims, their totals, and the
reasons one needs attention from ``gen/engine/fsa_claim_report``; this adapter
parses the grouping and serializes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..gen.engine import fsa
from ..gen.engine.fsa_claim_report import GROUPINGS, ClaimGroup, claim_report
from ..gen.services.claims import accept_claim_links, claim_link_proposals
from ..presentation import claim_role_label, fsa_usage_text

if TYPE_CHECKING:
    from .context import Api
    from .resources import QueryParams


def _amount(value) -> str:
    return str(value.to_decimal())


def _group(group: ClaimGroup) -> dict[str, object]:
    return {
        "key": group.key,
        "label": group.label,
        "claims": group.claims,
        "net_paid": _amount(group.net_paid),
        "reimbursable": _amount(group.reimbursable),
        "reimbursed": _amount(group.reimbursed),
        "rejected": _amount(group.rejected),
        "remaining": _amount(group.remaining),
        "attention": group.attention,
    }


def fsa_dashboard(api: Api, query: QueryParams) -> dict[str, object]:
    """Benefit years, claims still open or needing attention, and claims grouped ``by``."""
    by = query.text("by") or "status"
    query.finish()
    if by not in GROUPINGS:
        from .resources import QueryError  # resources imports this module

        raise QueryError("query.invalid", ("by",))
    report = claim_report(api.db, by=by)
    return {
        "claims": [
            {
                "handle": line.handle,
                "service_date": line.summary.claim.service_date.isoformat(),
                "provider": line.summary.claim.provider,
                "account": line.account,
                "funding_year": line.funding_year,
                "deadline": line.deadline.isoformat() if line.deadline else None,
                "status": line.summary.status.label,
                "paid": _amount(line.summary.net_paid),
                "reimbursed": _amount(line.summary.reimbursed),
                "rejected": _amount(line.summary.rejected),
                "remaining": _amount(line.summary.remaining_reimbursable),
                "attention": [item.text for item in line.attention],
            }
            for line in report.lines
            if line.attention or not line.summary.status.settled
        ],
        "report": {
            "by": report.by,
            "groups": [_group(group) for group in report.groups],
            "totals": _group(report.totals),
        },
        "attention": len(report.needing_attention),
        # Statement lines that clearly belong on one claim (writes nothing).
        "proposals": [
            {
                "claim": item.claim,
                "claim_label": item.claim_label,
                "transaction": item.transaction,
                "split": item.split,
                "role": item.role,
                "role_label": claim_role_label(item.role),
                "date": item.when.isoformat(),
                "description": item.description,
                "amount": _amount(item.amount),
                "reason": item.reason,
            }
            for item in claim_link_proposals(api.db).value or ()
        ],
        "years": [
            {
                "account": api.db.full_name(status.account),
                "account_handle": status.account.handle,
                "start": status.year.start.isoformat(),
                "through": status.year.through.isoformat(),
                "runout_through": (
                    status.year.runout_through.isoformat() if status.year.runout_through else None
                ),
                "election": _amount(status.year.election),
                "funded": _amount(status.funded),
                "used": _amount(status.used),
                # The flows behind "used" (see engine/fsa_flows).
                "direct_payments": _amount(status.direct_payments),
                "reimbursements": _amount(status.reimbursements),
                "provider_refunds": _amount(status.provider_refunds),
                "repaid": _amount(status.repaid),
                "usage_text": fsa_usage_text(status),
                "remaining": _amount(status.remaining),
                "overage": _amount(status.overage),
                "carryover_limit": (
                    _amount(status.year.carryover_limit)
                    if status.year.carryover_limit is not None
                    else None
                ),
                "grace_through": (
                    status.year.grace_through.isoformat() if status.year.grace_through else None
                ),
                "carried_in": _amount(status.carried_in),
                "carried_over": _amount(status.carried_over),
                "forfeited": _amount(status.forfeited),
                "phase": status.phase,
            }
            for status in fsa.dashboard_statuses(api.db)
        ],
    }


def fsa_claim_links_accept(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Link the chosen claim-link proposals that are still on offer."""
    raw = payload.get("links")
    if not isinstance(raw, list) or not all(
        isinstance(item, list) and len(item) == 3 and all(isinstance(part, str) for part in item)
        for item in raw
    ):
        raise ValueError("links must be a list of [claim, transaction, split]")
    accepted = accept_claim_links(api.db, tuple((item[0], item[1], item[2]) for item in raw))
    assert accepted.value is not None
    return {"linked": accepted.value.linked, "unchanged": accepted.value.unchanged}
