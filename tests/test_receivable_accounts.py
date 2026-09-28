"""What is owed back sits in a receivable account, never in liquidity (issue #170).

Tracking a reimbursable expense moves what the payer owes out of the expense and
into a Receivable account with a BreadSched-owned reclassification transaction.
Money back moves it out again, a write-off returns it to the expense, and a
dispute posts nothing. The receivable account counts toward net worth but never
toward liquidity, because the money has not been received and cannot be spent.
The same holds for FSA money waiting to be reimbursed.
"""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.engine import dashboard
from breadsched.gen.engine.ledger import balance
from breadsched.gen.engine.receivables import POSTING_NOTE, receivable_summary
from breadsched.gen.lib import Account, AccountType, Money, Transaction
from breadsched.gen.lib.account import GnuCashAccountType
from breadsched.gen.lib.commodity import Commodity
from breadsched.gen.lib.fsa_claim import FsaClaim, FsaClaimSplitLink
from breadsched.gen.services.receivables import (
    DEFAULT_ACCOUNT_NAME,
    RecordWriteOff,
    SaveReceivable,
    attach_expense_split,
    attach_reimbursement_split,
    delete_receivable,
    detach_split,
    mark_disputed,
    receivable_candidates,
    record_write_off,
    reimbursement_proposals,
    save_receivable,
)
from breadsched.gen.services.transactions import (
    DeleteTransaction,
    SaveTransaction,
    TransactionInput,
    TransactionSplitInput,
    delete_transaction,
    save_transaction,
    transaction_currency,
)

TODAY = date(2026, 10, 1)


def _spend(db, book, when, amount, account=None):
    transaction = Transaction.simple(
        when, "Doctor visit", account or book.groceries, book.checking, amount
    )
    with db.transaction("Spend") as txn:
        db.add_transaction(transaction, txn)
    split = next(s for s in transaction.splits if s.account == (account or book.groceries))
    return transaction.handle, split.handle


def _refund(db, book, when, amount):
    transaction = Transaction.simple(when, "Insurer payment", book.checking, book.groceries, amount)
    with db.transaction("Reimburse") as txn:
        db.add_transaction(transaction, txn)
    split = next(s for s in transaction.splits if s.account == book.groceries)
    return transaction.handle, split.handle


def _track(db, book, amount="150.00", **kwargs):
    receivable = save_receivable(
        db, SaveReceivable(date(2026, 9, 1), "Acme Insurance", "Visit", **kwargs)
    ).value
    expense = _spend(db, book, date(2026, 9, 1), amount)
    result = attach_expense_split(db, receivable.handle, *expense)
    assert result.ok, result.errors
    return db.get_receivable(receivable.handle), expense


def _board(db):
    return dashboard.build(db, dashboard.DashboardConfig(), as_of=TODAY)


def _balances(db, book, receivable):
    return (
        balance(db, book.groceries),
        balance(db, receivable.account),
        balance(db, book.checking),
    )


def test_tracking_moves_what_is_owed_into_a_receivable_account(db, book):
    before = _board(db)
    receivable, _expense = _track(db, book)

    account = db.get_account(receivable.account)
    assert account.atype is AccountType.RECEIVABLE
    assert account.name == DEFAULT_ACCOUNT_NAME
    assert account.parent == book.assets
    assert _balances(db, book, receivable) == (Money(0), Money("150.00"), Money("-150.00"))

    board = _board(db)
    # Spending 150 reduced liquid cash by 150; the receivable does not add it back.
    assert board.liquid == before.liquid - Money("150.00")
    # Net worth only fell by what will not come back: nothing, here.
    assert board.book_assets == before.book_assets

    [posting] = [db.get_transaction(handle) for handle in receivable.postings]
    assert posting.notes == POSTING_NOTE
    assert posting.post_date == date(2026, 9, 1)


def test_money_back_empties_the_receivable_and_leaves_the_expense_alone(db, book):
    receivable, _expense = _track(db, book)
    refund = _refund(db, book, date(2026, 9, 15), "100.00")
    assert attach_reimbursement_split(db, receivable.handle, *refund).ok
    receivable = db.get_receivable(receivable.handle)
    assert _balances(db, book, receivable) == (Money(0), Money("50.00"), Money("-50.00"))

    final = _refund(db, book, date(2026, 9, 20), "50.00")
    assert attach_reimbursement_split(db, receivable.handle, *final).ok
    receivable = db.get_receivable(receivable.handle)
    assert _balances(db, book, receivable) == (Money(0), Money(0), Money(0))
    assert len(receivable.postings) == 3


def test_an_expected_amount_leaves_the_rest_as_a_real_expense(db, book):
    receivable, _expense = _track(db, book, expected_amount=Money("120.00"))
    assert _balances(db, book, receivable) == (
        Money("30.00"),
        Money("120.00"),
        Money("-150.00"),
    )
    refund = _refund(db, book, date(2026, 9, 15), "120.00")
    attach_reimbursement_split(db, receivable.handle, *refund)
    receivable = db.get_receivable(receivable.handle)
    summary = receivable_summary(db, receivable)
    assert (summary.owed, summary.remaining, summary.status.value) == (
        Money("120.00"),
        Money(0),
        "settled",
    )
    assert balance(db, receivable.account) == Money(0)


