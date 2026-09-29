"""CSV export.

Amounts are written as plain decimal strings with no thousands separators and no
currency symbol, because the destination is a spreadsheet that needs to parse them
as numbers, not a human reading a report.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path

from ...gen.db.sqlite import DbSQLite
from ...gen.engine.projection import Projection
from ...gen.lib.money import Money
from ...gen.services.net_worth import NetWorthChange

__all__ = [
    "export_transactions",
    "export_projection",
    "export_net_worth_change",
    "net_worth_change_csv",
]


def export_transactions(
    db: DbSQLite,
    path: str | Path,
    account: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> int:
    """One row per split.  Returns the number of rows written."""
    rows = 0
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "num", "description", "account", "memo", "amount", "reconciled"])
        for txn in db.iter_transactions(account=account, start=start, end=end):
            for split in txn.splits:
                writer.writerow(
                    [
                        txn.post_date.isoformat(),
                        txn.num,
                        txn.description,
                        db.full_name(split.account),
                        split.memo,
                        str(split.value.to_decimal()),
                        split.reconcile.value,
                    ]
                )
                rows += 1
    return rows


def export_projection(projection: Projection, path: str | Path) -> int:
    """One row per projected month."""
    fields = [
        "month",
        "cash_open",
        "income",
        "expense",
        "net_flow",
        "contributions",
        "withdrawals",
        "retirement_distributions",
        "investment_income",
        "investment_fees",
        "rollovers",
        "debt_payments",
        "interest_earned",
        "investment_growth",
        "interest_charged",
        "cash_close",
        "holdings",
        "liabilities",
        "net_worth",
        "goals_set_aside",
        "cash_after_goals",
    ]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        for row in projection.rows:
            writer.writerow(
                [row.month.isoformat()]
                + [str(getattr(row, name).to_decimal()) for name in fields[1:]]
            )
    return len(projection.rows)


def net_worth_change_csv(change: NetWorthChange) -> str:
    """The postings behind a net worth change, then the totals they reconcile to.

    A blank amount is a missing quote, never a guessed conversion; the note column
    says so.
    """

    def number(value: Money | None) -> str:
        return "" if value is None else str(value.to_decimal())

    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["date", "description", "accounts", "currency", "net worth effect", "note"])
    for posting in change.postings:
        writer.writerow(
            [
                posting.posted.isoformat(),
                posting.description,
                "; ".join(posting.accounts),
                posting.currency,
                number(posting.effect),
                "missing quote" if posting.effect is None else "",
            ]
        )
    for label, when, value in (
        ("Opening net worth", change.opening_on, change.opening),
        ("Postings", change.closing_on, change.posted),
        ("Market and exchange-rate changes", change.closing_on, change.revaluation),
        ("Closing net worth", change.closing_on, change.closing),
        ("Change", change.closing_on, change.change),
    ):
        writer.writerow(
            [
                when.isoformat(),
                label,
                "",
                "",
                number(value),
                "missing quote" if value is None else "",
            ]
        )
    return output.getvalue()


def export_net_worth_change(change: NetWorthChange, path: str | Path) -> int:
    """Write :func:`net_worth_change_csv`; returns the number of postings written."""
    Path(path).write_text(net_worth_change_csv(change), encoding="utf-8")
    return len(change.postings)
