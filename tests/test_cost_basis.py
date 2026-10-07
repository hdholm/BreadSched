"""Cost basis and gains derived first-in, first-out from a security's ledger splits."""

from __future__ import annotations

from datetime import date
from fractions import Fraction

from breadsched.gen.engine.cost_basis import cost_basis, holdings_cost_basis, shares_text
from breadsched.gen.lib import (
    Account,
    AccountType,
    Commodity,
    CommodityPrice,
    Money,
    Split,
    Transaction,
)


def _fund(db, book):
    usd = db.get_commodity_by_mnemonic("USD")
    add_usd = usd is None
    if usd is None:
        usd = Commodity(namespace="CURRENCY", mnemonic="USD", fullname="US Dollar")
    fund = Commodity(namespace="FUND", mnemonic="INDEX", fullname="Index fund", fraction=1000)
    account = Account(
        name="Index holding",
        atype=AccountType.INVESTMENT,
        parent=book.assets,
        commodity=fund.handle,
        commodity_scu=1000,
    )
    with db.transaction("Security") as txn:
        if add_usd:
            db.add_commodity(usd, txn)
        db.add_commodity(fund, txn)
        db.add_account(account, txn)
    return account, fund, usd


def _trade(db, account, usd, other, when, quantity, value, description="Trade"):
    trade = Transaction(post_date=when, description=description)
    trade.currency = usd.handle
    trade.splits = [
        Split(account.handle, Money(value), quantity=Money(quantity)),
        Split(other, -Money(value)),
    ]
    with db.transaction(description) as txn:
        db.add_transaction(trade, txn)
    return trade


def _price(db, fund, usd, when, value):
    with db.transaction("Price") as txn:
        db.add_price(
            CommodityPrice(
                commodity=fund.handle, currency=usd.handle, quote_date=when, value=Money(value)
            ),
            txn,
        )


def test_sales_take_the_oldest_shares_first(db, book):
    account, fund, usd = _fund(db, book)
    _trade(db, account, usd, book.checking, date(2024, 1, 10), "10", "1000")
    _trade(db, account, usd, book.checking, date(2025, 1, 10), "10", "1500")
    _trade(db, account, usd, book.checking, date(2026, 2, 1), "-15", "-2400")
    _price(db, fund, usd, date(2026, 3, 1), "170")

    result = cost_basis(db, account, as_of=date(2026, 3, 31))

    [sale] = result.sales
    # 10 shares at 100 and 5 of the next 10 at 150.
    assert (sale.quantity, sale.cost, sale.proceeds, sale.gain) == (
        Money(15),
        Money("1750"),
        Money("2400"),
        Money("650"),
    )
    [lot] = result.lots
    assert (lot.acquired, lot.quantity, lot.cost) == (date(2025, 1, 10), Money(5), Money("750"))
    assert (result.quantity, result.cost) == (Money(5), Money("750"))
    assert result.market_value == Money("850")
    assert result.unrealized_gain == Money("100")
    assert result.realized_by_year() == {2026: Money("650")}
    assert result.problems == ()


def test_cost_shares_and_gains_stay_exact_over_thirds(db, book):
    account, fund, usd = _fund(db, book)
    _trade(db, account, usd, book.checking, date(2025, 1, 1), "3", "100")
    _trade(db, account, usd, book.checking, date(2025, 6, 1), "-1", "-40")
    _trade(db, account, usd, book.checking, date(2025, 9, 1), "-1", "-40")

    result = cost_basis(db, account, as_of=date(2025, 12, 31))

    third = Money(100) * Fraction(1, 3)
    assert [sale.cost for sale in result.sales] == [third, third]
    total_cost = sum((sale.cost for sale in result.sales), Money(0)) + result.cost
    assert total_cost == Money(100)
    assert result.realized_by_year() == {2025: Money(80) - third * 2}


def test_unknown_costs_are_named_not_guessed(db, book):
    account, fund, usd = _fund(db, book)
    _trade(db, account, usd, book.checking, date(2025, 1, 1), "2", "200")
    _trade(db, account, usd, book.opening, date(2025, 2, 1), "2", "0", "Share split")
    _trade(db, account, usd, book.checking, date(2025, 3, 1), "-6", "-600")

    result = cost_basis(db, account)

    [sale] = result.sales
    assert (sale.uncovered, sale.cost, sale.proceeds) == (Money(2), Money(200), Money(400))
    assert result.lots == ()
    assert result.problems == (
        "2 shares arrived with no recorded cost (a transfer in or a share split); their "
        "cost basis is zero",
        "2 shares were sold beyond the recorded purchases; their cost and gain are unknown",
    )


def test_without_a_quote_there_is_no_unrealized_gain(db, book):
    account, _fund_commodity, usd = _fund(db, book)
    _trade(db, account, usd, book.checking, date(2025, 1, 1), "4", "400")

    result = cost_basis(db, account)

    assert result.cost == Money(400)
    assert result.market_value is None and result.unrealized_gain is None
    assert result.problems == ("no market quote, so there is no unrealized gain",)


def test_only_security_accounts_with_activity_are_listed(db, book):
    account, fund, usd = _fund(db, book)
    assert holdings_cost_basis(db) == []
    assert cost_basis(db, book.checking) is None
    _trade(db, account, usd, book.checking, date(2025, 1, 1), "1.5", "150")

    [holding] = holdings_cost_basis(db)
    assert holding.account.handle == account.handle
    assert shares_text(holding.quantity) == "1.5"
    data = holding.as_dict()
    assert data["quantity"] == "1.5" and data["lots"][0]["cost"] == Money(150)


def test_web_lists_holdings_and_refuses_a_bad_date(db, book):
    import pytest

    from breadsched.web.context import Api
    from breadsched.web.holdings_resource import holdings
    from breadsched.web.resources import QueryError, QueryParams

    account, fund, usd = _fund(db, book)
    _trade(db, account, usd, book.checking, date(2025, 1, 1), "4", "400")
    _price(db, fund, usd, date(2025, 6, 1), "110")

    payload = holdings(Api(db), QueryParams("as_of=2025-12-31"))
    [item] = payload["holdings"]
    assert (item["name"], item["quantity"], item["unrealized_gain"]) == (
        "Assets:Index holding",
        "4",
        Money(40),
    )
    assert item["text"] == "4 shares cost 400.00; worth 440.00; unrealized gain 40.00."
    with pytest.raises(QueryError):
        holdings(Api(db), QueryParams("as_of=soon"))


def test_cli_prints_lots_and_json(db, book, tmp_path, capsys):
    import json

    from breadsched.cli.main import main
    from breadsched.gen.db.sqlite import DbSQLite

    path = tmp_path / "holdings.breadsched"
    db.backup_to(str(path))
    file_db = DbSQLite()
    file_db.load(str(path))
    try:
        account, _fund_commodity, usd = _fund(file_db, book)
        _trade(file_db, account, usd, book.checking, date(2025, 1, 1), "2", "200")
        _trade(file_db, account, usd, book.checking, date(2025, 5, 1), "-1", "-150")
    finally:
        file_db.close()

    assert main(["holdings", str(path), "--lots"]) == 0
    text = capsys.readouterr().out
    assert "Assets:Index holding: 1 shares cost 100.00; realized 2025: 50.00." in text
    assert "sold 2025-05-01: 1 shares for 150.00, cost 100.00, gain 50.00" in text
    assert "note: no market quote, so there is no unrealized gain" in text
    assert main(["holdings", str(path), "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert row["realized_by_year"] == {"2025": "50.00"}
