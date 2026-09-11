"""GnuCash compatibility.

These tests are the contract with an external application whose format we do not
control, so they assert against books built to GnuCash's own schema rather than
against anything this codebase produces.
"""

import sqlite3
from datetime import date

import pytest
from gnucash_fixtures import create_book, new_guid

from breadsched.gen.engine import ledger
from breadsched.gen.lib import AccountType, Money, PlanningResolution
from breadsched.gen.plug import IMPORTER, PluginManager
from breadsched.plugins.importer import gnucash_common, gnucash_sqlite, gnucash_xml


class TestFormatDetection:
    def test_recognises_a_sqlite_book(self, gnucash_sqlite_path):
        assert gnucash_common.detect_format(gnucash_sqlite_path.path) == "sqlite"

    def test_recognises_a_compressed_xml_book(self, gnucash_xml_path):
        assert gnucash_common.detect_format(gnucash_xml_path.path) == "xml-gz"

    def test_recognises_a_plain_xml_book(self, tmp_path, gnucash_xml_path):
        plain = tmp_path / "plain.gnucash"
        plain.write_text(gnucash_xml_path.plain, encoding="utf-8")
        assert gnucash_common.detect_format(plain) == "xml"

    def test_an_unrelated_file_is_unknown(self, tmp_path):
        other = tmp_path / "notes.txt"
        other.write_text("just some notes")
        assert gnucash_common.detect_format(other) == "unknown"

    def test_a_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            gnucash_common.detect_format(tmp_path / "nope.gnucash")

    def test_the_registry_picks_the_plugin_by_content_not_extension(
        self, gnucash_sqlite_path, gnucash_xml_path
    ):
        """Both books are named .gnucash; only the bytes tell them apart."""
        manager = PluginManager.instance()
        assert manager.for_file(gnucash_sqlite_path.path, IMPORTER).id == "gnucash-sqlite"
        assert manager.for_file(gnucash_xml_path.path, IMPORTER).id == "gnucash-xml"


class TestDateParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("20260125104000", date(2026, 1, 25)),      # modern SQLite books
            ("2026-01-25 10:40:00", date(2026, 1, 25)),  # older SQLite books
            ("2026-01-25 10:59:00 +0000", date(2026, 1, 25)),  # XML books
            ("20260125", date(2026, 1, 25)),             # recurrence start dates
        ],
    )
    def test_every_shape_gnucash_emits(self, raw, expected):
        assert gnucash_common.parse_gnc_date(raw) == expected

    def test_nonsense_is_rejected_rather_than_guessed(self):
        with pytest.raises(ValueError):
            gnucash_common.parse_gnc_date("last Tuesday")


class TestSqliteImport:
    def test_imports_the_whole_chart_of_accounts(self, db, gnucash_sqlite_path):
        result = gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        assert result.accounts >= 9
        assert db.get_account_by_name("Expenses:Rent") is not None

    def test_preserves_guids_as_handles(self, db, gnucash_sqlite_path):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        account = db.get_account(gnucash_sqlite_path.ids.checking)
        assert account is not None
        assert account.name == "Checking Account"

    def test_preserves_account_type_and_metadata(self, db, gnucash_sqlite_path):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        account = db.get_account(gnucash_sqlite_path.ids.checking)
        assert account.atype is AccountType.BANK
        assert account.code == "1010"
        assert account.description == "Everyday account"
        assert account.notes == "Generic account note"

    def test_placeholders_survive(self, db, gnucash_sqlite_path):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        assert db.get_account(gnucash_sqlite_path.ids.assets).placeholder is True

    def test_amounts_are_exact(self, db, gnucash_sqlite_path):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        assert ledger.balance(db, gnucash_sqlite_path.ids.checking) == Money("2350.00")

    def test_imported_history_is_not_sent_to_plan_resolution(self, db, gnucash_sqlite_path):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        transactions = list(db.iter_transactions())
        assert transactions
        assert all(
            transaction.planning_resolution is PlanningResolution.HISTORICAL
            for transaction in transactions
        )

    def test_multi_split_transactions_keep_every_leg(self, db, gnucash_sqlite_path):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        shop = next(
            t for t in db.iter_transactions() if t.description == "Supermarket"
        )
        assert len(shop.splits) == 3
        assert shop.is_balanced()

    def test_liability_balances_read_naturally(self, db, gnucash_sqlite_path):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        assert ledger.balance(db, gnucash_sqlite_path.ids.card) == Money("75.50")

    def test_commodities_come_across(self, db, gnucash_sqlite_path):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        usd = db.get_commodity_by_mnemonic("USD")
        assert usd is not None and usd.fraction == 100

    def test_the_source_book_is_never_written_to(self, db, gnucash_sqlite_path):
        import os

        before = os.stat(gnucash_sqlite_path.path).st_mtime_ns
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        assert os.stat(gnucash_sqlite_path.path).st_mtime_ns == before

    def test_reimporting_updates_rather_than_duplicates(self, db, gnucash_sqlite_path):
        first = gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        counts_before = db.summary()
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        assert db.summary() == counts_before
        assert first.transactions == 3

    def test_import_is_a_single_undoable_step(self, db, gnucash_sqlite_path):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        assert db.summary()["txn"] == 3
        assert db.undo() is True
        assert db.summary()["txn"] == 0
        assert db.summary()["account"] == 0