def test_more_money_back_than_owed_stays_a_refund_in_the_expense(db, book):
    receivable, _expense = _track(db, book, expected_amount=Money("100.00"))
    refund = _refund(db, book, date(2026, 9, 15), "130.00")
    attach_reimbursement_split(db, receivable.handle, *refund)
    receivable = db.get_receivable(receivable.handle)
    # 150 spent, 130 back: 20 of real expense, nothing still owed.
    assert _balances(db, book, receivable) == (Money("20.00"), Money(0), Money("-20.00"))


def test_a_write_off_returns_the_balance_to_the_expense(db, book):
    receivable, _expense = _track(db, book)
    refund = _refund(db, book, date(2026, 9, 15), "100.00")
    attach_reimbursement_split(db, receivable.handle, *refund)
    written = record_write_off(
        db, RecordWriteOff(receivable.handle, Money("80.00"), date(2026, 10, 1), "Deductible")
    )
    assert written.ok
    receivable = db.get_receivable(receivable.handle)
    # Only the 50 still owed can be written off; the expense takes it back.
    assert _balances(db, book, receivable) == (Money("50.00"), Money(0), Money("-50.00"))


def test_a_dispute_posts_nothing(db, book):
    receivable, _expense = _track(db, book)
    postings = list(receivable.postings)
    assert mark_disputed(db, receivable.handle, date(2026, 9, 5), "Denied").ok
    receivable = db.get_receivable(receivable.handle)
    assert receivable.postings == postings
    assert balance(db, receivable.account) == Money("150.00")


def test_unlinking_and_deleting_remove_the_reclassifications(db, book):
    receivable, expense = _track(db, book)
    [posting] = receivable.postings
    assert detach_split(db, receivable.handle, *expense).ok
    assert db.get_transaction(posting) is None
    assert balance(db, book.groceries) == Money("150.00")

    assert attach_expense_split(db, receivable.handle, *expense).ok
    assert balance(db, book.groceries) == Money(0)
    assert delete_receivable(db, receivable.handle).ok
    assert balance(db, book.groceries) == Money("150.00")
    assert db.undo() is True
    assert balance(db, book.groceries) == Money(0)


def test_reclassifications_are_owned_and_never_offered_as_links(db, book):
    receivable, _expense = _track(db, book)
    [posting] = receivable.postings
    currency = transaction_currency(db)
    request = SaveTransaction(
        TransactionInput(
            date(2026, 9, 2),
            "Edited",
            (
                TransactionSplitInput(book.groceries, _amount("1", currency)),
                TransactionSplitInput(book.checking, _amount("-1", currency)),
            ),
            currency=currency,
        ),
        existing_handle=posting,
    )
    assert [e.code for e in save_transaction(db, request).errors] == [
        "transaction.receivable_posting"
    ]
    assert [e.code for e in delete_transaction(db, DeleteTransaction(posting)).errors] == [
        "transaction.receivable_posting"
    ]
    costs, credits = receivable_candidates(db).value
    assert posting not in {item.transaction for item in (*costs, *credits)}
    assert reimbursement_proposals(db).value == ()
    other = save_receivable(db, SaveReceivable(date(2026, 9, 1), "Employer")).value
    split = db.get_transaction(posting).splits[1].handle
    assert [
        e.code for e in attach_reimbursement_split(db, other.handle, posting, split).errors
    ] == ["receivable.split.owned_posting"]


def _amount(value, currency):
    from breadsched.gen.lib.amount import Amount

    return Amount(Money(value), currency)


def test_editing_a_linked_expense_follows_through_to_the_receivable(db, book):
    receivable, (expense, split) = _track(db, book)
    other = next(s.handle for s in db.get_transaction(expense).splits if s.handle != split)
    currency = transaction_currency(db)
    request = SaveTransaction(
        TransactionInput(
            date(2026, 9, 1),
            "Doctor visit",
            (
                TransactionSplitInput(book.groceries, _amount("200", currency), handle=split),
                TransactionSplitInput(book.checking, _amount("-200", currency), handle=other),
            ),
            currency=currency,
        ),
        existing_handle=expense,
        source=db.get_transaction(expense),
    )
    saved = save_transaction(db, request)
    assert saved.ok, saved.errors
    receivable = db.get_receivable(receivable.handle)
    assert receivable_summary(db, receivable).owed == Money("200.00")
    assert balance(db, receivable.account) == Money("200.00")
    assert balance(db, book.groceries) == Money(0)


