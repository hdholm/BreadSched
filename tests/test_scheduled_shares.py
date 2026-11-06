"""Scheduled transactions that buy shares price them when entered (#417).

A payroll schedule moves $500.00 a pay period into a 401(k) fund. The schedule
stores only that dollar value, since the fund's price moves between paydays. When
an occurrence is entered, the fund leg's shares come from the latest quote on or
before its date; with no quote, the interfaces fetch one online or ask for the
share price (or the number of shares), and what is given is saved as a dated
price in the same undoable change. Entering is refused only when nothing prices
the leg, and automatic posting leaves such an occurrence due.
"""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.engine import schedule as schedule_engine
from breadsched.gen.lib import (
    Account,
    AccountType,
    Commodity,
    CommodityPrice,
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
)
from breadsched.gen.services import (
    DueDecision,
    DueQuotes,
    LegPrice,
    PostDue,
    ResolveDue,
    ResolveNext,
    due_quotes,
    post_due,
    resolve_due,
    resolve_next,
)
from breadsched.presentation import service_error_message

PAYDAY = date(2026, 11, 5)
TODAY = date(2026, 11, 6)


@pytest.fixture
def payroll(db, book):
    usd = db.get_commodity_by_mnemonic("USD")
    fund = Commodity(namespace="FUND", mnemonic="TDF2050", fullname="Target 2050", fraction=1000)
    with db.transaction("401(k)") as txn:
        db.add_commodity(fund, txn)
        account = Account(
            name="401(k)", atype=AccountType.RETIREMENT, parent=book.assets, commodity=fund.handle
        )
        db.add_account(account, txn)
        schedule = ScheduledTransaction(
            name="Payroll",
            recurrence=Recurrence(period=PeriodType.MONTH, interval=1, start=PAYDAY),
            splits=[
                ScheduledSplit(account.handle, Money("500.00")),
                ScheduledSplit(book.checking, Money("-500.00")),
            ],
        )
        db.add_scheduled(schedule, txn)
    return schedule, account, fund, usd


def _quote(db, fund, usd, when, value, source="breadsched"):
    with db.transaction("Quote") as txn:
        db.add_price(
            CommodityPrice(
                commodity=fund.handle,
                currency=usd.handle,
                quote_date=when,
                value=Money(value),
                source=source,
            ),
            txn,
        )


def _enter(db, schedule, prices=()):
    return resolve_due(
        db, ResolveDue(((schedule.handle, PAYDAY, DueDecision.POST),), TODAY, prices)
    )


def _fund_split(db, result, account):
    [handle] = result.value.transactions
    return next(s for s in db.get_transaction(handle).splits if s.account == account.handle)


def test_entered_shares_come_from_the_latest_stored_quote(db, payroll):
    schedule, account, fund, usd = payroll
    _quote(db, fund, usd, date(2026, 10, 30), "40.00")
    _quote(db, fund, usd, date(2026, 11, 4), "50.00")
    # A quote after the payday does not price it.
    _quote(db, fund, usd, date(2026, 11, 6), "80.00")

    result = _enter(db, schedule)

    assert result.ok, result.errors
    split = _fund_split(db, result, account)
    assert split.value == Money("500.00")
    assert split.quantity == Money(10)


def test_shares_round_to_the_fund_unit_and_sales_are_negative(db, payroll):
    schedule, account, fund, usd = payroll
    _quote(db, fund, usd, date(2026, 11, 4), "30.00")
    draft = schedule.instantiate(PAYDAY)
    schedule_engine.price_legs(db, draft)
    [bought] = [s for s in draft.splits if s.account == account.handle]
    assert bought.quantity == Money("16.667")

    sale = schedule.instantiate(PAYDAY)
    for item in sale.splits:
        item.value = -item.value
        item.quantity = item.value
    schedule_engine.price_legs(db, sale)
    [sold] = [s for s in sale.splits if s.account == account.handle]
    assert sold.quantity == Money("-16.667")


