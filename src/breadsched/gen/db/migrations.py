"""Versioned migrations for the native SQLite book format.

Migrations are deliberately small, ordered functions.  The database backend owns
transaction boundaries, backups and integrity checks; a migration owns only the SQL
needed to move one schema version to the next.  This keeps historical migrations
immutable and makes every supported upgrade path independently testable.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

Migration = Callable[[sqlite3.Connection], None]


def v1_to_v2(conn: sqlite3.Connection) -> None:
    """Add an auditable record of schema changes.

    Version 1 already stores authoritative objects as JSON blobs.  Version 2 does
    not alter those objects; it establishes migration history before future schema
    versions need to transform financial data.
    """
    conn.execute(
        """
        CREATE TABLE schema_migration (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


MIGRATIONS: dict[int, Migration] = {
    1: v1_to_v2,
}

LATEST_SCHEMA_VERSION = max(MIGRATIONS) + 1

__all__ = ["LATEST_SCHEMA_VERSION", "MIGRATIONS", "Migration"]
