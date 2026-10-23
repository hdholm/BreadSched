"""Static contract: the in-process book server implements the book API (#355)."""

from breadsched.api import BookApi, LocalBookServer
from breadsched.gen.db.sqlite import DbSQLite


def serve(db: DbSQLite) -> BookApi:
    return LocalBookServer(db)


def connect(url: str, token: str) -> BookApi:
    from breadsched.api.http_client import HttpBookClient

    return HttpBookClient(url, token)
