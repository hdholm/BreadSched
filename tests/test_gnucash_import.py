"""GnuCash compatibility.

These tests are the contract with an external application whose format we do not
control, so they assert against books built to GnuCash's own schema rather than
against anything this codebase produces.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from gnucash_fixtures import (
    create_book,
    new_guid,
    write_account,
    write_commodity,
    write_price,
    write_transaction,
)

from breadsched.gen.engine import activity, ledger, valuation
from breadsched.gen.lib import (
    Account,
    AccountType,
    FsaFundingYear,
    GnuCashAccountType,
    InvestmentActivityKind,
    Money,
    PlanningFlowKind,
    PlanningResolution,
    ScheduledSplit,
    ScheduledTransaction,
)
from breadsched.gen.plug import IMPORTER, PluginManager
from breadsched.gen.utils import OperationCancelled
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


def test_cancelling_an_import_rolls_back_the_whole_batch(db, gnucash_sqlite_path):
    before = db.summary()

    def cancel(_stage, _done, _total):
        raise OperationCancelled()

    with pytest.raises(OperationCancelled):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path, progress=cancel)

    assert db.summary() == before


def test_imported_formula_support_is_decided_only_by_the_safe_engine():
    supported = ScheduledTransaction(
        splits=[
            ScheduledSplit(
                "account",
                formula="ipmt( .05000 / 12.00 : i : 180.00 : 200,000.00 : 0 : 0 )",
            )
        ]
    )
    unsafe = ScheduledTransaction(
        splits=[ScheduledSplit("account", formula="__import__('os').system('false')")]
    )
    assert supported.formula_problem() is None
    assert supported.splits[0].formula == (
        "ipmt( .05000 / 12.00 : i : 180.00 : 200,000.00 : 0 : 0 )"
    )
    assert "unknown function" in unsafe.formula_problem()


class TestDateParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("20260125104000", date(2026, 1, 25)),  # modern SQLite books
            ("2026-01-25 10:40:00", date(2026, 1, 25)),  # older SQLite books
            ("2026-01-25 10:59:00 +0000", date(2026, 1, 25)),  # XML books
            ("20260125", date(2026, 1, 25)),  # recurrence start dates
        ],
    )
    def test_every_shape_gnucash_emits(self, raw, expected):
        assert gnucash_common.parse_gnc_date(raw) == expected

    def test_nonsense_is_rejected_rather_than_guessed(self):
        with pytest.raises(ValueError):
            gnucash_common.parse_gnc_date("last Tuesday")

    def test_missing_dates_are_rejected_rather_than_replaced_with_today(self):
        with pytest.raises(ValueError, match="missing GnuCash date"):
            gnucash_common.parse_gnc_date("")


class TestSqliteImport:
    def test_notify_false_suppresses_the_complete_importer_boundary(self, db, gnucash_sqlite_path):
        seen = []
        db.connect("database-changed", lambda *_: seen.append("changed"))

        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path, notify=False)

        assert seen == []

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
        assert account.commodity_scu == 100

    def test_retains_typed_account_fields_without_interpreting_them(self, db, gnucash_sqlite_path):
        with sqlite3.connect(gnucash_sqlite_path.path) as source:
            source.execute(
                "UPDATE accounts SET non_std_scu=1 WHERE guid=?",
                (gnucash_sqlite_path.ids.checking,),
            )
            source.execute(
                "INSERT INTO slots (obj_guid,name,slot_type,string_val) VALUES (?,?,?,?)",
                (gnucash_sqlite_path.ids.checking, "color", 4, "#315a74"),
            )
            source.execute(
                "INSERT INTO slots (obj_guid,name,slot_type,int64_val) VALUES (?,?,?,?)",
                (gnucash_sqlite_path.ids.checking, "tax-related", 1, 1),
            )

        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)

        account = db.get_account(gnucash_sqlite_path.ids.checking)
        assert account is not None
        fields = {(field.name, field.value_type): field.value for field in account.source_fields}
        assert fields[("account:non-standard-scu", "boolean")] == "true"
        assert fields[("slot:color", "string; source type 4")] == "#315a74"
        assert fields[("slot:tax-related", "int64; source type 1")] == "1"

    def test_historical_and_unknown_source_types_are_not_lost(self, db, gnucash_sqlite_path):
        unknown_guid = new_guid()
        with sqlite3.connect(gnucash_sqlite_path.path) as source:
            source.execute(
                "UPDATE accounts SET account_type=? WHERE guid=?",
                ("MONEYMRKT", gnucash_sqlite_path.ids.checking),
            )
            write_account(
                source,
                unknown_guid,
                "Imported special account",
                "HOUSEHOLD-SPECIAL",
                gnucash_sqlite_path.ids.root,
                gnucash_sqlite_path.ids.currency,
            )

        result = gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)

        legacy = db.get_account(gnucash_sqlite_path.ids.checking)
        assert legacy is not None
        assert legacy.atype is AccountType.BANK
        assert legacy.source_atype is GnuCashAccountType.MONEYMRKT
        assert legacy.source_type == "MONEYMRKT"
        unknown = db.get_account(unknown_guid)
        assert unknown is not None
        assert unknown.atype is AccountType.TECHNICAL
        assert unknown.source_atype is None
        assert unknown.source_type == "HOUSEHOLD-SPECIAL"
        assert any("preserved as Technical for review" in warning for warning in result.warnings)

    def test_adopted_account_keeps_source_identity_across_rename_and_move(
        self, db, gnucash_sqlite_path
    ):
        root = Account(name="Root", atype=AccountType.ROOT)
        assets = Account(
            name="Assets",
            atype=AccountType.ASSET,
            parent=root.handle,
            placeholder=True,
        )
        with db.transaction("Create native chart") as txn:
            db.add_account(root, txn)
            db.add_account(assets, txn)

        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        adopted = db.get_account(assets.handle)
        assert adopted is not None
        assert adopted.source_guid == gnucash_sqlite_path.ids.assets
        before = len(list(db.iter_accounts()))

        new_parent = new_guid()
        with sqlite3.connect(gnucash_sqlite_path.path) as source:
            write_account(
                source,
                new_parent,
                "Long-term holdings",
                "ASSET",
                gnucash_sqlite_path.ids.root,
                gnucash_sqlite_path.ids.currency,
                placeholder=1,
            )
            source.execute(
                "UPDATE accounts SET name=?, description=?, parent_guid=? WHERE guid=?",
                (
                    "Household holdings",
                    "Renamed and moved in source",
                    new_parent,
                    gnucash_sqlite_path.ids.assets,
                ),
            )

        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)

        refreshed = db.get_account(assets.handle)
        assert refreshed is not None
        assert refreshed.name == "Household holdings"
        assert refreshed.description == "Renamed and moved in source"
        assert refreshed.parent == new_parent
        assert refreshed.source_guid == gnucash_sqlite_path.ids.assets
        assert len(list(db.iter_accounts())) == before + 1
        checking = db.get_account(gnucash_sqlite_path.ids.checking)
        assert checking is not None
        assert checking.parent == assets.handle

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

    def test_imported_bank_account_can_keep_local_escrow_semantics_on_reimport(
        self, db, gnucash_sqlite_path
    ):
        escrow_guid = new_guid()
        funding_guid = new_guid()
        draw_guid = new_guid()
        with sqlite3.connect(gnucash_sqlite_path.path) as source:
            write_account(
                source,
                escrow_guid,
                "Property escrow",
                "BANK",
                gnucash_sqlite_path.ids.assets,
                gnucash_sqlite_path.ids.currency,
            )
            write_transaction(
                source,
                funding_guid,
                gnucash_sqlite_path.ids.currency,
                date(2026, 1, 5),
                "Fund property escrow",
                [
                    (escrow_guid, 10000, 100, ""),
                    (gnucash_sqlite_path.ids.checking, -10000, 100, ""),
                ],
            )
            write_transaction(
                source,
                draw_guid,
                gnucash_sqlite_path.ids.currency,
                date(2026, 1, 20),
                "Escrow insurance draw",
                [
                    (gnucash_sqlite_path.ids.rent, 8000, 100, ""),
                    (escrow_guid, -8000, 100, ""),
                ],
            )
            source.commit()

        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        escrow = db.get_account(escrow_guid)
        assert escrow is not None
        escrow.atype = AccountType.ESCROW
        with db.transaction("Classify imported escrow") as txn:
            db.commit_account(escrow, txn)

        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        report = activity.build_category_report(
            db, date(2026, 1, 1), date(2026, 1, 31), as_of=date(2026, 1, 31)
        )
        escrow_flow = next(
            row for row in report.planning_flows if row.kind is PlanningFlowKind.ESCROW_FUNDING
        )
        rent = next(row for row in report.categories if row.account == gnucash_sqlite_path.ids.rent)

        assert db.get_account(escrow_guid).atype is AccountType.ESCROW
        assert escrow_flow.actual == [Money("100")]
        assert rent.actual == [Money("1800")]

    def test_multi_split_transactions_keep_every_leg(self, db, gnucash_sqlite_path):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        shop = next(t for t in db.iter_transactions() if t.description == "Supermarket")
        assert len(shop.splits) == 3
        assert shop.is_balanced()

    def test_liability_balances_read_naturally(self, db, gnucash_sqlite_path):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        assert ledger.balance(db, gnucash_sqlite_path.ids.card) == Money("75.50")

    def test_commodities_come_across(self, db, gnucash_sqlite_path):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        usd = db.get_commodity_by_mnemonic("USD")
        assert usd is not None and usd.fraction == 100

    def test_security_prices_come_across_and_reimport_by_guid(self, db, gnucash_sqlite_path):
        security_guid = new_guid()
        price_guid = new_guid()
        account_guid = new_guid()
        transaction_guid = new_guid()
        with sqlite3.connect(gnucash_sqlite_path.path) as source:
            write_commodity(
                source,
                security_guid,
                namespace="FUND",
                mnemonic="INDEX",
                fullname="Generic index fund",
                fraction=10000,
            )
            write_account(
                source,
                account_guid,
                "Index holding",
                "STOCK",
                gnucash_sqlite_path.ids.assets,
                security_guid,
            )
            write_transaction(
                source,
                transaction_guid,
                gnucash_sqlite_path.ids.currency,
                date(2026, 1, 1),
                "Opening holding",
                [
                    (account_guid, 100000, 100, ""),
                    (gnucash_sqlite_path.ids.checking, -100000, 100, ""),
                ],
            )
            source.execute(
                "UPDATE splits SET quantity_num=10, quantity_denom=1 "
                "WHERE tx_guid=? AND account_guid=?",
                (transaction_guid, account_guid),
            )
            write_price(
                source,
                price_guid,
                security_guid,
                gnucash_sqlite_path.ids.currency,
                date(2026, 3, 1),
                12525,
            )

        first = gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        second = gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)

        price = db.get_price(price_guid)
        security = db.get_commodity_by_mnemonic("INDEX")
        assert first.prices == second.prices == 1
        assert price is not None
        assert security is not None
        assert price.value == Money("125.25")
        assert price.commodity == security.handle
        assert valuation.account_value(db, account_guid).total == Money("1252.50")

    def test_missing_transaction_date_is_reported_and_skipped(self, db, gnucash_sqlite_path):
        conn = sqlite3.connect(gnucash_sqlite_path.path)
        conn.execute(
            "UPDATE transactions SET post_date = NULL WHERE description = ?",
            ("Rent",),
        )
        conn.commit()
        conn.close()

        result = gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)

        assert result.skipped >= 1
        assert all(txn.description != "Rent" for txn in db.iter_transactions())
        assert any(reason == "missing GnuCash date" for reason, _ in result.skipped_details)

    def test_the_source_book_is_never_written_to(self, db, gnucash_sqlite_path):
        import os

        before = os.stat(gnucash_sqlite_path.path).st_mtime_ns
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        assert os.stat(gnucash_sqlite_path.path).st_mtime_ns == before

    def test_reimporting_updates_rather_than_duplicates(self, db, gnucash_sqlite_path):
        first = gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        counts_before = db.summary()
        second = gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        assert db.summary() == counts_before
        assert first.transactions == 3
        assert first.transactions_new == 3
        assert first.splits_new == first.splits
        assert second.transactions_unchanged == 3
        assert second.splits_unchanged == second.splits
        assert second.transactions_new == second.transactions_refreshed == 0

    def test_reimport_reports_source_records_that_were_refreshed(self, db, gnucash_sqlite_path):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        conn = sqlite3.connect(gnucash_sqlite_path.path)
        conn.execute(
            "UPDATE transactions SET description=? WHERE description=?",
            ("Updated household payment", "Rent"),
        )
        conn.execute(
            """UPDATE splits SET memo=? WHERE guid=(
                SELECT splits.guid FROM splits
                JOIN transactions ON transactions.guid=splits.tx_guid
                WHERE transactions.description=? LIMIT 1
            )""",
            ("Updated source memo", "Updated household payment"),
        )
        conn.commit()
        conn.close()

        result = gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)

        assert result.transactions_refreshed >= 1
        assert result.transactions_new == 0
        assert result.splits_refreshed == 1
        assert "refreshed" in result.detail()

    def test_reimport_preserves_breadsched_owned_account_configuration(
        self, db, gnucash_sqlite_path
    ):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        account = db.get_account(gnucash_sqlite_path.ids.checking)
        assert account is not None
        account.atype = AccountType.FSA
        account.fsa_years = [
            FsaFundingYear(
                start=date(2026, 1, 1),
                through=date(2026, 12, 31),
                election=Money("1200.00"),
            )
        ]
        account.annual_return = Decimal("0.041")
        account.annual_interest = Decimal("0.073")
        account.exclude_from_projection = True
        account.group = "Planning group"
        account.pays_in_full = False
        account.usual_payment = Money("125.00")
        account.payment_day = 18
        account.emergency_fund_override = False
        card = db.get_account(gnucash_sqlite_path.ids.card)
        assert card is not None
        card.card_payment_account = gnucash_sqlite_path.ids.checking
        with db.transaction("Configure imported account") as db_txn:
            db.commit_account(account, db_txn)
            db.commit_account(card, db_txn)

        with sqlite3.connect(gnucash_sqlite_path.path) as source:
            source.execute(
                "UPDATE accounts SET description=?, account_type=? WHERE guid=?",
                ("Updated source description", "ASSET", gnucash_sqlite_path.ids.checking),
            )
            source.commit()

        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        reimported = db.get_account(gnucash_sqlite_path.ids.checking)
        assert reimported is not None
        assert reimported.description == "Updated source description"
        assert reimported.atype is AccountType.FSA
        assert reimported.source_atype is GnuCashAccountType.ASSET
        assert reimported.fsa_years == account.fsa_years
        assert reimported.annual_return == Decimal("0.041")
        assert reimported.annual_interest == Decimal("0.073")
        assert reimported.exclude_from_projection is True
        assert reimported.group == "Planning group"
        assert reimported.pays_in_full is False
        assert reimported.usual_payment == Money("125.00")
        assert reimported.payment_day == 18
        assert reimported.emergency_fund_override is False
        reimported_card = db.get_account(gnucash_sqlite_path.ids.card)
        assert reimported_card is not None
        assert reimported_card.card_payment_account == gnucash_sqlite_path.ids.checking

    def test_incompatible_source_type_change_requires_review_and_keeps_type(
        self, db, gnucash_sqlite_path
    ):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        account = db.get_account(gnucash_sqlite_path.ids.checking)
        assert account is not None
        account.atype = AccountType.FSA
        with db.transaction("Set account type") as txn:
            db.commit_account(account, txn)

        with sqlite3.connect(gnucash_sqlite_path.path) as source:
            source.execute(
                "UPDATE accounts SET account_type=? WHERE guid=?",
                ("LIABILITY", account.handle),
            )
            source.commit()

        result = gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        reimported = db.get_account(account.handle)
        assert reimported is not None
        assert reimported.atype is AccountType.FSA
        assert reimported.source_atype is GnuCashAccountType.LIABILITY
        assert any("retained its BreadSched type" in warning for warning in result.warnings)

    def test_reimport_preserves_breadsched_owned_annotations(self, db, gnucash_sqlite_path):
        with sqlite3.connect(gnucash_sqlite_path.path) as source:
            source.row_factory = sqlite3.Row
            source_guid = source.execute(
                "SELECT guid FROM transactions WHERE description = 'Supermarket'"
            ).fetchone()["guid"]
            source.execute(
                "INSERT INTO slots (obj_guid,name,slot_type,string_val) VALUES (?,?,?,?)",
                (source_guid, "notes", 4, "Imported shopping note"),
            )
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        transaction = next(
            item for item in db.iter_transactions() if item.description == "Supermarket"
        )
        assert transaction.source_notes == "Imported shopping note"
        transaction.notes = "planning note"
        transaction.planned_occurrence = "schedule:occurrence"
        transaction.planned_for = date(2026, 1, 14)
        transaction.planned_amount = Money("75.00")
        transaction.planning_resolution = PlanningResolution.MATCHED
        transaction.rejected_plan_occurrences = ["other:occurrence"]
        transaction.splits[0].planning_flow = PlanningFlowKind.RETIREMENT_SAVING
        transaction.splits[0].investment_activity = InvestmentActivityKind.CONTRIBUTION
        transaction.splits[0].fsa_year_start = date(2026, 1, 1)
        with db.transaction("Annotate imported transaction") as db_txn:
            db.commit_transaction(transaction, db_txn)

        with sqlite3.connect(gnucash_sqlite_path.path) as source:
            source.execute(
                "UPDATE slots SET string_val=? WHERE obj_guid=? AND name='notes'",
                ("Revised imported note", transaction.handle),
            )

        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        reimported = db.get_transaction(transaction.handle)
        assert reimported is not None
        assert reimported.notes == "planning note"
        assert reimported.source_notes == "Revised imported note"
        assert reimported.planned_occurrence == "schedule:occurrence"
        assert reimported.planned_for == date(2026, 1, 14)
        assert reimported.planned_amount == Money("75.00")
        assert reimported.planning_resolution is PlanningResolution.MATCHED
        assert reimported.rejected_plan_occurrences == ["other:occurrence"]
        annotated_split = next(
            split for split in reimported.splits if split.handle == transaction.splits[0].handle
        )
        assert annotated_split.planning_flow is PlanningFlowKind.RETIREMENT_SAVING
        assert annotated_split.investment_activity is InvestmentActivityKind.CONTRIBUTION
        assert annotated_split.fsa_year_start == date(2026, 1, 1)

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
            date(2026, 1, 1),
            date(2026, 2, 1),
            date(2026, 3, 1),
        ]

    def test_template_splits_resolve_to_the_real_accounts(self, db, gnucash_sqlite_path):
        """The template account is a placeholder; the slot names the real one."""
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        sched = db.get_scheduled(gnucash_sqlite_path.ids.sched)
        accounts = {split.account for split in sched.splits}
        assert accounts == {gnucash_sqlite_path.ids.rent, gnucash_sqlite_path.ids.checking}

    def test_supported_template_formulas_remain_dynamic(self, db, gnucash_sqlite_path):
        with sqlite3.connect(gnucash_sqlite_path.path) as source:
            source.row_factory = sqlite3.Row
            targets = {
                row["guid_val"]: row["obj_guid"]
                for row in source.execute(
                    "SELECT obj_guid, guid_val FROM slots WHERE name = 'sched-xaction/account'"
                )
            }
            for target, slot_name in (
                (gnucash_sqlite_path.ids.rent, "sched-xaction/debit-formula"),
                (gnucash_sqlite_path.ids.checking, "sched-xaction/credit-formula"),
            ):
                source.execute(
                    "INSERT INTO slots (obj_guid,name,slot_type,string_val) VALUES (?,?,?,?)",
                    (targets[target], slot_name, 4, "100 + period"),
                )

        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        sched = db.get_scheduled(gnucash_sqlite_path.ids.sched)
        assert all(split.formula for split in sched.splits)
        april = sched.instantiate(date(2026, 4, 1))
        assert april.is_balanced()
        assert max(split.value for split in april.splits) == Money("104")

    def test_the_imported_schedule_produces_a_balanced_transaction(self, db, gnucash_sqlite_path):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        sched = db.get_scheduled(gnucash_sqlite_path.ids.sched)
        txn = sched.instantiate(date(2026, 4, 1))
        assert txn.is_balanced()
        assert txn.value_for(gnucash_sqlite_path.ids.rent) == Money("1800.00")

    def test_scheduled_import_can_be_skipped(self, db, gnucash_sqlite_path):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path, include_scheduled=False)
        assert list(db.iter_scheduled()) == []

    def test_reimport_preserves_unambiguous_local_investment_classification(
        self, db, gnucash_sqlite_path
    ):
        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
        account = db.get_account(gnucash_sqlite_path.ids.checking)
        schedule = db.get_scheduled(gnucash_sqlite_path.ids.sched)
        assert account is not None and schedule is not None
        account.atype = AccountType.INVESTMENT
        checking_split = next(split for split in schedule.splits if split.account == account.handle)
        checking_split.investment_activity = InvestmentActivityKind.WITHDRAWAL
        with db.transaction("Classify investment schedule") as txn:
            db.commit_account(account, txn)
            db.commit_scheduled(schedule, txn)

        gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)

        restored = db.get_scheduled(schedule.handle)
        assert restored is not None
        restored_split = next(split for split in restored.splits if split.account == account.handle)
        assert restored_split.investment_activity is InvestmentActivityKind.WITHDRAWAL


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
    def test_notify_false_suppresses_the_complete_importer_boundary(self, db, gnucash_xml_path):
        seen = []
        db.connect("database-changed", lambda *_: seen.append("changed"))

        gnucash_xml.import_book(db, gnucash_xml_path.path, notify=False)

        assert seen == []

    def test_imports_a_compressed_book(self, db, gnucash_xml_path):
        result = gnucash_xml.import_book(db, gnucash_xml_path.path)
        assert result.accounts == 4
        assert result.transactions == 2

    def test_missing_transaction_date_is_reported_and_skipped(self, db, tmp_path, gnucash_xml_path):
        damaged = tmp_path / "missing-date.gnucash"
        damaged.write_text(
            gnucash_xml_path.plain.replace(
                "<ts:date>2026-03-25 10:59:00 +0000</ts:date>",
                "<ts:date></ts:date>",
                1,
            ),
            encoding="utf-8",
        )

        result = gnucash_xml.import_book(db, damaged)

        assert result.skipped >= 1
        assert db.get_transaction(gnucash_xml_path.ids.txn1) is None
        assert any(reason == "missing GnuCash date" for reason, _ in result.skipped_details)

    def test_amounts_parse_from_the_fraction_notation(self, db, gnucash_xml_path):
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        assert ledger.balance(db, gnucash_xml_path.ids.bank) == Money("3093.17")

    def test_account_tree_is_rebuilt(self, db, gnucash_xml_path):
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        assert db.full_name(gnucash_xml_path.ids.bank) == "Current Account"
        account = db.get_account(gnucash_xml_path.ids.bank)
        assert account.code == "1200"
        assert account.notes == "Generic XML account note"
        assert account.commodity_scu == 1000

    def test_nested_xml_account_fields_remain_typed_and_inspectable(
        self, db, tmp_path, gnucash_xml_path
    ):
        extra = """
      <slot>
        <slot:key>reconcile-info</slot:key>
        <slot:value type="frame">
          <slot>
            <slot:key>last-date</slot:key>
            <slot:value type="gdate"><gdate>2026-01-31</gdate></slot:value>
          </slot>
        </slot:value>
      </slot>"""
        source = tmp_path / "account-fields.gnucash"
        source.write_text(
            gnucash_xml_path.plain.replace(
                "    </act:slots>\n  </gnc:account>",
                f"{extra}\n    </act:slots>\n  </gnc:account>",
                1,
            ),
            encoding="utf-8",
        )

        gnucash_xml.import_book(db, source)

        account = db.get_account(gnucash_xml_path.ids.bank)
        assert account is not None
        assert account.source_guid == gnucash_xml_path.ids.bank
        assert account.source_type == "BANK"
        fields = {(field.name, field.value_type): field.value for field in account.source_fields}
        assert fields[("slot:notes", "string")] == "Generic XML account note"
        assert fields[("slot:reconcile-info/last-date", "gdate")] == "2026-01-31"

    def test_reconcile_state_survives(self, db, gnucash_xml_path):
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        txn = db.get_transaction(gnucash_xml_path.ids.txn1)
        split = txn.split_for(gnucash_xml_path.ids.bank)
        assert split.reconcile.value == "c"

    def test_memos_and_numbers_survive(self, db, gnucash_xml_path):
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        txn = db.get_transaction(gnucash_xml_path.ids.txn2)
        assert txn.num == "DD"
        assert txn.source_notes == "Imported transaction note"
        assert txn.split_for(gnucash_xml_path.ids.util).memo == "quarterly"

    def test_non_dollar_commodities_survive(self, db, gnucash_xml_path):
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        assert db.get_commodity_by_mnemonic("GBP").fullname == "Pound Sterling"

    def test_xml_security_prices_are_imported(self, db, tmp_path, gnucash_xml_path):
        security_guid = new_guid()
        price_guid = new_guid()
        body = gnucash_xml_path.plain.replace(
            'xmlns:recurrence="http://www.gnucash.org/XML/recurrence">',
            'xmlns:recurrence="http://www.gnucash.org/XML/recurrence"\n'
            '     xmlns:price="http://www.gnucash.org/XML/price">',
        )
        price_xml = f"""
  <gnc:commodity version="2.0.0">
    <cmdty:space>FUND</cmdty:space><cmdty:id>INDEX</cmdty:id>
    <cmdty:name>Generic index fund</cmdty:name><cmdty:fraction>10000</cmdty:fraction>
  </gnc:commodity>
  <gnc:pricedb version="1">
    <price>
      <price:id type="guid">{price_guid}</price:id>
      <price:commodity><cmdty:space>FUND</cmdty:space><cmdty:id>INDEX</cmdty:id></price:commodity>
      <price:currency><cmdty:space>CURRENCY</cmdty:space><cmdty:id>GBP</cmdty:id></price:currency>
      <price:time><ts:date>2026-03-01 10:00:00 +0000</ts:date></price:time>
      <price:source>user:price-editor</price:source><price:type>last</price:type>
      <price:value>12525/100</price:value>
    </price>
  </gnc:pricedb>
