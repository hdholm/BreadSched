"""Specific identification: a sale names the lots it sells; realized gains by year."""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.engine import import_review
from breadsched.gen.engine.cost_basis import cost_basis, lots_before_sale
from breadsched.gen.engine.realized_gains import realized_gains
from breadsched.gen.lib import (
    Account,
    AccountType,
    Commodity,
    LotPick,
    Money,
    Split,
    Transaction,
)
from breadsched.gen.lib.amount import Amount
from breadsched.gen.services import SetSaleLots, sale_lots, set_sale_lots
from breadsched.gen.services.transactions import (
    SaveTransaction,
    TransactionInput,
    TransactionSplitInput,
    save_transaction,
)


def _fund(db, book, name="Index holding", method="fifo"):
    usd = db.get_commodity_by_mnemonic("USD")
    add_usd = usd is None
    if usd is None:
        usd = Commodity(namespace="CURRENCY", mnemonic="USD", fullname="US Dollar")
    fund = db.get_commodity_by_mnemonic("INDEX")
    add_fund = fund is None
    if fund is None:
        fund = Commodity(namespace="FUND", mnemonic="INDEX", fullname="Index fund", fraction=1000)
    account = Account(
        name=name,
        atype=AccountType.INVESTMENT,
        parent=book.assets,
        commodity=fund.handle,
        commodity_scu=1000,
    )
    account.cost_basis_method = method
    with db.transaction("Security") as txn:
        if add_usd:
            db.add_commodity(usd, txn)
        if add_fund:
            db.add_commodity(fund, txn)
        db.add_account(account, txn)
    return account, fund, usd


def _trade(db, account, usd, other, when, quantity, value, picks=()):
    trade = Transaction(post_date=when, description="Trade")
    trade.currency = usd.handle
    trade.splits = [
        Split(account.handle, Money(value), quantity=Money(quantity), lot_picks=picks),
        Split(other, -Money(value)),
    ]
    with db.transaction("Trade") as txn:
        db.add_transaction(trade, txn)
    return trade


def _three_lots(db, book, method="fifo"):
    account, fund, usd = _fund(db, book, method=method)
    first = _trade(db, account, usd, book.checking, date(2023, 1, 10), "10", "1000")
    second = _trade(db, account, usd, book.checking, date(2024, 1, 10), "10", "1500")
    third = _trade(db, account, usd, book.checking, date(2025, 1, 10), "10", "2000")
    return account, usd, first, second, third


def _sale_split(trade, account):
    return next(split for split in trade.splits if split.account == account.handle)


