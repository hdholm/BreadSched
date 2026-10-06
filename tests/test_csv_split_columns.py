"""CSV statements whose rows are split across several categories.

Each mapped (category column, amount column) pair is one split. Nothing is
inferred: every filled pair needs both cells, each category must already be an
account, and the filled amounts must add up exactly to the row's amount in the
row's own sign convention, or the row is refused. No balancing split is invented.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from breadsched.gen.lib import Money
from breadsched.gen.services.csv_import import (
    CsvImportRequest,
    CsvMapping,
    import_csv,
    preview_csv_import,
)

STATEMENT = """Date,Description,Amount,Cat 1,Amt 1,Cat 2,Amt 2
2026-09-01,Warehouse store,-150.00,Groceries,-100.00,Utilities,-50.00
2026-09-02,Corner Grocer,-20.00,Groceries,-20.00,,
2026-09-03,Short split,-30.00,Groceries,-10.00,Utilities,-10.00
2026-09-04,Unmapped,-5.00,,,,
"""

SPLITS = (("Cat 1", "Amt 1"), ("Cat 2", "Amt 2"))


def _request(path, account, **mapping):
    fields = {"date": "Date", "amount": "Amount", "description": "Description", "splits": SPLITS}
    fields.update(mapping)
    return CsvImportRequest(source=str(path), account=account, mapping=CsvMapping(**fields))


@pytest.fixture
def statement(tmp_path):
    path = tmp_path / "split.csv"
    path.write_text(STATEMENT, encoding="utf-8")
    return path


def _by_description(db):
    return {item.description: item for item in db.iter_transactions()}


def test_preview_maps_each_filled_pair_to_a_split(db, book, statement):
    before = len(list(db.iter_transactions()))

    preview = preview_csv_import(db, _request(statement, book.checking)).value

    assert preview is not None
    rows = {row.description: row for row in preview.rows}
    assert rows["Warehouse store"].status == "new"
    assert rows["Warehouse store"].splits == (
        (book.groceries, Money("-100.00")),
        (book.utilities, Money("-50.00")),
    )
    assert rows["Corner Grocer"].splits == ((book.groceries, Money("-20.00")),)
    assert (rows["Short split"].status, rows["Short split"].reason) == (
        "invalid",
        "the splits add up to -20.00, but the row's amount is -30.00",
    )
    assert rows["Unmapped"].status == "new" and rows["Unmapped"].splits == ()
    assert len(list(db.iter_transactions())) == before


def test_import_posts_balanced_splits_and_reimport_adds_nothing(db, book, statement):
    request = _request(statement, book.checking)

    imported = import_csv(db, request).value

    assert imported is not None
    assert imported.result.transactions_new == 3
    posted = _by_description(db)
    warehouse = posted["Warehouse store"]
    assert sorted((split.account, split.value) for split in warehouse.splits) == sorted(
        [
            (book.checking, Money("-150.00")),
            (book.groceries, Money("100.00")),
            (book.utilities, Money("50.00")),
        ]
    )
    assert sum((split.value for split in warehouse.splits), Money(0)) == Money(0)
    assert "Short split" not in posted
    # A row with no filled pair posts to the uncategorized account as before.
    unmapped_accounts = {db.full_name(split.account) for split in posted["Unmapped"].splits}
    assert "Expenses:Uncategorized CSV" in unmapped_accounts

    count = len(list(db.iter_transactions()))
    again = import_csv(db, request).value
    assert again is not None
    assert again.result.transactions_new == 0
    assert len(list(db.iter_transactions())) == count


def test_split_columns_and_one_category_column_are_refused_together(db, book, statement):
    result = preview_csv_import(db, _request(statement, book.checking, category="Cat 1"))
    assert not result.ok
    assert result.errors[0].code == "import.csv.split.mapping"
    assert result.errors[0].fields == ("splits",)

    half = preview_csv_import(db, _request(statement, book.checking, splits=(("Cat 1", ""),)))
    assert not half.ok and half.errors[0].code == "import.csv.split.mapping"

    missing = preview_csv_import(db, _request(statement, book.checking, splits=(("Cat 9", "X"),)))
    assert not missing.ok and missing.errors[0].code == "import.csv.column.not_found"


def test_half_filled_pairs_and_unknown_categories_refuse_the_row(db, book, tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text(
        "Date,Description,Amount,Cat 1,Amt 1,Cat 2,Amt 2\n"
        "2026-09-01,No amount,-10.00,Groceries,,,\n"
        "2026-09-02,No category,-10.00,,-10.00,,\n"
        "2026-09-03,Unknown,-10.00,Nowhere,-10.00,,\n"
        "2026-09-04,Bad number,-10.00,Groceries,ten,,\n",
        encoding="utf-8",
    )
    before = {item.handle for item in db.iter_accounts()}

    preview = preview_csv_import(db, _request(path, book.checking)).value

    assert preview is not None
    assert [(row.description, row.status, row.reason) for row in preview.rows] == [
        ("No amount", "invalid", "split 1 has a category but no amount"),
        ("No category", "invalid", "split 1 has an amount but no category"),
        ("Unknown", "invalid", "split 1: category 'Nowhere' is not an account in the book"),
        ("Bad number", "invalid", preview.rows[3].reason),
    ]
    assert preview.rows[3].reason.startswith("bad split 1 amount")
    imported = import_csv(db, _request(path, book.checking)).value
    assert imported is not None and imported.result.transactions_new == 0
    assert {item.handle for item in db.iter_accounts()} == before


def test_split_amounts_follow_the_row_sign_convention(db, book, tmp_path):
    """With money out shown positive, the split amounts are flipped with the row."""
    path = tmp_path / "positive.csv"
    path.write_text(
        "Date,Description,Debit,Credit,Cat 1,Amt 1,Cat 2,Amt 2\n"
        "2026-09-01,Card purchase,,150.00,Groceries,100.00,Utilities,50.00\n",
        encoding="utf-8",
    )
    request = _request(path, book.card, amount=None, debit="Debit", credit="Credit", invert=True)

    preview = preview_csv_import(db, request).value

    assert preview is not None
    [row] = preview.rows
    assert row.amount == Money("-150.00")
    assert row.splits == ((book.groceries, Money("-100.00")), (book.utilities, Money("-50.00")))


def test_a_split_row_is_not_offered_as_a_transfer(db, book, tmp_path):
    savings = tmp_path / "savings.csv"
    savings.write_text("Date,Description,Amount\n2026-09-01,To checking,-75.00\n", "utf-8")
    first = import_csv(
        db,
        CsvImportRequest(
            source=str(savings),
            account=book.savings,
            mapping=CsvMapping(date="Date", amount="Amount", description="Description"),
        ),
    )
    assert first.ok, first.errors
    checking = tmp_path / "checking.csv"
    checking.write_text(
        "Date,Description,Amount,Cat 1,Amt 1,Cat 2,Amt 2\n"
        "2026-09-01,From savings,75.00,Salary,75.00,,\n",
        encoding="utf-8",
    )

    preview = preview_csv_import(db, _request(checking, book.checking)).value

    assert preview is not None
    assert preview.rows[0].status == "new"
    assert preview.rows[0].splits == ((book.salary, Money("75.00")),)


def test_cli_maps_split_columns(tmp_path, capsys):
    from breadsched.cli.main import main
    from breadsched.gen.sample_book import create_sample_book

    book_path = tmp_path / "cli.breadsched"
    create_sample_book(book_path, as_of=date(2026, 8, 15))
    path = tmp_path / "split.csv"
    path.write_text(
        "Date,Description,Amount,Cat 1,Amt 1,Cat 2,Amt 2\n"
        "2026-09-01,Warehouse,-30.00,Expenses:Groceries,-20.00,Expenses:Utilities,-10.00\n",
        encoding="utf-8",
    )
    command = [
        "import-csv",
        str(book_path),
        str(path),
        "--account",
        "Checking",
        "--date",
        "Date",
        "--amount",
        "Amount",
        "--description",
        "Description",
        "--split",
        "Cat 1=Amt 1",
        "--split",
        "Cat 2=Amt 2",
    ]

    assert main([*command, "--preview", "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)["rows"]
    assert row["splits"] == [
        {"category": "Expenses:Groceries", "amount": "-20.00"},
        {"category": "Expenses:Utilities", "amount": "-10.00"},
    ]
    assert main([*command, "--preview"]) == 0
    assert "Expenses:Groceries (20.00); Expenses:Utilities (10.00)" in capsys.readouterr().out
    assert main([*command, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["transactions_new"] == 1

    assert main([*command[:-2], "--split", "Cat 2", "--preview"]) == 2
    assert "CATEGORY=AMOUNT" in capsys.readouterr().err
