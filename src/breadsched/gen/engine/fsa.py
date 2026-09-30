"""FSA benefit-year availability, separate from the custodial ledger balance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountType, FsaFundingYear
from ..lib.money import Money
from .fsa_flows import FsaFlowKind, classify

__all__ = ["FsaYearStatus", "dashboard_statuses", "previous_year", "year_status"]


@dataclass(frozen=True)
class FsaYearStatus:
    account: Account
    year: FsaFundingYear
    funded: Money
    used: Money
    remaining: Money
    overage: Money
    forfeited: Money
    as_of: date
    #: Money paid back into the FSA for this year (already taken off ``used``).
    repaid: Money = Money(0)
    #: Unused election carried in from the previous plan year, once its run-out
    #: has ended; already part of ``remaining``.
    carried_in: Money = Money(0)
    #: Unused election this closed year carries into the next; not forfeited.
    carried_over: Money = Money(0)
    #: The flows behind ``used`` (see :mod:`fsa_flows`): paid from the FSA card
    #: straight to a provider, paid out to a bank account, and credited back to
    #: the card by a provider. ``used`` is their net, less ``repaid``.
    direct_payments: Money = Money(0)
    reimbursements: Money = Money(0)
    provider_refunds: Money = Money(0)

    @property
    def label(self) -> str:
        return f"{self.year.start.isoformat()} – {self.year.through.isoformat()}"

    @property
    def phase(self) -> str:
        if self.as_of <= self.year.through:
            return "current"
        runout = self.year.runout_through or self.year.through
        if self.as_of <= runout:
            return "run-out"
        return "closed"


def _belongs(split_year: date | None, post_date: date, year: FsaFundingYear) -> bool:
    if split_year is not None:
        return split_year == year.start and post_date <= (year.runout_through or year.through)
    return year.start <= post_date <= year.through


def year_status(
    db: DbSQLite,
    account: Account,
    year: FsaFundingYear,
    *,
    as_of: date | None = None,
) -> FsaYearStatus:
    """Return benefit availability for one election year.

    Payroll funding into the custodial account does not determine benefit
    availability. The election is available from the start of the plan year and
    qualified usage reduces it. A post-year claim can be assigned explicitly to
    the prior year with ``Split.fsa_year_start`` during the run-out window.

    Money paid back into the account for a funding year (a claim's repayment,
    whose split carries ``fsa_year_start``) is not payroll funding: it gives
    that much of the election back. So does a provider refund credited to the
    FSA card, while money moved between FSA accounts is neither funding nor use
    (see :mod:`fsa_flows`).

    A plan with a carryover limit carries up to that much of a year's unused
    election into the account's next funding year once the run-out ends; only
    the rest is forfeited. The next year's availability includes it from then.
    """
    when = as_of or date.today()
    totals = dict.fromkeys(FsaFlowKind, Money(0))
    end = min(when, year.runout_through or year.through)
    if end >= year.start:
        accounts = {item.handle: item for item in db.iter_accounts()}
        for transaction in db.iter_transactions(account=account.handle, start=year.start, end=end):
            for split in transaction.splits:
                if split.account != account.handle:
                    continue
                kind = classify(transaction, split, accounts)
                if kind is FsaFlowKind.FUNDING:
                    counted = year.start <= transaction.post_date <= year.through
                else:
                    counted = _belongs(split.fsa_year_start, transaction.post_date, year)
                if counted:
                    totals[kind] = totals[kind] + abs(split.value)
    funded = totals[FsaFlowKind.FUNDING]
    repaid = totals[FsaFlowKind.REPAYMENT]
    used = (
        totals[FsaFlowKind.DIRECT_PAYMENT]
        + totals[FsaFlowKind.REIMBURSEMENT]
        - totals[FsaFlowKind.PROVIDER_REFUND]
        - repaid
    )
    carried_in = _carried_in(db, account, year, when)
    remaining_raw = year.election + carried_in - used
    available = remaining_raw if remaining_raw > 0 else Money(0)
    overage = -remaining_raw if remaining_raw < 0 else Money(0)
    runout = year.runout_through or year.through
    closed = when > runout
    remaining = Money(0) if closed else available
    carried_over = Money(0)
    if closed and year.carryover_limit is not None:
        carried_over = min(available, year.carryover_limit)
    forfeited = available - carried_over if closed else Money(0)
    return FsaYearStatus(
        account,
        year,
        funded,
        used,
        remaining,
        overage,
        forfeited,
        when,
        repaid=repaid,
        carried_in=carried_in,
        carried_over=carried_over,
        direct_payments=totals[FsaFlowKind.DIRECT_PAYMENT],
        reimbursements=totals[FsaFlowKind.REIMBURSEMENT],
        provider_refunds=totals[FsaFlowKind.PROVIDER_REFUND],
    )


def previous_year(account: Account, year: FsaFundingYear) -> FsaFundingYear | None:
    """The account's funding year that ends last before ``year`` starts."""
    earlier = [item for item in account.fsa_years if item.through < year.start]
    return max(earlier, key=lambda item: item.through, default=None)


def _carried_in(db: DbSQLite, account: Account, year: FsaFundingYear, when: date) -> Money:
    prior = previous_year(account, year)
    if prior is None or prior.carryover_limit is None:
        return Money(0)
    closes = prior.runout_through or prior.through
    if when <= closes:
        # The carryover is known only once the previous year's claims are in.
        return Money(0)
    left = year_status(db, account, prior, as_of=closes).remaining
    return min(left, prior.carryover_limit)


def dashboard_statuses(
    db: DbSQLite,
    *,
    as_of: date | None = None,
    recent_closed: int = 1,
) -> list[FsaYearStatus]:
    """Open years plus at most one recently closed year per account by default.

    A closed year is recent through 90 days after its inclusive run-out deadline
    (or plan-year end when no run-out is defined). Older history remains available
    through :func:`year_status` but does not belong on the Dashboard.
    """
    when = as_of or date.today()
    statuses: list[FsaYearStatus] = []
    for account in db.iter_accounts():
        if account.atype is not AccountType.FSA or account.hidden:
            continue
        account_statuses = [
            year_status(db, account, year, as_of=when)
            for year in sorted(account.fsa_years, key=lambda item: item.start, reverse=True)
        ]
        active = [
            status
            for status in account_statuses
            if status.year.start <= when and status.phase != "closed"
        ]
        closed = [
            status
            for status in account_statuses
            if status.phase == "closed"
            and (when - (status.year.runout_through or status.year.through)).days <= 90
        ]
        closed.sort(
            key=lambda status: status.year.runout_through or status.year.through,
            reverse=True,
        )
        statuses.extend(reversed(active))
        statuses.extend(closed[:recent_closed])
    return statuses
