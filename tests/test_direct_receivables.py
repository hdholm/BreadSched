"""A charge recorded straight to a receivable account is tracked as owed (#378).

A cost an employer will repay is often recorded with no expense at all: the card
charge goes to Assets:Receivables:Employer, and the payback (a paycheck line)
credits that account. Such a *direct* receivable is owed in full, needs no
reclassification posting, and money back credited to its account settles it.
Only a write-off posts, into the expense account it names.
"""

from __future__ import annotations

from datetime import date, timedelta

from breadsched.gen.engine import dashboard
from breadsched.gen.engine.ledger import balance_amount
from breadsched.gen.engine.planning import receivable_receipt_events
from breadsched.gen.engine.receivables import is_direct, receivable_summary
from breadsched.gen.lib import Account, AccountType, Money, Transaction
from breadsched.gen.lib.receivable import Receivable, ReceivableWriteOff
from breadsched.gen.services.receivables import (
    RecordWriteOff,
    SaveReceivable,
    accept_reimbursements,
    attach_expense_split,
    attach_reimbursement_split,
    receivable_candidates,
    record_write_off,
    reimbursement_proposals,
    save_receivable,
)

TODAY = date(2026, 10, 1)


def _employer(db, book, name="Employer"):
    account = Account(name=name, atype=AccountType.RECEIVABLE, parent=book.assets)
    with db.transaction(f"Add {name}") as txn:
        db.add_account(account, txn)
    return account.handle


def _charge(db, book, employer, amount="100.00", when=date(2026, 9, 3)):
    """A card charge recorded straight to the employer's receivable account."""
    charge = Transaction.simple(when, "Conference hotel", employer, book.card, amount)
    with db.transaction("Charge") as txn:
        db.add_transaction(charge, txn)
    return charge.handle, charge.split_for(employer).handle


def _payback(db, book, employer, amount="100.00", when=date(2026, 9, 25)):
    """The paycheck line repaying it: checking up, the receivable down."""
    pay = Transaction.simple(when, "Acme payroll", book.checking, employer, amount)
    with db.transaction("Payback") as txn:
        db.add_transaction(pay, txn)
    return pay.handle, pay.split_for(employer).handle


def _track(db, book, employer, **kwargs):
    receivable = save_receivable(
        db, SaveReceivable(date(2026, 9, 3), "Acme", "Conference", **kwargs)
    ).value
    charge = _charge(db, book, employer)
    result = attach_expense_split(db, receivable.handle, *charge)
    assert result.ok, result.errors
    return db.get_receivable(receivable.handle), charge


def test_a_direct_charge_is_owed_in_full_and_posts_nothing(db, book):
    employer = _employer(db, book)
    before = dashboard.build(db, dashboard.DashboardConfig(), as_of=TODAY)
    receivable, _charge_link = _track(db, book, employer)

    assert receivable.account == employer
    assert receivable.postings == []
    assert is_direct(db, receivable)
    summary = receivable_summary(db, receivable, as_of=TODAY)
    assert (summary.owed, summary.remaining, summary.status.value) == (
        Money("100.00"),
        Money("100.00"),
        "open",
    )
    assert balance_amount(db, employer).value == Money("100.00")
    after = dashboard.build(db, dashboard.DashboardConfig(), as_of=TODAY)
    # No spending and no liquidity: only the card owes more, and the employer owes it back.
    assert after.liquid == before.liquid
    assert after.book_assets - before.book_assets == Money("100.00")


def test_the_paycheck_line_is_proposed_and_settles_it(db, book):
    employer = _employer(db, book)
    receivable, _charge_link = _track(db, book, employer)
    payback = _payback(db, book, employer)

    [proposal] = reimbursement_proposals(db, as_of=TODAY).value
    assert (proposal.receivable, proposal.transaction, proposal.split) == (
        receivable.handle,
        *payback,
    )
    accepted = accept_reimbursements(
        db, ((proposal.receivable, proposal.transaction, proposal.split),)
    ).value
    assert accepted.linked == 1
    receivable = db.get_receivable(receivable.handle)
    summary = receivable_summary(db, receivable, as_of=TODAY)
    assert (summary.reimbursed, summary.remaining, summary.status.value) == (
        Money("100.00"),
        Money(0),
        "settled",
    )
    assert receivable.postings == []
    assert balance_amount(db, employer).value == Money(0)


def test_a_direct_charge_cannot_expect_less_than_it_cost(db, book):
    employer = _employer(db, book)
    receivable, _charge_link = _track(db, book, employer)
    before = db.get_receivable(receivable.handle).serialize()

    refused = save_receivable(
        db,
        SaveReceivable(
            date(2026, 9, 3),
            "Acme",
            "Conference",
            expected_amount=Money("80.00"),
            handle=receivable.handle,
        ),
    )
    assert [error.code for error in refused.errors] == [
        "receivable.expected_amount.below_direct_charge"
    ]
    assert db.get_receivable(receivable.handle).serialize() == before


