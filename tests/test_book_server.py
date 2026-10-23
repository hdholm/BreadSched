"""The book server: one API over an open book for every interface (#354-#356).

**Experimental** (branch ``claude/experimental-book-server``). The in-process
server implements the book API over the shared services, observes every change to
its book, reports failures in one error envelope, and gives the command line and
the browser the same JSON.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum

import pytest

from breadsched.api import ApiError, Change, LocalBookServer, open_book_server, to_json
from breadsched.api.views import change_json
from breadsched.cli.main import main
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.lib import (
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)
from breadsched.gen.sample_book import create_sample_book
from breadsched.gen.services import (
    DueDecision,
    ResolveDue,
    ResolveNext,
    ServiceError,
    SetAdvanceDays,
)
from breadsched.web.server import serve

TODAY = date(2026, 10, 1)


@pytest.fixture
def server(db, book):
    sched = ScheduledTransaction(
        name="Rent",
        recurrence=Recurrence(period=PeriodType.MONTH, interval=1, start=date(2026, 10, 6)),
        splits=[
            ScheduledSplit(book.rent, Money("1800.00")),
            ScheduledSplit(book.checking, Money("-1800.00")),
        ],
    )
    with db.transaction("Add rent") as txn:
        db.add_scheduled(sched, txn)
    served = LocalBookServer(db)
    yield served
    served.close()


def _changes(server) -> list[Change]:
    seen: list[Change] = []
    server.subscribe(seen.append)
    return seen


def test_a_write_through_the_server_is_one_change_every_subscriber_sees(server):
    seen = _changes(server)
    [sched] = server.db.iter_scheduled()

    outcome = server.resolve_next(ResolveNext(sched.handle, DueDecision.POST, as_of=TODAY))

    assert outcome.posted == 1
    assert server.revision == 1
    [change] = seen
    assert change.revision == 1
    assert {"transaction", "scheduled"} <= change.kinds
    assert change.label == "Enter next occurrence of Rent"
    assert change_json(change)["kinds"] == sorted(change.kinds)


def test_undo_redo_and_writes_from_other_code_are_changes_too(server, book):
    seen = _changes(server)
    [sched] = server.db.iter_scheduled()
    server.resolve_next(ResolveNext(sched.handle, DueDecision.SKIP, as_of=TODAY))
    assert server.undo() is True
    assert server.redo() is True
    with server.db.transaction("Elsewhere") as txn:
        server.db.add_transaction(
            Transaction.simple(TODAY, "Shop", book.groceries, book.checking, "12.00"), txn
        )
    assert [change.revision for change in seen] == [1, 2, 3, 4]
    assert seen[-1].kinds == frozenset({"transaction"})


def test_a_refused_write_changes_nothing_and_notifies_no_one(server):
    seen = _changes(server)
    [sched] = server.db.iter_scheduled()
    before = sched.serialize()

    with pytest.raises(ApiError) as caught:
        server.set_advance_days(SetAdvanceDays(sched.handle, remind_days=1000))
    with pytest.raises(ApiError) as stale:
        server.resolve_due(ResolveDue(((sched.handle, TODAY, DueDecision.POST),), TODAY))

    assert (caught.value.status, caught.value.code) == (400, "schedule.remind_days.invalid")
    assert stale.value.code == "schedule.due.not_pending"
    assert server.db.get_scheduled(sched.handle).serialize() == before
    assert seen == [] and server.revision == 0


def test_unsubscribing_and_closing_stop_notifications(server):
    seen: list[Change] = []
    stop = server.subscribe(seen.append)
    [sched] = server.db.iter_scheduled()
    server.set_advance_days(SetAdvanceDays(sched.handle, advance_days=2))
    stop()
    server.set_advance_days(SetAdvanceDays(sched.handle, advance_days=3))
    assert len(seen) == 1 and server.revision == 2
    server.close()
    server.set_advance_days(SetAdvanceDays(sched.handle, advance_days=4))
    assert server.revision == 2


def test_schedules_are_found_by_name_prefix_or_handle(server, book):
    [sched] = server.db.iter_scheduled()
    assert server.find_schedule("rent").handle == sched.handle
    assert server.find_schedule(sched.handle[:6]).handle == sched.handle
    with pytest.raises(ApiError) as missing:
        server.find_schedule("Gym")
    assert (missing.value.status, missing.value.code) == (404, "schedule.not_found")
    assert str(missing.value) == "no schedule matches 'Gym'"
    twin = ScheduledTransaction.from_dict({**sched.serialize(), "handle": "other"})
    with server.db.transaction("Twin") as txn:
        server.db.add_scheduled(twin, txn)
    with pytest.raises(ApiError) as ambiguous:
        server.find_schedule("Rent")
    assert (ambiguous.value.status, ambiguous.value.code) == (400, "schedule.reference.ambiguous")


def test_advance_days_read_and_write_through_the_server(server):
    [sched] = server.db.iter_scheduled()
    saved = server.set_advance_days(SetAdvanceDays(sched.handle, advance_days=2, remind_days=9))
    assert (saved.advance_days, saved.remind_days) == (2, 9)
    assert server.advance_days(sched.handle) == saved
    kept = server.set_advance_days(SetAdvanceDays(sched.handle, remind_days=4))
    assert (kept.advance_days, kept.remind_days) == (2, 4)
    with pytest.raises(ApiError) as gone:
        server.advance_days("missing")
    assert gone.value.status == 404


def test_one_error_envelope_and_one_leaf_encoding():
    missing = ApiError.from_service(ServiceError("schedule.not_found", ("schedule",)))
    invalid = ApiError.from_service(ServiceError("schedule.next.decision", ("decision",)))
    assert (missing.status, invalid.status) == (404, 400)
    assert missing.envelope() == {
        "error": "The scheduled transaction no longer exists",
        "code": "schedule.not_found",
        "fields": ["schedule"],
    }

    class Kind(Enum):
        A = "a"

    assert to_json(
        {"m": Money("1.50"), "d": date(2026, 1, 2), "x": Decimal("0.25"), "k": Kind.A, "t": (1,)}
    ) == {"m": "1.50", "d": "2026-01-02", "x": "0.25", "k": "a", "t": [1]}
    with pytest.raises(TypeError):
        to_json(object())


def test_the_web_error_is_the_book_api_error():
    from breadsched.web.controls import ResourceError, service_error

    assert ResourceError is ApiError
    assert isinstance(service_error(ServiceError("x.not_found")), ApiError)


def test_the_command_line_and_the_browser_give_the_same_json(tmp_path, capsys):
    """One serializer: ``due-review --json`` is the browser's due review, and a
    decision answers the same way through either interface.

    The command line runs while no server holds the book: today a running server
    keeps the book's writer lock, which phase 4 (#358) lets the command line share.
    """
    today = date.today()
    path = tmp_path / "parity.breadsched"
    create_sample_book(path, as_of=(today - timedelta(days=70)))

    assert main(["due-review", str(path), "--as-of", today.isoformat(), "--json"]) == 0
    cli_review = json.loads(capsys.readouterr().out)
    rent = next(item for item in cli_review["schedules"] if item["name"] == "Sample rent")
    assert main(["scheduled-next", str(path), rent["schedule"], "--json"]) == 0
    cli_next = json.loads(capsys.readouterr().out)

    db = DbSQLite()
    db.load(str(path))
    httpd = serve(db, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def call(route, payload=None):
        request = urllib.request.Request(
            base + route,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"X-BreadSched-Token": httpd.token, "Content-Type": "application/json"},
            method="GET" if payload is None else "POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    try:
        assert call("/api/due-review") == cli_review
        listed = call("/api/scheduled")
        definition = next(row for row in listed["definitions"] if row["handle"] == rent["schedule"])
        assert definition["next"] == cli_next
        first = {"schedule": rent["schedule"], "date": rent["items"][0]["date"], "decision": "skip"}
        web_outcome = call("/api/due-review", {"decisions": [first]})
    finally:
        httpd.shutdown()
        httpd.server_close()
        db.close()

    assert main(["due-review", str(path), "--skip", "Sample rent", "--json"]) == 0
    cli_outcome = json.loads(capsys.readouterr().out)
    assert set(cli_outcome) == set(web_outcome) == {"posted", "skipped", "deferred", "transactions"}
    assert (web_outcome["skipped"], cli_outcome["skipped"]) == (1, len(rent["items"]) - 1)


def test_open_book_server_closes_the_book(tmp_path):
    path = tmp_path / "open.breadsched"
    create_sample_book(path, as_of=TODAY)
    with open_book_server(str(path), "r") as served:
        assert served.due_review(TODAY) is not None
        db = served.db
    assert db._conn is None
