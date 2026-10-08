"""Entry autocomplete: propose an earlier transaction's accounts and amount.

Typing a description (matched by its normalized key) proposes the most recent
matching transaction's splits. The proposal is data only: the user
edits or ignores it and saves an ordinary balanced transaction. Reconcile state,
source identifiers, notes, planning links, and FSA claims are never copied.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import date

from breadsched.gen.lib import Commodity, Money, ReconcileState, Transaction
from breadsched.gen.services.autocomplete import SuggestEntry, suggest_entry


def _post(db, book, when, description, account, amount, **extra):
    transaction = Transaction.simple(when, description, account, book.checking, amount)
    for key, value in extra.items():
        setattr(transaction, key, value)
    with db.transaction("Post") as txn:
        db.add_transaction(transaction, txn)
    return transaction


def test_the_most_recent_matching_description_is_proposed(db, book):
    _post(db, book, date(2026, 8, 1), "CORNER GROCER #1", book.groceries, "40.00")
    latest = _post(db, book, date(2026, 9, 1), "Corner Grocer 0987", book.groceries, "55.25")
    _post(db, book, date(2026, 9, 5), "Hardware", book.utilities, "9.00")

    suggestion = suggest_entry(
        db, SuggestEntry(description="corner grocer #77", account=book.checking)
    ).value.suggestion

    assert suggestion is not None
    assert (suggestion.source, suggestion.when) == (latest.handle, date(2026, 9, 1))
    assert suggestion.transfer_account == book.groceries
    assert suggestion.amount == Money("-55.25")
    assert sorted((split.account, split.value) for split in suggestion.splits) == sorted(
        [(book.groceries, Money("55.25")), (book.checking, Money("-55.25"))]
    )


def test_no_match_or_empty_key_proposes_nothing(db, book):
    _post(db, book, date(2026, 9, 1), "Corner Grocer", book.groceries, "10")

    assert suggest_entry(db, SuggestEntry(description="Bakery")).value.suggestion is None
    assert suggest_entry(db, SuggestEntry(description="#1234")).value.suggestion is None
    assert suggest_entry(db, SuggestEntry(description="")).value.suggestion is None


def test_private_state_is_never_copied(db, book):
    source = _post(db, book, date(2026, 9, 1), "Pharmacy", book.groceries, "12.00")
    source.notes = "private"
    for split in source.splits:
        split.reconcile = ReconcileState.RECONCILED
        split.memo = "prescription"
    with db.transaction("Annotate") as txn:
        db.commit_transaction(source, txn)

    suggestion = suggest_entry(db, SuggestEntry(description="pharmacy")).value.suggestion

    assert suggestion is not None
    assert "notes" not in {item.name for item in fields(suggestion)}
    for split in suggestion.splits:
        assert {item.name for item in fields(split)} == {"account", "value", "memo"}
    assert {split.memo for split in suggestion.splits} == {"prescription"}


def test_the_account_and_currency_narrow_the_candidates(db, book):
    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    with db.transaction("EUR") as txn:
        db.add_commodity(euro, txn)
    local = _post(db, book, date(2026, 8, 1), "Coffee", book.groceries, "3.00")
    _post(db, book, date(2026, 9, 1), "Coffee", book.groceries, "4.00", currency=euro.handle)
    card = Transaction.simple(date(2026, 9, 2), "Coffee", book.groceries, book.card, "5.00")
    with db.transaction("Card") as txn:
        db.add_transaction(card, txn)

    in_checking = suggest_entry(db, SuggestEntry(description="coffee", account=book.checking))
    assert in_checking.value.suggestion.source == local.handle
    in_euro = suggest_entry(db, SuggestEntry(description="coffee", currency=euro.handle))
    assert (
        in_euro.value.suggestion.amount is None and in_euro.value.suggestion.currency == euro.handle
    )
    anywhere = suggest_entry(db, SuggestEntry(description="coffee"))
    assert anywhere.value.suggestion.source == card.handle
    assert anywhere.value.suggestion.transfer_account is None


def test_an_edited_transaction_is_not_proposed_to_itself(db, book):
    only = _post(db, book, date(2026, 9, 1), "Rent", book.rent, "900")

    assert (
        suggest_entry(db, SuggestEntry(description="rent", exclude=only.handle)).value.suggestion
        is None
    )


def test_an_unknown_account_is_refused(db, book):
    assert [e.code for e in suggest_entry(db, SuggestEntry("x", account="missing")).errors] == [
        "autocomplete.account.not_found"
    ]
