"""The request context every web resource adapter receives: the open book.

Each adapter in ``web/*_resource.py`` takes this context and the request, parses
it, calls the shared services and engines, and returns plain data;
``web.resources`` maps routes to those adapters and ``web.transport`` owns HTTP.
Nothing financial lives here.
"""

from __future__ import annotations

from ..gen.db.sqlite import DbSQLite

__all__ = ["Api", "api"]


class Api:
    """The open book a request works on."""

    def __init__(self, db: DbSQLite) -> None:
        self.db = db


def api(db: DbSQLite) -> Api:
    return Api(db)
