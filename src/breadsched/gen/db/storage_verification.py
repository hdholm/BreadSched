"""Checks that a native book's derived storage agrees with its object blobs.

Each table's indexed columns, and every row of ``split_index``, are derived from the
authoritative JSON blob purely to speed up queries. These checks report any drift
(a mismatched or duplicate object handle, a derived column that disagrees with its
blob, or a missing, orphaned, or different split row) so the indexes can be rebuilt
rather than answering differently depending on the query path.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from ..lib.transaction import Transaction
from .verification import BookIssue

__all__ = ["derived_column_issues", "split_index_issues"]


def derived_column_issues(conn: sqlite3.Connection) -> list[BookIssue]:
    """Rows whose handle or derived columns disagree with their blob."""
    issues: list[BookIssue] = []
    # Derived columns are query accelerators only; the JSON blob is
    # authoritative.  Detect drift so indexes can be rebuilt rather than
    # silently returning different data depending on the query path.
    derived_specs: dict[str, tuple[str, ...]] = {
        "commodity": ("mnemonic",),
        "price": ("commodity", "currency", "quote_date", "source"),
        "account": ("parent", "name", "atype"),
        "txn": ("post_date", "description"),
        "scheduled": ("name",),
        "scenario": ("name",),
        "fsa_claim": ("service_date", "provider"),
        "receivable": ("incurred_date", "payer"),
        "reconciliation": ("account", "statement_date", "status"),
        "payee": ("name",),
        "savings_goal": ("name",),
    }
    defaults: dict[tuple[str, str], Any] = {
        ("commodity", "mnemonic"): "",
        ("price", "commodity"): "",
        ("price", "currency"): "",
        ("price", "quote_date"): "",
        ("price", "source"): "",
        ("account", "parent"): None,
        ("account", "name"): "",
        ("account", "atype"): "",
        ("txn", "description"): "",
        ("scheduled", "name"): "",
        ("scenario", "name"): "",
        ("fsa_claim", "service_date"): "",
        ("receivable", "incurred_date"): "",
        ("fsa_claim", "provider"): "",
        ("receivable", "payer"): "",
        ("reconciliation", "account"): "",
        ("reconciliation", "statement_date"): "",
        ("reconciliation", "status"): "",
        ("payee", "name"): "",
        ("savings_goal", "name"): "",
    }
    for table, columns in derived_specs.items():
        selected = ", ".join(("handle", *columns, "blob"))
        internal_handles: dict[str, str] = {}
        for row in conn.execute(f"SELECT {selected} FROM {table}"):
            try:
                data = json.loads(row["blob"])
            except (json.JSONDecodeError, TypeError):
                continue  # the malformed-object diagnostic already owns this row
            internal = data.get("handle")
            if internal != row["handle"]:
                issues.append(
                    BookIssue(
                        f"{table}.handle_mismatch",
                        f"{table} row {row['handle']} contains object handle {internal!r}",
                        row["handle"],
                    )
                )
            if internal is not None:
                previous = internal_handles.get(internal)
                if previous is not None and previous != row["handle"]:
                    issues.append(
                        BookIssue(
                            f"{table}.duplicate_object_handle",
                            f"{table} rows {previous} and {row['handle']} both contain "
                            f"object handle {internal}",
                            row["handle"],
                        )
                    )
                else:
                    internal_handles[internal] = row["handle"]
            for column in columns:
                expected_value = data.get(column, defaults.get((table, column)))
                if table == "txn" and column == "post_date" and expected_value is not None:
                    expected_value = str(expected_value)
                if row[column] != expected_value:
                    issues.append(
                        BookIssue(
                            f"{table}.index_mismatch",
                            f"{table} derived column {column} disagrees with blob for "
                            f"{row['handle']}",
                            row["handle"],
                        )
                    )

    return issues


def split_index_issues(
    conn: sqlite3.Connection, transactions: Iterable[Transaction]
) -> list[BookIssue]:
    """Split rows missing from, unknown to, or different in ``split_index``."""
    issues: list[BookIssue] = []
    expected: dict[str, tuple[str, str, str, int, int, int, int]] = {}
    for transaction in transactions:
        for split in transaction.splits:
            expected[split.handle] = (
                transaction.handle,
                split.account,
                transaction.post_date.isoformat(),
                split.value.numerator,
                split.value.denominator,
                split.quantity.numerator,
                split.quantity.denominator,
            )

    actual = {
        row["handle"]: (
            row["txn"],
            row["account"],
            row["post_date"],
            row["value_num"],
            row["value_den"],
            row["quantity_num"],
            row["quantity_den"],
        )
        for row in conn.execute(
            "SELECT handle, txn, account, post_date, value_num, value_den, "
            "quantity_num, quantity_den FROM split_index"
        )
    }
    for handle in sorted(expected.keys() - actual.keys()):
        issues.append(
            BookIssue("split_index.missing", f"split {handle} is missing from split_index", handle)
        )
    for handle in sorted(actual.keys() - expected.keys()):
        issues.append(
            BookIssue("split_index.orphan", f"split_index contains unknown split {handle}", handle)
        )
    for handle in sorted(expected.keys() & actual.keys()):
        if expected[handle] != actual[handle]:
            issues.append(
                BookIssue(
                    "split_index.mismatch",
                    f"split_index disagrees with transaction data for split {handle}",
                    handle,
                )
            )
    return issues