def test_due_quotes_lists_share_legs_and_the_online_quotes_to_fetch(db, payroll):
    schedule, account, fund, usd = payroll

    needs = due_quotes(db, DueQuotes(((schedule.handle, PAYDAY),), TODAY))
    [leg] = needs.legs
    assert (leg.mnemonic, leg.currency, leg.value, leg.price, leg.shares) == (
        "TDF2050",
        "USD",
        Money("500.00"),
        None,
        None,
    )
    assert leg.account_name.endswith("401(k)")
    assert needs.unpriced == (leg,)
    # No quote source: nothing to fetch, so the price must be asked for.
    assert needs.requests == ()

    with db.transaction("Quote source") as txn:
        fund.quote_source = "alphavantage"
        db.commit_commodity(fund, txn)
    [request] = due_quotes(db, DueQuotes(((schedule.handle, PAYDAY),), TODAY)).requests
    assert (request.symbol, request.source) == ("TDF2050", "alphavantage")

    _quote(db, fund, usd, PAYDAY, "25.00", source="Online: alphavantage.co")
    fresh = due_quotes(db, DueQuotes(((schedule.handle, PAYDAY),), TODAY))
    assert fresh.requests == ()
    [priced] = fresh.legs
    assert (priced.price, priced.price_date, priced.shares) == (Money(25), PAYDAY, Money(20))
    assert fresh.unpriced == ()


def test_without_a_quote_entering_is_refused_and_writes_nothing(db, payroll):
    schedule, _account, _fund, _usd = payroll
    before = list(db.iter_transactions())

    result = _enter(db, schedule)

    assert not result.ok
    [error] = result.errors
    assert error.code == "schedule.due.no_quote"
    assert error.fields[-1] == "TDF2050"
    assert "price" in service_error_message(error)
    assert list(db.iter_transactions()) == before
    assert list(db.iter_prices()) == []


def test_a_typed_share_price_is_saved_and_prices_the_entry(db, payroll):
    schedule, account, fund, usd = payroll

    result = _enter(db, schedule, (LegPrice(schedule.handle, PAYDAY, account.handle, Money(20)),))

    assert result.ok, result.errors
    assert _fund_split(db, result, account).quantity == Money(25)
    [saved] = db.iter_prices()
    assert (saved.commodity, saved.currency, saved.quote_date, saved.value, saved.source) == (
        fund.handle,
        usd.handle,
        PAYDAY,
        Money(20),
        "breadsched",
    )
    # One undo removes the entry and the price together.
    db.undo()
    assert list(db.iter_prices()) == []
    assert list(db.iter_transactions()) == []


def test_typed_shares_are_entered_exactly_with_the_price_they_imply(db, payroll):
    schedule, account, _fund, _usd = payroll
    given = LegPrice(schedule.handle, PAYDAY, account.handle, shares=Money("12.345"))

    result = resolve_next(
        db, ResolveNext(schedule.handle, DueDecision.POST, PAYDAY, TODAY, (given,))
    )

    assert result.ok, result.errors
    assert _fund_split(db, result, account).quantity == Money("12.345")
    [saved] = db.iter_prices()
    # $500.00 / 12.345 shares, as precisely as a stored price keeps it.
    assert saved.value.quantize(10000) == Money("40.5022")


@pytest.mark.parametrize(
    ("given", "code"),
    [
        (dict(price=Money(0)), "schedule.due.price.invalid"),
        (dict(price=Money(20), shares=Money(5)), "schedule.due.price.invalid"),
        (dict(), "schedule.due.price.invalid"),
        (dict(price=Money(20), account="checking"), "schedule.due.price.leg"),
        (dict(price=Money(20), when=date(2026, 12, 5)), "schedule.due.price.not_entered"),
    ],
)
def test_a_price_that_cannot_apply_is_refused(db, book, payroll, given, code):
    schedule, account, _fund, _usd = payroll
    account_handle = book.checking if given.pop("account", None) else account.handle
    when = given.pop("when", PAYDAY)

    result = _enter(db, schedule, (LegPrice(schedule.handle, when, account_handle, **given),))

    assert [error.code for error in result.errors] == [code]
    assert service_error_message(result.errors[0]) != code
    assert list(db.iter_transactions()) == []
    assert list(db.iter_prices()) == []


def test_automatic_posting_leaves_an_unpriced_occurrence_due(db, book, payroll):
    schedule, account, fund, usd = payroll
    with db.transaction("Automatic") as txn:
        schedule.auto_create = True
        db.commit_scheduled(schedule, txn)

    assert post_due(db, PostDue(as_of=TODAY)).value.posted == ()
    assert schedule_engine.next_pending(db, schedule, as_of=TODAY).when == PAYDAY

    _quote(db, fund, usd, PAYDAY, "50.00")
    [posted] = post_due(db, PostDue(as_of=TODAY)).value.posted
    real = db.get_transaction(posted.transaction)
    assert next(s for s in real.splits if s.account == account.handle).quantity == Money(10)


