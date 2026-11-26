"""A schedule's versions (schema 16): each the whole transaction for some occurrences.

A version from a date, one in a calendar month every year, and one on a single
date are layered in that order over the schedule's own splits; a leg's formula may
use ``base`` (the leg below, in its own column) and ``base_total`` (the transaction
below). ``test_book_server`` covers replacing versions in process and over HTTP,
``test_web`` the browser's routes, and ``test_gui`` the desktop editor.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from breadsched.cli.main import main
from breadsched.gen.db.sqlite import SCHEMA_VERSION, DbSQLite
from breadsched.gen.lib import (
    Account,
    AccountType,
    Money,
    PeriodType,
    Recurrence,
    Scenario,
    ScenarioSchedule,
    ScheduledSplit,
    ScheduledTransaction,
    ScheduledVersion,
    VersionKind,
)
from breadsched.gen.lib.schedule_versions import (
    layered_legs,
    starting_version,
    versions_from_legacy,
)
from breadsched.gen.services import SetScheduleVersions, set_schedule_versions


def _rent(book, *, start=date(2026, 1, 1)) -> ScheduledTransaction:
    return ScheduledTransaction(
        name="Rent",
        recurrence=Recurrence(PeriodType.MONTH, start=start),
        splits=[
            ScheduledSplit(book.rent, Money("1800")),
            ScheduledSplit(book.checking, Money("-1800")),
        ],
    )


def _values(schedule, when) -> list[Money]:
    return [leg.value for leg in schedule.legs_at(when)]


def _fixed(kind, legs, **when) -> ScheduledVersion:
    return ScheduledVersion(
        kind, [ScheduledSplit(account, Money(amount)) for account, amount in legs], **when
    )


class TestLayering:
    def test_without_versions_every_occurrence_is_the_template(self, book):
        rent = _rent(book)
        assert _values(rent, date(2030, 6, 1)) == [Money("1800"), Money("-1800")]

    def test_the_latest_dated_version_in_force_wins(self, book):
        rent = _rent(book)
        rent.versions = [
            _fixed("from", [(book.rent, "1900"), (book.checking, "-1900")], start=date(2026, 6, 1)),
            _fixed("from", [(book.rent, "2000"), (book.checking, "-2000")], start=date(2027, 1, 1)),
        ]
        assert _values(rent, date(2026, 5, 1))[0] == Money("1800")
        assert _values(rent, date(2026, 6, 1))[0] == Money("1900")
        assert _values(rent, date(2027, 3, 1))[0] == Money("2000")

    def test_a_version_adds_and_drops_legs(self, book):
        rent = _rent(book)
        rent.versions = [
            _fixed(
                "from",
                [(book.rent, "1800"), (book.groceries, "50"), (book.savings, "-1850")],
                start=date(2026, 6, 1),
            )
        ]
        legs = rent.legs_at(date(2026, 7, 1))
        assert [(leg.account, leg.value) for leg in legs] == [
            (book.rent, Money("1800")),
            (book.groceries, Money("50")),
            (book.savings, Money("-1850")),
        ]
        assert rent.instantiate(date(2026, 7, 1)).is_balanced()

    def test_a_month_version_applies_on_top_of_the_dated_one(self, book):
        """Base is the leg below in its own column: base * 1.1 is 10% more of
        whatever dated version is in force, before or after it changes."""
        rent = _rent(book)
        rent.versions = [
            _fixed("from", [(book.rent, "2000"), (book.checking, "-2000")], start=date(2027, 1, 1)),
            ScheduledVersion(
                "month",
                [
                    ScheduledSplit(book.rent, formula="base * 1.1"),
                    ScheduledSplit(book.checking, formula="base * 1.1", direction=-1),
                ],
                month=12,
            ),
        ]
        assert _values(rent, date(2026, 12, 1)) == [Money("1980"), Money("-1980")]
        assert _values(rent, date(2027, 12, 1)) == [Money("2200"), Money("-2200")]
        assert _values(rent, date(2027, 11, 1)) == [Money("2000"), Money("-2000")]

    def test_a_one_date_version_is_on_top_of_both(self, book):
        rent = _rent(book)
        rent.versions = [
            ScheduledVersion.moving("month", rent.splits, Money("1700"), month=12),
            ScheduledVersion(
                "once",
                [
                    ScheduledSplit(book.rent, formula="base + 100"),
                    ScheduledSplit(book.checking, formula="base + 100", direction=-1),
                ],
                start=date(2026, 12, 1),
            ),
        ]
        assert _values(rent, date(2026, 12, 1)) == [Money("1800"), Money("-1800")]
        assert _values(rent, date(2027, 12, 1)) == [Money("1700"), Money("-1700")]

    def test_base_total_scales_the_transaction_and_a_new_leg_has_base_zero(self, book):
        rent = _rent(book)
        rent.versions = [
            ScheduledVersion(
                "from",
                [
                    ScheduledSplit(book.rent, formula="base * 1500 / base_total"),
                    ScheduledSplit(book.groceries, formula="base + 25"),
                    ScheduledSplit(
                        book.checking, formula="base * 1500 / base_total + 25", direction=-1
                    ),
                ],
                start=date(2026, 3, 1),
            )
        ]
        assert _values(rent, date(2026, 3, 1)) == [Money("1500"), Money("25"), Money("-1525")]

    def test_a_zero_transaction_below_keeps_each_legs_base(self, book):
        """Nothing to scale: a formula over base_total keeps the leg as it was."""
        rent = _rent(book)
        rent.versions = [
            _fixed("from", [(book.rent, "0"), (book.checking, "0")], start=date(2026, 3, 1)),
            ScheduledVersion.moving("month", rent.splits, Money("900"), month=4),
        ]
        assert _values(rent, date(2026, 4, 1)) == [Money("0"), Money("0")]

    def test_layering_the_template_alone_without_a_date(self, book):
        rent = _rent(book)
        legs = layered_legs(rent.splits, [_fixed("from", [], start=date(2026, 1, 1))], {}, None)
        assert [leg.value for leg in legs] == [Money("1800"), Money("-1800")]


class TestStartingAVersion:
    """Every editor starts a new version as the schedule then is."""

    def _with_raise(self, book) -> ScheduledTransaction:
        rent = _rent(book)
        rent.versions = [
            _fixed("from", [(book.rent, "1900"), (book.checking, "-1900")], start=date(2026, 6, 1))
        ]
        return rent

    def test_from_a_date_copies_the_legs_in_force_the_day_before(self, book):
        rent = self._with_raise(book)
        version = starting_version(rent, VersionKind.FROM, start=date(2026, 6, 1))
        # The day before June 1 the raise was not yet in force.
        assert [leg.amount for leg in version.splits] == [Money("1800"), Money("-1800")]
        later = starting_version(rent, VersionKind.FROM, start=date(2027, 1, 1))
        assert [leg.amount for leg in later.splits] == [Money("1900"), Money("-1900")]

    def test_a_month_starts_as_every_leg_base_times_one(self, book):
        version = starting_version(self._with_raise(book), VersionKind.MONTH, month=12)
        assert (version.kind, version.month) == (VersionKind.MONTH, 12)
        assert [(leg.formula, leg.direction) for leg in version.splits] == [
            ("base * 1.0", 1),
            ("base * 1.0", -1),
        ]

    def test_one_date_starts_as_that_occurrences_amounts(self, book):
        rent = self._with_raise(book)
        rent.versions.append(ScheduledVersion.moving("month", rent.splits, Money("1700"), month=7))
        version = starting_version(rent, VersionKind.ONCE, start=date(2026, 7, 1))
        assert [(leg.amount, leg.formula) for leg in version.splits] == [
            (Money("1700"), ""),
            (Money("-1700"), ""),
        ]

    def test_a_dated_version_needs_its_date_and_a_month_its_month(self, book):
        with pytest.raises(ValueError):
            starting_version(_rent(book), VersionKind.FROM)
        with pytest.raises(ValueError):
            starting_version(_rent(book), VersionKind.MONTH)


class TestTheService:
    def test_versions_are_saved_and_a_refusal_keeps_the_schedule(self, db, book):
        rent = _rent(book)
        with db.transaction("Rent") as txn:
            db.add_scheduled(rent, txn)
        raised = _fixed(
            "from", [(book.rent, "1900"), (book.checking, "-1900")], start=date(2026, 6, 1)
        )
        assert set_schedule_versions(db, SetScheduleVersions(rent.handle, (raised,))).ok
        assert db.get_scheduled(rent.handle).versions == [raised]

        before = db.get_scheduled(rent.handle).serialize()
        cases = {
            "schedule.versions.before_start": _fixed(
                "from", [(book.rent, "1"), (book.checking, "-1")], start=date(2025, 1, 1)
            ),
            "schedule.versions.not_occurrence": _fixed(
                "once", [(book.rent, "1"), (book.checking, "-1")], start=date(2026, 6, 2)
            ),
            "schedule.versions.unbalanced": _fixed(
                "from", [(book.rent, "1"), (book.checking, "-2")], start=date(2026, 9, 1)
            ),
            "schedule.versions.too_few": _fixed("month", [(book.rent, "1")], month=3),
            "schedule.versions.formula": ScheduledVersion(
                "month",
                [
                    ScheduledSplit(book.rent, formula="nonsense + 1"),
                    ScheduledSplit(book.checking, formula="base", direction=-1),
                ],
                month=3,
            ),
        }
        for code, version in cases.items():
            refused = set_schedule_versions(db, SetScheduleVersions(rent.handle, (version,)))
            assert not refused.ok, code
            assert code in {error.code for error in refused.errors}
            assert db.get_scheduled(rent.handle).serialize() == before


class TestSchema16:
    def test_legacy_changes_keep_their_amounts(self, book):
        splits = [
            {"account": book.rent, "amount": [1800, 1]},
            {"account": book.checking, "amount": [-1800, 1]},
        ]
        splits[0]["amount_changes"] = [{"start": "2026-06-01", "amount": "1900"}]
        splits[1]["amount_changes"] = [{"start": "2026-06-01", "amount": "-1900"}]
        versions = versions_from_legacy(
            splits,
            amount_changes=[{"start": "2027-01-01", "amount": "2000"}],
            seasonal_amounts=[{"month": 12, "amount": "2500"}],
            occurrence_adjustments=[{"when": "2026-08-01", "amount": "2100"}],
        )
        rent = _rent(book)
        rent.versions = versions
        assert [version.label() for version in versions] == [
            "from 2026-06-01",
            "from 2027-01-01",
            "every December",
            "on 2026-08-01 only",
        ]
        for when, expected in (
            (date(2026, 5, 1), "1800"),
            (date(2026, 6, 1), "1900"),
            (date(2026, 8, 1), "2100"),
            (date(2026, 12, 1), "2500"),
            (date(2027, 3, 1), "2000"),
            # Layered: December's version now also applies after the 2027 change.
            (date(2027, 12, 1), "2500"),
        ):
            assert _values(rent, when) == [Money(expected), -Money(expected)], when

    def test_a_schema_15_book_is_brought_forward_with_a_backup(self, tmp_path):
        path = tmp_path / "older.breadsched"
        db = DbSQLite()
        db.load(str(path))
        accounts = {}
        with db.transaction("Accounts") as txn:
            root = Account(name="Root", atype=AccountType.ROOT)
            db.add_account(root, txn)
            for name, atype in (("Rent", AccountType.EXPENSE), ("Checking", AccountType.BANK)):
                account = Account(name=name, atype=atype, parent=root.handle)
                db.add_account(account, txn)
                accounts[name] = account.handle
        rent = ScheduledTransaction(
            name="Rent",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
            splits=[
                ScheduledSplit(accounts["Rent"], Money("1800")),
                ScheduledSplit(accounts["Checking"], Money("-1800")),
            ],
        )
        scenario = Scenario(name="Move", schedule_overrides=[ScenarioSchedule.from_scheduled(rent)])
        with db.transaction("Fixtures") as txn:
            db.add_scheduled(rent, txn)
            db.add_scenario(scenario, txn)
        db.close()

        legacy = {
            "amount_changes": [{"start": "2027-01-01", "amount": "2000"}],
            "seasonal_amounts": [{"month": 12, "amount": "2500"}],
        }
        with sqlite3.connect(path) as raw:
            [(blob,)] = raw.execute("SELECT blob FROM scheduled").fetchall()
            data = {**json.loads(blob), **legacy}
            data.pop("versions", None)
            raw.execute("UPDATE scheduled SET blob=?", (json.dumps(data),))
            [(blob,)] = raw.execute("SELECT blob FROM scenario").fetchall()
            stored = json.loads(blob)
            for item in stored["schedule_overrides"]:
                item.pop("versions", None)
                item.update(legacy)
            raw.execute("UPDATE scenario SET blob=?", (json.dumps(stored),))
            raw.execute("UPDATE metadata SET value=? WHERE key='schema_version'", (json.dumps(15),))
            raw.execute("DELETE FROM schema_migration")
            raw.execute("INSERT INTO schema_migration(version, applied_at) VALUES (15, 'old')")

        db = DbSQLite()
        db.load(str(path))
        try:
            assert db.get_metadata("schema_version") == SCHEMA_VERSION == 16
            [migrated] = db.iter_scheduled()
            [override] = db.get_scenario(scenario.handle).schedule_overrides
            for schedule in (migrated, override):
                assert [version.label() for version in schedule.versions] == [
                    "from 2027-01-01",
                    "every December",
                ]
                assert _values(schedule, date(2026, 12, 1))[0] == Money("2500")
                assert _values(schedule, date(2027, 3, 1))[0] == Money("2000")
            assert db.integrity_problems() == []
        finally:
            db.close()
        assert Path(f"{path}.pre-migration-v15.bak").exists()
        with sqlite3.connect(path) as raw:
            [(blob,)] = raw.execute("SELECT blob FROM scheduled").fetchall()
        assert not {"amount_changes", "seasonal_amounts"} & set(json.loads(blob))


def test_the_command_line_lists_adds_and_removes_versions(tmp_path, capsys):
    path = tmp_path / "book.breadsched"
    assert main(["init", str(path)]) == 0
    db = DbSQLite()
    db.load(str(path))
    accounts = {db.full_name(a): a.handle for a in db.iter_accounts()}
    with db.transaction("Rent") as txn:
        db.add_scheduled(
            ScheduledTransaction(
                name="Rent",
                recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 1)),
                splits=[
                    ScheduledSplit(accounts["Expenses"], Money("1800")),
                    ScheduledSplit(accounts["Assets"], Money("-1800")),
                ],
            ),
            txn,
        )
    db.close()
    capsys.readouterr()

    def run(*argv) -> dict:
        assert main(["scheduled-versions", str(path), "Rent", *argv, "--json"]) == 0
        return json.loads(capsys.readouterr().out)

    assert run()["versions"] == []
    # Without legs, a month's version starts as every leg base * 1.0.
    december = run("--add", "month:dec")["versions"]
    assert [(leg["formula"], leg.get("direction", 1)) for leg in december[0]["splits"]] == [
        ("base * 1.0", 1),
        ("base * 1.0", -1),
    ]
    added = run(
        "--add",
        "from:2027-01-01",
        "--leg",
        "Expenses=base * 1.1",
        "--leg",
        "Assets=-base * 1.1",
    )["versions"]
    assert [item["label"] for item in added] == ["from 2027-01-01", "every December"]
    assert added[0]["account_names"] == ["Expenses", "Assets"]
    assert run("--remove", "month:12")["versions"][0]["label"] == "from 2027-01-01"
    assert main(["scheduled-versions", str(path), "Rent"]) == 0
    text = capsys.readouterr().out
    assert "from 2027-01-01" in text and "base * 1.1  (decrease)" in text
    # Refusals: an unbalanced version, a missing version, and legs without --add.
    assert (
        main(
            [
                "scheduled-versions",
                str(path),
                "Rent",
                "--add",
                "once:2026-03-01",
                "--leg",
                "Expenses=10",
                "--leg",
                "Assets=-20",
            ]
        )
        != 0
    )
    assert main(["scheduled-versions", str(path), "Rent", "--remove", "once:2026-03-01"]) != 0
    assert main(["scheduled-versions", str(path), "Rent", "--leg", "Expenses=1"]) != 0
    assert main(["scheduled-versions", str(path), "Rent", "--add", "weekly:3"]) != 0
