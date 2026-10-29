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
from .users import device_token

__all__ = ["open_book_api"]


@contextmanager
def open_book_api(
    book: str,
    mode: str = "w",
    *,
    url: str | None = None,
    token: str | None = None,
    directory: Path | None = None,
    ca_file: str | None = None,
    client_cert: str | None = None,
    client_key: str | None = None,
) -> Iterator[BookApi]:
    """The book API for ``book`` until the block ends.

    ``url`` and ``token`` name a server explicitly; otherwise a server holding the
    book on this host is used if there is one, and the book is opened in this
    process (``mode`` ``"w"`` or ``"r"``) if not. The certificate options apply to
    an ``https://`` server (see ``HttpBookClient``).
    """
    if url is not None:
        yield HttpBookClient(
            url,
            token or "",
            ca_file=ca_file,
            client_cert=client_cert,
            client_key=client_key,
        )
        return
    record = find_server(book, directory)
    if record is not None:
        # The same computer account: its device token says who it is, and lets the
        # server read files named here, as imports by path and the GnuCash write-back
        # need (#358, decision 0004).
        yield HttpBookClient(
            record.url, device_token(), host_files=True, book_id=record.book_id or None
        )
        return
    with open_book_server(book, mode) as server:
        yield server
