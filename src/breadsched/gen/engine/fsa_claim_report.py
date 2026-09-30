"""FSA claims grouped for reporting, and the claims that need attention.

:func:`claim_report` summarises every claim with :func:`fsa_claims.claim_summary`
and groups the results by FSA account, funding year, provider, or status, so the
desktop, browser, command line, and printed Dashboard show the same totals. It
never changes a claim.

A claim *needs attention* when something is left for the household to do:

- ``review`` — its figures disagree (refunds exceed payments, more reimbursed
  than the claim allows, allocation targets or a payer's share plus the EOB
  exceeding what was paid);
- ``eob`` — no EOB responsibility has been entered :data:`EOB_WAIT_DAYS` after
  the service date;
- ``deadline`` — money is still to be reimbursed and a funding year it draws on
  must be claimed within :data:`DEADLINE_WARNING_DAYS` (its run-out date, or the
  plan-year end when there is none);
- ``rejected`` — a reimbursement was rejected and money is still to be
  reimbursed, so it should be resubmitted or the claim closed.

A claim in several funding years is grouped by its first allocation's account
and year; its totals are claim-level and are never split between groups.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from ..db.sqlite import DbSQLite
from ..lib.money import Money
from .fsa_claims import FsaClaimStatus, FsaClaimSummary, allocation_year, claim_summary

__all__ = [
    "DEADLINE_WARNING_DAYS",
    "EOB_WAIT_DAYS",
    "GROUPINGS",
    "ClaimAttention",
    "ClaimGroup",
    "ClaimLine",
    "ClaimReport",
    "claim_report",
]

#: Days after the service date an EOB is normally expected by.
EOB_WAIT_DAYS = 30
#: Days before a funding year's claim deadline that an unclaimed balance is flagged.
DEADLINE_WARNING_DAYS = 30
#: How a report can group its claims.
GROUPINGS = ("status", "account", "year", "provider")

_STATUS_ORDER = {status: index for index, status in enumerate(FsaClaimStatus)}


@dataclass(frozen=True)
class ClaimAttention:
    """One reason a claim needs attention; ``code`` is stable, ``text`` is shown."""

    code: str
    text: str


@dataclass(frozen=True)
class ClaimLine:
    summary: FsaClaimSummary
    #: Full name of the first allocation's FSA account, or "" without one.
    account: str
    #: That allocation's funding year as "start – through", or "".
    funding_year: str
    #: The earliest claim deadline among the claim's funding years, if any.
    deadline: date | None
    attention: tuple[ClaimAttention, ...]

    @property
    def handle(self) -> str:
        return self.summary.claim.handle


@dataclass(frozen=True)
class ClaimGroup:
    key: str
    label: str
    claims: int
    net_paid: Money
    reimbursable: Money
    reimbursed: Money
    rejected: Money
    remaining: Money
    #: How many of the group's claims need attention.
    attention: int


@dataclass(frozen=True)
class ClaimReport:
    as_of: date
    by: str
    lines: tuple[ClaimLine, ...]
    groups: tuple[ClaimGroup, ...]

    @property
    def needing_attention(self) -> tuple[ClaimLine, ...]:
        return tuple(line for line in self.lines if line.attention)

    @property
    def totals(self) -> ClaimGroup:
        return _group("", "All claims", self.lines)


def _review_reasons(summary: FsaClaimSummary) -> list[str]:
    reasons = []
    if summary.net_paid < 0:
        reasons.append(
            f"Refunds exceed what was paid by {(-summary.net_paid).format()}; "
            "check the claim's payments and refunds"
        )
    shared = summary.shared
    if shared is not None and shared.over_allocated > 0:
        reasons.append(
            f"The payer's share and the EOB add up to {shared.over_allocated.format()} "
            "more than was paid"
        )
    if summary.reimbursed > summary.reimbursable and summary.reimbursable >= 0:
        excess = summary.reimbursed - summary.reimbursable
        reasons.append(f"Reimbursed {excess.format()} more than the claim allows")
    targets = [
        allocation.target
        for allocation in summary.claim.allocations
        if allocation.target is not None
    ]
    if targets and sum(targets, Money(0)) > summary.reimbursable:
        reasons.append("Funding-year targets exceed what can be reimbursed")
    return reasons or ["The claim's figures need checking"]


def _attention(
    db: DbSQLite, summary: FsaClaimSummary, deadlines: list[date], when: date
) -> tuple[ClaimAttention, ...]:
    found: list[ClaimAttention] = []
    status = summary.status
    if status is FsaClaimStatus.NEEDS_REVIEW:
        found.extend(ClaimAttention("review", text) for text in _review_reasons(summary))
    waited = (when - summary.claim.service_date).days
    if status is FsaClaimStatus.WAITING_EOB and waited > EOB_WAIT_DAYS:
        found.append(ClaimAttention("eob", f"No EOB entered {waited} days after the service"))
    open_money = summary.remaining_reimbursable > 0 and status in {
        FsaClaimStatus.OPEN,
        FsaClaimStatus.PARTIAL,
    }
    if open_money:
        upcoming = sorted(day for day in deadlines if 0 <= (day - when).days)
        if upcoming and (upcoming[0] - when).days <= DEADLINE_WARNING_DAYS:
            found.append(
                ClaimAttention(
                    "deadline",
                    f"Claim by {upcoming[0].isoformat()}: "
                    f"{summary.remaining_reimbursable.format()} still to reimburse",
                )
            )
        rejections = [
            rejection
            for allocation in summary.claim.allocations
            for rejection in allocation.rejections
        ]
        if rejections:
            last = max(rejections, key=lambda item: item.attempted_on)
            found.append(
                ClaimAttention(
                    "rejected",
                    f"{last.amount.format()} rejected on {last.attempted_on.isoformat()}"
                    + (f" ({last.reason})" if last.reason else "")
                    + ": resubmit, or close the claim",
                )
            )
    return tuple(found)


def _line(db: DbSQLite, summary: FsaClaimSummary, when: date) -> ClaimLine:
    account_name = year_label = ""
    deadlines: list[date] = []
    for index, allocation in enumerate(summary.claim.allocations):
        account = db.get_account(allocation.account)
        if account is None:
            continue
        year = allocation_year(db, allocation)
        deadlines.append(year.runout_through or year.through)
        if index == 0:
            account_name = db.full_name(account)
            year_label = f"{year.start.isoformat()} – {year.through.isoformat()}"
    return ClaimLine(
        summary=summary,
        account=account_name,
        funding_year=year_label,
        deadline=min(deadlines) if deadlines else None,
        attention=_attention(db, summary, deadlines, when),
    )


def _group(key: str, label: str, lines) -> ClaimGroup:
    lines = list(lines)
    total = Money(0)

    def add(values) -> Money:
        return sum(values, total)

    return ClaimGroup(
        key=key,
        label=label,
        claims=len(lines),
        net_paid=add(line.summary.net_paid for line in lines),
        reimbursable=add(line.summary.reimbursable for line in lines),
        reimbursed=add(line.summary.reimbursed for line in lines),
        rejected=add(line.summary.rejected for line in lines),
        remaining=add(line.summary.remaining_reimbursable for line in lines),
        attention=sum(1 for line in lines if line.attention),
    )


def _label(spellings: list[str]) -> str:
    """The spelling most claims in a group use; ties go to the first in sort order."""
    return min(set(spellings), key=lambda text: (-spellings.count(text), text))


def _key(line: ClaimLine, by: str) -> tuple[Any, str, str]:
    """(sort key, group key, label) for one claim under a grouping."""
    if by == "status":
        status = line.summary.status
        return _STATUS_ORDER[status], status.value, status.label
    if by == "account":
        label = line.account or "No FSA account"
        return (not line.account, label), label, label
    if by == "year":
        label = f"{line.account} {line.funding_year}".strip() or "No funding year"
        return (not line.account, label), label, label
    provider = line.summary.claim.provider.strip()
    label = provider or "No provider"
    return (not provider, label.casefold()), label.casefold(), label


def claim_report(
    db: DbSQLite,
    *,
    as_of: date | None = None,
    by: str = "status",
    account: str | None = None,
    funding_year: date | None = None,
    provider: str | None = None,
    status: FsaClaimStatus | None = None,
    attention_only: bool = False,
) -> ClaimReport:
    """Every claim (or those matching the filters), grouped ``by`` one of GROUPINGS.

    ``account`` and ``funding_year`` match any of a claim's allocations;
    ``provider`` matches ignoring case and surrounding space.
    """
    if by not in GROUPINGS:
        raise ValueError(f"group claims by one of {', '.join(GROUPINGS)}")
    when = as_of or date.today()
    wanted_provider = provider.strip().casefold() if provider is not None else None
    lines = []
    for claim in db.iter_fsa_claims():
        if account is not None and not any(a.account == account for a in claim.allocations):
            continue
        if funding_year is not None and not any(
            a.funding_year_start == funding_year for a in claim.allocations
        ):
            continue
        if wanted_provider is not None and claim.provider.strip().casefold() != wanted_provider:
            continue
        summary = claim_summary(db, claim, as_of=when)
        if status is not None and summary.status is not status:
            continue
        line = _line(db, summary, when)
        if attention_only and not line.attention:
            continue
        lines.append(line)
    lines.sort(key=lambda line: (line.summary.claim.service_date, line.handle))
    buckets: dict[str, tuple[Any, list[str], list[ClaimLine]]] = {}
    for line in lines:
        order, key, label = _key(line, by)
        bucket = buckets.setdefault(key, (order, [], []))
        bucket[1].append(label)
        bucket[2].append(line)
    groups = tuple(
        _group(key, _label(labels), members)
        for key, (_order, labels, members) in sorted(buckets.items(), key=lambda item: item[1][0])
    )
    return ClaimReport(as_of=when, by=by, lines=tuple(lines), groups=groups)
