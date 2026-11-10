"""One note per transaction and account, shared with GnuCash (#385).

Before schema 13 an imported record kept BreadSched's note and a separate,
read-only copy of the source's. Schema 13 merges them into one BreadSched-owned
note: the first import seeds it, a re-import takes the source's text only while
BreadSched left the note alone, and write-back sends it to GnuCash.
"""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.lib import Money, Transaction
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
