"""Net worth history values the whole book at each period end, like the Dashboard."""

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
from breadsched.gen.services import query_net_worth_history
from breadsched.plugins.export.html_report import net_worth_history_report


def test_points_value_each_period_end_through_today(db, funded_book):
    book = funded_book
    result = query_net_worth_history(
        db, date(2026, 1, 1), date(2026, 4, 30), "month", today=date(2026, 2, 20)
    )
    assert result.value is not None
    history = result.value
    # March and April start after today: a ledger has no future balances.
    assert [point.label for point in history.points] == ["Jan 2026", "Feb 2026"]
    january, february = history.points
    assert (january.valued_on, january.partial) == (date(2026, 1, 31), False)
    assert (february.valued_on, february.partial) == (date(2026, 2, 20), True)
    # 5000 + 4200 - 1800 - 310.55 in January; February pays rent from checking
    # and puts dinner on the card, a debt.
    assert january.assets == Money("7089.45")
    assert january.debts == Money(0)
    assert january.net_worth == Money("7089.45")
    assert february.assets == Money("5289.45")
    assert february.debts == Money("86.40")
    assert february.net_worth == Money("5203.05")
    assert january.change is None
    assert february.change == Money("-1886.40")
    for point in history.points:
        assert point.net_worth == valuation.net_worth(db, as_of=point.valued_on)
        assert not point.missing
        assets = sum((line.value for line in point.lines if line.kind == "asset"), Money(0))
        debts = sum((line.value for line in point.lines if line.kind == "debt"), Money(0))
        assert (assets, debts) == (point.assets, point.debts)
    assert {(line.name, line.kind) for line in february.lines} == {
        ("Assets", "asset"),
        ("Liabilities", "debt"),
    }
    assert all(line.account in {book.assets, book.liabilities} for line in february.lines)


def test_quarter_grouping_and_invalid_requests(db, funded_book):
    result = query_net_worth_history(
        db, date(2026, 1, 1), date(2026, 12, 31), "quarter", today=date(2026, 8, 1)
    )
    assert result.value is not None
    assert [(p.label, p.valued_on, p.partial) for p in result.value.points] == [
        ("Q1 2026", date(2026, 3, 31), False),
        ("Q2 2026", date(2026, 6, 30), False),
        ("Q3 2026", date(2026, 8, 1), True),
    ]
    backwards = query_net_worth_history(db, date(2026, 2, 1), date(2026, 1, 1))
    assert not backwards.ok and backwards.errors[0].code == "net_worth.range.invalid"
    unknown = query_net_worth_history(db, date(2026, 1, 1), date(2026, 2, 1), "week")
    assert not unknown.ok and unknown.errors[0].code == "net_worth.period.invalid"


def test_missing_quote_withholds_totals_and_names_the_account(db, book):
    usd = db.get_commodity_by_mnemonic("USD")
    assert usd is not None
    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    account = Account(
        name="Foreign cash", atype=AccountType.BANK, parent=book.assets, commodity=euro.handle
    )
    transaction = Transaction(post_date=date(2026, 1, 2), description="Opening cash")
    transaction.currency = euro.handle
    transaction.splits = [Split(account.handle, Money(10)), Split(book.opening, Money(-10))]
    with db.transaction("Foreign cash") as txn:
        db.add_commodity(euro, txn)
        db.add_account(account, txn)
        db.add_transaction(transaction, txn)
        db.add_price(
            CommodityPrice(
                commodity=euro.handle,
                currency=usd.handle,
                quote_date=date(2026, 2, 10),
                value=Money(2),
                source="sample-source",
            ),
            txn,
        )
    result = query_net_worth_history(
        db, date(2026, 1, 1), date(2026, 2, 28), today=date(2026, 3, 5)
    )
    assert result.value is not None
    january, february = result.value.points
    # No quote existed on January 31: nothing is presented as converted.
    assert january.net_worth is None and january.assets is None and january.change is None
    assert january.missing == ("Assets:Foreign cash",)
    assert next(line for line in january.lines if line.kind == "asset").value is None
    # The February 10 quote converts the balance from then on.
    assert february.missing == ()
    assert february.net_worth == Money(20)
    assert february.change is None
    html = net_worth_history_report(result.value)
    assert "<h1>Net worth history</h1>" in html or "Net worth history" in html
    assert "missing quote: Assets:Foreign cash" in html
    assert "20.00" in html


def test_carried_balances_match_a_full_valuation_every_month(db, book):
    """Each point adds only the splits since the previous one; it must still equal
    a fresh valuation of the whole ledger on its date."""
    with db.transaction("A year of activity") as txn:
        db.add_transaction(
            Transaction.simple(date(2025, 12, 31), "Opening", book.checking, book.opening, "900"),
            txn,
        )
        for month in range(1, 13):
            db.add_transaction(
                Transaction.simple(date(2026, month, 1), "Pay", book.checking, book.salary, "100"),
                txn,
            )
            db.add_transaction(
                Transaction.simple(
                    date(2026, month, month + 10), "Card", book.groceries, book.card, "7.5"
                ),
                txn,
            )
    result = query_net_worth_history(
        db, date(2026, 1, 1), date(2026, 12, 31), today=date(2026, 12, 31)
    )
    assert result.value is not None
    assert len(result.value.points) == 12
    for point in result.value.points:
        assert point.net_worth == valuation.net_worth(db, as_of=point.valued_on), point.label
    assert result.value.points[-1].net_worth == Money(900 + 1200 - 90)


