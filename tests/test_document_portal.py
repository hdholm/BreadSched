"""Books a sandboxed BreadSched reaches through the document portal.

The Flatpak may use Documents directly. A book chosen anywhere else arrives as
``$XDG_RUNTIME_DIR/doc/<id>/<name>``: a directory holding only that file, where a
file created beside the book never appears under its own name on the host. So
nothing that belongs beside such a book may be written there. Backups, the web
upload folder, and import logs go to BreadSched's data folder instead, and
attachments need a chosen folder. These tests stand in for the portal with a
directory at the same place; CI repeats the check with the real portal inside the
Flatpak sandbox.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from breadsched.gen.db.sqlite import DbSQLite, is_portal_book, migration_backup_path
from breadsched.gen.engine import attachments
from breadsched.gen.utils.user_paths import companion_path, data_directory, portal_document_id
from breadsched.presentation import book_open_notice

FIXTURE = Path(__file__).parent / "fixtures" / "native" / "schema-6.sql"


@pytest.fixture
def portal(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    home = tmp_path / "home"
    # Every platform's data folder lives under this fake home.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    document = runtime / "doc" / "4f2a9c1e"
    document.mkdir(parents=True)
    return document, data_directory() / "beside-documents" / "4f2a9c1e"


def test_portal_paths_are_recognised_inside_and_outside_the_sandbox(tmp_path, portal):
    document, _companions = portal
    assert portal_document_id(document / "book.breadsched") == "4f2a9c1e"
    host = tmp_path / "runtime" / "doc" / "by-app" / "org.breadsched.BreadSched" / "77aa"
    assert portal_document_id(host / "book.breadsched") == "77aa"
    assert portal_document_id(tmp_path / "Documents" / "book.breadsched") is None
    assert portal_document_id(Path("/run/flatpak/doc/abc/book.breadsched")) == "abc"
    assert not is_portal_book(":memory:")


def test_companions_of_an_ordinary_book_stay_beside_it(tmp_path, portal):
    book = tmp_path / "Documents" / "book.breadsched"
    assert companion_path(book, ".pre-restore.bak") == book.with_name(
        "book.breadsched.pre-restore.bak"
    )
    assert migration_backup_path(book, 6) == book.with_name("book.breadsched.pre-migration-v6.bak")


def test_a_migration_backup_of_a_portal_book_goes_to_the_data_folder(portal):
    document, companions = portal
    book = document / "schema-6.breadsched"
    with sqlite3.connect(book) as raw:
        raw.executescript(FIXTURE.read_text())

    db = DbSQLite()
    db.load(str(book))
    try:
        assert db.get_metadata("schema_version") == 10
        expected = companions / "schema-6.breadsched.pre-migration-v6.bak"
        assert db.migration_backup == str(expected)
    finally:
        db.close()
    assert expected.is_file()
    # Only the book itself is left in the portal directory.
    assert sorted(item.name for item in document.iterdir()) == ["schema-6.breadsched"]
    with sqlite3.connect(expected) as raw:
        row = raw.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    assert row == ("6",)


def test_restoring_over_a_portal_book_keeps_the_old_book_in_the_data_folder(tmp_path, portal):
    document, companions = portal
    book = document / "book.breadsched"
    for target in (book, tmp_path / "source.breadsched"):
        db = DbSQLite()
        db.load(str(target))
        db.close()
    backup = tmp_path / "source.bak"
    source = DbSQLite()
    source.load(str(tmp_path / "source.breadsched"))
    source.backup_to(str(backup))
    source.close()

    DbSQLite.restore_backup(str(backup), str(book), overwrite=True)

    assert (companions / "book.breadsched.pre-restore.bak").is_file()
    assert sorted(item.name for item in document.iterdir()) == ["book.breadsched"]


def test_a_portal_book_has_no_attachment_folder_until_one_is_chosen(tmp_path, portal):
    document, _companions = portal
    db = DbSQLite()
    db.load(str(document / "book.breadsched"))
    try:
        assert attachments.attachment_folder(db) is None
        chosen = tmp_path / "Receipts"
        with db.transaction("folder") as txn:
            db.set_metadata(attachments.FOLDER_KEY, str(chosen), txn)
        assert attachments.attachment_folder(db) == chosen
    finally:
        db.close()


def test_opening_a_portal_book_says_where_its_companions_go(tmp_path, portal):
    document, _companions = portal
    notice = book_open_notice(str(document / "book.breadsched"))
    assert notice is not None and "Keep books in Documents" in notice
    assert book_open_notice(str(tmp_path / "book.breadsched")) is None
    upgraded = book_open_notice(str(tmp_path / "book.breadsched"), migration_backup="/x.bak")
    assert upgraded is not None and "/x.bak" in upgraded