class TestEngine:
    def test_a_sale_naming_a_lot_sells_that_lot(self, db, book):
        account, usd, first, second, third = _three_lots(db, book)
        sale = _trade(
            db,
            account,
            usd,
            book.checking,
            date(2026, 2, 1),
            "-10",
            "-2500",
            picks=[LotPick(third.handle, Money(10))],
        )
        result = cost_basis(db, account)
        [sold] = result.sales
        assert sold.specific and sold.split == _sale_split(sale, account).handle
        assert (sold.cost, sold.gain) == (Money(2000), Money(500))
        assert [(lot.acquired, lot.quantity, lot.cost) for lot in sold.lots] == [
            (date(2025, 1, 10), Money(10), Money(2000))
        ]
        # The first two lots stay open, untouched.
        assert [lot.transaction for lot in result.lots] == [first.handle, second.handle]
        assert not [problem for problem in result.problems if "lot" in problem]

    def test_shares_beyond_the_named_lots_sell_by_the_method(self, db, book):
        account, usd, first, second, third = _three_lots(db, book)
        _trade(
            db,
            account,
            usd,
            book.checking,
            date(2026, 2, 1),
            "-15",
            "-3000",
            picks=[LotPick(second.handle, Money(5))],
        )
        [sold] = cost_basis(db, account).sales
        # Five named from the second lot, then ten first-in, first-out from the first.
        assert [(lot.transaction, lot.quantity, lot.cost) for lot in sold.lots] == [
            (second.handle, Money(5), Money(750)),
            (first.handle, Money(10), Money(1000)),
        ]
        assert sold.cost == Money(1750)

    def test_average_cost_accounts_honour_named_lots(self, db, book):
        account, usd, first, second, third = _three_lots(db, book, method="average")
        _trade(
            db,
            account,
            usd,
            book.checking,
            date(2026, 2, 1),
            "-4",
            "-800",
            picks=[LotPick(first.handle, Money(4))],
        )
        [sold] = cost_basis(db, account).sales
        assert sold.cost == Money(400)  # four shares of the first lot at 100

    def test_a_lot_already_sold_is_named_as_a_problem(self, db, book):
        account, usd, first, second, third = _three_lots(db, book)
        _trade(db, account, usd, book.checking, date(2025, 6, 1), "-10", "-1800")  # FIFO: first
        _trade(
            db,
            account,
            usd,
            book.checking,
            date(2026, 2, 1),
            "-5",
            "-1000",
            picks=[LotPick(first.handle, Money(5))],
        )
        result = cost_basis(db, account)
        later = result.sales[1]
        # The named lot is gone, so the account's method sells from the second lot.
        assert [(lot.transaction, lot.quantity) for lot in later.lots] == [
            (second.handle, Money(5))
        ]
        assert any("had 0 of the 5 shares named" in problem for problem in result.problems)

    def test_lots_keep_their_identity_through_a_share_split(self, db, book):
        account, usd, first, second, third = _three_lots(db, book)
        split = Transaction(post_date=date(2025, 6, 1), description="2-for-1 split")
        split.currency = usd.handle
        split.splits = [
            Split(account.handle, Money(0), quantity=Money(30)),
            Split(book.equity, Money(0), quantity=Money(0)),
        ]
        with db.transaction("Split") as txn:
            db.add_transaction(split, txn)
        _trade(
            db,
            account,
            usd,
            book.checking,
            date(2026, 2, 1),
            "-20",
            "-2400",
            picks=[LotPick(second.handle, Money(20))],
        )
        [sold] = cost_basis(db, account).sales
        assert [(lot.transaction, lot.quantity, lot.cost) for lot in sold.lots] == [
            (second.handle, Money(20), Money(1500))
        ]

    def test_lots_offered_before_a_sale_include_same_day_purchases(self, db, book):
        account, usd, first, second, third = _three_lots(db, book)
        _trade(db, account, usd, book.checking, date(2026, 2, 1), "5", "1100")
        sale = _trade(db, account, usd, book.checking, date(2026, 2, 1), "-5", "-1100")
        offered = lots_before_sale(db, account, _sale_split(sale, account).handle)
        assert [lot.quantity for lot in offered] == [Money(10), Money(10), Money(10), Money(5)]
        assert lots_before_sale(db, account, "no-such-split") == ()


