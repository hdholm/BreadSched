"""What a schedule editor needs to show a definition without reading the book.

``engine.schedule.schedule_edit_projection`` decides how a definition can be edited
and which split is which; this view carries that projection with the definition
and the class of every account its splits use, so an editor lays the definition
out (and decides whether its simple form can show it) from the view alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import schedule as schedule_engine
from ..engine.schedule import ScheduleEditProjection
from ..lib.account import AccountClass
from ..lib.money import Money
from ..lib.scenario import Scenario, ScenarioSchedule
from ..lib.scheduled import ScheduledTransaction
from .contracts import ServiceError, ServiceResult
from .due_review import NextOccurrence, next_occurrence

__all__ = [
    "AccountChoice",
    "PaymentDefinitionLine",
    "ScenarioChangeView",
    "ScenarioEventsView",
    "ScheduleEditorView",
    "SchedulesView",
    "UpcomingLine",
    "editor_view",
    "schedule_draft",
    "schedule_editor",
    "schedule_layout",
    "scenario_events",
    "schedules_view",
]


@dataclass(frozen=True, slots=True)
class ScheduleEditorView:
    schedule: ScheduledTransaction
    projection: ScheduleEditProjection
    #: The class of each account the definition's splits use, by handle.
    classes: Mapping[str, AccountClass] = field(default_factory=dict)


def editor_view(db: DbSQLite, scheduled: ScheduledTransaction) -> ScheduleEditorView:
    classes = {}
    for split in scheduled.splits:
        account = db.get_account(split.account)
        if account is not None:
            classes[split.account] = account.account_class
    return ScheduleEditorView(
        scheduled, schedule_engine.schedule_edit_projection(db, scheduled), classes
    )


def schedule_editor(db: DbSQLite, handle: str) -> ServiceResult[ScheduleEditorView]:
    """A saved definition's editor view, to check an edit against (#358)."""
    scheduled = db.get_scheduled(handle)
    if scheduled is None:
        return ServiceResult.failure(ServiceError("schedule.not_found", ("schedule",)))
    return ServiceResult.success(editor_view(db, scheduled))


def schedule_layout(
    db: DbSQLite, definition: ScheduledTransaction
) -> ServiceResult[ScheduleEditorView]:
    """Any definition, saved or not (a duplicate, a draft), laid out for an editor."""
    return ServiceResult.success(editor_view(db, definition))


def schedule_draft(db: DbSQLite, transaction: str) -> ServiceResult[ScheduleEditorView]:
    """An unsaved one-time definition made from a recorded transaction, to review."""
    found = db.get_transaction(transaction)
    if found is None:
        return ServiceResult.failure(ServiceError("transaction.not_found", ("transaction",)))
    return ServiceResult.success(editor_view(db, schedule_engine.from_transaction(found)))


@dataclass(frozen=True, slots=True)
class AccountChoice:
    """An account a schedule may use, as the editors list it."""

    handle: str
    name: str
    account_class: AccountClass
    hidden: bool
    #: Whether investment activity applies to it (#383).
    investment: bool


@dataclass(frozen=True, slots=True)
class PaymentDefinitionLine:
    """A card's monthly payment, derived from the card's settings, not saved."""

    handle: str
    account: str
    name: str
    frequency: str
    amount_due: Money
    payment_account: str | None
    next_due: date
    split_accounts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UpcomingLine:
    when: date
    name: str
    amount: Money
    schedule: str
    #: A card payment derived from the card's settings, and that card.
    account_linked: bool = False
    linked_account: str | None = None


@dataclass(frozen=True, slots=True)
class SchedulesView:
    """Everything a list of scheduled transactions shows, read once (#358)."""

    definitions: tuple[ScheduleEditorView, ...]
    #: Each saved definition's next occurrence to enter or skip, by handle.
    next: Mapping[str, NextOccurrence | None]
    payments: tuple[PaymentDefinitionLine, ...]
    upcoming: tuple[UpcomingLine, ...]
    accounts: tuple[AccountChoice, ...]
    #: Full names of the accounts the definitions use, by handle.
    names: Mapping[str, str] = field(default_factory=dict)


