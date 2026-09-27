"""Transfer review during CSV import.

A transfer between two of the user's accounts appears on both statements. When the
first statement was imported, its side of the transfer was posted against an
uncategorized placeholder. A row on the second statement with the opposite amount
within a few days is offered as that transfer's other side. Linking needs explicit
acceptance; otherwise the row is imported as an ordinary new transaction and the
existing one is left untouched. A transaction the user already categorized is never
offered, so an accepted category is never rewritten.
"""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.lib import Account, AccountType, Money, Transaction
from breadsched.gen.services.csv_import import CsvImportRequest, CsvMapping, import_csv
from breadsched.gen.services.csv_import import preview_csv_import as preview
from breadsched.plugins.importer.csv_import import placeholder_handles

MAPPING = CsvMapping(date="Date", description="Description", amount="Amount", memo="Memo")


def _statement(tmp_path, name, *rows):
    path = tmp_path / name
    body = "".join(f"{when},{text},{amount},{memo}\n" for when, text, amount, memo in rows)
    path.write_text("Date,Description,Amount,Memo\n" + body, encoding="utf-8")
    return path


def _request(path, account, **options):
    return CsvImportRequest(source=str(path), account=account, mapping=MAPPING, **options)


def _count(db):
    return len(list(db.iter_transactions()))


def _legs(db, handle):
    transaction = db.get_transaction(handle)
    return [(split.handle, split.account, split.value) for split in transaction.splits]


@pytest.fixture
def savings_side(db, book, tmp_path):
    """The savings statement, already imported, with the withdrawal uncategorized."""
    path = _statement(tmp_path, "savings.csv", ("2026-09-10", "To checking", "-500.00", ""))
    imported = import_csv(db, _request(path, book.savings))
    assert imported.ok, imported.errors
    [row] = imported.value.preview.rows
    return row.identity


def test_opposite_row_is_offered_as_the_transfer_side(db, book, tmp_path, savings_side):
    path = _statement(tmp_path, "checking.csv", ("2026-09-11", "From savings", "500.00", "xfer"))
    before = _count(db)

    [row] = preview(db, _request(path, book.checking)).value.rows

    assert row.status == "possible_transfer"
    assert row.existing == savings_side
    assert "Savings" in row.reason and "2026-09-10" in row.reason
    assert _count(db) == before


def test_without_acceptance_the_row_is_new_and_the_other_side_untouched(
    db, book, tmp_path, savings_side
):
    path = _statement(tmp_path, "checking.csv", ("2026-09-11", "From savings", "500.00", ""))
    original = _legs(db, savings_side)
    before = _count(db)

    result = import_csv(db, _request(path, book.checking)).value.result

    assert (result.transactions_new, result.transactions_linked) == (1, 0)
    assert _count(db) == before + 1
    assert _legs(db, savings_side) == original


def test_accepted_link_completes_the_transfer_once(db, book, tmp_path, savings_side):
    path = _statement(tmp_path, "checking.csv", ("2026-09-11", "From savings", "500.00", "xfer"))
    before = _count(db)

    result = import_csv(db, _request(path, book.checking, link_transfers=True)).value.result

    assert (result.transactions_new, result.transactions_linked) == (0, 1)
    assert _count(db) == before
    linked = db.get_transaction(savings_side)
    assert sorted((split.account, split.value) for split in linked.splits) == sorted(
        [(book.savings, Money("-500.00")), (book.checking, Money("500.00"))]
    )
    checking_leg = next(split for split in linked.splits if split.account == book.checking)
    assert checking_leg.memo == "xfer"
    assert linked.post_date == date(2026, 9, 10)

    [again] = preview(db, _request(path, book.checking)).value.rows
    assert (again.status, again.existing) == ("imported", savings_side)
    repeat = import_csv(db, _request(path, book.checking, link_transfers=True)).value.result
    assert (repeat.transactions_linked, repeat.transactions_unchanged) == (0, 1)
    assert _count(db) == before