class TestService:
    def _sold(self, db, book):
        account, usd, first, second, third = _three_lots(db, book)
        sale = _trade(db, account, usd, book.checking, date(2026, 2, 1), "-10", "-2500")
        return account, sale, _sale_split(sale, account), (first, second, third)

    def test_naming_lots_changes_the_gain_and_can_be_undone(self, db, book):
        account, sale, split, (first, second, third) = self._sold(db, book)
        assert cost_basis(db, account).sales[0].cost == Money(1000)
        result = set_sale_lots(
            db, SetSaleLots(sale.handle, split.handle, ((third.handle, "6"), (second.handle, "4")))
        )
        assert result.errors == ()
        chosen = result.value
        assert chosen.picks == (LotPick(third.handle, Money(6)), LotPick(second.handle, Money(4)))
        assert chosen.sale.cost == Money(1200 + 600)
        assert db.get_transaction(sale.handle).splits[0].lot_picks == chosen.picks
        assert db.undo()
        assert db.get_transaction(sale.handle).splits[0].lot_picks == ()
        assert cost_basis(db, account).sales[0].cost == Money(1000)

    def test_clearing_returns_the_sale_to_the_method(self, db, book):
        account, sale, split, lots = self._sold(db, book)
        set_sale_lots(db, SetSaleLots(sale.handle, split.handle, ((lots[2].handle, "10"),)))
        cleared = set_sale_lots(db, SetSaleLots(sale.handle, split.handle, ())).value
        assert cleared.picks == () and not cleared.sale.specific
        assert cleared.sale.cost == Money(1000)

    def test_the_chooser_offers_the_lots_open_before_the_sale(self, db, book):
        account, sale, split, lots = self._sold(db, book)
        offered = sale_lots(db, sale.handle, split.handle).value
        assert [lot.transaction for lot in offered.offered] == [lot.handle for lot in lots]
        assert offered.method == "fifo" and offered.account.handle == account.handle

    @pytest.mark.parametrize(
        ("picks", "code"),
        [
            ((("missing", "1"),), "lots.lot.unknown"),
            (((0, "11"),), "lots.quantity.exceeds_lot"),
            (((0, "0"),), "lots.quantity.invalid"),
            (((0, "-2"),), "lots.quantity.invalid"),
            (((0, "lots"),), "lots.quantity.invalid"),
            (((0, "NaN"),), "lots.quantity.invalid"),
            (((0, "2"), (0, "3")), "lots.lot.duplicate"),
            (((0, "10"), (1, "1")), "lots.quantity.exceeds_sale"),
        ],
    )
    def test_rejected_choices_leave_the_sale_unchanged(self, db, book, picks, code):
        account, sale, split, lots = self._sold(db, book)
        resolved = tuple(
            (lots[lot].handle if isinstance(lot, int) else lot, text) for lot, text in picks
        )
        before = db.get_transaction(sale.handle).serialize()
        result = set_sale_lots(db, SetSaleLots(sale.handle, split.handle, resolved))
        assert result.value is None and code in {error.code for error in result.errors}
        assert db.get_transaction(sale.handle).serialize() == before

    def test_only_a_sale_can_name_lots(self, db, book):
        account, sale, split, lots = self._sold(db, book)
        buy = _sale_split(lots[0], account)
        assert set_sale_lots(db, SetSaleLots(lots[0].handle, buy.handle, ())).errors[0].code == (
            "lots.split.not_sale"
        )
        cash = next(item for item in sale.splits if item.account != account.handle)
        assert sale_lots(db, sale.handle, cash.handle).errors[0].code == "lots.split.not_sale"
        assert sale_lots(db, "missing", split.handle).errors[0].code == (
            "lots.transaction.not_found"
        )
        assert sale_lots(db, sale.handle, "missing").errors[0].code == "lots.split.not_found"

    def test_editing_the_sale_keeps_its_lots(self, db, book):
        account, sale, split, lots = self._sold(db, book)
        set_sale_lots(db, SetSaleLots(sale.handle, split.handle, ((lots[2].handle, "10"),)))
        stored = db.get_transaction(sale.handle)
        usd = stored.currency
        edited = save_transaction(
            db,
            SaveTransaction(
                TransactionInput(
                    post_date=stored.post_date,
                    description="Sold some index fund",
                    currency=usd,
                    splits=tuple(
                        TransactionSplitInput(
                            account=item.account,
                            value=Amount(item.value, usd),
                            quantity=Amount(
                                item.quantity, db.get_account(item.account).commodity or usd
                            ),
                            handle=item.handle,
                            memo=item.memo,
                        )
                        for item in stored.splits
                    ),
                ),
                existing_handle=sale.handle,
            ),
        )
        assert edited.errors == ()
        assert db.get_transaction(sale.handle).splits[0].lot_picks == (
            LotPick(lots[2].handle, Money(10)),
        )

    def test_gnucash_reimport_keeps_the_lots_breadsched_chose(self, db, book):
        account, sale, split, lots = self._sold(db, book)
        set_sale_lots(db, SetSaleLots(sale.handle, split.handle, ((lots[1].handle, "10"),)))
        existing = db.get_transaction(sale.handle)
        incoming = Transaction.from_dict(existing.serialize())
        for item in incoming.splits:
            item.lot_picks = ()
        import_review.merge_local_state(incoming, existing)
        assert incoming.splits[0].lot_picks == (LotPick(lots[1].handle, Money(10)),)


