"""Browser-selected import files, retained under a stable per-book source path."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from ..gen.db.sqlite import DbSQLite
from .resources import ResourceError
from .server import Api


def import_upload(
    api: Api,
    db: DbSQLite,
    *,
    filename: str,
    content: bytes,
    number_format: str = "auto",
    date_format: str = "auto",
) -> dict:
    """Import a selected file; reuse its path for later uploads of the same name."""
    if (
        not filename
        or filename in {".", ".."}
        or len(filename.encode("utf-8")) > 255
        or any(c in filename for c in "/\\\0")
    ):
        raise ResourceError(400, "import.filename.invalid", ("filename",))
    if not content:
        raise ResourceError(400, "import.file.empty", ("file",))
    if db.path is None or db.path == ":memory:":
        raise ResourceError(400, "import.upload.book_required", ("file",))

    book = Path(db.path).resolve()
    # Keep the extension for importer detection; the digest prevents filename
    # traversal and keeps a stable identity for the same browser-selected name.
    suffix = Path(filename).suffix.lower()
    if suffix not in {".qif", ".ofx", ".qfx", ".gnucash", ".xml", ".sqlite", ".db"}:
        raise ResourceError(400, "import.format.unrecognized", ("filename",))
    directory = book.parent / f"{book.name}.uploads"
    directory.mkdir(mode=0o700, exist_ok=True)
    name = hashlib.sha256(filename.encode("utf-8")).hexdigest() + suffix
    target = directory / name
    if target.is_symlink():
        raise ResourceError(400, "import.filename.invalid", ("filename",))
    previous = target.read_bytes() if target.exists() else None
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix="upload-", delete=False) as tmp:
            staged = Path(tmp.name)
            tmp.write(content)
        os.replace(staged, target)
        staged = None
        try:
            return api.import_local(
                {
                    "path": str(target),
                    "include_scheduled": True,
                    "number_format": number_format,
                    "date_format": date_format,
                }
            )
        except Exception:
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                with tempfile.NamedTemporaryFile(
                    dir=directory, prefix="restore-", delete=False
                ) as tmp:
                    staged = Path(tmp.name)
                    tmp.write(previous)
                os.replace(staged, target)
                staged = None
            raise
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
