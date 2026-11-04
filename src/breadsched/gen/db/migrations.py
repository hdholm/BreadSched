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


def _v11_to_v12(conn: sqlite3.Connection) -> None:
    """Store Dashboard group membership once, on the accounts (#337).

    The dashboard configuration listed each group's accounts and accounts carried
    their own group; the Dashboard merged the two with the configuration winning.
    Each configured account now takes its first configured group as its own (the
    group it was shown in), and the configuration keeps only each group's name and
    kind, in order, the first of two same-named groups giving the kind.
    """
    import json

    row = conn.execute("SELECT value FROM metadata WHERE key='dashboard'").fetchone()
    if row is None:
        return
    config = json.loads(row[0])
    if not isinstance(config, dict):
        return
    assigned: dict[str, str] = {}
    groups: list[dict] = []
    names: set[str] = set()
    for group in config.get("groups") or []:
        if not isinstance(group, dict) or not group.get("name"):
            continue
        name = str(group["name"])
        for handle in group.get("accounts") or []:
            assigned.setdefault(str(handle), name)
        if name not in names:
            names.add(name)
            groups.append({"name": name, "kind": group.get("kind", "asset")})
    for handle, name in assigned.items():
        found = conn.execute("SELECT blob FROM account WHERE handle=?", (handle,)).fetchone()
        if found is None:
            continue
        data = json.loads(found[0])
        if data.get("group") != name:
            data["group"] = name
            conn.execute(
                "UPDATE account SET blob=? WHERE handle=?",
                (json.dumps(data, separators=(",", ":")), handle),
            )
    config["groups"] = groups
    conn.execute("UPDATE metadata SET value=? WHERE key='dashboard'", (json.dumps(config),))


def _v12_to_v13(conn: sqlite3.Connection) -> None:
    """Merge each transaction's and account's two notes into one (#385).

    Imported (GnuCash) notes and BreadSched notes were kept apart; they are one
    note now, the imported text first and BreadSched's after a blank line (the
    same text once), with a fingerprint of the imported text so a later import
    knows what it last brought (``lib.notes``).
    """
    import json

    from ..lib.notes import merge_legacy_notes

    for table in ("txn", "account"):
        rows = conn.execute(f"SELECT handle, blob FROM {table}").fetchall()  # noqa: S608
        for handle, blob in rows:
            data = json.loads(blob)
            if not isinstance(data, dict) or "source_notes" not in data:
                continue
            notes, digest = merge_legacy_notes(
                str(data.pop("source_notes") or ""), str(data.get("notes") or "")
            )
            data["notes"] = notes
            data["imported_notes_digest"] = digest
            conn.execute(
                f"UPDATE {table} SET blob=? WHERE handle=?",  # noqa: S608
                (json.dumps(data, separators=(",", ":")), handle),
            )


def _v13_to_v14(conn: sqlite3.Connection) -> None:
    """Write each whole-transaction plan link as split allocations (decision 0005).

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
    6: Migration(6, 7, "add reconciliation sessions", _v6_to_v7),
    7: Migration(7, 8, "add payees", _v7_to_v8),
    8: Migration(8, 9, "add reimbursable-expense receivables", _v8_to_v9),
    9: Migration(9, 10, "add savings goals", _v9_to_v10),
    10: Migration(10, 11, "remove payees", _v10_to_v11),
    11: Migration(11, 12, "store Dashboard group membership on accounts", _v11_to_v12),
    12: Migration(12, 13, "merge imported and BreadSched notes", _v12_to_v13),
    13: Migration(13, 14, "write plan links as split allocations", _v13_to_v14),
}
MIN_SUPPORTED_SCHEMA_VERSION = min(MIGRATIONS)

__all__ = ["MIGRATIONS", "MIN_SUPPORTED_SCHEMA_VERSION", "Migration", "MigrationAction"]
