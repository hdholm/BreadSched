"""Quicken Interchange Format importer tests."""

from __future__ import annotations

from breadsched.gen.engine import ledger
from breadsched.gen.lib import Money, PlanningResolution
from breadsched.gen.plug import IMPORTER, PluginManager
from breadsched.plugins.importer import qif


def test_qif_plugin_is_detected_by_content(tmp_path):
    path = tmp_path / "download.dat"
    path.write_text("!Type:Bank\nD01/01/2026\nT-1.00\nPTest\n^\n")

    plugin = PluginManager.instance().for_file(str(path), IMPORTER)

    assert plugin is not None
    assert plugin.id == "qif"


def test_qif_imports_bank_income_and_expense_history(db, book, tmp_path):
    path = tmp_path / "checking.qif"
    path.write_text(
        "!Account\n"
        "NChecking\n"
        "TBank\n"
        "^\n"
        "!Type:Bank\n"
        "D01/15/2026\n"
        "T-45.67\n"
        "PGrocery Store\n"
        "LGroceries\n"
        "^\n"
        "D01/25/2026\n"
        "T2000.00\n"
        "PEmployer\n"
        "LSalary\n"
        "^\n"
    )

    result = qif.import_book(db, path)

    assert result.transactions == 2
    assert result.skipped == 0
    transactions = sorted(db.iter_transactions(), key=lambda item: item.post_date)
    assert [item.description for item in transactions] == ["Grocery Store", "Employer"]
    assert all(
        item.planning_resolution is PlanningResolution.HISTORICAL
        for item in transactions
    )
    groceries = db.get_account(book.groceries)
    salary = db.get_account(book.salary)
    checking = db.get_account(book.checking)
    assert groceries is not None and salary is not None and checking is not None
    assert ledger.balance(db, groceries.handle, natural_sign=False) == Money("45.67")
    assert ledger.balance(db, salary.handle, natural_sign=False) == Money("-2000.00")
    assert ledger.balance(db, checking.handle, natural_sign=False) == Money("1954.33")


def test_qif_split_transaction_stays_balanced(db, book, tmp_path):
    path = tmp_path / "split.qif"
    path.write_text(
        "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
        "D02/01/2026\nT-100.00\nPBig box\n"
        "SGroceries\n$-60.00\n"
        "SUtilities\n$-40.00\n^\n"
    )

    result = qif.import_book(db, path)

    assert result.transactions == 1
    transaction = next(db.iter_transactions())
    assert transaction.imbalance() == Money(0)
    assert len(transaction.splits) == 3


def test_qif_reimport_survives_unrelated_record_insertion(db, book, tmp_path):
    path = tmp_path / "checking.qif"
    original = (
        "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
        "D01/15/2026\nT-45.67\nPGrocery Store\nLGroceries\n^\n"
        "D01/25/2026\nT2000.00\nPEmployer\nLSalary\n^\n"
    )
    path.write_text(original)
    qif.import_book(db, path)

    inserted = (
        "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
        "D01/10/2026\nT-10.00\nPCoffee\nLGroceries\n^\n"
        + original.split("!Type:Bank\n", 1)[1]
    )
    path.write_text(inserted)
    qif.import_book(db, path)

    assert len(list(db.iter_transactions())) == 3


def test_qif_detects_comma_decimal_amounts(db, book, tmp_path):
    path = tmp_path / "comma-decimal.qif"
    path.write_text(
        "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
        "D03/01/2026\nT-45,67\nPGroceries\nLGroceries\n^\n"
        "D03/02/2026\nT2.000,00\nPEmployer\nLSalary\n^\n"
    )

    result = qif.import_book(db, path)

    assert result.transactions == 2
    checking = db.get_account(book.checking)
    assert checking is not None
    assert ledger.balance(db, checking.handle, natural_sign=False) == Money("1954.33")


def test_qif_explicit_number_format_resolves_ambiguous_amount(db, book, tmp_path):
    path = tmp_path / "ambiguous.qif"
    path.write_text(
        "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
        "D03/01/2026\nT1,234\nPTransfer\nLSalary\n^\n"
    )

    result = qif.import_book(db, path, number_format="comma")

    assert result.transactions == 1
    checking = db.get_account(book.checking)
    assert checking is not None
    assert ledger.balance(db, checking.handle, natural_sign=False) == Money("1.234")
