"""The request context every web resource adapter receives: the open book.

Each adapter in ``web/*_resource.py`` takes this context and the request, parses
it, calls the shared services and engines, and returns plain data;
``web.resources`` maps routes to those adapters and ``web.transport`` owns HTTP.
Nothing financial lives here.
"""

from __future__ import annotations

from typing import cast

from ..api import LocalBookServer
from ..api.permissions import GuardedBook
from ..gen.db.sqlite import DbSQLite

__all__ = ["Api", "api"]


class Api:
    """The open book a request works on, and its in-process book server (#356)."""

    def __init__(self, db: DbSQLite) -> None:
        self.db = db
        self._server: LocalBookServer | None = None
        self._book: LocalBookServer | None = None

    @property
    def server(self) -> LocalBookServer:
        """The book server itself, unguarded: for the transport's own bookkeeping."""
        if self._server is None:
            # Requests come from other processes, so documents stay confined (#358).
            self._server = LocalBookServer(self.db, confine_documents=True)
        return self._server

    @property
    def book(self) -> LocalBookServer:
        """The book server over :attr:`db`; adapters moved onto it use only this. Every
        operation is checked against the role of the user asking (decision 0004)."""
        if self._book is None:
            self._book = cast(LocalBookServer, GuardedBook(self.server))
        return self._book


def api(db: DbSQLite) -> Api:
    return Api(db)
