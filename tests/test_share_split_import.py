"""GnuCash share splits import with a balancing leg that write-back never sends."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from gnucash_fixtures import new_guid, write_account, write_commodity, write_transaction

from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine.cost_basis import cost_basis
from breadsched.gen.lib import Money
from breadsched.gen.services.gnucash_writeback import (
    ApplyWriteback,
    apply_writeback,
    preview_writeback,
)
from breadsched.gen.services.imports import ImportBook, import_book


def _add_holding_and_split(gnc) -> tuple[str, str]:
    """A 10-share purchase for $1,000, then a 2-for-1 split: one zero-value split."""
    conn = sqlite3.connect(gnc.path)
    with conn:
        security = write_commodity(conn, new_guid(), "NASDAQ", "IDX", "Index fund", 1000)
        holding = write_account(
            conn, new_guid(), "Index holding", "STOCK", gnc.ids.assets, security
        )
        buy = write_transaction(
            conn,
            new_guid(),
            gnc.ids.currency,
            date(2026, 1, 2),
            "Buy index",
            [(holding, 100000, 100, ""), (gnc.ids.checking, -100000, 100, "")],
        )
        conn.execute(
            "UPDATE splits SET quantity_num=10, quantity_denom=1 WHERE tx_guid=? "
            "AND account_guid=?",
            (buy, holding),
        )
        split = write_transaction(
            conn, new_guid(), gnc.ids.currency, date(2026, 3, 2), "2-for-1 split",
            [(holding, 0, 100, "")],
        )  # fmt: skip
        conn.execute(
            "UPDATE splits SET quantity_num=10, quantity_denom=1, action='Split' WHERE tx_guid=?",
            (split,),
        )
    conn.close()
    return holding, split


@pytest.fixture
def imported(tmp_path, gnucash_sqlite_path):
    holding, split_guid = _add_holding_and_split(gnucash_sqlite_path)
    db = DbSQLite()
    db.load(str(tmp_path / "home.breadsched"))
    result = import_book(db, ImportBook(source=gnucash_sqlite_path.path, notify=False))
    assert result.value is not None
    holding_handle = next(a.handle for a in db.iter_accounts() if a.source_guid == holding)
    yield db, gnucash_sqlite_path, result.value.result, holding_handle, split_guid
    db.close()


def test_a_share_split_imports_with_a_zero_value_balancing_leg(imported):
    db, _gnc, result, holding, split_guid = imported
    assert result.share_splits == 1
    assert "only one split, with no value" not in result.reasons()
    transaction = db.get_transaction(split_guid)
    assert transaction is not None
    shares, balancing = sorted(transaction.splits, key=lambda s: s.importer_added)
    assert (shares.account, shares.quantity, shares.value) == (holding, Money(10), Money(0))
    assert balancing.importer_added is True
    assert (balancing.value, balancing.quantity) == (Money(0), Money(0))
    assert db.full_name(balancing.account) == "Equity:Share splits"
    assert db.get_account_by_name("Equity").placeholder is True

    # Cost basis already reads a value-less change of shares as a split.
    basis = cost_basis(db, holding)
    assert basis is not None
    assert basis.quantity == Money(20)
    assert sum((lot.cost for lot in basis.lots), Money(0)) == Money(1000)
    assert [move.kind for move in basis.moves if move.kind == "split"] == ["split"]


def test_the_marker_survives_storage_and_other_splits_are_stored_as_before(imported):
    db, *_rest, split_guid = imported
    stored = db.get_transaction(split_guid)
    marked = [split.serialize() for split in stored.splits if split.importer_added]
    assert [item["importer_added"] for item in marked] == [True]
    plain = [split.serialize() for split in stored.splits if not split.importer_added]
    assert all("importer_added" not in item for item in plain)
    assert db.integrity_problems() == []


def test_reimporting_keeps_the_leg_and_changes_nothing(imported):
    db, gnc, _first, _holding, split_guid = imported
    before = [split.serialize() for split in db.get_transaction(split_guid).splits]
    accounts = len(list(db.iter_accounts()))
    again = import_book(db, ImportBook(source=gnc.path, notify=False)).value.result
    assert again.transactions_refreshed == 0
    assert [split.serialize() for split in db.get_transaction(split_guid).splits] == before
    assert len(list(db.iter_accounts())) == accounts


def test_write_back_never_sends_the_leg(imported):
    db, gnc, *_rest, split_guid = imported
    plan = preview_writeback(db).value
    assert plan is not None
    assert plan.changes == () and plan.unsupported == ()

    transaction = db.get_transaction(split_guid)
    transaction.description = "2-for-1 stock split"
    with db.transaction("Rename") as txn:
        db.commit_transaction(transaction, txn)
    [change] = preview_writeback(db).value.changes
    assert change.transaction == split_guid
    assert not any("Share splits" in line for line in change.details)
    assert apply_writeback(db, ApplyWriteback((split_guid,))).value is not None

    conn = sqlite3.connect(gnc.path)
    try:
        rows = conn.execute(
            "SELECT value_num, quantity_num * 1.0 / quantity_denom FROM splits WHERE tx_guid=?",
            (split_guid,),
        ).fetchall()
        [(description,)] = conn.execute(
            "SELECT description FROM transactions WHERE guid=?", (split_guid,)
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(0, 10.0)]
    assert description == "2-for-1 stock split"
    assert preview_writeback(db).value.changes == ()
