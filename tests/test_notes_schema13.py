"""One note per transaction and account, shared with GnuCash (#385).

Before schema 13 an imported record kept BreadSched's note and a separate,
read-only copy of the source's. Schema 13 merges them into one BreadSched-owned
note: the first import seeds it, a re-import takes the source's text only while
BreadSched left the note alone, and write-back sends it to GnuCash.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from breadsched.gen.db.sqlite import SCHEMA_VERSION, DbSQLite
from breadsched.gen.lib import Account, AccountType, Money, Transaction
from breadsched.gen.lib.notes import merge_legacy_notes, notes_digest, reconcile_imported_notes


def test_a_digest_is_short_stable_and_empty_for_an_empty_note():
    assert notes_digest("") == ""
    assert notes_digest("Receipt") == notes_digest("Receipt")
    assert notes_digest("Receipt") != notes_digest("Receipt.")
    assert len(notes_digest("Receipt")) == 24


@pytest.mark.parametrize(
    ("imported", "local", "merged"),
    [
        ("", "", ""),
        ("From GnuCash", "", "From GnuCash"),
        ("", "Mine", "Mine"),
        ("Same text", "Same text", "Same text"),
        ("From GnuCash\n", "", "From GnuCash\n"),
        ("From GnuCash \n", "Mine", "From GnuCash\n\nMine"),
    ],
)
def test_legacy_notes_merge_imported_first(imported, local, merged):
    # The digest is the source text's as GnuCash holds it, so the next import of
    # the same text is not mistaken for a GnuCash change.
    assert merge_legacy_notes(imported, local) == (merged, notes_digest(imported))


def test_an_import_reconciles_the_note_by_who_changed_it():
    seeded = notes_digest("GnuCash v1")
    # Nothing changed in GnuCash: BreadSched's edit stays.
    assert reconcile_imported_notes("Mine", seeded, "GnuCash v1") == ("Mine", seeded, False)
    # Only GnuCash changed: its text is taken.
    assert reconcile_imported_notes("GnuCash v1", seeded, "GnuCash v2") == (
        "GnuCash v2",
        notes_digest("GnuCash v2"),
        False,
    )
    # Both changed: BreadSched's note stays and the conflict is reported.
    assert reconcile_imported_notes("Mine", seeded, "GnuCash v2") == (
        "Mine",
        notes_digest("GnuCash v2"),
        True,
    )
    # GnuCash now holds BreadSched's text (after a write-back): nothing to do.
    assert reconcile_imported_notes("Mine", seeded, "Mine") == ("Mine", notes_digest("Mine"), False)


def test_a_pre_13_record_read_directly_merges_its_two_notes():
    transaction = Transaction.simple(date(2026, 1, 2), "Shop", "a", "b", Money("5"))
    raw = transaction.serialize()
    raw.pop("imported_notes_digest")
    raw["notes"], raw["source_notes"] = "Mine", "From GnuCash"
    restored = Transaction.from_dict(raw)
    assert restored.notes == "From GnuCash\n\nMine"
    assert restored.imported_notes_digest == notes_digest("From GnuCash")
    assert "source_notes" not in restored.serialize()


def test_a_schema_12_book_merges_each_records_two_notes(tmp_path):
    path = tmp_path / "notes.breadsched"
    db = DbSQLite()
    db.load(str(path))
    with db.transaction("Setup") as txn:
        root = Account(name="Root", atype=AccountType.ROOT)
        db.add_account(root, txn)
        checking = Account(name="Checking", atype=AccountType.BANK, parent=root.handle)
        food = Account(name="Food", atype=AccountType.EXPENSE, parent=root.handle)
        db.add_account(checking, txn)
        db.add_account(food, txn)
        both = Transaction.simple(date(2026, 1, 2), "Both", food.handle, checking.handle, "5")
        imported_only = Transaction.simple(
            date(2026, 1, 3), "Imported", food.handle, checking.handle, "6"
        )
        plain = Transaction.simple(date(2026, 1, 4), "Plain", food.handle, checking.handle, "7")
        for item in (both, imported_only, plain):
            db.add_transaction(item, txn)
    db.close()
    legacy = {
        ("txn", both.handle): ("From GnuCash", "Mine"),
        ("txn", imported_only.handle): ("Only GnuCash", ""),
        ("account", checking.handle): ("Bank note", "Bank note"),
    }
    with sqlite3.connect(path) as raw:
        for table, handle in [("txn", plain.handle), *legacy]:
            data = json.loads(
                raw.execute(f"SELECT blob FROM {table} WHERE handle=?", (handle,)).fetchone()[0]  # noqa: S608
            )
            data.pop("imported_notes_digest", None)
            data["source_notes"], data["notes"] = legacy.get((table, handle), ("", "Untouched"))
            raw.execute(
                f"UPDATE {table} SET blob=? WHERE handle=?",  # noqa: S608
                (json.dumps(data), handle),
            )
        raw.execute("UPDATE metadata SET value='12' WHERE key='schema_version'")
        raw.execute("DELETE FROM schema_migration")
        raw.execute("INSERT INTO schema_migration(version, applied_at) VALUES (12, 'test')")

    migrated = DbSQLite()
    migrated.load(str(path))
    try:
        assert migrated.get_metadata("schema_version") == SCHEMA_VERSION == 13
        assert migrated.migration_backup is not None
        assert migrated.get_transaction(both.handle).notes == "From GnuCash\n\nMine"
        assert migrated.get_transaction(imported_only.handle).notes == "Only GnuCash"
        assert migrated.get_transaction(plain.handle).notes == "Untouched"
        assert migrated.get_transaction(plain.handle).imported_notes_digest == ""
        bank = migrated.get_account(checking.handle)
        assert bank.notes == "Bank note"
        assert bank.imported_notes_digest == notes_digest("Bank note")
    finally:
        migrated.close()
    with sqlite3.connect(path) as raw:
        for table in ("txn", "account"):
            for (blob,) in raw.execute(f"SELECT blob FROM {table}"):  # noqa: S608
                assert "source_notes" not in json.loads(blob)
