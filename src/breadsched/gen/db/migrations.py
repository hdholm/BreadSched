"""Forward migrations from the supported native-book baseline."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

Migration = Callable[[sqlite3.Connection], None]

# BreadSched 0.2.0a3 is the oldest supported native-book format. Earlier
# development formats were never used for durable books and intentionally have no
# upgrade path.
MIN_SUPPORTED_SCHEMA_VERSION = 3


def v3_to_v4(conn: sqlite3.Connection) -> None:
    """Remove the retired Budget domain from the supported a3 format."""
    obsolete_fields = {
        "scheduled": ("budgets", "budgets_decided"),
        "scenario": ("basis", "budget", "extend_budget"),
    }
    for table, fields in obsolete_fields.items():
        for handle, raw in conn.execute(f"SELECT handle, blob FROM {table}").fetchall():
            data = json.loads(raw)
            for field in fields:
                data.pop(field, None)
            conn.execute(
                f"UPDATE {table} SET blob=? WHERE handle=?",
                (json.dumps(data, separators=(",", ":")), handle),
            )
    conn.execute("DROP TABLE IF EXISTS budget")
    conn.execute("DELETE FROM metadata WHERE key='current_budget'")


MIGRATIONS: dict[int, Migration] = {3: v3_to_v4}
LATEST_SCHEMA_VERSION = 4

__all__ = [
    "LATEST_SCHEMA_VERSION",
    "MIGRATIONS",
    "MIN_SUPPORTED_SCHEMA_VERSION",
    "Migration",
]
