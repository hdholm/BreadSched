"""Versioned migrations for the native SQLite book format.

Migrations are deliberately small, ordered functions.  The database backend owns
transaction boundaries, backups and integrity checks; a migration owns only the SQL
needed to move one schema version to the next.  This keeps historical migrations
immutable and makes every supported upgrade path independently testable.
"""

from __future__ import annotations

import json
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


def v2_to_v3(conn: sqlite3.Connection) -> None:
    """Move FSA claims from metadata into first-class transactional rows."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fsa_claim (
            handle       TEXT PRIMARY KEY,
            service_date TEXT NOT NULL,
            provider     TEXT NOT NULL,
            blob         TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fsa_claim_service_date "
        "ON fsa_claim(service_date)"
    )
    row = conn.execute(
        "SELECT value FROM metadata WHERE key='fsa_claims'"
    ).fetchone()
    if row is not None:
        claims = json.loads(row[0])
        for claim in claims:
            conn.execute(
                "INSERT OR REPLACE INTO fsa_claim(handle,service_date,provider,blob) "
                "VALUES (?,?,?,?)",
                (
                    str(claim["handle"]),
                    str(claim["service_date"]),
                    str(claim.get("provider", "")),
                    json.dumps(claim, separators=(",", ":")),
                ),
            )
        conn.execute("DELETE FROM metadata WHERE key='fsa_claims'")


MIGRATIONS: dict[int, Migration] = {
    1: v1_to_v2,
    2: v2_to_v3,
}

LATEST_SCHEMA_VERSION = max(MIGRATIONS) + 1

__all__ = ["LATEST_SCHEMA_VERSION", "MIGRATIONS", "Migration"]
