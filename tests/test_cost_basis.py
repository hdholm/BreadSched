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
    _trade(db, account, usd, book.opening, date(2024, 12, 1), "2", "0", "Transfer in")
    _trade(db, account, usd, book.checking, date(2025, 1, 1), "2", "200")
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


def _use_average(db, account):
    from breadsched.gen.services import SaveAccount, save_account

    source = Account.from_dict(account.serialize())
    account.cost_basis_method = "average"
    result = save_account(db, SaveAccount(account, existing_handle=account.handle, source=source))
    assert result.ok, result.errors
    return db.get_account(account.handle)


def test_average_cost_takes_the_average_of_every_share_held(db, book):
    account, fund, usd = _fund(db, book)
    _trade(db, account, usd, book.checking, date(2024, 1, 10), "10", "1000")
    _trade(db, account, usd, book.checking, date(2025, 1, 10), "10", "1500")
    _trade(db, account, usd, book.checking, date(2026, 2, 1), "-15", "-2400")
    account = _use_average(db, account)

    result = cost_basis(db, account)

    [sale] = result.sales
    # 20 shares cost 2,500, so each costs 125; 15 sold cost 1,875.
    assert (sale.cost, sale.gain) == (Money("1875"), Money("525"))
    assert (result.quantity, result.cost) == (Money(5), Money("625"))
    assert [(lot.quantity, lot.cost) for lot in result.lots] == [
        (Money("2.5"), Money(250)),
        (Money("2.5"), Money(375)),
    ]
    assert result.as_dict()["method"] == "average"
    from breadsched.presentation import holding_cost_text

    assert holding_cost_text(result).startswith("5 shares cost 625.00 at average cost")


def test_an_unknown_method_is_refused_and_the_account_kept(db, book):
    from breadsched.gen.services import SaveAccount, save_account

    account, _fund_commodity, _usd = _fund(db, book)
    source = Account.from_dict(account.serialize())
    account.cost_basis_method = "lifo"
    result = save_account(db, SaveAccount(account, existing_handle=account.handle, source=source))
    assert [error.code for error in result.errors] == ["account.cost_basis_method.invalid"]
    assert db.get_account(account.handle).cost_basis_method == "fifo"


def test_web_and_cli_choose_the_method(db, book, tmp_path, capsys):
    import pytest

    from breadsched.cli.main import main
    from breadsched.gen.db.sqlite import DbSQLite
    from breadsched.web.account_resource import account_cost_basis_save
    from breadsched.web.context import Api

    account, _fund_commodity, _usd = _fund(db, book)
    api = Api(db)
    assert account_cost_basis_save(api, {"handle": account.handle, "method": "average"}) == {
        "handle": account.handle,
        "method": "average",
    }
    with pytest.raises(Exception, match="cost"):
        account_cost_basis_save(api, {"handle": account.handle, "method": "lifo"})
    assert db.get_account(account.handle).cost_basis_method == "average"
    with pytest.raises(ValueError, match="Investment or Retirement"):
        account_cost_basis_save(api, {"handle": book.checking, "method": "average"})

    path = tmp_path / "method.breadsched"
    db.backup_to(str(path))
    assert (
        main(
            ["account", str(path), "edit", "--name", "Assets:Index holding", "--cost-basis", "fifo"]
        )
        == 0
    )
    assert (
        main(["account", str(path), "edit", "--name", "Assets:Checking", "--cost-basis", "fifo"])
        == 2
    )
    capsys.readouterr()
    reopened = DbSQLite()
    reopened.load(str(path), mode="r")
    try:
        assert reopened.get_account(account.handle).cost_basis_method == "fifo"
    finally:
        reopened.close()


def _second_holding(db, book, fund, name="IRA holding"):
    other = Account(
        name=name,
        atype=AccountType.RETIREMENT,
        parent=book.assets,
        commodity=fund.handle,
        commodity_scu=1000,
    )
    with db.transaction("Second holding") as txn:
        db.add_account(other, txn)
    return other


