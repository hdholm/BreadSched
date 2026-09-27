"""User-mapped CSV statement import: preview, validation, identity, and review.

Rows are mapped by column name (or 1-based position without a header). Nothing is
written until the preview has been accepted. Re-importing the same rows never
duplicates or recategorizes them, and a row matching an existing transaction in
the target account on the same date and amount waits for explicit inclusion.
"""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.lib import Money, Transaction
from breadsched.gen.services.csv_import import (
    CsvImportRequest,
    CsvMapping,
    import_csv,
    preview_csv_import,
)

STATEMENT = """Date,Description,Amount,Memo
2026-09-01,Corner Grocer,-42.10,card 1234
2026-09-02,Payroll,1500.00,
2026-09-03,Coffee,-3.50,
2026-09-03,Coffee,-3.50,
"""


@pytest.fixture
def statement(tmp_path):
    path = tmp_path / "statement.csv"
    path.write_text(STATEMENT, encoding="utf-8")
    return path


def _request(path, account, **mapping):
    fields = {"date": "Date", "amount": "Amount", "description": "Description", "memo": "Memo"}
    fields.update(mapping)
    return CsvImportRequest(source=str(path), account=account, mapping=CsvMapping(**fields))


def _count(db):
    return len(list(db.iter_transactions()))


def test_preview_parses_rows_without_writing(db, book, statement):
    before = _count(db)

    result = preview_csv_import(db, _request(statement, book.checking))

    assert result.ok, result.errors
    preview = result.value
    assert (preview.date_format, preview.number_format, preview.delimiter) == ("iso", "dot", ",")
    assert [
        (row.line, row.when, row.amount, row.description, row.status) for row in preview.rows
    ] == [
        (2, date(2026, 9, 1), Money("-42.10"), "Corner Grocer", "new"),
        (3, date(2026, 9, 2), Money("1500.00"), "Payroll", "new"),
        (4, date(2026, 9, 3), Money("-3.50"), "Coffee", "new"),
        (5, date(2026, 9, 3), Money("-3.50"), "Coffee", "new"),
    ]
    assert preview.rows[0].memo == "card 1234"
    assert _count(db) == before


def test_import_posts_rows_and_reimport_is_idempotent(db, book, statement):
    before = _count(db)

    first = import_csv(db, _request(statement, book.checking))

    assert first.ok, first.errors
    assert first.value.result.transactions_new == 4
    assert _count(db) == before + 4
    posted = [
        txn
        for txn in db.iter_transactions()
        if any(split.account == book.checking for split in txn.splits)
        and txn.description == "Corner Grocer"
    ]
    [grocer] = posted
    assert {split.value for split in grocer.splits} == {Money("-42.10"), Money("42.10")}
    counter = next(split for split in grocer.splits if split.account != book.checking)
    assert db.get_account(counter.account).name == "Uncategorized CSV"

    again = preview_csv_import(db, _request(statement, book.checking))
    assert [row.status for row in again.value.rows] == ["imported"] * 4
    second = import_csv(db, _request(statement, book.checking))
    assert second.value.result.transactions_new == 0
    assert second.value.result.transactions_unchanged == 4
    assert _count(db) == before + 4


def test_one_undo_step_removes_the_whole_import(db, book, statement):
    before = _count(db)
    assert import_csv(db, _request(statement, book.checking)).ok

    assert db.undo() is True

    assert _count(db) == before


def test_reimport_never_recategorizes_an_accepted_row(db, book, statement):
    assert import_csv(db, _request(statement, book.checking)).ok
    grocer = next(txn for txn in db.iter_transactions() if txn.description == "Corner Grocer")
    counter = next(split for split in grocer.splits if split.account != book.checking)
    counter.account = book.groceries
    with db.transaction("Categorize") as txn:
        db.commit_transaction(grocer, txn)

    assert import_csv(db, _request(statement, book.checking)).ok

    stored = db.get_transaction(grocer.handle)
    assert {split.account for split in stored.splits} == {book.checking, book.groceries}


def test_possible_duplicate_waits_for_explicit_inclusion(db, book, statement):
    with db.transaction("Manual entry") as txn:
        db.add_transaction(
            Transaction.simple(
                date(2026, 9, 1), "Grocer (typed)", book.groceries, book.checking, "42.10"
            ),
            txn,
        )
    before = _count(db)

    preview = preview_csv_import(db, _request(statement, book.checking)).value
    assert preview.rows[0].status == "possible_duplicate"
    assert preview.rows[0].existing is not None

    skipped = import_csv(db, _request(statement, book.checking))
    assert skipped.value.result.transactions_new == 3
    assert _count(db) == before + 3

    included = import_csv(
        db,
        CsvImportRequest(
            source=str(statement),
            account=book.checking,
            mapping=_request(statement, book.checking).mapping,
            include_duplicates=True,
        ),
    )
    assert included.value.result.transactions_new == 1
    assert _count(db) == before + 4


