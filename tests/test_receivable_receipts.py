"""Expected reimbursements are planned receipts, but only when they can be counted on.

An open receivable with a future expected date becomes a one-off in Plan and
Projection that moves what is still owed from the receivable account into the cash
account that paid the expense. A disputed or overdue receivable is not spendable
cash, so it is left out, as is a settled or written-off one.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from breadsched.gen.engine import planning, projection
from breadsched.gen.engine.receivables import expected_receipt
from breadsched.gen.lib import Assumptions, Money, Scenario, Transaction
from breadsched.gen.services.receivables import (
    RecordWriteOff,
    SaveReceivable,
    attach_expense_split,
    attach_reimbursement_split,
    mark_disputed,
    record_write_off,
    save_receivable,
)

TODAY = date.today()
DUE = TODAY + timedelta(days=20)


def _post(db, transaction, account):
    with db.transaction(transaction.description) as txn:
        db.add_transaction(transaction, txn)
    return transaction.handle, next(s for s in transaction.splits if s.account == account).handle


def _receivable(db, book, *, paid_from=None, expected=DUE, amount="150.00"):
    receivable = save_receivable(
        db,
        SaveReceivable(
            incurred_date=TODAY - timedelta(days=5),
            payer="Acme Insurance",
            description="Clinic visit",
            expected_cash_date=expected,
        ),
    ).value
    spent = Transaction.simple(
        TODAY - timedelta(days=5), "Clinic", book.groceries, paid_from or book.checking, amount
    )
    txn, split = _post(db, spent, book.groceries)
    assert attach_expense_split(db, receivable.handle, txn, split).ok
    return db.get_receivable(receivable.handle)


def _events(db):
    scenario = Scenario(name="Base", start=TODAY.replace(day=1), years=1)
    events = planning.scenario_events(db, scenario, TODAY, TODAY + timedelta(days=365))
    return [event for event in events if event.key.startswith("receivable:")]


def test_an_open_receivable_is_a_planned_receipt_on_its_expected_date(db, book):
    receivable = _receivable(db, book)
    [event] = _events(db)
    assert event.planned_date == DUE
    assert event.description == "Expected from Acme Insurance: Clinic visit"
    assert {(s.account, s.amount) for s in event.splits} == {
        (book.checking, Money("150.00")),
        (receivable.account, Money("-150.00")),
    }
    assert event.placeholder and event.actual_transaction is None


def test_projected_cash_rises_by_the_receipt_only_from_its_date(db, book):
    _receivable(db, book)
    scenario = Scenario(
        name="Base",
        start=TODAY.replace(day=1),
        years=1,
        assumptions=Assumptions(cash_interest=Decimal(0)),
    )
    counted = projection.project(db, scenario)
    receivable = next(iter(db.iter_receivables()))
    assert mark_disputed(db, receivable.handle, TODAY, "Denied pending appeal").ok
    disputed = projection.project(db, scenario)
    due_month = next(
        i
        for i, row in enumerate(counted.rows)
        if row.month <= DUE <= row.month.replace(day=28) + timedelta(days=4)
    )
    assert counted.rows[due_month].cash_close - disputed.rows[due_month].cash_close == Money(
        "150.00"
    )
    before = due_month - 1
    if before >= 0:
        assert counted.rows[before].cash_close == disputed.rows[before].cash_close
    # Net worth is the same either way: the money only moves from the receivable to cash.
    assert counted.rows[-1].net_worth == disputed.rows[-1].net_worth


def test_disputed_overdue_settled_and_written_off_receivables_are_not_counted(db, book):
    disputed = _receivable(db, book)
    assert mark_disputed(db, disputed.handle, TODAY, "Appeal").ok
    _receivable(db, book, expected=TODAY - timedelta(days=1))  # overdue
    settled = _receivable(db, book)
    back = Transaction.simple(TODAY, "Paid back", book.checking, book.groceries, "150.00")
    txn, split = _post(db, back, book.groceries)
    assert attach_reimbursement_split(db, settled.handle, txn, split).ok
    written = _receivable(db, book)
    assert record_write_off(
        db, RecordWriteOff(written.handle, Money("150.00"), TODAY, "Not worth it")
    ).ok
    assert _events(db) == []


def test_a_partial_reimbursement_leaves_the_rest_expected(db, book):
    receivable = _receivable(db, book)
    back = Transaction.simple(TODAY, "Part paid", book.checking, book.groceries, "100.00")
    txn, split = _post(db, back, book.groceries)
    assert attach_reimbursement_split(db, receivable.handle, txn, split).ok
    [event] = _events(db)
    assert event.expected_amount == Money("50.00")


def test_a_card_paid_expense_is_expected_in_the_account_that_pays_the_card(db, book):
    card = db.get_account(book.card)
    card.card_payment_account = book.savings
    with db.transaction("Card setup") as txn:
        db.commit_account(card, txn)
    receivable = _receivable(db, book, paid_from=book.card)
    receipt = expected_receipt(db, db.get_receivable(receivable.handle))
    assert receipt is not None and receipt.cash_account == book.savings
