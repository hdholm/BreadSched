"""Write-back round trips on books GnuCash itself created, SQLite and XML (#174).

The fixtures in ``tests/fixtures/gnucash/`` were created by GnuCash 5.5 through
its Python bindings (``make_book.py``). Each test imports one, makes the wider
edits write-back supports (amounts, accounts, added and removed splits, a new
three-split transaction, a deletion, text and reconcile state), writes them, and
proves the result three ways: BreadSched's own read-back, a fresh import that
reproduces the edited book, and, where GnuCash's bindings are installed, GnuCash
itself opening the written book. CI installs them in the GnuCash job.
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
import subprocess
from datetime import date
from functools import cache
from pathlib import Path

import pytest

from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.lib import Money, ReconcileState, Split, Transaction
from breadsched.gen.services.gnucash_writeback import (
    ApplyWriteback,
    apply_writeback,
    preview_writeback,
)
from breadsched.gen.services.imports import ImportBook, import_book

FIXTURES = Path(__file__).parent / "fixtures" / "gnucash"
REPORT = Path(__file__).parents[1] / "scripts" / "gnucash_book_report.py"


@cache
def _gnucash_python() -> str | None:
    """A Python interpreter that can import GnuCash's bindings, if any."""
    candidates = [os.environ.get("BREADSCHED_GNUCASH_PYTHON", "")]
    candidates += ["/usr/bin/python3", "/usr/bin/python3.12", "/usr/bin/python3.13"]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "import gnucash"], capture_output=True, timeout=60
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return candidate
    return None


