"""The request context every web resource adapter receives: the open book.

Each adapter in ``web/*_resource.py`` takes this context and the request, parses
it, calls the shared services and engines, and returns plain data;
``web.resources`` maps routes to those adapters and ``web.transport`` owns HTTP.
Nothing financial lives here.
"""

from __future__ import annotations

from ..api import LocalBookServer
from ..gen.db.sqlite import DbSQLite

__all__ = ["Api", "api"]


class Api:
    """The open book a request works on, and its in-process book server (#356)."""

    def __init__(self, db: DbSQLite) -> None:
        self.db = db
        self._book: LocalBookServer | None = None

    @property
    def book(self) -> LocalBookServer:
        """The book server over :attr:`db`; adapters moved onto it use only this."""
        if self._book is None:
            # Requests come from other processes, so documents stay confined (#358).
            self._book = LocalBookServer(self.db, confine_documents=True)
        return self._book


def api(db: DbSQLite) -> Api:
    return Api(db)
