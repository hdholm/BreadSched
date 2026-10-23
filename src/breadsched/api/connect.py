"""Reaching a book through whichever book server holds it (#358).

**Experimental.** :func:`open_book_api` gives a caller the book API for a book:
an explicitly named server, else the server already holding the book (found by
:mod:`breadsched.api.discovery`), else an in-process server over the book opened
here. The caller cannot tell which it has.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .discovery import find_server
from .http_client import HttpBookClient
from .local import open_book_server
from .protocol import BookApi

__all__ = ["open_book_api"]


@contextmanager
def open_book_api(
    book: str,
    mode: str = "w",
    *,
    url: str | None = None,
    token: str | None = None,
    directory: Path | None = None,
) -> Iterator[BookApi]:
    """The book API for ``book`` until the block ends.

    ``url`` and ``token`` name a server explicitly; otherwise a server holding the
    book on this host is used if there is one, and the book is opened in this
    process (``mode`` ``"w"`` or ``"r"``) if not.
    """
    if url is not None:
        yield HttpBookClient(url, token or "")
        return
    record = find_server(book, directory)
    if record is not None:
        yield HttpBookClient(record.url, record.token)
        return
    with open_book_server(book, mode) as server:
        yield server
