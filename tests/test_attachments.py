"""Transaction tags and linked documents, including ones that go missing."""

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine import attachments
from breadsched.gen.lib import Money, Split, Transaction
from breadsched.gen.services import (
    attach_file,
    attach_location,
    attachment_report,
    detach,
    relink,
    set_attachment_folder,
    set_source_link_folder,
    set_tags,
    tag_counts,
    transactions_with_tag,
)
from breadsched.plugins.importer import gnucash_sqlite, gnucash_xml


def _purchase(db, book, description="Hardware store", tags=()):
    transaction = Transaction(
        post_date=date(2026, 9, 1),
        description=description,
        splits=[
            Split(account=book.groceries, value=Money(42)),
            Split(account=book.checking, value=Money(-42)),
        ],
    )
    transaction.tags = list(tags)
    with db.transaction("Purchase") as txn:
        db.add_transaction(transaction, txn)
    return transaction


@pytest.fixture
def file_db(tmp_path):
    database = DbSQLite()
    database.load(str(tmp_path / "household.breadsched"))
    yield database
    database.close()


@pytest.fixture
def file_book(file_db):
    from breadsched.gen.lib import Account, AccountType

    with file_db.transaction("Accounts") as txn:
        root = Account(name="Root", atype=AccountType.ROOT)
        file_db.add_account(root, txn)
        checking = Account(name="Checking", atype=AccountType.BANK, parent=root.handle)
        groceries = Account(name="Groceries", atype=AccountType.EXPENSE, parent=root.handle)
        file_db.add_account(checking, txn)
        file_db.add_account(groceries, txn)

    class Book:
        pass

    book = Book()
    book.checking, book.groceries = checking.handle, groceries.handle
    return book


def test_tags_are_normalized_deduplicated_and_reuse_the_books_spelling(db, book):
    first = _purchase(db, book, tags=["Home Repair"])
    second = _purchase(db, book, "Paint")
    result = set_tags(db, second.handle, ["  home   repair ", "Tax", "tax", ""])
    assert result.ok
    assert db.get_transaction(second.handle).tags == ["Home Repair", "Tax"]
    assert [(item.tag, item.transactions) for item in tag_counts(db)] == [
        ("Home Repair", 2),
        ("Tax", 1),
    ]
    assert {item.handle for item in transactions_with_tag(db, "HOME REPAIR")} == {
        first.handle,
        second.handle,
    }


def test_rejected_tags_keep_the_stored_transaction(db, book):
    transaction = _purchase(db, book, tags=["Tax"])
    for bad in (["a,b"], ["x" * 65]):
        result = set_tags(db, transaction.handle, bad)
        assert [error.code for error in result.errors] == ["tag.invalid"]
    assert db.get_transaction(transaction.handle).tags == ["Tax"]
    missing = set_tags(db, "nope", ["Tax"])
    assert [error.code for error in missing.errors] == ["transaction.not_found"]


def test_attaching_copies_into_a_folder_beside_the_book(file_db, file_book, tmp_path):
    transaction = _purchase(file_db, file_book)
    receipt = tmp_path / "elsewhere" / "receipt.pdf"
    receipt.parent.mkdir()
    receipt.write_bytes(b"%PDF")
    folder = tmp_path / "household attachments"

    first = attach_file(file_db, transaction.handle, receipt)
    assert first.ok and first.value.attachments == ["receipt.pdf"]
    assert (folder / "receipt.pdf").read_bytes() == b"%PDF"
    # A second copy of the same name is never overwritten.
    other = _purchase(file_db, file_book, "Second")
    second = attach_file(file_db, other.handle, receipt)
    assert second.value.attachments == ["receipt-2.pdf"]
    assert (folder / "receipt-2.pdf").exists()

    linked = attach_file(file_db, other.handle, receipt, copy=False)
    assert linked.value.attachments[-1] == str(receipt.resolve())
    duplicate = attach_file(file_db, other.handle, receipt, copy=False)
    assert [error.code for error in duplicate.errors] == ["attachment.duplicate"]

    report = attachment_report(file_db)
    assert report.folder == folder
    assert report.missing == ()
    assert len(report.attachments) == 3


