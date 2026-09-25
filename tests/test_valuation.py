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


def test_direct_currency_conversion_is_exact_and_as_of_with_quote_evidence(db, book):
    usd = db.get_commodity_by_mnemonic("USD")
    assert usd is not None
    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    old = CommodityPrice(
        commodity=euro.handle,
        currency=usd.handle,
        quote_date=date(2026, 2, 1),
        value=Money(4, 3),
        source="imported-book",
        quote_type="last",
    )
    future = CommodityPrice(
        commodity=euro.handle,
        currency=usd.handle,
        quote_date=date(2026, 3, 1),
        value=Money(3, 2),
    )
    with db.transaction("Currency quotes") as txn:
        db.add_commodity(euro, txn)
        db.add_price(old, txn)
        db.add_price(future, txn)

    source = Amount(Money(1), euro.handle)
    missing = valuation.convert_currency(db, source, as_of=date(2026, 1, 31))
    converted = valuation.convert_currency(db, source, as_of=date(2026, 2, 15))
    current = valuation.convert_currency(db, source, as_of=date(2026, 3, 2))

    assert missing.missing_quote and missing.amount is None
    assert missing.quote_date is None
    assert (missing.source_currency, missing.target_currency) == (euro.handle, usd.handle)
    assert converted.amount == Amount(Money(4, 3), usd.handle)
    assert converted.quote_date == date(2026, 2, 1)
    assert converted.quote_source == "imported-book"
    assert converted.quote_type == "last"
    assert current.amount == Amount(Money(3, 2), usd.handle)
    assert source == Amount(Money(1), euro.handle)


def test_currency_conversion_requires_a_direct_quote_and_valid_currency_units(db, book):
    usd = db.get_commodity_by_mnemonic("USD")
    assert usd is not None
    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    pound = Commodity(namespace="CURRENCY", mnemonic="GBP", fullname="Pound")
    fund = Commodity(namespace="FUND", mnemonic="INDEX", fullname="Index fund")
    with db.transaction("Currency units") as txn:
        for commodity in (euro, pound, fund):
            db.add_commodity(commodity, txn)
        db.add_price(
            CommodityPrice(
                commodity=usd.handle,
                currency=euro.handle,
                quote_date=date(2026, 1, 1),
                value=Money("0.8"),
            ),
            txn,
        )
        db.add_price(
            CommodityPrice(
                commodity=euro.handle,
                currency=pound.handle,
                quote_date=date(2026, 1, 1),
                value=Money("0.9"),
            ),
            txn,
        )

    same = valuation.convert_currency(db, Amount(Money("2.50"), usd.handle))
    assert same.amount == Amount(Money("2.50"), usd.handle)
    assert same.quote_date is None and not same.missing_quote
    assert valuation.convert_currency(db, Amount(Money(2), euro.handle)).missing_quote
    with pytest.raises(ValueError, match="source must be a known currency"):
        valuation.convert_currency(db, Amount(Money(1), fund.handle))
    with pytest.raises(ValueError, match="target must be a known currency"):
        valuation.convert_currency(db, Amount(Money(1), euro.handle), currency=fund)


def test_foreign_currency_account_value_exposes_direct_quote_or_ledger_fallback(db, book):
    usd = db.get_commodity_by_mnemonic("USD")
    assert usd is not None
    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    account = Account(
        name="Foreign cash", atype=AccountType.BANK, parent=book.assets, commodity=euro.handle
    )
    transaction = Transaction(post_date=date(2026, 1, 2), description="Opening cash")
    transaction.currency = euro.handle
    transaction.splits = [
        Split(account.handle, Money(10)),
        Split(book.opening, Money(-10)),
    ]
    quote = CommodityPrice(
        commodity=euro.handle,
        currency=usd.handle,
        quote_date=date(2026, 2, 1),
        value=Money(4, 3),
        source="imported-book",
    )
    with db.transaction("Foreign balance") as txn:
        db.add_commodity(euro, txn)
        db.add_account(account, txn)
        db.add_transaction(transaction, txn)
        db.add_price(quote, txn)

    before = valuation.account_value(db, account, as_of=date(2026, 1, 31))
    after = valuation.account_value(db, account, as_of=date(2026, 2, 2))
    assert before.missing_quote
    assert before.total_amount == Amount(Money(10), euro.handle)
    assert before.total == Money(10) and before.price_date is None
    assert after.source == "currency" and not after.missing_quote
    assert after.total_amount == Amount(Money(40, 3), usd.handle)
    assert after.price_date == date(2026, 2, 1)
    assert after.price_source == "imported-book"
    assert ledger.balance_amount(db, account) == Amount(Money(10), euro.handle)


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
