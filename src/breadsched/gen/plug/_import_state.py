"""Per-book state shared by import front ends."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

__all__ = ["LAST_IMPORT_SOURCE_KEY", "remember_import_source", "remembered_import_source"]

LAST_IMPORT_SOURCE_KEY = "import.last_source"


class _MetadataStore(Protocol):
    def get_metadata(self, key: str, default: object = None) -> object: ...

    def set_metadata(self, key: str, value: object) -> None: ...


def remembered_import_source(db: _MetadataStore) -> str | None:
    """Return the last successfully imported source recorded for this book."""
    stored = db.get_metadata(LAST_IMPORT_SOURCE_KEY, None)
    if not isinstance(stored, str) or not stored.strip():
        return None
    return stored


def remember_import_source(db: _MetadataStore, source: str | Path) -> str:
    """Record one successful import without implying permission to repeat it."""
    resolved = str(Path(source).expanduser().resolve())
    db.set_metadata(LAST_IMPORT_SOURCE_KEY, resolved)
    return resolved
