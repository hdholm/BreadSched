"""Stable payee identity with deterministic, previewed matching.

A payee is a separate, reviewable reference; it never rewrites the imported
description. Proposals come only from exact normalized description keys, each key
belongs to at most one payee, and nothing is assigned until the user accepts.
A transaction that already has a payee is never proposed again.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine.import_review import merge_local_state
from breadsched.gen.lib import Transaction
from breadsched.gen.services.payees import (
    SavePayee,
    apply_payee_proposals,
    assign_payee,
    delete_payee,
    match_key,
    preview_payee_proposals,
    save_payee,
)


def _spend(db, book, when, description, amount="10"):
    transaction = Transaction.simple(when, description, book.groceries, book.checking, amount)
    with db.transaction("Spend") as txn:
        db.add_transaction(transaction, txn)
    return transaction.handle


@pytest.mark.parametrize(
    ("description", "key"),
    [
        ("CORNER GROCER #1234", "corner grocer"),
        ("Corner Grocer 0987", "corner grocer"),
        ("  corner-grocer  ", "corner grocer"),
        ("CAFÉ Ｌｕｎａ *4411", "café luna"),
        ("12345 6789", ""),
    ],
)
def test_match_key_ignores_case_punctuation_and_reference_numbers(description, key):
    assert match_key(description) == key


def test_proposals_are_previewed_then_accepted_without_rewriting_descriptions(db, book):
    first = _spend(db, book, date(2026, 9, 1), "CORNER GROCER #1234")
    second = _spend(db, book, date(2026, 9, 8), "Corner Grocer 0987")
    other = _spend(db, book, date(2026, 9, 9), "Hardware store")
    grocer = save_payee(db, SavePayee("Corner Grocer", ("CORNER GROCER #1234",))).value
    assert grocer is not None and grocer.match_keys == ["corner grocer"]

    proposals = preview_payee_proposals(db).value
    assert [(item.transaction, item.payee_name, item.key) for item in proposals] == [
        (second, "Corner Grocer", "corner grocer"),
        (first, "Corner Grocer", "corner grocer"),
    ]
    assert all(db.get_transaction(handle).payee is None for handle in (first, second))

    applied = apply_payee_proposals(db, (first,)).value
    assert (applied.assigned, applied.unchanged) == (1, 0)
    assert db.get_transaction(first).payee == grocer.handle
    assert db.get_transaction(first).description == "CORNER GROCER #1234"
    assert db.get_transaction(second).payee is None
    assert db.get_transaction(other).payee is None

    assert [item.transaction for item in preview_payee_proposals(db).value] == [second]
    everything = apply_payee_proposals(db).value
    assert (everything.assigned, everything.unchanged) == (1, 0)
    assert preview_payee_proposals(db).value == ()


def test_an_existing_payee_is_never_replaced(db, book):
    handle = _spend(db, book, date(2026, 9, 1), "CORNER GROCER #1234")
    chosen = save_payee(db, SavePayee("Neighborhood Market")).value
    assert assign_payee(db, handle, chosen.handle).ok
    save_payee(db, SavePayee("Corner Grocer", ("corner grocer",)))

    assert preview_payee_proposals(db).value == ()
    stale = apply_payee_proposals(db, (handle,)).value
    assert (stale.assigned, stale.unchanged) == (0, 1)
    assert db.get_transaction(handle).payee == chosen.handle


def test_accepting_is_one_undo_step(db, book):
    handles = [_spend(db, book, date(2026, 9, day), f"Corner Grocer {day}") for day in (1, 2)]
    save_payee(db, SavePayee("Corner Grocer", ("Corner Grocer",)))
    assert apply_payee_proposals(db).value.assigned == 2

    assert db.undo() is True

    assert [db.get_transaction(handle).payee for handle in handles] == [None, None]


def test_rejected_payees_leave_the_book_unchanged(db, book):
    grocer = save_payee(db, SavePayee("Corner Grocer", ("corner grocer",))).value
    before = [(payee.handle, payee.name, payee.match_keys) for payee in db.iter_payees()]

    for request, code in [
        (SavePayee("   "), "payee.name.required"),
        (SavePayee("corner  GROCER"), "payee.name.duplicate"),
        (SavePayee("Other", ("#1234",)), "payee.match.empty"),
        (SavePayee("Other", ("CORNER GROCER 55",)), "payee.match.conflict"),
        (SavePayee("Other", handle="missing"), "payee.not_found"),
    ]:
        result = save_payee(db, request)
        assert [error.code for error in result.errors] == [code]

    assert [(payee.handle, payee.name, payee.match_keys) for payee in db.iter_payees()] == before
    assert db.get_payee(grocer.handle).name == "Corner Grocer"
    assert [e.code for e in apply_payee_proposals(db, ("missing",)).errors] == [
        "payee.transaction.not_found"
    ]
    assert [e.code for e in assign_payee(db, "missing", None).errors] == [
        "payee.transaction.not_found"
    ]


def test_renaming_keeps_assignments_and_delete_clears_them_in_one_step(db, book):
    handle = _spend(db, book, date(2026, 9, 1), "CORNER GROCER #1234")
    grocer = save_payee(db, SavePayee("Corner Grocer", ("corner grocer",))).value
    apply_payee_proposals(db)

    renamed = save_payee(db, SavePayee("The Corner Grocer", ("corner grocer",), grocer.handle))
    assert renamed.ok
    assert db.get_transaction(handle).payee == grocer.handle
    assert db.get_payee(grocer.handle).name == "The Corner Grocer"

    assert delete_payee(db, grocer.handle).value == 1
    assert db.get_payee(grocer.handle) is None
    assert db.get_transaction(handle).payee is None
    assert db.undo() is True
    assert db.get_payee(grocer.handle) is not None
    assert db.get_transaction(handle).payee == grocer.handle


def test_reimport_keeps_the_local_payee():
    existing = Transaction(handle="t", post_date=date(2026, 9, 1), description="GROCER")
    existing.payee = "payee-handle"
    incoming = Transaction(handle="t", post_date=date(2026, 9, 1), description="GROCER")

    merge_local_state(incoming, existing)

    assert incoming.payee == "payee-handle"


def test_verification_reports_a_missing_payee(db, book):
    handle = _spend(db, book, date(2026, 9, 1), "Grocer")
    transaction = db.get_transaction(handle)
    transaction.payee = "gone"
    with db.transaction("Damage") as txn:
        db.commit_transaction(transaction, txn)

    codes = [issue.code for issue in db.verify_book()]

    assert "transaction.missing_payee" in codes


def test_schema_7_book_migrates_to_payees(tmp_path):
    path = tmp_path / "schema-7.breadsched"
    sql = (Path(__file__).parent / "fixtures" / "native" / "schema-7.sql").read_text()
    with sqlite3.connect(path) as raw:
        raw.executescript(sql)

    db = DbSQLite()
    db.load(str(path))
    try:
        assert db.get_metadata("schema_version") == 10
        assert db.get_metadata("fixture_marker") == "schema-7"
        assert db.get_transaction("fixture-txn").payee is None
        assert list(db.iter_payees()) == []
        assert save_payee(db, SavePayee("Corner Grocer", ("corner grocer",))).ok
        assert [item.transaction for item in preview_payee_proposals(db).value] == ["fixture-txn"]
        assert [
            row[0]
            for row in db._require().execute(
                "SELECT version FROM schema_migration ORDER BY version"
            )
        ] == [7, 8, 9, 10]
        assert db.integrity_problems() == []
    finally:
        db.close()
    assert (tmp_path / "schema-7.breadsched.pre-migration-v7.bak").exists()


def test_cli_adds_previews_and_accepts(tmp_path, capsys):
    import json

    from breadsched.cli.main import main
    from breadsched.gen.sample_book import create_sample_book

    path = tmp_path / "cli.breadsched"
    create_sample_book(path, as_of=date(2026, 8, 15))
    db = DbSQLite()
    db.load(str(path))
    try:
        description = next(iter(db.iter_transactions())).description
    finally:
        db.close()

    assert main(["payees", str(path), "--add", "Known", "--match", description, "--json"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["match_keys"] == [match_key(description)]
    assert main(["payees", str(path), "--preview", "--json"]) == 0
    proposals = json.loads(capsys.readouterr().out)
    assert proposals and {item["payee_name"] for item in proposals} == {"Known"}
    assert main(["payees", str(path), "--accept-all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["assigned"] == len(proposals)
    assert main(["payees", str(path), "--json"]) == 0
    [listed] = json.loads(capsys.readouterr().out)
    assert listed["transactions"] == len(proposals)
    assert main(["payees", str(path), "--add", "known"]) == 2
    assert "Another payee already has that name" in capsys.readouterr().err


def _save(db, book, payee=None, set_payee=False, existing=None, description="Grocer"):
    from breadsched.gen.lib import Money
    from breadsched.gen.lib.amount import Amount
    from breadsched.gen.services.transactions import (
        SaveTransaction,
        TransactionInput,
        TransactionSplitInput,
        save_transaction,
        transaction_currency,
    )

    currency = transaction_currency(db)
    return save_transaction(
        db,
        SaveTransaction(
            TransactionInput(
                post_date=date(2026, 9, 1),
                description=description,
                currency=currency,
                splits=(
                    TransactionSplitInput(book.groceries, Amount(Money("12.00"), currency)),
                    TransactionSplitInput(book.checking, Amount(Money("-12.00"), currency)),
                ),
                payee=payee,
                set_payee=set_payee,
            ),
            existing_handle=existing,
        ),
    )


def test_the_editor_sets_keeps_and_clears_a_payee(db, book):
    grocer = save_payee(db, SavePayee("Corner Grocer")).value

    added = _save(db, book, grocer.handle, set_payee=True)
    assert added.ok, added.errors
    handle = added.value.handle
    assert db.get_transaction(handle).payee == grocer.handle

    # An editor that does not show payees keeps the stored one.
    assert _save(db, book, existing=handle, description="Grocer (edited)").ok
    assert db.get_transaction(handle).payee == grocer.handle

    assert _save(db, book, None, set_payee=True, existing=handle).ok
    assert db.get_transaction(handle).payee is None


def test_the_editor_refuses_a_missing_payee(db, book):
    before = len(list(db.iter_transactions()))

    result = _save(db, book, "missing", set_payee=True)

    assert [error.code for error in result.errors] == ["payee.not_found"]
    assert len(list(db.iter_transactions())) == before
