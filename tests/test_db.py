"""The database contract: atomic batches, working undo, honest signals."""

import json
import socket
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from breadsched.gen.db.base import DbError
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.lib import (
    Account,
    AccountType,
    Commodity,
    CommodityPrice,
    Money,
    Scenario,
    ScheduledTransaction,
    Split,
    Transaction,
    UnbalancedError,
)


class TestWriterLock:
    def test_second_writer_is_rejected_but_read_only_open_is_allowed(self, tmp_path):
        path = str(tmp_path / "locked.breadsched")
        first = DbSQLite()
        first.load(path)

        second = DbSQLite()
        with pytest.raises(DbError, match="already open for writing"):
            second.load(path)

        reader = DbSQLite()
        reader.load(path, mode="r")
        reader.close()

        first.close()
        second.load(path)
        second.close()

    def test_symlink_alias_cannot_open_second_writer(self, tmp_path):
        path = tmp_path / "real.breadsched"
        alias = tmp_path / "alias.breadsched"
        first = DbSQLite()
        first.load(str(path))
        try:
            try:
                alias.symlink_to(path)
            except (OSError, NotImplementedError):
                pytest.skip("symlinks are unavailable on this platform")
            second = DbSQLite()
            with pytest.raises(DbError, match="already open for writing"):
                second.load(str(alias))
        finally:
            first.close()

    def test_stale_same_host_lock_is_reclaimed(self, tmp_path):
        path = str(tmp_path / "stale.breadsched")
        seed = DbSQLite()
        seed.load(path)
        seed.close()

        lock_path = Path(f"{path}.lock")
        lock_path.write_text(
            json.dumps(
                {
                    "pid": 999_999_999,
                    "host": socket.gethostname(),
                    "token": "stale",
                    "book": str(Path(path).resolve()),
                }
            ),
            encoding="utf-8",
        )

        reopened = DbSQLite()
        reopened.load(path)
        assert lock_path.exists()
        reopened.close()
        assert not lock_path.exists()


class TestSyncedPathWarning:
    def test_opening_book_under_known_sync_root_warns(self, tmp_path, monkeypatch, breadsched_logs):
        synced = tmp_path / "OneDrive"
        synced.mkdir()
        monkeypatch.setenv("OneDrive", str(synced))
        path = synced / "book.breadsched"

        db = DbSQLite()
        db.load(str(path))
        db.close()

        assert breadsched_logs.containing("inside OneDrive")


class TestRootIntegrity:
    def test_verify_flags_parentless_non_root_when_chart_has_root(self, tmp_path):
        path = str(tmp_path / "orphaned.breadsched")
        db = DbSQLite()
        db.load(path)
        with db.transaction("chart") as txn:
            root = Account(name="Root", atype=AccountType.ROOT)
            orphan = Account(name="Detached checking", atype=AccountType.BANK)
            db.add_account(root, txn)
            db.add_account(orphan, txn)

        issues = db.verify_book()
        assert any(
            issue.code == "account.orphaned_top_level" and issue.handle == orphan.handle
            for issue in issues
        )
        db.close()


class TestPersistence:
    def test_account_survives_a_round_trip(self, tmp_path):
        path = str(tmp_path / "book.breadsched")
        db = DbSQLite()
        db.load(path)
        with db.transaction("Add account") as txn:
            account = Account(name="Checking", atype=AccountType.BANK)
            db.add_account(account, txn)
        handle = account.handle
        db.close()

        reopened = DbSQLite()
        reopened.load(path)
        assert reopened.get_account(handle).name == "Checking"
        reopened.close()

    def test_transaction_splits_survive_a_round_trip(self, db, book):
        with db.transaction("Post") as txn:
            original = Transaction.simple(
                date(2026, 1, 5), "Rent", book.rent, book.checking, "1800.00"
            )
            db.add_transaction(original, txn)
        loaded = db.get_transaction(original.handle)
        assert loaded.description == "Rent"
        assert len(loaded.splits) == 2
        assert loaded.value_for(book.rent) == Money("1800.00")

    def test_metadata_round_trip(self, db):
        db.set_metadata("book_name", "Household")
        assert db.get_metadata("book_name") == "Household"
        assert db.get_metadata("missing", "fallback") == "fallback"

    def test_metadata_cannot_escape_an_active_transaction(self, db, book):
        with pytest.raises(DbError, match="must use that DbTxn"):
            with db.transaction("Atomic edit") as txn:
                posted = Transaction.simple(
                    date(2026, 1, 5), "Example", book.rent, book.checking, "10"
                )
                db.add_transaction(posted, txn)
                db.set_metadata("example", "committed too early")
        assert db.get_transaction(posted.handle) is None
        assert db.get_metadata("example") is None

    def test_transactional_metadata_is_undoable(self, db):
        with db.transaction("Set metadata") as txn:
            db.set_metadata("example", {"value": 1}, txn)
        assert db.get_metadata("example") == {"value": 1}

        assert db.undo() is True
        assert db.get_metadata("example") is None
        assert db.redo() is True
        assert db.get_metadata("example") == {"value": 1}


