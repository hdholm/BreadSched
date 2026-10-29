"""Finding the book server that already holds a book (#358).

**Experimental.** A server started for a writable book (``breadsched serve`` or
``breadsched web``) publishes a *server record*: the book, the server's address, the
id requests name the book by there (a host may serve several, decision 0004), and
the process that holds the book's writer lock. It carries no token: a program of the
same computer account proves who it is with that account's device token
(``api.users``, decision 0004). Another interface opening
the same book reads the record and connects to that server instead of failing on
the lock.

The record lives in the user's runtime directory, not beside the book, so where an
account's server listens is never written to a folder the book may share with other
people or a sync service: one user-only (``0600``) file per book, named by a hash of the book's
canonical path, in a user-only (``0700``) directory. A record names the writer
lock its server holds by the lock's random token, and is trusted only while the
book's lock file still carries that token (and, on this host, while the process is
alive); any other record is stale and is removed. The token, not the host name,
identifies the holder, since a host name can change while a server runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path

from ..gen.db.book_lock import BookWriterLock
from ..gen.utils.user_paths import runtime_directory

__all__ = ["ServerRecord", "find_server", "publish_server", "record_path", "withdraw_server"]


@dataclass(frozen=True, slots=True)
class ServerRecord:
    """A running book server for one book."""

    book: str
    url: str
    pid: int
    host: str
    #: The token of the writer lock the server holds.
    lock: str
    #: The id a request names the book by, on a host serving several (decision 0004).
    book_id: str = ""


def _canonical(book: str) -> str:
    return str(Path(book).expanduser().resolve())


def record_path(book: str, directory: Path | None = None) -> Path:
    """Where the server record for ``book`` is kept."""
    root = runtime_directory() / "servers" if directory is None else directory
    digest = hashlib.sha256(_canonical(book).encode("utf-8")).hexdigest()[:32]
    return root / f"{digest}.json"


def publish_server(
    book: str, url: str, lock: str, directory: Path | None = None, *, book_id: str = ""
) -> ServerRecord:
    """Record that this process, holding ``book``'s writer lock ``lock``, serves it at
    ``url``, where requests name it ``book_id``."""
    record = ServerRecord(_canonical(book), url, os.getpid(), socket.gethostname(), lock, book_id)
    path = record_path(book, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "book": record.book,
                "url": record.url,
                "pid": record.pid,
                "host": record.host,
                "lock": record.lock,
                "book_id": record.book_id,
            },
            handle,
            sort_keys=True,
        )
    os.replace(temporary, path)
    return record


def withdraw_server(record: ServerRecord, directory: Path | None = None) -> None:
    """Remove ``record`` if it is still the published one."""
    path = record_path(record.book, directory)
    current = _read(path)
    if current is not None and (current.pid, current.url) == (record.pid, record.url):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _read(path: Path) -> ServerRecord | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ServerRecord(
            str(data["book"]),
            str(data["url"]),
            int(data["pid"]),
            str(data["host"]),
            str(data["lock"]),
            str(data.get("book_id", "")),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _holds_lock(record: ServerRecord) -> bool:
    try:
        lock = json.loads(BookWriterLock.lock_path(record.book).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return bool(record.lock) and lock.get("token") == record.lock


def find_server(book: str, directory: Path | None = None) -> ServerRecord | None:
    """The live server holding ``book`` on this host, or None (removing a stale record)."""
    path = record_path(book, directory)
    record = _read(path)
    if record is None:
        return None
    here = record.host == socket.gethostname()
    live = (
        record.book == _canonical(book)
        and _holds_lock(record)
        and (not here or BookWriterLock.pid_is_alive(record.pid))
    )
    if live:
        return record
    if record.book == _canonical(book):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return None
