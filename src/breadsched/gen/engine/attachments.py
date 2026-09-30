"""Linked documents: where they live, and which ones are missing.

Like GnuCash, BreadSched keeps attachments outside the book. A transaction lists
locations; the files themselves stay in an attachment folder beside the book (or
anywhere the household chooses). A location is one of:

- a path relative to the attachment folder (``receipts/2026-09-roof.pdf``), which
  keeps working when the book and its folder move together;
- an absolute path or ``file:`` URI, for a document kept elsewhere;
- a web address (``https://…``), which BreadSched never tries to fetch.

A file can go missing at any time (moved, renamed, on a disconnected drive). That
is reported, never treated as damage to the book: the location is kept so the
file can be restored or the link pointed somewhere else.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..db.sqlite import DbSQLite
from ..lib.transaction import Transaction
from ..utils.user_paths import portal_document_id

__all__ = [
    "AttachmentStatus",
    "FOLDER_KEY",
    "SOURCE_FOLDER_KEY",
    "source_link_folder",
    "attachment_folder",
    "copy_into_folder",
    "is_web_address",
    "location_for",
    "resolve",
    "statuses",
]

#: Book metadata key holding the attachment folder, absolute or relative to the book.
FOLDER_KEY = "attachment_folder"
#: Where relative links imported from GnuCash resolve: GnuCash's "Path head for
#: linked files" preference. The book and GnuCash share the file system, so the
#: link is kept as written and the file is never copied.
SOURCE_FOLDER_KEY = "gnucash_linked_files_folder"


def source_link_folder(db: DbSQLite) -> Path:
    """GnuCash's linked-files folder as configured here; the home folder by default."""
    configured = db.get_metadata(SOURCE_FOLDER_KEY, None)
    return Path(str(configured)).expanduser() if configured else Path.home()


@dataclass(frozen=True, slots=True)
class AttachmentStatus:
    """One linked document and whether it can be found."""

    transaction: str
    location: str
    #: ``file`` or ``web``; a web address is never checked.
    kind: str
    #: Where a file location resolves to; ``None`` for a web address, or for a
    #: relative location when the book has no folder (an in-memory book).
    path: Path | None
    #: ``None`` for a web address.
    present: bool | None
    #: ``breadsched`` for the book's own attachments, ``source`` for the link
    #: imported from GnuCash.
    owner: str = "breadsched"

    @property
    def missing(self) -> bool:
        return self.present is False


def _default_folder(book: Path) -> Path:
    return book.with_name(f"{book.stem} attachments")


def attachment_folder(db: DbSQLite) -> Path | None:
    """The folder relative locations resolve against.

    ``None`` without a book file, or for a document-portal book with no folder chosen.
    """
    path = db.path
    if not path or path == ":memory:":
        configured = db.get_metadata(FOLDER_KEY, None)
        return Path(configured) if configured and Path(configured).is_absolute() else None
    book = Path(path)
    configured = db.get_metadata(FOLDER_KEY, None)
    if not configured:
        # A document-portal book is only the one file: no folder beside it can be
        # created or read, so attachments need a folder the user chooses.
        return None if portal_document_id(book) is not None else _default_folder(book)
    folder = Path(str(configured)).expanduser()
    return folder if folder.is_absolute() else book.parent / folder


def is_web_address(location: str) -> bool:
    return urlparse(location).scheme.lower() in {"http", "https"}


def resolve(db: DbSQLite, location: str, folder: Path | None = None) -> Path | None:
    """The file a location names, or ``None`` for a web address or unknown folder."""
    if is_web_address(location):
        return None
    parsed = urlparse(location)
    if parsed.scheme.lower() == "file":
        raw = unquote(parsed.path)
        # ``file:///C:/x`` on Windows parses to ``/C:/x``.
        if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
            raw = raw[1:]
        return Path(raw)
    candidate = Path(location).expanduser()
    if candidate.is_absolute() or (len(location) > 1 and location[1] == ":"):
        return candidate
    base = folder if folder is not None else attachment_folder(db)
    return base / candidate if base is not None else None


def location_for(db: DbSQLite, path: Path) -> str:
    """How to record a chosen file: relative inside the attachment folder, else absolute."""
    path = path.resolve()
    folder = attachment_folder(db)
    if folder is not None:
        try:
            return path.relative_to(folder.resolve()).as_posix()
        except ValueError:
            pass
    return str(path)


def _status(
    db: DbSQLite, transaction: str, location: str, owner: str, folder: Path | None
) -> AttachmentStatus:
    if is_web_address(location):
        return AttachmentStatus(transaction, location, "web", None, None, owner)
    path = resolve(db, location, folder)
    return AttachmentStatus(
        transaction, location, "file", path, path is not None and path.is_file(), owner
    )


def statuses(db: DbSQLite, transaction: Transaction) -> list[AttachmentStatus]:
    """Every linked document of one transaction, the source link last."""
    folder = attachment_folder(db)
    found = [
        _status(db, transaction.handle, location, "breadsched", folder)
        for location in transaction.attachments
    ]
    if transaction.source_link:
        found.append(
            _status(
                db, transaction.handle, transaction.source_link, "source", source_link_folder(db)
            )
        )
    return found


def copy_into_folder(db: DbSQLite, source: Path) -> str:
    """Copy a file into the attachment folder and return its relative location.

    An existing file of the same name is never overwritten; the copy gets a
    numbered name instead.
    """
    folder = attachment_folder(db)
    if folder is None:
        raise ValueError("the book has no attachment folder")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / source.name
    counter = 2
    while target.exists():
        target = folder / f"{source.stem}-{counter}{source.suffix}"
        counter += 1
    shutil.copy2(source, target)
    return target.name
