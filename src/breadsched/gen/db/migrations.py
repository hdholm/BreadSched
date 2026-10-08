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


def _v9_to_v10(conn: sqlite3.Connection) -> None:
    """Add savings goals: earmarks toward a target amount by a target date."""
    conn.execute(
        """
        CREATE TABLE savings_goal (
            handle TEXT PRIMARY KEY,
            name   TEXT NOT NULL,
            blob   TEXT NOT NULL
        )
        """
    )


def _v10_to_v11(conn: sqlite3.Connection) -> None:
    """Remove payees: the description is a transaction's only name.

    A categorization rule that matched a payee becomes one rule per description
    key that payee claimed, in the same position, so it keeps matching what it
    matched; a rule's ``set_payee`` goes. Transactions lose their payee reference,
    and the payee table is dropped.
    """
    import json

    keys = {
        row[0]: list(json.loads(row[1]).get("match_keys", []))
        for row in conn.execute("SELECT handle, blob FROM payee")
    }
    row = conn.execute("SELECT value FROM metadata WHERE key='categorization_rules'").fetchone()
    if row is not None:
        rules = json.loads(row[0])
        converted = []
        if isinstance(rules, list):
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                rule = {k: v for k, v in rule.items() if k != "set_payee"}
                payee = rule.pop("payee", None)
                if payee is None:
                    converted.append(rule)
                    continue
                for number, key in enumerate(keys.get(payee, [])):
                    handle = rule["handle"] if number == 0 else f"{rule['handle']}-{number}"
                    converted.append({**rule, "handle": handle, "key": key})
        conn.execute(
            "UPDATE metadata SET value=? WHERE key='categorization_rules'",
            (json.dumps(converted),),
        )
    for handle, blob in conn.execute(
        "SELECT handle, blob FROM txn WHERE json_type(blob, '$.payee') IS NOT NULL"
    ).fetchall():
        data = json.loads(blob)
        data.pop("payee", None)
        conn.execute(
            "UPDATE txn SET blob=? WHERE handle=?",
            (json.dumps(data, separators=(",", ":")), handle),
        )
    conn.execute("DROP TABLE payee")


MIGRATIONS: dict[int, Migration] = {
    6: Migration(6, 7, "add reconciliation sessions", _v6_to_v7),
    7: Migration(7, 8, "add payees", _v7_to_v8),
    8: Migration(8, 9, "add reimbursable-expense receivables", _v8_to_v9),
    9: Migration(9, 10, "add savings goals", _v9_to_v10),
    10: Migration(10, 11, "remove payees", _v10_to_v11),
}
MIN_SUPPORTED_SCHEMA_VERSION = min(MIGRATIONS)

__all__ = ["MIGRATIONS", "MIN_SUPPORTED_SCHEMA_VERSION", "Migration", "MigrationAction"]
