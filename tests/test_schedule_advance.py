"""Enter and remind in advance (#352), and resolving a next occurrence early (#351).

A monthly rent bill is dated the 6th from October 2026 and the book is reviewed
on 2026-10-01, five days before its first occurrence. Without advance days the
due review is empty; *enter in advance* lists it to enter (and lets an automatic
schedule post it), *remind in advance* lists it as a reminder, and the Scheduled
view can enter or skip it at any time.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest
from gnucash_xml_fixtures import create_xml_book

from breadsched.cli.main import main
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine import schedule as schedule_engine
from breadsched.gen.engine.schedule import ReviewStage, review_stage
from breadsched.gen.lib import (
    Account,
    AccountType,
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
)
from breadsched.gen.services import (
    DueDecision,
    ResolveDue,
    ResolveNext,
    SaveSchedule,
    next_occurrence,
    pending_due_review,
    resolve_due,
    resolve_next,
    save_schedule,
)
from breadsched.plugins.importer import gnucash_sqlite, gnucash_xml
from breadsched.presentation import service_error_message

TODAY = date(2026, 10, 1)
FIRST = date(2026, 10, 6)


def _rent(book, **kwargs) -> ScheduledTransaction:
    return ScheduledTransaction(
        name="Rent",
        recurrence=Recurrence(period=PeriodType.MONTH, interval=1, start=FIRST),
        splits=[
            ScheduledSplit(book.rent, Money("1800.00")),
            ScheduledSplit(book.checking, Money("-1800.00")),
        ],
        **kwargs,
    )


@pytest.fixture
def add_rent(db, book):
    def add(placeholder: bool = False, **kwargs) -> ScheduledTransaction:
        sched = _rent(book, **kwargs)
        sched.placeholder = placeholder
        with db.transaction("Add rent") as txn:
            db.add_scheduled(sched, txn)
        return sched

    return add


def _snapshot(db):
    return (
        sorted(txn.handle for txn in db.iter_transactions()),
        sorted(repr(item.serialize()) for item in db.iter_scheduled()),
    )


def test_stages_run_from_overdue_to_reminder(book):
    sched = _rent(book, advance_days=3, remind_days=10)
    assert review_stage(sched, date(2026, 9, 30), TODAY) is ReviewStage.OVERDUE
    assert review_stage(sched, TODAY, TODAY) is ReviewStage.DUE
    assert review_stage(sched, date(2026, 10, 4), TODAY) is ReviewStage.ADVANCE
    assert review_stage(sched, date(2026, 10, 5), TODAY) is ReviewStage.REMINDER


def test_without_advance_days_an_upcoming_bill_is_not_reviewed(db, add_rent):
    sched = add_rent()
    assert pending_due_review(db, TODAY) == []
    upcoming = next_occurrence(db, sched.handle, as_of=TODAY)
    assert upcoming is not None
    assert (upcoming.when, upcoming.amount, upcoming.name) == (FIRST, Money("1800.00"), "Rent")


def test_remind_in_advance_lists_a_reminder_until_decided(db, add_rent):
    sched = add_rent(remind_days=7)
    [review] = pending_due_review(db, TODAY)
    [item] = review.items
    assert (item.when, item.stage, item.overdue) == (FIRST, "reminder", False)
    # Four days earlier it is not yet within the reminder window.
    assert pending_due_review(db, date(2026, 9, 27)) == []

    deferred = resolve_due(db, ResolveDue(((sched.handle, FIRST, DueDecision.DEFER),), TODAY))
    assert deferred.ok and deferred.value.deferred == 1
    assert len(pending_due_review(db, TODAY)[0].items) == 1

    posted = resolve_due(db, ResolveDue(((sched.handle, FIRST, DueDecision.POST),), TODAY))
    assert posted.ok and posted.value.posted == 1
    [transaction] = db.iter_transactions()
    assert transaction.post_date == FIRST and transaction.scheduled_from == sched.handle
    assert pending_due_review(db, TODAY) == []


def test_enter_in_advance_lists_the_occurrence_to_enter(db, add_rent):
    add_rent(advance_days=5, remind_days=40)
    [review] = pending_due_review(db, TODAY)
    assert [(item.when, item.stage) for item in review.items] == [
        (FIRST, "advance"),
        (date(2026, 11, 6), "reminder"),
    ]


def test_an_automatic_schedule_posts_its_advance_days_ahead(db, add_rent):
    add_rent(advance_days=5, auto_create=True)
    assert schedule_engine.post_due(db, as_of=date(2026, 9, 30)) == []
    posted = schedule_engine.post_due(db, as_of=TODAY)
    assert [txn.post_date for txn in posted] == [FIRST]
    # Posting again the same day finds nothing new.
    assert schedule_engine.post_due(db, as_of=TODAY) == []


def test_a_reminder_alone_is_never_posted_automatically(db, add_rent):
    add_rent(remind_days=10, auto_create=True)
    assert schedule_engine.post_due(db, as_of=TODAY) == []


def test_entering_the_next_occurrence_early_is_one_undoable_step(db, add_rent):
    sched = add_rent()

    entered = resolve_next(db, ResolveNext(sched.handle, DueDecision.POST, FIRST, TODAY))

    assert entered.ok and entered.value.posted == 1
    [transaction] = db.iter_transactions()
    assert transaction.post_date == FIRST and transaction.scheduled_from == sched.handle
    assert db.undo_message() == "Enter next occurrence of Rent"
    assert next_occurrence(db, sched.handle, as_of=TODAY).when == date(2026, 11, 6)
    assert db.undo() is True
    assert list(db.iter_transactions()) == []
    assert next_occurrence(db, sched.handle, as_of=TODAY).when == FIRST


def test_skipping_the_next_occurrence_marks_it_done_without_posting(db, add_rent):
    sched = add_rent()

    skipped = resolve_next(db, ResolveNext(sched.handle, DueDecision.SKIP, FIRST, TODAY))

    assert skipped.ok and skipped.value.skipped == 1
    assert list(db.iter_transactions()) == []
    assert FIRST in db.get_scheduled(sched.handle).skipped
    assert next_occurrence(db, sched.handle, as_of=TODAY).when == date(2026, 11, 6)


def test_the_next_occurrence_follows_an_overdue_one_first(db, add_rent):
    sched = add_rent()
    assert next_occurrence(db, sched.handle, as_of=date(2026, 12, 1)).when == FIRST


def test_refused_next_decisions_write_nothing(db, add_rent):
    sched = add_rent()
    disabled = add_rent(enabled=False)
    estimate = add_rent(placeholder=True)
    before = _snapshot(db)

    refusals = {
        "schedule.due.not_found": ResolveNext("missing", DueDecision.POST),
        "schedule.next.decision": ResolveNext(sched.handle, DueDecision.DEFER),
        "schedule.due.not_pending": ResolveNext(
            sched.handle, DueDecision.POST, date(2026, 11, 6), TODAY
        ),
    }
    for code, request in refusals.items():
        result = resolve_next(db, request)
        assert [error.code for error in result.errors] == [code]
        assert service_error_message(result.errors[0])
    for unusable in (disabled, estimate):
        assert next_occurrence(db, unusable.handle, as_of=TODAY) is None
        result = resolve_next(db, ResolveNext(unusable.handle, DueDecision.POST, as_of=TODAY))
        assert [error.code for error in result.errors] == ["schedule.next.none"]
    assert next_occurrence(db, "missing") is None
    assert _snapshot(db) == before


@pytest.mark.parametrize(("field", "value"), [("advance_days", -1), ("remind_days", 1000)])
def test_out_of_range_advance_days_are_refused(db, add_rent, field, value):
    sched = add_rent(advance_days=2, remind_days=4)
    stored = db.get_scheduled(sched.handle).serialize()
    edited = ScheduledTransaction.from_dict(stored)
    setattr(edited, field, value)

    result = save_schedule(db, SaveSchedule(edited, sched.handle))

    assert [error.code for error in result.errors] == [f"schedule.{field}.invalid"]
    assert "0 to 999" in service_error_message(result.errors[0])
    assert db.get_scheduled(sched.handle).serialize() == stored


def test_remind_days_are_stored_only_when_set(book):
    plain = _rent(book).serialize()
    assert "remind_days" not in plain
    reminded = _rent(book, remind_days=6)
    restored = ScheduledTransaction.from_dict(reminded.serialize())
    assert restored.remind_days == 6
    assert ScheduledTransaction.from_dict(plain).remind_days == 0


def test_gnucash_sqlite_remind_days_are_imported(db, gnucash_sqlite_path):
    with sqlite3.connect(gnucash_sqlite_path.path) as conn:
        conn.execute("UPDATE schedxactions SET adv_creation=3, adv_notify=8")
    gnucash_sqlite.import_book(db, gnucash_sqlite_path.path)
    imported = db.get_scheduled(gnucash_sqlite_path.ids.sched)
    assert (imported.advance_days, imported.remind_days) == (3, 8)


def test_gnucash_xml_remind_days_are_imported(db, tmp_path):
    path = tmp_path / "remind.gnucash"
    create_xml_book(path, compress=False)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "<sx:advanceRemindDays>0</sx:advanceRemindDays>",
            "<sx:advanceRemindDays>12</sx:advanceRemindDays>",
        ),
        encoding="utf-8",
    )
    gnucash_xml.import_book(db, str(path))
    [imported] = db.iter_scheduled()
    assert (imported.advance_days, imported.remind_days) == (5, 12)


def test_cli_sets_advance_days_and_resolves_the_next_occurrence(tmp_path, capsys):

    path = tmp_path / "cli.breadsched"
    db = DbSQLite()
    db.load(str(path))
    try:
        with db.transaction("Accounts") as txn:
            root = Account(name="Root", atype=AccountType.ROOT)
            db.add_account(root, txn)
            checking = Account(name="Checking", atype=AccountType.BANK, parent=root.handle)
            rent = Account(name="Rent", atype=AccountType.EXPENSE, parent=root.handle)
            db.add_account(checking, txn)
            db.add_account(rent, txn)
            accounts = SimpleNamespace(rent=rent.handle, checking=checking.handle)
            db.add_scheduled(_rent(accounts), txn)
    finally:
        db.close()
    book_path, as_of = str(path), ["--as-of", TODAY.isoformat()]

    assert main(["scheduled-advance", book_path, "Rent", "--remind-days", "7", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["remind_days"] == 7
    assert main(["scheduled-advance", book_path, "Rent", "--enter-days", "1000"]) != 0
    capsys.readouterr()

    assert main(["due-review", book_path, *as_of, "--json"]) == 0
    [listed] = json.loads(capsys.readouterr().out)
    assert [item["stage"] for item in listed["items"]] == ["reminder"]
    # Bulk decisions leave reminders for later.
    assert main(["due-review", book_path, *as_of, "--post-all", "--json"]) == 0
    [still] = json.loads(capsys.readouterr().out)
    assert [item["date"] for item in still["items"]] == [FIRST.isoformat()]

    assert main(["scheduled-next", book_path, "Rent", *as_of, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["date"] == FIRST.isoformat()
    assert main(["scheduled-next", book_path, "Rent", *as_of, "--skip", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["skipped"] == 1
    assert main(["scheduled-next", book_path, "Rent", *as_of, "--enter"]) == 0
    assert "Entered Rent for 2026-11-06" in capsys.readouterr().out
