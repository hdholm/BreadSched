"""Finding transactions across the book, with sorting, groups, and totals (#382)."""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.engine.transaction_query import TransactionQuery, run_query
from breadsched.gen.lib import Money, Transaction


@pytest.fixture
def activity(db, book):
    entries = [
        (date(2026, 1, 5), "Corner grocer", book.groceries, book.checking, "67.00", ["food"]),
        (date(2026, 1, 20), "Check 1067", book.rent, book.checking, "1067.25", []),
        (date(2026, 2, 3), "Market", book.groceries, book.card, "42.10", ["food", "Tax"]),
        (date(2026, 3, 1), "Pay", book.checking, book.salary, "2500.00", []),
    ]
    made = {}
    with db.transaction("Activity") as txn:
        for when, text, to, source, amount, tags in entries:
            transaction = Transaction.simple(when, text, to, source, amount)
            transaction.tags = tags
            db.add_transaction(transaction, txn)
            made[text] = transaction
    return made


def test_an_amount_finds_only_legs_of_that_size(db, book, activity):
    """67 finds the 67.00 charge's two legs, never check 1067 (#382)."""
    result = run_query(db, TransactionQuery(amount=Money("67")))
    assert {row.transaction.description for row in result.rows} == {"Corner grocer"}
    assert sorted(row.value for row in result.rows) == [Money("-67.00"), Money("67.00")]
    assert result.transactions == 1
    # Text, by contrast, searches everything a transaction holds.
    texts = run_query(db, TransactionQuery(text="67"))
    assert {row.transaction.description for row in texts.rows} == {"Corner grocer", "Check 1067"}


def test_accounts_dates_and_direction_choose_the_splits(db, book, activity):
    expenses = run_query(
        db,
        TransactionQuery(accounts=(book.expenses,), start=date(2026, 1, 1), end=date(2026, 1, 31)),
    )
    assert [(row.transaction.description, row.account_name) for row in expenses.rows] == [
        ("Corner grocer", "Expenses:Groceries"),
        ("Check 1067", "Expenses:Rent"),
    ]
    assert expenses.totals == {"USD": Money("1134.25")}
    # Without subaccounts the placeholder parent holds nothing.
    assert run_query(db, TransactionQuery(accounts=(book.expenses,), subaccounts=False)).rows == []
    out = run_query(db, TransactionQuery(accounts=(book.checking,), direction="credit"))
    assert [row.value for row in out.rows] == [Money("-67.00"), Money("-1067.25")]
    into = run_query(db, TransactionQuery(accounts=(book.checking,), direction="debit"))
    assert [row.transaction.description for row in into.rows] == ["Pay"]


def test_a_range_of_amounts_and_tags(db, book, activity):
    middling = run_query(
        db,
        TransactionQuery(
            accounts=(book.expenses,), amount_min=Money("40"), amount_max=Money("100")
        ),
    )
    assert [row.transaction.description for row in middling.rows] == ["Corner grocer", "Market"]
    taxed = run_query(db, TransactionQuery(tags=("tax",), accounts=(book.groceries,)))
    assert [row.transaction.description for row in taxed.rows] == ["Market"]


def test_rows_sort_and_group_with_subtotals(db, book, activity):
    by_month = run_query(
        db,
        TransactionQuery(
            accounts=(book.expenses,), group_by="month", sort="amount", descending=True
        ),
    )
    assert [(group.key, group.totals) for group in by_month.groups] == [
        ("2026-01", {"USD": Money("1134.25")}),
        ("2026-02", {"USD": Money("42.10")}),
    ]
    assert [row.value for row in by_month.groups[0].rows] == [Money("1067.25"), Money("67.00")]
    assert by_month.totals == {"USD": Money("1176.35")}
    by_account = run_query(db, TransactionQuery(accounts=(book.expenses,), group_by="account"))
    assert [group.key for group in by_account.groups] == ["Expenses:Groceries", "Expenses:Rent"]
    by_tag = run_query(db, TransactionQuery(accounts=(book.groceries,), group_by="tag"))
    assert {group.key: group.totals["USD"] for group in by_tag.groups} == {
        "food": Money("109.10"),
        "Tax": Money("42.10"),
    }
    # A split in two tag groups still counts once in the grand total.
    assert by_tag.totals == {"USD": Money("109.10")}


def test_a_balanced_book_totals_to_zero_across_every_split(db, book, activity):
    everything = run_query(db, TransactionQuery())
    assert len(everything.rows) == 8 and everything.totals == {"USD": Money(0)}


@pytest.mark.parametrize(
    ("query", "words"),
    [
        (TransactionQuery(direction="sideways"), "direction"),
        (TransactionQuery(sort="colour"), "sort"),
        (TransactionQuery(group_by="week"), "group by"),
        (TransactionQuery(start=date(2026, 2, 1), end=date(2026, 1, 1)), "start date"),
        (TransactionQuery(amount_min=Money("5"), amount_max=Money("1")), "smallest"),
    ],
)
def test_a_query_that_cannot_run_says_why(db, book, query, words):
    with pytest.raises(ValueError, match=words):
        run_query(db, query)


