"""Turning schedules into ledger entries.

Posting is idempotent: an occurrence is identified by (schedule handle, date), and
``post_due`` skips any occurrence already present in the ledger.  That matters
because the natural trigger for posting is "whenever the book is opened", which on
a laptop that sleeps through a month boundary can easily happen twice in a minute.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..db.sqlite import DbSQLite
from ..lib.account import AccountType
from ..lib.base import create_handle
from ..lib.money import Money
from ..lib.recurrence import PeriodType, Recurrence, add_months
from ..lib.scheduled import ScheduledSplit, ScheduledTransaction
from ..lib.transaction import Transaction

__all__ = [
    "Occurrence",
    "AccountPaymentDefinition",
    "AccountPaymentOccurrence",
    "account_payment_definitions",
    "already_posted",
    "delete_definition",
    "due_occurrences",
    "duplicate_definition",
    "duplicate_saved_definition",
    "forecast_occurrences",
    "from_transaction",
    "post_due",
    "post_occurrences",
    "skip_occurrences",
    "upcoming_occurrences",
]


@dataclass(slots=True)
class Occurrence:
    """A single firing of a schedule, real or hypothetical."""

    schedule: ScheduledTransaction
    when: date
    amount: Money

    @property
    def name(self) -> str:
        return self.schedule.name

    def instantiate(self) -> Transaction:
        return self.schedule.instantiate(self.when)


@dataclass(slots=True)
class AccountPaymentDefinition:
    """A monthly card payment whose durable source is the account configuration.

    It deliberately is not a :class:`ScheduledTransaction`: the amount is derived
    from the live card balance, and saving a copied schedule would create a second
    source of truth. The definition can be inspected in Scheduled and Upcoming;
    editing it returns to the card account.
    """

    account: str
    name: str
    recurrence: Recurrence
    next_due: date
    amount_due: Money
    payment_account: str | None
    pays_in_full: bool
    handle: str
    description: str
    placeholder: bool = False
    auto_create: bool = False
    enabled: bool = True
    variables: dict[str, str] | None = None

    @property
    def splits(self) -> list[ScheduledSplit]:
        if self.payment_account is None or self.amount_due <= 0:
            return []
        return [
            ScheduledSplit(self.account, self.amount_due, memo="Card payment"),
            ScheduledSplit(self.payment_account, -self.amount_due, memo="Card payment"),
        ]

    def amount(self, *_args, **_kwargs) -> Money:
        return self.amount_due


@dataclass(slots=True)
class AccountPaymentOccurrence:
    """One informational firing of an account-linked card payment."""

    schedule: AccountPaymentDefinition
    when: date
    amount: Money

    @property
    def name(self) -> str:
        return self.schedule.name


def _card_accounts_covered_by_schedules(
    db: DbSQLite,
    schedules: list[ScheduledTransaction],
    as_of: date,
) -> set[str]:
    """Cards already paid by an enabled explicit schedule."""
    covered: set[str] = set()
    for scheduled in schedules:
        if not scheduled.enabled or not scheduled.usable:
            continue
        when = _representative_unresolved_occurrence(db, scheduled, as_of)
        if when is None:
            continue
        for handle, amount in scheduled.resolved_splits(when=when):
            account = db.get_account(handle)
            if account is not None and account.atype is AccountType.CREDIT and amount > 0:
                covered.add(handle)
    return covered


def _representative_unresolved_occurrence(
    db: DbSQLite,
    scheduled: ScheduledTransaction,
    as_of: date,
) -> date | None:
    """Return a date proving that a saved definition still covers the account.

    An unbounded or not-yet-finished definition has a current/future occurrence.
    A bounded definition only remains authoritative after its end while one of its
    occurrences is still genuinely pending. This prevents a completed one-time or
    finite schedule from suppressing the account-owned card bill forever.
    """
    current_or_future = scheduled.recurrence.next_after(as_of - timedelta(days=1))
    if current_or_future is not None:
        return current_or_future

    since = scheduled.last_posted + timedelta(days=1) if scheduled.last_posted else None
    for when in scheduled.recurrence.occurrences(as_of, since=since):
        if when in scheduled.skipped or already_posted(db, scheduled.handle, when):
            continue
        return when
    return None


def _payment_recorded(
    db: DbSQLite,
    account: str,
    payment_account: str | None,
    start: date,
    end: date,
) -> bool:
    """Whether an actual cash-to-card payment has occurred in this due window."""
    for transaction in db.iter_transactions(account=account, start=start, end=end):
        if transaction.value_for(account) <= 0:
            continue
        if payment_account is not None:
            if transaction.value_for(payment_account) < 0:
                return True
            continue
        for split in transaction.splits:
            if split.account == account or split.value >= 0:
                continue
            source = db.get_account(split.account)
            if source is not None and source.atype.is_cash_like:
                return True
    return False


def account_payment_definitions(
    db: DbSQLite,
    as_of: date | None = None,
    schedules: list[ScheduledTransaction] | None = None,
) -> list[AccountPaymentDefinition]:
    """Build account-owned card payment definitions without persisting duplicates."""
    today = as_of or date.today()
    saved = list(schedules) if schedules is not None else list(db.iter_scheduled())
    covered = _card_accounts_covered_by_schedules(db, saved, today)
    month = date(today.year, today.month, 1)
    definitions: list[AccountPaymentDefinition] = []
    from . import ledger

    for account in db.iter_accounts():
        if (
            account.atype is not AccountType.CREDIT
            or account.hidden
            or account.payment_day is None
            or account.handle in covered
        ):
            continue
        balance = ledger.balance_recursive(db, account.handle, as_of=today)
        amount = Money(0)
        if balance > 0:
            if account.pays_in_full:
                amount = balance
            elif account.usual_payment is not None and account.usual_payment > 0:
                amount = min(balance, account.usual_payment)

        candidate = add_months(month, 0, day=account.payment_day)
        if today >= candidate and _payment_recorded(
            db,
            account.handle,
            account.card_payment_account,
            add_months(candidate, -1, day=account.payment_day) + timedelta(days=1),
            today,
        ):
            candidate = add_months(month, 1, day=account.payment_day)
        recurrence = Recurrence(
            PeriodType.MONTH,
            start=candidate,
            day_of_month=account.payment_day,
        )
        name = f"{db.full_name(account) or account.name} payment"
        definitions.append(
            AccountPaymentDefinition(
                account=account.handle,
                name=name,
                recurrence=recurrence,
                next_due=candidate,
                amount_due=amount,
                payment_account=account.card_payment_account,
                pays_in_full=account.pays_in_full,
                handle=f"account-payment:{account.handle}",
                description=name,
                variables={},
            )
        )
    return sorted(definitions, key=lambda item: item.name)


def upcoming_occurrences(
    db: DbSQLite,
    as_of: date | None = None,
    horizon_days: int = 30,
) -> list[Occurrence | AccountPaymentOccurrence]:
    """Saved due activity plus non-posting account-linked card payments."""
    today = as_of or date.today()
    found: list[Occurrence | AccountPaymentOccurrence] = list(
        due_occurrences(db, as_of=today, horizon_days=horizon_days)
    )
    horizon = today + timedelta(days=horizon_days)
    for definition in account_payment_definitions(db, as_of=today):
        if definition.amount_due <= 0 or definition.next_due > horizon:
            continue
        found.append(
            AccountPaymentOccurrence(
                schedule=definition,
                when=definition.next_due,
                amount=definition.amount_due,
            )
        )
    return sorted(found, key=lambda item: (item.when, item.name))


def duplicate_definition(source: ScheduledTransaction) -> ScheduledTransaction:
    """Copy a definition with independent identity and no completed-occurrence state."""
    duplicate = ScheduledTransaction.from_dict(source.serialize())
    duplicate.handle = create_handle()
    duplicate.gid = ""
    duplicate.change = 0
    duplicate.name = f"{source.name} copy"
    if duplicate.description == source.name:
        duplicate.description = duplicate.name
    duplicate.last_posted = None
    duplicate.skipped = []
    return duplicate


def duplicate_saved_definition(
    db: DbSQLite, handle: str, *, name: str | None = None
) -> ScheduledTransaction:
    """Persist an exact independent copy, including protected custom structure."""
    source = db.get_scheduled(handle)
    if source is None:
        raise KeyError(handle)
    duplicate = duplicate_definition(source)
    if name is not None:
        chosen = name.strip()
        if not chosen:
            raise ValueError("give the copied schedule a name")
        old_name = duplicate.name
        duplicate.name = chosen
        if duplicate.description == old_name:
            duplicate.description = chosen
    with db.transaction(f"Duplicate scheduled {source.name}") as txn:
        db.add_scheduled(duplicate, txn)
    return duplicate


def from_transaction(transaction: Transaction) -> ScheduledTransaction:
    """Build an unsaved one-time template from every financial leg of an actual."""
    return ScheduledTransaction(
        name=transaction.description or "Scheduled transaction",
        description=transaction.description,
        recurrence=Recurrence(PeriodType.ONCE, start=transaction.post_date),
        splits=[
            ScheduledSplit(
                split.account,
                split.value,
                memo=split.memo,
                planning_flow=split.planning_flow,
                investment_activity=split.investment_activity,
            )
            for split in transaction.splits
        ],
        currency=transaction.currency,
    )


def delete_definition(db: DbSQLite, handle: str) -> ScheduledTransaction:
    """Delete a baseline schedule without leaving live scenario references behind."""
    source = db.get_scheduled(handle)
    if source is None:
        raise KeyError(handle)
    scenarios = [
        scenario.name
        for scenario in db.iter_scenarios()
        if any(item.source_schedule == handle for item in scenario.schedule_overrides)
    ]
    if scenarios:
        names = ", ".join(sorted(scenarios))
        raise ValueError(
            f"remove this schedule's override from scenario(s) {names} before deleting it"
        )
    with db.transaction(f"Delete scheduled {source.name}") as txn:
        db.remove_scheduled(handle, txn)
    return source


def already_posted(db: DbSQLite, schedule_handle: str, when: date) -> bool:
    for txn in db.iter_transactions(start=when, end=when):
        if txn.scheduled_from == schedule_handle:
            return True
    return False


def due_occurrences(
    db: DbSQLite,
    as_of: date | None = None,
    horizon_days: int | None = None,
) -> list[Occurrence]:
    """Occurrences that should already have been posted, plus any within advance days.

    ``horizon_days`` overrides each schedule's own ``advance_days``, which is what
    the "show me the next 30 days" filter in the UI uses.
    """
    today = as_of or date.today()
    found: list[Occurrence] = []
    for sched in db.iter_scheduled():
        if not sched.usable:
            continue
        if not sched.enabled:
            continue
        advance = horizon_days if horizon_days is not None else sched.advance_days
        until = today + timedelta(days=advance)
        since = sched.last_posted + timedelta(days=1) if sched.last_posted else None
        for when in sched.upcoming(until, since=since):
            if already_posted(db, sched.handle, when):
                continue
            if when in sched.skipped:
                continue
            found.append(Occurrence(sched, when, sched.amount(when=when)))
    return sorted(found, key=lambda o: (o.when, o.name))


def post_due(
    db: DbSQLite,
    as_of: date | None = None,
    only_auto: bool = True,
    message: str = "Post scheduled transactions",
) -> list[Transaction]:
    """Write due occurrences into the ledger.  Returns what was actually posted."""
    today = as_of or date.today()
    posted: list[Transaction] = []
    candidates = [
        occ
        for occ in due_occurrences(db, as_of=today, horizon_days=0)
        # A placeholder describes a planning expectation, not a transaction that
        # will happen; posting one would invent a ledger entry.
        if occ.when <= today
        and not occ.schedule.placeholder
        and (occ.schedule.auto_create or not only_auto)
    ]
    if not candidates:
        return posted

    with db.transaction(message) as txn:
        touched: dict[str, ScheduledTransaction] = {}
        for occ in candidates:
            real = occ.instantiate()
            db.add_transaction(real, txn)
            posted.append(real)
            sched = touched.setdefault(occ.schedule.handle, occ.schedule)
            if sched.last_posted is None or occ.when > sched.last_posted:
                sched.last_posted = occ.when
        for sched in touched.values():
            db.commit_scheduled(sched, txn)
    return posted


def post_occurrences(
    db: DbSQLite,
    chosen: list[Occurrence],
    message: str = "Post scheduled transactions",
) -> list[Transaction]:
    """Post exactly the occurrences given, and nothing else.

    The user chose these one at a time, so the engine posts them one at a time
    rather than re-deriving what is due — which could differ by the time they
    answered.
    """
    posted: list[Transaction] = []
    if not chosen:
        return posted
    with db.transaction(message) as txn:
        touched: dict[str, ScheduledTransaction] = {}
        for occurrence in chosen:
            real = occurrence.instantiate()
            db.add_transaction(real, txn)
            posted.append(real)
            sched = touched.setdefault(occurrence.schedule.handle, occurrence.schedule)
            if sched.last_posted is None or occurrence.when > sched.last_posted:
                sched.last_posted = occurrence.when
        for sched in touched.values():
            db.commit_scheduled(sched, txn)
    return posted


def skip_occurrences(
    db: DbSQLite,
    chosen: list[Occurrence],
    message: str = "Skip scheduled transactions",
) -> int:
    """Mark occurrences as dealt with without posting them."""
    if not chosen:
        return 0
    with db.transaction(message) as txn:
        touched: dict[str, ScheduledTransaction] = {}
        for occurrence in chosen:
            sched = touched.setdefault(occurrence.schedule.handle, occurrence.schedule)
            sched.skip(occurrence.when)
        for sched in touched.values():
            db.commit_scheduled(sched, txn)
    return len(chosen)


def forecast_occurrences(
    db: DbSQLite,
    start: date,
    end: date,
    include_disabled: bool = False,
) -> list[Occurrence]:
    """Every firing in a window, ignoring whether it has been posted.

    This is the projection view of schedules: it answers "what will happen", where
    :func:`due_occurrences` answers "what still needs recording".
    """
    found: list[Occurrence] = []
    for sched in db.iter_scheduled():
        if not sched.usable:
            continue
        if not sched.enabled and not include_disabled:
            continue
        for when in sched.recurrence.occurrences(end, since=start):
            if when in sched.skipped:
                continue
            found.append(Occurrence(sched, when, sched.amount(when=when)))
    return sorted(found, key=lambda o: (o.when, o.name))
