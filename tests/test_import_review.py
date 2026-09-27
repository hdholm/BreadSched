"""Re-import must not silently replace transactions reconciled in BreadSched.

Issue #117: a split reconciled locally keeps its reconcile state across an
unchanged refresh, and a GnuCash change to a protected fact is held for an
explicit, atomic, batched decision instead of being applied.
"""

from __future__ import annotations

import sqlite3

import pytest

from breadsched.gen.db.verification import verify_domain
from breadsched.gen.engine import import_review, ledger
from breadsched.gen.engine import reconciliation as reconcile_engine
from breadsched.gen.lib import ReconcileState
from breadsched.gen.services.import_review import (
    HeldImportDecision,
    ResolveHeldImports,
    pending_import_changes,
    resolve_import_changes,
)
from breadsched.plugins.importer import gnucash_sqlite


def _reconcile_checking(db, ids):
    """Complete a BreadSched statement covering every imported checking split."""
    transactions = [
        item
        for item in db.iter_transactions()
        if any(split.account == ids.checking for split in item.splits)
    ]
    statement = max(item.post_date for item in transactions)
    reconciliation = reconcile_engine.start(
        db, ids.checking, statement, ledger.balance(db, ids.checking, statement)
    )
    reconcile_engine.set_selection(
        db,
        reconciliation.handle,
        [
            split.handle
            for item in transactions
            for split in item.splits
            if split.account == ids.checking
        ],
    )
    return reconcile_engine.complete(db, reconciliation.handle)


def _transaction(db, description):
    return next(item for item in db.iter_transactions() if item.description == description)


def _checking_split(transaction, ids):
    return next(split for split in transaction.splits if split.account == ids.checking)


def _snapshot(db):
    return (
        sorted((item.handle, repr(item.serialize())) for item in db.iter_transactions()),
        db.get_metadata(import_review.REVIEW_KEY, {}),
    )


@pytest.fixture
def reconciled(db, gnucash_sqlite_path):
    gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    statement = _reconcile_checking(db, gnucash_sqlite_path.ids)
    return gnucash_sqlite_path, statement


def _set_source(path, sql, *params):
    with sqlite3.connect(path) as source:
        source.execute(sql, params)


def test_unchanged_reimport_keeps_breadsched_reconciliation(db, reconciled):
    source, statement = reconciled

    result = gnucash_sqlite.import_book(db, source.path)

    rent = _transaction(db, "Rent")
    split = _checking_split(rent, source.ids)
    assert split.reconcile is ReconcileState.RECONCILED
    assert split.reconcile_date == statement.statement_date
    assert result.transactions_unchanged == 3
    assert result.transactions_held == result.transactions_refreshed == 0
    assert not [issue for issue in verify_domain(db) if issue.code.startswith("reconciliation")]
    reconcile_engine.reopen(db, statement.handle)


def test_change_confined_to_unreconciled_split_applies(db, reconciled):
    source, statement = reconciled
    rent = _transaction(db, "Rent")
    expense_split = next(split for split in rent.splits if split.account == source.ids.rent)
    _set_source(
        source.path, "UPDATE splits SET memo=? WHERE guid=?", "revised", expense_split.handle
    )

    result = gnucash_sqlite.import_book(db, source.path)

    refreshed = db.get_transaction(rent.handle)
    assert next(s for s in refreshed.splits if s.handle == expense_split.handle).memo == "revised"
    assert _checking_split(refreshed, source.ids).reconcile is ReconcileState.RECONCILED
    assert result.transactions_refreshed == 1
    assert result.transactions_held == 0
    assert pending_import_changes(db) == []


def test_protected_change_is_held_and_blocked_by_completed_statement(db, reconciled):
    source, statement = reconciled
    rent = _transaction(db, "Rent")
    checking = _checking_split(rent, source.ids)
    expense = next(split for split in rent.splits if split.account == source.ids.rent)
    _set_source(
        source.path,
        "UPDATE splits SET value_num=-185000, quantity_num=-185000 WHERE guid=?",
        checking.handle,
    )
    _set_source(
        source.path,
        "UPDATE splits SET value_num=185000, quantity_num=185000 WHERE guid=?",
        expense.handle,
    )
    before = repr(db.get_transaction(rent.handle).serialize())

    result = gnucash_sqlite.import_book(db, source.path)

    assert result.transactions_held == 1
    assert "1 GnuCash change(s) held for review" in result.detail()
    assert repr(db.get_transaction(rent.handle).serialize()) == before
    [held] = pending_import_changes(db)
    assert held.transaction == rent.handle
    assert any("Checking Account amount" in line for line in held.changes)
    assert held.blocked_by and not held.can_use_source

    snapshot = _snapshot(db)
    refused = resolve_import_changes(
        db, ResolveHeldImports(((rent.handle, HeldImportDecision.USE_SOURCE),))
    )
    assert not refused.ok
    assert refused.errors[0].code == "import.review.reconciliation_blocks"
    assert _snapshot(db) == snapshot

    reconcile_engine.reopen(db, statement.handle)
    [unblocked] = pending_import_changes(db)
    assert unblocked.can_use_source
    applied = resolve_import_changes(
        db, ResolveHeldImports(((rent.handle, HeldImportDecision.USE_SOURCE),))
    )
    assert applied.ok and applied.value.applied == 1
    assert _checking_split(db.get_transaction(rent.handle), source.ids).value == -1850
    assert pending_import_changes(db) == []


