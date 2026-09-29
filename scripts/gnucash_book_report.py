"""Report what real GnuCash reads from a book, as JSON (for write-back checks).

Run with a Python that has GnuCash's own bindings (``python3-gnucash``)::

    /usr/bin/python3 scripts/gnucash_book_report.py path/to/book.gnucash

The book is opened read-only through GnuCash's engine, the way GnuCash itself
loads it. The report lists every transaction with its splits, every account's
balance, and any transaction GnuCash finds out of balance, so a test can compare
GnuCash's reading of a written book with BreadSched's.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _number(value) -> str:
    return f"{value.num()}/{value.denom()}"


def main(path: str) -> int:
    from gnucash import Session, SessionOpenMode

    book_path = Path(path).resolve()
    with open(book_path, "rb") as handle:
        scheme = "sqlite3" if handle.read(16).startswith(b"SQLite format 3") else "xml"
    session = Session(f"{scheme}://{book_path}", SessionOpenMode.SESSION_READ_ONLY)
    try:
        root = session.book.get_root_account()
        transactions: dict[str, dict] = {}
        balances: dict[str, str] = {}
        imbalanced: list[str] = []
        for account in root.get_descendants():
            name = account.get_full_name()
            balances[name] = _number(account.GetBalance())
            for split in account.GetSplitList():
                txn = split.GetParent()
                guid = txn.GetGUID().to_string()
                if guid in transactions:
                    continue
                if not txn.GetImbalanceValue().zero_p():
                    imbalanced.append(guid)
                transactions[guid] = {
                    "date": txn.GetDate().strftime("%Y-%m-%d"),
                    "description": txn.GetDescription(),
                    "num": txn.GetNum(),
                    "currency": txn.GetCurrency().get_mnemonic(),
                    "splits": sorted(
                        (
                            {
                                "guid": item.GetGUID().to_string(),
                                "account": item.GetAccount().get_full_name(),
                                "value": _number(item.GetValue()),
                                "quantity": _number(item.GetAmount()),
                                "memo": item.GetMemo(),
                                "action": item.GetAction(),
                                "reconcile": item.GetReconcile(),
                            }
                            for item in txn.GetSplitList()
                        ),
                        key=lambda row: row["guid"],
                    ),
                }
        report = {"transactions": transactions, "balances": balances, "imbalanced": imbalanced}
    finally:
        session.end()
    json.dump(report, sys.stdout, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