class TestIntegrity:
    def test_unbalanced_transactions_are_refused(self, db, book):
        bad = Transaction(post_date=date(2026, 1, 1), description="Wrong")
        bad.add_split(Split(book.checking, Money("100")))
        bad.add_split(Split(book.rent, Money("-90")))
        with pytest.raises(UnbalancedError):
            with db.transaction("Bad") as txn:
                db.add_transaction(bad, txn)
        assert db.get_transaction(bad.handle) is None

    def test_single_split_transactions_are_refused(self, db, book):
        lonely = Transaction(post_date=date(2026, 1, 1))
        lonely.add_split(Split(book.checking, Money("0")))
        with pytest.raises(UnbalancedError):
            lonely.validate()

    def test_accounts_with_history_cannot_be_deleted(self, db, funded_book):
        with pytest.raises(DbError):
            with db.transaction("Delete") as txn:
                db.remove_account(funded_book.checking, txn)

    def test_accounts_with_children_cannot_be_deleted(self, db, book):
        with pytest.raises(DbError):
            with db.transaction("Delete") as txn:
                db.remove_account(book.assets, txn)

    def test_a_failed_batch_leaves_nothing_behind(self, db, book):
        good = Transaction.simple(date(2026, 1, 1), "Fine", book.rent, book.checking, "10")
        bad = Transaction(post_date=date(2026, 1, 2), description="Broken")
        bad.add_split(Split(book.checking, Money("1")))
        bad.add_split(Split(book.rent, Money("-2")))

        with pytest.raises(UnbalancedError):
            with db.transaction("Two writes, one bad") as txn:
                db.add_transaction(good, txn)
                db.add_transaction(bad, txn)

        assert db.get_transaction(good.handle) is None


class TestUndo:
    def test_undo_restores_the_previous_state(self, db, book):
        with db.transaction("Post rent") as txn:
            posted = Transaction.simple(
                date(2026, 1, 5), "Rent", book.rent, book.checking, "1800.00"
            )
            db.add_transaction(posted, txn)
        assert db.get_transaction(posted.handle) is not None

        assert db.undo() is True
        assert db.get_transaction(posted.handle) is None

        assert db.redo() is True
        assert db.get_transaction(posted.handle) is not None

    def test_undo_of_an_edit_restores_the_old_value(self, db, book):
        account = db.get_account(book.checking)
        with db.transaction("Rename") as txn:
            account.name = "Everyday"
            db.commit_account(account, txn)
        assert db.get_account(book.checking).name == "Everyday"
        db.undo()
        assert db.get_account(book.checking).name == "Checking"

    def test_undo_carries_its_description(self, db, book):
        with db.transaction("Post rent") as txn:
            db.add_transaction(
                Transaction.simple(date(2026, 1, 5), "R", book.rent, book.checking, "1"), txn
            )
        assert db.undo_message() == "Post rent"

    def test_undo_on_an_untouched_book_is_a_no_op(self, db):
        assert db.undo() is False
        assert db.redo() is False

    def test_a_new_edit_clears_the_redo_stack(self, db, book):
        with db.transaction("First") as txn:
            db.add_transaction(
                Transaction.simple(date(2026, 1, 5), "A", book.rent, book.checking, "1"), txn
            )
        db.undo()
        with db.transaction("Second") as txn:
            db.add_transaction(
                Transaction.simple(date(2026, 1, 6), "B", book.rent, book.checking, "2"), txn
            )
        assert db.redo() is False


class TestSignals:
    def test_add_emits_once_with_the_handle(self, db, book):
        seen = []
        db.connect("transaction-add", lambda handles: seen.append(handles))
        with db.transaction("Post") as txn:
            posted = Transaction.simple(date(2026, 1, 5), "R", book.rent, book.checking, "1")
            db.add_transaction(posted, txn)
        assert seen == [[posted.handle]]

    def test_signals_fire_after_commit_not_during(self, db, book):
        observed = []

        def on_add(handles):
            observed.append(db.get_transaction(handles[0]) is not None)

        db.connect("transaction-add", on_add)
        with db.transaction("Post") as txn:
            db.add_transaction(
                Transaction.simple(date(2026, 1, 5), "R", book.rent, book.checking, "1"), txn
            )
        assert observed == [True]

    def test_batch_mode_suppresses_per_object_signals(self, db, book):
        seen = []
        db.connect("transaction-add", lambda handles: seen.append(handles))
        with db.transaction("Import", batch=True) as txn:
            for day in range(1, 6):
                db.add_transaction(
                    Transaction.simple(date(2026, 1, day), "R", book.rent, book.checking, "1"),
                    txn,
                )
        assert seen == []

    def test_worker_transaction_can_defer_all_notifications(self, db, book):
        seen = []
        db.connect("database-changed", lambda *_: seen.append("changed"))
        db.connect("undo-available", lambda *_: seen.append("undo"))
        with db.transaction("Background import", batch=True, notify=False) as txn:
            db.add_transaction(
                Transaction.simple(date(2026, 1, 5), "R", book.rent, book.checking, "1"),
                txn,
            )

        assert seen == []
        assert db.undo_stack

    def test_a_failing_listener_does_not_break_the_write(self, db, book):
        db.connect("transaction-add", lambda handles: 1 / 0)
        with db.transaction("Post") as txn:
            posted = Transaction.simple(date(2026, 1, 5), "R", book.rent, book.checking, "1")
            db.add_transaction(posted, txn)
        assert db.get_transaction(posted.handle) is not None

    def test_unknown_signals_are_rejected_at_connect_time(self, db):
        with pytest.raises(KeyError):
            db.connect("no-such-signal", lambda: None)