def test_the_command_line_finds_groups_and_totals(tmp_path, capsys):
    import json

    from breadsched.cli.main import main as cli
    from breadsched.gen.sample_book import create_sample_book

    path = tmp_path / "find.breadsched"
    create_sample_book(path, as_of=date(2026, 8, 15))
    assert cli(["find", str(path), "--account", "Expenses", "--group-by", "month", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["splits"] > 0 and payload["groups"]
    months = [group["key"] for group in payload["groups"]]
    assert months == sorted(months)
    total = sum((Money(group["totals"]["USD"]) for group in payload["groups"]), Money(0))
    assert Money(payload["totals"]["USD"]) == total
    assert all(
        row["account_name"].startswith("Expenses:") for g in payload["groups"] for row in g["rows"]
    )

    assert cli(["find", str(path), "--account", "Expenses", "--group-by", "month"]) == 0
    text = capsys.readouterr().out
    assert "subtotal:" in text and "split(s) in" in text

    assert cli(["find", str(path), "--amount", "lots"]) == 2
    assert "--amount: 'lots' is not an amount" in capsys.readouterr().err
    assert cli(["find", str(path), "--start", "2026-09-01", "--end", "2026-01-01"]) == 2
    assert "The first date is after the last" in capsys.readouterr().err


def test_subtotals_nest_in_the_order_chosen(db, book, activity):
    """#408: subtotals by account, then month, then description, each with its total."""
    activity["Corner grocer"].notes = "weekly shop"
    with db.transaction("Notes") as txn:
        db.commit_transaction(activity["Corner grocer"], txn)
    result = run_query(
        db,
        TransactionQuery(
            accounts=(book.groceries, book.rent),
            subtotals=("account", "month", "description"),
        ),
    )
    assert result.query.levels == ("account", "month", "description")
    groceries, rent = result.groups
    assert (groceries.key, groceries.depth, groceries.totals) == (
        "Expenses:Groceries",
        0,
        {"USD": Money("109.10")},
    )
    assert [(child.key, child.depth) for child in groceries.children] == [
        ("2026-01", 1),
        ("2026-02", 1),
    ]
    [shop] = groceries.children[0].children
    assert (shop.key, shop.depth, [row.value for row in shop.rows]) == (
        "Corner grocer",
        2,
        [Money("67.00")],
    )
    assert rent.totals == {"USD": Money("1067.25")}
    assert result.totals == {"USD": Money("1176.35")}

    by_notes = run_query(db, TransactionQuery(accounts=(book.groceries,), subtotals=("notes",)))
    assert [group.key for group in by_notes.groups] == ["(no notes)", "weekly shop"]
    # The one-level form still works, and the two agree.
    assert [g.key for g in run_query(db, TransactionQuery(group_by="month")).groups] == [
        g.key for g in run_query(db, TransactionQuery(subtotals=("month",))).groups
    ]


@pytest.mark.parametrize(
    ("subtotals", "words"),
    [(("account", "account"), "chosen once"), (("week",), "subtotals"), (("none",), "subtotals")],
)
def test_subtotals_that_cannot_run_say_why(db, book, subtotals, words):
    query = TransactionQuery(subtotals=subtotals)
    assert any(words in problem for problem in query.problems())


def test_the_command_line_nests_subtotals(tmp_path, capsys):
    import json

    from breadsched.cli.main import main as cli
    from breadsched.gen.sample_book import create_sample_book

    path = tmp_path / "find.breadsched"
    create_sample_book(path, as_of=date(2026, 8, 15))
    command = [
        "find",
        str(path),
        "--account",
        "Expenses:Groceries",
        "--account",
        "Expenses:Housing",
        "--subtotal",
        "account",
        "--subtotal",
        "month",
    ]
    assert cli([*command, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subtotals"] == ["account", "month"]
    assert [group["key"] for group in payload["groups"]] == [
        "Expenses:Groceries",
        "Expenses:Housing",
    ]
    assert all(group["children"] and group["depth"] == 0 for group in payload["groups"])
    assert {child["depth"] for g in payload["groups"] for child in g["children"]} == {1}
    assert cli(command) == 0
    assert "  subtotal:" in capsys.readouterr().out


def test_the_transaction_detail_layout_prints_nested_groups(db, book, activity):
    from breadsched.gen.services.transaction_search import find_transactions
    from breadsched.plugins.export.report_layout import transaction_detail_layout

    result = find_transactions(
        db, TransactionQuery(accounts=(book.expenses,), subtotals=("account", "month"))
    ).value
    document = transaction_detail_layout(result)
    assert (document.title, document.kind) == ("Transaction detail", "transaction-detail")
    assert "subtotals by account, then month" in document.subtitle
    text = document.text()
    for expected in ("Expenses:Groceries", "2026-01", "Subtotal: 2026-02", "Corner grocer"):
        assert expected in text, expected
    assert text.index("Subtotal: Expenses:Groceries") < text.index("Expenses:Rent")
