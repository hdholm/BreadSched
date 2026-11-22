"""Pay a reconciled credit card statement: what is offered, and what saving does.

When a card's reconciliation finishes, the statement says what is owed and when.
:func:`card_payment_offer` proposes the payment, to be changed in the transaction
editor; :func:`pay_card_statement` saves it and settles the statement's payment
due in the same undoable change:

* A payment of the whole statement, or of at least its minimum, settles it. A
  payment short of the statement asks for the minimum once, and the statement
  keeps it.
* When a commitment schedule pays the card, the payment counts toward that
  schedule's occurrence, which then expects the statement balance; a settling
  payment closes it, and a payment below the minimum leaves the rest of the
  statement expected, in the due review as everywhere else.
* When the card's own terms make it due, the reconciled statement decides it
  (``engine.schedule.account_payment_definitions``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..db.sqlite import DbSQLite
from ..engine import card_payment
from ..engine.plan_allocations import account_classes, plan_legs
from ..lib.commodity import DEFAULT_CURRENCY, DEFAULT_CURRENCY_HANDLE, Commodity
from ..lib.money import Money
from ..lib.transaction import PlanAllocation
from .contracts import ServiceError, ServiceResult
from .transactions import (
    SavedTransaction,
    SaveTransaction,
    TransactionInput,
    build_transaction,
    transaction_currency,
)

__all__ = [
    "CardPaymentOffer",
    "PaidCardStatement",
    "PayCardStatement",
    "card_payment_offer",
    "pay_card_statement",
]


@dataclass(frozen=True, slots=True)
class CardPaymentOffer:
    """The payment a finished card statement proposes, before it is edited."""

    statement: str
    account: str
    account_name: str
    statement_date: date
    statement_balance: Money
    minimum_payment: Money | None
    #: Still owed of the statement (less what was paid since it); zero offers nothing.
    amount: Money
    due_date: date
    #: The date proposed for the payment: today, as it is usually made at once.
    post_date: date
    description: str
    pay_from: str | None
    pay_from_name: str | None
    #: The schedule whose occurrence the payment settles, if one pays the card.
    schedule: str | None
    schedule_name: str | None
    #: The currency the payment is entered in.
    currency: str = ""


@dataclass(frozen=True, slots=True)
class PayCardStatement:
    """Save ``definition`` as the payment of card statement ``statement``.

    ``minimum_payment`` is the statement's minimum, in the card's own terms; it is
    needed only for a payment short of the statement balance, and kept with the
    statement once given.
    """

    statement: str
    definition: TransactionInput
    minimum_payment: Money | None = None


@dataclass(frozen=True, slots=True)
class PaidCardStatement:
    transaction: SavedTransaction
    #: Whether the payment settled the statement's payment due.
    settled: bool
    paid: Money
    #: Still owed of the statement after the payment.
    remaining: Money
    minimum_payment: Money | None


def _due(db: DbSQLite, statement: str, today: date | None):
    found = db.get_reconciliation(statement)
    if found is None:
        return None, ServiceError("card.payment.statement.not_found", ("statement",))
    due = card_payment.statement_payment_due(db, found, today)
    if due is None:
        return None, ServiceError("card.payment.not_card", ("statement",))
    return (found, due), None


def card_payment_offer(
    db: DbSQLite, statement: str, today: date | None = None
) -> ServiceResult[CardPaymentOffer]:
    """The payment to propose once a card statement is reconciled. Its ``amount``
    is zero when the statement owes nothing more, and then nothing is offered."""
    found, error = _due(db, statement, today)
    if error is not None:
        return ServiceResult.failure(error)
    _statement, due = found
    account = db.get_account(due.account)
    assert account is not None
    name = db.full_name(account) or account.name
    paying = db.get_scheduled(due.schedule) if due.schedule is not None else None
    on = today or date.today()
    source = db.get_account(due.pay_from) if due.pay_from is not None else None
    return ServiceResult.success(
        CardPaymentOffer(
            statement=statement,
            account=due.account,
            account_name=name,
            statement_date=due.statement_date,
            statement_balance=due.statement_balance,
            minimum_payment=due.minimum_payment,
            amount=due.remaining,
            due_date=due.due_date,
            post_date=on,
            description=paying.description if paying is not None else f"{name} payment",
            pay_from=due.pay_from,
            pay_from_name=db.full_name(source) if source is not None else None,
            schedule=due.schedule,
            schedule_name=paying.name if paying is not None else None,
            currency=transaction_currency(db),
        )
    )


def pay_card_statement(
    db: DbSQLite, request: PayCardStatement, today: date | None = None
) -> ServiceResult[PaidCardStatement]:
    """Save the payment and settle the statement's payment due, as one change.

    Refused, and nothing saved, when the statement is not a completed card
    statement (``card.payment.statement.not_found``, ``card.payment.not_card``),
    when the entry pays nothing into the card (``card.payment.no_payment``), when
    a payment short of the statement has no minimum to compare with
    (``card.payment.minimum_needed``), or when the minimum given is not above zero
    or exceeds the statement balance (``card.payment.minimum_invalid``).
    """
    found, error = _due(db, request.statement, today)
    if error is not None:
        return ServiceResult.failure(error)
    statement, due = found
    built = build_transaction(db, SaveTransaction(request.definition))
    if built.value is None:
        return ServiceResult.failure(*built.errors)
    payment = built.value
    paid_now = payment.value_for(due.account)
    if paid_now <= 0:
        return ServiceResult.failure(ServiceError("card.payment.no_payment", ("splits",)))
    minimum = request.minimum_payment
    if minimum is not None and not (Money(0) < minimum <= due.statement_balance):
        return ServiceResult.failure(
            ServiceError("card.payment.minimum_invalid", ("minimum_payment",))
        )
    minimum = minimum if minimum is not None else due.minimum_payment
    paid = due.paid + paid_now
    if paid < due.statement_balance and minimum is None:
        return ServiceResult.failure(
            ServiceError("card.payment.minimum_needed", ("minimum_payment",))
        )
    threshold = min(minimum, due.statement_balance) if minimum is not None else None
    settled = paid >= due.statement_balance or (threshold is not None and paid >= threshold)

    scheduled = db.get_scheduled(due.schedule) if due.schedule is not None else None
    if scheduled is not None and due.occurrence is not None:
        # The payment counts toward the occurrence, which expects the statement.
        key = scheduled.occurrence_key(due.occurrence)
        class_of = account_classes(db)
        for split in plan_legs(payment, class_of):
            if split.account == due.account and split.value > 0:
                split.plan_allocations = [
                    PlanAllocation(key, split.value, due.occurrence, due.statement_balance)
                ]
        if settled and paid < due.statement_balance:
            # What is left is not expected of this occurrence: it is carried.
            scheduled.closed_occurrences = sorted({*scheduled.closed_occurrences, due.occurrence})
    if request.minimum_payment is not None:
        statement.minimum_payment = request.minimum_payment
    account = db.get_account(due.account)
    name = db.full_name(account) if account is not None else due.account
    with db.transaction(f"Pay the {name} statement") as txn:
        if (
            payment.currency == DEFAULT_CURRENCY_HANDLE
            and db.get_commodity(DEFAULT_CURRENCY_HANDLE) is None
        ):
            # A book with no currency yet gets the default, as any first entry does.
            db.add_commodity(Commodity.from_dict(DEFAULT_CURRENCY.serialize()), txn)
            db.set_metadata("default_currency", DEFAULT_CURRENCY_HANDLE, txn)
        db.add_transaction(payment, txn)
        if request.minimum_payment is not None:
            db.commit_reconciliation(statement, txn)
        if scheduled is not None:
            db.commit_scheduled(scheduled, txn)
    return ServiceResult.success(
        PaidCardStatement(
            SavedTransaction(payment.handle, payment.post_date, payment.description),
            settled,
            paid_now,
            max(due.statement_balance - paid, Money(0)),
            minimum,
        )
    )
