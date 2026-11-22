"""A schedule's number, notes, tags, and documents (schema 15).

Each transaction a schedule posts carries them, as it carries the schedule's
splits; a transaction made scheduled takes them with it. The book server, the
command line, and the browser set them (``test_book_server`` covers the book API
in process and over HTTP).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from breadsched.cli.main import main
from breadsched.gen.db.sqlite import SCHEMA_VERSION, DbSQLite
from breadsched.gen.engine import schedule as schedule_engine
from breadsched.gen.lib import (
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    Split,
    Transaction,
)
from breadsched.gen.services import (
    FixedScheduleInput,
    SaveFixedSchedule,
    save_fixed_schedule,
)


def _rent(book) -> ScheduledTransaction:
    return ScheduledTransaction(
        name="Rent",
        recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 10, 6)),
        splits=[
            ScheduledSplit(book.rent, Money("1800")),
            ScheduledSplit(book.checking, Money("-1800")),
        ],
    )


class TestTheStoredForm:
    def test_a_schedule_without_them_stores_exactly_what_it_did(self, book):
        stored = _rent(book).serialize()
        for key in ("num", "notes", "attachments"):
            assert key not in stored
        # Tags, which every stored object has, were stored before schema 15 too.
        assert stored["tags"] == []

    def test_they_survive_a_round_trip(self, book):
        rent = _rent(book)
        rent.num, rent.notes = "ACH", "Lease to June"
        rent.tags, rent.attachments = ["Home"], ["lease.pdf"]
        again = ScheduledTransaction.from_dict(rent.serialize())
        assert (again.num, again.notes, again.tags, again.attachments) == (
            "ACH",
            "Lease to June",
            ["Home"],
            ["lease.pdf"],
        )


class TestPosting:
    def test_each_posting_carries_them(self, book):
        rent = _rent(book)
        rent.num, rent.notes = "ACH", "Lease to June"
        rent.tags, rent.attachments = ["Home"], ["lease.pdf", "https://example.com/lease"]
        posted = rent.instantiate(date(2026, 11, 6))
        assert (posted.num, posted.notes, posted.tags) == ("ACH", "Lease to June", ["Home"])
        assert posted.attachments == ["lease.pdf", "https://example.com/lease"]
        # Its own lists, not the schedule's.
        posted.tags.append("Extra")
        assert rent.tags == ["Home"]

    def test_a_transaction_made_scheduled_takes_them(self, book):
        paid = Transaction(post_date=date(2026, 9, 6), description="Rent", num="1042")
        paid.notes = "Lease"
        paid.tags = ["Home"]
        paid.attachments = ["lease.pdf"]
        paid.splits = [Split(book.rent, Money(1800)), Split(book.checking, Money(-1800))]
        draft = schedule_engine.from_transaction(paid)
        assert (draft.num, draft.notes, draft.tags, draft.attachments) == (
            "1042",
            "Lease",
            ["Home"],
            ["lease.pdf"],
        )


class TestSaving:
    def _input(self, book, **values) -> FixedScheduleInput:
        return FixedScheduleInput(
            name="Rent",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 10, 6)),
            category=book.rent,
            funding=book.checking,
            amount=Money("1800"),
            **values,
        )

    def test_tags_are_tidied_and_left_out_fields_keep_the_stored_ones(self, db, book):
        saved = save_fixed_schedule(
            db,
            SaveFixedSchedule(
                self._input(book, num=" ACH ", notes="Lease", tags=("Home", " home", ""))
            ),
        )
        assert saved.ok, saved.errors
        stored = db.get_scheduled(saved.value.handle)
        assert (stored.num, stored.notes, stored.tags) == ("ACH", "Lease", ["Home"])
        again = save_fixed_schedule(
            db,
            SaveFixedSchedule(self._input(book, notes="Renewed"), existing_handle=stored.handle),
        )
        assert again.ok
        stored = db.get_scheduled(stored.handle)
        assert (stored.num, stored.notes, stored.tags) == ("ACH", "Renewed", ["Home"])

    def test_a_refused_tag_saves_nothing(self, db, book):
        refused = save_fixed_schedule(db, SaveFixedSchedule(self._input(book, tags=("a,b",))))
        assert not refused.ok
        assert refused.errors[0].code == "tag.invalid"
        assert list(db.iter_scheduled()) == []


def test_a_schema_14_book_is_brought_forward_with_a_backup(tmp_path):
    path = tmp_path / "older.breadsched"
    db = DbSQLite()
    db.load(str(path))
    db.close()
    with sqlite3.connect(path) as raw:
        raw.execute("UPDATE metadata SET value=? WHERE key='schema_version'", (json.dumps(14),))
        raw.execute("DELETE FROM schema_migration")
        raw.execute("INSERT INTO schema_migration(version, applied_at) VALUES (14, 'old')")
    db = DbSQLite()
    db.load(str(path))
    try:
        assert db.get_metadata("schema_version") == SCHEMA_VERSION == 15
    finally:
        db.close()
    assert Path(f"{path}.pre-migration-v14.bak").exists()


def test_the_command_line_sets_them_and_links_a_schedules_documents(tmp_path, capsys):
    path = tmp_path / "book.breadsched"
    assert main(["init", str(path)]) == 0
    db = DbSQLite()
    db.load(str(path))
    accounts = {db.full_name(a): a.handle for a in db.iter_accounts()}
    with db.transaction("Rent") as txn:
        db.add_scheduled(
            ScheduledTransaction(
                name="Rent",
                recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 10, 6)),
                splits=[
                    ScheduledSplit(accounts["Expenses"], Money("1800")),
                    ScheduledSplit(accounts["Assets"], Money("-1800")),
                ],
            ),
            txn,
        )
    db.close()
    capsys.readouterr()
    assert (
        main(
            [
                "scheduled-details",
                str(path),
                "Rent",
                "--num",
                "ACH",
                "--tags",
                "Home, Lease",
                "--notes",
                "To June",
                "--json",
            ]
        )
        == 0
    )
    shown = json.loads(capsys.readouterr().out)
    assert (shown["num"], shown["tags"], shown["notes"]) == ("ACH", ["Home", "Lease"], "To June")
    assert (
        main(
            [
                "attachments",
                str(path),
                "--schedule",
                "Rent",
                "--add",
                "https://example.com/lease",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["attachments", str(path), "--schedule", "Rent", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)["attachments"]
    assert [(item["location"], item["holder"]) for item in listed] == [
        ("https://example.com/lease", "schedule")
    ]
    # A schedule's or a transaction's, never both at once.
    assert main(["attachments", str(path), "--schedule", "Rent", "--transaction", "x"]) != 0


class TestSplitsTypedAsLedgerAmounts:
    """The desktop types a schedule's splits on the split grid: signed amounts."""

    def _input(self, book, *splits) -> FixedScheduleInput:
        from breadsched.gen.lib.amount import Amount
        from breadsched.gen.services import TransactionSplitInput

        return FixedScheduleInput(
            name="Pay split three ways",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 10, 25)),
            ledger_splits=tuple(
                TransactionSplitInput(account, Amount(Money(value), "USD"), memo=memo)
                for account, value, memo in splits
            ),
        )

    def test_they_are_the_schedules_splits_in_order(self, db, book):
        saved = save_fixed_schedule(
            db,
            SaveFixedSchedule(
                self._input(
                    book,
                    (book.checking, "700", "To checking"),
                    (book.savings, "300", ""),
                    (book.salary, "-1000", ""),
                )
            ),
        )
        assert saved.ok, saved.errors
        stored = db.get_scheduled(saved.value.handle)
        assert [(s.account, s.amount, s.memo) for s in stored.splits] == [
            (book.checking, Money(700), "To checking"),
            (book.savings, Money(300), ""),
            (book.salary, Money(-1000), ""),
        ]
        assert stored.instantiate(date(2026, 10, 25)).is_balanced()

    def test_unbalanced_or_zero_splits_are_refused(self, db, book):
        for splits, code in (
            (((book.checking, "700", ""), (book.salary, "-600", "")), "schedule.splits.unbalanced"),
            (((book.checking, "0", ""), (book.salary, "0", "")), "schedule.amount.zero"),
            (((book.checking, "5", ""),), "schedule.splits.too_few"),
        ):
            refused = save_fixed_schedule(db, SaveFixedSchedule(self._input(book, *splits)))
            assert not refused.ok and code in {error.code for error in refused.errors}
        assert list(db.iter_scheduled()) == []

    def test_they_travel_over_http(self, book):
        from breadsched.api.schedule_codec import fixed_request_from_json, fixed_request_json

        request = SaveFixedSchedule(
            self._input(book, (book.checking, "-40", "x"), (book.rent, "40", ""))
        )
        again = fixed_request_from_json(fixed_request_json(request))
        assert again.definition.ledger_splits == request.definition.ledger_splits