class TestRealizedGainsReport:
    def test_sales_are_listed_with_their_lots_and_totalled_by_year(self, db, book):
        account, usd, first, second, third = _three_lots(db, book)
        _trade(db, account, usd, book.checking, date(2025, 6, 1), "-5", "-900")
        _trade(
            db,
            account,
            usd,
            book.checking,
            date(2026, 2, 1),
            "-10",
            "-2500",
            picks=[LotPick(third.handle, Money(10))],
        )
        report = realized_gains(db)
        assert [(sale.sold, sale.sale.gain) for sale in report.sales] == [
            (date(2025, 6, 1), Money(400)),
            (date(2026, 2, 1), Money(500)),
        ]
        assert [(total.year, total.gain, total.sales) for total in report.years] == [
            (2025, Money(400), 1),
            (2026, Money(500), 1),
        ]
        assert report.sales[1].lots[0].acquired == date(2025, 1, 10)
        assert [sale.sold for sale in realized_gains(db, year=2026).sales] == [date(2026, 2, 1)]
        assert realized_gains(db, account="other").sales == ()
        data = report.as_dict(db)
        assert data["years"][0]["currency"] == "USD"
        assert data["sales"][1]["specific"] is True
        assert data["sales"][1]["lots"][0]["lot"] == third.handle

    def test_accounts_add_into_one_total_per_year_and_currency(self, db, book):
        account, usd, first, second, third = _three_lots(db, book)
        other, _fund_commodity, _usd = _fund(db, book, name="Second holding")
        _trade(db, other, usd, book.checking, date(2024, 3, 1), "4", "400")
        _trade(db, account, usd, book.checking, date(2026, 2, 1), "-1", "-150")
        _trade(db, other, usd, book.checking, date(2026, 3, 1), "-1", "-90")
        [total] = realized_gains(db).years
        assert (total.year, total.proceeds, total.cost, total.gain, total.sales) == (
            2026,
            Money(240),
            Money(200),
            Money(40),
            2,
        )


def test_the_command_line_reports_gains_and_names_lots(tmp_path, capsys):
    import json
    from types import SimpleNamespace

    from breadsched.cli.main import main as cli
    from breadsched.gen.db.sqlite import DbSQLite

    path = tmp_path / "lots.breadsched"
    assert cli(["init", str(path)]) == 0
    db = DbSQLite()
    db.load(str(path))
    try:
        assets = next(a for a in db.iter_accounts() if a.name == "Assets")
        checking = Account(name="Brokerage cash", atype=AccountType.BANK, parent=assets.handle)
        with db.transaction("Cash") as txn:
            db.add_account(checking, txn)
        holder = SimpleNamespace(assets=assets.handle, checking=checking.handle)
        account, _fund_commodity, usd = _fund(db, holder)
        first = _trade(db, account, usd, checking.handle, date(2023, 1, 10), "10", "1000")
        newer = _trade(db, account, usd, checking.handle, date(2024, 1, 10), "10", "1500")
        sale = _trade(db, account, usd, checking.handle, date(2026, 2, 1), "-5", "-1000")
    finally:
        db.close()
    capsys.readouterr()

    assert cli(["realized-gains", str(path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    [sold] = report["sales"]
    assert (sold["gain"], sold["specific"], sold["lots"][0]["lot"]) == (
        "500.00",
        False,
        first.handle,
    )

    assert cli(["sale-lots", str(path), sale.handle[:10], "--lot", f"{newer.handle[:10]}=5"]) == 0
    capsys.readouterr()
    assert cli(["realized-gains", str(path), "--year", "2026"]) == 0
    text = capsys.readouterr().out
    assert "named lots" in text and "bought 2024-01-10: 5 shares, cost 750.00" in text
    assert "2026" in text and "250.00" in text

    assert cli(["sale-lots", str(path), sale.handle, "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert [lot["lot"] for lot in shown["offered"]] == [first.handle, newer.handle]
    assert shown["picks"] == [{"lot": newer.handle, "quantity": "5"}]

    assert cli(["sale-lots", str(path), sale.handle, "--lot", f"{first.handle}=50"]) == 2
    assert "more shares than it holds" in capsys.readouterr().err
    assert cli(["sale-lots", str(path), first.handle]) == 2
    assert "sells no shares" in capsys.readouterr().err
    assert cli(["sale-lots", str(path), sale.handle, "--clear"]) == 0
    capsys.readouterr()
    assert cli(["realized-gains", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sales"][0]["specific"] is False


def test_a_lot_choice_is_not_a_change_for_gnucash(db, book):
    """Write-back and re-import compare GnuCash's facts; the lots chosen are not among them."""
    account, usd, first, second, third = _three_lots(db, book)
    sale = _trade(db, account, usd, book.checking, date(2026, 2, 1), "-5", "-1000")
    before = import_review.transaction_source_facts(db.get_transaction(sale.handle))
    split = _sale_split(sale, account)
    set_sale_lots(db, SetSaleLots(sale.handle, split.handle, ((second.handle, "5"),)))
    after = import_review.transaction_source_facts(db.get_transaction(sale.handle))
    assert before == after
