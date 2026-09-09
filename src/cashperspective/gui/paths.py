"""Where things live on disk.

Kept apart from the application so the default book's location can be stated once
and tested without a GTK runtime.
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import APP_NAME

__all__ = ["default_book_path", "documents_directory", "BOOK_SUFFIX", "READABLE_SUFFIXES"]

#: The suffix new books are given.
BOOK_SUFFIX = ".breadsched"

#: Suffixes the open dialog offers. BreadSched intentionally starts with a
#: clean on-disk format; old prototype books are not migrated.
READABLE_SUFFIXES = (BOOK_SUFFIX,)


def documents_directory() -> Path:
    """The user's documents folder, falling back to home.

    ``XDG_DOCUMENTS_DIR`` is honoured where it is set, so a localised or
    relocated Documents folder is respected rather than assumed to be in English
    and directly under home.
    """
    configured = os.environ.get("XDG_DOCUMENTS_DIR")
    if configured:
        return Path(configured).expanduser()
    documents = Path.home() / "Documents"
    return documents if documents.is_dir() else Path.home()


def default_book_path() -> Path:
    """The one book someone gets if they never want to think about files."""
    return documents_directory() / f"{APP_NAME}{BOOK_SUFFIX}"
