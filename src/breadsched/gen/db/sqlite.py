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
import os
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, TypeVar

from ..lib.account import Account
from ..lib.base import PrimaryObject
from ..lib.commodity import Commodity, CommodityPrice
from ..lib.fsa_claim import FsaClaim
from ..lib.reconciliation import Reconciliation
from ..lib.scenario import Scenario
from ..lib.scheduled import ScheduledTransaction
from ..lib.transaction import Transaction, UnbalancedError
from ..utils.logs import get_logger
from ..utils.user_paths import sync_service_for_path
from .base import DbBase, DbError, DbReadonlyError, DbTxn
from .verification import BookIssue, BookVerification, verify_domain

LOG = get_logger(__name__)

__all__ = ["DbSQLite"]

SCHEMA_VERSION = 7
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
        self._book_lock_path: Path | None = None
        self._book_lock_token: str | None = None

    # ------------------------------------------------------------- life cycle

    @staticmethod
    def _writer_lock_path(path: str) -> Path:
        resolved = Path(path).expanduser().resolve()
        return resolved.with_name(f"{resolved.name}.lock")

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            # ``os.kill(pid, 0)`` is a harmless existence probe on POSIX. On
            # Windows, however, signal value 0 is CTRL_C_EVENT and can interrupt
            # the process whose liveness we are trying to inspect. Query a process
            # handle instead and treat access-denied or unexpected errors
            # conservatively as evidence that the process may still be alive.
            import ctypes

            win_dll = getattr(ctypes, "WinDLL", None)
            if win_dll is None:  # pragma: no cover - defensive Windows fallback
                return True
            kernel32 = win_dll("kernel32", use_last_error=True)
            process_query_limited_information = 0x1000
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            error_invalid_parameter = 87
            get_last_error = getattr(ctypes, "get_last_error", lambda: 0)
            return get_last_error() != error_invalid_parameter
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True

    def _acquire_book_lock(self, path: str) -> None:
        if path == ":memory:":
            return
        lock_path = self._writer_lock_path(path)
        hostname = socket.gethostname()
        token = os.urandom(16).hex()
        payload = {
            "pid": os.getpid(),
            "host": hostname,
            "token": token,
            "book": str(Path(path).expanduser().resolve()),
        }
        while True:
            try:
                fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                try:
                    existing = json.loads(lock_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    existing = {}
                owner_host = existing.get("host")
                owner_pid = existing.get("pid")
                stale = (
                    owner_host == hostname
                    and isinstance(owner_pid, int)
                    and not self._pid_is_alive(owner_pid)
                )
                if stale:
                    try:
                        lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                owner = "another process"
                if owner_host and owner_pid:
                    owner = f"PID {owner_pid} on {owner_host}"
                raise DbError(
                    f"book is already open for writing by {owner}; "
                    "close that writer or open this book read-only"
                ) from None
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._book_lock_path = lock_path
                self._book_lock_token = token
                return

    def _release_book_lock(self) -> None:
        lock_path = self._book_lock_path
        token = self._book_lock_token
        self._book_lock_path = None
        self._book_lock_token = None
        if lock_path is None or token is None:
            return
        try:
            existing = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if existing.get("token") != token:
            return
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass

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
            self._acquire_book_lock(path)

        # Read-only means read-only at SQLite level, not merely at our Python API.
        # This prevents PRAGMAs or accidental direct SQL from modifying the book.
        try:
            if self.readonly and path != ":memory:":
                uri = Path(path).resolve().as_uri() + "?mode=ro"
                self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
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
        self._set_metadata_uncommitted("schema_version", SCHEMA_VERSION)
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

        if version != SCHEMA_VERSION:
            direction = "newer" if version > SCHEMA_VERSION else "older"
            raise DbError(
                f"book uses unsupported {direction} schema {version}; this BreadSched "
                f"alpha opens schema {SCHEMA_VERSION} only"
            )

    @staticmethod
    def _remove_sqlite_sidecars(path: Path) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
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
    def restore_backup(cls, source: str, destination: str, *, overwrite: bool = False) -> str:
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

        report = cls.verify_path(str(source_path))
        if report.sqlite:
            raise DbError("backup failed SQLite integrity check: " + "; ".join(report.sqlite))
        if report.issues:
            first = report.issues[0]
            raise DbError(f"backup failed logical verification: {first.code}: {first.message}")

        # Hold the ordinary writer lock for the destination throughout preservation
        # and replacement. Replacing a pathname beneath a live SQLite connection
        # can split two writers across different inodes and corrupt either copy.
        target.parent.mkdir(parents=True, exist_ok=True)
        guard = cls()
        guard._acquire_book_lock(str(target))
        try:
            if target.exists():
                # Opening writable while our external writer guard is held lets
                # SQLite recover a genuine hot rollback journal before we preserve
                # the destination. A read-only open cannot perform that recovery.
                recovery = sqlite3.connect(target)
                try:
                    rows = recovery.execute("PRAGMA integrity_check").fetchall()
                    problems = [str(row[0]) for row in rows if str(row[0]).lower() != "ok"]
                    if problems:
                        raise DbError(
                            "existing destination failed SQLite recovery: " + "; ".join(problems)
                        )
                finally:
                    recovery.close()
                old = cls()
                old.load(str(target), mode="r")
                try:
                    old.backup_to(str(target) + ".pre-restore.bak", overwrite=True)
                finally:
                    old.close()

            temporary = target.with_name(target.name + ".restore.tmp")
            temporary.unlink(missing_ok=True)
            source_conn = sqlite3.connect(source_path)
            restored = sqlite3.connect(temporary)
            try:
                source_conn.backup(restored)
                restored.commit()
            except Exception:
                restored.close()
                source_conn.close()
                temporary.unlink(missing_ok=True)
                raise
            else:
                restored.close()
                source_conn.close()
            try:
                copied = cls.verify_path(str(temporary))
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            if not copied.ok:
                temporary.unlink(missing_ok=True)
                raise DbError("restored copy failed verification before installation")
            cls._remove_sqlite_sidecars(target)
            os.replace(temporary, target)
        finally:
            guard._release_book_lock()
        return str(target)

    def integrity_problems(self) -> list[str]:
        """Return SQLite integrity failures; an empty list means the file is sound."""
        rows = self._require().execute("PRAGMA integrity_check").fetchall()
        return [str(row[0]) for row in rows if str(row[0]).lower() != "ok"]

    def verification_report(self) -> BookVerification:
        """Return one combined physical/logical verification result."""
        return BookVerification(tuple(self.integrity_problems()), tuple(self.verify_book()))

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

        # Derived columns are query accelerators only; the JSON blob is
        # authoritative.  Detect drift so indexes can be rebuilt rather than
        # silently returning different data depending on the query path.
        derived_specs: dict[str, tuple[str, ...]] = {
            "commodity": ("mnemonic",),
            "price": ("commodity", "currency", "quote_date", "source"),
            "account": ("parent", "name", "atype"),
            "txn": ("post_date", "description"),
            "scheduled": ("name",),
            "scenario": ("name",),
            "fsa_claim": ("service_date", "provider"),
            "reconciliation": ("account", "statement_date", "status"),
        }
        defaults: dict[tuple[str, str], Any] = {
            ("commodity", "mnemonic"): "",
            ("price", "commodity"): "",
            ("price", "currency"): "",
            ("price", "quote_date"): "",
            ("price", "source"): "",
            ("account", "parent"): None,
            ("account", "name"): "",
            ("account", "atype"): "",
            ("txn", "description"): "",
            ("scheduled", "name"): "",
            ("scenario", "name"): "",
            ("fsa_claim", "service_date"): "",
            ("fsa_claim", "provider"): "",
            ("reconciliation", "account"): "",
            ("reconciliation", "statement_date"): "",
            ("reconciliation", "status"): "",
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
                    issues.append(
                        BookIssue(
                            f"{table}.handle_mismatch",
                            f"{table} row {row['handle']} contains object handle {internal!r}",
                            row["handle"],
                        )
                    )
                if internal is not None:
                    previous = internal_handles.get(internal)
                    if previous is not None and previous != row["handle"]:
                        issues.append(
                            BookIssue(
                                f"{table}.duplicate_object_handle",
                                f"{table} rows {previous} and {row['handle']} both contain "
                                f"object handle {internal}",
                                row["handle"],
                            )
                        )
                    else:
                        internal_handles[internal] = row["handle"]
                for column in columns:
                    expected_value = data.get(column, defaults.get((table, column)))
                    if table == "txn" and column == "post_date" and expected_value is not None:
                        expected_value = str(expected_value)
                    if row[column] != expected_value:
                        issues.append(
                            BookIssue(
                                f"{table}.index_mismatch",
                                f"{table} derived column {column} disagrees with blob for "
                                f"{row['handle']}",
                                row["handle"],
                            )
                        )

        for claim in self.iter_fsa_claims():
            issues.extend(self._verify_fsa_claim_references(claim))

        expected: dict[str, tuple[str, str, str, int, int, int, int]] = {}
        for transaction in self.iter_transactions():
            for split in transaction.splits:
                expected[split.handle] = (
                    transaction.handle,
                    split.account,
                    transaction.post_date.isoformat(),
                    split.value.numerator,
                    split.value.denominator,
                    split.quantity.numerator,
                    split.quantity.denominator,
                )

        actual = {
            row["handle"]: (
                row["txn"],
                row["account"],
                row["post_date"],
                row["value_num"],
                row["value_den"],
                row["quantity_num"],
                row["quantity_den"],
            )
            for row in conn.execute(
                "SELECT handle, txn, account, post_date, value_num, value_den, "
                "quantity_num, quantity_den FROM split_index"
            )
        }
        for handle in sorted(expected.keys() - actual.keys()):
            issues.append(
                BookIssue(
                    "split_index.missing", f"split {handle} is missing from split_index", handle
                )
            )
        for handle in sorted(actual.keys() - expected.keys()):
            issues.append(
                BookIssue(
                    "split_index.orphan", f"split_index contains unknown split {handle}", handle
                )
            )
        for handle in sorted(expected.keys() & actual.keys()):
            if expected[handle] != actual[handle]:
                issues.append(
                    BookIssue(
                        "split_index.mismatch",
                        f"split_index disagrees with transaction data for split {handle}",
                        handle,
                    )
                )
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

    def _verify_changes(
        self,
        records: list[tuple[str, str, dict | None, dict | None]],
        *,
        reverse: bool = False,
    ) -> list[BookIssue]:
        """Verify only objects and reverse references affected by one write batch.

        ``verify_book()`` remains the exhaustive diagnostic. Normal commits and
        undo/redo already know which rows changed, so rebuilding every transaction
        and the complete split index on each edit is unnecessary work.
        """
        states: dict[tuple[str, str], tuple[dict | None, dict | None]] = {}
        for table, handle, before, after in records:
            key = (table, handle)
            original = states.get(key, (before, before))[0]
            states[key] = (original, after)

        targets = {key: (before if reverse else after) for key, (before, after) in states.items()}
        issues: list[BookIssue] = []
        for (table, handle), data in targets.items():
            if data is None:
                issues.extend(self._verify_deleted_reference(table, handle))
                continue
            issues.extend(self._verify_changed_object(table, handle, data))
        return issues

    def _verify_changed_object(
        self, table: str, handle: str, data: dict[str, Any]
    ) -> list[BookIssue]:
        if table == "metadata":
            return []
        issues = self._verify_derived_row(table, handle, data)

        if table == "account":
            account = Account.from_dict(data)
            if account.parent is not None and self.get_account(account.parent) is None:
                issues.append(
                    BookIssue(
                        "account.missing_parent",
                        f"account {account.name!r} refers to missing parent {account.parent}",
                        handle,
                    )
                )
            if account.commodity is not None and self.get_commodity(account.commodity) is None:
                issues.append(
                    BookIssue(
                        "account.missing_commodity",
                        f"account {account.name!r} refers to missing commodity {account.commodity}",
                        handle,
                    )
                )
            if account.linked_asset is not None and self.get_account(account.linked_asset) is None:
                issues.append(
                    BookIssue(
                        "account.missing_linked_asset",
                        f"account {account.name!r} refers to missing linked asset "
                        f"{account.linked_asset}",
                        handle,
                    )
                )
            if (
                account.card_payment_account is not None
                and self.get_account(account.card_payment_account) is None
            ):
                issues.append(
                    BookIssue(
                        "account.missing_card_payment_account",
                        f"account {account.name!r} refers to missing card payment account "
                        f"{account.card_payment_account}",
                        handle,
                    )
                )
            seen: set[str] = set()
            current = account
            while current.parent is not None:
                if current.handle in seen:
                    issues.append(
                        BookIssue(
                            "account.parent_cycle",
                            f"account hierarchy contains a cycle involving {account.name!r}",
                            handle,
                        )
                    )
                    break
                seen.add(current.handle)
                parent = self.get_account(current.parent)
                if parent is None:
                    break
                current = parent

        elif table == "price":
            price = CommodityPrice.from_dict(data)
            if self.get_commodity(price.commodity) is None:
                issues.append(
                    BookIssue(
                        "price.missing_commodity",
                        f"price {handle} refers to missing commodity {price.commodity}",
                        handle,
                    )
                )
            currency = self.get_commodity(price.currency)
            if currency is None:
                issues.append(
                    BookIssue(
                        "price.missing_currency",
                        f"price {handle} refers to missing currency {price.currency}",
                        handle,
                    )
                )
            elif not currency.is_currency:
                issues.append(
                    BookIssue(
                        "price.non_currency_quote",
                        f"price {handle} quote commodity is not a currency",
                        handle,
                    )
                )

        elif table == "txn":
            transaction = Transaction.from_dict(data)
            if (
                transaction.currency is not None
                and self.get_commodity(transaction.currency) is None
            ):
                issues.append(
                    BookIssue(
                        "transaction.missing_currency",
                        f"transaction {transaction.describe()} refers to missing currency "
                        f"{transaction.currency}",
                        handle,
                    )
                )
            try:
                transaction.validate()
            except UnbalancedError as exc:
                issues.append(BookIssue("transaction.unbalanced", str(exc), handle))
            split_handles: set[str] = set()
            for transaction_split in transaction.splits:
                if transaction_split.handle in split_handles:
                    issues.append(
                        BookIssue(
                            "transaction.duplicate_split_handle",
                            f"transaction {transaction.describe()} contains duplicate split handle "
                            f"{transaction_split.handle}",
                            handle,
                        )
                    )
                split_handles.add(transaction_split.handle)
                if self.get_account(transaction_split.account) is None:
                    issues.append(
                        BookIssue(
                            "transaction.missing_account",
                            f"transaction {transaction.describe()} has a split for missing account "
                            f"{transaction_split.account}",
                            handle,
                        )
                    )
            issues.extend(self._verify_transaction_index(transaction))

        elif table == "scheduled":
            scheduled = ScheduledTransaction.from_dict(data)
            if scheduled.currency is not None and self.get_commodity(scheduled.currency) is None:
                issues.append(
                    BookIssue(
                        "scheduled.missing_currency",
                        f"scheduled transaction {scheduled.name!r} refers to missing currency "
                        f"{scheduled.currency}",
                        handle,
                    )
                )
            for scheduled_split in scheduled.splits:
                if self.get_account(scheduled_split.account) is None:
                    issues.append(
                        BookIssue(
                            "scheduled.missing_account",
                            f"scheduled transaction {scheduled.name!r} refers to missing account "
                            f"{scheduled_split.account}",
                            handle,
                        )
                    )
        elif table == "scenario":
            scenario = Scenario.from_dict(data)
            refs = set(scenario.assumptions.per_account) | set(scenario.opening_overrides)
            refs.update(item.account for item in scenario.one_offs)
            for period in scenario.assumption_periods:
                refs.update(period.per_account)
            for account_handle in sorted(refs):
                if self.get_account(account_handle) is None:
                    issues.append(
                        BookIssue(
                            "scenario.missing_account",
                            f"scenario {scenario.name!r} "
                            f"refers to missing account {account_handle}",
                            handle,
                        )
                    )

        elif table == "fsa_claim":
            issues.extend(self._verify_fsa_claim_references(FsaClaim.from_dict(data)))

        elif table == "reconciliation":
            issues.extend(self._verify_reconciliation_references(Reconciliation.from_dict(data)))

        return issues

    def _verify_reconciliation_references(self, reconciliation: Reconciliation) -> list[BookIssue]:
        issues: list[BookIssue] = []
        if self.get_account(reconciliation.account) is None:
            issues.append(
                BookIssue(
                    "reconciliation.missing_account",
                    f"reconciliation {reconciliation.handle} refers to missing account "
                    f"{reconciliation.account}",
                    reconciliation.handle,
                )
            )
            return issues
        split_accounts = {
            split.handle: split.account
            for transaction in self.iter_transactions()
            for split in transaction.splits
        }
        for split_handle in reconciliation.selected_splits:
            split_account = split_accounts.get(split_handle)
            if split_account is None:
                issues.append(
                    BookIssue(
                        "reconciliation.missing_split",
                        f"reconciliation {reconciliation.handle} refers to missing split "
                        f"{split_handle}",
                        reconciliation.handle,
                    )
                )
            elif split_account != reconciliation.account:
                issues.append(
                    BookIssue(
                        "reconciliation.wrong_account",
                        f"reconciliation {reconciliation.handle} includes split {split_handle} "
                        f"from another account",
                        reconciliation.handle,
                    )
                )
        return issues

    def _verify_fsa_claim_references(self, claim: FsaClaim) -> list[BookIssue]:
        issues: list[BookIssue] = []
        links = [*claim.payments, *claim.refunds]
        links.extend(link for allocation in claim.allocations for link in allocation.reimbursements)
        for link in links:
            transaction = self.get_transaction(link.transaction)
            if transaction is None:
                issues.append(
                    BookIssue(
                        "fsa_claim.missing_transaction",
                        f"FSA claim {claim.handle} refers to missing transaction "
                        f"{link.transaction}",
                        claim.handle,
                    )
                )
            elif not any(split.handle == link.split for split in transaction.splits):
                issues.append(
                    BookIssue(
                        "fsa_claim.missing_split",
                        f"FSA claim {claim.handle} refers to missing split {link.split}",
                        claim.handle,
                    )
                )
        for allocation in claim.allocations:
            if self.get_account(allocation.account) is None:
                issues.append(
                    BookIssue(
                        "fsa_claim.missing_account",
                        f"FSA claim {claim.handle} refers to missing account {allocation.account}",
                        claim.handle,
                    )
                )
        return issues

    def _verify_derived_row(self, table: str, handle: str, data: dict[str, Any]) -> list[BookIssue]:
        columns_by_table: dict[str, tuple[str, ...]] = {
            "commodity": ("mnemonic",),
            "price": ("commodity", "currency", "quote_date", "source"),
            "account": ("parent", "name", "atype"),
            "txn": ("post_date", "description"),
            "scheduled": ("name",),
            "scenario": ("name",),
            "fsa_claim": ("service_date", "provider"),
            "reconciliation": ("account", "statement_date", "status"),
        }
        defaults: dict[tuple[str, str], Any] = {
            ("commodity", "mnemonic"): "",
            ("price", "commodity"): "",
            ("price", "currency"): "",
            ("price", "quote_date"): "",
            ("price", "source"): "",
            ("account", "parent"): None,
            ("account", "name"): "",
            ("account", "atype"): "",
            ("txn", "description"): "",
            ("scheduled", "name"): "",
            ("scenario", "name"): "",
            ("fsa_claim", "service_date"): "",
            ("fsa_claim", "provider"): "",
            ("reconciliation", "account"): "",
            ("reconciliation", "statement_date"): "",
            ("reconciliation", "status"): "",
        }
        columns = columns_by_table[table]
        selected = ", ".join(("handle", *columns))
        row = (
            self._require()
            .execute(f"SELECT {selected} FROM {table} WHERE handle=?", (handle,))
            .fetchone()
        )
        if row is None:
            return [
                BookIssue(f"{table}.missing_row", f"{table} object {handle} was not stored", handle)
            ]
        issues: list[BookIssue] = []
        if data.get("handle") != handle:
            issues.append(
                BookIssue(
                    f"{table}.handle_mismatch",
                    f"{table} row {handle} contains object handle {data.get('handle')!r}",
                    handle,
                )
            )
        for column in columns:
            expected = data.get(column, defaults.get((table, column)))
            if table == "txn" and column == "post_date" and expected is not None:
                expected = str(expected)
            if row[column] != expected:
                issues.append(
                    BookIssue(
                        f"{table}.index_mismatch",
                        f"{table} derived column {column} disagrees with blob for {handle}",
                        handle,
                    )
                )
        return issues

    def _verify_transaction_index(self, transaction: Transaction) -> list[BookIssue]:
        expected = {
            split.handle: (
                transaction.handle,
                split.account,
                transaction.post_date.isoformat(),
                split.value.numerator,
                split.value.denominator,
                split.quantity.numerator,
                split.quantity.denominator,
            )
            for split in transaction.splits
        }
        actual = {
            row["handle"]: (
                row["txn"],
                row["account"],
                row["post_date"],
                row["value_num"],
                row["value_den"],
                row["quantity_num"],
                row["quantity_den"],
            )
            for row in self._require().execute(
                "SELECT handle, txn, account, post_date, value_num, value_den, "
                "quantity_num, quantity_den "
                "FROM split_index WHERE txn=?",
                (transaction.handle,),
            )
        }
        issues: list[BookIssue] = []
        for split_handle in sorted(expected.keys() - actual.keys()):
            issues.append(
                BookIssue(
                    "split_index.missing",
                    f"split {split_handle} is missing from split_index",
                    split_handle,
                )
            )
        for split_handle in sorted(actual.keys() - expected.keys()):
            issues.append(
                BookIssue(
                    "split_index.orphan",
                    f"split_index contains unknown split {split_handle}",
                    split_handle,
                )
            )
        for split_handle in sorted(expected.keys() & actual.keys()):
            if expected[split_handle] != actual[split_handle]:
                issues.append(
                    BookIssue(
                        "split_index.mismatch",
                        f"split_index disagrees with transaction data for split {split_handle}",
                        split_handle,
                    )
                )
        return issues

    def _verify_deleted_reference(self, table: str, handle: str) -> list[BookIssue]:
        issues: list[BookIssue] = []
        if table == "metadata":
            return issues
        if table == "account":
            for account in self._accounts.values():
                if account.parent == handle:
                    issues.append(
                        BookIssue(
                            "account.missing_parent",
                            f"account {account.name!r} refers to missing parent {handle}",
                            account.handle,
                        )
                    )
                if account.linked_asset == handle:
                    issues.append(
                        BookIssue(
                            "account.missing_linked_asset",
                            f"account {account.name!r} refers to missing linked asset {handle}",
                            account.handle,
                        )
                    )
                if account.card_payment_account == handle:
                    issues.append(
                        BookIssue(
                            "account.missing_card_payment_account",
                            f"account {account.name!r} refers to missing card payment account "
                            f"{handle}",
                            account.handle,
                        )
                    )
            for reconciliation in self.iter_reconciliations(account=handle):
                issues.append(
                    BookIssue(
                        "reconciliation.missing_account",
                        f"reconciliation {reconciliation.handle} refers to missing account "
                        f"{handle}",
                        reconciliation.handle,
                    )
                )
            row = (
                self._require()
                .execute("SELECT txn FROM split_index WHERE account=? LIMIT 1", (handle,))
                .fetchone()
            )
            if row is not None:
                issues.append(
                    BookIssue(
                        "transaction.missing_account",
                        f"transaction {row['txn']} has a split for missing account {handle}",
                        row["txn"],
                    )
                )
            for scheduled in self.iter_scheduled():
                if any(split.account == handle for split in scheduled.splits):
                    issues.append(
                        BookIssue(
                            "scheduled.missing_account",
                            f"scheduled transaction {scheduled.name!r} refers to missing account "
                            f"{handle}",
                            scheduled.handle,
                        )
                    )
            for scenario in self.iter_scenarios():
                refs = set(scenario.assumptions.per_account) | set(scenario.opening_overrides)
                refs.update(item.account for item in scenario.one_offs)
                for period in scenario.assumption_periods:
                    refs.update(period.per_account)
                if handle in refs:
                    issues.append(
                        BookIssue(
                            "scenario.missing_account",
                            f"scenario {scenario.name!r} refers to missing account {handle}",
                            scenario.handle,
                        )
                    )
            for claim in self.iter_fsa_claims():
                if any(allocation.account == handle for allocation in claim.allocations):
                    issues.append(
                        BookIssue(
                            "fsa_claim.missing_account",
                            f"FSA claim {claim.handle} refers to missing account {handle}",
                            claim.handle,
                        )
                    )

        elif table == "txn":
            existing_splits = {
                split.handle
                for transaction in self.iter_transactions()
                for split in transaction.splits
            }
            for reconciliation in self.iter_reconciliations():
                for split_handle in reconciliation.selected_splits:
                    if split_handle not in existing_splits:
                        issues.append(
                            BookIssue(
                                "reconciliation.missing_split",
                                f"reconciliation {reconciliation.handle} refers to missing "
                                f"split {split_handle}",
                                reconciliation.handle,
                            )
                        )
            for claim in self.iter_fsa_claims():
                links = [*claim.payments, *claim.refunds]
                links.extend(
                    link for allocation in claim.allocations for link in allocation.reimbursements
                )
                if any(link.transaction == handle for link in links):
                    issues.append(
                        BookIssue(
                            "fsa_claim.missing_transaction",
                            f"FSA claim {claim.handle} refers to missing transaction {handle}",
                            claim.handle,
                        )
                    )

        elif table == "commodity":
            for account in self._accounts.values():
                if account.commodity == handle:
                    issues.append(
                        BookIssue(
                            "account.missing_commodity",
                            f"account {account.name!r} refers to missing commodity {handle}",
                            account.handle,
                        )
                    )
            for transaction in self.iter_transactions():
                if transaction.currency == handle:
                    issues.append(
                        BookIssue(
                            "transaction.missing_currency",
                            f"transaction {transaction.describe()} "
                            f"refers to missing currency {handle}",
                            transaction.handle,
                        )
                    )
            for scheduled in self.iter_scheduled():
                if scheduled.currency == handle:
                    issues.append(
                        BookIssue(
                            "scheduled.missing_currency",
                            f"scheduled transaction {scheduled.name!r} refers to missing currency "
                            f"{handle}",
                            scheduled.handle,
                        )
                    )
            for price in self.iter_prices():
                if price.commodity == handle or price.currency == handle:
                    issues.append(
                        BookIssue(
                            "price.missing_commodity",
                            f"price {price.handle} refers to deleted commodity {handle}",
                            price.handle,
                        )
                    )

        return issues

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
        return self._write(scenario, txn, "scenario")

    def commit_scenario(self, scenario: Scenario, txn: DbTxn) -> None:
        self._write(scenario, txn, "scenario")

    def remove_scenario(self, handle: str, txn: DbTxn) -> None:
        self._delete("scenario", handle, txn)

    def get_scenario(self, handle: str) -> Scenario | None:
        data = self._read("scenario", handle)
        return Scenario.from_dict(data) if data else None

    def get_scenario_by_name(self, name: str) -> Scenario | None:
        row = (
            self._require()
            .execute("SELECT blob FROM scenario WHERE name=? LIMIT 1", (name,))
            .fetchone()
        )
        return Scenario.from_dict(json.loads(row["blob"])) if row else None

    def iter_scenarios(self) -> Iterator[Scenario]:
        for row in self._require().execute("SELECT handle, blob FROM scenario ORDER BY name"):
            obj = self._decode_row("scenario", row["handle"], row["blob"], Scenario)
            if obj is not None:
                yield obj

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