def test_a_foreign_currency_leg_takes_its_amount_from_the_exchange_rate(db, book):
    usd = db.get_commodity_by_mnemonic("USD")
    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro", fraction=100)
    with db.transaction("Euro") as txn:
        db.add_commodity(euro, txn)
        account = Account(
            name="Euro cash", atype=AccountType.BANK, parent=book.assets, commodity=euro.handle
        )
        db.add_account(account, txn)
        # Quoted the other way round: one euro is 1/0.8 dollars.
        db.add_price(
            CommodityPrice(
                commodity=usd.handle,
                currency=euro.handle,
                quote_date=date(2026, 11, 1),
                value=Money("0.80"),
            ),
            txn,
        )
    schedule = ScheduledTransaction(
        name="Transfer",
        recurrence=Recurrence(period=PeriodType.MONTH, interval=1, start=PAYDAY),
        splits=[
            ScheduledSplit(account.handle, Money("100.00")),
            ScheduledSplit(book.checking, Money("-100.00")),
        ],
    )
    real = schedule_engine.entered_transaction(db, schedule, PAYDAY)
    assert next(s for s in real.splits if s.account == account.handle).quantity == Money(80)


def _cli_book(path):
    from breadsched.gen.db.sqlite import DbSQLite

    db = DbSQLite()
    db.load(str(path))
    try:
        with db.transaction("Accounts") as txn:
            root = Account(name="Root", atype=AccountType.ROOT)
            db.add_account(root, txn)
            checking = Account(name="Checking", atype=AccountType.BANK, parent=root.handle)
            db.add_account(checking, txn)
            fund = Commodity(
                namespace="FUND",
                mnemonic="TDF2050",
                fullname="Target 2050",
                fraction=1000,
                quote_source="tsp",
            )
            db.add_commodity(fund, txn)
            account = Account(
                name="401(k)",
                atype=AccountType.RETIREMENT,
                parent=root.handle,
                commodity=fund.handle,
            )
            db.add_account(account, txn)
            db.add_scheduled(
                ScheduledTransaction(
                    name="Payroll",
                    recurrence=Recurrence(period=PeriodType.MONTH, interval=1, start=PAYDAY),
                    splits=[
                        ScheduledSplit(account.handle, Money("500.00")),
                        ScheduledSplit(checking.handle, Money("-500.00")),
                    ],
                    auto_create=True,
                ),
                txn,
            )
    finally:
        db.close()
    return account.handle


def _fund_quantities(path, account):
    from breadsched.gen.db.sqlite import DbSQLite

    db = DbSQLite()
    db.load(str(path))
    try:
        return [
            split.quantity
            for txn in db.iter_transactions()
            for split in txn.splits
            if split.account == account
        ]
    finally:
        db.close()


class _Fetcher:
    def __init__(self, value=None, when=PAYDAY):
        self.value = value
        self.when = when
        self.asked = []

    def fetch(self, requests, currency):
        from decimal import Decimal

        from breadsched.gen.services.quotes import FetchedQuote, QuoteFailure

        self.asked.extend(item.symbol for item in requests)
        if self.value is None:
            return [], [QuoteFailure(item.symbol, item.source, "offline") for item in requests]
        return [
            FetchedQuote(
                item.symbol, item.source, Decimal(self.value), currency, self.when, "tsp.gov"
            )
            for item in requests
        ], []


def test_cli_asks_for_a_price_online_quotes_cannot_give(tmp_path, capsys, monkeypatch):
    from breadsched.cli import quote_commands
    from breadsched.cli.main import main

    path = tmp_path / "shares.breadsched"
    account = _cli_book(path)
    offline = _Fetcher()
    monkeypatch.setattr(quote_commands, "_fetcher", lambda: offline)
    as_of = ["--as-of", TODAY.isoformat()]

    assert main(["due-review", str(path), *as_of, "--post", "Payroll"]) != 0
    error = capsys.readouterr().err
    assert "TDF2050" in error and "--share-price" in error
    assert offline.asked == ["TDF2050"]
    assert _fund_quantities(path, account) == []

    assert (
        main(["due-review", str(path), *as_of, "--post", "Payroll", "--shares", "tdf2050=12.5"])
        == 0
    )
    capsys.readouterr()
    assert _fund_quantities(path, account) == [Money("12.5")]


