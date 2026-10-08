"""The native SQLite backend.

Layout follows the pattern Gramps settled on after years of schema churn: one row
per object holding a serialised blob, plus a handful of *derived* indexed columns
that exist purely so the common queries can be answered without deserialising the
whole table. The blob is authoritative; every indexed column can be rebuilt from
it, so evolving query indexes does not create a second source of financial truth.

``split_index`` is the one table with no object of its own.  It is a flattened view
of the splits inside each transaction blob, and it is what makes "the register for
this account" and "the balance on this date" index scans instead of full scans.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, TypeVar

from ..lib.account import Account
from ..lib.base import PrimaryObject
from ..lib.commodity import DEFAULT_CURRENCY, Commodity, CommodityPrice
from ..lib.fsa_claim import FsaClaim
from ..lib.receivable import Receivable
from ..lib.reconciliation import Reconciliation
from ..lib.savings_goal import SavingsGoal
from ..lib.scenario import Assumptions, Scenario
from ..lib.scheduled import ScheduledTransaction
from ..lib.transaction import Transaction
from ..utils.logs import get_logger
from ..utils.user_paths import portal_document_id, sync_service_for_path
from .backups import backup_connection, migration_backup_path, restore_backup, snapshot_of
from .base import DbError, DbReadonlyError, DbTxn
from .book_lock import BookWriterLock
from .change_verification import ChangeVerification
from .migrations import MIGRATIONS, MIN_SUPPORTED_SCHEMA_VERSION
from .storage_verification import derived_column_issues, split_index_issues
from .verification import BookIssue, BookVerification, verify_domain

LOG = get_logger(__name__)

__all__ = ["DbSQLite", "is_portal_book", "migration_backup_path"]

SCHEMA_VERSION = 11
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
CREATE TABLE IF NOT EXISTS price (
    handle     TEXT PRIMARY KEY,
    commodity  TEXT NOT NULL,
    currency   TEXT NOT NULL,
    quote_date TEXT NOT NULL,
    source     TEXT NOT NULL,
    blob       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_lookup
    ON price(commodity, currency, quote_date);
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
    value_den INTEGER NOT NULL,
    quantity_num INTEGER NOT NULL DEFAULT 0,
    quantity_den INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_split_account ON split_index(account, post_date);
CREATE INDEX IF NOT EXISTS idx_split_txn ON split_index(txn);
CREATE TABLE IF NOT EXISTS scheduled (
    handle TEXT PRIMARY KEY,
    name   TEXT NOT NULL,
    blob   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scenario (
    handle TEXT PRIMARY KEY,
    name   TEXT NOT NULL,
    blob   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fsa_claim (
    handle       TEXT PRIMARY KEY,
    service_date TEXT NOT NULL,
    provider     TEXT NOT NULL,
    blob         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fsa_claim_service_date ON fsa_claim(service_date);
CREATE TABLE IF NOT EXISTS reconciliation (
    handle         TEXT PRIMARY KEY,
    account        TEXT NOT NULL,
    statement_date TEXT NOT NULL,
    status         TEXT NOT NULL,
    blob           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reconciliation_account_date
    ON reconciliation(account, statement_date);
CREATE TABLE IF NOT EXISTS receivable (
    handle        TEXT PRIMARY KEY,
    incurred_date TEXT NOT NULL,
    payer         TEXT NOT NULL,
    blob          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receivable_incurred_date ON receivable(incurred_date);
CREATE TABLE IF NOT EXISTS savings_goal (
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
    "price": (CommodityPrice, "price"),
    "account": (Account, "account"),
    "txn": (Transaction, "transaction"),
    "scheduled": (ScheduledTransaction, "scheduled"),
    "scenario": (Scenario, "scenario"),
    "fsa_claim": (FsaClaim, "fsa-claim"),
    "reconciliation": (Reconciliation, "reconciliation"),
    "receivable": (Receivable, "receivable"),
    "savings_goal": (SavingsGoal, "savings-goal"),
}


def is_portal_book(path: str | Path | None) -> bool:
    """Whether ``path`` is a file the sandbox reaches only through the document portal."""
    return bool(path) and path != ":memory:" and portal_document_id(str(path)) is not None


class DbSQLite(ChangeVerification):
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
        self._book_lock: BookWriterLock | None = None
        #: Where the verified backup made before the last migration was written.
        self.migration_backup: str | None = None

    # ------------------------------------------------------------- life cycle

    def _release_book_lock(self) -> None:
        lock, self._book_lock = self._book_lock, None
        if lock is not None:
            lock.release()

    def load(self, path: str, mode: str = "w") -> None:
        """Open ``path`` writable (``"w"``) or as a read-only snapshot (``"r"``).

        A read-only open copies the whole book into memory in one SQLite backup,
        so everything read through it — accounts, transactions, schedules,
        prices, metadata — belongs to the single committed generation that
        existed when it opened. A write committed afterwards is not seen until
        the next open, and the writer is never held up by a long reader: the
        copy takes a shared lock only while it runs (#234).
        """
        self._load(path, mode, tolerate_malformed=False)

    def load_for_verification(self, path: str) -> None:
        """Open a book read-only while tolerating malformed object blobs.

        Normal application opens remain strict: malformed primary data should stop
        ordinary financial operations.  The verifier is different; its purpose is
        to diagnose damaged books, so it must be able to get past one bad object and
        report the rest of the damage without modifying anything.
        """
        self._load(path, "r", tolerate_malformed=True, snapshot=False)

    def _load(
        self, path: str, mode: str, *, tolerate_malformed: bool, snapshot: bool = True
    ) -> None:
        if mode not in {"r", "w"}:
            raise ValueError("mode must be 'r' or 'w'")

        self.path = path
        self.readonly = mode == "r"
        if path != ":memory:":
            sync_service = sync_service_for_path(path)
            if sync_service is not None:
                LOG.warning(
                    "book %s is inside %s; SQLite files should not rely on cloud sync "
                    "as their only copy",
                    path,
                    sync_service,
                )
        self._tolerate_malformed = tolerate_malformed
        self._verification_load_issues.clear()
        # Undo records belong to one open book only.  Reusing a backend instance
        # must never make edits from the previous book replayable into this one.
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._active_txn = None
        existing_book = path != ":memory:" and Path(path).exists() and Path(path).stat().st_size > 0

        if not self.readonly:
            self._book_lock = BookWriterLock.acquire(path)

        # Read-only means read-only at SQLite level, not merely at our Python API.
        # This prevents PRAGMAs or accidental direct SQL from modifying the book.
        try:
            if self.readonly and path != ":memory:":
                uri = Path(path).resolve().as_uri() + "?mode=ro"
                source = sqlite3.connect(uri, uri=True, check_same_thread=False)
                if snapshot:
                    self._conn = snapshot_of(source)
                else:
                    # Verification diagnoses the file itself, damage included.
                    self._conn = source
            else:
                self._conn = sqlite3.connect(path, check_same_thread=False)
        except Exception:
            self._release_book_lock()
            raise
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
            self._release_book_lock()
            raise

    def _initialise_new_book(self) -> None:
        conn = self._require_writable()
        conn.executescript(_SCHEMA)
        currency = DEFAULT_CURRENCY.serialize()
        conn.execute(
            "INSERT INTO commodity(handle,mnemonic,blob) VALUES (?,?,?)",
            (
                DEFAULT_CURRENCY.handle,
                DEFAULT_CURRENCY.mnemonic,
                json.dumps(currency, separators=(",", ":")),
            ),
        )
        self._set_metadata_uncommitted("default_currency", DEFAULT_CURRENCY.handle)
        self._set_metadata_uncommitted("schema_version", SCHEMA_VERSION)
        conn.execute(
            "INSERT INTO schema_migration(version) VALUES (?)",
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
                f"book uses unsupported newer schema {version}; this BreadSched "
                f"alpha opens through schema {SCHEMA_VERSION}"
            )
        if version < MIN_SUPPORTED_SCHEMA_VERSION:
            raise DbError(
                f"book schema {version} predates the supported rolling window "
                f"(schema {MIN_SUPPORTED_SCHEMA_VERSION})"
            )
        if version < SCHEMA_VERSION:
            if self.readonly:
                raise DbError(
                    f"book uses schema {version}; open it writable once (for example with "
                    f"`breadsched migrate`) to migrate to schema {SCHEMA_VERSION}"
                )
            self._migrate(version)

    def _migrate(self, version: int) -> None:
        conn = self._require_writable()
        self._backup_before_migration(version)
        current = version
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migration (
                    version    INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migration(version) VALUES (?)",
                (current,),
            )
            while current < SCHEMA_VERSION:
                migration = MIGRATIONS.get(current)
                if migration is None or migration.target != current + 1:
                    raise DbError(
                        f"no sequential migration is available from schema {current} "
                        f"to {current + 1}"
                    )
                migration.apply(conn)
                current = migration.target
                self._set_metadata_uncommitted("schema_version", current)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migration(version) VALUES (?)",
                    (current,),
                )
            problems = self.integrity_problems()
            if problems:
                raise DbError("migrated book failed SQLite integrity check: " + "; ".join(problems))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def backup_to(self, destination: str, *, overwrite: bool = False) -> str:
        """Write a consistent, sidecar-free backup of the currently open book."""
        return backup_connection(self._require(), destination, self.path, overwrite=overwrite)

    def _backup_before_migration(self, version: int) -> str | None:
        """Preserve a verified snapshot before the first migration write."""
        if self.path is None or self.path == ":memory:":
            return None
        target = migration_backup_path(self.path, version)
        self.migration_backup = str(target)
        return self.backup_to(str(target), overwrite=True)

    @classmethod
    def restore_backup(cls, source: str, destination: str, *, overwrite: bool = False) -> str:
        """Restore a verified BreadSched backup to ``destination`` (see ``backups``)."""

        def preserve(target: Path, copy: Path) -> None:
            old = cls()
            old.load(str(target), mode="r")
            try:
                old.backup_to(str(copy), overwrite=True)
            finally:
                old.close()

        return restore_backup(
            source, destination, overwrite=overwrite, verify=cls.verify_path, preserve=preserve
        )

    def integrity_problems(self) -> list[str]:
        """Return SQLite integrity failures; an empty list means the file is sound."""
        rows = self._require().execute("PRAGMA integrity_check").fetchall()
        return [str(row[0]) for row in rows if str(row[0]).lower() != "ok"]

    def verification_report(self) -> BookVerification:
        """Return one combined physical/logical verification result."""
        raw_version = self.get_metadata("schema_version")
        try:
            native_schema_version = int(raw_version) if raw_version is not None else None
        except (TypeError, ValueError):
            native_schema_version = None
        return BookVerification(
            tuple(self.integrity_problems()),
            tuple(self.verify_book()),
            native_schema_version,
        )

    @classmethod
    def verify_path(cls, path: str) -> BookVerification:
        """Verify a native book read-only, tolerating malformed object records."""
        verifier = cls()
        verifier.load_for_verification(path)
        try:
            return verifier.verification_report()
        finally:
            verifier.close()

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

        issues.extend(derived_column_issues(conn))

        for claim in self.iter_fsa_claims():
            issues.extend(self._verify_fsa_claim_references(claim))

        issues.extend(split_index_issues(conn, self.iter_transactions()))
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
        self._release_book_lock()

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
        if table == "metadata":
            if data is None:
                conn.execute("DELETE FROM metadata WHERE key=?", (handle,))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)",
                    (handle, json.dumps(data["value"])),
                )
            return
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
                "INSERT OR REPLACE INTO account(handle,parent,name,atype,blob) VALUES (?,?,?,?,?)",
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
                "INSERT INTO split_index(handle,txn,account,post_date,value_num,value_den,"
                "quantity_num,quantity_den) VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        split["handle"],
                        handle,
                        split["account"],
                        data["post_date"],
                        split["value"][0],
                        split["value"][1],
                        split.get("quantity", split["value"])[0],
                        split.get("quantity", split["value"])[1],
                    )
                    for split in data.get("splits", [])
                ],
            )
        elif table == "commodity":
            conn.execute(
                "INSERT OR REPLACE INTO commodity(handle,mnemonic,blob) VALUES (?,?,?)",
                (handle, data.get("mnemonic", ""), blob),
            )
        elif table == "price":
            conn.execute(
                "INSERT OR REPLACE INTO price"
                "(handle,commodity,currency,quote_date,source,blob) VALUES (?,?,?,?,?,?)",
                (
                    handle,
                    data.get("commodity", ""),
                    data.get("currency", ""),
                    data.get("quote_date", ""),
                    data.get("source", ""),
                    blob,
                ),
            )
        elif table == "fsa_claim":
            conn.execute(
                "INSERT OR REPLACE INTO fsa_claim(handle,service_date,provider,blob) "
                "VALUES (?,?,?,?)",
                (handle, data.get("service_date", ""), data.get("provider", ""), blob),
            )
        elif table == "receivable":
            conn.execute(
                "INSERT OR REPLACE INTO receivable(handle,incurred_date,payer,blob) "
                "VALUES (?,?,?,?)",
                (handle, data.get("incurred_date", ""), data.get("payer", ""), blob),
            )
        elif table == "reconciliation":
            conn.execute(
                "INSERT OR REPLACE INTO reconciliation"
                "(handle,account,statement_date,status,blob) VALUES (?,?,?,?,?)",
                (
                    handle,
                    data.get("account", ""),
                    data.get("statement_date", ""),
                    data.get("status", ""),
                    blob,
                ),
            )
        else:
            conn.execute(
                f"INSERT OR REPLACE INTO {table}(handle,name,blob) VALUES (?,?,?)",
                (handle, data.get("name", ""), blob),
            )

    def _read(self, table: str, handle: str) -> dict[str, Any] | None:
        if table == "metadata":
            row = (
                self._require()
                .execute("SELECT value FROM metadata WHERE key=?", (handle,))
                .fetchone()
            )
            return {"value": json.loads(row["value"])} if row else None
        row = (
            self._require()
            .execute(f"SELECT blob FROM {table} WHERE handle=?", (handle,))
            .fetchone()
        )
        return json.loads(row["blob"]) if row else None

    def _write(self, obj: Any, txn: DbTxn, table: str) -> str:
        if self._active_txn is not txn:
            raise DbError("writes require the active database transaction")
        before = self._read(table, obj.handle)
        candidate = obj.serialize()
        if before is not None:
            before_content = {key: value for key, value in before.items() if key != "change"}
            candidate_content = {key: value for key, value in candidate.items() if key != "change"}
            if candidate_content == before_content:
                obj.change = int(before.get("change", obj.change))
                return obj.handle
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
            issues = self._verify_changes(txn.records)
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
            if txn.notify and not txn.batch:
                self._emit_for(txn)
            self.block_signals(False)
            if txn.notify and txn.batch and txn.records:
                self.emit("database-changed", (self,))
            if txn.notify:
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
        metadata_changed = False
        for table, handle, before, after in txn.records:
            if reverse:
                before, after = after, before
            if table == "metadata":
                metadata_changed = True
                continue
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
        if grouped or metadata_changed:
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
                issues = self._verify_changes(txn.records, reverse=reverse)
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
        if (
            self._require()
            .execute("SELECT 1 FROM split_index WHERE account=? LIMIT 1", (handle,))
            .fetchone()
        ):
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
        sql = (
            "SELECT s.*, json_extract(t.blob, '$.currency') AS currency, "
            "t.description AS description "
            "FROM split_index s JOIN txn t ON t.handle=s.txn WHERE s.account=?"
        )
        params: list[Any] = [account]
        if start:
            sql += " AND s.post_date >= ?"
            params.append(start.isoformat())
        if end:
            sql += " AND s.post_date <= ?"
            params.append(end.isoformat())
        sql += " ORDER BY s.post_date, s.handle"
        return list(self._require().execute(sql, params))

    # ------------------------------------------------------------- commodities

    def add_commodity(self, commodity: Commodity, txn: DbTxn) -> str:
        return self._write(commodity, txn, "commodity")

    def commit_commodity(self, commodity: Commodity, txn: DbTxn) -> None:
        self._write(commodity, txn, "commodity")

    def remove_commodity(self, handle: str, txn: DbTxn) -> None:
        self._delete("commodity", handle, txn)

    def get_commodity(self, handle: str) -> Commodity | None:
        data = self._read("commodity", handle)
        return Commodity.from_dict(data) if data else None

    def get_commodity_by_mnemonic(self, mnemonic: str) -> Commodity | None:
        row = (
            self._require()
            .execute("SELECT blob FROM commodity WHERE mnemonic=? LIMIT 1", (mnemonic,))
            .fetchone()
        )
        return Commodity.from_dict(json.loads(row["blob"])) if row else None

    def iter_commodities(self) -> Iterator[Commodity]:
        for row in self._require().execute("SELECT handle, blob FROM commodity ORDER BY mnemonic"):
            obj = self._decode_row("commodity", row["handle"], row["blob"], Commodity)
            if obj is not None:
                yield obj

    def add_price(self, price: CommodityPrice, txn: DbTxn) -> str:
        return self._write(price, txn, "price")

    def commit_price(self, price: CommodityPrice, txn: DbTxn) -> None:
        self._write(price, txn, "price")

    def remove_price(self, handle: str, txn: DbTxn) -> None:
        self._delete("price", handle, txn)

    def get_price(self, handle: str) -> CommodityPrice | None:
        data = self._read("price", handle)
        return CommodityPrice.from_dict(data) if data else None

    def iter_prices(
        self,
        commodity: str | None = None,
        currency: str | None = None,
        through: date | None = None,
    ) -> Iterator[CommodityPrice]:
        clauses: list[str] = []
        params: list[str] = []
        if commodity is not None:
            clauses.append("commodity=?")
            params.append(commodity)
        if currency is not None:
            clauses.append("currency=?")
            params.append(currency)
        if through is not None:
            clauses.append("quote_date<=?")
            params.append(through.isoformat())
        sql = "SELECT handle, blob FROM price"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY quote_date DESC, CASE source WHEN 'breadsched' THEN 0 ELSE 1 END, handle"
        for row in self._require().execute(sql, params):
            obj = self._decode_row("price", row["handle"], row["blob"], CommodityPrice)
            if obj is not None:
                yield obj

    # --------------------------------------------------- scheduled / scenarios

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
        for row in self._require().execute("SELECT handle, blob FROM scheduled ORDER BY name"):
            obj = self._decode_row("scheduled", row["handle"], row["blob"], ScheduledTransaction)
            if obj is not None:
                yield obj

    def add_scenario(self, scenario: Scenario, txn: DbTxn) -> str:
        self._validate_scenario_parent(scenario)
        return self._write(scenario, txn, "scenario")

    def commit_scenario(self, scenario: Scenario, txn: DbTxn) -> None:
        self._validate_scenario_parent(scenario)
        self._write(scenario, txn, "scenario")

    def remove_scenario(self, handle: str, txn: DbTxn) -> None:
        children = [
            child.name for child in self._iter_stored_scenarios() if child.parent_handle == handle
        ]
        if children:
            names = ", ".join(sorted(children, key=str.casefold))
            raise ValueError(f"reparent child scenario(s) before deleting: {names}")
        self._delete("scenario", handle, txn)

    def _stored_scenario(self, handle: str) -> Scenario | None:
        data = self._read("scenario", handle)
        return Scenario.from_dict(data) if data else None

    def _iter_stored_scenarios(self) -> Iterator[Scenario]:
        for row in self._require().execute("SELECT handle, blob FROM scenario ORDER BY name"):
            obj = self._decode_row("scenario", row["handle"], row["blob"], Scenario)
            if obj is not None:
                yield obj

    def _validate_scenario_parent(self, scenario: Scenario) -> None:
        parent_handle = scenario.parent_handle
        if parent_handle is None:
            return
        if not scenario.inherits_base_assumptions:
            raise ValueError("a scenario parent requires assumption inheritance")
        seen = {scenario.handle}
        while parent_handle is not None:
            if parent_handle in seen:
                raise ValueError(f"scenario parent cycle involving {scenario.name!r}")
            seen.add(parent_handle)
            parent = self._stored_scenario(parent_handle)
            if parent is None:
                raise ValueError(f"scenario parent does not exist: {parent_handle}")
            parent_handle = parent.parent_handle

    def _with_parent_assumptions(
        self, scenario: Scenario, resolving: tuple[str, ...] = ()
    ) -> Scenario:
        if not scenario.inherits_base_assumptions:
            return scenario
        if scenario.handle in resolving:
            raise ValueError(f"scenario parent cycle involving {scenario.name!r}")
        if scenario.parent_handle is None:
            stored = self.get_metadata("planning.base_assumptions", None)
            base = Assumptions.from_dict(stored) if isinstance(stored, dict) else Assumptions()
            scenario.attach_base_assumptions(base)
            return scenario
        parent = self._stored_scenario(scenario.parent_handle)
        if parent is None:
            raise ValueError(
                f"scenario {scenario.name!r} has missing parent {scenario.parent_handle}"
            )
        parent = self._with_parent_assumptions(parent, (*resolving, scenario.handle))
        scenario.attach_parent_assumptions(
            parent.effective_assumptions(),
            parent_name=parent.name,
            assumption_sources=parent.assumption_sources(),
            account_assumption_sources=parent.account_assumption_sources(),
        )
        return scenario

    def get_scenario(self, handle: str) -> Scenario | None:
        scenario = self._stored_scenario(handle)
        return self._with_parent_assumptions(scenario) if scenario else None

    def get_scenario_by_name(self, name: str) -> Scenario | None:
        row = (
            self._require()
            .execute("SELECT blob FROM scenario WHERE name=? LIMIT 1", (name,))
            .fetchone()
        )
        return (
            self._with_parent_assumptions(Scenario.from_dict(json.loads(row["blob"])))
            if row
            else None
        )

    def iter_scenarios(self) -> Iterator[Scenario]:
        for scenario in self._iter_stored_scenarios():
            yield self._with_parent_assumptions(scenario)

    # --------------------------------------------------------------- FSA claims

    def add_fsa_claim(self, claim: FsaClaim, txn: DbTxn) -> str:
        return self._write(claim, txn, "fsa_claim")

    def commit_fsa_claim(self, claim: FsaClaim, txn: DbTxn) -> None:
        self._write(claim, txn, "fsa_claim")

    def remove_fsa_claim(self, handle: str, txn: DbTxn) -> None:
        self._delete("fsa_claim", handle, txn)

    def get_fsa_claim(self, handle: str) -> FsaClaim | None:
        row = (
            self._require()
            .execute("SELECT blob FROM fsa_claim WHERE handle=?", (handle,))
            .fetchone()
        )
        return FsaClaim.from_dict(json.loads(row["blob"])) if row else None

    def iter_fsa_claims(self) -> Iterator[FsaClaim]:
        for row in self._require().execute(
            "SELECT handle, blob FROM fsa_claim ORDER BY service_date, handle"
        ):
            obj = self._decode_row("fsa_claim", row["handle"], row["blob"], FsaClaim)
            if obj is not None:
                yield obj

    # ---------------------------------------------------------- reconciliation

    def add_reconciliation(self, reconciliation: Reconciliation, txn: DbTxn) -> str:
        return self._write(reconciliation, txn, "reconciliation")

    def commit_reconciliation(self, reconciliation: Reconciliation, txn: DbTxn) -> None:
        self._write(reconciliation, txn, "reconciliation")

    def get_reconciliation(self, handle: str) -> Reconciliation | None:
        data = self._read("reconciliation", handle)
        return Reconciliation.from_dict(data) if data else None

    def iter_reconciliations(self, account: str | None = None) -> Iterator[Reconciliation]:
        sql = "SELECT handle, blob FROM reconciliation"
        params: list[str] = []
        if account is not None:
            sql += " WHERE account=?"
            params.append(account)
        sql += " ORDER BY statement_date, handle"
        for row in self._require().execute(sql, params):
            obj = self._decode_row("reconciliation", row["handle"], row["blob"], Reconciliation)
            if obj is not None:
                yield obj

    # ------------------------------------------------------------- receivables

    def add_receivable(self, receivable: Receivable, txn: DbTxn) -> str:
        return self._write(receivable, txn, "receivable")

    def commit_receivable(self, receivable: Receivable, txn: DbTxn) -> None:
        self._write(receivable, txn, "receivable")

    def remove_receivable(self, handle: str, txn: DbTxn) -> None:
        self._delete("receivable", handle, txn)

    def get_receivable(self, handle: str) -> Receivable | None:
        data = self._read("receivable", handle)
        return Receivable.from_dict(data) if data else None

    def iter_receivables(self) -> Iterator[Receivable]:
        for row in self._require().execute(
            "SELECT handle, blob FROM receivable ORDER BY incurred_date, handle"
        ):
            obj = self._decode_row("receivable", row["handle"], row["blob"], Receivable)
            if obj is not None:
                yield obj

    # ----------------------------------------------------------- savings goals

    def add_savings_goal(self, goal: SavingsGoal, txn: DbTxn) -> str:
        return self._write(goal, txn, "savings_goal")

    def commit_savings_goal(self, goal: SavingsGoal, txn: DbTxn) -> None:
        self._write(goal, txn, "savings_goal")

    def remove_savings_goal(self, handle: str, txn: DbTxn) -> None:
        self._delete("savings_goal", handle, txn)

    def get_savings_goal(self, handle: str) -> SavingsGoal | None:
        data = self._read("savings_goal", handle)
        return SavingsGoal.from_dict(data) if data else None

    def iter_savings_goals(self) -> Iterator[SavingsGoal]:
        for row in self._require().execute(
            "SELECT handle, blob FROM savings_goal ORDER BY name, handle"
        ):
            obj = self._decode_row("savings_goal", row["handle"], row["blob"], SavingsGoal)
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

    def set_metadata(self, key: str, value: Any, txn: DbTxn | None = None) -> None:
        conn = self._require_writable()
        if txn is None:
            if self._active_txn is not None:
                raise DbError("metadata writes inside a database transaction must use that DbTxn")
            self._set_metadata_uncommitted(key, value)
            conn.commit()
            return
        if self._active_txn is not txn:
            raise DbError("metadata writes require the active database transaction")
        before = self._read("metadata", key)
        after = {"value": value}
        self._store("metadata", key, after)
        txn.add("metadata", key, before, after)

    # ------------------------------------------------------------------ counts

    def summary(self) -> dict[str, int]:
        conn = self._require()
        return {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in (
                "account",
                "txn",
                "split_index",
                "scheduled",
                "scenario",
                "fsa_claim",
            )
        }
