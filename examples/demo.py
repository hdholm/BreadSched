#!/usr/bin/env python3
"""End-to-end demonstration.

Builds a GnuCash book, imports it, budgets against it, adds a schedule, saves two
scenarios, and prints twenty-year forecasts under both. Run it from the repository
root with ``python examples/demo.py``.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
# The GnuCash book builder is shared with the test suite rather than duplicated.
sys.path.insert(0, str(ROOT / "tests"))

from gnucash_fixtures import (  # noqa: E402
    GNUCASH_SCHEMA,
    write_account,
    write_commodity,
    write_transaction,
)

from breadsched.gen.db.sqlite import DbSQLite  # noqa: E402
from breadsched.gen.engine import cashflow, ledger, projection, schedule  # noqa: E402
from breadsched.gen.lib import (  # noqa: E402
    Assumptions,
    Budget,
    Money,
    PeriodType,
    ProjectionBasis,
    Recurrence,
    Scenario,
    ScheduledSplit,
    ScheduledTransaction,
)
from breadsched.plugins.importer import gnucash_sqlite  # noqa: E402


def build_gnucash_book(path: Path) -> None:
    """Write a small but realistic GnuCash SQLite book."""
    conn = sqlite3.connect(path)
    conn.executescript(GNUCASH_SCHEMA)
    usd = uuid.uuid4().hex
    write_commodity(conn, usd)

    ids = {name: uuid.uuid4().hex for name in (
        "root", "assets", "checking", "savings", "brokerage", "liab", "card",
        "income", "salary", "expenses", "rent", "food", "utilities", "equity",
        "opening",
    )}
    tree = [
        ("root", "Root Account", "ROOT", None, 1),
        ("assets", "Assets", "ASSET", "root", 1),
        ("checking", "Checking Account", "BANK", "assets", 0),
        ("savings", "Savings", "BANK", "assets", 0),
        ("brokerage", "Brokerage", "MUTUAL", "assets", 0),
        ("liab", "Liabilities", "LIABILITY", "root", 1),
        ("card", "Credit Card", "CREDIT", "liab", 0),
        ("income", "Income", "INCOME", "root", 1),
        ("salary", "Salary", "INCOME", "income", 0),
        ("expenses", "Expenses", "EXPENSE", "root", 1),
        ("rent", "Rent", "EXPENSE", "expenses", 0),
        ("food", "Groceries", "EXPENSE", "expenses", 0),
        ("utilities", "Utilities", "EXPENSE", "expenses", 0),
        ("equity", "Equity", "EQUITY", "root", 1),
        ("opening", "Opening Balances", "EQUITY", "equity", 0),
    ]
    for key, name, atype, parent, placeholder in tree:
        write_account(
            conn, ids[key], name, atype,
            ids[parent] if parent else None, usd, placeholder=placeholder,
        )

    write_transaction(conn, uuid.uuid4().hex, usd, date(2025, 12, 31), "Opening balances", [
        (ids["checking"], 850000, 100, ""),
        (ids["savings"], 1500000, 100, ""),
        (ids["brokerage"], 8200000, 100, ""),
        (ids["card"], -230000, 100, ""),
        (ids["opening"], -10320000, 100, ""),
    ])
    for month in (1, 2, 3):
        write_transaction(conn, uuid.uuid4().hex, usd, date(2026, month, 25), "Payroll", [
            (ids["checking"], 620000, 100, ""),
            (ids["salary"], -620000, 100, ""),
        ])
        write_transaction(conn, uuid.uuid4().hex, usd, date(2026, month, 1), "Rent", [
            (ids["rent"], 210000, 100, ""),
            (ids["checking"], -210000, 100, ""),
        ])
        write_transaction(conn, uuid.uuid4().hex, usd, date(2026, month, 12), "Supermarket", [
            (ids["food"], 68000 + month * 1500, 100, ""),
            (ids["checking"], -(68000 + month * 1500), 100, ""),
        ])
    conn.commit()
    conn.close()


def rule(title: str) -> None:
    print(f"\n{title}\n{'=' * len(title)}")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="breadsched-demo-"))
    source = workdir / "gnucash-source.gnucash"
    build_gnucash_book(source)

    db = DbSQLite()
    db.load(str(workdir / "household.breadsched"))

    rule("1. Import the GnuCash book")
    result = gnucash_sqlite.import_book(db, source)
    print(f"   {result}")
    print(f"   Cash on hand:  {ledger.cash_on_hand(db).format('$')}")
    print(f"   Net worth:     {ledger.net_worth(db).format('$')}")

    accounts = {db.full_name(a): a.handle for a in db.iter_accounts()}
    checking = accounts["Assets:Checking Account"]
    brokerage = accounts["Assets:Brokerage"]

    rule("2. Register for the checking account")
    for row in ledger.register(db, checking)[:6]:
        print(f"   {row.post_date}  {row.description:<20} "
              f"{row.amount.format('$'):>12}  {row.running.format('$'):>12}")

    rule("3. Budget versus actual, first quarter")
    budget = Budget(name="2026", start=date(2026, 1, 1), periods=12)
    budget.set_monthly(accounts["Income:Salary"], "6200.00")
    budget.set_monthly(accounts["Expenses:Rent"], "2100.00")
    budget.set_monthly(accounts["Expenses:Groceries"], "700.00")
    budget.set_monthly(accounts["Expenses:Utilities"], "180.00")
    budget.set_amount(accounts["Expenses:Utilities"], 0, "340.00")
    budget.set_amount(accounts["Expenses:Utilities"], 1, "320.00")
    with db.transaction("Add 2026 budget") as txn:
        db.add_budget(budget, txn)

    report = cashflow.build_report(db, budget)
    print(f"   {'account':<24}{'budgeted':>12}{'actual':>12}{'variance':>12}")
    for line in report.lines:
        first_quarter = line.periods[:3]
        budgeted = sum((p.budgeted for p in first_quarter), Money(0))
        actual = sum((p.actual for p in first_quarter), Money(0))
        print(f"   {line.name:<24}{budgeted.format('$'):>12}"
              f"{actual.format('$'):>12}{(actual - budgeted).format('$'):>12}")
    print(f"   Planned monthly surplus: {report.net_cash_flow(3).format('$')}")

    rule("4. Add a scheduled monthly investment")
    with db.transaction("Add investing schedule") as txn:
        db.add_scheduled(
            ScheduledTransaction(
                name="Monthly investment",
                recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 4, 1)),
                splits=[
                    ScheduledSplit(brokerage, Money("1500.00")),
                    ScheduledSplit(checking, Money("-1500.00")),
                ],
                auto_create=True,
            ),
            txn,
        )
    upcoming = schedule.forecast_occurrences(db, date(2026, 4, 1), date(2026, 7, 1))
    print("   Next occurrences: " + ", ".join(o.when.isoformat() for o in upcoming))

    rule("5. Save two scenarios and project twenty years")
    base = Scenario(
        name="Base case", start=date(2026, 4, 1), years=20,
        basis=ProjectionBasis.COMBINED, budget=budget.handle,
        assumptions=Assumptions(
            income_growth="0.03", expense_inflation="0.025",
            investment_return="0.06", cash_interest="0.02",
        ),
    )
    stress = Scenario(
        name="Long recession", start=date(2026, 4, 1), years=20,
        basis=ProjectionBasis.COMBINED, budget=budget.handle,
        assumptions=Assumptions(
            income_growth="0.00", expense_inflation="0.05",
            investment_return="0.01", cash_interest="0.005",
        ),
    )
    stress.add_one_off(date(2028, 6, 1), accounts["Expenses:Utilities"],
                       "18000.00", "Roof replacement")
    with db.transaction("Save scenarios") as txn:
        db.add_scenario(base, txn)
        db.add_scenario(stress, txn)

    for account_name, rate in (("Assets:Brokerage", "0.06"),):
        account = db.get_account(accounts[account_name])
        account.annual_return = Decimal(rate)
        with db.transaction("Set return assumption") as txn:
            db.commit_account(account, txn)

    print(f"   {'scenario':<18}{'yr 5 net worth':>18}{'yr 10':>16}{'yr 20':>16}"
          f"{'lowest cash':>16}")
    projections = {}
    for scenario in (base, stress):
        result = projection.project(db, scenario)
        projections[scenario.name] = result
        year_end = result.year_end("net_worth")
        print(f"   {scenario.name:<18}{year_end[4].format('$'):>18}"
              f"{year_end[9].format('$'):>16}{year_end[19].format('$'):>16}"
              f"{result.minimum_cash.format('$'):>16}")

    rule("6. What the difference is worth")
    rows = projection.compare(projections["Base case"], projections["Long recession"])
    for row in rows[11::60]:
        print(f"   {row['label']:<10} base {row['base_net_worth'].format('$'):>16}"
              f"   recession {row['other_net_worth'].format('$'):>16}"
              f"   gap {row['net_worth_delta'].format('$'):>16}")

    shortfall = projections["Long recession"].first_shortfall()
    print(f"\n   Recession case cash shortfall: "
          f"{shortfall.label if shortfall else 'none'}")

    db.close()
    print(f"\nBook written to {workdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