def test_a_refused_attachment_removes_its_copy(file_db, file_book, tmp_path):
    transaction = _purchase(file_db, file_book)
    receipt = tmp_path / "receipt.pdf"
    receipt.write_bytes(b"%PDF")
    folder = tmp_path / "household attachments"
    folder.mkdir()
    (folder / "receipt.pdf").write_bytes(b"old")
    transaction.attachments = ["receipt-2.pdf"]
    with file_db.transaction("Seed") as txn:
        file_db.commit_transaction(transaction, txn)
    # The copy would be named receipt-2.pdf, which the transaction already lists.
    result = attach_file(file_db, transaction.handle, receipt)
    assert [error.code for error in result.errors] == ["attachment.duplicate"]
    assert not (folder / "receipt-2.pdf").exists()
    assert (folder / "receipt.pdf").read_bytes() == b"old"
    assert file_db.get_transaction(transaction.handle).attachments == ["receipt-2.pdf"]


def test_missing_documents_are_reported_and_can_be_relinked(file_db, file_book, tmp_path):
    transaction = _purchase(file_db, file_book)
    kept = tmp_path / "kept.pdf"
    kept.write_bytes(b"x")
    assert attach_location(file_db, transaction.handle, "receipts/gone.pdf").ok
    assert attach_location(file_db, transaction.handle, "https://example.com/invoice").ok
    assert attach_location(file_db, transaction.handle, kept.as_uri()).ok

    statuses = attachments.statuses(file_db, file_db.get_transaction(transaction.handle))
    assert [(item.kind, item.present) for item in statuses] == [
        ("file", False),
        ("web", None),
        ("file", True),
    ]
    missing = attachment_report(file_db, missing_only=True)
    assert [item.location for item in missing.attachments] == ["receipts/gone.pdf"]
    assert missing.attachments[0].path == tmp_path / "household attachments/receipts/gone.pdf"

    moved = tmp_path / "found.pdf"
    moved.write_bytes(b"y")
    result = relink(file_db, transaction.handle, "receipts/gone.pdf", str(moved))
    assert result.value.attachments[0] == str(moved)
    assert attachment_report(file_db, missing_only=True).attachments == ()

    assert detach(file_db, transaction.handle, str(moved)).ok
    assert moved.exists()  # unlinking never deletes the file
    unknown = detach(file_db, transaction.handle, str(moved))
    assert [error.code for error in unknown.errors] == ["attachment.not_found"]


def test_attachment_folder_can_move_and_relative_links_follow(file_db, file_book, tmp_path):
    transaction = _purchase(file_db, file_book)
    attach_location(file_db, transaction.handle, "roof.pdf")
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "roof.pdf").write_bytes(b"x")
    assert attachment_report(file_db).missing
    moved = set_attachment_folder(file_db, "Documents")
    assert moved.value == tmp_path / "Documents"
    assert not attachment_report(file_db).missing
    assert set_attachment_folder(file_db, "").value == tmp_path / "household attachments"


def test_an_in_memory_book_has_no_folder_for_copies(db, book, tmp_path):
    transaction = _purchase(db, book)
    receipt = tmp_path / "receipt.pdf"
    receipt.write_bytes(b"x")
    refused = attach_file(db, transaction.handle, receipt)
    assert [error.code for error in refused.errors] == ["attachment.folder.unavailable"]
    relative = set_attachment_folder(db, "docs")
    assert [error.code for error in relative.errors] == ["attachment.folder.relative"]
    assert set_attachment_folder(db, str(tmp_path)).value == tmp_path
    absent = attach_file(db, transaction.handle, tmp_path / "absent.pdf")
    assert [error.code for error in absent.errors] == ["attachment.file.not_found"]
    assert db.get_transaction(transaction.handle).attachments == []


def test_windows_file_uris_resolve_to_drive_paths(db):
    assert str(attachments.resolve(db, "file:///C:/Receipts/a%20b.pdf")).endswith("a b.pdf")
    assert attachments.resolve(db, "file:///C:/Receipts/x.pdf").parts[0] in {"C:", "C:\\", "C:/"}


def _link_supermarket(path, name, value):
    with sqlite3.connect(path) as source:
        guid = source.execute(
            "SELECT guid FROM transactions WHERE description = 'Supermarket'"
        ).fetchone()[0]
        source.execute(
            "INSERT INTO slots (obj_guid,name,slot_type,string_val) VALUES (?,?,?,?)",
            (guid, name, 4, value),
        )
    return guid