class TestSqliteScheduledImport:
    def test_reads_the_schedule_and_its_rule(self, db, gnucash_sqlite_path):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        sched = db.get_scheduled(gnucash_sqlite_path.ids.sched)
        assert sched is not None
        assert sched.name == "Monthly rent"
        assert sched.enabled is True
        assert sched.recurrence.occurrences(date(2026, 3, 31)) == [
            date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1),
        ]

    def test_template_splits_resolve_to_the_real_accounts(self, db, gnucash_sqlite_path):
        """The template account is a placeholder; the slot names the real one."""
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        sched = db.get_scheduled(gnucash_sqlite_path.ids.sched)
        accounts = {split.account for split in sched.splits}
        assert accounts == {
            gnucash_sqlite_path.ids.rent, gnucash_sqlite_path.ids.checking
        }

    def test_the_imported_schedule_produces_a_balanced_transaction(
        self, db, gnucash_sqlite_path
    ):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        sched = db.get_scheduled(gnucash_sqlite_path.ids.sched)
        txn = sched.instantiate(date(2026, 4, 1))
        assert txn.is_balanced()
        assert txn.value_for(gnucash_sqlite_path.ids.rent) == Money("1800.00")

    def test_scheduled_import_can_be_skipped(self, db, gnucash_sqlite_path):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path, include_scheduled=False)
        assert list(db.iter_scheduled()) == []


class TestStandaloneReaders:
    """Reading a GnuCash book without importing it, in the spirit of gnucash-cli."""

    def test_lists_accounts_with_full_paths(self, gnucash_sqlite_path):
        accounts = gnucash_sqlite.read_accounts(gnucash_sqlite_path.path)
        paths = {row["full_name"] for row in accounts}
        assert "Assets:Checking Account" in paths
        assert "Expenses:Rent" in paths

    def test_lists_transactions_with_splits(self, gnucash_sqlite_path):
        rows = gnucash_sqlite.read_transactions(gnucash_sqlite_path.path)
        assert len(rows) == 3
        payroll = next(r for r in rows if r["description"] == "Payroll deposit")
        assert payroll["date"] == "2026-01-25"
        assert {s["value"] for s in payroll["splits"]} == {"4200.00", "-4200.00"}

    def test_filters_transactions_by_account(self, gnucash_sqlite_path):
        rows = gnucash_sqlite.read_transactions(
            gnucash_sqlite_path.path, account_guid=gnucash_sqlite_path.ids.card
        )
        assert [r["description"] for r in rows] == ["Supermarket"]

    def test_filters_transactions_by_date(self, gnucash_sqlite_path):
        rows = gnucash_sqlite.read_transactions(
            gnucash_sqlite_path.path, start=date(2026, 1, 20), end=date(2026, 1, 31)
        )
        assert [r["description"] for r in rows] == ["Payroll deposit"]


