"""An OFX row repeating a transaction from elsewhere in the account is held back.

A statement re-imported is recognized by FITID and changes nothing. A new row that
matches a transaction entered by hand, imported from a CSV, or imported from an
earlier statement under another FITID, on the same date for the same amount, is a
possible duplicate: it is held back, one row per existing transaction, unless the
import includes possible duplicates.
"""

from __future__ import annotations

from datetime import date

from breadsched.gen.lib import Transaction
from breadsched.gen.services import ImportBook, import_book
from breadsched.plugins.importer import ofx

_HEAD = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX>
<SIGNONMSGSRSV1><SONRS><FI><ORG>Sample Bank</FI></SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>USD
<BANKACCTFROM><BANKID>123456789<ACCTID>00001234<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST>
"""
_TAIL = """</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


def _row(fitid, when, amount, name):
    return (
        f"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>{when}120000<TRNAMT>{amount}"
        f"<FITID>{fitid}<NAME>{name}</STMTTRN>\n"
    )


def _statement(tmp_path, name, *rows):
    path = tmp_path / name
    path.write_text(_HEAD + "".join(rows) + _TAIL)
    return path


def _account(db, tmp_path):
    ofx.import_book(
        db, _statement(tmp_path, "opening.ofx", _row("o1", "20260101", "100.00", "Open"))
    )
    return db.get_account_by_name("Sample Bank Checking 1234").handle


def _by_hand(db, account, book, when, amount, description):
    transaction = Transaction.simple(when, description, book.groceries, account, amount)
    with db.transaction(description) as txn:
        db.add_transaction(transaction, txn)


def _count(db):
    return len(list(db.iter_transactions()))


def test_a_row_matching_a_hand_entered_transaction_is_held_back(db, book, tmp_path):
    account = _account(db, tmp_path)
    _by_hand(db, account, book, date(2026, 1, 15), "45.67", "Groceries (typed)")
    before = _count(db)
    path = _statement(tmp_path, "jan.ofx", _row("g1", "20260115", "-45.67", "Grocery Store"))

    held = ofx.import_book(db, path)

    assert (held.possible_duplicates, held.transactions_new) == (1, 0)
    assert held.reasons() == {
        "possible duplicate of a transaction already in this account on the same date "
        "for the same amount": 1
    }
    assert "Possible duplicates: 1 held back" in held.detail()
    assert _count(db) == before

    included = ofx.import_book(db, path, include_duplicates=True)
    assert (included.possible_duplicates, included.transactions_new) == (0, 1)
    assert _count(db) == before + 1
    # Once imported, the row is the statement's own and is recognized again.
    again = ofx.import_book(db, path)
    assert (again.possible_duplicates, again.transactions_new) == (0, 0)


def test_each_existing_transaction_answers_for_one_row(db, book, tmp_path):
    account = _account(db, tmp_path)
    _by_hand(db, account, book, date(2026, 1, 15), "3.50", "Coffee (typed)")
    path = _statement(
        tmp_path,
        "coffee.ofx",
        _row("c1", "20260115", "-3.50", "Coffee"),
        _row("c2", "20260115", "-3.50", "Coffee"),
    )
    result = ofx.import_book(db, path)
    assert (result.possible_duplicates, result.transactions_new) == (1, 1)


def test_a_statements_own_identical_rows_are_never_its_duplicates(db, book, tmp_path):
    _account(db, tmp_path)
    rows = (_row("c1", "20260115", "-3.50", "Coffee"), _row("c2", "20260115", "-3.50", "Coffee"))
    first = ofx.import_book(db, _statement(tmp_path, "two.ofx", *rows))
    assert (first.possible_duplicates, first.transactions_new) == (0, 2)
    again = ofx.import_book(db, _statement(tmp_path, "two.ofx", *rows))
    assert (again.possible_duplicates, again.transactions_new) == (0, 0)


def test_the_same_activity_under_a_new_fitid_is_held_back(db, book, tmp_path):
    _account(db, tmp_path)
    ofx.import_book(db, _statement(tmp_path, "a.ofx", _row("x-1", "20260120", "-60.00", "Fuel")))
    before = _count(db)
    renumbered = ofx.import_book(
        db, _statement(tmp_path, "b.ofx", _row("y-1", "20260120", "-60.00", "Fuel"))
    )
    assert renumbered.possible_duplicates == 1 and _count(db) == before


def test_the_import_service_and_cli_pass_the_choice(db, book, tmp_path, capsys):
    account = _account(db, tmp_path)
    _by_hand(db, account, book, date(2026, 1, 15), "45.67", "Typed")
    path = _statement(tmp_path, "jan.ofx", _row("g1", "20260115", "-45.67", "Grocery Store"))
    held = import_book(db, ImportBook(source=str(path)))
    assert held.value is not None and held.value.result.possible_duplicates == 1
    included = import_book(db, ImportBook(source=str(path), include_duplicates=True))
    assert included.value is not None and included.value.result.transactions_new == 1

    import json

    from breadsched.cli.main import main

    book_path = tmp_path / "cli.breadsched"
    main(["init", str(book_path)])
    opening = _statement(tmp_path, "open-cli.ofx", _row("o1", "20260101", "100.00", "Open"))
    assert main(["import", str(book_path), str(opening), "--no-infer"]) == 0
    checking = "Sample Bank Checking 1234"
    typed = ["--date", "2026-01-15", "--description", "Typed", "--amount", "45.67"]
    main(["add", str(book_path), *typed, "--from", checking, "--to", "Expenses"])
    capsys.readouterr()
    assert main(["import", str(book_path), str(path), "--no-infer", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["possible_duplicates"] == 1
    command = ["import", str(book_path), str(path), "--no-infer", "--include-duplicates", "--json"]
    assert main(command) == 0
    assert json.loads(capsys.readouterr().out)["transactions_new"] == 1