"""
        body = body.replace(
            '  <gnc:account version="2.0.0">',
            price_xml + '\n  <gnc:account version="2.0.0">',
            1,
        )
        path = tmp_path / f"{security_guid}.gnucash"
        path.write_text(body, encoding="utf-8")

        result = gnucash_xml.import_book(db, path)

        price = db.get_price(price_guid)
        assert result.prices == 1
        assert price is not None
        assert price.value == Money("125.25")

    def test_a_plain_uncompressed_book_reads_too(self, db, tmp_path, gnucash_xml_path):
        plain = tmp_path / "plain.gnucash"
        plain.write_text(gnucash_xml_path.plain, encoding="utf-8")
        result = gnucash_xml.import_book(db, plain)
        assert result.transactions == 2

    def test_importing_both_formats_into_one_book(self, db, gnucash_sqlite_path, gnucash_xml_path):
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
                (
                    date(2026, 1, 1),
                    "Lopsided",
                    [
                        ("food", 10000, 100, ""),
                        ("bank", -9000, 100, ""),
                    ],
                ),
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
                (
                    date(2026, 1, 1),
                    "Lopsided",
                    [
                        ("food", 10000, 100, ""),
                        ("bank", -9000, 100, ""),
                    ],
                ),
            ],
        )
        gnucash_sqlite.import_book(db, book.path)

        repaired = next(iter(db.iter_transactions()))
        assert repaired.is_balanced()
        assert len(repaired.splits) == 3

    def test_a_split_pointing_at_a_missing_account_is_skipped_not_fatal(self, db, tmp_path):
        """One damaged transaction must not cost the user the other thousand."""
        book = create_book(
            tmp_path / "orphan.gnucash",
            accounts=[
                ("root", "Root Account", "ROOT", None, 0),
                ("bank", "Checking", "BANK", "root", 0),
                ("food", "Groceries", "EXPENSE", "root", 0),
            ],
            transactions=[
                (
                    date(2026, 1, 2),
                    "Fine",
                    [
                        ("food", 5000, 100, ""),
                        ("bank", -5000, 100, ""),
                    ],
                ),
            ],
        )

        # Repoint one split at an account that is not in the book at all.
        conn = sqlite3.connect(book.path)
        conn.execute(
            "INSERT INTO transactions VALUES (?,?,?,?,?,?)",
            (new_guid(), book.currency, "", "20260101120000", "20260101120000", "Orphaned"),
        )
        orphan_txn = conn.execute(
            "SELECT guid FROM transactions WHERE description='Orphaned'"
        ).fetchone()[0]
        for account, value in ((book.bank, 100), (new_guid(), -100)):
            conn.execute(
                "INSERT INTO splits VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_guid(), orphan_txn, account, "", "", "n", None, value, 100, value, 100, None),
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
            a.name
            for a in db.iter_accounts()
            if len(a.name) == 32 and all(c in "0123456789abcdef" for c in a.name)
        ]
        assert guidish == []

    def test_template_transactions_stay_out_of_the_register(self, db, gnucash_xml_path):
        result = gnucash_xml.import_book(db, gnucash_xml_path.path)
        assert result.transactions == 2
        assert {t.description for t in db.iter_transactions()} == {"March salary", "Electricity"}

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
            date(2026, 1, 1),
            date(2026, 2, 1),
            date(2026, 3, 1),
        ]

    def test_template_splits_resolve_to_real_accounts(self, db, gnucash_xml_path):
        """The slot indirection: the split names the template, the slot the account."""
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        schedule = db.get_scheduled(gnucash_xml_path.ids.sx)
        accounts = {split.account for split in schedule.splits}
        assert accounts == {gnucash_xml_path.ids.util, gnucash_xml_path.ids.bank}

    def test_credit_and_debit_formulas_become_signed_amounts(self, db, gnucash_xml_path):
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        schedule = db.get_scheduled(gnucash_xml_path.ids.sx)
        by_account = {s.account: s.resolve() for s in schedule.splits}
        assert by_account[gnucash_xml_path.ids.util] == Money("825.00")
        assert by_account[gnucash_xml_path.ids.bank] == Money("-825.00")

    def test_the_imported_schedule_produces_a_balanced_transaction(self, db, gnucash_xml_path):
        gnucash_xml.import_book(db, gnucash_xml_path.path)
        schedule = db.get_scheduled(gnucash_xml_path.ids.sx)
        txn = schedule.instantiate(date(2026, 4, 1))
        assert txn.is_balanced()
        assert txn.value_for(gnucash_xml_path.ids.util) == Money("825.00")

    def test_it_appears_in_the_due_list(self, db, gnucash_xml_path):
        from breadsched.gen.engine import schedule as schedule_engine

        gnucash_xml.import_book(db, gnucash_xml_path.path)
        due = schedule_engine.due_occurrences(db, as_of=date(2026, 3, 15), horizon_days=0)
        assert [o.when for o in due] == [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]

    def test_scheduled_import_can_be_skipped(self, db, gnucash_xml_path):
        result = gnucash_xml.import_book(db, gnucash_xml_path.path, include_scheduled=False)
        assert result.scheduled == 0
        assert list(db.iter_scheduled()) == []
