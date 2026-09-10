"""Scheduled transactions, template sections, and merging into an existing book.

Three defects are pinned here, all of which reached a user:

* GnuCash's ``<gnc:template-transactions>`` section uses the same element names as
  the real ledger, so an importer matching on element name alone posts template
  transactions into the register against accounts named after a schedule's GUID.
* The XML importer read no schedules at all, so the scheduled view stayed empty.
* Importing into a book that already had a chart of accounts produced a *second*
  root, and the account tree walks from one root — showing an empty book while
  every balance sat under the other.
"""

from __future__ import annotations

from datetime import date

import pytest
from gnucash_fixtures import create_book
from gnucash_xml_fixtures import create_xml_book

from breadsched.cli.main import main as cli
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine import ledger, schedule
from breadsched.gen.lib import Money
from breadsched.plugins.importer import gnucash_xml


@pytest.fixture
def xml_book(tmp_path):
    return create_xml_book(tmp_path / "household.gnucash")


class TestTemplatesStayOutOfTheLedger:
    def test_no_template_accounts_are_imported(self, db, xml_book):
        gnucash_xml.import_book(db, xml_book.path)
        names = {a.name for a in db.iter_accounts()}
        assert "Template Root" not in names
        assert xml_book.schedule not in names, (
            "a template account named after the schedule GUID reached the ledger"
        )

    def test_only_real_accounts_arrive(self, db, xml_book):
        gnucash_xml.import_book(db, xml_book.path)
        assert {a.name for a in db.iter_accounts()} == {
            "Root Account", "Assets", "Checking", "Expenses", "Rent"
        }

    def test_no_template_transactions_reach_the_register(self, db, xml_book):
        gnucash_xml.import_book(db, xml_book.path)
        descriptions = [t.description for t in db.iter_transactions()]
        assert descriptions == ["January rent"]

    def test_the_register_shows_no_guid_shaped_accounts(self, db, xml_book):
        """The reported symptom: rows posted to a long hexadecimal 'account'."""
        gnucash_xml.import_book(db, xml_book.path)
        for row in ledger.register(db, db.get_account_by_name("Checking").handle):
            label = row.transfer_label(db)
            assert label and len(label) != 32, f"GUID-shaped transfer account: {label}"

    def test_balances_are_unaffected_by_the_template_section(self, db, xml_book):
        gnucash_xml.import_book(db, xml_book.path)
        assert ledger.balance(db, db.get_account_by_name("Checking").handle) == Money(
            "-1800.00"
        )


class TestXmlScheduledTransactions:
    def test_the_schedule_is_imported(self, db, xml_book):
        result = gnucash_xml.import_book(db, xml_book.path)
        assert result.scheduled == 1
        assert [s.name for s in db.iter_scheduled()] == ["Monthly rent"]

    def test_its_recurrence_is_read(self, db, xml_book):
        gnucash_xml.import_book(db, xml_book.path)
        sched = next(iter(db.iter_scheduled()))
        assert sched.recurrence.occurrences(date(2026, 3, 31)) == [
            date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)
        ]

    def test_its_flags_are_read(self, db, xml_book):
        gnucash_xml.import_book(db, xml_book.path)
        sched = next(iter(db.iter_scheduled()))
        assert sched.enabled is True
        assert sched.auto_create is True
        assert sched.advance_days == 5

    def test_template_splits_resolve_to_real_accounts(self, db, xml_book):
        """The slot indirection: the template split names the real account."""
        gnucash_xml.import_book(db, xml_book.path)
        sched = next(iter(db.iter_scheduled()))
        assert {db.full_name(s.account) for s in sched.splits} == {
            "Expenses:Rent", "Assets:Checking"
        }

    def test_the_credit_formula_carries_the_opposite_sign(self, db, xml_book):
        gnucash_xml.import_book(db, xml_book.path)
        sched = next(iter(db.iter_scheduled()))
        rent = db.get_account_by_name("Expenses:Rent").handle
        bank = db.get_account_by_name("Assets:Checking").handle
        amounts = {s.account: s.resolve() for s in sched.splits}
        assert amounts[rent] == Money("1800.00")
        assert amounts[bank] == Money("-1800.00")

    def test_the_schedule_produces_a_balanced_transaction(self, db, xml_book):
        gnucash_xml.import_book(db, xml_book.path)
        sched = next(iter(db.iter_scheduled()))
        txn = sched.instantiate(date(2026, 4, 1))
        assert txn.is_balanced()

    def test_it_appears_in_the_due_list(self, db, xml_book):
        gnucash_xml.import_book(db, xml_book.path)
        due = schedule.due_occurrences(db, as_of=date(2026, 3, 15), horizon_days=0)
        assert [o.when for o in due] == [
            date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)
        ]

    def test_scheduled_import_can_still_be_skipped(self, db, xml_book):
        gnucash_xml.import_book(db, xml_book.path, include_scheduled=False)
        assert list(db.iter_scheduled()) == []

    def test_an_uncompressed_book_behaves_identically(self, db, tmp_path):
        plain = create_xml_book(tmp_path / "plain.gnucash", compress=False)
        result = gnucash_xml.import_book(db, plain.path)
        assert result.scheduled == 1
        assert result.transactions == 1


