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


def _single_account_type(data: dict[str, object]) -> str:
    """Collapse the former ledger-type/account-kind pair into one type."""
    raw_type = str(data.get("atype", "ASSET")).strip().upper().replace("_", " ")
    legacy_kind = str(data.get("kind", data.get("planning_role", "ordinary"))).strip().lower()
    semantic_types = {
        "retirement": "RETIREMENT",
        "fsa": "FSA",
        "investment": "INVESTMENT",
        "escrow": "ESCROW",
    }
    if legacy_kind in semantic_types:
        return semantic_types[legacy_kind]
    if legacy_kind == "debt":
        return "CREDIT CARD" if raw_type in {"CREDIT", "CREDIT CARD"} else "LOAN"
    aliases = {
        "CREDIT": "CREDIT CARD",
        "STOCK": "INVESTMENT",
        "MUTUAL": "INVESTMENT",
        "CURRENCY": "ASSET",
        "RECEIVABLE": "ASSET",
        "PAYABLE": "LIABILITY",
        "TRADING": "TECHNICAL",
    }
    supported = {
        "ROOT",
        "BANK",
        "CASH",
        "ASSET",
        "INVESTMENT",
        "RETIREMENT",
        "FSA",
        "ESCROW",
        "CREDIT CARD",
        "LOAN",
        "LIABILITY",
        "INCOME",
        "EXPENSE",
        "EQUITY",
        "TECHNICAL",
    }
    normalized = aliases.get(raw_type, raw_type)
    return normalized if normalized in supported else "ASSET"


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


def v4_to_v5(conn: sqlite3.Connection) -> None:
    """Repair account rows left in the former two-field representation."""
    for handle, raw in conn.execute("SELECT handle, blob FROM account").fetchall():
        data = json.loads(raw)
        account_type = _single_account_type(data)
        data["atype"] = account_type
        data.pop("kind", None)
        data.pop("planning_role", None)
        conn.execute(
            "UPDATE account SET atype=?, blob=? WHERE handle=?",
            (account_type, json.dumps(data, separators=(",", ":")), handle),
        )


def v5_to_v6(conn: sqlite3.Connection) -> None:
    """Add dated commodity prices and indexed account-commodity quantities."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS price (
            handle     TEXT PRIMARY KEY,
            commodity  TEXT NOT NULL,
            currency   TEXT NOT NULL,
            quote_date TEXT NOT NULL,
            source     TEXT NOT NULL,
            blob       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_price_lookup
            ON price(commodity, currency, quote_date);
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(split_index)")}
    if "quantity_num" not in columns:
        conn.execute("ALTER TABLE split_index ADD COLUMN quantity_num INTEGER NOT NULL DEFAULT 0")
    if "quantity_den" not in columns:
        conn.execute("ALTER TABLE split_index ADD COLUMN quantity_den INTEGER NOT NULL DEFAULT 1")
    updates: list[tuple[int, int, str]] = []
    for (raw,) in conn.execute("SELECT blob FROM txn").fetchall():
        data = json.loads(raw)
        for split in data.get("splits", []):
            quantity = split.get("quantity") or split.get("value", [0, 1])
            updates.append((int(quantity[0]), int(quantity[1]), str(split["handle"])))
    conn.executemany(
        "UPDATE split_index SET quantity_num=?, quantity_den=? WHERE handle=?",
        updates,
    )


MIGRATIONS: dict[int, Migration] = {3: v3_to_v4, 4: v4_to_v5, 5: v5_to_v6}
LATEST_SCHEMA_VERSION = 6

__all__ = [
    "LATEST_SCHEMA_VERSION",
    "MIGRATIONS",
    "MIN_SUPPORTED_SCHEMA_VERSION",
    "Migration",
]
