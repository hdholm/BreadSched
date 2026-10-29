"""A book another process holds, used from GTK through its server (decision 0004).

When the computer account's book host (``breadsched serve``, or
another GTK window's sharing) already holds a book, GTK cannot open the file for
writing: the host has its writer lock. Instead it opens a :class:`RemoteBook`, the
same book reached over the host's loopback address as the computer account's own
user (its device token), without a prompt. Every view and dialog already reaches the
book only through ``book_server(db)``, so they work unchanged: ``book_server`` gives
them :class:`MainLoopBook`, the host's book API with its change notices delivered on
GTK's main loop, as the in-process server's are.

:data:`OpenBook` is what GTK holds as its open book: the file itself, or this.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

from ..api.discovery import ServerRecord, find_server
from ..api.http_client import HttpBookClient
from ..api.protocol import BookApi, Change, Listener
from ..api.users import device_token
from ..gen.db.sqlite import DbSQLite
from ..gen.utils.logs import get_logger
from .gi_setup import GLib

__all__ = [
    "MainLoopBook",
    "OpenBook",
    "RemoteBook",
    "book_snapshot",
    "open_held_book",
    "worker_book",
]

LOG = get_logger(__name__)

#: Seconds a request to the host may take: a projection or Plan of a large book is
#: calculated there before it answers.
HOST_TIMEOUT = 600.0


class MainLoopBook:
    """A book server elsewhere, its change notices delivered on GTK's main loop.

    One change stream from the host serves every view: each ``subscribe`` adds a
    listener, called on the main loop in the order the changes happened.
    """

    def __init__(self, client: HttpBookClient) -> None:
        self.client = client
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()
        self._stop: Callable[[], None] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)

    @property
    def revision(self) -> int:
        return self.client.revision

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)
            if self._stop is None:
                self._stop = self.client.subscribe(self._arrived)

        def stop() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return stop

    def _arrived(self, change: Change) -> None:
        # The stream's thread: hand the change to the main loop.
        GLib.idle_add(self._deliver, change)

    def _deliver(self, change: Change) -> bool:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(change)
            except Exception:  # noqa: BLE001 - one view's failure must not stop the rest
                LOG.exception("a view could not follow a change to the shared book")
        return GLib.SOURCE_REMOVE

    def close(self) -> None:
        with self._lock:
            stop, self._stop = self._stop, None
            self._listeners.clear()
        if stop is not None:
            stop()


class RemoteBook:
    """The book at ``path``, held by the host at ``record``, used as this account."""

    #: An upgrade's backup belongs to the process that opened the file.
    migration_backup = None

    def __init__(self, path: str, record: ServerRecord, client: HttpBookClient) -> None:
        self.path = path
        self.record = record
        self._stream = MainLoopBook(client)
        #: What ``book_server`` gives every view and dialog for this book.
        self.book_api: BookApi = cast(BookApi, self._stream)
        self.is_open = True

    def revision(self) -> int:
        return self.book_api.revision

    def undo(self) -> bool:
        return self.book_api.undo()

    def redo(self) -> bool:
        return self.book_api.redo()

    def close(self) -> None:
        if self.is_open:
            self.is_open = False
            self._stream.close()


#: The book GTK has open: the file, or a book another process holds.
OpenBook = DbSQLite | RemoteBook


@contextmanager
def worker_book(book: OpenBook) -> Iterator[BookApi]:
    """A book API a worker thread may use for a long read: the host's, for a book
    another process holds (it reads its own snapshot); else a read-only snapshot of
    the file, closed afterwards; or, for a book with no file, the book itself."""
    from ..api.local import LocalBookServer, book_server

    if isinstance(book, RemoteBook):
        yield book.book_api
        return
    path = book.path
    if not path or path == ":memory:":
        yield book_server(book)
        return
    snapshot = DbSQLite()
    snapshot.load(path, "r")
    server = LocalBookServer(snapshot)
    try:
        yield server
    finally:
        server.close()
        snapshot.close()


@contextmanager
def book_snapshot(book: OpenBook) -> Iterator[DbSQLite]:
    """The book's file for an export or a backup: the open file itself, or, for a book
    another process holds, a read-only snapshot of its file, closed afterwards."""
    if isinstance(book, DbSQLite):
        yield book
        return
    snapshot = DbSQLite()
    snapshot.load(book.path, "r")
    try:
        yield snapshot
    finally:
        snapshot.close()


def open_held_book(path: str) -> RemoteBook | None:
    """The book at ``path`` through the account's host holding it, if one does."""
    record = find_server(path)
    if record is None:
        return None
    client = HttpBookClient(
        record.url,
        device_token(),
        timeout=HOST_TIMEOUT,
        host_files=True,
        book_id=record.book_id or None,
    )
    return RemoteBook(path, record, client)