def gnucash_report(path: Path) -> dict | None:
    python = _gnucash_python()
    if python is None:
        if os.environ.get("BREADSCHED_REQUIRE_GNUCASH"):
            pytest.fail("GnuCash's Python bindings are required but not installed")
        return None
    done = subprocess.run(
        [python, str(REPORT), str(path)], capture_output=True, text=True, timeout=300
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _materialize(kind: str, directory: Path, newline: str = "\n") -> Path:
    if kind == "sqlite":
        path = directory / "household.gnucash"
        conn = sqlite3.connect(path)
        conn.executescript((FIXTURES / "household-gnucash-5.5.sql").read_text())
        conn.close()
        return path
    path = directory / "household-xml.gnucash"
    text = (FIXTURES / "household-gnucash-5.5.xml").read_bytes().replace(b"\r\n", b"\n")
    path.write_bytes(gzip.compress(text.replace(b"\n", newline.encode())))
    return path


@pytest.fixture(params=["sqlite", "xml"])
def linked(request, tmp_path):
    source = _materialize(request.param, tmp_path)
    db = DbSQLite()
    db.load(str(tmp_path / "home.breadsched"))
    imported = import_book(db, ImportBook(source=str(source), notify=False))
    assert imported.value is not None, imported.errors
    yield db, source, request.param
    db.close()


def _by_name(db, name):
    return db.get_account_by_name(name).handle


def _txn(db, description):
    return next(item for item in db.iter_transactions() if item.description == description)


def _save(db, *transactions):
    with db.transaction("Edit") as txn:
        for transaction in transactions:
            db.commit_transaction(transaction, txn)


def _edit_everything(db) -> dict[str, str]:
    """Make every kind of supported change; return the handles involved."""
    checking = _by_name(db, "Assets:Checking")
    savings = _by_name(db, "Assets:Savings")
    groceries = _by_name(db, "Expenses:Groceries")
    rent_account = _by_name(db, "Expenses:Rent")
    utilities = _by_name(db, "Expenses:Utilities")
    card = _by_name(db, "Credit Card")

    rent = _txn(db, "Rent")
    rent.description = "Rent (February)"
    rent.num = "1002"
    expense = next(split for split in rent.splits if split.account == rent_account)
    expense.value = expense.quantity = Money("1750.00")
    expense.memo = "February rent"
    rent.splits.append(Split(utilities, Money("50.00"), memo="Water"))
    for split in rent.splits:
        if split.account == checking:
            split.reconcile = ReconcileState.CLEARED

    supermarket = _txn(db, "Supermarket")
    card_split = next(split for split in supermarket.splits if split.account == card)
    supermarket.splits.remove(card_split)
    grocery = next(split for split in supermarket.splits if split.account == groceries)
    grocery.value = grocery.quantity = Money("50.00")

    electric = _txn(db, "Electric")
    for split in electric.splits:
        if split.account == checking:
            split.account = savings
    electric.post_date = date(2026, 2, 11)
    _save(db, rent, supermarket, electric)

    new = Transaction(
        post_date=date(2026, 2, 20),
        description="Market & hardware <split>",
        num="77",
        splits=[
            Split(groceries, Money("30.25"), memo="Produce"),
            Split(utilities, Money("19.75"), action="Buy"),
            Split(checking, Money("-50.00")),
        ],
    )
    with db.transaction("New") as txn:
        db.add_transaction(new, txn)
    deleted = _txn(db, "To savings")
    with db.transaction("Delete") as txn:
        db.remove_transaction(deleted.handle, txn)
    return {"rent": rent.handle, "new": new.handle, "deleted": deleted.handle}


def _facts(db):
    """What the ledger says, by account name, for comparing two books."""
    rows = []
    for transaction in db.iter_transactions():
        rows.append(
            (
                transaction.handle,
                transaction.post_date,
                transaction.description,
                transaction.num,
                tuple(
                    sorted(
                        (
                            split.handle,
                            db.full_name(db.get_account(split.account)),
                            split.value,
                            split.memo,
                            split.action,
                            split.reconcile.value,
                        )
                        for split in transaction.splits
                    )
                ),
            )
        )
    return sorted(rows)


def test_wider_edits_round_trip_through_the_written_book(linked, tmp_path):
    db, source, kind = linked
    handles = _edit_everything(db)
    plan = preview_writeback(db).value
    assert plan is not None and plan.unsupported == ()
    assert plan.format == kind
    kinds = {change.transaction: change.kinds for change in plan.changes}
    assert kinds[handles["new"]] == ("new",)
    assert kinds[handles["deleted"]] == ("delete",)
    assert kinds[handles["rent"]] == ("edit", "reconcile")
    assert len(plan.changes) == 5

    applied = apply_writeback(db, ApplyWriteback(tuple(kinds)))
    assert applied.value is not None, applied.errors
    assert preview_writeback(db).value.changes == ()

    # A fresh import of the written book reproduces the edited BreadSched book.
    fresh = DbSQLite()
    fresh.load(str(tmp_path / "fresh.breadsched"))
    try:
        assert import_book(fresh, ImportBook(source=str(source), notify=False)).value
        assert _facts(fresh) == _facts(db)
    finally:
        fresh.close()

    report = gnucash_report(source)
    if report is None:
        pytest.skip("GnuCash's Python bindings are not installed")
    assert report["imbalanced"] == []
    assert set(report["transactions"]) == {t.handle for t in db.iter_transactions()}
    new = report["transactions"][handles["new"]]
    assert (new["date"], new["description"], new["num"]) == (
        "2026-02-20",
        "Market & hardware <split>",
        "77",
    )
    assert sorted((s["account"], s["value"], s["memo"], s["action"]) for s in new["splits"]) == [
        ("Assets.Checking", "-5000/100", "", ""),
        ("Expenses.Groceries", "3025/100", "Produce", ""),
        ("Expenses.Utilities", "1975/100", "", "Buy"),
    ]
    rent = report["transactions"][handles["rent"]]
    assert rent["description"] == "Rent (February)" and rent["num"] == "1002"
    assert {s["account"]: s["reconcile"] for s in rent["splits"]}["Assets.Checking"] == "c"
    for account in db.iter_accounts():
        if account.is_root:
            continue
        name = db.full_name(account).replace(":", ".")
        if name in report["balances"]:
            number, denominator = report["balances"][name].split("/")
            expected = sum(
                (
                    split.quantity if split.quantity is not None else split.value
                    for transaction in db.iter_transactions()
                    for split in transaction.splits
                    if split.account == account.handle
                ),
                Money(0),
            )
            assert Money(int(number), int(denominator)) == expected, name


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_xml_write_back_leaves_untouched_bytes_alone(tmp_path, newline):
    source = _materialize("xml", tmp_path, newline)
    before = gzip.decompress(source.read_bytes()).decode()
    db = DbSQLite()
    db.load(str(tmp_path / "home.breadsched"))
    try:
        assert import_book(db, ImportBook(source=str(source), notify=False)).value
        rent = _txn(db, "Rent")
        rent.description = "Rent edited"
        _save(db, rent)
        assert apply_writeback(db, ApplyWriteback((rent.handle,))).value
    finally:
        db.close()
    after = gzip.decompress(source.read_bytes()).decode()
    assert after.count("\r\n") == (after.count("\n") if newline == "\r\n" else 0)
    start = before.index(f'<trn:id type="guid">{rent.handle}</trn:id>')
    block_start = before.rindex("<gnc:transaction", 0, start)
    block_end = before.index("</gnc:transaction>", start) + len(f"</gnc:transaction>{newline}")
    assert after[:block_start] == before[:block_start]
    assert after[block_start:].endswith(before[block_end:])
    edited = after[block_start : len(after) - len(before[block_end:])]
    assert edited == before[block_start:block_end].replace(
        "<trn:description>Rent</trn:description>",
        "<trn:description>Rent edited</trn:description>",
    )


def test_an_xml_book_open_in_gnucash_is_refused(tmp_path):
    source = _materialize("xml", tmp_path)
    db = DbSQLite()
    db.load(str(tmp_path / "home.breadsched"))
    try:
        assert import_book(db, ImportBook(source=str(source), notify=False)).value
        Path(f"{source}.LCK").write_text("locked")
        assert [error.code for error in preview_writeback(db).errors] == ["writeback.source.locked"]
    finally:
        db.close()


def test_the_fixture_books_are_what_gnucash_wrote(tmp_path):
    """The committed fixtures open in real GnuCash unchanged (when it is installed)."""
    for kind in ("sqlite", "xml"):
        directory = tmp_path / kind
        directory.mkdir()
        report = gnucash_report(_materialize(kind, directory))
        if report is None:
            pytest.skip("GnuCash's Python bindings are not installed")
        assert len(report["transactions"]) == 5 and report["imbalanced"] == []
