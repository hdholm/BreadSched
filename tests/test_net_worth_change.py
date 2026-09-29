"""A net worth change drills down to the postings that made it, and the rest is
market and exchange-rate movement, so the two always reconcile exactly."""

import csv
from datetime import date

from breadsched.gen.engine import valuation
from breadsched.gen.lib import (
    Account,
    AccountType,
    Commodity,
    CommodityPrice,
    Money,
    Split,
    Transaction,
)
from breadsched.gen.services import query_net_worth_change, query_net_worth_history
from breadsched.plugins.export.csv_export import export_net_worth_change
from breadsched.plugins.export.html_report import net_worth_change_report


def test_postings_explain_the_whole_change_and_match_the_history(db, funded_book):
    book = funded_book
    with db.transaction("Pay the card") as txn:
        db.add_transaction(
            Transaction.simple(date(2026, 2, 16), "Card payment", book.card, book.checking, "50"),
            txn,
        )
    result = query_net_worth_change(db, date(2026, 2, 1), date(2026, 2, 28), date(2026, 2, 20))
    assert result.value is not None
    change = result.value
    assert (change.opening_on, change.closing_on, change.partial) == (
        date(2026, 1, 31),
        date(2026, 2, 20),
        True,
    )
    assert [(p.posted, p.description, p.effect) for p in change.postings] == [
        (date(2026, 2, 3), "February rent", Money("-1800.00")),
        (date(2026, 2, 14), "Dinner out", Money("-86.40")),
    ]
    assert change.postings[1].accounts == (db.full_name(book.card),)
    # Paying the card moves money between the household's own accounts.
    assert change.transfers == 1
    assert change.posted == change.change == Money("-1886.40")
    assert change.revaluation == Money(0)
    assert not change.missing
    # The carried closing balances equal a fresh valuation of the whole ledger.
    assert change.closing == valuation.net_worth(db, as_of=change.closing_on)
    assert change.opening == valuation.net_worth(db, as_of=change.opening_on)
    history = query_net_worth_history(
        db, date(2026, 1, 1), date(2026, 2, 28), today=date(2026, 2, 20)
    ).value
    assert history is not None
    assert history.points[-1].change == change.change
    assert (change.opening, change.closing) == (
        history.points[0].net_worth,
        history.points[1].net_worth,
    )


def _foreign_cash(db, book):
    usd = db.get_commodity_by_mnemonic("USD")
    assert usd is not None
    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    account = Account(
        name="Foreign cash", atype=AccountType.BANK, parent=book.assets, commodity=euro.handle
    )
    with db.transaction("Foreign cash") as txn:
        db.add_commodity(euro, txn)
        db.add_account(account, txn)
    return usd, euro, account


def _euros(db, book, euro, account, when, amount, description):
    transaction = Transaction(post_date=when, description=description)
    transaction.currency = euro.handle
    transaction.splits = [Split(account.handle, Money(amount)), Split(book.opening, -Money(amount))]
    with db.transaction(description) as txn:
        db.add_transaction(transaction, txn)


def _quote(db, euro, usd, when, value):
    with db.transaction("Quote") as txn:
        db.add_price(
            CommodityPrice(
                commodity=euro.handle,
                currency=usd.handle,
                quote_date=when,
                value=Money(value),
                source="sample-source",
            ),
            txn,
        )


def test_exchange_rate_movement_is_the_revaluation(db, funded_book):
    book = funded_book
    usd, euro, account = _foreign_cash(db, book)
    _quote(db, euro, usd, date(2026, 2, 10), 2)
    _quote(db, euro, usd, date(2026, 2, 25), 3)
    _euros(db, book, euro, account, date(2026, 2, 12), 10, "Euros bought")
    result = query_net_worth_change(db, date(2026, 2, 1), date(2026, 2, 28), date(2026, 3, 5))
    assert result.value is not None
    change = result.value
    euros = next(p for p in change.postings if p.description == "Euros bought")
    # Converted at the quote applicable on its posting date, not the closing one.
    assert (euros.effect, euros.currency) == (Money(20), "EUR")
    assert change.posted == Money("-1866.40")
    assert change.change == Money("-1856.40")
    assert change.revaluation == Money(10)
    assert change.posted + change.revaluation == change.change
    assert not change.partial


def test_a_missing_quote_withholds_totals_and_names_the_posting(db, funded_book):
    book = funded_book
    usd, euro, account = _foreign_cash(db, book)
    _euros(db, book, euro, account, date(2026, 2, 5), 10, "Euros before any quote")
    _quote(db, euro, usd, date(2026, 2, 10), 2)
    result = query_net_worth_change(db, date(2026, 2, 1), date(2026, 2, 28), date(2026, 3, 5))
    assert result.value is not None
    change = result.value
    euros = next(p for p in change.postings if p.description.startswith("Euros"))
    assert euros.effect is None and euros.currency == "EUR"
    assert change.missing == ("2026-02-05 Euros before any quote",)
    # The closing value converts, but nothing is added up with a guessed rate.
    assert change.closing is not None and change.change is not None
    assert change.posted is None and change.revaluation is None


def test_invalid_windows_are_rejected(db, funded_book):
    backwards = query_net_worth_change(db, date(2026, 2, 1), date(2026, 1, 1))
    assert not backwards.ok and backwards.errors[0].code == "net_worth.range.invalid"
    future = query_net_worth_change(db, date(2026, 3, 1), date(2026, 3, 31), date(2026, 2, 1))
    assert not future.ok and future.errors[0].code == "net_worth.range.future"


def test_printout_and_export_carry_the_same_totals(db, funded_book, tmp_path):
    book = funded_book
    usd, euro, account = _foreign_cash(db, book)
    _quote(db, euro, usd, date(2026, 2, 10), 2)
    _quote(db, euro, usd, date(2026, 2, 25), 3)
    _euros(db, book, euro, account, date(2026, 2, 12), 10, "Euros bought")
    change = query_net_worth_change(db, date(2026, 2, 1), date(2026, 2, 28), date(2026, 3, 5)).value
    assert change is not None
    html = net_worth_change_report(change)
    path = tmp_path / "change.csv"
    assert export_net_worth_change(change, path) == 3
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    assert rows[0][4] == "net worth effect"
    totals = {row[1]: row[4] for row in rows[4:]}
    assert totals == {
        "Opening net worth": "7089.45",
        "Postings": "-1866.40",
        "Market and exchange-rate changes": "10.00",
        "Closing net worth": "5233.05",
        "Change": "-1856.40",
    }
    for text in ("7,089.45", "(1,866.40)", "10.00", "5,233.05", "(1,856.40)", "Euros bought"):
        assert text in html, text
    assert rows[2][:5] == ["2026-02-12", "Euros bought", "Assets:Foreign cash", "EUR", "20.00"]