class TestXmlImport:
    def test_imports_a_compressed_book(self, db, gnucash_xml_path):
        result = gnucash_xml.import_book(db, gnucash_xml_path.path)
        assert result.accounts == 4
        assert result.transactions == 2

    def test_amounts_parse_from_the_fraction_notation(self, db, gnucash_xml_path):
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        assert ledger.balance(db, gnucash_xml_path.ids.bank) == Money("3093.17")

    def test_account_tree_is_rebuilt(self, db, gnucash_xml_path):
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        assert db.full_name(gnucash_xml_path.ids.bank) == "Current Account"
        account = db.get_account(gnucash_xml_path.ids.bank)
        assert account.code == "1200"
        assert account.notes == "Generic XML account note"

    def test_reconcile_state_survives(self, db, gnucash_xml_path):
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        txn = db.get_transaction(gnucash_xml_path.ids.txn1)
        split = txn.split_for(gnucash_xml_path.ids.bank)
        assert split.reconcile.value == "c"

    def test_memos_and_numbers_survive(self, db, gnucash_xml_path):
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        txn = db.get_transaction(gnucash_xml_path.ids.txn2)
        assert txn.num == "DD"
        assert txn.split_for(gnucash_xml_path.ids.util).memo == "quarterly"

    def test_non_dollar_commodities_survive(self, db, gnucash_xml_path):
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        assert db.get_commodity_by_mnemonic("GBP").fullname == "Pound Sterling"

    def test_a_plain_uncompressed_book_reads_too(self, db, tmp_path, gnucash_xml_path):
        plain = tmp_path / "plain.gnucash"
        plain.write_text(gnucash_xml_path.plain, encoding="utf-8")
        result = gnucash_xml.import_book(db, plain)
        assert result.transactions == 2

    def test_importing_both_formats_into_one_book(
        self, db, gnucash_sqlite_path, gnucash_xml_path
    ):
        """Two households, two formats, one BreadSched book: handles must not collide."""
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        assert db.summary()["txn"] == 5
        assert ledger.balance(db, gnucash_sqlite_path.ids.checking) == Money("2350.00")
        assert ledger.balance(db, gnucash_xml_path.ids.bank) == Money("3093.17")


class TestDamagedBooks:
    """Real books contain damage. An importer that refuses them is not usable."""

    def test_an_out_of_balance_transaction_is_posted_to_imbalance(self, db, tmp_path):
        """A refused import helps nobody; the difference is made visible instead."""
        book = create_book(
            tmp_path / "broken.gnucash",
            accounts=[
                ("root", "Root Account", "ROOT", None, 0),
                ("bank", "Checking", "BANK", "root", 0),
                ("food", "Groceries", "EXPENSE", "root", 0),
            ],
            transactions=[
                # 100.00 of groceries paid for with 90.00: ten dollars unaccounted.
                (date(2026, 1, 1), "Lopsided", [
                    ("food", 10000, 100, ""),
                    ("bank", -9000, 100, ""),
                ]),
            ],
        )

        result = gnucash_sqlite.import_book(db, book.path)

        assert result.warnings
        imbalance = db.get_account_by_name("Imbalance")
        assert imbalance is not None
        assert ledger.balance(db, imbalance.handle, natural_sign=False) == Money("-10.00")
        assert result.transactions == 1

    def test_the_repaired_transaction_still_balances(self, db, tmp_path):
        book = create_book(
            tmp_path / "broken.gnucash",
            accounts=[
                ("root", "Root Account", "ROOT", None, 0),
                ("bank", "Checking", "BANK", "root", 0),
                ("food", "Groceries", "EXPENSE", "root", 0),
            ],
            transactions=[
                (date(2026, 1, 1), "Lopsided", [
                    ("food", 10000, 100, ""),
                    ("bank", -9000, 100, ""),
                ]),
            ],
        )
        gnucash_sqlite.import_book(db, book.path)

        repaired = next(iter(db.iter_transactions()))
        assert repaired.is_balanced()
        assert len(repaired.splits) == 3

    def test_a_split_pointing_at_a_missing_account_is_skipped_not_fatal(
        self, db, tmp_path
    ):
        """One damaged transaction must not cost the user the other thousand."""
        book = create_book(
            tmp_path / "orphan.gnucash",
            accounts=[
                ("root", "Root Account", "ROOT", None, 0),
                ("bank", "Checking", "BANK", "root", 0),
                ("food", "Groceries", "EXPENSE", "root", 0),
            ],
            transactions=[
                (date(2026, 1, 2), "Fine", [
                    ("food", 5000, 100, ""),
                    ("bank", -5000, 100, ""),
                ]),
            ],
        )

        # Repoint one split at an account that is not in the book at all.
        conn = sqlite3.connect(book.path)
        conn.execute(
            "INSERT INTO transactions VALUES (?,?,?,?,?,?)",
            (new_guid(), book.currency, "", "20260101120000", "20260101120000",
             "Orphaned"),
        )
        orphan_txn = conn.execute(
            "SELECT guid FROM transactions WHERE description='Orphaned'"
        ).fetchone()[0]
        for account, value in ((book.bank, 100), (new_guid(), -100)):
            conn.execute(
                "INSERT INTO splits VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_guid(), orphan_txn, account, "", "", "n", None, value, 100,
                 value, 100, None),
            )
        conn.commit()
        conn.close()

        result = gnucash_sqlite.import_book(db, book.path)

        assert result.skipped == 1
        assert result.transactions == 1  # the healthy one still came across
        assert db.get_account(book.bank) is not None
        assert ledger.balance(db, book.bank) == Money("-50.00")