class TestQueries:
    def test_full_name_builds_the_colon_path(self, db, book):
        assert db.full_name(book.rent) == "Expenses:Rent"

    def test_lookup_by_full_path(self, db, book):
        assert db.get_account_by_name("Expenses:Rent").handle == book.rent

    def test_lookup_by_bare_name_when_unambiguous(self, db, book):
        assert db.get_account_by_name("Groceries").handle == book.groceries

    def test_descendants_are_depth_first(self, db, book):
        names = [a.name for a in db.descendants(book.root)]
        assert "Checking" in names and "Rent" in names
        assert names.index("Assets") < names.index("Checking")

    def test_transactions_filter_by_account(self, db, funded_book):
        found = list(db.iter_transactions(account=funded_book.card))
        assert len(found) == 1
        assert found[0].description == "Dinner out"

    def test_transactions_filter_by_date_window(self, db, funded_book):
        found = list(db.iter_transactions(start=date(2026, 2, 1), end=date(2026, 2, 28)))
        assert {t.description for t in found} == {"February rent", "Dinner out"}

    def test_split_index_tracks_edits(self, db, book):
        with db.transaction("Post") as txn:
            posted = Transaction.simple(date(2026, 1, 5), "R", book.rent, book.checking, "100")
            db.add_transaction(posted, txn)
        assert len(db.split_rows(book.rent)) == 1

        with db.transaction("Move to groceries") as txn:
            posted.splits[0].account = book.groceries
            db.commit_transaction(posted, txn)
        assert db.split_rows(book.rent) == []
        assert len(db.split_rows(book.groceries)) == 1

    def test_deleting_a_transaction_clears_its_index_rows(self, db, book):
        with db.transaction("Post") as txn:
            posted = Transaction.simple(date(2026, 1, 5), "R", book.rent, book.checking, "100")
            db.add_transaction(posted, txn)
        with db.transaction("Delete") as txn:
            db.remove_transaction(posted.handle, txn)
        assert db.split_rows(book.rent) == []


