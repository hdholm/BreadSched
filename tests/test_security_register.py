"""Registers of accounts that hold a security show and take shares (#323).

A GnuCash book keeps each split's shares as its quantity. These tests cover the
shared rules (``fill_trade``, ``read_entry_trade``), the ledger's running share
balance, the register sheet behind the GTK grid, and the footer text, so a
security's register shows shares, price, and shares held, and saves the shares
it is given.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from breadsched.gen.engine import ledger
from breadsched.gen.engine.entry_input import TradeFill, fill_trade, trade_price
from breadsched.gen.lib import (
    Account,
    AccountType,
    Commodity,
    CommodityPrice,
    Money,
    Split,
    Transaction,
)
from breadsched.gen.services.entry_input import read_entry_trade
from breadsched.gui.register_sheet import COLUMNS, SECURITY_COLUMNS, RegisterSheet
from breadsched.presentation.investments import register_share_summary


def _fund(db, book, *, scu=1000):
    usd = db.get_commodity_by_mnemonic("USD")
    add_usd = usd is None
    if usd is None:
        usd = Commodity(namespace="CURRENCY", mnemonic="USD", fullname="US Dollar")
    fund = Commodity(namespace="FUND", mnemonic="VTTHX", fullname="Target 2035", fraction=scu)
    account = Account(
        name="VTTHX",
        atype=AccountType.INVESTMENT,
        parent=book.assets,
        commodity=fund.handle,
        commodity_scu=scu,
    )
    with db.transaction("Security") as txn:
        if add_usd:
            db.add_commodity(usd, txn)
        db.add_commodity(fund, txn)
        db.add_account(account, txn)
    return account, fund, usd


def _trade(db, account, usd, other, when, quantity, value, description="Buy"):
    trade = Transaction(post_date=when, description=description)
    trade.currency = usd.handle
    trade.splits = [
        Split(account.handle, Money(value), quantity=Money(quantity)),
        Split(other, -Money(value)),
    ]
    with db.transaction(description) as txn:
        db.add_transaction(trade, txn)
    return trade


def _imported(db, book):
    account, fund, usd = _fund(db, book)
    _trade(db, account, usd, book.checking, date(2026, 1, 10), "10.123", "250.00")
    _trade(db, account, usd, book.checking, date(2026, 2, 10), "4", "110.00", "Buy more")
    _trade(db, account, usd, book.checking, date(2026, 3, 10), "-2.5", "-70.00", "Sell")
    return account, fund, usd


# --------------------------------------------------------------- shared rules


class TestFillTrade:
    def _fill(self, shares, price, amount, changed):
        return fill_trade(
            None if shares is None else Decimal(shares),
            None if price is None else Decimal(price),
            None if amount is None else Decimal(amount),
            changed,
            share_fraction=1000,
            currency_fraction=100,
        )

    def test_shares_and_price_give_the_amount_to_the_cent(self):
        assert self._fill("10.125", "25.43", None, "price") == TradeFill(
            Decimal("10.125"), Decimal("25.43"), Decimal("257.48")
        )

    def test_an_amount_with_shares_gives_the_price(self):
        filled = self._fill("4", None, "110.00", "amount")
        assert filled.price == Decimal("27.5")
        assert filled.amount == Decimal("110.00")

    def test_an_amount_with_only_a_price_gives_shares_to_the_share_unit(self):
        filled = self._fill(None, "30", "100", "amount")
        assert filled.shares == Decimal("3.333")

    def test_changing_the_shares_keeps_the_price_and_recomputes_the_amount(self):
        filled = self._fill("6", "27.5", "110.00", "shares")
        assert (filled.price, filled.amount) == (Decimal("27.5"), Decimal("165.00"))

    def test_changing_the_amount_keeps_the_shares_and_recomputes_the_price(self):
        filled = self._fill("4", "27.5", "120", "amount")
        assert (filled.shares, filled.price) == (Decimal("4"), Decimal("30"))

    def test_signs_are_dropped_and_nothing_divides_by_zero(self):
        assert self._fill("-2.5", None, "-70", "shares") == TradeFill(
            Decimal("2.5"), Decimal("28"), Decimal("70")
        )
        assert self._fill("0", None, "70", "amount").price is None

    def test_a_price_keeps_six_places(self):
        assert trade_price(Decimal("100"), Decimal("3")) == Decimal("33.333333")
        assert trade_price(Decimal("0"), Decimal("3")) is None


class TestReadEntryTrade:
    def test_reads_arithmetic_and_fills_the_third(self, db, book):
        account, _fund_, _usd = _fund(db, book)
        result = read_entry_trade(
            db, account.handle, shares="10+2", price="25", amount="", changed="price"
        )
        assert result.value == TradeFill(Decimal("12"), Decimal("25"), Decimal("300.00"))

    def test_refuses_unreadable_text_and_an_account_without_shares(self, db, book):
        account, _fund_, _usd = _fund(db, book)
        codes = [
            read_entry_trade(
                db, account.handle, shares="ten", price="", amount="", changed="shares"
            )
            .errors[0]
            .code,
            read_entry_trade(db, account.handle, shares="", price="x", amount="", changed="price")
            .errors[0]
            .code,
            read_entry_trade(db, book.checking, shares="1", price="", amount="", changed="shares")
            .errors[0]
            .code,
            read_entry_trade(db, "missing", shares="1", price="", amount="", changed="shares")
            .errors[0]
            .code,
        ]
        assert codes == [
            "entry.shares.invalid",
            "entry.price.invalid",
            "entry.account.not_security",
            "entry.account.not_found",
        ]


# ---------------------------------------------------------------- the ledger


class TestShareBalance:
    def test_a_security_register_carries_the_shares_held_after_each_row(self, db, book):
        account, fund, _usd = _imported(db, book)
        assert ledger.security_of(db, account.handle) is not None
        assert ledger.security_of(db, account.handle).handle == fund.handle
        assert ledger.share_fraction(db, account.handle) == 1000
        rows = ledger.register(db, account.handle)
        assert [row.shares for row in rows] == [Money("10.123"), Money(4), Money("-2.5")]
        assert [row.share_balance for row in rows] == [
            Money("10.123"),
            Money("14.123"),
            Money("11.623"),
        ]

    def test_a_dated_register_opens_with_the_shares_held_before_it(self, db, book):
        account, _fund_, _usd = _imported(db, book)
        rows = ledger.register(db, account.handle, start=date(2026, 2, 1))
        assert [row.share_balance for row in rows] == [Money("14.123"), Money("11.623")]

    def test_an_account_kept_in_a_currency_has_no_share_balance(self, db, book):
        _imported(db, book)
        assert ledger.security_of(db, book.checking) is None
        assert all(row.share_balance is None for row in ledger.register(db, book.checking))

    def test_updating_after_a_save_equals_rebuilding(self, db, book):
        account, _fund_, usd = _imported(db, book)
        rows = ledger.register(db, account.handle)
        added = _trade(db, account, usd, book.checking, date(2026, 1, 20), "1", "26", "Between")
        updated = ledger.update_register(db, account.handle, rows, {added.handle})
        rebuilt = ledger.register(db, account.handle)
        assert [(r.transaction.handle, r.share_balance) for r in updated] == [
            (r.transaction.handle, r.share_balance) for r in rebuilt
        ]


# ---------------------------------------------------------- the register sheet


def _row(sheet, description):
    return next(
        index
        for index, row in enumerate(sheet.rows)
        if row.register_row is not None and row.register_row.description == description
    )


def _type(sheet, column, text):
    sheet.column = column
    sheet.set_text(text)
    assert sheet.settle(), sheet.message


class TestSecuritySheet:
    def test_columns_follow_what_the_account_holds(self, db, book):
        account, _fund_, _usd = _imported(db, book)
        sheet = RegisterSheet(db, account.handle, today=date(2026, 4, 1))
        assert sheet.columns == SECURITY_COLUMNS
        headings = sheet.headings()
        assert [headings[c] for c in ("shares", "price", "share_balance")] == [
            "Shares",
            "Price",
            "Shares Held",
        ]
        assert RegisterSheet(db, book.checking).columns == COLUMNS

    def test_imported_rows_show_shares_price_and_shares_held(self, db, book):
        account, _fund_, _usd = _imported(db, book)
        sheet = RegisterSheet(db, account.handle, today=date(2026, 4, 1))
        cells = {
            description: tuple(
                sheet.text(_row(sheet, description), column)
                for column in ("shares", "price", "increase", "decrease", "share_balance")
            )
            for description in ("Buy", "Buy more", "Sell")
        }
        assert cells == {
            "Buy": ("10.123", "24.696236", "250.00", "", "10.123"),
            "Buy more": ("4", "27.50", "110.00", "", "14.123"),
            "Sell": ("-2.5", "28.00", "", "70.00", "11.623"),
        }
        index = _row(sheet, "Sell")
        assert "2.5 shares" in sheet.summary(index) and "11.623 shares held" in sheet.summary(index)
        assert sheet.closing_shares() == Money("11.623")

    def test_a_share_split_row_shows_shares_and_no_price(self, db, book):
        account, _fund_, usd = _imported(db, book)
        split = Transaction(post_date=date(2026, 3, 20), description="2-for-1 split")
        split.currency = usd.handle
        # As the GnuCash importer stores one: a zero leg in an equity account.
        split.splits = [
            Split(account.handle, Money(0), quantity=Money("11.623")),
            Split(book.opening, Money(0)),
        ]
        with db.transaction("Split") as txn:
            db.add_transaction(split, txn)
        sheet = RegisterSheet(db, account.handle, today=date(2026, 4, 1))
        index = _row(sheet, "2-for-1 split")
        assert [sheet.text(index, c) for c in ("shares", "price", "share_balance")] == [
            "11.623",
            "",
            "23.246",
        ]

    def test_entering_shares_and_a_price_fills_the_amount_and_saves_the_shares(self, db, book):
        account, _fund_, _usd = _imported(db, book)
        sheet = RegisterSheet(db, account.handle, today=date(2026, 4, 1))
        sheet.to_blank()
        _type(sheet, "date", "2026-04-01")
        _type(sheet, "description", "Monthly buy")
        _type(sheet, "transfer", "Assets:Checking")
        _type(sheet, "shares", "8")
        _type(sheet, "price", "30.25")
        assert sheet.draft.increase == "242.00"
        assert sheet.enter(), sheet.message
        saved = next(t for t in db.iter_transactions() if t.description == "Monthly buy")
        mine = saved.split_for(account.handle)
        assert (mine.value, mine.quantity) == (Money("242.00"), Money(8))
        assert saved.split_for(book.checking).quantity == Money("-242.00")
        assert sheet.closing_shares() == Money("19.623")

    def test_a_sell_amount_with_shares_saves_negative_shares(self, db, book):
        account, _fund_, _usd = _imported(db, book)
        sheet = RegisterSheet(db, account.handle, today=date(2026, 4, 1))
        sheet.to_blank()
        _type(sheet, "description", "Sell some")
        _type(sheet, "transfer", "Assets:Checking")
        _type(sheet, "shares", "3")
        _type(sheet, "decrease", "90")
        assert sheet.draft.price == "30.00"
        assert sheet.enter(), sheet.message
        saved = next(t for t in db.iter_transactions() if t.description == "Sell some")
        mine = saved.split_for(account.handle)
        assert (mine.value, mine.quantity) == (Money(-90), Money(-3))

    def test_an_amount_and_a_price_give_the_shares(self, db, book):
        account, _fund_, _usd = _imported(db, book)
        sheet = RegisterSheet(db, account.handle, today=date(2026, 4, 1))
        sheet.to_blank()
        _type(sheet, "description", "Dollar buy")
        _type(sheet, "transfer", "Assets:Checking")
        _type(sheet, "price", "30")
        _type(sheet, "increase", "100")
        assert sheet.draft.shares == "3.333"
        assert sheet.enter(), sheet.message
        saved = next(t for t in db.iter_transactions() if t.description == "Dollar buy")
        assert saved.split_for(account.handle).quantity == Money("3.333")

    def test_a_buy_without_shares_is_refused_and_writes_nothing(self, db, book):
        account, _fund_, _usd = _imported(db, book)
        before = [t.serialize() for t in db.iter_transactions()]
        sheet = RegisterSheet(db, account.handle, today=date(2026, 4, 1))
        sheet.to_blank()
        _type(sheet, "description", "No shares")
        _type(sheet, "transfer", "Assets:Checking")
        _type(sheet, "increase", "100")
        assert not sheet.enter()
        assert sheet.error and "shares" in sheet.message
        assert sheet.column == "shares"
        assert [t.serialize() for t in db.iter_transactions()] == before

    def test_unreadable_shares_are_refused_where_they_were_typed(self, db, book):
        account, _fund_, _usd = _imported(db, book)
        sheet = RegisterSheet(db, account.handle, today=date(2026, 4, 1))
        sheet.to_blank()
        sheet.column = "shares"
        sheet.set_text("lots")
        assert not sheet.settle()
        assert sheet.error and "shares" in sheet.message

    def test_editing_a_description_keeps_the_stored_shares_exactly(self, db, book):
        account, _fund_, usd = _fund(db, book)
        third = _trade(db, account, usd, book.checking, date(2026, 1, 10), "0", "100", "Third")
        stored = db.get_transaction(third.handle)
        stored.split_for(account.handle).quantity = Money(1, 3)
        with db.transaction("Thirds") as txn:
            db.commit_transaction(stored, txn)
        sheet = RegisterSheet(db, account.handle, today=date(2026, 4, 1))
        sheet.move_to(_row(sheet, "Third"))
        _type(sheet, "description", "Third, renamed")
        assert sheet.enter(), sheet.message
        saved = db.get_transaction(third.handle)
        assert saved.description == "Third, renamed"
        assert saved.split_for(account.handle).quantity == Money(1, 3)

    def test_changing_an_amount_keeps_the_shares_and_reprices(self, db, book):
        account, _fund_, _usd = _imported(db, book)
        sheet = RegisterSheet(db, account.handle, today=date(2026, 4, 1))
        sheet.move_to(_row(sheet, "Buy more"))
        _type(sheet, "increase", "120")
        assert sheet.draft.price == "30.00"
        assert sheet.enter(), sheet.message
        saved = next(t for t in db.iter_transactions() if t.description == "Buy more")
        mine = saved.split_for(account.handle)
        assert (mine.value, mine.quantity) == (Money(120), Money(4))

    def test_split_lines_carry_shares_for_the_security(self, db, book):
        account, _fund_, _usd = _imported(db, book)
        sheet = RegisterSheet(db, account.handle, today=date(2026, 4, 1))
        sheet.to_blank()
        _type(sheet, "description", "Buy with fee")
        assert sheet.toggle_split()
        header = sheet.header_index()
        sheet.cursor = header + 1
        _type(sheet, "transfer", "Assets:VTTHX")
        _type(sheet, "shares", "2")
        _type(sheet, "price", "25")
        assert sheet.draft.lines[0].increase == "50.00"
        sheet.cursor = header + 2
        _type(sheet, "transfer", "Assets:Checking")
        _type(sheet, "decrease", "50")
        assert sheet.enter(), sheet.message
        saved = next(t for t in db.iter_transactions() if t.description == "Buy with fee")
        assert saved.split_for(account.handle).quantity == Money(2)


# ------------------------------------------------------------------ the footer


class TestShareSummary:
    def test_names_the_quote_behind_the_value(self, db, book):
        account, fund, usd = _imported(db, book)
        with db.transaction("Price") as txn:
            db.add_price(
                CommodityPrice(
                    commodity=fund.handle,
                    currency=usd.handle,
                    quote_date=date(2026, 3, 31),
                    value=Money("30"),
                    source="Finance::Quote",
                ),
                txn,
            )
        text = register_share_summary(db, account.handle)
        assert text == "11.623 VTTHX shares · worth 348.69 · 2026-03-31 · Finance::Quote"

    def test_without_a_quote_says_so_and_shows_the_ledger_amount(self, db, book):
        account, _fund_, _usd = _imported(db, book)
        assert register_share_summary(db, account.handle) == (
            "11.623 VTTHX shares · no quote; ledger amount 290.00"
        )
        assert register_share_summary(db, book.checking) is None


@pytest.mark.parametrize("scu", [1, 10000])
def test_the_share_unit_comes_from_the_account(db, book, scu):
    account, _fund_, _usd = _fund(db, book, scu=scu)
    assert ledger.share_fraction(db, account.handle) == scu
