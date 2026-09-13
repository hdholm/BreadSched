"""FSA benefit-year availability, separate from the custodial ledger balance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountKind, FsaFundingYear
from ..lib.money import Money

__all__ = ["FsaYearStatus", "year_status", "dashboard_statuses"]


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
    """
    when = as_of or date.today()
    funded = Money(0)
    used = Money(0)
    end = min(when, year.runout_through or year.through)
    if end >= year.start:
        for transaction in db.iter_transactions(account=account.handle, start=year.start, end=end):
            for split in transaction.splits:
                if split.account != account.handle:
                    continue
                if split.value > 0 and year.start <= transaction.post_date <= year.through:
                    funded = funded + split.value
                elif split.value < 0 and _belongs(
                    split.fsa_year_start, transaction.post_date, year
                ):
                    used = used - split.value
    remaining_raw = year.election - used
    available = remaining_raw if remaining_raw > 0 else Money(0)
    overage = -remaining_raw if remaining_raw < 0 else Money(0)
    runout = year.runout_through or year.through
    closed = when > runout
    remaining = Money(0) if closed else available
    forfeited = available if closed else Money(0)
    return FsaYearStatus(account, year, funded, used, remaining, overage, forfeited, when)


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
        if account.kind is not AccountKind.FSA:
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