class TestSchemaMigration:
    @staticmethod
    def _make_schema_3_book(path):
        db = DbSQLite()
        db.load(str(path))
        with db.transaction("Supported baseline objects") as txn:
            scheduled = ScheduledTransaction(name="Estimate")
            scenario = Scenario(name="Plan")
            card = Account(name="Card", atype=AccountType.CREDIT)
            benefit = Account(name="Benefit", atype=AccountType.FSA)
            db.add_scheduled(scheduled, txn)
            db.add_scenario(scenario, txn)
            db.add_account(card, txn)
            db.add_account(benefit, txn)
        db.close()

        conn = sqlite3.connect(path)
        for account, legacy_type, legacy_kind in (
            (card, "CREDIT", "debt"),
            (benefit, "ASSET", "fsa"),
        ):
            raw = json.loads(
                conn.execute(
                    "SELECT blob FROM account WHERE handle=?", (account.handle,)
                ).fetchone()[0]
            )
            raw["atype"] = legacy_type
            raw["kind"] = legacy_kind
            conn.execute(
                "UPDATE account SET atype=?, blob=? WHERE handle=?",
                (legacy_type, json.dumps(raw, separators=(",", ":")), account.handle),
            )
        for table, handle, fields in (
            ("scheduled", scheduled.handle, {"budgets": ["old"], "budgets_decided": True}),
            (
                "scenario",
                scenario.handle,
                {"basis": "combined", "budget": "old", "extend_budget": False},
            ),
        ):
            raw = json.loads(
                conn.execute(f"SELECT blob FROM {table} WHERE handle=?", (handle,)).fetchone()[0]
            )
            raw.update(fields)
            conn.execute(
                f"UPDATE {table} SET blob=? WHERE handle=?",
                (json.dumps(raw, separators=(",", ":")), handle),
            )
        conn.execute(
            "CREATE TABLE budget(handle TEXT PRIMARY KEY, name TEXT NOT NULL, blob TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO budget VALUES ('old', 'Old', '{}')")
        conn.execute("INSERT OR REPLACE INTO metadata VALUES ('current_budget', 'old')")
        conn.execute("INSERT OR REPLACE INTO metadata VALUES ('schema_version', '3')")
        conn.execute("DELETE FROM schema_migration")
        conn.execute("INSERT INTO schema_migration(version) VALUES (3)")
        conn.commit()
        conn.close()
        return scheduled.handle, scenario.handle, card.handle, benefit.handle

    def test_supported_baseline_is_backed_up_and_cleaned(self, tmp_path):
        path = tmp_path / "a3.breadsched"
        scheduled, scenario, card, benefit = self._make_schema_3_book(path)

        db = DbSQLite()
        db.load(str(path))

        assert db.get_metadata("schema_version") == 6
        assert db.get_metadata("current_budget") is None
        assert "budgets" not in db.get_scheduled(scheduled).serialize()
        assert "basis" not in db.get_scenario(scenario).serialize()
        assert db.get_account(card).atype is AccountType.CREDIT
        assert db.get_account(benefit).atype is AccountType.FSA
        for handle, expected in ((card, "CREDIT CARD"), (benefit, "FSA")):
            stored_type, raw_blob = (
                db._require()
                .execute("SELECT atype, blob FROM account WHERE handle=?", (handle,))
                .fetchone()
            )
            assert stored_type == expected
            assert json.loads(raw_blob)["atype"] == expected
            assert "kind" not in json.loads(raw_blob)
        assert (
            db._require()
            .execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='budget'")
            .fetchone()
            is None
        )
        assert [
            row[0]
            for row in db._require().execute(
                "SELECT version FROM schema_migration ORDER BY version"
            )
        ] == [3, 4, 5, 6]
        db.close()

        backup = tmp_path / "a3.breadsched.pre-migration-v3.bak"
        assert backup.exists()
        old = sqlite3.connect(backup)
        try:
            assert (
                old.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
                == "3"
            )
            assert (
                old.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='budget'"
                ).fetchone()
                is not None
            )
        finally:
            old.close()

    def test_schema_4_book_stranded_by_the_old_decoder_is_repaired(self, tmp_path):
        from breadsched.gen.db.migrations import v3_to_v4

        path = tmp_path / "stranded-schema-4.breadsched"
        _scheduled, _scenario, card, benefit = self._make_schema_3_book(path)
        conn = sqlite3.connect(path)
        try:
            v3_to_v4(conn)
            conn.execute("UPDATE metadata SET value='4' WHERE key='schema_version'")
            conn.execute("INSERT INTO schema_migration(version) VALUES (4)")
            conn.commit()
        finally:
            conn.close()

        db = DbSQLite()
        db.load(str(path))
        try:
            assert db.get_metadata("schema_version") == 6
            assert db.get_account(card).atype is AccountType.CREDIT
            assert db.get_account(benefit).atype is AccountType.FSA
        finally:
            db.close()

        assert (tmp_path / "stranded-schema-4.breadsched.pre-migration-v4.bak").exists()

    def test_formats_before_a3_are_rejected(self, tmp_path):
        path = tmp_path / "unsupported.breadsched"
        db = DbSQLite()
        db.load(str(path))
        db.set_metadata("schema_version", 2)
        db.close()

        unsupported = DbSQLite()
        with pytest.raises(DbError, match="predates the supported BreadSched 0.2.0a3 baseline"):
            unsupported.load(str(path))

    def test_failed_cleanup_rolls_back_the_book(self, tmp_path, monkeypatch):
        from breadsched.gen.db import sqlite as sqlite_backend

        path = tmp_path / "failure.breadsched"
        self._make_schema_3_book(path)

        def fail_halfway(conn):
            conn.execute("DROP TABLE budget")
            raise RuntimeError("simulated migration failure")

        monkeypatch.setitem(sqlite_backend.MIGRATIONS, 3, fail_halfway)
        db = DbSQLite()
        with pytest.raises(RuntimeError, match="simulated migration failure"):
            db.load(str(path))
        assert not db.is_open

        raw = sqlite3.connect(path)
        try:
            assert (
                raw.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
                == "3"
            )
            assert (
                raw.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='budget'"
                ).fetchone()
                is not None
            )
        finally:
            raw.close()
        assert (tmp_path / "failure.breadsched.pre-migration-v3.bak").exists()

    def test_read_only_baseline_requires_one_writable_open(self, tmp_path):
        path = tmp_path / "a3-readonly.breadsched"
        self._make_schema_3_book(path)

        db = DbSQLite()
        with pytest.raises(DbError, match="open it writable once to migrate"):
            db.load(str(path), mode="r")
        assert not (tmp_path / "a3-readonly.breadsched.pre-migration-v3.bak").exists()

    def test_new_books_report_clean_integrity(self, db):
        assert db.get_metadata("schema_version") == 6
        assert db.integrity_problems() == []

    def test_schema_5_backfills_indexed_security_quantities(self, tmp_path):
        path = tmp_path / "a5.breadsched"
        db = DbSQLite()
        db.load(str(path))
        with db.transaction("Security units") as txn:
            root = Account(name="Root", atype=AccountType.ROOT)
            holding = Account(name="Holding", atype=AccountType.INVESTMENT, parent=root.handle)
            equity = Account(name="Opening", atype=AccountType.EQUITY, parent=root.handle)
            db.add_account(root, txn)
            db.add_account(holding, txn)
            db.add_account(equity, txn)
            purchase = Transaction(post_date=date(2026, 1, 1), description="Holding")
            purchase.splits = [
                Split(holding.handle, Money("100"), quantity=Money("7.5")),
                Split(equity.handle, Money("-100")),
            ]
            db.add_transaction(purchase, txn)
        split_handle = purchase.splits[0].handle
        db.close()

        conn = sqlite3.connect(path)
        conn.executescript(
            """
            ALTER TABLE split_index RENAME TO split_index_v6;
            CREATE TABLE split_index (
                handle TEXT PRIMARY KEY, txn TEXT NOT NULL, account TEXT NOT NULL,
                post_date TEXT NOT NULL, value_num INTEGER NOT NULL, value_den INTEGER NOT NULL
            );
            INSERT INTO split_index(handle,txn,account,post_date,value_num,value_den)
                SELECT handle,txn,account,post_date,value_num,value_den FROM split_index_v6;
            DROP TABLE split_index_v6;
            CREATE INDEX idx_split_account ON split_index(account, post_date);
            CREATE INDEX idx_split_txn ON split_index(txn);
            DROP TABLE price;
            UPDATE metadata SET value='5' WHERE key='schema_version';
            DELETE FROM schema_migration WHERE version=6;
            """
        )
        conn.commit()
        conn.close()

        migrated = DbSQLite()
        migrated.load(str(path))
        try:
            row = (
                migrated._require()
                .execute(
                    "SELECT quantity_num, quantity_den FROM split_index WHERE handle=?",
                    (split_handle,),
                )
                .fetchone()
            )
            assert Money(row[0], row[1]) == Money("7.5")
            assert migrated.get_metadata("schema_version") == 6
        finally:
            migrated.close()


