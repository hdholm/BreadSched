"""Historical category activity turned into reviewable planning estimates.

The analyzer deliberately produces suggestions rather than silently changing the
book. Accepted suggestions become ordinary scheduled estimates, so Plan,
Projection, Review, and scenarios continue to consume one event model.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

from ..db.sqlite import DbSQLite
from ..lib.account import AccountClass
from ..lib.money import Money
from ..lib.recurrence import PeriodType, Recurrence
from ..lib.scenario import ScenarioSchedule
from ..lib.scheduled import ScheduledSplit, ScheduledTransaction

__all__ = [
    "HistoricalEstimateProposal",
    "accept_historical_estimate",
    "propose_historical_estimates",
]


@dataclass(frozen=True, slots=True)
class HistoricalEstimateProposal:
    """One category estimate inferred from closed historical months."""

    category: str
    category_name: str
    funding: str
    funding_name: str
    amount: Money
    recurrence: Recurrence
    sample_months: int
    active_months: int
    transaction_count: int
    confidence: float
    reason: str

    @property
    def source_name(self) -> str:
        """Display name of the account money historically flowed from."""
        return self.funding_name if self.amount >= 0 else self.category_name

    @property
    def destination_name(self) -> str:
        """Display name of the account money historically flowed to."""
        return self.category_name if self.amount >= 0 else self.funding_name

    @property
    def display_amount(self) -> Money:
        """Unsigned amount for UIs that display direction separately."""
        return abs(self.amount)


def _month_start(when: date) -> date:
    return when.replace(day=1)


def _add_months(when: date, months: int) -> date:
    index = when.year * 12 + when.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def _typical_amount(values: list[Money]) -> Money:
    decimals = sorted(value.to_decimal() for value in values)
    return Money(median(decimals)).quantize(100)


def _funding_account(db: DbSQLite, category: str, start: date, end: date) -> str | None:
    counts: Counter[str] = Counter()
    for txn in db.iter_transactions(account=category, start=start, end=end):
        for split in txn.splits:
            if split.account == category:
                continue
            account = db.get_account(split.account)
            if account is None or account.atype.is_flow or account.is_root:
                continue
            counts[split.account] += 1
    return counts.most_common(1)[0][0] if counts else None


def propose_historical_estimates(
    db: DbSQLite,
    *,
    as_of: date | None = None,
    months: int = 12,
    min_active_months: int = 3,
) -> list[HistoricalEstimateProposal]:
    """Propose monthly category estimates from completed historical months.

    This first conservative pass uses the median total of active months rather
    than a mean, which keeps one exceptional month from dominating the estimate.
    Categories need activity in at least ``min_active_months`` and a recognizable
    non-flow funding account. The proposal date is the first day of the next open
    month; users may refine recurrence and timing in the normal schedule editor.
    """
    if months < 1:
        raise ValueError("months of history must be positive")
    if min_active_months < 1:
        raise ValueError("minimum active months must be positive")

    today = as_of or date.today()
    current_month = _month_start(today)
    history_start = _add_months(current_month, -months)
    history_end = current_month - timedelta(days=1)
    proposals: list[HistoricalEstimateProposal] = []

    for account in db.iter_accounts():
        if account.is_root or account.placeholder:
            continue
        if account.account_class not in (AccountClass.INCOME, AccountClass.EXPENSE):
            continue

        monthly: list[Money] = []
        txn_count = 0
        for offset in range(months):
            start = _add_months(history_start, offset)
            end = _add_months(start, 1) - timedelta(days=1)
            total = Money(0)
            for txn in db.iter_transactions(account=account.handle, start=start, end=end):
                value = txn.value_for(account.handle) * account.sign()
                if value:
                    total = total + value
                    txn_count += 1
            if total:
                monthly.append(total)

        if len(monthly) < min_active_months:
            continue
        funding = _funding_account(db, account.handle, history_start, history_end)
        funding_account = db.get_account(funding) if funding else None
        if funding_account is None:
            continue

        amount = _typical_amount(monthly)
        active_ratio = len(monthly) / months
        confidence = min(0.95, 0.45 + active_ratio * 0.5)
        proposals.append(
            HistoricalEstimateProposal(
                category=account.handle,
                category_name=db.full_name(account),
                funding=funding_account.handle,
                funding_name=db.full_name(funding_account),
                amount=amount,
                recurrence=Recurrence(PeriodType.MONTH, start=current_month),
                sample_months=months,
                active_months=len(monthly),
                transaction_count=txn_count,
                confidence=confidence,
                reason=(
                    f"median of {len(monthly)} active month(s) across "
                    f"{months} completed month(s)"
                ),
            )
        )

    return sorted(proposals, key=lambda item: item.category_name)


def accept_historical_estimate(
    db: DbSQLite,
    proposal: HistoricalEstimateProposal,
    *,
    scenario_handle: str | None = None,
) -> str:
    """Persist a proposal as an ordinary Base or scenario estimate."""
    category = db.get_account(proposal.category)
    funding = db.get_account(proposal.funding)
    if category is None or funding is None:
        raise ValueError("proposal accounts no longer exist")
    signed = proposal.amount * category.sign()
    splits = [
        ScheduledSplit(category.handle, signed),
        ScheduledSplit(funding.handle, -signed),
    ]
    name = f"Estimated {proposal.category_name}"

    if scenario_handle is None:
        schedule = ScheduledTransaction(
            name=name,
            recurrence=proposal.recurrence,
            splits=splits,
            auto_create=False,
        )
        schedule.placeholder = True
        with db.transaction(f"Add historical estimate {proposal.category_name}") as txn:
            db.add_scheduled(schedule, txn)
        return schedule.handle

    scenario = db.get_scenario(scenario_handle)
    if scenario is None:
        raise ValueError("saved scenario no longer exists")
    schedule = ScenarioSchedule(
        name=name,
        recurrence=proposal.recurrence,
        splits=splits,
        placeholder=True,
    )
    scenario.schedule_overrides.append(schedule)
    with db.transaction(f"Add historical estimate to {scenario.name}") as txn:
        db.commit_scenario(scenario, txn)
    return schedule.handle
