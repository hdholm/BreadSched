"""Forward migrations for the rolling native-book compatibility window."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

MigrationAction = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class Migration:
    """One explicit, sequential native-book transformation."""

    source: int
    target: int
    name: str
    apply: MigrationAction


def _v6_to_v7(conn: sqlite3.Connection) -> None:
    """Add auditable account-statement reconciliation sessions."""
    # Do not use ``executescript`` here: sqlite3 may commit an open transaction
    # before running a script, which would defeat the migration runner's rollback.
    conn.execute(
        """
        CREATE TABLE reconciliation (
            handle         TEXT PRIMARY KEY,
            account        TEXT NOT NULL,
            statement_date TEXT NOT NULL,
            status         TEXT NOT NULL,
            blob           TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_reconciliation_account_date
        ON reconciliation(account, statement_date)
        """
    )


def _v7_to_v8(conn: sqlite3.Connection) -> None:
    """Add payees; transactions gain an optional payee reference in their blob."""
    conn.execute(
        """
        CREATE TABLE payee (
            handle TEXT PRIMARY KEY,
            name   TEXT NOT NULL,
            blob   TEXT NOT NULL
        )
        """
    )


def _v8_to_v9(conn: sqlite3.Connection) -> None:
    """Add reimbursable-expense receivables, linked to ordinary ledger splits."""
    conn.execute(
        """
        CREATE TABLE receivable (
            handle        TEXT PRIMARY KEY,
            incurred_date TEXT NOT NULL,
            payer         TEXT NOT NULL,
            blob          TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX idx_receivable_incurred_date ON receivable(incurred_date)")


MIGRATIONS: dict[int, Migration] = {
    6: Migration(6, 7, "add reconciliation sessions", _v6_to_v7),
    7: Migration(7, 8, "add payees", _v7_to_v8),
    8: Migration(8, 9, "add reimbursable-expense receivables", _v8_to_v9),
}
MIN_SUPPORTED_SCHEMA_VERSION = min(MIGRATIONS)

__all__ = ["MIGRATIONS", "MIN_SUPPORTED_SCHEMA_VERSION", "Migration", "MigrationAction"]
