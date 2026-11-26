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


def _v14_to_v15(conn: sqlite3.Connection) -> None:
    """Let schedules carry a number, notes, tags, and linked documents.

    The new fields are optional keys of a schedule's stored form, absent until set,
    so no row changes; the version itself is the step. It keeps a release that
    does not know them (schema 14) from opening the book and dropping them when it
    saves a schedule. The step checks that no schedule was added or lost.
    """
    before = conn.execute("SELECT COUNT(*) FROM scheduled").fetchone()[0]
    after = conn.execute("SELECT COUNT(*) FROM scheduled").fetchone()[0]
    if after != before:
        raise RuntimeError(f"schedule document migration changed the count {before}→{after}")


#: The keys schema 15 kept a schedule's changes under, before versions.
_LEGACY_KEYS = ("amount_changes", "seasonal_amounts", "occurrence_adjustments")


def _with_versions(data: dict) -> bool:
    """Rewrite one stored schedule's changes as versions; True if it changed."""
    from ..lib.schedule_versions import versions_from_legacy

    splits = [split for split in data.get("splits") or [] if isinstance(split, dict)]
    legacy = any(data.get(key) for key in _LEGACY_KEYS) or any(
        split.get("amount_changes") for split in splits
    )
    if "versions" in data and not legacy:
        return False
    versions = versions_from_legacy(
        splits,
        data.get("amount_changes") or [],
        data.get("seasonal_amounts") or [],
        data.get("occurrence_adjustments") or [],
    )
    data["versions"] = [version.serialize() for version in versions]
    for key in _LEGACY_KEYS:
        data.pop(key, None)
    for split in splits:
        split.pop("amount_changes", None)
    return True


def _v15_to_v16(conn: sqlite3.Connection) -> None:
    """Keep a schedule's changes as versions of the whole transaction.

    Schema 15 kept four narrower changes: a whole-transaction amount from a date, each
    fixed leg's amount from a date, a whole-transaction amount for a calendar month,
    and one for a single date. Each schedule, and each scenario's own schedules, now
    keeps them as ``versions`` (``lib.schedule_versions.versions_from_legacy``), with
    the amounts they gave; a month's version now also applies after a later
    whole-transaction change, as versions are layered. The step checks that no
    schedule or scenario was added or lost and that every schedule reads back.
    """
    import json

    from ..lib.scenario import ScenarioSchedule
    from ..lib.scheduled import ScheduledTransaction

    before = conn.execute("SELECT COUNT(*) FROM scheduled").fetchone()[0]
    for handle, blob in conn.execute("SELECT handle, blob FROM scheduled").fetchall():
        data = json.loads(blob)
        if not isinstance(data, dict) or not _with_versions(data):
            continue
        ScheduledTransaction.from_dict({**data, "handle": handle})
        conn.execute(
            "UPDATE scheduled SET blob=? WHERE handle=?",
            (json.dumps(data, separators=(",", ":")), handle),
        )
    after = conn.execute("SELECT COUNT(*) FROM scheduled").fetchone()[0]
    if after != before:
        raise RuntimeError(f"schedule version migration changed the count {before}→{after}")

    scenarios = conn.execute("SELECT COUNT(*) FROM scenario").fetchone()[0]
    for handle, blob in conn.execute("SELECT handle, blob FROM scenario").fetchall():
        data = json.loads(blob)
        if not isinstance(data, dict):
            continue
        changed = False
        for item in data.get("schedule_overrides") or []:
            if isinstance(item, dict) and _with_versions(item):
                ScenarioSchedule.from_dict(item)
                changed = True
        if changed:
            conn.execute(
                "UPDATE scenario SET blob=? WHERE handle=?",
                (json.dumps(data, separators=(",", ":")), handle),
            )
    if conn.execute("SELECT COUNT(*) FROM scenario").fetchone()[0] != scenarios:
        raise RuntimeError("schedule version migration changed the scenario count")


MIGRATIONS: dict[int, Migration] = {
    13: Migration(13, 14, "write plan links as split allocations", _v13_to_v14),
    14: Migration(14, 15, "let schedules carry notes, tags, and documents", _v14_to_v15),
    15: Migration(15, 16, "keep schedule changes as whole-transaction versions", _v15_to_v16),
}
MIN_SUPPORTED_SCHEMA_VERSION = min(MIGRATIONS)

__all__ = ["MIGRATIONS", "MIN_SUPPORTED_SCHEMA_VERSION", "Migration", "MigrationAction"]