def schedules_view(db: DbSQLite, days: int = 60) -> ServiceResult[SchedulesView]:
    """The saved definitions, card payments, and occurrences within ``days``."""
    if days < 0:
        return ServiceResult.failure(ServiceError("schedules.days.invalid", ("days",)))
    saved = list(db.iter_scheduled())
    definitions = tuple(editor_view(db, item) for item in saved)
    payments = tuple(
        PaymentDefinitionLine(
            handle=item.handle,
            account=item.account,
            name=item.name,
            frequency=item.recurrence.describe(),
            amount_due=item.amount_due,
            payment_account=item.payment_account,
            next_due=item.next_due,
            split_accounts=tuple(split.account for split in item.splits),
        )
        for item in schedule_engine.account_payment_definitions(db, schedules=saved)
    )
    upcoming = tuple(
        UpcomingLine(
            when=occurrence.when,
            name=occurrence.name,
            amount=occurrence.amount,
            schedule=occurrence.schedule.handle,
            account_linked=isinstance(
                occurrence.schedule, schedule_engine.AccountPaymentDefinition
            ),
            linked_account=getattr(occurrence.schedule, "account", None),
        )
        for occurrence in schedule_engine.upcoming_occurrences(db, horizon_days=days)
    )
    accounts = _account_choices(db)
    used = {split.account for item in saved for split in item.splits}
    names = {
        handle: db.full_name(account)
        for handle in used
        if (account := db.get_account(handle)) is not None
    }
    return ServiceResult.success(
        SchedulesView(
            definitions=definitions,
            next={item.handle: next_occurrence(db, item.handle) for item in saved},
            payments=payments,
            upcoming=upcoming,
            accounts=accounts,
            names=names,
        )
    )


@dataclass(frozen=True, slots=True)
class ScenarioChangeView:
    """One scenario's own estimate, laid out as an editor shows it."""

    schedule: ScenarioSchedule
    projection: ScheduleEditProjection
    classes: Mapping[str, AccountClass] = field(default_factory=dict)
    #: The baseline schedule it replaces, by name, when it replaces one.
    source_name: str | None = None


@dataclass(frozen=True, slots=True)
class ScenarioEventsView:
    """A scenario's estimates beside the baseline schedules they may replace (#358)."""

    scenario: Scenario
    accounts: tuple[AccountChoice, ...]
    baseline: tuple[ScheduleEditorView, ...]
    changes: tuple[ScenarioChangeView, ...]


def _account_choices(db: DbSQLite) -> tuple[AccountChoice, ...]:
    return tuple(
        AccountChoice(
            handle=account.handle,
            name=db.full_name(account),
            account_class=account.account_class,
            hidden=account.hidden,
            investment=account.atype.is_investment,
        )
        for account in sorted(
            (item for item in db.iter_accounts() if not item.is_root and not item.placeholder),
            key=db.full_name,
        )
    )


def scenario_events(db: DbSQLite, scenario: str) -> ServiceResult[ScenarioEventsView]:
    """A saved scenario's estimates, the baseline schedules, and the account choices."""
    found = db.get_scenario(scenario)
    if found is None:
        return ServiceResult.failure(ServiceError("scenario.not_found", ("scenario",)))
    changes = []
    for item in found.schedule_overrides:
        laid_out = editor_view(db, item)  # type: ignore[arg-type]
        source = db.get_scheduled(item.source_schedule) if item.source_schedule else None
        changes.append(
            ScenarioChangeView(
                item,
                laid_out.projection,
                laid_out.classes,
                source.name if source is not None else None,
            )
        )
    return ServiceResult.success(
        ScenarioEventsView(
            scenario=found,
            accounts=_account_choices(db),
            baseline=tuple(editor_view(db, item) for item in db.iter_scheduled()),
            changes=tuple(changes),
        )
    )