def test_keep_local_is_remembered_until_source_changes_again(db, reconciled):
    source, _statement = reconciled
    rent = _transaction(db, "Rent")
    _set_source(
        source.path,
        "UPDATE transactions SET description=? WHERE guid=?",
        "Rent (edited)",
        rent.handle,
    )
    gnucash_sqlite.import_book(db, source.path)
    assert [item.transaction for item in pending_import_changes(db)] == [rent.handle]

    kept = resolve_import_changes(
        db, ResolveHeldImports(((rent.handle, HeldImportDecision.KEEP_LOCAL),))
    )
    assert kept.ok and kept.value.kept == 1
    assert pending_import_changes(db) == []
    again = gnucash_sqlite.import_book(db, source.path)
    assert again.transactions_kept == 1 and again.transactions_held == 0
    assert db.get_transaction(rent.handle).description == "Rent"

    _set_source(
        source.path,
        "UPDATE transactions SET description=? WHERE guid=?",
        "Rent (edited twice)",
        rent.handle,
    )
    changed = gnucash_sqlite.import_book(db, source.path)
    assert changed.transactions_held == 1
    assert pending_import_changes(db)[0].changes == ("Description: Rent -> Rent (edited twice)",)


def test_use_source_keeps_reconciliation_and_annotations_and_is_undoable(db, reconciled):
    source, statement = reconciled
    rent = _transaction(db, "Rent")
    rent.notes = "local planning note"
    with db.transaction("Annotate") as txn:
        db.commit_transaction(rent, txn)
    _set_source(
        source.path,
        "UPDATE transactions SET description=? WHERE guid=?",
        "Rent (edited)",
        rent.handle,
    )
    gnucash_sqlite.import_book(db, source.path)
    before = _snapshot(db)

    result = resolve_import_changes(
        db, ResolveHeldImports(((rent.handle, HeldImportDecision.USE_SOURCE),))
    )

    assert result.ok and result.value.applied == 1
    updated = db.get_transaction(rent.handle)
    assert updated.description == "Rent (edited)"
    assert updated.notes == "local planning note"
    split = _checking_split(updated, source.ids)
    assert split.reconcile is ReconcileState.RECONCILED
    assert split.reconcile_date == statement.statement_date
    assert not [issue for issue in verify_domain(db) if issue.code.startswith("reconciliation")]
    assert db.undo() is True
    assert _snapshot(db) == before


def test_later_leaves_the_change_pending_and_batch_is_atomic(db, reconciled):
    source, _statement = reconciled
    rent = _transaction(db, "Rent")
    payroll = _transaction(db, "Payroll deposit")
    for item in (rent, payroll):
        _set_source(source.path, "UPDATE transactions SET num=? WHERE guid=?", "42", item.handle)
    gnucash_sqlite.import_book(db, source.path)
    assert len(pending_import_changes(db)) == 2
    snapshot = _snapshot(db)

    rejected = resolve_import_changes(
        db,
        ResolveHeldImports(
            (
                (rent.handle, HeldImportDecision.KEEP_LOCAL),
                ("not-a-held-transaction", HeldImportDecision.USE_SOURCE),
            )
        ),
    )
    assert rejected.errors[0].code == "import.review.not_pending"
    assert _snapshot(db) == snapshot

    later = resolve_import_changes(
        db,
        ResolveHeldImports(
            (
                (rent.handle, HeldImportDecision.LATER),
                (payroll.handle, HeldImportDecision.USE_SOURCE),
            )
        ),
    )
    assert later.ok and (later.value.deferred, later.value.applied) == (1, 1)
    assert [item.transaction for item in pending_import_changes(db)] == [rent.handle]
    assert db.get_transaction(payroll.handle).num == "42"