class TestBookVerification:
    def test_clean_book_has_no_logical_issues(self, db, funded_book):
        assert db.verify_book() == []

    def test_missing_split_account_is_reported_without_modifying_book(self, db, funded_book):
        import json

        txn = next(db.iter_transactions())
        raw = db._read("txn", txn.handle)
        raw["splits"][0]["account"] = "missing-account"
        db._require().execute(
            "UPDATE txn SET blob=? WHERE handle=?",
            (json.dumps(raw, separators=(",", ":")), txn.handle),
        )
        db._require().commit()

        issues = db.verify_book()
        assert any(issue.code == "transaction.missing_account" for issue in issues)
        assert any(issue.code == "split_index.mismatch" for issue in issues)

    def test_orphaned_parent_and_parent_cycle_are_reported(self, db, book):
        import json

        raw = db._read("account", book.checking)
        raw["parent"] = "does-not-exist"
        db._require().execute(
            "UPDATE account SET parent=?, blob=? WHERE handle=?",
            ("does-not-exist", json.dumps(raw, separators=(",", ":")), book.checking),
        )
        db._require().commit()
        db._accounts[book.checking] = Account.from_dict(raw)
        assert any(issue.code == "account.missing_parent" for issue in db.verify_book())

        raw["parent"] = book.checking
        db._require().execute(
            "UPDATE account SET parent=?, blob=? WHERE handle=?",
            (book.checking, json.dumps(raw, separators=(",", ":")), book.checking),
        )
        db._require().commit()
        db._accounts[book.checking] = Account.from_dict(raw)
        assert any(issue.code == "account.parent_cycle" for issue in db.verify_book())

    def test_orphan_split_index_row_is_reported(self, db):
        db._require().execute(
            "INSERT INTO split_index(handle,txn,account,post_date,value_num,value_den) "
            "VALUES ('ghost','ghost-txn','ghost-account','2026-01-01',1,1)"
        )
        db._require().commit()
        assert any(issue.code == "split_index.orphan" for issue in db.verify_book())


class TestWriteTimeInvariants:
    def test_missing_split_account_rolls_back_the_whole_transaction(self, db, book):
        posted = Transaction.simple(
            date(2026, 2, 1), "Invalid target", "missing-account", book.checking, "10"
        )
        with pytest.raises(DbError, match="transaction.missing_account"):
            with db.transaction("Reject broken posting") as txn:
                db.add_transaction(posted, txn)
        assert db.get_transaction(posted.handle) is None

    def test_parent_cycle_is_rejected_and_cache_is_restored(self, db, book):
        account = db.get_account(book.checking)
        original_parent = account.parent
        account.parent = account.handle
        with pytest.raises(DbError, match="account.parent_cycle"):
            with db.transaction("Reject cycle") as txn:
                db.commit_account(account, txn)
        assert db.get_account(book.checking).parent == original_parent


class TestMalformedObjectDiagnostics:
    def test_verification_open_reports_a_bad_transaction_blob(self, tmp_path):
        import sqlite3

        path = tmp_path / "damaged.breadsched"
        db = DbSQLite()
        db.load(str(path))
        with db.transaction("Setup") as txn:
            root = Account(name="Root", atype=AccountType.ROOT)
            checking = Account(name="Checking", atype=AccountType.BANK, parent=root.handle)
            expense = Account(name="Expense", atype=AccountType.EXPENSE, parent=root.handle)
            db.add_account(root, txn)
            db.add_account(checking, txn)
            db.add_account(expense, txn)
            posted = Transaction.simple(
                date(2026, 1, 1), "One", expense.handle, checking.handle, "1"
            )
            db.add_transaction(posted, txn)
        db.close()

        conn = sqlite3.connect(path)
        conn.execute("UPDATE txn SET blob='{not-json' WHERE handle=?", (posted.handle,))
        conn.commit()
        conn.close()

        verifier = DbSQLite()
        verifier.load_for_verification(str(path))
        try:
            issues = verifier.verify_book()
            assert any(issue.code == "transaction.malformed" for issue in issues)
            assert any(issue.code == "split_index.orphan" for issue in issues)
        finally:
            verifier.close()


