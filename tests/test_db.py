"""The database contract: atomic batches, working undo, honest signals."""

import json
import socket
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from breadsched.gen.db.base import DbError
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.lib import (
    Account,
    AccountType,
    Budget,
    Commodity,
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
            posted = Transaction.simple(
                date(2026, 1, 5), "R", book.rent, book.checking, "1"
            )
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
                    Transaction.simple(
                        date(2026, 1, day), "R", book.rent, book.checking, "1"
                    ),
                    txn,
                )
        assert seen == []

    def test_a_failing_listener_does_not_break_the_write(self, db, book):
        db.connect("transaction-add", lambda handles: 1 / 0)
        with db.transaction("Post") as txn:
            posted = Transaction.simple(
                date(2026, 1, 5), "R", book.rent, book.checking, "1"
            )
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
            posted = Transaction.simple(
                date(2026, 1, 5), "R", book.rent, book.checking, "100"
            )
            db.add_transaction(posted, txn)
        assert len(db.split_rows(book.rent)) == 1

        with db.transaction("Move to groceries") as txn:
            posted.splits[0].account = book.groceries
            db.commit_transaction(posted, txn)
        assert db.split_rows(book.rent) == []
        assert len(db.split_rows(book.groceries)) == 1

    def test_deleting_a_transaction_clears_its_index_rows(self, db, book):
        with db.transaction("Post") as txn:
            posted = Transaction.simple(
                date(2026, 1, 5), "R", book.rent, book.checking, "100"
            )
            db.add_transaction(posted, txn)
        with db.transaction("Delete") as txn:
            db.remove_transaction(posted.handle, txn)
        assert db.split_rows(book.rent) == []