def test_gnucash_linked_document_is_kept_as_a_link_and_resolved_in_place(
    db, gnucash_sqlite_path, tmp_path
):
    guid = _link_supermarket(gnucash_sqlite_path.path, "assoc_uri", "receipts/old.pdf")
    _link_supermarket(gnucash_sqlite_path.path, "doclink", "receipts/market.pdf")
    gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    transaction = db.get_transaction(guid)
    assert transaction.source_link == "receipts/market.pdf"
    assert transaction.attachments == []

    (tmp_path / "receipts").mkdir()
    (tmp_path / "receipts" / "market.pdf").write_bytes(b"x")
    assert set_source_link_folder(db, str(tmp_path)).value == tmp_path
    (status,) = attachments.statuses(db, transaction)
    assert (status.owner, status.present) == ("source", True)
    assert status.path == tmp_path / "receipts" / "market.pdf"
    relative = set_source_link_folder(db, "relative")
    assert [error.code for error in relative.errors] == ["attachment.folder.relative"]


def test_reimport_keeps_breadsched_tags_and_attachments(db, gnucash_sqlite_path):
    guid = _link_supermarket(gnucash_sqlite_path.path, "doclink", "https://example.com/r")
    first = gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    assert set_tags(db, guid, ["Tax"]).ok
    assert attach_location(db, guid, "https://example.com/extra").ok

    second = gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    reimported = db.get_transaction(guid)
    assert reimported.tags == ["Tax"]
    assert reimported.attachments == ["https://example.com/extra"]
    assert reimported.source_link == "https://example.com/r"
    assert second.transactions_unchanged == first.transactions

    with sqlite3.connect(gnucash_sqlite_path.path) as source:
        source.execute(
            "UPDATE slots SET string_val=? WHERE obj_guid=? AND name='doclink'",
            ("https://example.com/new", guid),
        )
    third = gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    assert third.transactions_refreshed == 1
    assert db.get_transaction(guid).source_link == "https://example.com/new"
    assert db.get_transaction(guid).tags == ["Tax"]


def test_gnucash_xml_linked_document_is_imported(db, gnucash_xml_path, tmp_path):
    body = gnucash_xml_path.plain.replace(
        "<trn:splits>",
        "<trn:slots><slot><slot:key>doclink</slot:key>"
        '<slot:value type="string">file:///srv/docs/salary.pdf</slot:value></slot>'
        "</trn:slots>\n    <trn:splits>",
        1,
    )
    path = tmp_path / "linked.gnucash"
    path.write_text(body, encoding="utf-8")
    gnucash_xml.import_book(db, str(path), include_scheduled=False)
    linked = [item for item in db.iter_transactions() if item.source_link]
    assert [item.source_link for item in linked] == ["file:///srv/docs/salary.pdf"]
    (status,) = attachments.statuses(db, linked[0])
    assert status.path == Path("/srv/docs/salary.pdf") and status.missing


