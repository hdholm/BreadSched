"""Payees are gone: a transaction's description is its only name, as in GnuCash.

Schema 11 removes the payee table. Rules that matched a payee keep matching what
they matched, through that payee's description keys; transactions lose their payee
reference; and the description keys shared by rules, reimbursement proposals, and
entry autocomplete live in ``engine.description_keys``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from breadsched.gen.db.sqlite import SCHEMA_VERSION, DbSQLite
from breadsched.gen.engine.categorization import RULES_KEY, load_rules, propose_categories
from breadsched.gen.engine.description_keys import match_key
from breadsched.gen.lib import Transaction


@pytest.mark.parametrize(
    ("description", "key"),
    [
        ("CORNER GROCER #1234", "corner grocer"),
        ("Corner Grocer 0987", "corner grocer"),
        ("  corner-grocer  ", "corner grocer"),
        ("CAFÉ Ｌｕｎａ *4411", "café luna"),
        ("12345 6789", ""),
    ],
)
def test_match_key_ignores_case_punctuation_and_reference_numbers(description, key):
    assert match_key(description) == key


def _downgrade(path, *, transaction: str, groceries: str, dining: str) -> None:
    with sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE payee (handle TEXT PRIMARY KEY, name TEXT NOT NULL, blob TEXT)")
        for handle, name, keys in (
            ("p-grocer", "Corner Grocer", ["corner grocer", "grocer outlet"]),
            ("p-cafe", "Cafe Luna", ["cafe luna"]),
            ("p-empty", "Nobody", []),
        ):
            raw.execute(
                "INSERT INTO payee VALUES (?,?,?)",
                (handle, name, json.dumps({"handle": handle, "name": name, "match_keys": keys})),
            )
        rules = [
            {
                "handle": "r1",
                "category": groceries,
                "payee": "p-grocer",
                "key": None,
                "set_payee": None,
            },
            {
                "handle": "r2",
                "category": dining,
                "payee": None,
                "key": "cafe luna",
                "set_payee": "p-cafe",
            },
            {
                "handle": "r3",
                "category": dining,
                "payee": "p-empty",
                "key": None,
                "set_payee": None,
            },
        ]
        raw.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (RULES_KEY, json.dumps(rules)))
        blob = json.loads(
            raw.execute("SELECT blob FROM txn WHERE handle=?", (transaction,)).fetchone()[0]
        )
        blob["payee"] = "p-grocer"
        raw.execute(
            "UPDATE txn SET blob=? WHERE handle=?",
            (json.dumps(blob, separators=(",", ":")), transaction),
        )
        raw.execute("UPDATE metadata SET value='10' WHERE key='schema_version'")
        raw.execute("DELETE FROM schema_migration")
        raw.execute("INSERT INTO schema_migration(version, applied_at) VALUES (10, 'test')")


def test_a_schema_10_book_loses_payees_and_keeps_its_rules(tmp_path):
    path = tmp_path / "payees.breadsched"
    db = DbSQLite()
    db.load(str(path))
    from breadsched.gen.lib import Account, AccountType

    with db.transaction("Setup") as txn:
        root = Account(name="Root", atype=AccountType.ROOT)
        db.add_account(root, txn)
        cash = Account(name="Cash", atype=AccountType.CASH, parent=root.handle)
        groceries = Account(name="Groceries", atype=AccountType.EXPENSE, parent=root.handle)
        dining = Account(name="Dining", atype=AccountType.EXPENSE, parent=root.handle)
        for account in (cash, groceries, dining):
            db.add_account(account, txn)
        spend = Transaction.simple(
            date(2026, 9, 1), "CORNER GROCER #1234", groceries.handle, cash.handle, "10"
        )
        db.add_transaction(spend, txn)
    db.close()
    _downgrade(path, transaction=spend.handle, groceries=groceries.handle, dining=dining.handle)

    migrated = DbSQLite()
    migrated.load(str(path))
    try:
        assert migrated.get_metadata("schema_version") == SCHEMA_VERSION == 11
        conn = migrated._require()
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='payee'").fetchone() is None
        blob = conn.execute("SELECT blob FROM txn WHERE handle=?", (spend.handle,)).fetchone()[0]
        assert "payee" not in json.loads(blob)
        # The payee rule became one rule per key in its place; the payee with no
        # keys matched nothing and leaves no rule; set_payee is gone.
        assert [(rule.handle, rule.key, rule.category) for rule in load_rules(migrated)] == [
            ("r1", "corner grocer", groceries.handle),
            ("r1-1", "grocer outlet", groceries.handle),
            ("r2", "cafe luna", dining.handle),
        ]
        stored = migrated.get_metadata(RULES_KEY)
        assert all(set(rule) == {"handle", "category", "key"} for rule in stored)
        assert migrated.verify_book() == []
        assert migrated.integrity_problems() == []
    finally:
        migrated.close()
    assert (tmp_path / "payees.breadsched.pre-migration-v10.bak").exists()


def test_rules_match_only_descriptions(db, book):
    from breadsched.gen.engine.categorization import placeholder_handles
    from breadsched.gen.lib import Account, AccountType
    from breadsched.gen.services.categorization import AddRule, add_rule

    placeholder = dict(placeholder_handles())[("csv", "EXPENSE")]
    with db.transaction("Placeholder") as txn:
        db.add_account(
            Account(
                name="Uncategorized CSV",
                atype=AccountType.EXPENSE,
                parent=book.expenses,
                handle=placeholder,
            ),
            txn,
        )
        db.add_transaction(
            Transaction.simple(
                date(2026, 9, 1), "Corner Grocer 0987", placeholder, book.checking, "10"
            ),
            txn,
        )
    assert add_rule(db, AddRule(book.groceries, "CORNER GROCER #1")).ok
    [proposal] = propose_categories(db)
    assert proposal.category == book.groceries
    refused = add_rule(db, AddRule(book.groceries, "#1234"))
    assert [error.code for error in refused.errors] == ["rule.match.empty"]
