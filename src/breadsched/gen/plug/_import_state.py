"""Per-book state shared by import front ends."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

__all__ = [
    "LAST_IMPORT_SOURCE_KEY",
    "ImportLimit",
    "current_import_limit",
    "last_import_date",
    "limited_import",
    "remember_import_source",
    "remembered_import_source",
]

LAST_IMPORT_SOURCE_KEY = "import.last_source"
#: Source path -> ISO date of its last successful import.
IMPORT_DATES_KEY = "import.dates"


@dataclass(frozen=True, slots=True)
class ImportLimit:
    """Which of a source's transactions an import takes.

    A transaction is taken when its date is from ``since`` through ``until`` (each
    open when ``None``) and, when ``accounts`` is not empty, at least one of its
    splits posts to one of those accounts. Everything else in the source (accounts,
    commodities, prices, schedules) is always imported.
    """

    since: date | None = None
    until: date | None = None
    #: Handles of book accounts; empty for every account.
    accounts: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return self.since is not None or self.until is not None or bool(self.accounts)

    def takes(self, when: date, accounts: Collection[str]) -> bool:
        if self.since is not None and when < self.since:
            return False
        if self.until is not None and when > self.until:
            return False
        return not self.accounts or any(handle in self.accounts for handle in accounts)

    def describe(self) -> str:
        """The limit in words, for an import's summary: ``dated 2026-11-01 and later``."""
        if self.since is not None and self.until is not None:
            dates = f"dated {self.since.isoformat()} through {self.until.isoformat()}"
        elif self.since is not None:
            dates = f"dated {self.since.isoformat()} and later"
        elif self.until is not None:
            dates = f"dated through {self.until.isoformat()}"
        else:
            dates = ""
        count = len(self.accounts)
        chosen = f"in {count} chosen account{'' if count == 1 else 's'}" if count else ""
        return " ".join(part for part in (dates, chosen) if part)


_LIMIT: ContextVar[ImportLimit | None] = ContextVar("breadsched_import_limit", default=None)


def current_import_limit() -> ImportLimit | None:
    """The limit the running import was asked to keep to, if any."""
    return _LIMIT.get()


@contextmanager
def limited_import(limit: ImportLimit | None) -> Iterator[None]:
    """Run one import under ``limit``; every importer's sink reads it."""
    token = _LIMIT.set(limit if limit else None)
    try:
        yield
    finally:
        _LIMIT.reset(token)


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
    stored = db.get_metadata(IMPORT_DATES_KEY, None)
    dates = dict(stored) if isinstance(stored, dict) else {}
    dates[resolved] = date.today().isoformat()
    # An uploaded statement arrives under a new temporary path each time, so the
    # file's name stands in for it.
    dates[f"name:{Path(resolved).name}"] = dates[resolved]
    db.set_metadata(IMPORT_DATES_KEY, dates)
    return resolved


def last_import_date(db: _MetadataStore, source: str | Path) -> date | None:
    """The day ``source`` was last imported into this book, if it ever was."""
    stored = db.get_metadata(IMPORT_DATES_KEY, None)
    if not isinstance(stored, dict):
        return None
    try:
        resolved = str(Path(source).expanduser().resolve())
        found = stored.get(resolved) or stored.get(f"name:{Path(resolved).name}")
        return date.fromisoformat(str(found))
    except (OSError, ValueError):
        return None