class TestSchemaMigration:
    @staticmethod
    def _make_v1_book(path):
        """Create a real v1-shaped file from the current schema for upgrade tests."""
        db = DbSQLite()
        db.load(str(path))
        with db.transaction("Add account") as txn:
            account = Account(name="Checking", atype=AccountType.BANK)
            db.add_account(account, txn)
        handle = account.handle
        db.set_metadata("schema_version", 1)
        db._require().execute("DROP TABLE schema_migration")
        db._require().commit()
        db.close()
        return handle

    def test_v1_book_is_backed_up_and_migrated_to_latest(self, tmp_path):
        path = tmp_path / "old.breadsched"
        handle = self._make_v1_book(path)

        db = DbSQLite()
        db.load(str(path))

        assert db.get_metadata("schema_version") == 3
        assert db.get_account(handle).name == "Checking"
        versions = [row[0] for row in db._require().execute(
            "SELECT version FROM schema_migration ORDER BY version"
        )]
        assert versions == [2, 3]
        assert db.integrity_problems() == []
        db.close()

        backup = tmp_path / "old.breadsched.pre-migration-v1.bak"
        assert backup.exists()
        import sqlite3

        old = sqlite3.connect(backup)
        try:
            version = old.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            assert version == "1"
            assert old.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migration'"
            ).fetchone() is None
        finally:
            old.close()

    def test_v2_claim_metadata_migrates_to_primary_rows(self, tmp_path):
        path = tmp_path / "claims-v2.breadsched"
        db = DbSQLite()
        db.load(str(path))
        legacy_claim = {
            "handle": "a" * 32,
            "service_date": "2026-05-01",
            "provider": "Generic provider",
            "description": "Generic service",
            "eob_responsibility": None,
            "payments": [],
            "refunds": [],
            "allocations": [],
        }
        conn = db._require()
        conn.execute("DROP TABLE fsa_claim")
        conn.execute("DROP INDEX IF EXISTS idx_fsa_claim_service_date")
        conn.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES ('fsa_claims', ?)",
            (json.dumps([legacy_claim]),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES ('schema_version', '2')"
        )
        conn.execute("DELETE FROM schema_migration WHERE version=3")
        conn.commit()
        db.close()

        migrated = DbSQLite()
        migrated.load(str(path))
        claim = migrated.get_fsa_claim(legacy_claim["handle"])
        assert migrated.get_metadata("schema_version") == 3
        assert migrated.get_metadata("fsa_claims") is None
        assert claim is not None
        assert claim.provider == "Generic provider"
        assert claim.description == "Generic service"
        assert migrated.verify_book() == []
        migrated.close()

    def test_failed_migration_rolls_back_the_original_book(self, tmp_path, monkeypatch):
        import sqlite3

        from breadsched.gen.db import sqlite as sqlite_backend

        path = tmp_path / "failure.breadsched"
        self._make_v1_book(path)

        def fail_halfway(conn):
            conn.execute("CREATE TABLE should_rollback(value TEXT)")
            raise RuntimeError("simulated migration failure")

        monkeypatch.setitem(sqlite_backend.MIGRATIONS, 1, fail_halfway)
        db = DbSQLite()
        with pytest.raises(RuntimeError, match="simulated migration failure"):
            db.load(str(path))
        assert not db.is_open

        raw = sqlite3.connect(path)
        try:
            assert raw.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0] == "1"
            assert raw.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='should_rollback'"
            ).fetchone() is None
        finally:
            raw.close()
        assert (tmp_path / "failure.breadsched.pre-migration-v1.bak").exists()

    def test_read_only_open_is_enforced_by_sqlite(self, tmp_path):
        import sqlite3

        path = tmp_path / "book.breadsched"
        db = DbSQLite()
        db.load(str(path))
        db.close()

        readonly = DbSQLite()
        readonly.load(str(path), mode="r")
        with pytest.raises(sqlite3.OperationalError):
            readonly._require().execute("CREATE TABLE forbidden(value TEXT)")
        readonly.close()

    def test_old_schema_is_not_silently_migrated_when_opened_read_only(self, tmp_path):
        path = tmp_path / "old-readonly.breadsched"
        self._make_v1_book(path)

        db = DbSQLite()
        with pytest.raises(DbError, match="open it writable once to migrate"):
            db.load(str(path), mode="r")
        assert not (tmp_path / "old-readonly.breadsched.pre-migration-v1.bak").exists()

    def test_new_books_report_clean_integrity(self, db):
        assert db.integrity_problems() == []


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

    def test_account_referenced_by_a_budget_cannot_be_deleted(self, db, book):
        with db.transaction("Planning account") as txn:
            planned = Account(name="Planned", atype=AccountType.EXPENSE, parent=book.expenses)
            db.add_account(planned, txn)
            budget = Budget(name="Plan")
            budget.set_amount(planned.handle, 0, "25")
            db.add_budget(budget, txn)

        with pytest.raises(DbError, match="budget.missing_account"):
            with db.transaction("Unsafe delete") as txn:
                db.remove_account(planned.handle, txn)
        assert db.get_account(planned.handle) is not None

    def test_mutually_linked_budget_and_scenario_are_atomic(self, db):
        budget = Budget(name="Plan")
        scenario = Scenario(name="Plan")
        budget.scenario = scenario.handle
        scenario.budget = budget.handle
        with db.transaction("Create linked planning objects") as txn:
            db.add_budget(budget, txn)
            db.add_scenario(scenario, txn)
        assert db.get_budget(budget.handle).scenario == scenario.handle
        assert db.get_scenario(scenario.handle).budget == budget.handle


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
        db._require().execute(
            "INSERT INTO metadata(key,value) VALUES ('stray_uncommitted','no')"
        )
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
            ("bad", "missing", "Bad", "BANK", json.dumps({
                "handle": "bad", "name": "Bad", "atype": "BANK", "parent": "missing"
            })),
        )
        conn.commit()
        conn.close()
        with pytest.raises(DbError, match="logical verification"):
            DbSQLite.restore_backup(str(source), str(destination))
        assert not destination.exists()


class TestDerivedIndexVerification:
    @pytest.mark.parametrize(
        "table,column,bad_value,code",
        [
            ("account", "name", "wrong", "account.index_mismatch"),
            ("txn", "description", "wrong", "txn.index_mismatch"),
            ("commodity", "mnemonic", "WRONG", "commodity.index_mismatch"),
            ("scheduled", "name", "wrong", "scheduled.index_mismatch"),
            ("budget", "name", "wrong", "budget.index_mismatch"),
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
            elif table == "budget":
                obj = Budget(name="Original")
                db.add_budget(obj, txn)
            elif table == "scenario":
                obj = Scenario(name="Original")
                db.add_scenario(obj, txn)
            elif table == "commodity":
                obj = commodity
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
    def test_restore_removes_stale_destination_wal_and_shm(self, tmp_path):
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

        DbSQLite.restore_backup(str(backup_path), str(destination), overwrite=True)
        assert not Path(str(destination) + "-wal").exists()
        assert not Path(str(destination) + "-shm").exists()
        restored = DbSQLite()
        restored.load(str(destination), mode="r")
        try:
            assert restored.get_account(wanted.handle) is not None
        finally:
            restored.close()
