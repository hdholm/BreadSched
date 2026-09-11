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

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum

from ..db.sqlite import DbSQLite
from ..lib.money import Money
from ..lib.scenario import OneOff, Scenario, ScenarioSchedule
from ..lib.scheduled import ScheduledTransaction
from ..lib.transaction import PlanningResolution, Transaction

__all__ = [
    "EventSource",
    "EventStatus",
    "MatchCandidate",
    "PlannedEvent",
    "PlannedSplit",
    "actualize_transaction",
    "event_by_key",
    "mark_unexpected",
    "match_candidates",
    "reject_candidate",
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

    def as_dict(self) -> dict[str, object]:
        return {"account": self.account, "amount": self.amount}


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

    @property
    def status(self) -> EventStatus:
        return (
            EventStatus.ACTUALIZED
            if self.actual_transaction is not None
            else EventStatus.EXPECTED
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
        if self.actual_amount is None:
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
            "variance": self.variance,
            "expected_splits": [split.as_dict() for split in self.expected_splits],
            "actual_splits": [split.as_dict() for split in self.actual_splits],
            "actual_transaction": self.actual_transaction,
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
    return tuple(PlannedSplit(split.account, split.value) for split in transaction.splits)


def _linked_actuals(db: DbSQLite) -> dict[str, Transaction]:
    """Return occurrence key -> actual, including transactions from older books."""
    linked: dict[str, Transaction] = {}
    for transaction in db.iter_transactions():
        key = transaction.planned_occurrence
        if key is None and transaction.scheduled_from is not None:
            key = ScheduledTransaction.occurrence_key_for(
                transaction.scheduled_from, transaction.post_date
            )
        if key is not None:
            linked.setdefault(key, transaction)
    return linked


def _scheduled_event(
    schedule: ScheduledTransaction,
    when: date,
    actual: Transaction | None,
) -> PlannedEvent:
    expected = tuple(
        PlannedSplit(account, amount)
        for account, amount in schedule.resolved_splits(when=when)
    )
    expected_amount = _positive_total(expected)
    if actual is None:
        return PlannedEvent(
            key=schedule.occurrence_key(when),
            planned_date=when,
            source=EventSource.SCHEDULED,
            source_handle=schedule.handle,
            description=schedule.description,
            expected_splits=expected,
            expected_amount=expected_amount,
            placeholder=schedule.placeholder,
        )

    actual_splits = _transaction_splits(actual)
    if actual.planned_amount is not None:
        expected_amount = actual.planned_amount
    actual_amount = _positive_total(actual_splits)
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
        actual_amount=actual_amount,
    )


def scheduled_events(
    db: DbSQLite,
    start: date,
    end: date,
    *,
    budget_handle: str | None = None,
    include_disabled: bool = False,
    include_actualized: bool = True,
    exclude_handles: set[str] | None = None,
) -> list[PlannedEvent]:
    """Generate recurring plan events in chronological order.

    Skipped dates are deliberately absent from the plan.  Posted/matched dates are
    retained as actualized events so historical budget-vs-actual reporting can
    preserve both the original estimate and the ledger fact.
    """
    linked = _linked_actuals(db) if include_actualized else {}
    found: list[PlannedEvent] = []
    excluded = exclude_handles or set()
    for schedule in db.iter_scheduled():
        if schedule.handle in excluded:
            continue
        if not schedule.enabled and not include_disabled:
            continue
        if not schedule.in_budget(budget_handle):
            continue
        for when in schedule.recurrence.occurrences(end, since=start):
            if when in schedule.skipped:
                continue
            key = schedule.occurrence_key(when)
            actual = linked.get(key) if include_actualized else None
            found.append(_scheduled_event(schedule, when, actual))
    return sorted(found, key=lambda item: (item.when, item.planned_date, item.key))


def _scenario_schedule_event(
    scenario: Scenario,
    schedule: ScenarioSchedule,
    when: date,
    actual: Transaction | None,
) -> PlannedEvent:
    expected = tuple(
        PlannedSplit(account, amount) for account, amount in schedule.resolved_splits(when)
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
            actual.planned_amount
            if actual.planned_amount is not None
            else expected_amount
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
    linked = _linked_actuals(db)
    found: list[PlannedEvent] = []
    for schedule in scenario.schedule_overrides:
        if not schedule.enabled:
            continue
        for when in schedule.recurrence.occurrences(end, since=start):
            if when in schedule.skipped:
                continue
            key = schedule.occurrence_key(scenario.handle, when)
            found.append(_scenario_schedule_event(scenario, schedule, when, linked.get(key)))
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
) -> list[PlannedEvent]:
    """All transaction-like planning events for a scenario, in event-date order."""
    replaced = {
        item.source_schedule
        for item in scenario.schedule_overrides
        if item.source_schedule is not None
    }
    events = scheduled_events(
        db,
        start,
        end,
        budget_handle=scenario.budget,
        exclude_handles=replaced,
    )
    events.extend(_scenario_scheduled_events(db, scenario, start, end))
    events.extend(
        _one_off_event(item, index)
        for index, item in enumerate(scenario.one_offs)
        if start <= item.when <= end
    )
    return sorted(events, key=lambda item: (item.when, item.planned_date, item.key))


def event_by_key(db: DbSQLite, key: str) -> PlannedEvent | None:
    """Resolve a stable scheduled-occurrence key without scanning an arbitrary horizon."""
    prefix = "scheduled:"
    if not key.startswith(prefix):
        return None
    payload = key[len(prefix):]
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
    actual = _linked_actuals(db).get(key)
    return _scheduled_event(schedule, when, actual)


def unresolved_events(
    db: DbSQLite,
    start: date,
    end: date,
    *,
    budget_handle: str | None = None,
) -> list[PlannedEvent]:
    """Expected scheduled occurrences that have not yet been resolved to an actual."""
    return [
        event
        for event in scheduled_events(db, start, end, budget_handle=budget_handle)
        if event.status is EventStatus.EXPECTED
    ]


def actualize_transaction(transaction: Transaction, event: PlannedEvent) -> Transaction:
    """Attach an actual transaction to one planned occurrence without losing estimate.

    The caller remains responsible for committing ``transaction``.  Snapshotting
    the expected amount means later edits to the schedule cannot rewrite history.
    """
    transaction.planned_occurrence = event.key
    transaction.planned_for = event.planned_date
    transaction.planned_amount = event.expected_amount
    transaction.planning_resolution = PlanningResolution.MATCHED
    transaction.rejected_plan_occurrences.clear()
    if event.source is EventSource.SCHEDULED:
        transaction.scheduled_from = event.source_handle
    return transaction


def reject_candidate(transaction: Transaction, event: PlannedEvent) -> Transaction:
    """Remember that ``event`` is not the plan occurrence resolved by ``transaction``."""
    if event.key not in transaction.rejected_plan_occurrences:
        transaction.rejected_plan_occurrences.append(event.key)
    return transaction


def mark_unexpected(transaction: Transaction) -> Transaction:
    """Explicitly classify an actual transaction as having no planned occurrence."""
    transaction.scheduled_from = None
    transaction.planned_occurrence = None
    transaction.planned_for = None
    transaction.planned_amount = None
    transaction.planning_resolution = PlanningResolution.UNEXPECTED
    transaction.rejected_plan_occurrences.clear()
    return transaction


def match_candidates(
    db: DbSQLite,
    transaction: Transaction,
    *,
    window_days: int = 7,
    budget_handle: str | None = None,
) -> list[MatchCandidate]:
    """Rank unresolved schedule occurrences near ``transaction`` conservatively.

    Candidates must share at least one account. Ranking is deterministic: closest
    date first, then closest gross amount, then the greatest account overlap.
    This intentionally avoids fuzzy/ML matching until the exact rules have earned
    user trust.
    """
    if transaction.planning_resolution is PlanningResolution.UNEXPECTED:
        return []
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
        budget_handle=budget_handle,
        include_actualized=True,
    ):
        if event.status is EventStatus.ACTUALIZED:
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
