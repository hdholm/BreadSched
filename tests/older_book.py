"""A native book as the oldest schema that still opens left it."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from breadsched.gen.db.migrations import MIN_SUPPORTED_SCHEMA_VERSION
from breadsched.gen.db.sqlite import DbSQLite

OLDEST = MIN_SUPPORTED_SCHEMA_VERSION


def make_oldest_book(path: Path) -> Path:
    """Create an empty book at ``path`` marked as the oldest supported schema."""
    db = DbSQLite()
    db.load(str(path))
    db.close()
    with sqlite3.connect(path) as raw:
        raw.execute("UPDATE metadata SET value=? WHERE key='schema_version'", (str(OLDEST),))
        raw.execute("DELETE FROM schema_migration")
        raw.execute(
            "INSERT INTO schema_migration(version, applied_at) VALUES (?, 'test')", (OLDEST,)
        )
    return path
