"""Files a client sends as bytes, kept where the book server may read them (#358).

A client that is not on the serving computer cannot name a file there: it sends
the file's bytes instead. An import file is kept beside the book, in the
``.uploads`` folder (under one name per file name, so sending the same statement
again refreshes it), where the importers read it; a document is copied into the
attachment folder through a private staging folder. The book server lets a remote
client name only files in the uploads folder.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..db.sqlite import DbSQLite
from ..lib.transaction import Transaction
from ..utils.user_paths import companion_path
from .attachments import attach_file
from .contracts import ServiceError, ServiceResult

__all__ = [
    "IMPORT_SUFFIXES",
    "StoredUpload",
    "attach_content",
    "in_uploads",
    "store_import_upload",
    "upload_directory",
]

#: The extensions an import file may have; the importers detect the format by them.
IMPORT_SUFFIXES = frozenset({".qif", ".ofx", ".qfx", ".gnucash", ".xml", ".sqlite", ".db", ".csv"})


def _ordinary(filename: str) -> bool:
    return not (
        not filename
        or filename in {".", ".."}
        or len(filename.encode("utf-8")) > 255
        or any(c in filename for c in "/\\\0")
    )


def upload_directory(db: DbSQLite) -> Path | None:
    """The book's uploads folder (beside it, or in the data folder for a
    document-portal book), or None for a book held only in memory."""
    if db.path is None or db.path == ":memory:":
        return None
    return companion_path(Path(db.path).resolve(), ".uploads")


def in_uploads(db: DbSQLite, path: str) -> bool:
    """Whether ``path`` is a file in the book's uploads folder (links resolved)."""
    directory = upload_directory(db)
    if directory is None or not path:
        return False
    root = os.path.realpath(directory)
    target = os.path.realpath(path)
    return target.startswith(root + os.sep) and os.path.isfile(target)


@dataclass(frozen=True, slots=True)
class StoredUpload:
    """An import file kept in the uploads folder, and what it replaced."""

    path: Path
    previous: bytes | None

    def undo(self) -> None:
        """Put back what the upload replaced (an import of it was refused)."""
        if self.previous is None:
            self.path.unlink(missing_ok=True)
        else:
            _replace(self.path, self.previous)


def _replace(target: Path, content: bytes) -> None:
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix="upload-", delete=False) as tmp:
            staged = Path(tmp.name)
            tmp.write(content)
        os.replace(staged, target)
        staged = None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def store_import_upload(db: DbSQLite, filename: str, content: bytes) -> ServiceResult[StoredUpload]:
    """Keep an import file sent as bytes; the same file name replaces its last copy."""
    if not _ordinary(filename):
        return ServiceResult.failure(ServiceError("import.filename.invalid", ("filename",)))
    if not content:
        return ServiceResult.failure(ServiceError("import.file.empty", ("file",)))
    directory = upload_directory(db)
    if directory is None:
        return ServiceResult.failure(ServiceError("import.upload.book_required", ("file",)))
    # Keep the extension for importer detection; the digest prevents filename
    # traversal and keeps a stable identity for the same file name.
    suffix = Path(filename).suffix.lower()
    if suffix not in IMPORT_SUFFIXES:
        return ServiceResult.failure(ServiceError("import.format.unrecognized", ("filename",)))
    directory.mkdir(mode=0o700, exist_ok=True)
    target = directory / (hashlib.sha256(filename.encode("utf-8")).hexdigest() + suffix)
    if target.is_symlink():
        return ServiceResult.failure(ServiceError("import.filename.invalid", ("filename",)))
    previous = target.read_bytes() if target.exists() else None
    _replace(target, content)
    return ServiceResult.success(StoredUpload(target, previous))


def attach_content(
    db: DbSQLite, handle: str, filename: str, content: bytes
) -> ServiceResult[Transaction]:
    """Copy a document sent as bytes into the attachment folder and link it."""
    if not _ordinary(filename):
        return ServiceResult.failure(ServiceError("attachment.filename.invalid", ("filename",)))
    if not content:
        return ServiceResult.failure(ServiceError("attachment.file.empty", ("file",)))
    with tempfile.TemporaryDirectory(prefix="breadsched-attachment-") as staging:
        staged = Path(staging) / filename
        staged.write_bytes(content)
        return attach_file(db, handle, staged)