def _move(db, usd, source, target, when, quantity, value):
    move = Transaction(post_date=when, description="Move shares")
    move.currency = usd.handle
    move.splits = [
        Split(source.handle, -Money(value), quantity=-Money(quantity)),
        Split(target.handle, Money(value), quantity=Money(quantity)),
    ]
    with db.transaction("Move shares") as txn:
        db.add_transaction(move, txn)
    return move


def book_opening(db):
    return next(item.handle for item in db.iter_accounts() if item.name == "Opening Balances")


def _share_split(db, account, when, quantity):
    change = Transaction(post_date=when, description="Stock split")
    change.splits = [
        Split(account.handle, Money(0), quantity=Money(quantity)),
        Split(book_opening(db), Money(0)),
    ]
    with db.transaction("Stock split") as txn:
        db.add_transaction(change, txn)
    return change


def test_a_transfer_carries_lots_and_realizes_nothing(db, book):
    account, fund, usd = _fund(db, book)
    ira = _second_holding(db, book, fund)
    first = _trade(db, account, usd, book.checking, date(2024, 1, 10), "10", "1000")
    _trade(db, account, usd, book.checking, date(2025, 1, 10), "10", "1500")
    # The transfer is recorded at market value; the cost moves, not that value.
    move = _move(db, usd, account, ira, date(2025, 6, 1), "12", "2400")
    _trade(db, ira, usd, book.checking, date(2026, 1, 5), "-11", "-2200")

    source = cost_basis(db, account)
    assert source.sales == ()
    [moved_out] = source.moves
    assert (moved_out.kind, moved_out.quantity, moved_out.cost, moved_out.other_account) == (
        "transfer_out",
        Money(-12),
        Money(1300),
        ira.handle,
    )
    assert (source.quantity, source.cost) == (Money(8), Money(1200))

    target = cost_basis(db, ira)
    [moved_in] = target.moves
    assert (moved_in.kind, moved_in.cost, moved_in.transaction) == (
        "transfer_in",
        Money(1300),
        move.handle,
    )
    # The 10 shares bought in 2024 sell first, at their original cost.
    [sale] = target.sales
    assert (sale.cost, sale.gain) == (Money(1150), Money(1050))
    [lot] = target.lots
    assert (lot.acquired, lot.quantity, lot.cost, lot.transaction) == (
        date(2025, 1, 10),
        Money(1),
        Money(150),
        target.moves[0].lots[1].transaction,
    )
    assert target.moves[0].lots[0].transaction == first.handle
    assert target.problems == ("no market quote, so there is no unrealized gain",)
    assert target.as_dict()["moves"][0]["quantity"] == "12"


def test_an_average_cost_account_moves_the_average(db, book):
    account, fund, usd = _fund(db, book)
    ira = _second_holding(db, book, fund)
    _trade(db, account, usd, book.checking, date(2024, 1, 10), "10", "1000")
    _trade(db, account, usd, book.checking, date(2025, 1, 10), "10", "1500")
    account = _use_average(db, account)
    _move(db, usd, account, ira, date(2025, 6, 1), "4", "800")

    assert cost_basis(db, account).cost == Money(2000)
    assert cost_basis(db, ira).cost == Money(500)


def test_a_share_split_changes_shares_not_cost(db, book):
    account, fund, usd = _fund(db, book)
    _trade(db, account, usd, book.checking, date(2024, 1, 10), "10", "1000")
    _trade(db, account, usd, book.checking, date(2025, 1, 10), "10", "1500")
    _share_split(db, account, date(2025, 3, 1), "20")
    _trade(db, account, usd, book.checking, date(2025, 9, 1), "-20", "-1800")
    _share_split(db, account, date(2025, 10, 1), "-10")

    result = cost_basis(db, account)

    first_split, reverse = result.moves
    assert (first_split.kind, first_split.held, first_split.quantity) == (
        "split",
        Money(20),
        Money(20),
    )
    assert (reverse.held, reverse.quantity) == (Money(20), Money(-10))
    # Two for one: the first 20 shares are the 2024 purchase at 50 each.
    [sale] = result.sales
    assert (sale.cost, sale.gain) == (Money(1000), Money(800))
    [lot] = result.lots
    assert (lot.quantity, lot.cost) == (Money(10), Money(1500))
    assert result.problems == ("no market quote, so there is no unrealized gain",)


