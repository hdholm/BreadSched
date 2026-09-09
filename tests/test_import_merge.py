"""Importing into a book that already has a chart of accounts.

Every one of these is a regression test for something a user hit: an account tree
that showed five empty placeholders while the money sat in an invisible parallel
tree, and a register full of thirty-two-character hexadecimal account names.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from gnucash_fixtures import create_book, new_guid, write_account, write_transaction

from cashperspective.cli.main import main as cli
from cashperspective.gen.db.sqlite import DbSQLite
from cashperspective.gen.engine import ledger
from cashperspective.gen.lib import Money
from cashperspective.plugins.importer import gnucash_sqlite

CHART = [
    ("root", "Root Account", "ROOT", None, 0),
    ("assets", "Assets", "ASSET", "root", 1),
    ("bank", "Checking", "BANK", "assets", 0),
    ("income", "Income", "INCOME", "root", 1),
    ("wages", "Salary", "INCOME", "income", 0),
    ("expenses", "Expenses", "EXPENSE", "root", 1),
    ("rent", "Rent", "EXPENSE", "expenses", 0),
]

MOVEMENTS = [
    (date(2026, 1, 1), "Payroll",
     [("bank", 500000, 100, ""), ("wages", -500000, 100, "")]),
    (date(2026, 1, 3), "Rent",
     [("rent", 180000, 100, ""), ("bank", -180000, 100, "")]),
]


@pytest.fixture
def initialised_book(tmp_path, capsys):
    """A book created by `cashperspective init`, so it already has a root and placeholders."""
    path = tmp_path / "household.cashperspective"
    cli(["init", str(path)])
    capsys.readouterr()
    db = DbSQLite()
    db.load(str(path))
    yield db
    db.close()


class TestSingleRoot:
    def test_importing_does_not_create_a_second_root(self, initialised_book, tmp_path):
        """Two roots make the account tree show whichever one it happens to find."""
        source = create_book(tmp_path / "src.gnucash", CHART, MOVEMENTS)
        gnucash_sqlite.import_book(initialised_book, source.path)

        roots = [a for a in initialised_book.iter_accounts() if a.parent is None]
        assert len(roots) == 1

    def test_imported_accounts_hang_off_the_book_root(self, initialised_book, tmp_path):
        source = create_book(tmp_path / "src.gnucash", CHART, MOVEMENTS)
        gnucash_sqlite.import_book(initialised_book, source.path)

        root = initialised_book.root_account()
        names = {a.name for a in initialised_book.descendants(root.handle)}
        assert {"Checking", "Rent", "Salary"} <= names

    def test_balances_are_visible_from_the_root(self, initialised_book, tmp_path):
        """The reported symptom: every top-level account showing zero."""
        source = create_book(tmp_path / "src.gnucash", CHART, MOVEMENTS)
        gnucash_sqlite.import_book(initialised_book, source.path)

        assets = initialised_book.get_account_by_name("Assets")
        expenses = initialised_book.get_account_by_name("Expenses")
        assert ledger.balance_recursive(initialised_book, assets.handle) == Money(
            "3200.00"
        )
        assert ledger.balance_recursive(initialised_book, expenses.handle) == Money(
            "1800.00"
        )

    def test_placeholders_are_merged_not_duplicated(self, initialised_book, tmp_path):
        """`init` makes an Assets placeholder; so does the source book."""
        source = create_book(tmp_path / "src.gnucash", CHART, MOVEMENTS)
        gnucash_sqlite.import_book(initialised_book, source.path)

        root = initialised_book.root_account()
        top = [a.name for a in initialised_book.child_accounts(root.handle)]
        assert top.count("Assets") == 1
        assert top.count("Expenses") == 1

    def test_splits_point_at_the_merged_accounts(self, initialised_book, tmp_path):
        source = create_book(tmp_path / "src.gnucash", CHART, MOVEMENTS)
        gnucash_sqlite.import_book(initialised_book, source.path)

        for txn in initialised_book.iter_transactions():
            for split in txn.splits:
                assert initialised_book.get_account(split.account) is not None

    def test_importing_into_an_empty_book_keeps_the_source_root(self, db, tmp_path):
        source = create_book(tmp_path / "src.gnucash", CHART, MOVEMENTS)
        gnucash_sqlite.import_book(db, source.path)
        roots = [a for a in db.iter_accounts() if a.parent is None]
        assert len(roots) == 1
        assert roots[0].name == "Root Account"

    def test_two_imports_still_leave_one_root(self, initialised_book, tmp_path):
        source = create_book(tmp_path / "src.gnucash", CHART, MOVEMENTS)
        gnucash_sqlite.import_book(initialised_book, source.path)
        gnucash_sqlite.import_book(initialised_book, source.path)
        roots = [a for a in initialised_book.iter_accounts() if a.parent is None]
        assert len(roots) == 1


class TestTemplateAccounts:
    """GnuCash names template accounts after the schedule's GUID."""

    @pytest.fixture
    def book_with_templates(self, tmp_path):
        source = create_book(tmp_path / "src.gnucash", CHART, MOVEMENTS)
        conn = sqlite3.connect(source.path)
        # Exactly what GnuCash writes: GUID-named accounts under the template root,
        # each holding a template transaction that has not happened.
        template_guids = []
        for _ in range(3):
            guid = new_guid()
            write_account(
                conn, guid, guid, "BANK", source.template_root, source.currency
            )
            template_guids.append(guid)
            write_transaction(
                conn, new_guid(), source.currency, date(2026, 1, 1), "Rent template",
                [(guid, 180000, 100, ""), (guid, -180000, 100, "")],
            )
        conn.commit()
        conn.close()
        source.templates = template_guids
        return source

    def test_template_accounts_stay_out_of_the_chart(
        self, initialised_book, book_with_templates
    ):
        gnucash_sqlite.import_book(initialised_book, book_with_templates.path)
        names = {a.name for a in initialised_book.iter_accounts()}
        assert not any(len(n) == 32 and all(c in "0123456789abcdef" for c in n)
                       for n in names), "GUID-named template accounts were imported"

    def test_template_root_is_not_imported(self, initialised_book, book_with_templates):
        gnucash_sqlite.import_book(initialised_book, book_with_templates.path)
        assert initialised_book.get_account(book_with_templates.template_root) is None
        assert initialised_book.get_account_by_name("Template Root") is None

    def test_template_transactions_stay_out_of_the_register(
        self, initialised_book, book_with_templates
    ):
        result = gnucash_sqlite.import_book(initialised_book, book_with_templates.path)
        assert result.transactions == 2  # the two real ones only

    def test_the_books_table_is_the_source_of_truth(self, book_with_templates):
        """Not schedxactions: a template account no schedule references is still one."""
        conn = sqlite3.connect(book_with_templates.path)
        conn.row_factory = sqlite3.Row
        account_root, template_root = gnucash_sqlite.book_roots(conn)
        conn.close()
        assert template_root == book_with_templates.template_root
        assert account_root == book_with_templates.root

    def test_a_book_without_a_books_table_still_imports(self, db, tmp_path):
        """Older books, and books written by other tools, have no books table."""
        source = create_book(tmp_path / "old.gnucash", CHART, MOVEMENTS)
        conn = sqlite3.connect(source.path)
        conn.execute("DROP TABLE books")
        conn.commit()
        conn.close()
        result = gnucash_sqlite.import_book(db, source.path)
        assert result.transactions == 2