def test_groups_break_a_lone_tree_into_its_children_and_reconcile(db, funded_book):
    book = funded_book
    history = query_net_worth_history(
        db, date(2026, 1, 1), date(2026, 2, 28), today=date(2026, 2, 20)
    ).value
    assert history is not None
    february = history.points[1]
    # Every asset sits under "Assets" and every debt under "Liabilities", so the
    # groups are their children; the top-level lines are the groups' exact sums.
    names = {(line.name, line.kind) for line in february.groups}
    assert ("Assets:Checking", "asset") in names
    assert ("Liabilities:Credit Card", "debt") in names
    assert all(not name.endswith(("Assets", "Liabilities")) for name, _ in names)
    for kind, total in (("asset", february.assets), ("debt", february.debts)):
        assert sum((line.value for line in february.groups if line.kind == kind), Money(0)) == total
    assert {line.account for line in february.lines} <= {book.assets, book.liabilities}


def test_net_worth_charts_draw_the_points_own_values(db, funded_book):
    from breadsched.gen.services import net_worth_charts

    history = query_net_worth_history(
        db, date(2026, 1, 1), date(2026, 2, 28), today=date(2026, 2, 20)
    ).value
    assert history is not None
    lines, composition = net_worth_charts(history, "USD")
    assert (lines.key, lines.kind, lines.currency) == ("net_worth", "line", "USD")
    assert [series.name for series in lines.series] == ["Assets", "Debts", "Net worth"]
    assert [series.slot for series in lines.series] == [1, 2, 3]
    assert lines.series[2].values == tuple(point.net_worth for point in history.points)
    # February is valued to date: the as-of rule marks it.
    assert [(m.index, m.label) for m in lines.markers] == [(1, "As of 2026-02-20")]
    assert not lines.partial_note
    assert (composition.key, composition.kind) == ("net_worth_composition", "stacked")
    assert composition.totals == tuple(point.net_worth for point in history.points)
    # Debts stack below zero, and each column sums to its net worth exactly.
    card = next(s for s in composition.series if s.name == "Liabilities:Credit Card (debt)")
    assert card.values[1] == Money("-86.40")
    for index, point in enumerate(history.points):
        column = [series.values[index] or Money(0) for series in composition.series]
        assert sum(column, Money(0)) == point.net_worth
    # Assets come first, then debts; slots follow that order.
    kinds = [series.key.split(":")[0] for series in composition.series]
    assert kinds == sorted(kinds, key=lambda kind: kind != "asset")
    assert [series.slot for series in composition.series] == list(
        range(1, len(composition.series) + 1)
    )


def test_a_withheld_point_breaks_the_lines_and_the_charts_say_so(db, book):
    from breadsched.gen.services import net_worth_charts

    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    account = Account(
        name="Foreign cash", atype=AccountType.BANK, parent=book.assets, commodity=euro.handle
    )
    transaction = Transaction(post_date=date(2026, 1, 2), description="Opening cash")
    transaction.currency = euro.handle
    transaction.splits = [Split(account.handle, Money(10)), Split(book.opening, Money(-10))]
    with db.transaction("Foreign cash") as txn:
        db.add_commodity(euro, txn)
        db.add_account(account, txn)
        db.add_transaction(transaction, txn)
    history = query_net_worth_history(
        db, date(2026, 1, 1), date(2026, 1, 31), today=date(2026, 3, 5)
    ).value
    assert history is not None
    lines, composition = net_worth_charts(history)
    assert lines.series[2].values == (None,)
    assert all(series.values == (None,) for series in composition.series)
    assert lines.partial_note.startswith("Missing quote, so left out: Jan 2026")
    html = net_worth_history_report(history)
    assert "Missing quote, so left out: Jan 2026" in html


def test_more_than_eight_groups_combine_as_other(db, book):
    from breadsched.gen.services import net_worth_charts

    with db.transaction("Accounts and money") as txn:
        for number in range(10):
            account = Account(name=f"Pot {number}", atype=AccountType.BANK, parent=book.assets)
            db.add_account(account, txn)
            db.add_transaction(
                Transaction.simple(
                    date(2026, 1, 3), "Fund", account.handle, book.opening, str(100 * (number + 1))
                ),
                txn,
            )
    history = query_net_worth_history(
        db, date(2026, 1, 1), date(2026, 1, 31), today=date(2026, 3, 5)
    ).value
    assert history is not None
    _lines, composition = net_worth_charts(history)
    names = [series.name for series in composition.series]
    assert len(names) == 8 and names[-1] == "Other"
    assert names[0] == "Assets:Pot 9"
    assert composition.series[-1].values == (Money(100 + 200 + 300),)
    assert (
        sum((series.values[0] or Money(0) for series in composition.series), Money(0))
        == history.points[0].net_worth
    )
