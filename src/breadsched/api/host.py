"""One book host serving several books (decision 0004, phase 3).

**Experimental.** A host (``breadsched serve``, ``breadsched web``, or GTK's sharing)
may hold several books on one address. Each book keeps its own writer lock,
revision, and change stream; a request names the book it is for by id in the
``X-BreadSched-Book`` header. A host holding one book needs no header, so a client
that knows only the address keeps working.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from ..gen.services import ServiceError
from ..presentation import service_error_message
from .contract import ApiError

__all__ = ["BOOK_HEADER", "HostedBook", "choose_book", "hosted_name"]

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


def _refuse(status: int, code: str) -> ApiError:
    return ApiError(status, code, (BOOK_HEADER,), service_error_message(ServiceError(code, ())))


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