def test_a_transfer_beyond_the_purchases_is_named(db, book):
    account, fund, usd = _fund(db, book)
    ira = _second_holding(db, book, fund)
    _trade(db, account, usd, book.checking, date(2024, 1, 10), "2", "200")
    _move(db, usd, account, ira, date(2025, 6, 1), "5", "500")

    source = cost_basis(db, account)
    target = cost_basis(db, ira)

    assert source.problems == (
        "3 shares were moved out beyond the recorded purchases; the receiving account "
        "has no cost for them",
    )
    [moved_in] = target.moves
    assert moved_in.cost == Money(200)
    assert target.problems[0].startswith("3 shares arrived with no recorded cost")


def test_a_same_day_exchange_both_ways_is_not_matched(db, book):
    account, fund, usd = _fund(db, book)
    ira = _second_holding(db, book, fund)
    _trade(db, account, usd, book.checking, date(2024, 1, 10), "4", "400")
    _move(db, usd, account, ira, date(2025, 6, 1), "3", "450")
    _move(db, usd, ira, account, date(2025, 6, 1), "1", "150")

    source = cost_basis(db, account)
    target = cost_basis(db, ira)

    # Each leg is a sale or purchase at its recorded value; nothing recurses.
    assert source.moves == () and target.moves == ()
    assert [sale.gain for sale in source.sales] == [Money(150)]
    assert (source.quantity, source.cost) == (Money(2), Money(250))
    assert (target.quantity, target.cost) == (Money(2), Money(300))
    assert any("moved both ways with Assets:IRA holding" in note for note in source.problems)


def test_moves_are_worded_for_every_interface(db, book, tmp_path, capsys):
    from breadsched.cli.main import main
    from breadsched.gen.db.sqlite import DbSQLite
    from breadsched.presentation import lot_move_text
    from breadsched.web.context import Api
    from breadsched.web.holdings_resource import holdings
    from breadsched.web.resources import QueryParams

    account, fund, usd = _fund(db, book)
    ira = _second_holding(db, book, fund)
    _trade(db, account, usd, book.checking, date(2024, 1, 10), "10", "1000")
    _share_split(db, account, date(2025, 3, 1), "10")
    _move(db, usd, account, ira, date(2025, 6, 1), "4", "800")

    split, moved = cost_basis(db, account).moves
    assert lot_move_text(split) == "share split 2025-03-01: 10 shares became 20"
    assert lot_move_text(moved, "Assets:IRA holding") == (
        "moved out 2025-06-01 to Assets:IRA holding: 4 shares, cost 200.00"
    )
    [received] = cost_basis(db, ira).moves
    assert lot_move_text(received, "Assets:Index holding") == (
        "moved in 2025-06-01 from Assets:Index holding: 4 shares, cost 200.00"
    )

    payload = holdings(Api(db), QueryParams(""))
    texts = [move["text"] for item in payload["holdings"] for move in item["moves"]]
    assert "moved in 2025-06-01 from Assets:Index holding: 4 shares, cost 200.00" in texts

    path = tmp_path / "moves.breadsched"
    db.backup_to(str(path))
    assert main(["holdings", str(path), "--lots"]) == 0
    text = capsys.readouterr().out
    assert "  share split 2025-03-01: 10 shares became 20" in text
    assert "  moved out 2025-06-01 to Assets:IRA holding: 4 shares, cost 200.00" in text
    reopened = DbSQLite()
    reopened.load(str(path), mode="r")
    reopened.close()
