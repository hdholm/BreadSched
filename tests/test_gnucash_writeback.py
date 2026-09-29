"""Write simple BreadSched edits back to the imported GnuCash SQLite book (#174)."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.lib import Money, ReconcileState, Transaction
from breadsched.gen.services.gnucash_writeback import (
    ApplyWriteback,
    apply_writeback,
    preview_writeback,
    set_writeback_keep_backups,
)
from breadsched.gen.services.imports import ImportBook, import_book


@pytest.fixture
def linked(tmp_path, gnucash_sqlite_path):
    """A file-backed BreadSched book imported from the GnuCash fixture."""
    db = DbSQLite()
    db.load(str(tmp_path / "home.breadsched"))
    imported = import_book(db, ImportBook(source=gnucash_sqlite_path.path, notify=False))
    assert imported.value is not None
    yield db, gnucash_sqlite_path
    db.close()


def _gnc(path, sql, *params):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _rent(db):
    return next(item for item in db.iter_transactions() if item.description == "Rent")


def test_preview_needs_an_imported_sqlite_book(db):
    result = preview_writeback(db)
    assert [error.code for error in result.errors] == ["writeback.source.unknown"]


def test_a_freshly_imported_book_has_nothing_to_write(linked):
    db, _gnc_book = linked
    plan = preview_writeback(db).value
    assert plan is not None
    assert plan.changes == ()


def test_text_edits_are_previewed_written_and_round_trip(linked):
    db, gnc = linked
    rent = _rent(db)
    rent.description = "Rent (January)"
    rent.num = "101"
    for split in rent.splits:
        if split.memo == "January":
            split.memo = "Jan rent"
    with db.transaction("Local edit") as txn:
        db.commit_transaction(rent, txn)
    before = (
        gnc.path.read_bytes() if hasattr(gnc.path, "read_bytes") else open(gnc.path, "rb").read()
    )

    plan = preview_writeback(db).value
    assert plan is not None
    [change] = plan.changes
    assert change.transaction == rent.handle and change.kinds == ("edit",)
    assert "description: 'Rent' -> 'Rent (January)'" in change.details
    assert "number: (blank) -> '101'" in change.details
    assert open(gnc.path, "rb").read() == before  # previewing writes nothing

    applied = apply_writeback(db, ApplyWriteback((rent.handle,)))
    assert applied.value is not None, applied.errors
    assert _gnc(
        gnc.path, "SELECT description, num FROM transactions WHERE guid=?", rent.handle
    ) == [("Rent (January)", "101")]
    assert ("Jan rent",) in _gnc(gnc.path, "SELECT memo FROM splits WHERE tx_guid=?", rent.handle)
    assert open(applied.value.backup, "rb").read() == before
    assert db.get_transaction(rent.handle).description == "Rent (January)"
    assert preview_writeback(db).value.changes == ()


def test_a_new_two_split_transaction_is_inserted_and_joins_the_import_baseline(linked):
    db, gnc = linked
    checking = db.get_account_by_name("Assets:Checking Account")
    groceries = db.get_account_by_name("Expenses:Groceries")
    assert checking is not None and groceries is not None
    entry = Transaction.simple(
        date(2026, 2, 3), "Farmers market", groceries.handle, checking.handle, "23.40"
    )
    with db.transaction("New entry") as txn:
        db.add_transaction(entry, txn)

    plan = preview_writeback(db).value
    assert plan is not None
    [change] = plan.changes
    assert change.kinds == ("new",)
    applied = apply_writeback(db, ApplyWriteback((entry.handle,)))
    assert applied.value is not None, applied.errors

    rows = _gnc(
        gnc.path,
        "SELECT account_guid, value_num, value_denom FROM splits WHERE tx_guid=? "
        "ORDER BY value_num",
        entry.handle,
    )
    assert rows == [(gnc.ids.checking, -2340, 100), (gnc.ids.food, 2340, 100)]
    assert _gnc(gnc.path, "SELECT description FROM transactions WHERE guid=?", entry.handle) == [
        ("Farmers market",)
    ]
    assert preview_writeback(db).value.changes == ()
    assert (
        len([item for item in db.iter_transactions() if item.description == "Farmers market"]) == 1
    )


def test_reconcile_state_set_in_breadsched_is_written(linked):
    db, gnc = linked
    rent = _rent(db)
    checking = db.get_account_by_name("Assets:Checking Account")
    target = next(split for split in rent.splits if split.account == checking.handle)
    target.reconcile = ReconcileState.RECONCILED
    target.reconcile_date = date(2026, 1, 31)
    with db.transaction("Reconcile") as txn:
        db.commit_transaction(rent, txn)

    [change] = preview_writeback(db).value.changes
    assert change.kinds == ("reconcile",)
    assert apply_writeback(db, ApplyWriteback((rent.handle,))).value is not None
    [(state, stamp)] = _gnc(
        gnc.path, "SELECT reconcile_state, reconcile_date FROM splits WHERE guid=?", target.handle
    )
    assert state == "y" and stamp.startswith("20260131")


def test_amount_changes_and_three_split_entries_are_listed_not_written(linked):
    db, gnc = linked
    rent = _rent(db)
    rent.splits[0].value = rent.splits[0].value + Money(1)
    rent.splits[1].value = rent.splits[1].value - Money(1)
    with db.transaction("Amount edit") as txn:
        db.commit_transaction(rent, txn)
    plan = preview_writeback(db).value
    assert plan is not None and plan.changes == ()
    [item] = plan.unsupported
    assert item.transaction == rent.handle
    assert "amount or account" in item.reason


def test_a_changed_or_locked_book_is_refused(linked):
    db, gnc = linked
    conn = sqlite3.connect(gnc.path)
    conn.execute("CREATE TABLE gnclock (Hostname varchar(255), PID int)")
    conn.execute("INSERT INTO gnclock VALUES ('host', 1)")
    conn.commit()
    conn.close()
    assert [error.code for error in preview_writeback(db).errors] == ["writeback.source.changed"]

    import_book(db, ImportBook(source=gnc.path, notify=False))
    assert [error.code for error in preview_writeback(db).errors] == ["writeback.source.locked"]


def test_a_failed_read_back_restores_the_book(linked, monkeypatch):
    from breadsched.plugins.export import gnucash_writeback

    db, gnc = linked
    rent = _rent(db)
    rent.description = "Rent edited"
    with db.transaction("Edit") as txn:
        db.commit_transaction(rent, txn)
    before = open(gnc.path, "rb").read()

    def broken(*_args):
        raise gnucash_writeback.WritebackError("writeback.verify.failed", "mismatch")

    monkeypatch.setattr(gnucash_writeback, "_verify", broken)
    result = apply_writeback(db, ApplyWriteback((rent.handle,)))
    assert [error.code for error in result.errors] == ["writeback.verify.failed"]
    assert open(gnc.path, "rb").read() == before


def test_backups_are_rotated_to_the_configured_count(linked):
    db, gnc = linked
    assert set_writeback_keep_backups(db, 0).errors[0].code == "writeback.keep_backups.invalid"
    assert set_writeback_keep_backups(db, 1).value == 1
    rent = _rent(db)
    backups = []
    for text in ("Rent A", "Rent B"):
        rent = db.get_transaction(rent.handle)
        rent.description = text
        with db.transaction("Edit") as txn:
            db.commit_transaction(rent, txn)
        applied = apply_writeback(db, ApplyWriteback((rent.handle,)))
        assert applied.value is not None, applied.errors
        backups.append(applied.value.backup)
    from pathlib import Path

    remaining = sorted(Path(backups[-1]).parent.glob("*.bak"))
    assert [str(item) for item in remaining] == [backups[-1]]


def test_cli_previews_and_applies(tmp_path, capsys, gnucash_sqlite_path):
    import json

    from breadsched.cli.main import main

    book = tmp_path / "cli.breadsched"
    assert main(["init", str(book)]) == 0
    assert main(["import", str(book), gnucash_sqlite_path.path]) == 0
    db = DbSQLite()
    db.load(str(book))
    rent = _rent(db)
    rent.description = "Rent via CLI"
    with db.transaction("Edit") as txn:
        db.commit_transaction(rent, txn)
    db.close()
    capsys.readouterr()

    assert main(["gnucash-writeback", str(book), "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert [item["transaction"] for item in preview["changes"]] == [rent.handle]
    assert main(["gnucash-writeback", str(book), "--all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["written"] == [rent.handle]
    assert _gnc(
        gnucash_sqlite_path.path, "SELECT description FROM transactions WHERE guid=?", rent.handle
    ) == [("Rent via CLI",)]


def test_unchosen_local_edits_survive_a_write(linked):
    db, gnc = linked
    rent = _rent(db)
    payroll = next(item for item in db.iter_transactions() if item.description == "Payroll deposit")
    rent.description = "Rent edited"
    payroll.description = "Payroll edited"
    with db.transaction("Edits") as txn:
        db.commit_transaction(rent, txn)
        db.commit_transaction(payroll, txn)

    assert apply_writeback(db, ApplyWriteback((rent.handle,))).value is not None

    assert db.get_transaction(payroll.handle).description == "Payroll edited"
    [pending] = preview_writeback(db).value.changes
    assert pending.transaction == payroll.handle


def test_a_round_trip_mismatch_restores_the_book(linked, monkeypatch):
    from breadsched.plugins.export import gnucash_writeback

    db, gnc = linked
    rent = _rent(db)
    rent.description = "Rent edited"
    with db.transaction("Edit") as txn:
        db.commit_transaction(rent, txn)
    before = open(gnc.path, "rb").read()
    monkeypatch.setattr(gnucash_writeback, "source_facts", lambda *_args: ("different",))

    result = apply_writeback(db, ApplyWriteback((rent.handle,)))

    assert [error.code for error in result.errors] == ["writeback.roundtrip.mismatch"]
    assert open(gnc.path, "rb").read() == before
