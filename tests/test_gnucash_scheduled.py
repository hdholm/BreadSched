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

import sqlite3
from datetime import date

import pytest
from gnucash_fixtures import create_book, new_guid, write_transaction
from gnucash_xml_fixtures import create_xml_book

from breadsched.cli.main import main as cli
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine import ledger, planning, schedule
from breadsched.gen.lib import Money
from breadsched.gen.lib.transaction import PlanningResolution
from breadsched.plugins.importer import gnucash_sqlite, gnucash_xml


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
            "Root Account",
            "Assets",
            "Checking",
            "Expenses",
            "Rent",
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
        assert ledger.balance_amount(db, db.get_account_by_name("Checking").handle).value == Money(
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
            date(2026, 1, 1),
            date(2026, 2, 1),
            date(2026, 3, 1),
        ]

    def test_its_flags_are_read(self, db, xml_book):
        gnucash_xml.import_book(db, xml_book.path)
        sched = next(iter(db.iter_scheduled()))
        assert sched.enabled is True
        assert sched.auto_create is True
        assert sched.advance_days == 5

    def test_inactive_import_and_source_state_changes(self, db, tmp_path):
        source = create_xml_book(tmp_path / "source.gnucash", compress=False)
        path = tmp_path / "source.gnucash"
        path.write_text(source.body.replace("<sx:enabled>y", "<sx:enabled>n"))
        gnucash_xml.import_book(db, path)
        assert db.get_scheduled(source.schedule).enabled is False
        assert schedule.due_occurrences(db, as_of=date(2026, 3, 15)) == []
        assert schedule.forecast_occurrences(db, date(2026, 3, 1), date(2026, 4, 30)) == []

        path.write_text(source.body)
        gnucash_xml.import_book(db, path)
        assert db.get_scheduled(source.schedule).enabled is True
        path.write_text(source.body.replace("<sx:enabled>y", "<sx:enabled>n"))
        gnucash_xml.import_book(db, path)
        assert db.get_scheduled(source.schedule).enabled is False
        assert len(list(db.iter_scheduled())) == 1

    def test_a_once_recurrence_accepts_gnucashs_zero_multiplier(self, db, tmp_path):
        source = create_xml_book(tmp_path / "one-time.gnucash", compress=False)
        body = source.body.replace(
            "<recurrence:mult>1</recurrence:mult>",
            "<recurrence:mult>0</recurrence:mult>",
        ).replace(
            "<recurrence:period_type>month</recurrence:period_type>",
            "<recurrence:period_type>once</recurrence:period_type>",
        )
        (tmp_path / "one-time.gnucash").write_text(body)

        result = gnucash_xml.import_book(db, tmp_path / "one-time.gnucash")
        imported = db.get_scheduled(source.schedule)

        assert result.scheduled == 1
        assert result.skipped == 0
        assert imported is not None
        assert imported.recurrence.period.value == "once"
        assert imported.recurrence.interval == 1
        assert imported.recurrence.occurrences(date(2027, 1, 1)) == [date(2026, 1, 1)]

    def test_an_unknown_recurrence_is_preserved_but_does_not_fire(self, db, tmp_path):
        source = create_xml_book(tmp_path / "custom.gnucash", compress=False)
        path = tmp_path / "custom.gnucash"
        path.write_text(
            source.body.replace(
                "<recurrence:period_type>month</recurrence:period_type>",
                "<recurrence:period_type>custom-cycle</recurrence:period_type>",
            )
        )

        result = gnucash_xml.import_book(db, path)
        imported = db.get_scheduled(source.schedule)

        assert imported is not None
        assert imported.source_recurrence is not None
        assert "custom-cycle" in str(imported.source_recurrence)
        assert imported.usable is False
        assert schedule.forecast_occurrences(db, date(2026, 1, 1), date(2027, 1, 1)) == []
        assert any("excluded from planning and posting" in item for item in result.warnings)

    def test_template_splits_resolve_to_real_accounts(self, db, xml_book):
        """The slot indirection: the template split names the real account."""
        gnucash_xml.import_book(db, xml_book.path)
        sched = next(iter(db.iter_scheduled()))
        assert {db.full_name(s.account) for s in sched.splits} == {
            "Expenses:Rent",
            "Assets:Checking",
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
        assert [o.when for o in due] == [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]

    def test_scheduled_import_can_still_be_skipped(self, db, xml_book):
        gnucash_xml.import_book(db, xml_book.path, include_scheduled=False)
        assert list(db.iter_scheduled()) == []

    def test_an_uncompressed_book_behaves_identically(self, db, tmp_path):
        plain = create_xml_book(tmp_path / "plain.gnucash", compress=False)
        result = gnucash_xml.import_book(db, plain.path)
        assert result.scheduled == 1
        assert result.transactions == 1


@pytest.mark.parametrize("inactive", [0, "n", "false"])
def test_inactive_sqlite_schedule_survives_reimport(db, gnucash_sqlite_path, inactive):
    with sqlite3.connect(gnucash_sqlite_path.path) as conn:
        conn.execute("UPDATE schedxactions SET enabled=?", (inactive,))
    gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    sched = next(iter(db.iter_scheduled()))
    assert sched.enabled is False
    assert schedule.due_occurrences(db, as_of=date(2026, 3, 15)) == []
    assert schedule.forecast_occurrences(db, date(2026, 3, 1), date(2026, 4, 30)) == []

    with sqlite3.connect(gnucash_sqlite_path.path) as conn:
        conn.execute("UPDATE schedxactions SET enabled=1")
    gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    assert db.get_scheduled(sched.handle).enabled is True
    with sqlite3.connect(gnucash_sqlite_path.path) as conn:
        conn.execute("UPDATE schedxactions SET enabled=?", (inactive,))
    gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    assert db.get_scheduled(sched.handle).enabled is False
    assert len(list(db.iter_scheduled())) == 1


def test_sqlite_once_recurrence_accepts_gnucashs_zero_multiplier(db, gnucash_sqlite_path):
    with sqlite3.connect(gnucash_sqlite_path.path) as conn:
        conn.execute("UPDATE recurrences SET recurrence_mult=0, recurrence_period_type='once'")

    result = gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    imported = db.get_scheduled(gnucash_sqlite_path.ids.sched)

    assert result.scheduled == 1
    assert result.skipped == 0
    assert imported is not None
    assert imported.recurrence.period.value == "once"
    assert imported.recurrence.interval == 1
    assert imported.recurrence.occurrences(date(2027, 1, 1)) == [date(2026, 1, 1)]


def test_sqlite_unknown_recurrence_is_preserved_but_does_not_fire(db, gnucash_sqlite_path):
    with sqlite3.connect(gnucash_sqlite_path.path) as conn:
        conn.execute("UPDATE recurrences SET recurrence_period_type='custom-cycle'")

    gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    imported = db.get_scheduled(gnucash_sqlite_path.ids.sched)

    assert imported is not None
    assert imported.source_recurrence["recurrence_period_type"] == "custom-cycle"
    assert imported.usable is False
    assert schedule.due_occurrences(db, as_of=date(2027, 1, 1)) == []


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
                (
                    date(2026, 1, 25),
                    "Payroll",
                    [("bank", 420000, 100, ""), ("wages", -420000, 100, "")],
                ),
                # Posted straight to a top-level account that will be merged.
                (
                    date(2026, 1, 26),
                    "Direct to Assets",
                    [("assets", 5000, 100, ""), ("wages", -5000, 100, "")],
                ),
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

    def test_imported_accounts_hang_off_the_existing_tree(self, initialised, source, capsys):
        cli(["import", str(initialised), source.path])
        capsys.readouterr()
        db = DbSQLite()
        db.load(str(initialised))
        assert db.get_account_by_name("Assets:Checking") is not None
        assert db.get_account_by_name("Income:Salary") is not None
        db.close()

    def test_the_tree_shows_balances_from_the_single_root(self, initialised, source, capsys):
        """The reported symptom: top-level accounts all reading zero."""
        cli(["import", str(initialised), source.path])
        capsys.readouterr()
        db = DbSQLite()
        db.load(str(initialised))
        root = db.root_account()
        assets = next(a for a in db.child_accounts(root.handle) if a.name == "Assets")
        assert ledger.balance_recursive_amount(db, assets.handle).value == Money("4250.00")
        db.close()

    def test_a_split_posted_to_a_merged_account_is_not_lost(self, initialised, source, capsys):
        """A merged account keeps the source GUID in its splits; it must be remapped."""
        cli(["import", str(initialised), source.path])
        capsys.readouterr()
        db = DbSQLite()
        db.load(str(initialised))
        assets = db.get_account_by_name("Assets")
        assert ledger.balance_amount(db, assets.handle).value == Money("50.00")
        db.close()

    def test_nothing_is_skipped_by_the_merge(self, initialised, source, capsys):
        import json

        cli(["import", str(initialised), source.path, "--json"])
        report = json.loads(capsys.readouterr().out)
        assert report["skipped"] == 0
        assert report["transactions"] == 2


# ------------------------------------------- occurrences GnuCash already created


def _run_by_gnucash(path: str, ids, *dates: date) -> list[str]:
    """Record that GnuCash's Since Last Run created the schedule's ``dates``, as it
    does: a transaction for each, named on a ``from-sched-xaction`` slot, and the
    last one as the schedule's last occurrence."""
    created = []
    with sqlite3.connect(path) as conn:
        for when in dates:
            guid = write_transaction(
                conn,
                new_guid(),
                ids.currency,
                when,
                "Rent",
                [(ids.rent, 180000, 100, ""), (ids.checking, -180000, 100, "")],
            )
            conn.execute(
                "INSERT INTO slots (obj_guid,name,slot_type,guid_val) VALUES (?,?,?,?)",
                (guid, "from-sched-xaction", 10, ids.sched),
            )
            created.append(guid)
        conn.execute(
            "UPDATE schedxactions SET last_occur=?, instance_count=?",
            (dates[-1].strftime("%Y%m%d"), len(dates)),
        )
    return created


def _shows_them_as_done(db, sched: str, created: list[str]) -> None:
    today = date(2026, 3, 15)
    assert db.get_scheduled(sched).last_posted == date(2026, 3, 1)
    for handle in created:
        assert db.get_transaction(handle).scheduled_from == sched
    assert schedule.already_posted(db, sched, date(2026, 2, 1))
    # Nothing GnuCash already entered is due, upcoming, or on the due review again.
    assert schedule.due_occurrences(db, as_of=today) == []
    assert schedule.review_occurrences(db, as_of=today) == []
    assert [item.when for item in schedule.upcoming_occurrences(db, as_of=today)] == [
        date(2026, 4, 1)
    ]
    # The Plan shows each as matched by the transaction GnuCash entered for it.
    events = {
        event.planned_date: event.actual_transaction
        for event in planning.scheduled_events(db, date(2026, 2, 1), date(2026, 3, 31))
    }
    assert events == {date(2026, 2, 1): created[0], date(2026, 3, 1): created[1]}


def test_sqlite_occurrences_gnucash_created_are_not_due_again(db, gnucash_sqlite_path):
    """GnuCash had entered the past occurrences, yet each still showed as upcoming
    and the Plan as unmatched: the SQLite importer read neither the schedule's last
    occurrence nor the link on the transactions GnuCash created from it."""
    ids = gnucash_sqlite_path.ids
    created = _run_by_gnucash(gnucash_sqlite_path.path, ids, date(2026, 2, 1), date(2026, 3, 1))
    gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    _shows_them_as_done(db, ids.sched, created)
    # Created transactions remain settled history, never queued for Review.
    assert all(
        db.get_transaction(handle).planning_resolution is PlanningResolution.HISTORICAL
        for handle in created
    )


def test_importing_again_repairs_a_book_imported_before(db, gnucash_sqlite_path):
    """A book imported without the links gains them when imported again."""
    ids = gnucash_sqlite_path.ids
    gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    created = _run_by_gnucash(gnucash_sqlite_path.path, ids, date(2026, 2, 1), date(2026, 3, 1))
    with sqlite3.connect(gnucash_sqlite_path.path) as conn:
        conn.execute("UPDATE schedxactions SET last_occur=NULL")
        conn.execute("UPDATE slots SET name='unrelated' WHERE name='from-sched-xaction'")
    gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    assert db.get_transaction(created[0]).scheduled_from is None
    assert len(schedule.due_occurrences(db, as_of=date(2026, 3, 15))) == 3

    with sqlite3.connect(gnucash_sqlite_path.path) as conn:
        conn.execute("UPDATE schedxactions SET last_occur='20260301'")
        conn.execute("UPDATE slots SET name='from-sched-xaction' WHERE name='unrelated'")
    gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    _shows_them_as_done(db, ids.sched, created)


def test_a_link_made_in_breadsched_is_kept_on_import(db, gnucash_sqlite_path):
    ids = gnucash_sqlite_path.ids
    [handle] = _run_by_gnucash(gnucash_sqlite_path.path, ids, date(2026, 2, 1))
    gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    other = new_guid()
    with db.transaction("link elsewhere") as txn:
        linked = db.get_transaction(handle)
        linked.scheduled_from = other
        db.commit_transaction(linked, txn)
    gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    assert db.get_transaction(handle).scheduled_from == other


def test_xml_transactions_gnucash_created_from_a_schedule_are_linked(db, tmp_path, xml_book):
    body = xml_book.body.replace(
        "<trn:date-posted><ts:date>2026-01-02 10:59:00 +0000</ts:date></trn:date-posted>",
        "<trn:date-posted><ts:date>2026-01-01 10:59:00 +0000</ts:date></trn:date-posted>"
        "<trn:slots><slot><slot:key>from-sched-xaction</slot:key>"
        f'<slot:value type="guid">{xml_book.schedule}</slot:value></slot></trn:slots>',
        1,
    ).replace("<sx:start>", "<sx:last><gdate>2026-01-01</gdate></sx:last>\n    <sx:start>", 1)
    path = tmp_path / "run.gnucash"
    path.write_text(body, encoding="utf-8")
    gnucash_xml.import_book(db, str(path))

    assert db.get_transaction(xml_book.txn).scheduled_from == xml_book.schedule
    assert db.get_scheduled(xml_book.schedule).last_posted == date(2026, 1, 1)
    [event] = planning.scheduled_events(db, date(2026, 1, 1), date(2026, 1, 31))
    assert event.actual_transaction == xml_book.txn
    assert [item.when for item in schedule.due_occurrences(db, as_of=date(2026, 1, 20))] == []
