"""Exact dated security valuation without rewriting ledger history."""

from datetime import date

import pytest

from breadsched.gen.engine import dashboard, ledger, projection, valuation
from breadsched.gen.lib import (
    Account,
    AccountType,
    Amount,
    Commodity,
    CommodityPrice,
    Money,
    Scenario,
    Split,
    Transaction,
)


def _holding(db, book):
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
    purchase = Transaction(post_date=date(2026, 1, 2), description="Opening holding")
    purchase.currency = usd.handle
    purchase.splits = [
        Split(account.handle, Money("1000"), quantity=Money("10")),
        Split(book.opening, Money("-1000")),
    ]
    with db.transaction("Security holding") as txn:
        if add_usd:
            db.add_commodity(usd, txn)
        db.add_commodity(fund, txn)
        db.add_account(account, txn)
        db.add_transaction(purchase, txn)
    return account, fund, usd


def test_latest_as_of_price_marks_units_to_market(db, book):
    account, fund, usd = _holding(db, book)
    old = CommodityPrice(
        commodity=fund.handle,
        currency=usd.handle,
        quote_date=date(2026, 2, 1),
        value=Money("120"),
        source="imported-book",
    )
    current = CommodityPrice(
        commodity=fund.handle,
        currency=usd.handle,
        quote_date=date(2026, 3, 1),
        value=Money("125"),
    )
    with db.transaction("Prices") as txn:
        db.add_price(old, txn)
        db.add_price(current, txn)

    before = valuation.account_value(db, account, as_of=date(2026, 2, 15))
    after = valuation.account_value(db, account, as_of=date(2026, 3, 15))

    assert ledger.balance(db, account) == Money("1000")
    assert before.total == Money("1200")
    assert after.total == Money("1250")
    assert after.quantity == Money("10")
    assert after.price == Money("125")
    assert after.price_date == date(2026, 3, 1)
    assert after.source == "market"
    assert before.price_source == "imported-book"
    assert not before.missing_quote
    assert after.price_source == "breadsched"
    assert after.quantity_amount == Amount(Money("10"), fund.handle)
    assert after.total_amount == Amount(Money("1250"), usd.handle)


def test_dated_price_rejects_units_of_another_commodity(db, book):
    _account, fund, usd = _holding(db, book)
    price = CommodityPrice(
        commodity=fund.handle,
        currency=usd.handle,
        quote_date=date(2026, 3, 1),
        value=Money("125"),
    )

    with pytest.raises(ValueError, match="does not match"):
        price.convert(Amount(Money("10"), "some-other-security"), fraction=usd.fraction)


def test_missing_quote_falls_back_to_the_book_value(db, book):
    account, fund, _usd = _holding(db, book)

    result = valuation.account_value(db, account, as_of=date(2026, 3, 1))

    assert result.total == Money("1000")
    assert result.source == "ledger"
    assert result.commodity == fund
    assert result.missing_quote
    assert result.price_source is None


def test_dashboard_uses_market_value_and_explains_the_quote(db, book):
    account, fund, usd = _holding(db, book)
    with db.transaction("Price") as txn:
        db.add_price(
            CommodityPrice(
                commodity=fund.handle,
                currency=usd.handle,
                quote_date=date(2026, 3, 1),
                value=Money("125"),
            ),
            txn,
        )
    config = dashboard.DashboardConfig(
        groups=[dashboard.GroupConfig("Investments", [account.handle], "asset")]
    )

    group = dashboard.build(db, config, as_of=date(2026, 3, 15)).group("Investments")

    assert group is not None
    assert group.total == Money("1250")
    assert group.accounts[0].source == "market"
    assert "10.00 INDEX at 125.00 USD as of 2026-03-01" in group.accounts[0].note


def test_price_crud_is_undoable_and_verified(db, book):
    _account, fund, usd = _holding(db, book)
    price = CommodityPrice(
        commodity=fund.handle,
        currency=usd.handle,
        quote_date=date(2026, 3, 1),
        value=Money("125.25"),
    )
    with db.transaction("Price") as txn:
        db.add_price(price, txn)

    assert db.get_price(price.handle) == price
    assert list(db.iter_prices(commodity=fund.handle))[0] == price
    assert db.verify_book() == []
    assert db.undo()
    assert db.get_price(price.handle) is None


def test_user_entry_updates_same_day_quote_instead_of_duplicating_it(db):
    security, first = valuation.save_security_price(
        db,
        security_handle=None,
        currency_handle=None,
        quote_date=date(2026, 3, 1),
        value=Money("125"),
        mnemonic="INDEX",
        fullname="Index fund",
    )
    usd = db.get_commodity_by_mnemonic("USD")
    assert usd is not None
    _security, second = valuation.save_security_price(
        db,
        security_handle=security.handle,
        currency_handle=usd.handle,
        quote_date=date(2026, 3, 1),
        value=Money("126.50"),
    )

    assert first.handle == second.handle
    assert [item.value for item in db.iter_prices()] == [Money("126.50")]


def test_projection_starts_from_as_of_market_value(db, book):
    account, fund, usd = _holding(db, book)
    with db.transaction("Price") as txn:
        db.add_price(
            CommodityPrice(
                commodity=fund.handle,
                currency=usd.handle,
                quote_date=date(2026, 2, 28),
                value=Money("125"),
            ),
            txn,
        )
    scenario = Scenario(name="Market opening", start=date(2026, 3, 1), years=1)

    result = projection.project(db, scenario)

    assert result.rows[0].ledger.opening_holdings[account.handle] == Money("1250")
