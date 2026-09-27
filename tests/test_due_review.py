"""Reviewed, duplicate-safe batch decisions for due and missed occurrences.

The synthetic sample book is created for March 2026 and reviewed on 2026-09-27,
so wages (5th), rent (6th), and utilities (20th) each have six missed monthly
occurrences from April to September. Its groceries Plan estimate is never due.
"""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine import schedule as schedule_engine
from breadsched.gen.lib import Money
from breadsched.gen.sample_book import create_sample_book
from breadsched.gen.services import (
    DueDecision,
    ResolveDue,
    pending_due_review,
    resolve_due,
)

AS_OF = date(2026, 9, 27)


@pytest.fixture
def missed(tmp_path):
    path = tmp_path / "missed.breadsched"
    create_sample_book(path, as_of=date(2026, 3, 15))
    db = DbSQLite()
    db.load(str(path))
    yield db
    db.close()


def _review(db, name):
    return next(item for item in pending_due_review(db, AS_OF) if item.name == name)


def _snapshot(db):
    return (
        sorted(txn.handle for txn in db.iter_transactions()),
        sorted(repr(item.serialize()) for item in db.iter_scheduled()),
    )


def test_pending_review_groups_due_occurrences_by_schedule(missed):
    reviews = pending_due_review(missed, AS_OF)

    assert [item.name for item in reviews] == ["Sample wages", "Sample rent", "Sample utilities"]
    rent = reviews[1]
    assert [item.when for item in rent.items] == [date(2026, m, 6) for m in range(4, 10)]
    assert all(item.overdue for item in rent.items)
    assert rent.total == Money("8700.00")
    assert rent.frequency == "every month"
    # The groceries Plan estimate is plan-only and never offered for posting.
    assert all("groceries" not in item.name.casefold() for item in reviews)


def test_batch_posts_skips_and_defers_atomically(missed):
    rent = _review(missed, "Sample rent")
    utilities = _review(missed, "Sample utilities")
    before = len(list(missed.iter_transactions()))
    decisions = (
        *((rent.schedule, item.when, DueDecision.POST) for item in rent.items[:2]),
        (rent.schedule, rent.items[2].when, DueDecision.SKIP),
        (utilities.schedule, utilities.items[0].when, DueDecision.DEFER),
    )

    result = resolve_due(missed, ResolveDue(decisions, as_of=AS_OF))

    assert result.ok
    assert (result.value.posted, result.value.skipped, result.value.deferred) == (2, 1, 1)
    assert len(list(missed.iter_transactions())) == before + 2
    remaining = _review(missed, "Sample rent")
    assert [item.when for item in remaining.items] == [date(2026, m, 6) for m in range(7, 10)]
    assert len(_review(missed, "Sample utilities").items) == 6
    posted = [missed.get_transaction(handle) for handle in result.value.transactions]
    assert [txn.post_date for txn in posted] == [date(2026, 4, 6), date(2026, 5, 6)]
    assert all(txn.scheduled_from == rent.schedule for txn in posted)

    # One undo step reverses the whole batch.
    assert missed.undo() is True
    assert len(list(missed.iter_transactions())) == before
    assert len(_review(missed, "Sample rent").items) == 6


def test_stale_decision_is_refused_without_writing_anything(missed):
    rent = _review(missed, "Sample rent")
    first = rent.items[0].when
    # Another window posts the first missed rent occurrence after this list was shown.
    occurrence = next(
        item
        for item in schedule_engine.due_occurrences(missed, as_of=AS_OF, horizon_days=0)
        if item.schedule.handle == rent.schedule and item.when == first
    )
    schedule_engine.post_occurrences(missed, [occurrence])
    snapshot = _snapshot(missed)

    result = resolve_due(
        missed,
        ResolveDue(
            (
                (rent.schedule, first, DueDecision.POST),
                (rent.schedule, rent.items[1].when, DueDecision.POST),
            ),
            as_of=AS_OF,
        ),
    )

    assert not result.ok
    assert result.errors[0].code == "schedule.due.not_pending"
    assert _snapshot(missed) == snapshot
    posted_on_first = [
        txn
        for txn in missed.iter_transactions(start=first, end=first)
        if txn.scheduled_from == rent.schedule
    ]
    assert len(posted_on_first) == 1


def test_duplicate_unknown_and_future_decisions_are_rejected(missed):
    rent = _review(missed, "Sample rent")
    when = rent.items[0].when
    snapshot = _snapshot(missed)

    duplicate = resolve_due(
        missed,
        ResolveDue(
            ((rent.schedule, when, DueDecision.POST), (rent.schedule, when, DueDecision.SKIP)),
            as_of=AS_OF,
        ),
    )
    unknown = resolve_due(
        missed, ResolveDue((("missing-schedule", when, DueDecision.POST),), as_of=AS_OF)
    )
    future = resolve_due(
        missed,
        ResolveDue(((rent.schedule, date(2026, 10, 6), DueDecision.POST),), as_of=AS_OF),
    )

    assert duplicate.errors[0].code == "schedule.due.duplicate"
    assert unknown.errors[0].code == "schedule.due.not_found"
    assert future.errors[0].code == "schedule.due.not_pending"
    assert _snapshot(missed) == snapshot


def test_skipped_occurrences_are_not_offered_again(missed):
    wages = _review(missed, "Sample wages")
    decisions = tuple((wages.schedule, item.when, DueDecision.SKIP) for item in wages.items)

    assert resolve_due(missed, ResolveDue(decisions, as_of=AS_OF)).ok
    assert all(item.name != "Sample wages" for item in pending_due_review(missed, AS_OF))


def test_cli_lists_and_decides_due_dates(tmp_path, capsys):
    import json

    from breadsched.cli.main import main

    path = tmp_path / "cli.breadsched"
    create_sample_book(path, as_of=date(2026, 3, 15))
    command = ["due-review", str(path), "--as-of", AS_OF.isoformat()]

    assert main(command) == 0
    text = capsys.readouterr().out
    assert "Sample rent" in text and "2026-04-06 to 2026-09-06" in text
    assert "every month" in text

    assert main([*command, "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    rent = next(item for item in listed if item["name"] == "Sample rent")
    assert len(rent["items"]) == 6 and rent["total"] == "8700.00"

    assert (
        main([*command, "--post", "Sample rent@2026-04-06", "--skip", "Sample wages", "--json"])
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"posted": 1, "skipped": 6}

    assert main([*command, "--json"]) == 0
    after = {item["name"]: item for item in json.loads(capsys.readouterr().out)}
    assert "Sample wages" not in after
    assert [item["date"] for item in after["Sample rent"]["items"]][0] == "2026-05-06"

    # A date no longer due is refused and nothing is written.
    assert main([*command, "--post", "Sample rent@2026-04-06"]) != 0
    assert main([*command, "--post-all", "--skip-all"]) != 0
