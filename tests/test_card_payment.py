"""Paying a reconciled credit card statement (``services.card_payments``).

A finished card statement offers its payment. A payment of the whole statement,
or of at least its minimum, settles the payment due; one below the minimum leaves
the rest of the statement due. The card's own terms or a schedule that pays it
make the payment due, and both are settled the same way.
"""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.engine import reconciliation as reconcile
from breadsched.gen.engine import schedule as schedule_engine
from breadsched.gen.lib import (
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)
from breadsched.gen.lib.amount import Amount
from breadsched.gen.services import (
    PayCardStatement,
    ResolveDue,
    TransactionInput,
    TransactionSplitInput,
    card_payment_offer,
    pay_card_statement,
    resolve_due,
    transaction_currency,
)
from breadsched.gen.services.due_review import DueDecision

STATEMENT = date(2026, 1, 20)
DUE = date(2026, 2, 15)


@pytest.fixture
def card_book(db, book):
    """A card paid from checking on the 15th, with one $300 statement reconciled."""
    with db.transaction("Card terms") as txn:
        card = db.get_account(book.card)
        card.card.payment_day = 15
        card.card.payment_account = book.checking
        db.commit_account(card, txn)
        db.add_transaction(
            Transaction.simple(date(2026, 1, 5), "Groceries", book.groceries, book.card, "300"),
            txn,
        )
    opened = reconcile.start(db, book.card, STATEMENT, Money(300))
    state = reconcile.summary(db, opened)
    reconcile.update(db, opened.handle, split_handles=[item.split for item in state.candidates])
    statement = reconcile.complete(db, opened.handle)
    book.currency = transaction_currency(db)
    return book, statement


def _payment(book, amount: str, when: date = date(2026, 1, 25)) -> TransactionInput:
    value = Money(amount)
    return TransactionInput(
        post_date=when,
        description="Card payment",
        splits=(
            TransactionSplitInput(book.card, Amount(value, book.currency)),
            TransactionSplitInput(book.checking, Amount(-value, book.currency)),
        ),
        currency=book.currency,
    )


def _dues(db, as_of: date) -> dict[date, Money]:
    return {
        item.next_due: item.amount_due
        for item in schedule_engine.account_payment_definitions(db, as_of)
    }


class TestTheOffer:
    def test_a_finished_statement_offers_what_it_owes(self, db, card_book):
        book, statement = card_book
        offer = card_payment_offer(db, statement.handle, today=date(2026, 1, 21)).value
        assert offer is not None
        assert (offer.amount, offer.statement_balance, offer.due_date) == (
            Money(300),
            Money(300),
            DUE,
        )
        assert (offer.pay_from, offer.post_date, offer.schedule) == (
            book.checking,
            date(2026, 1, 21),
            None,
        )
        assert offer.description == "Liabilities:Credit Card payment"

    def test_a_statement_paid_already_offers_nothing(self, db, card_book):
        book, statement = card_book
        assert pay_card_statement(db, PayCardStatement(statement.handle, _payment(book, "300"))).ok
        assert card_payment_offer(db, statement.handle).value.amount == Money(0)

    def test_another_account_offers_nothing(self, db, book):
        opened = reconcile.start(db, book.checking, STATEMENT, Money(0))
        done = reconcile.complete(db, opened.handle)
        refused = card_payment_offer(db, done.handle)
        assert not refused.ok and refused.errors[0].code == "card.payment.not_card"


