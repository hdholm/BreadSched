"""Event-domain planning: expected occurrences, actualization, and matching.

Book schedules and scenario-specific schedule estimates are the authoritative
sources of recurring plan events. A ``PlannedEvent`` preserves the estimate
that was in force for an occurrence while
also linking an actual ledger transaction when one has been posted or matched.
Reporting periods are deliberately absent from this module: callers may group the
chronological stream by month, quarter, year, or any other display period without
changing the dates on which financial state changes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum

from ..db.sqlite import DbSQLite
from ..lib.account import AccountClass
from ..lib.money import Money
from ..lib.scenario import OneOff, Scenario, ScenarioSchedule
from ..lib.scheduled import ScheduledTransaction
from ..lib.transaction import (
    InvestmentActivityKind,
    PlanningFlowKind,
    PlanningResolution,
    Transaction,
)
from .currency import reporting_currency_handle
from .plan_allocations import (
    OccurrenceActual,
    allocate_whole,
    clear_allocations,
    occurrence_actuals,
)
from .reimbursement_outlook import scenario_receipts

__all__ = [
    "EventSource",
    "EventStatus",
    "MatchCandidate",
    "PlannedEvent",
    "PlannedSplit",
    "actualize_transaction",
    "event_by_key",
    "linked_actuals",
    "mark_unexpected",
    "match_candidates",
    "occurrence_actuals",
    "period_end",
    "remainder_event",
    "reject_candidate",
    "skip_occurrence",
    "unresolved_events",
    "scenario_events",
    "scheduled_events",
]


class EventSource(str, Enum):
    """Where a planned event came from."""

    SCHEDULED = "scheduled"
    ONE_OFF = "one_off"
    SCENARIO_SCHEDULE = "scenario_schedule"


class EventStatus(str, Enum):
    """Whether an expectation is still prospective or has a ledger actual."""

    EXPECTED = "expected"
    ACTUALIZED = "actualized"


@dataclass(frozen=True, slots=True)
class PlannedSplit:
    """One expected or actual leg of a planning event."""

    account: str
    amount: Money
    planning_flow: PlanningFlowKind | None = None
    investment_activity: InvestmentActivityKind | None = None
    amount_source: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "account": self.account,
            "amount": self.amount,
            "planning_flow": (self.planning_flow.value if self.planning_flow is not None else None),
            "investment_activity": (
                self.investment_activity.value if self.investment_activity is not None else None
            ),
            "amount_source": self.amount_source,
        }


@dataclass(frozen=True, slots=True)
class PlannedEvent:
    """One dated financial expectation, optionally resolved to an actual.

    ``planned_date`` never changes, even when the actual transaction lands on a
    nearby day.  ``when`` is the date on which projection state should change:
    the actual posting date when resolved, otherwise the planned date.
    """

    key: str
    planned_date: date
    source: EventSource
    description: str
    expected_splits: tuple[PlannedSplit, ...]
    expected_amount: Money
    source_handle: str | None = None
    placeholder: bool = False
    funded_from_cash: bool = False
    actual_transaction: str | None = None
    actual_date: date | None = None
    actual_splits: tuple[PlannedSplit, ...] = ()
    actual_amount: Money | None = None
    expected_currency: str | None = None
    actual_currency: str | None = None
    # Original currency when a report converted the expected values for display.
    converted_from: str | None = None
    #: Every transaction counting toward this occurrence (decision 0005); the
    #: ``actual_*`` fields above describe the first of them.
    actuals: tuple[OccurrenceActual, ...] = ()
    #: No more is expected: a commitment paid or matched as a whole, an estimate
    #: whose period has ended, or one closed early.
    closed: bool = False
    #: Closed early by the user (Close occurrence), not by being met or lapsing.
    closed_early: bool = False
    #: For a remainder event: the occurrence it is what is left of.
    remainder_of: str | None = None

    @property
    def matchable(self) -> bool:
        """Whether an actual may still be matched to it: any open occurrence, and an
        estimate whose period has ended, for a late purchase (decision 0005)."""
        return not self.closed or (self.placeholder and not self.closed_early)

    @property
    def applied_amount(self) -> Money:
        """What the actuals counting toward it add up to."""
        return self.actual_amount if self.actual_amount is not None else Money(0)

    @property
    def remaining_amount(self) -> Money:
        """Expected less applied: negative when more was spent than planned."""
        return self.expected_amount - self.applied_amount

    @property
    def status(self) -> EventStatus:
        return (
            EventStatus.ACTUALIZED if self.actual_transaction is not None else EventStatus.EXPECTED
        )

    @property
    def when(self) -> date:
        return self.actual_date or self.planned_date

    @property
    def splits(self) -> tuple[PlannedSplit, ...]:
        return self.actual_splits or self.expected_splits

    @property
    def amount(self) -> Money:
        return self.actual_amount if self.actual_amount is not None else self.expected_amount

    @property
    def variance(self) -> Money | None:
        if self.actual_amount is None or (
            self.expected_currency is not None
            and self.actual_currency is not None
            and self.expected_currency != self.actual_currency
        ):
            return None
        return self.actual_amount - self.expected_amount

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "planned_date": self.planned_date,
            "when": self.when,
            "source": self.source.value,
            "source_handle": self.source_handle,
            "description": self.description,
            "status": self.status.value,
            "expected_amount": self.expected_amount,
            "actual_amount": self.actual_amount,
            "expected_currency": self.expected_currency,
            "actual_currency": self.actual_currency,
            "converted_from": self.converted_from,
            "variance": self.variance,
            "expected_splits": [split.as_dict() for split in self.expected_splits],
            "actual_splits": [split.as_dict() for split in self.actual_splits],
            "amount_explanations": [
                {
                    "account": split.account,
                    "amount": split.amount,
                    "source": split.amount_source,
                }
                for split in self.expected_splits
            ],
            "actual_transaction": self.actual_transaction,
            "actual_transactions": [item.transaction.handle for item in self.actuals],
            "applied_amount": self.applied_amount,
            "remaining_amount": self.remaining_amount,
            "closed": self.closed,
            "placeholder": self.placeholder,
        }


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    """A deterministic possible resolution of a real transaction to an estimate."""

    event: PlannedEvent
    date_distance: int
    amount_difference: Money
    common_accounts: int

    @property
    def sort_key(self) -> tuple[int, Money, int, date, str]:
        return (
            self.date_distance,
            self.amount_difference,
            -self.common_accounts,
            self.event.planned_date,
            self.event.key,
        )


def _positive_total(splits: tuple[PlannedSplit, ...]) -> Money:
    total = Money(0)
    for split in splits:
        if split.amount > 0:
            total = total + split.amount
    return total


def _transaction_splits(transaction: Transaction) -> tuple[PlannedSplit, ...]:
    return tuple(
        PlannedSplit(
            split.account,
            split.value,
            split.planning_flow,
            split.investment_activity,
            "actual ledger split",
        )
        for split in transaction.splits
    )


def period_end(schedule: ScheduledTransaction, when: date) -> date:
    """The last day of an estimate's period: the day before its next occurrence, or
    for a last occurrence its end date (its own date when it has none)."""
    following = schedule.recurrence.next_after(when)
    while following is not None and following in schedule.skipped:
        following = schedule.recurrence.next_after(following)
    if following is not None:
        return following - timedelta(days=1)
    end = schedule.recurrence.end
    return end if end is not None and end >= when else when


def _closed(
    schedule: ScheduledTransaction,
    when: date,
    actuals: tuple[OccurrenceActual, ...],
    expected_amount: Money,
    applied: Money,
    today: date,
) -> bool:
    """Whether an occurrence expects no more (decision 0005, section 2)."""
    if when in schedule.closed_occurrences:
        return True
    if schedule.placeholder:
        return period_end(schedule, when) < today
    return bool(actuals) and (
        any(item.whole for item in actuals) or (expected_amount > 0 and applied >= expected_amount)
    )


def _scheduled_event(
    schedule: ScheduledTransaction,
    when: date,
    actuals: tuple[OccurrenceActual, ...],
    reporting_currency: str,
    today: date | None = None,
) -> PlannedEvent:
    expected = tuple(
        PlannedSplit(
            account,
            amount,
            split.planning_flow,
            split.investment_activity,
            source,
        )
        for split, (account, amount), source in zip(
            schedule.splits,
            schedule.resolved_splits(when=when),
            schedule.resolved_split_sources(when),
            strict=True,
        )
    )
    expected_amount = _positive_total(expected)
    on = today or date.today()
    if not actuals:
        return PlannedEvent(
            key=schedule.occurrence_key(when),
            planned_date=when,
            source=EventSource.SCHEDULED,
            source_handle=schedule.handle,
            description=schedule.description,
            expected_splits=expected,
            expected_amount=expected_amount,
            expected_currency=schedule.currency or reporting_currency,
            placeholder=schedule.placeholder,
            closed=_closed(schedule, when, (), expected_amount, Money(0), on),
            closed_early=when in schedule.closed_occurrences,
        )

    first = actuals[0]
    actual = first.transaction
    if first.planned_amount is not None:
        expected_amount = first.planned_amount
    actual_splits = tuple(
        PlannedSplit(
            split.account,
            split.value * item.share,
            split.planning_flow,
            split.investment_activity,
            "actual ledger split",
        )
        for item in actuals
        for split in item.transaction.splits
    )
    applied = sum((item.amount for item in actuals), Money(0))
    return PlannedEvent(
        key=schedule.occurrence_key(when),
        planned_date=when,
        source=EventSource.SCHEDULED,
        source_handle=schedule.handle,
        description=schedule.description,
        expected_splits=expected,
        expected_amount=expected_amount,
        placeholder=schedule.placeholder,
        actual_transaction=actual.handle,
        actual_date=actual.post_date,
        actual_splits=actual_splits,
        actual_amount=applied,
        expected_currency=schedule.currency or reporting_currency,
        actual_currency=actual.currency or reporting_currency,
        actuals=actuals,
        closed=_closed(schedule, when, actuals, expected_amount, applied, on),
        closed_early=when in schedule.closed_occurrences,
    )


def remainder_event(event: PlannedEvent) -> PlannedEvent | None:
    """What is still expected of a partly met, open occurrence, as an expected event.

    Projection counts it where it would otherwise count the whole estimate or
    nothing: an estimate half spent mid-month still expects its other half. It is
    dated on the occurrence's date, or its latest actual's when that is later, and
    its legs are the expected legs scaled to what remains.
    """
    if (
        not event.actuals
        or event.closed
        or event.expected_amount <= 0
        or event.remaining_amount <= 0
        or event.variance is None
    ):
        return None
    share = event.remaining_amount / event.expected_amount
    latest = max(item.transaction.post_date for item in event.actuals)
    return PlannedEvent(
        key=f"{event.key}#remaining",
        planned_date=max(event.planned_date, latest),
        source=event.source,
        source_handle=event.source_handle,
        description=event.description,
        expected_splits=tuple(
            PlannedSplit(
                split.account,
                split.amount * share,
                split.planning_flow,
                split.investment_activity,
                "what remains of the occurrence",
            )
            for split in event.expected_splits
        ),
        expected_amount=event.remaining_amount,
        expected_currency=event.expected_currency,
        placeholder=event.placeholder,
        funded_from_cash=event.funded_from_cash,
        remainder_of=event.key,
    )


def scheduled_events(
    db: DbSQLite,
    start: date,
    end: date,
    *,
    include_disabled: bool = False,
    include_actualized: bool = True,
    exclude_handles: set[str] | None = None,
    remainders: bool = False,
    today: date | None = None,
) -> list[PlannedEvent]:
    """Generate recurring plan events in chronological order.

    Skipped dates are deliberately absent from the plan.  Posted/matched dates are
    retained as actualized events so historical plan-vs-actual reporting can
    preserve both the original estimate and the ledger fact. ``remainders`` adds,
    after each partly met open occurrence, what is still expected of it
    (:func:`remainder_event`), for Projection; ``today`` decides which estimates'
    periods have ended.
    """
    linked = occurrence_actuals(db) if include_actualized else {}
    reporting = reporting_currency_handle(db)
    found: list[PlannedEvent] = []
    excluded = exclude_handles or set()
    for schedule in db.iter_scheduled():
        if schedule.handle in excluded:
            continue
        if not schedule.usable:
            continue
        if not schedule.enabled and not include_disabled:
            continue
        for when in schedule.recurrence.occurrences(end, since=start):
            if when in schedule.skipped:
                continue
            key = schedule.occurrence_key(when)
            event = _scheduled_event(schedule, when, tuple(linked.get(key, ())), reporting, today)
            found.append(event)
            if remainders and (rest := remainder_event(event)) is not None:
                found.append(rest)
    return sorted(found, key=lambda item: (item.when, item.planned_date, item.key))


def _scenario_schedule_event(
    scenario: Scenario,
    schedule: ScenarioSchedule,
    when: date,
    actual: Transaction | None,
) -> PlannedEvent:
    expected = tuple(
        PlannedSplit(
            account,
            amount,
            split.planning_flow,
            split.investment_activity,
            source,
        )
        for split, (account, amount), source in zip(
            schedule.splits,
            schedule.resolved_splits(when),
            schedule.resolved_split_sources(when),
            strict=True,
        )
    )
    expected_amount = _positive_total(expected)
    key = schedule.occurrence_key(scenario.handle, when)
    if actual is None:
        return PlannedEvent(
            key=key,
            planned_date=when,
            source=EventSource.SCENARIO_SCHEDULE,
            source_handle=schedule.handle,
            description=schedule.description,
            expected_splits=expected,
            expected_amount=expected_amount,
            placeholder=schedule.placeholder,
        )

    actual_splits = _transaction_splits(actual)
    return PlannedEvent(
        key=key,
        planned_date=when,
        source=EventSource.SCENARIO_SCHEDULE,
        source_handle=schedule.handle,
        description=schedule.description,
        expected_splits=expected,
        expected_amount=(
            actual.planned_amount if actual.planned_amount is not None else expected_amount
        ),
        placeholder=schedule.placeholder,
        actual_transaction=actual.handle,
        actual_date=actual.post_date,
        actual_splits=actual_splits,
        actual_amount=_positive_total(actual_splits),
    )


def _scenario_scheduled_events(
    db: DbSQLite,
    scenario: Scenario,
    start: date,
    end: date,
) -> list[PlannedEvent]:
    linked = occurrence_actuals(db)
    found: list[PlannedEvent] = []
    for schedule in scenario.schedule_overrides:
        if not schedule.enabled:
            continue
        for when in schedule.recurrence.occurrences(end, since=start):
            if when in schedule.skipped:
                continue
            key = schedule.occurrence_key(scenario.handle, when)
            parts = linked.get(key)
            found.append(
                _scenario_schedule_event(
                    scenario, schedule, when, parts[0].transaction if parts else None
                )
            )
    return found


def _one_off_event(item: OneOff, index: int) -> PlannedEvent:
    split = PlannedSplit(item.account, item.amount)
    return PlannedEvent(
        key=f"one-off:{index}:{item.when.isoformat()}:{item.account}",
        planned_date=item.when,
        source=EventSource.ONE_OFF,
        source_handle=None,
        description=item.description or "One-off event",
        expected_splits=(split,),
        expected_amount=abs(item.amount),
        funded_from_cash=True,
    )


def scenario_events(
    db: DbSQLite,
    scenario: Scenario,
    start: date,
    end: date,
    *,
    remainders: bool = False,
) -> list[PlannedEvent]:
    """All transaction-like planning events for a scenario, in event-date order.

    ``remainders`` adds what is still expected of partly met occurrences
    (:func:`scheduled_events`), as Projection counts it.
    """
    replaced = {
        item.source_schedule
        for item in scenario.schedule_overrides
        if item.source_schedule is not None
    }
    events = scheduled_events(
        db,
        start,
        end,
        exclude_handles=replaced,
        remainders=remainders,
    )
    events.extend(_scenario_scheduled_events(db, scenario, start, end))
    events.extend(
        _one_off_event(item, index)
        for index, item in enumerate(scenario.one_offs)
        if start <= item.when <= end
    )
    events.extend(goal_purchase_events(db, scenario, start, end))
    events.extend(receivable_receipt_events(db, start, end, scenario))
    return sorted(events, key=lambda item: (item.when, item.planned_date, item.key))


def receivable_receipt_events(
    db: DbSQLite, start: date, end: date, scenario: Scenario | None = None
) -> list[PlannedEvent]:
    """Reimbursements still expected, as one-off receipts on their expected dates.

    Each moves what the payer sends out of the receivable account into the cash
    account that paid the expense (``receivables.expected_receipt``), so the
    expected money counts as cash only from the day it is due. A disputed or overdue
    receivable is not counted on. A scenario may expect less, nothing, or another
    date (``reimbursement_outlook.scenario_receipts``); what it does not expect is
    written off on that date back into the expense, so the receivable still empties
    and the shortfall is a projected expense. These are expectations: they never
    post, and the actual reimbursement, once recorded, settles the receivable instead.
    """
    found: list[PlannedEvent] = []
    for item in scenario_receipts(db, scenario, start, end):
        receipt, receivable = item.receipt, item.receivable
        if receipt.expense_account is None:
            # A direct receivable (#378) has no expense to write off into: what a
            # scenario does not expect simply stays owed.
            if item.amount <= 0:
                continue
            splits = [
                PlannedSplit(receipt.cash_account, item.amount),
                PlannedSplit(receipt.account, -item.amount),
            ]
        else:
            splits = [PlannedSplit(receipt.account, -receipt.amount)]
            if item.amount > 0:
                splits.insert(0, PlannedSplit(receipt.cash_account, item.amount))
            if item.shortfall > 0:
                splits.append(PlannedSplit(receipt.expense_account, item.shortfall))
        description = (
            f"Expected from {receivable.payer}: {receivable.description}"
            if item.amount > 0
            else f"Not expected from {receivable.payer}: {receivable.description}"
        )
        found.append(
            PlannedEvent(
                key=f"receivable:{receivable.handle}:{item.when.isoformat()}",
                planned_date=item.when,
                source=EventSource.ONE_OFF,
                source_handle=None,
                description=description,
                expected_splits=tuple(splits),
                expected_amount=receipt.amount if receipt.expense_account else item.amount,
                placeholder=True,
            )
        )
    return found


def goal_purchase_events(
    db: DbSQLite, scenario: Scenario, start: date, end: date
) -> list[PlannedEvent]:
    """The purchases a scenario models for its savings goals, as one-off events.

    Each moves the goal's target (as the scenario sets it) out of the goal's account
    into the purchase account on the purchase date. It is a scenario estimate: it
    never posts, and like other one-offs its amount is not escalated.
    """
    found: list[PlannedEvent] = []
    for handle, override in scenario.goal_overrides.items():
        if override.excluded or not override.purchases:
            continue
        assert override.purchase_on is not None and override.purchase_account is not None
        goal = db.get_savings_goal(handle)
        if goal is None or not goal.is_open(override.purchase_on):
            continue
        if not start <= override.purchase_on <= end:
            continue
        amount = override.target_amount or goal.target_amount
        splits = (
            PlannedSplit(override.purchase_account, amount),
            PlannedSplit(goal.account, -amount),
        )
        found.append(
            PlannedEvent(
                key=f"goal-purchase:{scenario.handle}:{goal.handle}:{override.purchase_on}",
                planned_date=override.purchase_on,
                source=EventSource.ONE_OFF,
                source_handle=None,
                description=f"Buy: {goal.name}",
                expected_splits=splits,
                expected_amount=amount,
                placeholder=True,
            )
        )
    return found


def linked_actuals(db: DbSQLite) -> dict[str, list[OccurrenceActual]]:
    """Every transaction counting toward a scheduled occurrence, by occurrence key.

    Reading them scans the ledger, so a caller resolving many keys reads them once
    and passes the result to :func:`event_by_key`.
    """
    return occurrence_actuals(db)


def event_by_key(
    db: DbSQLite,
    key: str,
    linked: dict[str, list[OccurrenceActual]] | None = None,
    today: date | None = None,
) -> PlannedEvent | None:
    """Resolve a stable scheduled-occurrence key without scanning an arbitrary horizon.

    ``linked`` is :func:`linked_actuals` already read; without it the ledger is
    scanned for this one key.
    """
    prefix = "scheduled:"
    if not key.startswith(prefix):
        return None
    payload = key[len(prefix) :]
    try:
        handle, raw_date = payload.rsplit(":", 1)
        when = date.fromisoformat(raw_date)
    except ValueError:
        return None
    schedule = db.get_scheduled(handle)
    if schedule is None or when in schedule.skipped:
        return None
    if when not in schedule.recurrence.occurrences(when, since=when):
        return None
    parts = (linked if linked is not None else occurrence_actuals(db)).get(key, ())
    return _scheduled_event(schedule, when, tuple(parts), reporting_currency_handle(db), today)


def unresolved_events(
    db: DbSQLite,
    start: date,
    end: date,
) -> list[PlannedEvent]:
    """Scheduled occurrences that still expect something: not met, lapsed, or closed."""
    return [event for event in scheduled_events(db, start, end) if not event.closed]


def actualize_transaction(
    transaction: Transaction,
    event: PlannedEvent,
    class_of: Callable[[str], AccountClass | None] | None = None,
) -> Transaction:
    """Attach an actual transaction to one planned occurrence without losing estimate.

    The caller remains responsible for committing ``transaction``.  Snapshotting
    the expected amount means later edits to the schedule cannot rewrite history.
    Its plan legs are allocated to the occurrence in full (decision 0005);
    ``class_of`` (:func:`plan_allocations.account_classes`) says which legs are on
    income and expense accounts.
    """
    allocate_whole(
        transaction,
        event.key,
        event.planned_date,
        event.expected_amount,
        class_of or (lambda _handle: None),
    )
    transaction.planned_occurrence = event.key
    transaction.planned_for = event.planned_date
    transaction.planned_amount = event.expected_amount
    transaction.planning_resolution = PlanningResolution.MATCHED
    transaction.rejected_plan_occurrences.clear()
    planned_flows = {
        split.account: split.planning_flow
        for split in event.expected_splits
        if split.planning_flow is not None
    }
    planned_investment_activities = {
        split.account: split.investment_activity
        for split in event.expected_splits
        if split.investment_activity is not None
    }
    for split in transaction.splits:
        if split.planning_flow is None and split.account in planned_flows:
            split.planning_flow = planned_flows[split.account]
        if split.investment_activity is None and split.account in planned_investment_activities:
            split.investment_activity = planned_investment_activities[split.account]
    if event.source is EventSource.SCHEDULED:
        transaction.scheduled_from = event.source_handle
    return transaction


def reject_candidate(transaction: Transaction, event: PlannedEvent) -> Transaction:
    """Remember that ``event`` is not the plan occurrence resolved by ``transaction``."""
    if event.key not in transaction.rejected_plan_occurrences:
        transaction.rejected_plan_occurrences.append(event.key)
    return transaction


def skip_occurrence(db: DbSQLite, event: PlannedEvent) -> PlannedEvent:
    """Persist a per-occurrence skip on the event's owning schedule.

    Baseline scheduled events are updated in place. Scenario events are resolved
    by scenario and schedule handle so an alternate never mutates Base.
    """
    when = event.planned_date
    if event.source is EventSource.SCHEDULED:
        if event.source_handle is None:
            raise ValueError("scheduled event has no source handle")
        baseline_schedule = db.get_scheduled(event.source_handle)
        if baseline_schedule is None:
            raise ValueError("scheduled transaction no longer exists")
        baseline_schedule.skip(when)
        with db.transaction("Skip scheduled occurrence") as txn:
            db.commit_scheduled(baseline_schedule, txn)
        return event

    if event.source is EventSource.SCENARIO_SCHEDULE:
        for scenario in db.iter_scenarios():
            for scenario_schedule in scenario.schedule_overrides:
                if scenario_schedule.handle != event.source_handle:
                    continue
                if when not in scenario_schedule.skipped:
                    scenario_schedule.skipped.append(when)
                    scenario_schedule.skipped.sort()
                with db.transaction("Skip scenario scheduled occurrence") as txn:
                    db.commit_scenario(scenario, txn)
                return event
        raise ValueError("scenario scheduled transaction no longer exists")

    raise ValueError("this planned event cannot be skipped")


def mark_unexpected(transaction: Transaction) -> Transaction:
    """Explicitly classify an actual transaction as having no planned occurrence."""
    transaction.scheduled_from = None
    transaction.planned_occurrence = None
    transaction.planned_for = None
    transaction.planned_amount = None
    clear_allocations(transaction)
    transaction.planning_resolution = PlanningResolution.UNEXPECTED
    transaction.rejected_plan_occurrences.clear()
    return transaction


def match_candidates(
    db: DbSQLite,
    transaction: Transaction,
    *,
    window_days: int = 7,
) -> list[MatchCandidate]:
    """Rank unresolved schedule occurrences near ``transaction`` conservatively.

    Candidates must share at least one account. Ranking is deterministic: closest
    date first, then closest gross amount, then the greatest account overlap.
    This intentionally avoids fuzzy/ML matching until the exact rules have earned
    user trust.
    """
    if transaction.planning_resolution is PlanningResolution.UNEXPECTED:
        return []
    reporting_currency = reporting_currency_handle(db)
    actual_currency = transaction.currency or reporting_currency
    start = transaction.post_date - timedelta(days=window_days)
    end = transaction.post_date + timedelta(days=window_days)
    actual_splits = _transaction_splits(transaction)
    actual_amount = _positive_total(actual_splits)
    actual_accounts = {split.account for split in actual_splits}
    candidates: list[MatchCandidate] = []
    for event in scheduled_events(
        db,
        start,
        end,
        include_actualized=True,
    ):
        if not event.matchable or any(
            item.transaction.handle == transaction.handle for item in event.actuals
        ):
            continue
        if event.expected_currency != actual_currency:
            continue
        if event.key in transaction.rejected_plan_occurrences:
            continue
        expected_accounts = {split.account for split in event.expected_splits}
        common = len(actual_accounts & expected_accounts)
        if common == 0:
            continue
        candidates.append(
            MatchCandidate(
                event=event,
                date_distance=abs((transaction.post_date - event.planned_date).days),
                amount_difference=abs(actual_amount - event.expected_amount),
                common_accounts=common,
            )
        )
    return sorted(candidates, key=lambda candidate: candidate.sort_key)
