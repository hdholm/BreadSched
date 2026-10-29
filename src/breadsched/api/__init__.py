"""The book server: one API that owns an open book for every interface (#354).

A *book server* owns one open book: its storage, undo history, and change
notifications. The command line, the browser, and GTK reach a book only
through the :class:`~breadsched.api.protocol.BookApi` protocol, so every interface
reads the same results, writes through the same validation, and serializes the same
JSON. :class:`~breadsched.api.local.LocalBookServer` implements the protocol in
process over :mod:`breadsched.gen.services`; an HTTP server and client implement
the same protocol (#357).

* :mod:`~breadsched.api.contract` – the error envelope and leaf JSON encoding.
* :mod:`~breadsched.api.protocol` – what a book server offers, and its change events.
* :mod:`~breadsched.api.local` – the in-process server.
* :mod:`~breadsched.api.views` – the one JSON form of each response.
* :mod:`~breadsched.api.http_client` – the same protocol over ``/api/v1``.
* :mod:`~breadsched.api.discovery` and :mod:`~breadsched.api.connect` – finding and
  using the server that already holds a book.
"""

from __future__ import annotations

from .contract import ApiError, to_json, unwrap
from .local import LocalBookServer, open_book_server
from .protocol import BookApi, Change

__all__ = [
    "ApiError",
    "BookApi",
    "Change",
    "LocalBookServer",
    "open_book_server",
    "to_json",
    "unwrap",
]
