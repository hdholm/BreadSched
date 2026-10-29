"""One book host serving several books (decision 0004, phase 3).

A host (``breadsched serve``, ``breadsched web``, or GTK's sharing)
may hold several books on one address. Each book keeps its own writer lock,
revision, and change stream; a request names the book it is for by id in the
``X-BreadSched-Book`` header. A host holding one book needs no header, so a client
that knows only the address keeps working.

The computer account has one host (decision 0004): ``breadsched serve`` started while
one runs *joins* it, asking it to open the book (:class:`JoinedBooks`), rather than
starting a second server on another port.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

from ..gen.db.sqlite import DbError, DbSQLite
from ..gen.services import ServiceError
from ..presentation import service_error_message
from .contract import ApiError
from .discovery import ServerRecord, publish_server, withdraw_server

__all__ = [
    "BOOK_HEADER",
    "HostServer",
    "HostedBook",
    "JoinedBooks",
    "choose_book",
    "host_refusal",
    "hosted_name",
]

#: The request header naming the book a request is for.
BOOK_HEADER = "X-BreadSched-Book"

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class HostedBook:
    """A book a host holds, as a caller allowed to use it sees it."""

    id: str
    name: str
    role: str


def hosted_name(path: str | None) -> str:
    """The name a book is shown by: its file's name without the folder or suffix."""
    if not path or path == ":memory:":
        return "Book"
    return Path(path).stem or "Book"


def _refuse(status: int, code: str, fields: tuple[str, ...] = (BOOK_HEADER,)) -> ApiError:
    return ApiError(status, code, fields, service_error_message(ServiceError(code, ())))


def host_refusal(status: int, code: str) -> ApiError:
    """A refusal of a request to change what the host serves, in the presentation
    layer's wording."""
    return _refuse(status, code, ())


def choose_book(books: Mapping[str, T], wanted: str) -> T:
    """The book ``wanted`` names, or the only one a host holds when it names none."""
    if wanted:
        chosen = books.get(wanted)
        if chosen is None:
            raise _refuse(404, "book.not_found")
        return chosen
    if len(books) == 1:
        return next(iter(books.values()))
    raise _refuse(400, "book.required")


class HostServer(Protocol):
    """What :class:`JoinedBooks` needs of the running server."""

    def add_book(self, db: DbSQLite) -> str: ...

    def remove_book(self, key: str) -> bool: ...


class JoinedBooks:
    """The books other commands asked this host to open, each until they let it go.

    The host opens the book for writing itself, so it holds the writer lock, and
    publishes the book's server record under its own process; commands on this
    computer then find the book at the host's address like any other it serves.
    """

    def __init__(self, server: HostServer, url: str) -> None:
        self._server = server
        self._url = url
        self._lock = threading.Lock()
        self._joined: dict[str, tuple[DbSQLite, ServerRecord | None]] = {}

    def open(self, path: str) -> str:
        """Serve the book at ``path``; its id. ``ApiError`` if it cannot be."""
        file = Path(path).expanduser()
        if not file.is_file():
            raise _refuse(404, "host.book.missing", ("path",))
        db = DbSQLite()
        try:
            db.load(str(file), mode="w")
        except DbError as exc:
            db.close()
            # Most often another program, or this host, already holds it.
            raise _refuse(409, "host.book.held", ("path",)) from exc
        try:
            key = self._server.add_book(db)
        except ValueError as exc:
            db.close()
            raise _refuse(409, "host.book.held", ("path",)) from exc
        lock = db.writer_lock_token
        record = publish_server(str(file), self._url, lock, book_id=key) if lock else None
        with self._lock:
            self._joined[key] = (db, record)
        return key

    def close(self, key: str) -> bool:
        """Stop serving a book opened by :meth:`open`; False for any other."""
        with self._lock:
            joined = self._joined.pop(key, None)
        if joined is None:
            return False
        db, record = joined
        if record is not None:
            withdraw_server(record)
        self._server.remove_book(key)
        db.close()
        return True

    def close_all(self) -> None:
        with self._lock:
            keys = list(self._joined)
        for key in keys:
            self.close(key)