class TestTransactionConcurrency:
    def test_writes_require_entered_transaction(self, db):
        account = Account(name="Outside", atype=AccountType.BANK)
        txn = db.transaction("not entered")
        with pytest.raises(DbError, match="active database transaction"):
            db.add_account(account, txn)

    def test_write_transactions_are_serialized_between_threads(self, db):
        import threading
        import time

        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        errors = []

        def first():
            try:
                with db.transaction("first"):
                    first_entered.set()
                    release_first.wait(timeout=2)
            except Exception as exc:  # pragma: no cover - surfaced by assertion
                errors.append(exc)

        def second():
            try:
                first_entered.wait(timeout=2)
                with db.transaction("second"):
                    second_entered.set()
            except Exception as exc:  # pragma: no cover - surfaced by assertion
                errors.append(exc)

        t1 = threading.Thread(target=first)
        t2 = threading.Thread(target=second)
        t1.start()
        t2.start()
        assert first_entered.wait(timeout=2)
        time.sleep(0.05)
        assert not second_entered.is_set()
        release_first.set()
        t1.join(timeout=2)
        t2.join(timeout=2)
        assert second_entered.is_set()
        assert errors == []


class TestRollbackJournalRecovery:
    def test_books_do_not_leave_persistent_wal_or_shm_files(self, tmp_path):
        path = tmp_path / "book.breadsched"
        db = DbSQLite()
        db.load(str(path))
        assert db._require().execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        with db.transaction("account") as txn:
            db.add_account(Account(name="Checking", atype=AccountType.BANK), txn)
        db.close()

        assert path.exists()
        assert not Path(str(path) + "-wal").exists()
        assert not Path(str(path) + "-shm").exists()
        assert not Path(str(path) + "-journal").exists()

    def test_abrupt_uncommitted_write_is_rolled_back(self, tmp_path):
        import subprocess
        import sys

        path = tmp_path / "crash-book.breadsched"
        db = DbSQLite()
        db.load(str(path))
        db.close()

        code = """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA journal_mode=DELETE')
conn.execute('PRAGMA synchronous=FULL')
conn.execute('BEGIN IMMEDIATE')
conn.execute("INSERT INTO metadata(key,value) VALUES ('uncommitted_crash_marker','bad')")
os._exit(0)
"""
        subprocess.run([sys.executable, "-c", code, str(path)], check=True)

        recovered = DbSQLite()
        recovered.load(str(path))
        try:
            assert recovered.get_metadata("uncommitted_crash_marker") is None
            assert recovered.integrity_problems() == []
        finally:
            recovered.close()


class TestTransactionLifecycleSafety:
    def test_close_refuses_while_a_transaction_is_active(self, db):
        with db.transaction("active"):
            with pytest.raises(DbError, match="transaction is active"):
                db.close()
        assert db.is_open is True

    def test_close_rolls_back_stray_uncommitted_sql(self, tmp_path):
        path = tmp_path / "stray.breadsched"
        db = DbSQLite()
        db.load(str(path))
        db._require().execute("INSERT INTO metadata(key,value) VALUES ('stray_uncommitted','no')")
        db.close()

        reopened = DbSQLite()
        reopened.load(str(path), mode="r")
        try:
            assert reopened.get_metadata("stray_uncommitted") is None
        finally:
            reopened.close()

    def test_reopening_backend_discards_old_book_undo_history(self, tmp_path):
        first = tmp_path / "first.breadsched"
        second = tmp_path / "second.breadsched"
        db = DbSQLite()
        db.load(str(first))
        with db.transaction("first book edit") as txn:
            db.add_account(Account(name="First", atype=AccountType.BANK), txn)
        assert db.undo_message() == "first book edit"
        db.close()

        db.load(str(second))
        try:
            assert db.undo() is False
            assert db.redo() is False
            assert db.undo_message() is None
        finally:
            db.close()

    def test_exception_during_commit_restores_transaction_state(self, db, monkeypatch):
        real_verify = db._verify_changes
        calls = 0

        def explode_once(records, *, reverse=False):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("verification exploded")
            return real_verify(records, reverse=reverse)

        monkeypatch.setattr(db, "_verify_changes", explode_once)
        with pytest.raises(RuntimeError, match="verification exploded"):
            with db.transaction("broken commit") as txn:
                db.add_account(Account(name="Not durable", atype=AccountType.BANK), txn)

        assert db._active_txn is None
        with db.transaction("works afterwards") as txn:
            kept = Account(name="Durable", atype=AccountType.BANK)
            db.add_account(kept, txn)
        assert db.get_account(kept.handle) is not None

    def test_normal_commit_does_not_run_full_book_verification(self, db, book, monkeypatch):
        def unexpected_full_scan():
            raise AssertionError("normal commits must not run verify_book()")

        monkeypatch.setattr(db, "verify_book", unexpected_full_scan)
        posted = Transaction.simple(
            date(2026, 3, 1), "Incremental verification", book.rent, book.checking, "10"
        )
        with db.transaction("incremental commit") as txn:
            db.add_transaction(posted, txn)

        assert db.get_transaction(posted.handle) is not None
        assert db.undo() is True
        assert db.get_transaction(posted.handle) is None
        assert db.redo() is True
        assert db.get_transaction(posted.handle) is not None

    def test_undo_is_not_allowed_inside_an_active_transaction(self, db, book):
        with db.transaction("post") as txn:
            db.add_transaction(
                Transaction.simple(date(2026, 1, 1), "One", book.rent, book.checking, "1"), txn
            )
        with db.transaction("another"):
            with pytest.raises(DbError, match="cannot undo or redo"):
                db.undo()


