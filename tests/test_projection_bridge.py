"""The projection's conservation identity, stated term by term for people."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine import projection
from breadsched.gen.engine.projection_bridge import (
    month_bridges,
    projection_bridges,
    range_bridges,
)
from breadsched.gen.lib import (
    Account,
    AccountType,
    Assumptions,
    Money,
    Recurrence,
    Scenario,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)
from breadsched.gen.sample_book import create_sample_book
from breadsched.gen.services import SaveSchedule, save_schedule


def _add_investments_and_a_loan(db):
    """A 401(k) funded monthly from checking, and an interest-bearing loan paid down."""
    checking = db.get_account_by_name("Assets:Checking").handle
    assets = db.get_account_by_name("Assets").handle
    liabilities = db.get_account_by_name("Liabilities").handle
    opening = db.get_account_by_name("Equity:Opening balances").handle
    retirement = Account(name="401k", atype=AccountType.RETIREMENT, parent=assets)
    loan = Account(name="Car loan", atype=AccountType.LOAN, parent=liabilities)
    loan.annual_interest = Decimal("0.06")
    with db.transaction("Bridge accounts") as txn:
        db.add_account(retirement, txn)
        db.add_account(loan, txn)
        db.add_transaction(
            Transaction.simple(
                date(2026, 8, 1), "Opening 401k", retirement.handle, opening, "20000"
            ),
            txn,
        )
        db.add_transaction(
            Transaction.simple(date(2026, 8, 1), "Car loan", opening, loan.handle, "10000"),
            txn,
        )
    for name, account, amount in (
        ("401k contribution", retirement.handle, "500"),
        ("Car payment", loan.handle, "300"),
    ):
        saved = save_schedule(
            db,
            SaveSchedule(
                ScheduledTransaction(
                    name=name,
                    recurrence=Recurrence(start=date(2026, 9, 15)),
                    splits=[
                        ScheduledSplit(account, Money(amount)),
                        ScheduledSplit(checking, Money(f"-{amount}")),
                    ],
                )
            ),
        )
        assert saved.ok, saved.errors


@pytest.fixture
def result(tmp_path):
    path = tmp_path / "sample.breadsched"
    create_sample_book(path, as_of=date(2026, 9, 1))
    db = DbSQLite()
    db.load(str(path))
    _add_investments_and_a_loan(db)
    try:
        scenario = Scenario(
            name="Bridge",
            start=date(2026, 9, 1),
            years=2,
            assumptions=Assumptions(investment_return="0.06", cash_interest="0.02"),
        )
        yield projection.project(db, scenario)
    finally:
        db.close()


def test_every_month_and_the_whole_horizon_reconcile_exactly(result):
    for row in result.rows:
        cash, investments, debts, worth = month_bridges(result, row.index)
        assert all(item.reconciles for item in (cash, investments, debts, worth))
        assert cash.opening == row.cash_open and cash.closing == row.cash_close
        assert investments.closing == row.holdings
        assert debts.closing == row.liabilities
        assert worth.closing == row.net_worth
    cash, investments, debts, worth = projection_bridges(result)
    assert worth.reconciles and worth.unexplained == Money(0)
    assert worth.opening == result.rows[0].ledger.opening_cash + (
        result.rows[0].ledger.holdings_open - result.rows[0].ledger.liabilities_open
    )
    assert worth.closing == result.rows[-1].net_worth
    # The sample grows investments and charges mortgage interest, so each effect
    # is a visible, non-zero term rather than folded into flows.
    terms = {term.key: term.amount for term in worth.changes}
    assert terms["performance"] > 0
    assert terms["cash_interest"] > 0
    assert terms["debt_interest"] < 0
    planned = {term.key: term.amount for term in investments.changes}["planned"]
    assert planned == Money("12000.00")  # 24 contributions of 500
    principal = {term.key: term.amount for term in debts.changes}["principal"]
    assert principal == Money("-7200.00")  # 24 payments of 300 reduce the debt
    # Contributions and payments leave cash but not net worth.
    assert terms["planned"] == cash.changes[0].amount + planned - principal


def test_a_range_sums_its_months(result):
    first = month_bridges(result, 0)
    second = month_bridges(result, 1)
    both = range_bridges([result.rows[0].ledger, result.rows[1].ledger])
    for one, two, combined in zip(first, second, both, strict=True):
        assert combined.opening == one.opening
        assert combined.closing == two.closing
        for term_one, term_two, term in zip(
            one.changes, two.changes, combined.changes, strict=True
        ):
            assert term.amount == term_one.amount + term_two.amount
        assert combined.as_dict()["reconciles"] is True


def test_a_broken_ledger_shows_its_unexplained_difference(result):
    import dataclasses

    ledger = dataclasses.replace(
        result.rows[0].ledger, closing_cash=result.rows[0].ledger.closing_cash + Money("1.00")
    )
    cash = range_bridges([ledger])[0]
    assert not cash.reconciles
    assert cash.unexplained == Money("1.00")
    with pytest.raises(IndexError):
        month_bridges(result, len(result.rows))


def test_cli_prints_and_serializes_the_bridges(tmp_path, capsys):
    import json

    from breadsched.cli.main import main

    path = str(tmp_path / "book.breadsched")
    assert main(["sample", path]) == 0
    capsys.readouterr()
    assert main(["project", path, "--years", "1", "--start", "2026-09-01", "--bridge"]) == 0
    text = capsys.readouterr().out
    assert "How the projection reconciles, Sep 2026 through Aug 2027:" in text
    assert "Planned events, net of transfers" in text
    assert text.count("Unexplained") == 4

    assert (
        main(
            [
                "project",
                path,
                "--years",
                "1",
                "--start",
                "2026-09-01",
                "--bridge",
                "2026-12",
                "--json",
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert data["bridge_period"] == "Dec 2026"
    assert [item["key"] for item in data["bridges"]] == [
        "cash",
        "investments",
        "debts",
        "net_worth",
    ]
    assert all(item["reconciles"] for item in data["bridges"])
    december = next(row for row in data["rows"] if row["label"] == "Dec 2026")
    assert data["bridges"][3]["closing"] == december["net_worth"]

    assert main(["project", path, "--start", "2026-09-01", "--bridge", "1999-01"]) == 2
    assert "no projected month '1999-01'" in capsys.readouterr().err
