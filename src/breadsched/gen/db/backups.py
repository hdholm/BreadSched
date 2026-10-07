"""Consistent copies of a native book: backups, restores, and read snapshots.

A backup goes through SQLite's backup API, so committed pages still in the WAL are
included; it is assembled under a temporary name and installed atomically only
after its integrity check passes. A restore verifies the backup, holds the
destination's writer lock throughout, preserves any existing book as
``.pre-restore.bak`` first, and verifies the restored copy before installing it.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

from ..utils.user_paths import companion_path
from .base import DbError
from .book_lock import BookWriterLock
from .verification import BookVerification

__all__ = [
    "backup_connection",
    "migration_backup_path",
    "remove_sqlite_sidecars",
    "restore_backup",
    "snapshot_of",
]


def migration_backup_path(book: str | Path, version: int) -> Path:
    """Where the verified backup made before migrating ``book`` from ``version`` goes.

    Beside the book, or in BreadSched's data folder when the book was reached
    through the document portal and nothing can be written beside it.
    """
    return companion_path(book, f".pre-migration-v{version}.bak")


def snapshot_of(source: sqlite3.Connection) -> sqlite3.Connection:
    """An in-memory copy of ``source``'s one committed generation; closes ``source``.

    SQLite's backup copies every page under one shared lock (restarting if the
    writer commits meanwhile), so the copy never mixes generations. The copy is
    detached from the file: later commits are invisible to it and it never holds
    up the writer, at the cost of the book's size in memory.
    """
    snapshot = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        source.backup(snapshot)
        snapshot.execute("PRAGMA query_only=ON")
    except Exception:
        snapshot.close()
        raise
    finally:
        source.close()
    return snapshot


def remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def backup_connection(
    source: sqlite3.Connection,
    destination: str,
    source_path: str | None,
    *,
    overwrite: bool = False,
) -> str:
    """Write a consistent, sidecar-free backup of the book open on ``source``.

    SQLite's backup API includes committed pages that still live in the WAL,
    unlike copying only the main database file.  The destination is assembled
    under a temporary name and atomically installed only after its SQLite
    integrity check succeeds.
    """
    target = Path(destination)
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
    remove_sqlite_sidecars(target)
    os.replace(temporary, target)
    return str(target)


def restore_backup(
    source: str,
    destination: str,
    *,
    overwrite: bool,
    verify: Callable[[str], BookVerification],
    preserve: Callable[[Path, Path], None],
) -> str:
    """Restore a verified BreadSched backup to ``destination``.

    Restores never overwrite an existing book silently.  When overwrite is
    explicitly requested, ``preserve`` first writes a consistent
    ``.pre-restore.bak`` copy of the old book so a mistaken restore remains
    reversible. ``verify`` checks a book file physically and logically.
    """
    source_path = Path(source)
    target = Path(destination)
    if not source_path.exists():
        raise DbError(f"backup does not exist: {source}")
    if source_path.resolve() == target.resolve():
        raise DbError("backup source and restore destination must differ")
    if target.exists() and not overwrite:
        raise DbError(f"restore destination already exists: {target}")

    report = verify(str(source_path))
    if report.sqlite:
        raise DbError("backup failed SQLite integrity check: " + "; ".join(report.sqlite))
    if report.issues:
        first = report.issues[0]
        raise DbError(f"backup failed logical verification: {first.code}: {first.message}")

    # Hold the ordinary writer lock for the destination throughout preservation
    # and replacement. Replacing a pathname beneath a live SQLite connection
    # can split two writers across different inodes and corrupt either copy.
    target.parent.mkdir(parents=True, exist_ok=True)
    guard = BookWriterLock.acquire(str(target))
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
            preserve(target, companion_path(target, ".pre-restore.bak"))

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
            copied = verify(str(temporary))
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        if not copied.ok:
            temporary.unlink(missing_ok=True)
            raise DbError("restored copy failed verification before installation")
        remove_sqlite_sidecars(target)
        os.replace(temporary, target)
    finally:
        if guard is not None:
            guard.release()
    return str(target)
