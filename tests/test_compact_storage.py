"""Transactions are stored minimized and read back exactly (decision 0001, #354).

A stored transaction leaves out each key whose value is what reading supplies when
the key is missing. The defaults are pinned in ``gen.db.compact``; these tests fail
if ``from_dict`` ever supplies something else (which would silently change old
books), and prove that storage, undo, verification, and SQL queries over the blob
behave exactly as with full JSON.
"""

from __future__ import annotations

import json
from datetime import date

from breadsched.gen.db.compact import (
    SPLIT_DEFAULTS,
    TRANSACTION_DEFAULTS,
    expand_transaction,
    minimize_transaction,
)
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.lib import Account, AccountType, Transaction
from breadsched.gen.lib.transaction import PlanningResolution, ReconcileState


def _stored_blob(db, handle):
    row = db._require().execute("SELECT blob FROM txn WHERE handle=?", (handle,)).fetchone()
    return json.loads(row["blob"])


def _required_only() -> dict:
    full = Transaction.simple(date(2026, 1, 2), "x", "a", "b", "1.00").serialize()
    split_keys = {"handle", "account", "value", "quantity"}
    return {
        **{key: full[key] for key in ("_schema", "handle", "change", "post_date", "enter_date")},
        "splits": [{key: split[key] for key in split_keys} for split in full["splits"]],
    }


def test_the_pinned_defaults_are_what_reading_supplies():
    restored = Transaction.from_dict(_required_only()).serialize()
    for key, value in TRANSACTION_DEFAULTS.items():
        assert restored[key] == value, key
    for split in restored["splits"]:
        for key, value in SPLIT_DEFAULTS.items():
            assert split[key] == value, key
    assert restored["planning_resolution"] == PlanningResolution.UNRESOLVED.value


def test_minimizing_then_expanding_gives_the_full_form(db, book):
    plain = Transaction.simple(date(2026, 3, 1), "Shop", book.groceries, book.checking, "9.99")
    rich = Transaction.simple(date(2026, 3, 2), "Rent", book.rent, book.checking, "1800.00")
    rich.notes, rich.num, rich.tags = "March", "104", ["home"]
    rich.scheduled_from = "rent-schedule"
    rich.planning_resolution = PlanningResolution.UNRESOLVED  # differs from the derived default
    rich.splits[0].memo = "flat"
    rich.splits[0].reconcile = ReconcileState.RECONCILED
    rich.splits[0].reconcile_date = date(2026, 3, 31)
    for transaction in (plain, rich):
        data = transaction.serialize()
        small = minimize_transaction(data)
        assert len(json.dumps(small)) < len(json.dumps(data))
        assert expand_transaction(small) == data
        assert Transaction.from_dict(small).serialize() == data
    assert "planning_resolution" in minimize_transaction(rich.serialize())


def test_books_store_the_small_form_and_read_the_full_one(db, book):
    transaction = Transaction.simple(
        date(2026, 3, 1), "Shop", book.groceries, book.checking, "9.99"
    )
    with db.transaction("Add") as txn:
        db.add_transaction(transaction, txn)
    stored = _stored_blob(db, transaction.handle)
    assert "notes" not in stored and "memo" not in stored["splits"][0]
    assert {"handle", "account", "value", "quantity"} <= set(stored["splits"][0])
    assert db.get_transaction(transaction.handle).serialize() == transaction.serialize()
    assert db._read("txn", transaction.handle) == transaction.serialize()


def test_an_unchanged_write_is_still_not_a_change(db, book):
    transaction = Transaction.simple(
        date(2026, 3, 1), "Shop", book.groceries, book.checking, "9.99"
    )
    with db.transaction("Add") as txn:
        db.add_transaction(transaction, txn)
    steps = len(db.undo_stack)
    again = db.get_transaction(transaction.handle)
    with db.transaction("Same") as txn:
        db.commit_transaction(again, txn)
    assert len(db.undo_stack) == steps


def test_undo_and_redo_restore_the_exact_records(db, book):
    transaction = Transaction.simple(
        date(2026, 3, 1), "Shop", book.groceries, book.checking, "9.99"
    )
    with db.transaction("Add") as txn:
        db.add_transaction(transaction, txn)
    original = db.get_transaction(transaction.handle).serialize()
    edited = db.get_transaction(transaction.handle)
    edited.notes = "receipt kept"
    with db.transaction("Edit") as txn:
        db.commit_transaction(edited, txn)
    assert db.undo() is True
    assert db.get_transaction(transaction.handle).serialize() == original
    assert db.redo() is True
    assert db.get_transaction(transaction.handle).notes == "receipt kept"


def test_full_and_minimized_blobs_live_side_by_side(tmp_path, book):
    """A book written before minimizing keeps its full blobs; both kinds read and
    verify alike, and SQL over the blob treats a missing key as null."""
    path = tmp_path / "mixed.breadsched"
    db = DbSQLite()
    db.load(str(path))
    try:
        with db.transaction("Accounts") as txn:
            root = Account(name="Root", atype=AccountType.ROOT)
            db.add_account(root, txn)
            a = Account(name="A", atype=AccountType.BANK, parent=root.handle)
            b = Account(name="B", atype=AccountType.EXPENSE, parent=root.handle)
            db.add_account(a, txn)
            db.add_account(b, txn)
        old = Transaction.simple(date(2026, 1, 1), "Old", b.handle, a.handle, "5.00")
        new = Transaction.simple(date(2026, 1, 2), "New", b.handle, a.handle, "6.00")
        new.scheduled_from = "sched"
        with db.transaction("Both") as txn:
            db.add_transaction(old, txn)
            db.add_transaction(new, txn)
        # Rewrite one row the way earlier versions stored it.
        conn = db._require()
        conn.execute(
            "UPDATE txn SET blob=? WHERE handle=?",
            (json.dumps(old.serialize(), separators=(",", ":")), old.handle),
        )
        conn.commit()
        assert db.get_transaction(old.handle).serialize() == old.serialize()
        assert db.get_transaction(new.handle).serialize() == new.serialize()
        flagged = conn.execute(
            "SELECT handle FROM txn WHERE json_extract(blob, '$.scheduled_from') IS NOT NULL"
        ).fetchall()
        assert [row[0] for row in flagged] == [new.handle]
        assert db.verify_book() == []
    finally:
        db.close()


def test_a_minimized_book_is_smaller(tmp_path, book):
    from breadsched.gen.sample_book import create_sample_book

    path = tmp_path / "sample.breadsched"
    create_sample_book(path, as_of=date(2026, 6, 1))
    db = DbSQLite()
    db.load(str(path), mode="r")
    try:
        sizes = [
            (len(json.dumps(minimize_transaction(t.serialize()))), len(json.dumps(t.serialize())))
            for t in db.iter_transactions()
        ]
    finally:
        db.close()
    assert sizes and sum(small for small, _ in sizes) < 0.7 * sum(full for _, full in sizes)