class TestUserBackups:
    def test_backup_to_includes_current_book_and_is_readable(self, tmp_path):
        path = tmp_path / "book.breadsched"
        backup = tmp_path / "book.backup"
        db = DbSQLite()
        db.load(str(path))
        with db.transaction("account") as txn:
            account = Account(name="Backed up", atype=AccountType.BANK)
            db.add_account(account, txn)
        assert db.backup_to(str(backup)) == str(backup)
        db.close()

        restored = DbSQLite()
        restored.load(str(backup), mode="r")
        try:
            assert restored.get_account(account.handle).name == "Backed up"
            assert restored.verify_book() == []
        finally:
            restored.close()

    def test_backup_refuses_to_overwrite_without_permission(self, db, tmp_path):
        target = tmp_path / "existing.backup"
        target.write_text("do not replace")
        with pytest.raises(DbError, match="already exists"):
            db.backup_to(str(target))
        assert target.read_text() == "do not replace"

    def test_restore_preserves_the_book_it_replaces(self, tmp_path):
        source_book = tmp_path / "source.breadsched"
        target_book = tmp_path / "target.breadsched"
        backup = tmp_path / "saved.backup"

        source = DbSQLite()
        source.load(str(source_book))
        with source.transaction("source account") as txn:
            wanted = Account(name="Wanted", atype=AccountType.BANK)
            source.add_account(wanted, txn)
        source.backup_to(str(backup))
        source.close()

        target = DbSQLite()
        target.load(str(target_book))
        with target.transaction("old account") as txn:
            old = Account(name="Old", atype=AccountType.BANK)
            target.add_account(old, txn)
        target.close()

        DbSQLite.restore_backup(str(backup), str(target_book), overwrite=True)

        restored = DbSQLite()
        restored.load(str(target_book), mode="r")
        try:
            assert restored.get_account(wanted.handle) is not None
            assert restored.get_account(old.handle) is None
        finally:
            restored.close()
        preserved = DbSQLite()
        preserved.load(str(target_book) + ".pre-restore.bak", mode="r")
        try:
            assert preserved.get_account(old.handle) is not None
        finally:
            preserved.close()

    def test_restore_rejects_a_logically_damaged_backup(self, tmp_path):
        source = tmp_path / "damaged.backup"
        destination = tmp_path / "restored.breadsched"
        db = DbSQLite()
        db.load(str(source))
        db.close()
        conn = sqlite3.connect(source)
        conn.execute(
            "INSERT INTO account(handle,parent,name,atype,blob) VALUES (?,?,?,?,?)",
            (
                "bad",
                "missing",
                "Bad",
                "BANK",
                json.dumps({"handle": "bad", "name": "Bad", "atype": "BANK", "parent": "missing"}),
            ),
        )
        conn.commit()
        conn.close()
        with pytest.raises(DbError, match="logical verification"):
            DbSQLite.restore_backup(str(source), str(destination))
        assert not destination.exists()

    def test_restore_refuses_to_replace_a_book_with_a_live_writer(self, tmp_path):
        source_path = tmp_path / "source.breadsched"
        backup_path = tmp_path / "source.backup"
        destination = tmp_path / "open.breadsched"
        source = DbSQLite()
        source.load(str(source_path))
        source.backup_to(str(backup_path))
        source.close()

        live = DbSQLite()
        live.load(str(destination))
        try:
            with pytest.raises(DbError, match="already open for writing"):
                DbSQLite.restore_backup(str(backup_path), str(destination), overwrite=True)
        finally:
            live.close()