class TestTheCardsOwnTerms:
    """No schedule pays the card: its payment day and payment account make it due."""

    def test_paid_in_full_the_due_payment_is_settled(self, db, card_book):
        book, statement = card_book
        assert _dues(db, date(2026, 1, 21)) == {DUE: Money(300)}
        paid = pay_card_statement(db, PayCardStatement(statement.handle, _payment(book, "300")))
        assert paid.ok and paid.value.settled and paid.value.remaining == Money(0)
        # Settled before its date: nothing more is due for this statement.
        assert DUE not in _dues(db, date(2026, 1, 26))

    def test_a_short_payment_asks_for_the_minimum_and_saves_nothing(self, db, card_book):
        book, statement = card_book
        refused = pay_card_statement(db, PayCardStatement(statement.handle, _payment(book, "100")))
        assert not refused.ok
        assert refused.errors[0].code == "card.payment.minimum_needed"
        assert [t.description for t in db.iter_transactions()] == ["Groceries"]

    def test_at_least_the_minimum_settles_it_and_the_statement_keeps_the_minimum(
        self, db, card_book
    ):
        book, statement = card_book
        paid = pay_card_statement(
            db,
            PayCardStatement(statement.handle, _payment(book, "100"), minimum_payment=Money(35)),
        )
        assert paid.ok and paid.value.settled and paid.value.remaining == Money(200)
        assert db.get_reconciliation(statement.handle).minimum_payment == Money(35)
        assert DUE not in _dues(db, date(2026, 1, 26))

    def test_below_the_minimum_the_rest_of_the_statement_stays_due(self, db, card_book):
        book, statement = card_book
        paid = pay_card_statement(
            db,
            PayCardStatement(statement.handle, _payment(book, "20"), minimum_payment=Money(35)),
        )
        assert paid.ok and not paid.value.settled
        assert _dues(db, date(2026, 1, 26))[DUE] == Money(280)
        # A second payment counts with the first: $40 in all reaches the minimum,
        # and no question is asked, since the statement keeps it.
        again = pay_card_statement(db, PayCardStatement(statement.handle, _payment(book, "20")))
        assert again.ok and again.value.settled
        assert DUE not in _dues(db, date(2026, 1, 27))

    def test_a_refused_minimum_or_a_payment_out_of_the_card_saves_nothing(self, db, card_book):
        book, statement = card_book
        for request, code in (
            (
                PayCardStatement(statement.handle, _payment(book, "20"), Money(400)),
                "card.payment.minimum_invalid",
            ),
            (
                PayCardStatement(statement.handle, _payment(book, "-20"), Money(35)),
                "card.payment.no_payment",
            ),
            (PayCardStatement("missing", _payment(book, "20")), "card.payment.statement.not_found"),
        ):
            refused = pay_card_statement(db, request)
            assert not refused.ok and refused.errors[0].code == code
        assert db.get_reconciliation(statement.handle).minimum_payment is None
        assert [t.description for t in db.iter_transactions()] == ["Groceries"]

    def test_it_is_one_undoable_change(self, db, card_book):
        book, statement = card_book
        assert pay_card_statement(
            db, PayCardStatement(statement.handle, _payment(book, "100"), Money(35))
        ).ok
        db.undo()
        assert [t.description for t in db.iter_transactions()] == ["Groceries"]
        assert db.get_reconciliation(statement.handle).minimum_payment is None


class TestAScheduleThatPaysTheCard:
    """A commitment pays the card: the payment counts toward its occurrence."""

    @pytest.fixture
    def scheduled(self, db, card_book):
        book, statement = card_book
        payment = ScheduledTransaction(
            name="Card bill",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 15)),
            splits=[
                ScheduledSplit(book.card, Money(250)),
                ScheduledSplit(book.checking, Money(-250)),
            ],
        )
        payment.last_posted = date(2026, 1, 15)
        with db.transaction("Card bill") as txn:
            db.add_scheduled(payment, txn)
        return book, statement, payment

    def _due_review(self, db, as_of: date) -> dict[date, Money]:
        return {
            item.when: item.amount
            for item in schedule_engine.due_occurrences(db, as_of=as_of, horizon_days=0)
        }

    def test_the_offer_names_the_schedule_and_its_occurrence(self, db, scheduled):
        book, statement, payment = scheduled
        offer = card_payment_offer(db, statement.handle, today=date(2026, 1, 21)).value
        assert (offer.schedule, offer.schedule_name, offer.due_date) == (
            payment.handle,
            "Card bill",
            DUE,
        )
        assert offer.amount == Money(300)

    def test_paid_in_full_its_occurrence_is_no_longer_due(self, db, scheduled):
        book, statement, payment = scheduled
        assert self._due_review(db, DUE) == {DUE: Money(250)}
        assert pay_card_statement(db, PayCardStatement(statement.handle, _payment(book, "300"))).ok
        assert DUE not in self._due_review(db, DUE)

    def test_the_minimum_closes_it_with_the_rest_carried(self, db, scheduled):
        book, statement, payment = scheduled
        assert pay_card_statement(
            db, PayCardStatement(statement.handle, _payment(book, "100"), Money(35))
        ).ok
        assert DUE in db.get_scheduled(payment.handle).closed_occurrences
        assert DUE not in self._due_review(db, DUE)

    def test_below_the_minimum_the_rest_of_the_statement_posts_when_due(self, db, scheduled):
        book, statement, payment = scheduled
        assert pay_card_statement(
            db, PayCardStatement(statement.handle, _payment(book, "20"), Money(35))
        ).ok
        # The occurrence expects the statement, less what was paid.
        assert self._due_review(db, DUE) == {DUE: Money(280)}
        posted = resolve_due(db, ResolveDue(((payment.handle, DUE, DueDecision.POST),), as_of=DUE))
        assert posted.ok, posted.errors
        [entered] = [db.get_transaction(handle) for handle in posted.value.transactions]
        assert entered.value_for(book.card) == Money(280)
        assert entered.is_balanced()
        assert DUE not in self._due_review(db, DUE)
