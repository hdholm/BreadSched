"""Compare transaction storage layouts on a realistic household book.

``python scripts/storage_benchmark.py [--transactions N] [--repeat R]``

The current layout keeps each transaction as a JSON blob in ``txn`` with a derived
``split_index``; the candidate normalizes transactions and splits into typed,
constrained tables (``txn_n``/``split_n``) and keeps only rarely used,
BreadSched-owned transaction fields as JSON. Both are written and read with the
same plain SQL the storage layer would use, so the figures compare the layouts and
not the surrounding undo journal or verification, which either layout needs. Each
operation reports the best of ``--repeat`` runs. Results go to standard output as
a Markdown table; ``docs/design/decisions/0001-transaction-storage.md`` records
the measured decision.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from breadsched.gen.lib.money import Money  # noqa: E402
from breadsched.gen.lib.transaction import ReconcileState, Split, Transaction  # noqa: E402

BLOB_SCHEMA = """
CREATE TABLE txn (
    handle TEXT PRIMARY KEY, post_date TEXT NOT NULL, description TEXT, blob TEXT NOT NULL
);
CREATE INDEX idx_txn_date ON txn(post_date);
CREATE TABLE split_index (
    handle TEXT PRIMARY KEY, txn TEXT NOT NULL, account TEXT NOT NULL,
    post_date TEXT NOT NULL, value_num INTEGER NOT NULL, value_den INTEGER NOT NULL,
    quantity_num INTEGER NOT NULL DEFAULT 0, quantity_den INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_split_account ON split_index(account, post_date);
CREATE INDEX idx_split_txn ON split_index(txn);
"""

NORMALIZED_SCHEMA = """
CREATE TABLE txn_n (
    handle TEXT PRIMARY KEY,
    post_date TEXT NOT NULL CHECK (post_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    enter_date TEXT,
    num TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    currency TEXT,
    notes TEXT NOT NULL DEFAULT '',
    extra TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_txn_n_date ON txn_n(post_date);
CREATE TABLE split_n (
    handle TEXT PRIMARY KEY,
    txn TEXT NOT NULL REFERENCES txn_n(handle) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    account TEXT NOT NULL,
    value_num INTEGER NOT NULL,
    value_den INTEGER NOT NULL CHECK (value_den > 0),
    quantity_num INTEGER NOT NULL,
    quantity_den INTEGER NOT NULL CHECK (quantity_den > 0),
    memo TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    reconcile TEXT NOT NULL CHECK (reconcile IN ('n', 'c', 'y', 'f', 'v')),
    reconcile_date TEXT,
    extra TEXT NOT NULL DEFAULT '{}',
    UNIQUE (txn, position)
);
CREATE INDEX idx_split_n_account ON split_n(account);
"""

_TXN_COLUMNS = {"handle", "post_date", "enter_date", "num", "description", "currency", "notes"}
_SPLIT_COLUMNS = {
    "handle",
    "account",
    "value",
    "quantity",
    "memo",
    "action",
    "reconcile",
    "reconcile_date",
}


def household(count: int, seed: int = 7) -> tuple[list[str], list[Transaction]]:
    """A household history: paychecks, bills, groceries, card payments, and splits."""
    rng = random.Random(seed)
    accounts = [f"acct{index:03d}" for index in range(60)]
    bank, card = accounts[0], accounts[1]
    start = date(2006, 1, 1)
    transactions: list[Transaction] = []
    for index in range(count):
        when = start + timedelta(days=index * 20 * 365 // max(count, 1))
        kind = rng.random()
        transaction = Transaction(
            post_date=when, description=f"Merchant {rng.randrange(400)} #{rng.randrange(9999)}"
        )
        transaction.num = str(1000 + index) if kind < 0.1 else ""
        transaction.notes = "Annual renewal" if kind > 0.97 else ""
        amount = Money(rng.randrange(100, 250_000), 100)
        if kind < 0.15:
            # A paycheck with deductions: four or five splits.
            parts = [amount, Money(rng.randrange(1000, 40000), 100)]
            transaction.add_split(Split(bank, amount, memo="Net pay"))
            for deduction in parts[1:]:
                transaction.add_split(Split(rng.choice(accounts[2:20]), deduction, memo="Tax"))
            total = sum((split.value for split in transaction.splits), Money(0))
            transaction.add_split(Split(accounts[20], -total, memo="Gross pay"))
        else:
            source = card if kind < 0.6 else bank
            transaction.add_split(Split(rng.choice(accounts[21:]), amount))
            transaction.add_split(Split(source, -amount))
        for split in transaction.splits:
            if when < date(2025, 1, 1):
                split.reconcile = ReconcileState.RECONCILED
                split.reconcile_date = when
        transactions.append(transaction)
    return accounts, transactions


def _blob_rows(data: dict) -> tuple[tuple, list[tuple]]:
    txn = (
        data["handle"],
        data["post_date"],
        data.get("description", ""),
        json.dumps(data, separators=(",", ":")),
    )
    splits = [
        (
            split["handle"],
            data["handle"],
            split["account"],
            data["post_date"],
            split["value"][0],
            split["value"][1],
            split["quantity"][0],
            split["quantity"][1],
        )
        for split in data["splits"]
    ]
    return txn, splits


def _normalized_rows(data: dict) -> tuple[tuple, list[tuple]]:
    extra = {key: value for key, value in data.items() if key not in _TXN_COLUMNS | {"splits"}}
    txn = (
        data["handle"],
        data["post_date"],
        data.get("enter_date"),
        data.get("num", ""),
        data.get("description", ""),
        data.get("currency"),
        data.get("notes", ""),
        json.dumps(extra, separators=(",", ":")),
    )
    splits = []
    for position, split in enumerate(data["splits"]):
        rest = {key: value for key, value in split.items() if key not in _SPLIT_COLUMNS}
        splits.append(
            (
                split["handle"],
                data["handle"],
                position,
                split["account"],
                split["value"][0],
                split["value"][1],
                split["quantity"][0],
                split["quantity"][1],
                split.get("memo", ""),
                split.get("action", ""),
                split["reconcile"],
                split.get("reconcile_date"),
                json.dumps(rest, separators=(",", ":")),
            )
        )
    return txn, splits


class Layout:
    name = ""
    schema = ""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(self.schema)

    def write(self, transactions: Iterable[dict]) -> None:
        raise NotImplementedError

    def load_all(self) -> list[Transaction]:
        raise NotImplementedError

    def register(self, account: str) -> list[Transaction]:
        raise NotImplementedError

    def balance(self, account: str) -> Money:
        raise NotImplementedError

    def close(self) -> None:
        self.conn.close()


class BlobLayout(Layout):
    name = "blob + split_index (current)"
    schema = BLOB_SCHEMA

    def write(self, transactions: Iterable[dict]) -> None:
        with self.conn:
            for data in transactions:
                txn, splits = _blob_rows(data)
                self.conn.execute("INSERT OR REPLACE INTO txn VALUES (?,?,?,?)", txn)
                self.conn.execute("DELETE FROM split_index WHERE txn=?", (txn[0],))
                self.conn.executemany("INSERT INTO split_index VALUES (?,?,?,?,?,?,?,?)", splits)

    def load_all(self) -> list[Transaction]:
        rows = self.conn.execute("SELECT blob FROM txn ORDER BY post_date, handle")
        return [Transaction.from_dict(json.loads(blob)) for (blob,) in rows]

    def register(self, account: str) -> list[Transaction]:
        rows = self.conn.execute(
            "SELECT DISTINCT t.handle, t.post_date, t.blob FROM txn t JOIN split_index s"
            " ON s.txn = t.handle WHERE s.account=? ORDER BY t.post_date, t.handle",
            (account,),
        )
        return [Transaction.from_dict(json.loads(row[2])) for row in rows]

    def balance(self, account: str) -> Money:
        total = Money(0)
        for num, den in self.conn.execute(
            "SELECT value_num, value_den FROM split_index WHERE account=?", (account,)
        ):
            total = total + Money(num, den)
        return total


class NormalizedLayout(Layout):
    name = "normalized tables (candidate)"
    schema = NORMALIZED_SCHEMA

    def write(self, transactions: Iterable[dict]) -> None:
        with self.conn:
            for data in transactions:
                txn, splits = _normalized_rows(data)
                self.conn.execute("DELETE FROM split_n WHERE txn=?", (txn[0],))
                self.conn.execute("INSERT OR REPLACE INTO txn_n VALUES (?,?,?,?,?,?,?,?)", txn)
                self.conn.executemany(
                    "INSERT INTO split_n VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", splits
                )

    def _assemble(self, txn_rows: list[tuple], split_rows: Iterable[tuple]) -> list[Transaction]:
        splits: dict[str, list[dict]] = {}
        for row in split_rows:
            split = {
                "handle": row[0],
                "account": row[3],
                "value": [row[4], row[5]],
                "quantity": [row[6], row[7]],
                "memo": row[8],
                "action": row[9],
                "reconcile": row[10],
                "reconcile_date": row[11],
                **(json.loads(row[12]) if row[12] != "{}" else {}),
            }
            splits.setdefault(row[1], []).append(split)
        result = []
        for row in txn_rows:
            data = {
                "handle": row[0],
                "post_date": row[1],
                "enter_date": row[2],
                "num": row[3],
                "description": row[4],
                "currency": row[5],
                "notes": row[6],
                **(json.loads(row[7]) if row[7] != "{}" else {}),
                "splits": splits.get(row[0], []),
            }
            result.append(Transaction.from_dict(data))
        return result

    def load_all(self) -> list[Transaction]:
        txns = self.conn.execute("SELECT * FROM txn_n ORDER BY post_date, handle").fetchall()
        splits = self.conn.execute("SELECT * FROM split_n ORDER BY txn, position")
        return self._assemble(txns, splits)

    def register(self, account: str) -> list[Transaction]:
        txns = self.conn.execute(
            "SELECT * FROM txn_n WHERE handle IN (SELECT txn FROM split_n WHERE account=?)"
            " ORDER BY post_date, handle",
            (account,),
        ).fetchall()
        splits = self.conn.execute(
            "SELECT * FROM split_n WHERE txn IN (SELECT txn FROM split_n WHERE account=?)"
            " ORDER BY txn, position",
            (account,),
        )
        return self._assemble(txns, splits)

    def balance(self, account: str) -> Money:
        total = Money(0)
        for num, den in self.conn.execute(
            "SELECT value_num, value_den FROM split_n WHERE account=?", (account,)
        ):
            total = total + Money(num, den)
        return total


def best(operation: Callable[[], object], repeat: int) -> float:
    timings = []
    for _ in range(repeat):
        started = time.perf_counter()
        operation()
        timings.append(time.perf_counter() - started)
    return min(timings)


def measure(count: int, repeat: int) -> list[tuple[str, dict[str, float]]]:
    accounts, transactions = household(count)
    serialized = [transaction.serialize() for transaction in transactions]
    edits = serialized[-50:]
    results = []
    with tempfile.TemporaryDirectory() as folder:
        for layout_type in (BlobLayout, NormalizedLayout):
            path = Path(folder) / f"{layout_type.__name__}.sqlite"
            layout = layout_type(path)
            started = time.perf_counter()
            layout.write(serialized)
            bulk = time.perf_counter() - started
            layout.conn.execute("VACUUM")
            size = os.path.getsize(path)
            loaded = layout.load_all()
            assert [t.serialize() for t in loaded] == sorted(
                serialized, key=lambda item: (item["post_date"], item["handle"])
            ), f"{layout.name} does not round-trip"
            single = []
            for data in edits:
                started = time.perf_counter()
                layout.write([data])
                single.append(time.perf_counter() - started)
            busiest = accounts[0]
            figures = {
                "file size (MiB)": size / 2**20,
                "bulk write (s)": bulk,
                "load every transaction (s)": best(layout.load_all, repeat),
                "busiest register (s)": best(partial(layout.register, busiest), repeat),
                "account balance (ms)": best(partial(layout.balance, busiest), repeat) * 1000,
                "one edit commit (ms, median)": statistics.median(single) * 1000,
            }
            results.append((layout.name, figures))
            layout.close()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--transactions", type=int, default=30_000)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args(argv)
    results = measure(args.transactions, args.repeat)
    (current_name, current), (candidate_name, candidate) = results
    print(f"{args.transactions} transactions, best of {args.repeat}\n")
    print(f"| measure | {current_name} | {candidate_name} | candidate / current |")
    print("|---|---:|---:|---:|")
    for key in current:
        ratio = candidate[key] / current[key] if current[key] else float("nan")
        print(f"| {key} | {current[key]:.3f} | {candidate[key]:.3f} | {ratio:.2f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