def test_source_reconciled_split_change_is_held_but_not_blocked(db, gnucash_sqlite_path):
    source = gnucash_sqlite_path
    gnucash_sqlite.import_book(db, source.path)
    rent = _transaction(db, "Rent")
    checking = _checking_split(rent, source.ids)
    _set_source(source.path, "UPDATE splits SET reconcile_state='y' WHERE guid=?", checking.handle)
    gnucash_sqlite.import_book(db, source.path)
    assert _checking_split(db.get_transaction(rent.handle), source.ids).reconcile is (
        ReconcileState.RECONCILED
    )

    _set_source(
        source.path,
        "UPDATE splits SET reconcile_state='n', memo='fixed' WHERE guid=?",
        checking.handle,
    )
    result = gnucash_sqlite.import_book(db, source.path)

    assert result.transactions_held == 1
    [held] = pending_import_changes(db)
    assert held.can_use_source
    assert resolve_import_changes(
        db, ResolveHeldImports(((rent.handle, HeldImportDecision.USE_SOURCE),))
    ).ok
    updated = _checking_split(db.get_transaction(rent.handle), source.ids)
    assert updated.memo == "fixed"
    # Account, value, and quantity are unchanged, so the local Reconciled state stays.
    assert updated.reconcile is ReconcileState.RECONCILED


def test_unreconciled_transactions_are_still_overwritten(db, gnucash_sqlite_path):
    source = gnucash_sqlite_path
    gnucash_sqlite.import_book(db, source.path)
    rent = _transaction(db, "Rent")
    rent.description = "local edit"
    with db.transaction("Edit") as txn:
        db.commit_transaction(rent, txn)

    result = gnucash_sqlite.import_book(db, source.path)

    assert db.get_transaction(rent.handle).description == "Rent"
    assert result.transactions_refreshed == 1 and result.transactions_held == 0


def test_cli_lists_and_resolves_held_changes(capsys, tmp_path, gnucash_sqlite_path):
    import json

    from breadsched.cli.main import main
    from breadsched.gen.db.sqlite import DbSQLite

    book = str(tmp_path / "held.breadsched")
    source = gnucash_sqlite_path
    assert main(["init", book]) == 0
    assert main(["import", book, source.path, "--no-infer"]) == 0
    db = DbSQLite()
    db.load(book)
    try:
        _reconcile_checking(db, source.ids)
        rent = _transaction(db, "Rent")
    finally:
        db.close()
    _set_source(
        source.path,
        "UPDATE transactions SET description=? WHERE guid=?",
        "Rent (edited)",
        rent.handle,
    )
    capsys.readouterr()
    assert main(["import", book, source.path, "--no-infer", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["transactions_held"] == 1

    assert main(["import-review", book, "--json"]) == 0
    [listed] = json.loads(capsys.readouterr().out)
    assert listed["transaction"] == rent.handle
    assert listed["changes"] == ["Description: Rent -> Rent (edited)"]
    assert listed["can_use_gnucash"] is True

    assert main(["import-review", book, "--use-gnucash", rent.handle[:12], "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"kept": 0, "applied": 1, "deferred": 0}
    assert main(["import-review", book]) == 0
    assert "No GnuCash changes are held" in capsys.readouterr().out


def test_xml_reimport_uses_the_same_hold_rule(db, tmp_path, gnucash_xml_path):
    import xml.etree.ElementTree as ET
    from datetime import date

    from breadsched.plugins.importer import gnucash_xml

    source = tmp_path / "held.gnucash"
    source.write_text(gnucash_xml_path.plain, encoding="utf-8")
    gnucash_xml.import_book(db, source, include_scheduled=False)
    transaction = next(iter(db.iter_transactions()))
    transaction.splits[0].reconcile = ReconcileState.RECONCILED
    transaction.splits[0].reconcile_date = date(2026, 1, 31)
    with db.transaction("Reconcile locally") as txn:
        db.commit_transaction(transaction, txn)

    unchanged = gnucash_xml.import_book(db, source, include_scheduled=False)
    kept_split = db.get_transaction(transaction.handle).splits[0]
    assert kept_split.reconcile is ReconcileState.RECONCILED
    assert kept_split.reconcile_date == date(2026, 1, 31)
    assert unchanged.transactions_held == 0

    tree = ET.parse(source)
    ns = gnucash_xml.NS
    for element in tree.iter(f"{{{ns['gnc']}}}transaction"):
        if element.findtext("trn:id", namespaces=ns) == transaction.handle:
            element.find("trn:description", ns).text = "changed in GnuCash"
    tree.write(source, encoding="unicode", xml_declaration=True)

    held = gnucash_xml.import_book(db, source, include_scheduled=False)

    assert held.transactions_held == 1
    assert db.get_transaction(transaction.handle).description == transaction.description
    assert [item.transaction for item in pending_import_changes(db)] == [transaction.handle]