def test_one_undo_step_restores_the_uncategorized_side(db, book, tmp_path, savings_side):
    path = _statement(tmp_path, "checking.csv", ("2026-09-11", "From savings", "500.00", ""))
    original = _legs(db, savings_side)
    assert import_csv(db, _request(path, book.checking, link_transfers=True)).ok

    assert db.undo() is True

    assert _legs(db, savings_side) == original


def test_categorized_far_or_same_sign_transactions_are_not_offered(db, book, tmp_path):
    with db.transaction("Existing") as txn:
        db.add_transaction(
            Transaction.simple(date(2026, 9, 10), "Food", book.groceries, book.savings, "40"),
            txn,
        )
    far = _statement(tmp_path, "far.csv", ("2026-09-01", "To checking", "-75.00", ""))
    same = _statement(tmp_path, "same.csv", ("2026-09-10", "Out", "-60.00", ""))
    assert import_csv(db, _request(far, book.savings)).ok
    assert import_csv(db, _request(same, book.savings)).ok
    path = _statement(
        tmp_path,
        "checking.csv",
        ("2026-09-10", "Categorized", "40.00", ""),
        ("2026-09-06", "Too late", "75.00", ""),
        ("2026-09-10", "Same sign", "-60.00", ""),
    )

    rows = preview(db, _request(path, book.checking)).value.rows

    assert [row.status for row in rows] == ["new", "new", "new"]


def test_each_existing_side_is_offered_to_one_row(db, book, tmp_path, savings_side):
    path = _statement(
        tmp_path,
        "checking.csv",
        ("2026-09-12", "From savings", "500.00", ""),
        ("2026-09-10", "Also 500", "500.00", ""),
    )

    rows = preview(db, _request(path, book.checking)).value.rows

    # The nearer date wins; the other row is ordinary new activity.
    assert [(row.description, row.status) for row in rows] == [
        ("From savings", "new"),
        ("Also 500", "possible_transfer"),
    ]
    result = import_csv(db, _request(path, book.checking, link_transfers=True)).value.result
    assert (result.transactions_new, result.transactions_linked) == (1, 1)


def test_ofx_placeholder_side_is_offered(db, book, tmp_path):
    ofx_expense = next(
        handle for kind, handle in placeholder_handles() if kind == ("ofx", "EXPENSE")
    )
    with db.transaction("OFX side") as txn:
        db.add_account(
            Account(
                name="Uncategorized OFX",
                atype=AccountType.EXPENSE,
                parent=book.expenses,
                handle=ofx_expense,
            ),
            txn,
        )
        db.add_transaction(
            Transaction.simple(date(2026, 9, 10), "Card payment", ofx_expense, book.checking, "80"),
            txn,
        )
    path = _statement(tmp_path, "card.csv", ("2026-09-11", "Payment thanks", "80.00", ""))

    [row] = preview(db, _request(path, book.card)).value.rows

    assert row.status == "possible_transfer"


def test_cli_links_transfers_only_when_asked(tmp_path, capsys):
    import json

    from breadsched.cli.main import main
    from breadsched.gen.sample_book import create_sample_book

    book_path = tmp_path / "cli.breadsched"
    create_sample_book(book_path, as_of=date(2026, 8, 15))
    savings = _statement(tmp_path, "savings.csv", ("2026-09-10", "To checking", "-500.00", ""))
    checking = _statement(tmp_path, "checking.csv", ("2026-09-11", "From savings", "500.00", ""))
    base = ["--date", "Date", "--amount", "Amount", "--description", "Description", "--json"]
    assert main(["import-csv", str(book_path), str(savings), "--account", "Savings", *base]) == 0
    capsys.readouterr()

    command = ["import-csv", str(book_path), str(checking), "--account", "Checking", *base]
    assert main([*command, "--preview"]) == 0
    [row] = json.loads(capsys.readouterr().out)["rows"]
    assert row["status"] == "possible_transfer"

    assert main([*command, "--link-transfers"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["transactions_new"], data["transactions_linked"]) == (0, 1)