def test_cli_tags_attaches_reports_missing_and_filters_the_register(tmp_path, capsys):
    import json

    from breadsched.cli.main import main

    path = str(tmp_path / "docs.breadsched")
    assert main(["sample", path, "--as-of", "2026-09-15"]) == 0
    capsys.readouterr()
    assert main(["accounts", path, "--json"]) == 0
    bank = next(
        item
        for item in json.loads(capsys.readouterr().out)
        if item.get("type", item.get("atype")) == "BANK"
    )
    assert main(["register", path, bank["handle"], "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    handle = rows[-1]["handle"]

    assert main(["tags", path, "--transaction", handle[:12], "--set", "Tax, Home repair"]) == 0
    assert "Tax, Home repair" in capsys.readouterr().out
    assert main(["tags", path, "--json"]) == 0
    assert {"tag": "Tax", "transactions": 1} in json.loads(capsys.readouterr().out)
    assert main(["tags", path, "tax", "--json"]) == 0
    assert [item["handle"] for item in json.loads(capsys.readouterr().out)] == [handle]
    assert main(["register", path, bank["handle"], "--tag", "home repair", "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert row["handle"] == handle and row["tags"] == ["Tax", "Home repair"]
    assert main(["tags", path, "--transaction", handle, "--set", "a" * 70]) != 0
    capsys.readouterr()

    receipt = tmp_path / "receipt.pdf"
    receipt.write_bytes(b"%PDF")
    assert main(["attachments", path, "--transaction", handle, "--add", str(receipt)]) == 0
    assert "Linked receipt.pdf" in capsys.readouterr().out
    assert (tmp_path / "docs attachments" / "receipt.pdf").exists()
    assert main(["attachments", path, "--transaction", handle, "--add", "https://x.test/i"]) == 0
    capsys.readouterr()
    (tmp_path / "docs attachments" / "receipt.pdf").unlink()
    assert main(["attachments", path, "--missing"]) == 0
    out = capsys.readouterr().out
    assert "receipt.pdf" in out and "missing" in out and "x.test" not in out
    assert (
        main(
            ["attachments", path, "--transaction", handle, "--relink", "receipt.pdf"]
            + ["--to", str(receipt)]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["attachments", path, "--missing", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["attachments"] == []
    assert main(["attachments", path, "--transaction", handle, "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)["attachments"]
    assert [(item["kind"], item["present"]) for item in listed] == [
        ("file", True),
        ("web", None),
    ]
    assert main(["attachments", path, "--transaction", handle, "--remove", str(receipt)]) == 0
    assert receipt.exists()
    capsys.readouterr()
    assert main(["attachments", path, "--folder", "papers", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["folder"] == str(tmp_path / "papers")


def test_csv_export_carries_tags_and_documents_on_every_split(db, book, tmp_path):
    import csv

    from breadsched.plugins.export.csv_export import export_transactions

    transaction = _purchase(db, book, tags=["Tax", "Home repair"])
    transaction.attachments = ["receipts/a, b.pdf", "https://example.com/i"]
    transaction.source_link = "file:///srv/g.pdf"
    with db.transaction("Documents") as txn:
        db.commit_transaction(transaction, txn)
    out = tmp_path / "out.csv"
    assert export_transactions(db, out) == 2
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert {row["tags"] for row in rows} == {"Tax, Home repair"}
    assert {row["documents"] for row in rows} == {
        "receipts/a, b.pdf | https://example.com/i | file:///srv/g.pdf"
    }


def test_saving_a_transaction_sets_tags_only_when_given(db, book):
    from breadsched.gen.lib import Amount
    from breadsched.gen.services import (
        SaveTransaction,
        TransactionInput,
        TransactionSplitInput,
        save_transaction,
        transaction_currency,
    )

    _purchase(db, book, "Earlier", tags=["Home Repair"])
    transaction = _purchase(db, book, tags=["Tax"])
    transaction.attachments = ["kept.pdf"]
    with db.transaction("Documents") as txn:
        db.commit_transaction(transaction, txn)
    currency = transaction_currency(db)

    def request(tags):
        return SaveTransaction(
            TransactionInput(
                post_date=transaction.post_date,
                description="Hardware store",
                splits=tuple(
                    TransactionSplitInput(
                        split.account, Amount(split.value, currency), handle=split.handle
                    )
                    for split in transaction.splits
                ),
                tags=tags,
            ),
            existing_handle=transaction.handle,
        )

    assert save_transaction(db, request(None)).ok
    assert db.get_transaction(transaction.handle).tags == ["Tax"]
    assert save_transaction(db, request(("home repair", "Tax"))).ok
    stored = db.get_transaction(transaction.handle)
    assert stored.tags == ["Home Repair", "Tax"] and stored.attachments == ["kept.pdf"]
    refused = save_transaction(db, request(("a,b",)))
    assert [error.code for error in refused.errors] == ["tag.invalid"]
    assert db.get_transaction(transaction.handle).tags == ["Home Repair", "Tax"]


def test_contained_locations_stay_inside_the_attachment_folder(file_db, file_book, tmp_path):
    from breadsched.gen.services import contained_location

    folder = tmp_path / "household attachments"
    (folder / "2026").mkdir(parents=True)
    assert contained_location(file_db, "https://example.com/r") == "https://example.com/r"
    assert contained_location(file_db, "2026/./a.pdf") == "2026/a.pdf"
    assert contained_location(file_db, str(folder / "2026" / "a.pdf")) == "2026/a.pdf"
    for escape in ("../book.pdf", str(tmp_path / "x.pdf"), "2026/../../x.pdf", "file:x.pdf"):
        refused = contained_location(file_db, escape)
        assert getattr(refused, "code", None) == "attachment.outside_folder", escape
    assert contained_location(file_db, " ").code == "attachment.location.required"