def test_an_explicit_account_must_be_a_receivable_in_the_expense_currency(db, book):
    wrong = save_receivable(
        db, SaveReceivable(date(2026, 9, 1), "Acme Insurance", account=book.savings)
    )
    assert [e.code for e in wrong.errors] == ["receivable.account.not_receivable"]
    missing = save_receivable(
        db, SaveReceivable(date(2026, 9, 1), "Acme Insurance", account="missing")
    )
    assert [e.code for e in missing.errors] == ["receivable.account.not_found"]

    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    held = Account(
        name="Owed in euros",
        atype=AccountType.RECEIVABLE,
        parent=book.assets,
        commodity=euro.handle,
    )
    with db.transaction("Euro receivable") as txn:
        db.add_commodity(euro, txn)
        db.add_account(held, txn)
    receivable = save_receivable(
        db, SaveReceivable(date(2026, 9, 1), "Acme Insurance", account=held.handle)
    ).value
    expense = _spend(db, book, date(2026, 9, 1), "150.00")
    refused = attach_expense_split(db, receivable.handle, *expense)
    assert [e.code for e in refused.errors] == ["receivable.account.currency_mismatch"]
    # The refused write leaves the stored receivable and the ledger unchanged.
    assert db.get_receivable(receivable.handle).expenses == []
    assert balance(db, book.groceries) == Money("150.00")


def test_a_legacy_receivable_is_moved_into_an_account_on_its_next_change(db, book):
    from breadsched.gen.lib.receivable import Receivable, ReceivableSplitLink

    expense = _spend(db, book, date(2026, 9, 1), "150.00")
    legacy = Receivable(
        date(2026, 9, 1), "Acme Insurance", expenses=[ReceivableSplitLink(*expense)]
    )
    with db.transaction("Legacy") as txn:
        db.add_receivable(legacy, txn)
    assert balance(db, book.groceries) == Money("150.00")
    assert mark_disputed(db, legacy.handle, date(2026, 9, 5)).ok
    stored = db.get_receivable(legacy.handle)
    assert stored.account is not None
    assert balance(db, stored.account) == Money("150.00")


def test_gnucash_receivable_accounts_are_receivables_and_never_cash():
    assert GnuCashAccountType.RECEIVABLE.to_account_type() is AccountType.RECEIVABLE
    assert not AccountType.RECEIVABLE.is_cash_like
    assert not AccountType.RECEIVABLE.supports_emergency_fund
    assert AccountType.RECEIVABLE.is_debit_balance


def test_fsa_money_waiting_to_be_reimbursed_is_net_worth_not_liquidity(db, book):
    fsa = Account(name="Health FSA", atype=AccountType.FSA, parent=book.assets)
    with db.transaction("FSA") as txn:
        db.add_account(fsa, txn)
    funded = Transaction.simple(date(2026, 1, 2), "FSA election", fsa.handle, book.salary, "500")
    with db.transaction("Fund") as txn:
        db.add_transaction(funded, txn)
    board = _board(db)
    assert board.liquid == Money(0)
    assert board.book_assets == Money(500)


@pytest.mark.parametrize("expected", [None, Money("100.00")])
def test_an_expense_also_claimed_from_the_fsa_is_flagged(db, book, expected):
    from breadsched.gen.engine.fsa_claims import save_claim

    receivable, expense = _track(db, book, expected_amount=expected)
    assert receivable_summary(db, receivable).fsa_claims == ()
    claim = FsaClaim(date(2026, 9, 1), "Clinic", payments=[FsaClaimSplitLink(*expense)])
    save_claim(db, claim)
    summary = receivable_summary(db, db.get_receivable(receivable.handle))
    assert summary.fsa_claims == (claim.handle,)


def test_an_imported_gnucash_receivable_is_used_only_when_chosen(db, book):
    imported = Account(name="Accounts Receivable", atype=AccountType.RECEIVABLE, parent=book.assets)
    imported.source_guid = "0" * 32
    with db.transaction("Imported A/R") as txn:
        db.add_account(imported, txn)
    receivable, _expense = _track(db, book)
    assert receivable.account != imported.handle
    assert db.get_account(receivable.account).name == DEFAULT_ACCOUNT_NAME

    chosen, _other = _track(db, book, account=imported.handle)
    assert chosen.account == imported.handle
    assert balance(db, imported.handle) == Money("150.00")


def test_the_dashboard_shows_what_is_owed_and_what_needs_attention(db, book):
    _track(db, book)
    late, _expense = _track(db, book, amount="40.00", expected_cash_date=date(2026, 9, 20))
    board = _board(db)
    assert board.receivables_owed == Money("190.00")
    assert board.receivables_attention == Money("40.00")
    summary = board.summary()
    assert summary["receivables_owed"] == Money("190.00")


def test_an_expense_changed_outside_the_services_is_caught_up_after_import(db, book):
    from breadsched.gen.services.receivables import sync_all_receivables

    receivable, (expense, split) = _track(db, book)
    changed = db.get_transaction(expense)
    for item in changed.splits:
        item.value = item.quantity = Money("175.00") if item.handle == split else Money("-175.00")
    with db.transaction("As an import would") as txn:
        db.commit_transaction(changed, txn)
    assert balance(db, receivable.account) == Money("150.00")
    assert sync_all_receivables(db) == 1
    assert balance(db, receivable.account) == Money("175.00")
    assert balance(db, book.groceries) == Money(0)
    assert sync_all_receivables(db) == 0
