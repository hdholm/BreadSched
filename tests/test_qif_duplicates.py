"""A QIF row repeating a transaction from elsewhere in the account is held back.

A QIF export re-imported is recognized by its rows' content and changes nothing. A
new row that matches a transaction entered by hand or imported from another format,
on the same date for the same amount, is a possible duplicate: it is held back, one
row per existing transaction, unless the import includes possible duplicates. The
file's own transactions never count, whatever order its rows come in.
"""

from __future__ import annotations

from datetime import date

from breadsched.gen.lib import Transaction
from breadsched.gen.services import ImportBook, import_book
from breadsched.plugins.importer import qif

_REASON = (
    "possible duplicate of a transaction already in this account on the same date "
    "for the same amount"
)


def _row(when, amount, payee, category="Groceries"):
    return f"D{when}\nT{amount}\nP{payee}\nL{category}\n^\n"


def _export(tmp_path, name, *rows):
    path = tmp_path / name
    path.write_text("!Account\nNChecking\nTBank\n^\n!Type:Bank\n" + "".join(rows))
    return path


def _by_hand(db, book, when, amount, description):
    transaction = Transaction.simple(when, description, book.groceries, book.checking, amount)
    with db.transaction(description) as txn:
        db.add_transaction(transaction, txn)


def _count(db):
    return len(list(db.iter_transactions()))


def test_a_row_matching_a_hand_entered_transaction_is_held_back(db, book, tmp_path):
    _by_hand(db, book, date(2026, 1, 15), "45.67", "Groceries (typed)")
    before = _count(db)
    path = _export(tmp_path, "jan.qif", _row("01/15/2026", "-45.67", "Grocery Store"))

    held = qif.import_book(db, path)

    assert (held.possible_duplicates, held.transactions_new) == (1, 0)
    assert held.reasons() == {_REASON: 1}
    assert "Possible duplicates: 1 held back" in held.detail()
    assert _count(db) == before

    included = qif.import_book(db, path, include_duplicates=True)
    assert (included.possible_duplicates, included.transactions_new) == (0, 1)
    assert _count(db) == before + 1
    again = qif.import_book(db, path)
    assert (again.possible_duplicates, again.transactions_new) == (0, 0)


def test_each_existing_transaction_answers_for_one_row(db, book, tmp_path):
    _by_hand(db, book, date(2026, 1, 15), "3.50", "Coffee (typed)")
    path = _export(
        tmp_path,
        "coffee.qif",
        _row("01/15/2026", "-3.50", "Coffee"),
        _row("01/15/2026", "-3.50", "Coffee"),
    )
    result = qif.import_book(db, path)
    assert (result.possible_duplicates, result.transactions_new) == (1, 1)


def test_an_exports_own_rows_are_never_its_duplicates_in_any_order(db, book, tmp_path):
    # The first export has only the bakery; a later one adds a same-day, same-amount
    # row before it. The bakery is the file's own, so the cafe is not its duplicate.
    qif.import_book(db, _export(tmp_path, "a.qif", _row("01/15/2026", "-3.50", "Bakery")))
    later = _export(
        tmp_path,
        "a.qif",
        _row("01/15/2026", "-3.50", "Cafe"),
        _row("01/15/2026", "-3.50", "Bakery"),
    )
    result = qif.import_book(db, later)
    assert (result.possible_duplicates, result.transactions_new) == (0, 1)
    assert result.transactions_unchanged == 1


def test_a_split_row_is_compared_by_its_account_amount(db, book, tmp_path):
    _by_hand(db, book, date(2026, 1, 20), "80.00", "Store (typed)")
    split = (
        "D01/20/2026\nT-80.00\nPStore\nLGroceries\nSGroceries\n$-50.00\nSHousehold\n$-30.00\n^\n"
    )
    result = qif.import_book(db, _export(tmp_path, "split.qif", split))
    assert (result.possible_duplicates, result.transactions_new) == (1, 0)


def test_the_import_service_and_cli_pass_the_choice(db, book, tmp_path, capsys):
    _by_hand(db, book, date(2026, 1, 15), "45.67", "Typed")
    path = _export(tmp_path, "jan.qif", _row("01/15/2026", "-45.67", "Grocery Store"))
    held = import_book(db, ImportBook(source=str(path)))
    assert held.value is not None and held.value.result.possible_duplicates == 1
    included = import_book(db, ImportBook(source=str(path), include_duplicates=True))
    assert included.value is not None and included.value.result.transactions_new == 1

    import json

    from breadsched.cli.main import main

    book_path = tmp_path / "cli.breadsched"
    main(["init", str(book_path)])
    opening = _export(tmp_path, "open.qif", _row("01/01/2026", "100.00", "Open", "Income"))
    assert main(["import", str(book_path), str(opening), "--no-infer"]) == 0
    typed = ["--date", "2026-01-15", "--description", "Typed", "--amount", "45.67"]
    main(["add", str(book_path), *typed, "--from", "Checking", "--to", "Expenses"])
    capsys.readouterr()
    assert main(["import", str(book_path), str(path), "--no-infer", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["possible_duplicates"] == 1
    command = ["import", str(book_path), str(path), "--no-infer", "--include-duplicates", "--json"]
    assert main(command) == 0
    assert json.loads(capsys.readouterr().out)["transactions_new"] == 1
