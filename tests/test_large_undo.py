"""Undoing a large step says what it undoes, and shows its progress (#405)."""

from __future__ import annotations

from datetime import date

from breadsched.gen.lib import Transaction


def _import_like(db, book, count):
    """One undo step adding ``count`` transactions, as an import does."""
    with db.transaction("Import GnuCash book") as txn:
        for day in range(count):
            db.add_transaction(
                Transaction.simple(
                    date(2026, 1, 1 + day % 28), f"Imported {day}", book.rent, book.checking, "10"
                ),
                txn,
            )


def test_the_preview_names_the_step_and_counts_its_changes(db, book):
    _import_like(db, book, 3)
    before = {t.handle for t in db.iter_transactions()}
    preview = db.undo_preview()
    assert preview is not None
    assert preview.message == "Import GnuCash book"
    assert preview.total == 3
    assert preview.changes == (("transaction", "added", 3),)
    # The preview changes nothing.
    assert {t.handle for t in db.iter_transactions()} == before


def test_progress_is_reported_and_the_signals_can_wait(db, book):
    _import_like(db, book, 450)
    heard: list[str] = []
    db.connect("database-changed", lambda *_args: heard.append("changed"))
    progress: list[tuple[int, int]] = []
    assert db.undo(lambda done, total: progress.append((done, total)), announce=False)
    assert progress == [(200, 450), (400, 450), (450, 450)]
    assert not [t for t in db.iter_transactions() if t.description.startswith("Imported")]
    assert heard == []
    db.announce_undo()
    assert heard == ["changed"]
    assert db.redo()
    assert len([t for t in db.iter_transactions() if t.description.startswith("Imported")]) == 450


def test_nothing_to_undo_previews_nothing(db):
    while db.undo_stack:
        db.undo()
    assert db.undo_preview() is None
