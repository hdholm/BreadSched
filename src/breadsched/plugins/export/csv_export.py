"""CSV export.

Amounts are written as plain decimal strings with no thousands separators and no
currency symbol, because the destination is a spreadsheet that needs to parse them
as numbers, not a human reading a report.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from ...gen.db.sqlite import DbSQLite
from ...gen.engine.projection import Projection

__all__ = ["export_transactions", "export_projection", "export_budget_report"]


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
        "debt_payments",
        "interest_earned",
        "investment_growth",
        "interest_charged",
        "cash_close",
        "holdings",
        "liabilities",
        "net_worth",
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


def export_budget_report(report, path: str | Path) -> int:
    """Budget versus actual, one row per account with a column pair per period."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        header = ["account", "class"]
        for label in report.labels:
            header += [f"{label} budget", f"{label} actual"]
        header += ["total budget", "total actual", "variance"]
        writer.writerow(header)
        for line in report.lines:
            row = [line.name, line.account_class.value]
            for period in line.periods:
                row += [str(period.budgeted.to_decimal()), str(period.actual.to_decimal())]
            row += [
                str(line.budgeted_total.to_decimal()),
                str(line.actual_total.to_decimal()),
                str(line.variance_total.to_decimal()),
            ]
            writer.writerow(row)
    return len(report.lines)