def test_cli_prices_shares_from_an_online_quote(tmp_path, capsys, monkeypatch):
    from breadsched.cli import quote_commands
    from breadsched.cli.main import main

    path = tmp_path / "shares.breadsched"
    account = _cli_book(path)
    online = _Fetcher("40")
    monkeypatch.setattr(quote_commands, "_fetcher", lambda: online)

    assert (
        main(["scheduled-next", str(path), "Payroll", "--as-of", TODAY.isoformat(), "--enter"]) == 0
    )
    capsys.readouterr()
    assert _fund_quantities(path, account) == [Money("12.5")]

    # A share price given on the command line wins over the stored quote.
    entered = ["scheduled-next", str(path), "Payroll", "--as-of", TODAY.isoformat(), "--enter"]
    assert main([*entered, "--share-price", "TDF2050=50"]) == 0
    capsys.readouterr()
    assert sorted(_fund_quantities(path, account)) == [Money(10), Money("12.5")]


def test_cli_automatic_posting_fetches_quotes_then_leaves_unpriced_dates_due(
    tmp_path, capsys, monkeypatch
):
    from breadsched.cli import quote_commands
    from breadsched.cli.main import main

    path = tmp_path / "shares.breadsched"
    account = _cli_book(path)
    monkeypatch.setattr(quote_commands, "_fetcher", lambda: _Fetcher())
    as_of = ["--as-of", TODAY.isoformat()]

    assert main(["scheduled", str(path), *as_of, "--post"]) == 0
    assert "Posted 0" in capsys.readouterr().out

    monkeypatch.setattr(quote_commands, "_fetcher", lambda: _Fetcher("50"))
    assert main(["scheduled", str(path), *as_of, "--post"]) == 0
    assert "Posted 1" in capsys.readouterr().out
    assert _fund_quantities(path, account) == [Money(10)]


@pytest.fixture
def web(db, monkeypatch):
    import json
    import threading
    import urllib.request

    from breadsched.web import quote_resource
    from breadsched.web.server import serve

    class Settings:
        def get(self, *_args, **_kwargs):
            return ""

    monkeypatch.setattr(quote_resource, "settings", Settings)
    httpd = serve(db, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def call(path, payload):
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "X-BreadSched-Token": httpd.token,
                "X-BreadSched-Local": httpd.local_token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    import urllib.error

    try:
        yield call
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_browser_fetches_a_quote_then_enters_typed_shares(db, payroll, web, monkeypatch):
    from breadsched.web import quote_resource

    schedule, account, fund, _usd = payroll
    with db.transaction("Quote source") as txn:
        fund.quote_source = "tsp"
        db.commit_commodity(fund, txn)
    offline = _Fetcher()
    monkeypatch.setattr(quote_resource, "make_fetcher", lambda _key: offline)
    occurrence = {"schedule": schedule.handle, "date": PAYDAY.isoformat()}

    status, found = web("/api/due-review/quotes", {"occurrences": [occurrence]})
    assert status == 200 and offline.asked == ["TDF2050"]
    [leg] = found["legs"]
    assert (leg["symbol"], leg["value"], leg["price"], leg["account"]) == (
        "TDF2050",
        "500.00",
        None,
        account.handle,
    )
    assert found["failures"] == [{"symbol": "TDF2050", "reason": "offline"}]

    # With no price the entry is refused and nothing is written.
    decision = {**occurrence, "decision": "post"}
    status, refused = web("/api/due-review", {"decisions": [decision]})
    assert (status, refused["code"]) == (400, "schedule.due.no_quote")
    assert list(db.iter_transactions()) == []

    # Shares typed in a comma-decimal browser, sent as an exact pair.
    typed = {**occurrence, "account": account.handle, "shares": [125, 10]}
    status, entered = web(
        "/api/due-review", {"decisions": [decision], "prices": [typed], "number_format": "comma"}
    )
    assert status == 200 and entered["posted"] == 1
    [txn] = db.iter_transactions()
    assert next(s for s in txn.splits if s.account == account.handle).quantity == Money("12.5")

    # A later payday fetches a newer quote than the price saved on the first.
    monkeypatch.setattr(quote_resource, "make_fetcher", lambda _key: _Fetcher("50", TODAY))
    july = {"schedule": schedule.handle, "date": date(2026, 12, 5).isoformat()}
    _status, fetched = web("/api/due-review/quotes", {"occurrences": [july]})
    [priced] = fetched["legs"]
    assert (priced["price"], priced["shares"], priced["price_source"]) == (
        "50.00",
        "10",
        "Online: tsp.gov",
    )
