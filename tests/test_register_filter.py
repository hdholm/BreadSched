"""The register filters search everything a transaction holds (#382).

Searching "67" used to find check 1067 and imported notes but never a 67.00
charge, because amounts were not searched at all.
"""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.engine.ledger import transaction_matches
from breadsched.gen.lib import Money, Split, Transaction


def _purchase(db, book):
    transaction = Transaction(post_date=date(2026, 3, 14), description="Corner grocer", num="1067")
    transaction.add_split(Split(book.groceries, Money("1067.25"), memo="weekly shop"))
    transaction.add_split(Split(book.checking, Money("-1067.25")))
    transaction.notes = "Party supplies"
    transaction.source_notes = "Imported: FITID 99881"
    transaction.tags = ["household"]
    with db.transaction("Purchase") as txn:
        db.add_transaction(transaction, txn)
    return transaction


@pytest.mark.parametrize(
    "needle",
    [
        "1067",  # the check number, and part of the amount
        "1,067.25",  # the amount as shown
        "1067.25",  # the amount as typed
        "67.25",
        "2026-03-14",
        "CORNER",
        "party",
        "99881",
        "household",
        "weekly",
        "Expenses:Groceries",
        "  ",  # nothing to find keeps everything
    ],
)
def test_the_filter_finds_every_part_of_a_transaction(db, book, needle):
    assert transaction_matches(db, _purchase(db, book), needle)


@pytest.mark.parametrize("needle", ["67.26", "Rent", "1066"])
def test_the_filter_leaves_out_what_is_not_there(db, book, needle):
    assert not transaction_matches(db, _purchase(db, book), needle)


def test_a_67_charge_is_found_by_its_amount(db, book):
    charge = Transaction.simple(date(2026, 3, 2), "Fuel", book.groceries, book.card, "67")
    with db.transaction("Charge") as txn:
        db.add_transaction(charge, txn)
    assert transaction_matches(db, charge, "67")
    assert transaction_matches(db, charge, "67.00")
