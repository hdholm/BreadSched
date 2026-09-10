"""The native SQLite backend.

Layout follows the pattern Gramps settled on after years of schema churn: one row
per object holding a serialised blob, plus a handful of *derived* indexed columns
that exist purely so the common queries can be answered without deserialising the
whole table.  The blob is authoritative; every indexed column can be rebuilt from
it, which means adding a new query later is a migration of derived data only.

``split_index`` is the one table with no object of its own.  It is a flattened view
of the splits inside each transaction blob, and it is what makes "the register for
this account" and "the balance on this date" index scans instead of full scans.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, TypeVar

from ..lib.account import Account
from ..lib.base import PrimaryObject
from ..lib.budget import Budget
from ..lib.commodity import Commodity
from ..lib.scenario import Scenario
from ..lib.scheduled import ScheduledTransaction
from ..lib.transaction import Transaction
from .base import DbBase, DbError, DbReadonlyError, DbTxn
from .migrations import LATEST_SCHEMA_VERSION, MIGRATIONS
from .verification import BookIssue, verify_domain

__all__ = ["DbSQLite"]

SCHEMA_VERSION = LATEST_SCHEMA_VERSION
T = TypeVar("T", bound=PrimaryObject)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commodity (
    handle   TEXT PRIMARY KEY,
    mnemonic TEXT NOT NULL,
    blob     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commodity_mnemonic ON commodity(mnemonic);
CREATE TABLE IF NOT EXISTS account (
    handle TEXT PRIMARY KEY,
    parent TEXT,
    name   TEXT NOT NULL,
    atype  TEXT NOT NULL,
    blob   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_parent ON account(parent);
CREATE TABLE IF NOT EXISTS txn (
    handle      TEXT PRIMARY KEY,
    post_date   TEXT NOT NULL,
    description TEXT,
    blob        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_txn_date ON txn(post_date);
CREATE TABLE IF NOT EXISTS split_index (
    handle    TEXT PRIMARY KEY,
    txn       TEXT NOT NULL,
    account   TEXT NOT NULL,
    post_date TEXT NOT NULL,
    value_num INTEGER NOT NULL,
    value_den INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_split_account ON split_index(account, post_date);
CREATE INDEX IF NOT EXISTS idx_split_txn ON split_index(txn);
CREATE TABLE IF NOT EXISTS scheduled (
    handle TEXT PRIMARY KEY,
    name   TEXT NOT NULL,
    blob   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS budget (
    handle TEXT PRIMARY KEY,
    name   TEXT NOT NULL,
    blob   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scenario (
    handle TEXT PRIMARY KEY,
    name   TEXT NOT NULL,
    blob   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_migration (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

#: table -> (object class, signal stem)
_TABLES: dict[str, tuple[type, str]] = {
    "commodity": (Commodity, "commodity"),
    "account": (Account, "account"),
    "txn": (Transaction, "transaction"),
    "scheduled": (ScheduledTransaction, "scheduled"),
    "budget": (Budget, "budget"),
    "scenario": (Scenario, "scenario"),
}


class DbSQLite(DbBase):
    """Single-file SQLite store."""

    def __init__(self) -> None:
        super().__init__()
        self._conn: sqlite3.Connection | None = None
        self.path: str | None = None
        self._accounts: dict[str, Account] = {}
        self._active_txn: DbTxn | None = None
        self._write_lock = threading.RLock()
        self._tolerate_malformed = False
        self._verification_load_issues: dict[tuple[str, str], BookIssue] = {}

    # ------------------------------------------------------------- life cycle

    def load(self, path: str, mode: str = "w") -> None:
        self._load(path, mode, tolerate_malformed=False)

    def load_for_verification(self, path: str) -> None:
        """Open a book read-only while tolerating malformed object blobs.

        Normal application opens remain strict: malformed primary data should stop
        ordinary financial operations.  The verifier is different; its purpose is
        to diagnose damaged books, so it must be able to get past one bad object and
        report the rest of the damage without modifying anything.
        """
        self._load(path, "r", tolerate_malformed=True)

    def _load(self, path: str, mode: str, *, tolerate_malformed: bool) -> None:
        if mode not in {"r", "w"}:
            raise ValueError("mode must be 'r' or 'w'")

        self.path = path
        self.readonly = mode == "r"
        self._tolerate_malformed = tolerate_malformed
        self._verification_load_issues.clear()
        # Undo records belong to one open book only.  Reusing a backend instance
        # must never make edits from the previous book replayable into this one.
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._active_txn = None
        existing_book = path != ":memory:" and Path(path).exists() and Path(path).stat().st_size > 0

        # Read-only means read-only at SQLite level, not merely at our Python API.
        # This prevents PRAGMAs or accidental direct SQL from modifying the book.
        if self.readonly and path != ":memory:":
            uri = Path(path).resolve().as_uri() + "?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")

        if not self.readonly:
            # BreadSched is a single-writer desktop application.  DELETE journaling
            # keeps the user's book as one durable file instead of leaving persistent
            # ``-wal`` and ``-shm`` companions beside it. SQLite may create a temporary
            # ``-journal`` during a write, but removes it after a successful commit.
            self._conn.execute("PRAGMA journal_mode=DELETE")
            self._conn.execute("PRAGMA synchronous=FULL")

        try:
            if not self.readonly and not existing_book:
                self._initialise_new_book()
            elif existing_book or self.readonly:
                self._open_existing_book()

            self._accounts = {}
            for row in self._conn.execute("SELECT handle, blob FROM account"):
                account = self._decode_row("account", row["handle"], row["blob"], Account)
                if account is not None:
                    self._accounts[row["handle"]] = account
        except Exception:
            self._conn.close()
            self._conn = None
            self._accounts.clear()
            self._verification_load_issues.clear()
            raise

    def _initialise_new_book(self) -> None:
        conn = self._require_writable()
        conn.executescript(_SCHEMA)
        self._set_metadata_uncommitted("schema_version", SCHEMA_VERSION)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migration(version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()

    def _open_existing_book(self) -> None:
        problems = self.integrity_problems()
        if problems:
            raise DbError("book failed SQLite integrity check: " + "; ".join(problems))

        version = self.get_metadata("schema_version")
        if version is None:
            raise DbError("book has no schema version; refusing to guess its format")
        try:
            version = int(version)
        except (TypeError, ValueError) as exc:
            raise DbError(f"invalid book schema version: {version!r}") from exc

        if version > SCHEMA_VERSION:
            raise DbError(
                f"book was written by a newer version (schema {version}); upgrade BreadSched"
            )
        if version < SCHEMA_VERSION:
            if self.readonly:
                raise DbError(
                    f"book uses schema {version}; open it writable once to migrate to schema "
                    f"{SCHEMA_VERSION}"
                )
            self._migrate(version)

    def _migrate(self, version: int) -> None:
        conn = self._require_writable()
        self._backup_before_migration(version)
        current = version
        try:
            conn.execute("BEGIN IMMEDIATE")
            while current < SCHEMA_VERSION:
                migration = MIGRATIONS.get(current)
                if migration is None:
                    raise DbError(
                        f"no migration is available from schema {current} to {current + 1}"
                    )
                migration(conn)
                current += 1
                self._set_metadata_uncommitted("schema_version", current)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migration(version) VALUES (?)",
                    (current,),
                )
            problems = self.integrity_problems()
            if problems:
                raise DbError("migrated book failed integrity check: " + "; ".join(problems))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _remove_sqlite_sidecars(path: Path) -> None:
        for suffix in ("-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)

    def backup_to(self, destination: str, *, overwrite: bool = False) -> str:
        """Write a consistent, sidecar-free backup of the currently open book.

        SQLite's backup API includes committed pages that still live in the WAL,
        unlike copying only the main database file.  The destination is assembled
        under a temporary name and atomically installed only after its SQLite
        integrity check succeeds.
        """
        source = self._require()
        target = Path(destination)
        source_path = self.path
        if source_path is not None and source_path != ":memory:":
            try:
                if target.resolve() == Path(source_path).resolve():
                    raise DbError("backup destination must differ from the open book")
            except FileNotFoundError:
                pass
        if target.exists() and not overwrite:
            raise DbError(f"backup destination already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.unlink(missing_ok=True)
        backup = sqlite3.connect(temporary)
        try:
            source.backup(backup)
            backup.commit()
            rows = backup.execute("PRAGMA integrity_check").fetchall()
            problems = [str(row[0]) for row in rows if str(row[0]).lower() != "ok"]
            if problems:
                raise DbError("backup failed SQLite integrity check: " + "; ".join(problems))
        except Exception:
            backup.close()
            temporary.unlink(missing_ok=True)
            raise
        else:
            backup.close()
        self._remove_sqlite_sidecars(target)
        os.replace(temporary, target)
        return str(target)

    @classmethod
    def restore_backup(
        cls, source: str, destination: str, *, overwrite: bool = False
    ) -> str:
        """Restore a verified BreadSched backup to ``destination``.

        Restores never overwrite an existing book silently.  When overwrite is
        explicitly requested, a consistent ``.pre-restore.bak`` copy of the old
        book is made first so a mistaken restore remains reversible.
        """
        source_path = Path(source)
        target = Path(destination)
        if not source_path.exists():
            raise DbError(f"backup does not exist: {source}")
        if source_path.resolve() == target.resolve():
            raise DbError("backup source and restore destination must differ")
        if target.exists() and not overwrite:
            raise DbError(f"restore destination already exists: {target}")

        verifier = cls()
        verifier.load_for_verification(str(source_path))
        try:
            sqlite_issues = verifier.integrity_problems()
            logical_issues = verifier.verify_book()
            if sqlite_issues:
                raise DbError("backup failed SQLite integrity check: " + "; ".join(sqlite_issues))
            if logical_issues:
                first = logical_issues[0]
                raise DbError(f"backup failed logical verification: {first.code}: {first.message}")
        finally:
            verifier.close()

        if target.exists():
            old = cls()
            old.load(str(target), mode="r")
            try:
                old.backup_to(str(target) + ".pre-restore.bak", overwrite=True)
            finally:
                old.close()

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".restore.tmp")
        temporary.unlink(missing_ok=True)
        source_conn = sqlite3.connect(source_path)
        restored = sqlite3.connect(temporary)
        try:
            source_conn.backup(restored)
            restored.commit()
        finally:
            restored.close()
            source_conn.close()
        cls._remove_sqlite_sidecars(target)
        os.replace(temporary, target)
        return str(target)

    def _backup_before_migration(self, version: int) -> str | None:
        """Create a consistent, sidecar-free SQLite backup before changing a book."""
        if self.path in {None, ":memory:"}:
            return None
        return self.backup_to(f"{self.path}.pre-migration-v{version}.bak", overwrite=True)

    def integrity_problems(self) -> list[str]:
        """Return SQLite integrity failures; an empty list means the file is sound."""
        rows = self._require().execute("PRAGMA integrity_check").fetchall()
        return [str(row[0]) for row in rows if str(row[0]).lower() != "ok"]

    def _record_malformed(self, table: str, handle: str, exc: Exception) -> None:
        key = (table, handle)
        self._verification_load_issues.setdefault(
            key,
            BookIssue(
                f"{table}.malformed",
                f"{table} object {handle} cannot be decoded: {type(exc).__name__}: {exc}",
                handle,
            ),
        )

    def _decode_row(self, table: str, handle: str, blob: str, cls: type[T]) -> T | None:
        try:
            return cls.from_dict(json.loads(blob))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
            if not self._tolerate_malformed:
                raise DbError(f"malformed {table} object {handle}: {exc}") from exc
            self._record_malformed(table, handle, exc)
            return None

    def verify_book(self) -> list[BookIssue]:
        """Check logical object relationships and SQLite-derived indexes."""
        issues = list(self._verification_load_issues.values())
        issues.extend(verify_domain(self))
        conn = self._require()

        # Derived columns are query accelerators only; the JSON blob is
        # authoritative.  Detect drift so indexes can be rebuilt rather than
        # silently returning different data depending on the query path.
        derived_specs: dict[str, tuple[str, ...]] = {
            "commodity": ("mnemonic",),
            "account": ("parent", "name", "atype"),
            "txn": ("post_date", "description"),
            "scheduled": ("name",),
            "budget": ("name",),
            "scenario": ("name",),
        }
        defaults: dict[tuple[str, str], Any] = {
            ("commodity", "mnemonic"): "",
            ("account", "parent"): None,
            ("account", "name"): "",
            ("account", "atype"): "",
            ("txn", "description"): "",
            ("scheduled", "name"): "",
            ("budget", "name"): "",
            ("scenario", "name"): "",
        }
        for table, columns in derived_specs.items():
            selected = ", ".join(("handle", *columns, "blob"))
            internal_handles: dict[str, str] = {}
            for row in conn.execute(f"SELECT {selected} FROM {table}"):
                try:
                    data = json.loads(row["blob"])
                except (json.JSONDecodeError, TypeError):
                    continue  # the malformed-object diagnostic already owns this row
                internal = data.get("handle")
                if internal != row["handle"]:
                    issues.append(BookIssue(
                        f"{table}.handle_mismatch",
                        f"{table} row {row['handle']} contains object handle {internal!r}",
                        row["handle"],
                    ))
                if internal is not None:
                    previous = internal_handles.get(internal)
                    if previous is not None and previous != row["handle"]:
                        issues.append(BookIssue(
                            f"{table}.duplicate_object_handle",
                            f"{table} rows {previous} and {row['handle']} both contain "
                            f"object handle {internal}",
                            row["handle"],
                        ))
                    else:
                        internal_handles[internal] = row["handle"]
                for column in columns:
                    expected_value = data.get(column, defaults.get((table, column)))
                    if table == "txn" and column == "post_date" and expected_value is not None:
                        expected_value = str(expected_value)
                    if row[column] != expected_value:
                        issues.append(BookIssue(
                            f"{table}.index_mismatch",
                            f"{table} derived column {column} disagrees with blob for "
                            f"{row['handle']}",
                            row["handle"],
                        ))

        expected: dict[str, tuple[str, str, str, int, int]] = {}
        for transaction in self.iter_transactions():
            for split in transaction.splits:
                expected[split.handle] = (
                    transaction.handle, split.account, transaction.post_date.isoformat(),
                    split.value.numerator, split.value.denominator,
                )

        actual = {
            row["handle"]: (row["txn"], row["account"], row["post_date"],
                            row["value_num"], row["value_den"])
            for row in conn.execute(
                "SELECT handle, txn, account, post_date, value_num, value_den FROM split_index"
            )
        }
        for handle in sorted(expected.keys() - actual.keys()):
            issues.append(BookIssue(
                "split_index.missing", f"split {handle} is missing from split_index", handle
            ))
        for handle in sorted(actual.keys() - expected.keys()):
            issues.append(BookIssue(
                "split_index.orphan", f"split_index contains unknown split {handle}", handle
            ))
        for handle in sorted(expected.keys() & actual.keys()):
            if expected[handle] != actual[handle]:
                issues.append(BookIssue(
                    "split_index.mismatch",
                    f"split_index disagrees with transaction data for split {handle}",
                    handle,
                ))
        seen = {(issue.code, issue.handle, issue.message) for issue in issues}
        for issue in self._verification_load_issues.values():
            key = (issue.code, issue.handle, issue.message)
            if key not in seen:
                issues.append(issue)
                seen.add(key)
        return issues

    @classmethod
    def create_memory(cls) -> DbSQLite:
        """A fresh in-memory book.  Used heavily by the test suite."""
        db = cls()
        db.load(":memory:")
        return db

    def close(self) -> None:
        if self._active_txn is not None:
            raise DbError("cannot close a book while a database transaction is active")
        if self._conn is not None:
            # Every supported write path commits explicitly.  Anything else left
            # pending on the connection is accidental and must not become durable
            # merely because the application is closing.
            self._conn.rollback()
            self._conn.close()
            self._conn = None
        self._accounts.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._tolerate_malformed = False
        self._verification_load_issues.clear()
        self.readonly = False

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise DbError("no book is open")
        return self._conn

    def _require_writable(self) -> sqlite3.Connection:
        if self.readonly:
            raise DbReadonlyError("book is open read-only")
        return self._require()

    # ------------------------------------------------------------- raw storage

    def _store(self, table: str, handle: str, data: dict[str, Any] | None) -> None:
        """Insert, replace or delete one row, keeping derived tables in step."""
        conn = self._require_writable()
        if data is None:
            conn.execute(f"DELETE FROM {table} WHERE handle=?", (handle,))
            if table == "txn":
                conn.execute("DELETE FROM split_index WHERE txn=?", (handle,))
            if table == "account":
                self._accounts.pop(handle, None)
            return

        blob = json.dumps(data, separators=(",", ":"))
        if table == "account":
            conn.execute(
                "INSERT OR REPLACE INTO account(handle,parent,name,atype,blob)"
                " VALUES (?,?,?,?,?)",
                (handle, data.get("parent"), data.get("name", ""), data.get("atype", ""), blob),
            )
            self._accounts[handle] = Account.from_dict(data)
        elif table == "txn":
            conn.execute(
                "INSERT OR REPLACE INTO txn(handle,post_date,description,blob) VALUES (?,?,?,?)",
                (handle, data["post_date"], data.get("description", ""), blob),
            )
            conn.execute("DELETE FROM split_index WHERE txn=?", (handle,))
            conn.executemany(
                "INSERT INTO split_index(handle,txn,account,post_date,value_num,value_den)"
                " VALUES (?,?,?,?,?,?)",
                [
                    (
                        split["handle"], handle, split["account"], data["post_date"],
                        split["value"][0], split["value"][1],
                    )
                    for split in data.get("splits", [])
                ],
            )
        elif table == "commodity":
            conn.execute(
                "INSERT OR REPLACE INTO commodity(handle,mnemonic,blob) VALUES (?,?,?)",
                (handle, data.get("mnemonic", ""), blob),
            )
        else:
            conn.execute(
                f"INSERT OR REPLACE INTO {table}(handle,name,blob) VALUES (?,?,?)",
                (handle, data.get("name", ""), blob),
            )

    def _read(self, table: str, handle: str) -> dict[str, Any] | None:
        row = self._require().execute(
            f"SELECT blob FROM {table} WHERE handle=?", (handle,)
        ).fetchone()
        return json.loads(row["blob"]) if row else None

    def _write(self, obj: Any, txn: DbTxn, table: str) -> str:
        if self._active_txn is not txn:
            raise DbError("writes require the active database transaction")
        before = self._read(table, obj.handle)
        obj.change = int(time.time())
        after = obj.serialize()
        self._store(table, obj.handle, after)
        txn.add(table, obj.handle, before, after)
        return obj.handle

    def _delete(self, table: str, handle: str, txn: DbTxn) -> None:
        if self._active_txn is not txn:
            raise DbError("writes require the active database transaction")
        before = self._read(table, handle)
        if before is None:
            return
        self._store(table, handle, None)
        txn.add(table, handle, before, None)

    def _reload_account_cache(self) -> None:
        conn = self._require()
        self._accounts = {
            row["handle"]: Account.from_dict(json.loads(row["blob"]))
            for row in conn.execute("SELECT handle, blob FROM account")
        }

    # ---------------------------------------------------------- transactions

    def _txn_begin(self, txn: DbTxn) -> None:
        self._write_lock.acquire()
        try:
            if self._active_txn is not None:
                raise DbError("nested database transactions are not supported")
            conn = self._require_writable()
            conn.execute("BEGIN IMMEDIATE")
            self._active_txn = txn
            txn.timestamp = time.time()
            if txn.batch:
                self.block_signals(True)
        except Exception:
            self._write_lock.release()
            raise

    def _txn_commit(self, txn: DbTxn) -> None:
        conn: sqlite3.Connection | None = None
        try:
            if self._active_txn is not txn:
                raise DbError("attempted to commit a transaction that is not active")
            conn = self._require_writable()
            issues = self.verify_book()
            if issues:
                first = issues[0]
                raise DbError(
                    f"transaction would leave an invalid book: {first.code}: {first.message}"
                )
            conn.commit()
            self._active_txn = None
            if txn.records:
                self.undo_stack.append(txn)
                del self.undo_stack[: max(0, len(self.undo_stack) - self.undo_limit)]
                self.redo_stack.clear()
            # A batch stays silent through commit: an importer that emitted one signal
            # per row would repaint the account tree tens of thousands of times.
            if not txn.batch:
                self._emit_for(txn)
            self.block_signals(False)
            if txn.batch and txn.records:
                self.emit("database-changed", (self,))
            self.emit("undo-available", (bool(self.undo_stack),))
            self.emit("redo-available", (False,))
        except Exception:
            if conn is not None:
                conn.rollback()
            self._active_txn = None
            self.block_signals(False)
            if self._conn is not None:
                self._reload_account_cache()
            txn.records.clear()
            raise
        finally:
            self._write_lock.release()

    def _txn_abort(self, txn: DbTxn) -> None:
        try:
            if self._active_txn is not txn:
                raise DbError("attempted to abort a transaction that is not active")
            conn = self._require()
            conn.rollback()
            self._active_txn = None
            if txn.batch:
                self.block_signals(False)
            # The in-memory account cache is not covered by the SQL rollback.
            self._reload_account_cache()
            txn.records.clear()
        finally:
            self._write_lock.release()

    def _emit_for(self, txn: DbTxn, reverse: bool = False) -> None:
        grouped: dict[str, list[str]] = {}
        for table, handle, before, after in txn.records:
            if reverse:
                before, after = after, before
            stem = _TABLES[table][1]
            if before is None and after is not None:
                key = f"{stem}-add"
            elif after is None:
                key = f"{stem}-delete"
            else:
                key = f"{stem}-update"
            if key in self._known_signals():
                grouped.setdefault(key, []).append(handle)
        for signal, handles in grouped.items():
            self.emit(signal, (handles,))
        if grouped:
            self.emit("database-changed", (self,))

    # --------------------------------------------------------------- undo/redo

    def _replay(self, txn: DbTxn, reverse: bool) -> None:
        if self._active_txn is not None:
            raise DbError("cannot undo or redo while a database transaction is active")
        with self._write_lock:
            conn = self._require_writable()
            try:
                conn.execute("BEGIN IMMEDIATE")
                records = reversed(txn.records) if reverse else txn.records
                for table, handle, before, after in records:
                    target = before if reverse else after
                    self._store(table, handle, target)
                issues = self.verify_book()
                if issues:
                    first = issues[0]
                    raise DbError(
                        f"undo/redo would leave an invalid book: {first.code}: {first.message}"
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                self._reload_account_cache()
                raise
        self._emit_for(txn, reverse=reverse)

    def undo(self) -> bool:
        if not self.undo_stack:
            return False
        txn = self.undo_stack[-1]
        self._replay(txn, reverse=True)
        self.undo_stack.pop()
        self.redo_stack.append(txn)
        self.emit("undo-available", (bool(self.undo_stack),))
        self.emit("redo-available", (True,))
        return True

    def redo(self) -> bool:
        if not self.redo_stack:
            return False
        txn = self.redo_stack[-1]
        self._replay(txn, reverse=False)
        self.redo_stack.pop()
        self.undo_stack.append(txn)
        self.emit("undo-available", (True,))
        self.emit("redo-available", (bool(self.redo_stack),))
        return True

    def undo_message(self) -> str | None:
        return self.undo_stack[-1].message if self.undo_stack else None

    def redo_message(self) -> str | None:
        return self.redo_stack[-1].message if self.redo_stack else None

    # ---------------------------------------------------------------- accounts

    def add_account(self, account: Account, txn: DbTxn) -> str:
        return self._write(account, txn, "account")

    def commit_account(self, account: Account, txn: DbTxn) -> None:
        self._write(account, txn, "account")

    def remove_account(self, handle: str, txn: DbTxn) -> None:
        if self._require().execute(
            "SELECT 1 FROM split_index WHERE account=? LIMIT 1", (handle,)
        ).fetchone():
            raise DbError("cannot delete an account that still has transactions")
        if self.child_accounts(handle):
            raise DbError("cannot delete an account that still has children")
        self._delete("account", handle, txn)

    def get_account(self, handle: str) -> Account | None:
        return self._accounts.get(handle)

    def iter_accounts(self) -> Iterator[Account]:
        return iter(list(self._accounts.values()))

    def get_account_by_name(self, full_name: str) -> Account | None:
        """Look up by ``Assets:Current:Checking`` style path, or by bare name."""
        wanted = full_name.strip()
        for account in self._accounts.values():
            if self.full_name(account) == wanted:
                return account
        matches = [a for a in self._accounts.values() if a.name == wanted]
        return matches[0] if len(matches) == 1 else None

    def child_accounts(self, handle: str | None) -> list[Account]:
        return sorted(
            (a for a in self._accounts.values() if a.parent == handle),
            key=lambda a: (a.code, a.name.lower()),
        )

    def descendants(self, handle: str) -> list[Account]:
        """Every account below ``handle``, depth first, excluding itself."""
        out: list[Account] = []
        stack = list(self.child_accounts(handle))
        while stack:
            node = stack.pop(0)
            out.append(node)
            stack = list(self.child_accounts(node.handle)) + stack
        return out

    # ------------------------------------------------------------ transactions

    def add_transaction(self, txn_obj: Transaction, txn: DbTxn) -> str:
        txn_obj.validate()
        return self._write(txn_obj, txn, "txn")

    def commit_transaction(self, txn_obj: Transaction, txn: DbTxn) -> None:
        txn_obj.validate()
        self._write(txn_obj, txn, "txn")

    def remove_transaction(self, handle: str, txn: DbTxn) -> None:
        self._delete("txn", handle, txn)

    def get_transaction(self, handle: str) -> Transaction | None:
        data = self._read("txn", handle)
        return Transaction.from_dict(data) if data else None

    def iter_transactions(
        self,
        account: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> Iterator[Transaction]:
        conn = self._require()
        clauses: list[str] = []
        params: list[Any] = []
        if account:
            sql = (
                "SELECT DISTINCT t.handle, t.post_date, t.blob FROM txn t"
                " JOIN split_index s ON s.txn = t.handle WHERE s.account = ?"
            )
            params.append(account)
        else:
            sql = "SELECT t.handle, t.post_date, t.blob FROM txn t WHERE 1=1"
        if start:
            clauses.append("t.post_date >= ?")
            params.append(start.isoformat())
        if end:
            clauses.append("t.post_date <= ?")
            params.append(end.isoformat())
        if clauses:
            sql += " AND " + " AND ".join(clauses)
        sql += " ORDER BY t.post_date, t.handle"
        for row in conn.execute(sql, params):
            obj = self._decode_row("transaction", row["handle"], row["blob"], Transaction)
            if obj is not None:
                yield obj

    def split_rows(
        self,
        account: str,
        start: date | None = None,
        end: date | None = None,
    ) -> list[sqlite3.Row]:
        """Indexed split lookup: the fast path for balances and registers."""
        sql = "SELECT * FROM split_index WHERE account=?"
        params: list[Any] = [account]
        if start:
            sql += " AND post_date >= ?"
            params.append(start.isoformat())
        if end:
            sql += " AND post_date <= ?"
            params.append(end.isoformat())
        sql += " ORDER BY post_date, handle"
        return list(self._require().execute(sql, params))

    # ------------------------------------------------------------- commodities

    def add_commodity(self, commodity: Commodity, txn: DbTxn) -> str:
        return self._write(commodity, txn, "commodity")

    def get_commodity(self, handle: str) -> Commodity | None:
        data = self._read("commodity", handle)
        return Commodity.from_dict(data) if data else None

    def get_commodity_by_mnemonic(self, mnemonic: str) -> Commodity | None:
        row = self._require().execute(
            "SELECT blob FROM commodity WHERE mnemonic=? LIMIT 1", (mnemonic,)
        ).fetchone()
        return Commodity.from_dict(json.loads(row["blob"])) if row else None

    def iter_commodities(self) -> Iterator[Commodity]:
        for row in self._require().execute(
            "SELECT handle, blob FROM commodity ORDER BY mnemonic"
        ):
            obj = self._decode_row("commodity", row["handle"], row["blob"], Commodity)
            if obj is not None:
                yield obj

    # --------------------------------------------------- scheduled / budget etc

    def add_scheduled(self, sched: ScheduledTransaction, txn: DbTxn) -> str:
        return self._write(sched, txn, "scheduled")

    def commit_scheduled(self, sched: ScheduledTransaction, txn: DbTxn) -> None:
        self._write(sched, txn, "scheduled")

    def remove_scheduled(self, handle: str, txn: DbTxn) -> None:
        self._delete("scheduled", handle, txn)

    def get_scheduled(self, handle: str) -> ScheduledTransaction | None:
        data = self._read("scheduled", handle)
        return ScheduledTransaction.from_dict(data) if data else None

    def iter_scheduled(self) -> Iterator[ScheduledTransaction]:
        for row in self._require().execute(
            "SELECT handle, blob FROM scheduled ORDER BY name"
        ):
            obj = self._decode_row("scheduled", row["handle"], row["blob"], ScheduledTransaction)
            if obj is not None:
                yield obj

    def add_budget(self, budget: Budget, txn: DbTxn) -> str:
        return self._write(budget, txn, "budget")

    def commit_budget(self, budget: Budget, txn: DbTxn) -> None:
        self._write(budget, txn, "budget")

    def remove_budget(self, handle: str, txn: DbTxn) -> None:
        self._delete("budget", handle, txn)

    def get_budget(self, handle: str) -> Budget | None:
        data = self._read("budget", handle)
        return Budget.from_dict(data) if data else None

    def iter_budgets(self) -> Iterator[Budget]:
        for row in self._require().execute(
            "SELECT handle, blob FROM budget ORDER BY name"
        ):
            obj = self._decode_row("budget", row["handle"], row["blob"], Budget)
            if obj is not None:
                yield obj

    def add_scenario(self, scenario: Scenario, txn: DbTxn) -> str:
        return self._write(scenario, txn, "scenario")

    def commit_scenario(self, scenario: Scenario, txn: DbTxn) -> None:
        self._write(scenario, txn, "scenario")

    def remove_scenario(self, handle: str, txn: DbTxn) -> None:
        self._delete("scenario", handle, txn)

    def get_scenario(self, handle: str) -> Scenario | None:
        data = self._read("scenario", handle)
        return Scenario.from_dict(data) if data else None

    def get_scenario_by_name(self, name: str) -> Scenario | None:
        row = self._require().execute(
            "SELECT blob FROM scenario WHERE name=? LIMIT 1", (name,)
        ).fetchone()
        return Scenario.from_dict(json.loads(row["blob"])) if row else None

    def iter_scenarios(self) -> Iterator[Scenario]:
        for row in self._require().execute(
            "SELECT handle, blob FROM scenario ORDER BY name"
        ):
            obj = self._decode_row("scenario", row["handle"], row["blob"], Scenario)
            if obj is not None:
                yield obj

    # ---------------------------------------------------------------- metadata

    def get_metadata(self, key: str, default: Any = None) -> Any:
        conn = self._conn
        if conn is None:
            return default
        try:
            row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return default
        return json.loads(row["value"]) if row else default

    def _set_metadata_uncommitted(self, key: str, value: Any) -> None:
        self._require_writable().execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)",
            (key, json.dumps(value)),
        )

    def set_metadata(self, key: str, value: Any) -> None:
        conn = self._require_writable()
        self._set_metadata_uncommitted(key, value)
        conn.commit()

    # ------------------------------------------------------------------ counts

    def summary(self) -> dict[str, int]:
        conn = self._require()
        return {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("account", "txn", "split_index", "scheduled", "budget", "scenario")
        }