class TestXmlScheduledTransactions:
    """The reported bug: GUID-named accounts in the register, no schedules listed."""

    def test_template_accounts_are_not_imported(self, db, gnucash_xml_path):
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        assert db.get_account(gnucash_xml_path.ids.tmpl_acct) is None
        assert db.get_account(gnucash_xml_path.ids.tmpl_root) is None

    def test_no_guid_named_accounts_appear(self, db, gnucash_xml_path):
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        guidish = [
            a.name for a in db.iter_accounts()
            if len(a.name) == 32 and all(c in "0123456789abcdef" for c in a.name)
        ]
        assert guidish == []

    def test_template_transactions_stay_out_of_the_register(self, db, gnucash_xml_path):
        result = gnucash_xml.import_book(db, gnucash_xml_path.path)
        assert result.transactions == 2
        assert {t.description for t in db.iter_transactions()} == {
            "March salary", "Electricity"
        }

    def test_the_schedule_is_imported(self, db, gnucash_xml_path):
        result = gnucash_xml.import_book(db, gnucash_xml_path.path)
        assert result.scheduled == 1
        schedule = db.get_scheduled(gnucash_xml_path.ids.sx)
        assert schedule is not None
        assert schedule.name == "Monthly rent"
        assert schedule.enabled is True
        assert schedule.auto_create is True
        assert schedule.advance_days == 2

    def test_the_recurrence_is_read(self, db, gnucash_xml_path):
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        schedule = db.get_scheduled(gnucash_xml_path.ids.sx)
        assert schedule.recurrence.occurrences(date(2026, 3, 31)) == [
            date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)
        ]

    def test_template_splits_resolve_to_real_accounts(self, db, gnucash_xml_path):
        """The slot indirection: the split names the template, the slot the account."""
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        schedule = db.get_scheduled(gnucash_xml_path.ids.sx)
        accounts = {split.account for split in schedule.splits}
        assert accounts == {gnucash_xml_path.ids.util, gnucash_xml_path.ids.bank}

    def test_credit_and_debit_formulas_become_signed_amounts(
        self, db, gnucash_xml_path
    ):
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        schedule = db.get_scheduled(gnucash_xml_path.ids.sx)
        by_account = {s.account: s.resolve() for s in schedule.splits}
        assert by_account[gnucash_xml_path.ids.util] == Money("825.00")
        assert by_account[gnucash_xml_path.ids.bank] == Money("-825.00")

    def test_the_imported_schedule_produces_a_balanced_transaction(
        self, db, gnucash_xml_path
    ):
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        schedule = db.get_scheduled(gnucash_xml_path.ids.sx)
        txn = schedule.instantiate(date(2026, 4, 1))
        assert txn.is_balanced()
        assert txn.value_for(gnucash_xml_path.ids.util) == Money("825.00")

    def test_it_appears_in_the_due_list(self, db, gnucash_xml_path):
        from breadsched.gen.engine import schedule as schedule_engine

        gnucash_xml.import_book(db, gnucash_xml_path.path)
        due = schedule_engine.due_occurrences(db, as_of=date(2026, 3, 15), horizon_days=0)
        assert [o.when for o in due] == [
            date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)
        ]

    def test_scheduled_import_can_be_skipped(self, db, gnucash_xml_path):
        result = gnucash_xml.import_book(
            db, gnucash_xml_path.path, include_scheduled=False
        )
        assert result.scheduled == 0
        assert list(db.iter_scheduled()) == []