class TestMergingIntoAnExistingBook:
    """`breadsched init` makes a chart of accounts; an import must join it, not rival it."""

    @pytest.fixture
    def initialised(self, tmp_path, capsys):
        path = tmp_path / "book.breadsched"
        cli(["init", str(path)])
        capsys.readouterr()
        return path

    @pytest.fixture
    def source(self, tmp_path):
        return create_book(
            tmp_path / "source.gnucash",
            [
                ("root", "Root Account", "ROOT", None, 0),
                ("assets", "Assets", "ASSET", "root", 1),
                ("bank", "Checking", "BANK", "assets", 0),
                ("income", "Income", "INCOME", "root", 1),
                ("wages", "Salary", "INCOME", "income", 0),
            ],
            [
                (date(2026, 1, 25), "Payroll",
                 [("bank", 420000, 100, ""), ("wages", -420000, 100, "")]),
                # Posted straight to a top-level account that will be merged.
                (date(2026, 1, 26), "Direct to Assets",
                 [("assets", 5000, 100, ""), ("wages", -5000, 100, "")]),
            ],
        )

    def test_the_book_keeps_exactly_one_root(self, initialised, source, capsys):
        cli(["import", str(initialised), source.path])
        capsys.readouterr()
        db = DbSQLite()
        db.load(str(initialised))
        roots = [a for a in db.iter_accounts() if a.parent is None]
        assert len(roots) == 1
        db.close()

    def test_top_level_accounts_are_not_duplicated(self, initialised, source, capsys):
        cli(["import", str(initialised), source.path])
        capsys.readouterr()
        db = DbSQLite()
        db.load(str(initialised))
        names = [a.name for a in db.iter_accounts()]
        assert names.count("Assets") == 1
        assert names.count("Income") == 1
        db.close()

    def test_imported_accounts_hang_off_the_existing_tree(
        self, initialised, source, capsys
    ):
        cli(["import", str(initialised), source.path])
        capsys.readouterr()
        db = DbSQLite()
        db.load(str(initialised))
        assert db.get_account_by_name("Assets:Checking") is not None
        assert db.get_account_by_name("Income:Salary") is not None
        db.close()

    def test_the_tree_shows_balances_from_the_single_root(
        self, initialised, source, capsys
    ):
        """The reported symptom: top-level accounts all reading zero."""
        cli(["import", str(initialised), source.path])
        capsys.readouterr()
        db = DbSQLite()
        db.load(str(initialised))
        root = db.root_account()
        assets = next(a for a in db.child_accounts(root.handle) if a.name == "Assets")
        assert ledger.balance_recursive(db, assets.handle) == Money("4250.00")
        db.close()

    def test_a_split_posted_to_a_merged_account_is_not_lost(
        self, initialised, source, capsys
    ):
        """A merged account keeps the source GUID in its splits; it must be remapped."""
        cli(["import", str(initialised), source.path])
        capsys.readouterr()
        db = DbSQLite()
        db.load(str(initialised))
        assets = db.get_account_by_name("Assets")
        assert ledger.balance(db, assets.handle) == Money("50.00")
        db.close()

    def test_nothing_is_skipped_by_the_merge(self, initialised, source, capsys):
        import json

        cli(["import", str(initialised), source.path, "--json"])
        report = json.loads(capsys.readouterr().out)
        assert report["skipped"] == 0
        assert report["transactions"] == 2
