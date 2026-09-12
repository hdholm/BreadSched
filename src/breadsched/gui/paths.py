"""Where things live on disk.

Kept apart from the application so the default book's location can be stated once
and tested without a GTK runtime.
"""

from __future__ import annotations

from pathlib import Path

from .. import APP_NAME
from ..gen.utils.user_paths import documents_directory as _documents_directory

__all__ = ["default_book_path", "documents_directory", "BOOK_SUFFIX", "READABLE_SUFFIXES"]

#: The suffix new books are given.
BOOK_SUFFIX = ".breadsched"

#: Suffixes the open dialog offers. BreadSched intentionally starts with a
#: clean on-disk format; old prototype books are not migrated.
READABLE_SUFFIXES = (BOOK_SUFFIX,)


def documents_directory() -> Path:
    """Return the platform-appropriate Documents directory."""
    return _documents_directory(home=Path.home())


def default_book_path() -> Path:
    """The one book someone gets if they never want to think about files."""
    return documents_directory() / f"{APP_NAME}{BOOK_SUFFIX}"