def test_delete_journal_recovers_an_interrupted_write(tmp_path):
    path = tmp_path / "interrupted.breadsched"
    db = DbSQLite()
    db.load(str(path))
    db.close()
    script = """
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA journal_mode=DELETE')
connection.execute('PRAGMA synchronous=FULL')
connection.execute('BEGIN IMMEDIATE')
connection.execute(
    \"INSERT OR REPLACE INTO metadata(key,value) VALUES ('interrupted', 'true')\"
)
os._exit(7)
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)], check=False)
    assert result.returncode == 7

    recovered = DbSQLite()
    recovered.load(str(path))
    try:
        assert recovered.get_metadata("interrupted") is None
        assert recovered.verification_report().ok
    finally:
        recovered.close()


class TestDerivedIndexVerification:
    @pytest.mark.parametrize(
        "table,column,bad_value,code",
        [
            ("account", "name", "wrong", "account.index_mismatch"),
            ("txn", "description", "wrong", "txn.index_mismatch"),
            ("commodity", "mnemonic", "WRONG", "commodity.index_mismatch"),
            ("price", "source", "wrong", "price.index_mismatch"),
            ("scheduled", "name", "wrong", "scheduled.index_mismatch"),
            ("scenario", "name", "wrong", "scenario.index_mismatch"),
        ],
    )
    def test_verifier_detects_drift_in_derived_columns(
        self, tmp_path, table, column, bad_value, code
    ):
        path = tmp_path / "derived.breadsched"
        db = DbSQLite()
        db.load(str(path))
        # Use a minimal valid object for whichever derived table is under test.
        with db.transaction("objects") as txn:
            root = Account(name="Root", atype=AccountType.ROOT)
            db.add_account(root, txn)
            commodity = Commodity(mnemonic="USD", fullname="US Dollar")
            db.add_commodity(commodity, txn)
            if table == "txn":
                expense = Account(name="Expense", atype=AccountType.EXPENSE, parent=root.handle)
                bank = Account(name="Bank", atype=AccountType.BANK, parent=root.handle)
                db.add_account(expense, txn)
                db.add_account(bank, txn)
                obj = Transaction.simple(
                    date(2026, 1, 1),
                    "Original",
                    expense.handle,
                    bank.handle,
                    "1",
                )
                db.add_transaction(obj, txn)
            elif table == "scheduled":
                obj = ScheduledTransaction(name="Original")
                db.add_scheduled(obj, txn)
            elif table == "scenario":
                obj = Scenario(name="Original")
                db.add_scenario(obj, txn)
            elif table == "commodity":
                obj = commodity
            elif table == "price":
                security = Commodity(namespace="FUND", mnemonic="INDEX")
                db.add_commodity(security, txn)
                obj = CommodityPrice(
                    commodity=security.handle,
                    currency=commodity.handle,
                    quote_date=date(2026, 1, 1),
                    value=Money("10"),
                )
                db.add_price(obj, txn)
            else:
                obj = root
        db.close()

        conn = sqlite3.connect(path)
        conn.execute(f"UPDATE {table} SET {column}=? WHERE handle=?", (bad_value, obj.handle))
        conn.commit()
        conn.close()

        verifier = DbSQLite()
        verifier.load_for_verification(str(path))
        try:
            assert any(issue.code == code for issue in verifier.verify_book())
        finally:
            verifier.close()


class TestObjectIdentityVerification:
    def test_verifier_detects_row_and_blob_handle_disagreement(self, tmp_path):
        path = tmp_path / "identity.breadsched"
        db = DbSQLite()
        db.load(str(path))
        with db.transaction("account") as txn:
            account = Account(name="One", atype=AccountType.BANK)
            db.add_account(account, txn)
        db.close()

        conn = sqlite3.connect(path)
        row = conn.execute("SELECT blob FROM account WHERE handle=?", (account.handle,)).fetchone()
        data = json.loads(row[0])
        data["handle"] = "different-internal-handle"
        conn.execute("UPDATE account SET blob=? WHERE handle=?", (json.dumps(data), account.handle))
        conn.commit()
        conn.close()

        verifier = DbSQLite()
        verifier.load_for_verification(str(path))
        try:
            assert any(issue.code == "account.handle_mismatch" for issue in verifier.verify_book())
        finally:
            verifier.close()

    def test_verifier_detects_duplicate_internal_handles(self, tmp_path):
        path = tmp_path / "duplicate-identity.breadsched"
        db = DbSQLite()
        db.load(str(path))
        with db.transaction("accounts") as txn:
            first = Account(name="First", atype=AccountType.BANK)
            second = Account(name="Second", atype=AccountType.BANK)
            db.add_account(first, txn)
            db.add_account(second, txn)
        db.close()

        conn = sqlite3.connect(path)
        row = conn.execute("SELECT blob FROM account WHERE handle=?", (second.handle,)).fetchone()
        data = json.loads(row[0])
        data["handle"] = first.handle
        conn.execute("UPDATE account SET blob=? WHERE handle=?", (json.dumps(data), second.handle))
        conn.commit()
        conn.close()

        verifier = DbSQLite()
        verifier.load_for_verification(str(path))
        try:
            codes = {issue.code for issue in verifier.verify_book()}
            assert "account.handle_mismatch" in codes
            assert "account.duplicate_object_handle" in codes
        finally:
            verifier.close()


class TestRestoreSidecars:
    def test_restore_removes_stale_destination_sidecars(self, tmp_path):
        source_path = tmp_path / "source.breadsched"
        backup_path = tmp_path / "source.backup"
        destination = tmp_path / "destination.breadsched"

        source = DbSQLite()
        source.load(str(source_path))
        with source.transaction("wanted") as txn:
            wanted = Account(name="Wanted", atype=AccountType.BANK)
            source.add_account(wanted, txn)
        source.backup_to(str(backup_path))
        source.close()

        target = DbSQLite()
        target.load(str(destination))
        target.close()
        Path(str(destination) + "-wal").write_bytes(b"stale wal")
        Path(str(destination) + "-shm").write_bytes(b"stale shm")
        Path(str(destination) + "-journal").write_bytes(b"stale journal")

        DbSQLite.restore_backup(str(backup_path), str(destination), overwrite=True)
        assert not Path(str(destination) + "-wal").exists()
        assert not Path(str(destination) + "-shm").exists()
        assert not Path(str(destination) + "-journal").exists()
        restored = DbSQLite()
        restored.load(str(destination), mode="r")
        try:
            assert restored.get_account(wanted.handle) is not None
        finally:
            restored.close()