def test_a_direct_write_off_names_the_expense_it_becomes(db, book):
    employer = _employer(db, book)
    receivable, _charge_link = _track(db, book, employer)
    before = db.get_receivable(receivable.handle).serialize()

    refused = record_write_off(
        db, RecordWriteOff(receivable.handle, Money("30.00"), date(2026, 9, 30))
    )
    assert [error.code for error in refused.errors] == ["receivable.write_off.account_required"]
    assert db.get_receivable(receivable.handle).serialize() == before
    not_expense = record_write_off(
        db,
        RecordWriteOff(receivable.handle, Money("30.00"), date(2026, 9, 30), account=book.checking),
    )
    assert [error.code for error in not_expense.errors] == [
        "receivable.write_off.account_not_expense"
    ]

    written = record_write_off(
        db,
        RecordWriteOff(
            receivable.handle, Money("30.00"), date(2026, 9, 30), "Not covered", book.groceries
        ),
    )
    assert written.ok, written.errors
    receivable = db.get_receivable(receivable.handle)
    [posting] = [db.get_transaction(handle) for handle in receivable.postings]
    assert {split.account: split.value for split in posting.splits} == {
        book.groceries: Money("30.00"),
        employer: Money("-30.00"),
    }
    assert balance_amount(db, employer).value == Money("70.00")
    assert receivable_summary(db, receivable, as_of=TODAY).remaining == Money("70.00")


def test_a_write_off_keeps_its_account_when_stored():
    item = ReceivableWriteOff(date(2026, 9, 30), Money("5.00"), "gone", "expense-handle")
    stored = Receivable(date(2026, 9, 1), "Acme", write_offs=[item]).serialize()
    restored = Receivable()
    restored.unserialize(stored)
    assert restored.write_offs == [item]
    plain = ReceivableWriteOff(date(2026, 9, 30), Money("5.00"))
    assert "account" not in plain.serialize()


def test_costs_are_all_expenses_or_all_charges_in_one_receivable_account(db, book):
    employer = _employer(db, book)
    other = _employer(db, book, "Client")
    receivable, _charge_link = _track(db, book, employer)
    before = db.get_receivable(receivable.handle).serialize()

    spend = Transaction.simple(date(2026, 9, 4), "Lunch", book.groceries, book.checking, "12")
    elsewhere = Transaction.simple(date(2026, 9, 4), "Taxi", other, book.card, "20")
    with db.transaction("More") as txn:
        db.add_transaction(spend, txn)
        db.add_transaction(elsewhere, txn)
    mixed = attach_expense_split(
        db, receivable.handle, spend.handle, spend.split_for(book.groceries).handle
    )
    assert [error.code for error in mixed.errors] == ["receivable.split.mixed_kinds"]
    moved = attach_expense_split(
        db, receivable.handle, elsewhere.handle, elsewhere.split_for(other).handle
    )
    assert [error.code for error in moved.errors] == ["receivable.split.other_account"]
    assert db.get_receivable(receivable.handle).serialize() == before


def test_an_ordinary_receivable_takes_money_back_into_its_account(db, book):
    # An insurer's check recorded as checking <- receivable account needs no
    # posting: it already leaves the receivable account.
    spend = Transaction.simple(date(2026, 9, 1), "Clinic", book.groceries, book.checking, "150")
    with db.transaction("Spend") as txn:
        db.add_transaction(spend, txn)
    receivable = save_receivable(db, SaveReceivable(date(2026, 9, 1), "Acme Insurance")).value
    assert attach_expense_split(
        db, receivable.handle, spend.handle, spend.split_for(book.groceries).handle
    ).ok
    receivable = db.get_receivable(receivable.handle)
    held = receivable.account
    assert balance_amount(db, held).value == Money("150")
    check = Transaction.simple(date(2026, 9, 20), "Acme Insurance", book.checking, held, "150")
    with db.transaction("Check") as txn:
        db.add_transaction(check, txn)
    assert attach_reimbursement_split(
        db, receivable.handle, check.handle, check.split_for(held).handle
    ).ok
    receivable = db.get_receivable(receivable.handle)
    assert len(receivable.postings) == 1
    assert balance_amount(db, held).value == Money(0)
    assert balance_amount(db, book.groceries).value == Money(0)
    assert receivable_summary(db, receivable, as_of=TODAY).status.value == "settled"


def test_direct_charges_are_offered_as_costs_and_paybacks_as_credits(db, book):
    employer = _employer(db, book)
    charge = _charge(db, book, employer)
    payback = _payback(db, book, employer)
    costs, credits = receivable_candidates(db).value
    assert charge in [(item.transaction, item.split) for item in costs]
    assert payback in [(item.transaction, item.split) for item in credits]


def test_the_expected_payback_is_planned_and_a_shortfall_stays_owed(db, book):
    from breadsched.gen.lib.scenario import ReimbursementOverride, Scenario

    card = db.get_account(book.card)
    card.card.payment_account = book.checking
    with db.transaction("Card pays from checking") as txn:
        db.commit_account(card, txn)
    employer = _employer(db, book)
    # Receipts are expected from today on, so the dates follow the clock.
    expected = date.today() + timedelta(days=15)
    start, end = date.today(), date.today() + timedelta(days=30)
    receivable, _charge_link = _track(db, book, employer, expected_cash_date=expected)

    [event] = receivable_receipt_events(db, start, end)
    assert {split.account: split.amount for split in event.expected_splits} == {
        book.checking: Money("100.00"),
        employer: Money("-100.00"),
    }
    scenario = Scenario(name="Partial")
    scenario.reimbursement_overrides[receivable.handle] = ReimbursementOverride(
        amount=Money("60.00")
    )
    [partial] = receivable_receipt_events(db, start, end, scenario)
    # No expense to write off into: the other 40 stays owed.
    assert {split.account: split.amount for split in partial.expected_splits} == {
        book.checking: Money("60.00"),
        employer: Money("-60.00"),
    }
