"""Web FSA Dashboard: benefit years, open claims, and the claim report.

Availability comes from ``gen/engine/fsa`` and the claims, their totals, and the
reasons one needs attention from ``gen/engine/fsa_claim_report``; this adapter
parses the grouping and serializes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..gen.engine import fsa
from ..gen.engine.fsa_claim_report import GROUPINGS, ClaimGroup, claim_report
from ..gen.engine.fsa_claims import FsaClaimStatus

if TYPE_CHECKING:
    from .resources import QueryParams
    from .server import Api


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
            if line.attention or line.summary.status is not FsaClaimStatus.FULLY_REIMBURSED
        ],
        "report": {
            "by": report.by,
            "groups": [_group(group) for group in report.groups],
            "totals": _group(report.totals),
        },
        "attention": len(report.needing_attention),
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
                "remaining": _amount(status.remaining),
                "overage": _amount(status.overage),
                "forfeited": _amount(status.forfeited),
                "phase": status.phase,
            }
            for status in fsa.dashboard_statuses(api.db)
        ],
    }
