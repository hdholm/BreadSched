"""Forward migrations for the native-book compatibility window.

The window starts at the schema of the last alpha series (13). A book from an
earlier schema must first be opened by the last alpha, 0.2.0a321.
"""

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


def _v13_to_v14(conn: sqlite3.Connection) -> None:
    """Write each whole-transaction plan link as split allocations.

    A transaction linked to an occurrence (``planned_occurrence``, or posted from a
    schedule) allocates each of its plan legs in full to it: the legs on income or
    expense accounts or carrying a planning purpose, else its positive legs. Its
    ``planned_for`` and ``planned_amount`` are kept on each allocation. Nothing else
    changes; the step checks that no transaction was added or lost and that every
    link it wrote names the occurrence the transaction already named.
    """
    import json

    classes = {}
    for handle, blob in conn.execute("SELECT handle, blob FROM account").fetchall():
        data = json.loads(blob)
        if isinstance(data, dict):
            classes[handle] = _account_class(str(data.get("atype", "")))
    before = conn.execute("SELECT COUNT(*) FROM txn").fetchone()[0]
    rows = conn.execute("SELECT handle, post_date, blob FROM txn").fetchall()
    written = 0
    for handle, post_date, blob in rows:
        data = json.loads(blob)
        if not isinstance(data, dict):
            continue
        splits = [split for split in data.get("splits") or [] if isinstance(split, dict)]
        if any(split.get("plan_allocations") for split in splits):
            continue
        key = data.get("planned_occurrence")
        if key is None and data.get("scheduled_from"):
            when = data.get("planned_for") or str(post_date)[:10]
            key = f"scheduled:{data['scheduled_from']}:{when}"
        if key is None:
            continue

        def value(split: dict) -> tuple[int, int]:
            raw = split.get("value") or [0, 1]
            return int(raw[0]), int(raw[1])

        legs = [
            split
            for split in splits
            if value(split)[0]
            and (
                split.get("planning_flow") is not None
                or classes.get(str(split.get("account"))) in {"income", "expense"}
            )
        ] or [split for split in splits if value(split)[0] * value(split)[1] > 0]
        for split in legs:
            split["plan_allocations"] = [
                {
                    "occurrence": key,
                    "amount": list(value(split)),
                    "planned_for": data.get("planned_for"),
                    "planned_amount": data.get("planned_amount"),
                }
            ]
        if legs:
            written += 1
            conn.execute(
                "UPDATE txn SET blob=? WHERE handle=?",
                (json.dumps(data, separators=(",", ":")), handle),
            )
    after = conn.execute("SELECT COUNT(*) FROM txn").fetchone()[0]
    if after != before:
        raise RuntimeError(
            f"plan allocation migration changed the transaction count {before}→{after}"
        )
    for handle, blob in conn.execute("SELECT handle, blob FROM txn").fetchall():
        data = json.loads(blob)
        if not isinstance(data, dict) or not data.get("planned_occurrence"):
            continue
        named = {
            item.get("occurrence")
            for split in data.get("splits") or []
            for item in split.get("plan_allocations") or []
        }
        if named and named != {data["planned_occurrence"]}:
            raise RuntimeError(f"transaction {handle} lost its planned occurrence")


def _account_class(atype: str) -> str:
    """An account type's class, as ``lib.account`` defines it, for a migration."""
    from ..lib.account import AccountType

    try:
        return AccountType.parse(atype).account_class.value
    except (KeyError, ValueError):
        return ""


MIGRATIONS: dict[int, Migration] = {
    13: Migration(13, 14, "write plan links as split allocations", _v13_to_v14),
}
MIN_SUPPORTED_SCHEMA_VERSION = min(MIGRATIONS)

__all__ = ["MIGRATIONS", "MIN_SUPPORTED_SCHEMA_VERSION", "Migration", "MigrationAction"]
