"""Schema 12 and the deduplicated records of #337 and #338.

Dashboard group membership was stored twice, in the dashboard configuration's
account lists and in each account's group; schema 12 keeps it on the accounts.
FSA claims and receivables pointed at ledger splits through two identical types;
one ``SplitLink`` now serves both, with the stored form unchanged. The ledger's
compatibility scalars are gone in favour of the Amount-returning functions.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine import ledger
from breadsched.gen.lib import (
    Account,
    AccountType,
    FsaClaim,
    Money,
    Receivable,
    SplitLink,
    fsa_claim,
    receivable,
)


def _schema_11_book(path) -> dict[str, str]:
    db = DbSQLite()
    db.load(str(path))
    with db.transaction("Setup") as txn:
        root = Account(name="Root", atype=AccountType.ROOT)
        db.add_account(root, txn)
        handles = {"root": root.handle}
        for name, atype, group in (
            ("Checking", AccountType.BANK, ""),
            ("Savings", AccountType.BANK, "Rainy day"),
            ("House", AccountType.ASSET, "Elsewhere"),
            ("Mortgage", AccountType.LIABILITY, ""),
            ("Card", AccountType.CREDIT, "Debts"),
        ):
            account = Account(name=name, atype=atype, parent=root.handle)
            account.group = group
            db.add_account(account, txn)
            handles[name] = account.handle
    db.close()
    config = {
        "groups": [
            {
                "name": "Cash",
                "accounts": [handles["Checking"], handles["Savings"]],
                "kind": "liquid",
            },
            {
                "name": "Home",
                "accounts": [handles["House"], handles["Mortgage"]],
                "kind": "property",
            },
            # A repeated account stays in its first group, as it was shown.
            {"name": "Again", "accounts": [handles["Checking"]], "kind": "asset"},
            # A second group of the same name merges into the first.
            {"name": "Cash", "accounts": [], "kind": "asset"},
            {"name": "Gone", "accounts": ["no-such-account"], "kind": "asset"},
        ],
        "liquidity_days": 21,
        "emergency_months": 4,
        "reserve": [0, 1],
        "minimum_bill": [0, 1],
    }
    with sqlite3.connect(path) as raw:
        raw.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('dashboard', ?)", (json.dumps(config),)
        )
        raw.execute("UPDATE metadata SET value='11' WHERE key='schema_version'")
        raw.execute("DELETE FROM schema_migration")
        raw.execute("INSERT INTO schema_migration(version, applied_at) VALUES (11, 'test')")
    return handles


def test_claims_and_receivables_share_one_split_link():
    link = SplitLink("txn", "split")
    assert link.serialize() == {"transaction": "txn", "split": "split"}
    assert SplitLink.from_dict(link.serialize()) == link
    assert not hasattr(fsa_claim, "FsaClaimSplitLink")
    assert not hasattr(receivable, "ReceivableSplitLink")
    claim = FsaClaim(service_date=date(2026, 1, 1), provider="Clinic", payments=[link])
    restored = FsaClaim.from_dict(claim.serialize())
    assert restored.payments == [link] and type(restored.payments[0]) is SplitLink
    owed = Receivable(
        incurred_date=date(2026, 1, 1),
        payer="Insurer",
        expected_amount=Money(10),
        expenses=[link],
    )
    assert Receivable.from_dict(owed.serialize()).expenses == [link]


def test_the_ledger_keeps_only_the_amount_forms():
    for name in ("balance", "balance_recursive", "totals_by_class", "net_worth", "cash_on_hand"):
        assert not hasattr(ledger, name), name
        assert hasattr(ledger, f"{name}_amount"), name
