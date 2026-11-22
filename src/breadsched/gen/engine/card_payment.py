"""The card payment a reconciled credit card statement makes due.

When a card's statement is reconciled, what it owes and when it is due are known.
Its payment is due on the card's next payment day after the statement date, or on
the next open occurrence of a schedule that pays the card. A payment into the card
since the statement settles that due once it reaches the statement balance, or the
statement's minimum payment when one was given; below that, the rest of the
statement stays due (``docs/design/household-workflows.md``, Pay a card statement).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..db.sqlite import DbSQLite
from ..lib.account import Account, AccountType
from ..lib.money import Money
from ..lib.reconciliation import Reconciliation, ReconciliationStatus
from ..lib.recurrence import add_months
from ..lib.scheduled import ScheduledTransaction

__all__ = [
    "CardPaymentDue",
    "latest_statement",
    "paid_into_card",
    "paying_schedule",
    "statement_due_date",
    "statement_payment_due",
]


@dataclass(frozen=True, slots=True)
class CardPaymentDue:
    """What one reconciled card statement makes due, and what is paid toward it."""

    account: str
    statement: str
    statement_date: date
    #: What the statement says is owed (the reconciled ending balance).
    statement_balance: Money
    minimum_payment: Money | None
    due_date: date
    #: The account the card is normally paid from, if known.
    pay_from: str | None
    #: A commitment schedule that pays the card, and the occurrence this
    #: statement's payment settles; None when the card's own terms make it due.
    schedule: str | None
    occurrence: date | None
    #: Paid into the card since the statement date.
    paid: Money

    @property
    def remaining(self) -> Money:
        """What is still owed of the statement."""
        return max(self.statement_balance - self.paid, Money(0))

    @property
    def threshold(self) -> Money:
        """What settles the due: the minimum when given, else the whole statement."""
        if self.minimum_payment is None:
            return self.statement_balance
        return min(self.minimum_payment, self.statement_balance)

    @property
    def settled(self) -> bool:
        return self.statement_balance <= 0 or self.paid >= self.threshold

    @property
    def occurrence_key(self) -> str | None:
        if self.schedule is None or self.occurrence is None:
            return None
        return ScheduledTransaction.occurrence_key_for(self.schedule, self.occurrence)


def statement_due_date(account: Account, statement_date: date) -> date | None:
    """The card's first payment day after ``statement_date``, if it has one."""
    day = account.card.payment_day
    if day is None:
        return None
    month = date(statement_date.year, statement_date.month, 1)
    candidate = add_months(month, 0, day=day)
    return candidate if candidate > statement_date else add_months(month, 1, day=day)


def latest_statement(db: DbSQLite, account: str) -> Reconciliation | None:
    """The account's most recent completed statement, by statement date."""
    completed = [
        item
        for item in db.iter_reconciliations(account)
        if item.status is ReconciliationStatus.COMPLETED
    ]
    return max(completed, key=lambda item: item.statement_date, default=None)


def paid_into_card(
    db: DbSQLite,
    account: str,
    payment_account: str | None,
    after: date,
    through: date | None = None,
    *,
    excluding: str | None = None,
) -> Money:
    """What was paid into the card after ``after`` (through ``through``): money
    in from its payment account, or from any cash account when none is set."""
    total = Money(0)
    start = after + timedelta(days=1)
    for transaction in db.iter_transactions(account=account, start=start, end=through):
        if transaction.handle == excluding:
            continue
        value = transaction.value_for(account)
        if value <= 0:
            continue
        if payment_account is not None:
            if transaction.value_for(payment_account) < 0:
                total += value
            continue
        for split in transaction.splits:
            if split.account == account or split.value >= 0:
                continue
            source = db.get_account(split.account)
            if source is not None and source.atype.is_cash_like:
                total += value
                break
    return total


def paying_schedule(
    db: DbSQLite, account: str, on_or_after: date
) -> tuple[ScheduledTransaction, date] | None:
    """An enabled commitment that pays the card, and its first occurrence on or
    after ``on_or_after`` not yet posted, skipped, or closed."""
    from . import schedule as schedule_engine

    for scheduled in db.iter_scheduled():
        if not scheduled.enabled or not scheduled.usable or scheduled.placeholder:
            continue
        if not any(split.account == account for split in scheduled.splits):
            continue
        when = scheduled.recurrence.next_after(on_or_after - timedelta(days=1))
        while when is not None and (
            when in scheduled.skipped
            or when in scheduled.closed_occurrences
            or schedule_engine.already_posted(db, scheduled.handle, when)
        ):
            when = scheduled.recurrence.next_after(when)
        if when is None:
            continue
        if dict(scheduled.resolved_splits(when=when)).get(account, Money(0)) > 0:
            return scheduled, when
    return None


def statement_payment_due(
    db: DbSQLite,
    statement: Reconciliation,
    today: date | None = None,
    *,
    excluding: str | None = None,
) -> CardPaymentDue | None:
    """The payment a completed credit card statement makes due; None for another
    kind of account, or a statement not completed."""
    account = db.get_account(statement.account)
    if (
        account is None
        or account.atype is not AccountType.CREDIT
        or statement.status is not ReconciliationStatus.COMPLETED
    ):
        return None
    paying = paying_schedule(db, account.handle, statement.statement_date)
    pay_from = account.card.payment_account
    if paying is not None:
        scheduled, occurrence = paying
        due = occurrence
        if pay_from is None:
            pay_from = next(
                (
                    handle
                    for handle, amount in scheduled.resolved_splits(when=occurrence)
                    if amount < 0
                ),
                None,
            )
    else:
        due = statement_due_date(account, statement.statement_date) or (
            today or statement.statement_date
        )
    return CardPaymentDue(
        account=account.handle,
        statement=statement.handle,
        statement_date=statement.statement_date,
        statement_balance=statement.ending_balance,
        minimum_payment=statement.minimum_payment,
        due_date=due,
        pay_from=pay_from,
        schedule=paying[0].handle if paying is not None else None,
        occurrence=paying[1] if paying is not None else None,
        paid=paid_into_card(
            db,
            account.handle,
            account.card.payment_account,
            statement.statement_date,
            excluding=excluding,
        ),
    )