def test_debit_credit_columns_day_first_comma_decimal_and_cp1252(db, book, tmp_path):
    path = tmp_path / "bank.csv"
    path.write_bytes(
        "Datum;Empfänger;Soll;Haben\n"
        "13/09/2026;Bäckerei;4,20;\n"
        "14/09/2026;Gehalt;;1.234,56\n".encode("cp1252")
    )
    request = CsvImportRequest(
        source=str(path),
        account=book.checking,
        mapping=CsvMapping(date="Datum", description="Empfänger", debit="Soll", credit="Haben"),
    )

    preview = preview_csv_import(db, request).value

    assert (preview.encoding, preview.delimiter) == ("cp1252", ";")
    assert (preview.date_format, preview.number_format) == ("day-first", "comma")
    assert [(row.when, row.amount, row.description) for row in preview.rows] == [
        (date(2026, 9, 13), Money("-4.20"), "Bäckerei"),
        (date(2026, 9, 14), Money("1234.56"), "Gehalt"),
    ]


def test_invalid_rows_are_listed_and_skipped(db, book, tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text(
        "Date,Description,Amount\n2026-09-01,Good,-1.00\n2026-13-01,Bad date,-2.00\n"
        "2026-09-02,Bad amount,abc\n",
        encoding="utf-8",
    )
    request = _request(path, book.checking, memo=None)

    preview = preview_csv_import(db, request).value
    assert [(row.line, row.status) for row in preview.rows] == [
        (2, "new"),
        (3, "invalid"),
        (4, "invalid"),
    ]
    assert "date" in preview.rows[1].reason
    assert "amount" in preview.rows[2].reason

    imported = import_csv(db, request).value.result
    assert imported.transactions_new == 1
    assert imported.skipped == 2


def test_ambiguous_dates_and_bad_mapping_are_refused(db, book, tmp_path):
    path = tmp_path / "ambiguous.csv"
    path.write_text("Date,Amount\n01/02/2026,-1.00\n03/04/2026,-2.00\n", encoding="utf-8")
    before = _count(db)

    ambiguous = preview_csv_import(db, _request(path, book.checking, description=None, memo=None))
    assert ambiguous.errors[0].code == "import.csv.date_format.ambiguous"
    chosen = preview_csv_import(
        db, _request(path, book.checking, description=None, memo=None, date_format="day-first")
    )
    assert chosen.value.rows[0].when == date(2026, 2, 1)

    missing = preview_csv_import(db, _request(path, book.checking, amount="Total", memo=None))
    assert missing.errors[0].code == "import.csv.column.not_found"
    both = preview_csv_import(
        db, _request(path, book.checking, debit="Amount", description=None, memo=None)
    )
    assert both.errors[0].code == "import.csv.amount.mapping"
    placeholder = preview_csv_import(
        db, _request(path, book.expenses, description=None, memo=None, date_format="iso")
    )
    assert placeholder.errors[0].code == "import.csv.account.invalid"
    assert _count(db) == before


def test_positional_columns_without_header(db, book, tmp_path):
    path = tmp_path / "plain.csv"
    path.write_text("2026-09-05,Rent,-900.00\n", encoding="utf-8")
    request = CsvImportRequest(
        source=str(path),
        account=book.checking,
        mapping=CsvMapping(date="1", description="2", amount="3", header=False),
    )

    [row] = preview_csv_import(db, request).value.rows

    assert (row.line, row.when, row.amount, row.description) == (
        1,
        date(2026, 9, 5),
        Money("-900.00"),
        "Rent",
    )


def test_cli_previews_then_imports(tmp_path, capsys):
    import json

    from breadsched.cli.main import main
    from breadsched.gen.sample_book import create_sample_book

    book_path = tmp_path / "cli.breadsched"
    create_sample_book(book_path, as_of=date(2026, 8, 15))
    path = tmp_path / "statement.csv"
    path.write_text(STATEMENT, encoding="utf-8")
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
    ]

    assert main([*command, "--preview"]) == 0
    text = capsys.readouterr().out
    assert "Corner Grocer" in text and "new" in text

    assert main([*command, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["transactions_new"] == 4
    assert main([*command, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["transactions_new"] == 0


def test_quoted_fields_and_inverted_signs(db, book, tmp_path):
    path = tmp_path / "quoted.csv"
    path.write_text(
        'Date,Description,Amount\n2026-09-06,"Hardware, Inc.\nStore 12",25.00\n',
        encoding="utf-8",
    )
    request = CsvImportRequest(
        source=str(path),
        account=book.checking,
        mapping=CsvMapping(date="Date", description="Description", amount="Amount", invert=True),
    )

    [row] = preview_csv_import(db, request).value.rows

    assert row.description == "Hardware, Inc.\nStore 12"
    assert row.amount == Money("-25.00")


def test_windows_line_endings_give_the_same_rows_and_identities(db, book, tmp_path):
    unix = tmp_path / "unix.csv"
    windows = tmp_path / "windows.csv"
    text = 'Date,Description,Amount\n2026-09-06,"Hardware, Inc.\nStore 12",-25.00\n'
    unix.write_bytes(text.encode("utf-8"))
    windows.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    mapping = CsvMapping(date="Date", description="Description", amount="Amount")

    [left] = preview_csv_import(db, CsvImportRequest(str(unix), book.checking, mapping)).value.rows
    [right] = preview_csv_import(
        db, CsvImportRequest(str(windows), book.checking, mapping)
    ).value.rows

    assert right.description == "Hardware, Inc.\nStore 12"
    assert right.identity == left.identity
