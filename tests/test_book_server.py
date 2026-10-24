"""The book server: one API over an open book for every interface (#354-#357).

**Experimental** (branch ``claude/experimental-book-server``). The same contract
runs against the in-process server and against the HTTP client talking to a running
web server's ``/api/v1``: each observes every change to its book, refuses a bad
write without changing anything, and reports failures in one error envelope. The
command line and the browser give the same JSON.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from types import SimpleNamespace

import pytest

from breadsched.api import ApiError, Change, LocalBookServer, open_book_server, to_json
from breadsched.api.http_client import HttpBookClient
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
    DuplicateSchedule,
    PostDue,
    ResolveDue,
    ResolveNext,
    ServiceError,
    SetAdvanceDays,
)
from breadsched.web.server import serve

TODAY = date(2026, 10, 1)


def _add_rent(db, book) -> None:
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


def _serve(db):
    httpd = serve(db, host="127.0.0.1", port=0)
    httpd.RequestHandlerClass.EVENT_KEEPALIVE = 0.2
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}"


@pytest.fixture(params=["in-process", "http"])
def target(request, db, book):
    """The book API over a small book with one rent schedule, either way."""
    _add_rent(db, book)
    if request.param == "in-process":
        server = LocalBookServer(db)
        yield SimpleNamespace(api=server, db=db, remote=False)
        server.close()
        return
    httpd, base = _serve(db)
    try:
        yield SimpleNamespace(
            api=HttpBookClient(base, httpd.token), db=db, remote=True, base=base, token=httpd.token
        )
    finally:
        httpd.shutdown()
        httpd.server_close()


def _snapshot(db):
    return (
        sorted(txn.handle for txn in db.iter_transactions()),
        sorted(repr(item.serialize()) for item in db.iter_scheduled()),
    )


def _until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for change events")
        time.sleep(0.02)


def _follow(api) -> tuple[list[Change], object]:
    seen: list[Change] = []
    stop = api.subscribe(seen.append)
    return seen, stop


def test_a_write_is_one_change_every_subscriber_sees(target):
    api = target.api
    start = api.revision
    seen, stop = _follow(api)
    [sched] = target.db.iter_scheduled()

    outcome = api.resolve_next(ResolveNext(sched.handle, DueDecision.POST, as_of=TODAY))

    assert outcome.posted == 1 and len(outcome.transactions) == 1
    assert api.revision == start + 1
    _until(lambda: len(seen) == 1)
    stop()
    [change] = seen
    assert change.revision == start + 1
    assert {"transaction", "scheduled"} <= change.kinds
    assert change.label == "Enter next occurrence of Rent"
    assert change_json(change)["kinds"] == sorted(change.kinds)


def test_undo_redo_and_writes_from_other_code_are_changes_too(target, book):
    api = target.api
    start = api.revision
    seen, stop = _follow(api)
    [sched] = target.db.iter_scheduled()
    api.resolve_next(ResolveNext(sched.handle, DueDecision.SKIP, as_of=TODAY))
    assert api.undo() is True
    assert api.redo() is True
    with target.db.transaction("Elsewhere") as txn:
        target.db.add_transaction(
            Transaction.simple(TODAY, "Shop", book.groceries, book.checking, "12.00"), txn
        )
    _until(lambda: len(seen) == 4)
    stop()
    assert [change.revision - start for change in seen] == [1, 2, 3, 4]
    assert seen[-1].kinds == frozenset({"transaction"})
    assert api.revision == start + 4


def test_a_refused_write_changes_nothing_and_notifies_no_one(target):
    api = target.api
    start = api.revision
    seen, stop = _follow(api)
    [sched] = target.db.iter_scheduled()
    before = sched.serialize()

    with pytest.raises(ApiError) as caught:
        api.set_advance_days(SetAdvanceDays(sched.handle, remind_days=1000))
    with pytest.raises(ApiError) as stale:
        api.resolve_due(ResolveDue(((sched.handle, TODAY, DueDecision.POST),), TODAY))
    with pytest.raises(ApiError) as deferred:
        api.resolve_next(ResolveNext(sched.handle, DueDecision.DEFER))

    assert (caught.value.status, caught.value.code) == (400, "schedule.remind_days.invalid")
    assert "0 to 999" in str(caught.value)
    assert (stale.value.status, stale.value.code) == (400, "schedule.due.not_pending")
    assert deferred.value.status == 400
    assert target.db.get_scheduled(sched.handle).serialize() == before
    time.sleep(0.3)
    stop()
    assert seen == [] and api.revision == start


def test_schedules_are_found_by_name_prefix_or_handle(target):
    api = target.api
    [sched] = target.db.iter_scheduled()
    assert api.find_schedule("rent").handle == sched.handle
    assert api.find_schedule(sched.handle[:6]).serialize() == sched.serialize()
    with pytest.raises(ApiError) as missing:
        api.find_schedule("Gym")
    assert (missing.value.status, missing.value.code) == (404, "schedule.not_found")
    assert str(missing.value) == "no schedule matches 'Gym'"
    twin = ScheduledTransaction.from_dict({**sched.serialize(), "handle": "other"})
    with target.db.transaction("Twin") as txn:
        target.db.add_scheduled(twin, txn)
    with pytest.raises(ApiError) as ambiguous:
        api.find_schedule("Rent")
    assert (ambiguous.value.status, ambiguous.value.code) == (400, "schedule.reference.ambiguous")


def test_due_review_and_next_occurrence_read_alike(target):
    api = target.api
    [sched] = target.db.iter_scheduled()
    api.set_advance_days(SetAdvanceDays(sched.handle, remind_days=7))
    [review] = api.due_review(TODAY)
    assert review.schedule == sched.handle and review.total == Money("1800.00")
    assert [(item.when, item.stage) for item in review.items] == [(date(2026, 10, 6), "reminder")]
    upcoming = api.next_occurrence(sched.handle, TODAY)
    assert upcoming is not None and (upcoming.when, upcoming.amount) == (
        date(2026, 10, 6),
        Money("1800.00"),
    )
    assert api.next_occurrence("missing") is None
    outcome = api.resolve_due(
        ResolveDue(((sched.handle, date(2026, 10, 6), DueDecision.DEFER),), TODAY)
    )
    assert (outcome.posted, outcome.deferred) == (0, 1)


def test_advance_days_read_and_write_alike(target):
    api = target.api
    [sched] = target.db.iter_scheduled()
    saved = api.set_advance_days(SetAdvanceDays(sched.handle, advance_days=2, remind_days=9))
    assert (saved.advance_days, saved.remind_days) == (2, 9)
    assert api.advance_days(sched.handle) == saved
    kept = api.set_advance_days(SetAdvanceDays(sched.handle, remind_days=4))
    assert (kept.advance_days, kept.remind_days) == (2, 4)
    with pytest.raises(ApiError) as gone:
        api.advance_days("missing")
    assert gone.value.status == 404


def test_duplicating_and_deleting_a_schedule_alike(target):
    api = target.api
    [sched] = target.db.iter_scheduled()
    start = api.revision

    copy = api.duplicate_schedule(DuplicateSchedule(sched.handle, name="Rent (flat 2)"))
    assert copy.name == "Rent (flat 2)" and copy.handle != sched.handle

    def legs(item):
        return [(split.account, split.amount) for split in item.splits]

    assert legs(target.db.get_scheduled(copy.handle)) == legs(sched)
    deleted = api.delete_schedule(copy.handle)
    assert (deleted.handle, deleted.name) == (copy.handle, "Rent (flat 2)")
    assert target.db.get_scheduled(copy.handle) is None
    assert api.revision == start + 2

    before = _snapshot(target.db)
    for call, code, status in (
        (lambda: api.delete_schedule("missing"), "schedule.not_found", 404),
        (lambda: api.duplicate_schedule(DuplicateSchedule("missing")), "schedule.not_found", 404),
        (
            lambda: api.duplicate_schedule(DuplicateSchedule(sched.handle, name="  ")),
            "schedule.name.required",
            400,
        ),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert (refused.value.code, refused.value.status) == (code, status)
    assert _snapshot(target.db) == before


def test_posting_due_without_a_review_alike(target, book):
    api = target.api
    [manual] = target.db.iter_scheduled()
    automatic = ScheduledTransaction.from_dict({**manual.serialize(), "handle": "auto"})
    automatic.name = "Gym"
    automatic.auto_create = True
    with target.db.transaction("Automatic") as txn:
        target.db.add_scheduled(automatic, txn)
    october_7 = date(2026, 10, 7)

    only_automatic = api.post_due(PostDue(as_of=october_7))
    assert [(item.schedule, item.when) for item in only_automatic.posted] == [
        ("auto", date(2026, 10, 6))
    ]
    everything = api.post_due(PostDue(as_of=october_7, only_automatic=False))
    assert [(item.schedule, item.description) for item in everything.posted] == [
        (manual.handle, "Rent")
    ]
    posted = target.db.get_transaction(everything.posted[0].transaction)
    assert posted.post_date == date(2026, 10, 6) and posted.scheduled_from == manual.handle
    assert api.post_due(PostDue(as_of=october_7, only_automatic=False)).posted == ()


# ------------------------------------------------------------ in process only


def test_closing_the_server_stops_notifications(db, book):
    _add_rent(db, book)
    server = LocalBookServer(db)
    seen: list[Change] = []
    stop = server.subscribe(seen.append)
    [sched] = db.iter_scheduled()
    server.set_advance_days(SetAdvanceDays(sched.handle, advance_days=2))
    stop()
    server.set_advance_days(SetAdvanceDays(sched.handle, advance_days=3))
    server.subscribe(seen.append)
    server.close()
    server.set_advance_days(SetAdvanceDays(sched.handle, advance_days=4))
    assert len(seen) == 1


def test_the_revision_is_stored_with_the_book(tmp_path):
    """Every process agrees on the revision: it is kept in the book, moves forward on
    undo, and a read-only snapshot sees the committed value."""
    path = tmp_path / "revision.breadsched"
    create_sample_book(path, as_of=TODAY)
    with open_book_server(str(path)) as server:
        start = server.revision
        [sched] = [item for item in server.db.iter_scheduled() if item.name == "Sample rent"]
        server.set_advance_days(SetAdvanceDays(sched.handle, advance_days=3))
        assert server.undo() is True
        assert server.revision == start + 2
        reader = DbSQLite()
        reader.load(str(path), mode="r")
        try:
            assert reader.revision() == start + 2
        finally:
            reader.close()
    with open_book_server(str(path), "r") as reopened:
        assert reopened.revision == start + 2


# ------------------------------------------------------------------ HTTP only


@pytest.fixture
def remote(db, book):
    _add_rent(db, book)
    httpd, base = _serve(db)
    yield SimpleNamespace(
        base=base, token=httpd.token, db=db, client=HttpBookClient(base, httpd.token)
    )
    httpd.shutdown()
    httpd.server_close()


def _raw(remote, method, path, body=None, headers=None):
    request = urllib.request.Request(
        remote.base + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "X-BreadSched-Token": remote.token,
            "Content-Type": "application/json",
            **(headers or {}),
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read()), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read()), dict(error.headers)


def test_every_response_names_the_revision_and_a_stale_write_is_refused(remote):
    [sched] = remote.db.iter_scheduled()
    status, book, headers = _raw(remote, "GET", "/api/v1/book")
    assert status == 200 and headers["ETag"] == f'"{book["revision"]}"'
    path = f"/api/v1/schedules/{sched.handle}/advance-days"
    before = remote.db.get_scheduled(sched.handle).serialize()

    stale = f'"{book["revision"] - 1}"'
    status, refused, _ = _raw(remote, "PATCH", path, {"remind_days": 5}, {"If-Match": stale})
    assert (status, refused["code"]) == (412, "book.revision.stale")
    assert remote.db.get_scheduled(sched.handle).serialize() == before

    current = f'"{book["revision"]}"'
    status, saved, headers = _raw(remote, "PATCH", path, {"remind_days": 5}, {"If-Match": current})
    assert status == 200 and saved["remind_days"] == 5
    assert headers["ETag"] == f'"{book["revision"] + 1}"'

    client = remote.client
    client.require_revision = book["revision"]
    with pytest.raises(ApiError) as caught:
        client.set_advance_days(SetAdvanceDays(sched.handle, remind_days=6))
    assert caught.value.status == 412
    client.require_revision = client.seen_revision
    assert client.set_advance_days(SetAdvanceDays(sched.handle, remind_days=6)).remind_days == 6


def test_v1_refuses_what_it_does_not_serve(remote):
    [sched] = remote.db.iter_scheduled()
    before = remote.db.get_scheduled(sched.handle).serialize()
    path = f"/api/v1/schedules/{sched.handle}/advance-days"
    cases = [
        ("GET", "/api/v1/nothing", None, 404, "resource.not_found"),
        ("POST", "/api/v1/book", {}, 405, "request.method.not_allowed"),
        ("PATCH", path, {"remind_days": "7"}, 400, "request.invalid"),
        ("PATCH", path, {"colour": "red"}, 400, "request.invalid"),
        (
            "PATCH",
            "/api/v1/schedules/missing/advance-days",
            {"remind_days": 1},
            404,
            "schedule.not_found",
        ),
        (
            "POST",
            f"/api/v1/schedules/{sched.handle}/next",
            {"decision": "later"},
            400,
            "request.invalid",
        ),
        ("GET", "/api/v1/due-review?as_of=soon", None, 400, "query.date.invalid"),
        ("GET", "/api/v1/due-review?colour=red", None, 400, "query.unknown"),
        ("POST", "/api/v1/undo", [], 400, "request.body.object_required"),
    ]
    for method, route, body, status, code in cases:
        got, payload, _ = _raw(remote, method, route, body)
        assert (got, payload["code"]) == (status, code), route
    assert remote.db.get_scheduled(sched.handle).serialize() == before

    request = urllib.request.Request(remote.base + "/api/v1/book")
    with pytest.raises(urllib.error.HTTPError) as untrusted:
        urllib.request.urlopen(request, timeout=10)
    assert untrusted.value.code == 403


def test_a_delete_needs_no_body_but_any_body_must_be_json(remote):
    [sched] = remote.db.iter_scheduled()
    copy = remote.client.duplicate_schedule(DuplicateSchedule(sched.handle))
    request = urllib.request.Request(
        f"{remote.base}/api/v1/schedules/{copy.handle}",
        headers={"X-BreadSched-Token": remote.token},
        method="DELETE",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert json.loads(response.read())["schedule"] == copy.handle
    assert remote.db.get_scheduled(copy.handle) is None
    status, payload, _ = _raw(
        remote,
        "DELETE",
        f"/api/v1/schedules/{sched.handle}",
        body={"colour": "red"},
    )
    assert (status, payload["code"]) == (400, "request.invalid")
    form = urllib.request.Request(
        f"{remote.base}/api/v1/schedules/{sched.handle}",
        data=b"x=1",
        headers={"X-BreadSched-Token": remote.token, "Content-Type": "text/plain"},
        method="DELETE",
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(form, timeout=10)
    assert caught.value.code == 403
    assert remote.db.get_scheduled(sched.handle) is not None


def test_a_client_that_missed_changes_is_told_to_reload(remote):
    client = remote.client
    start = client.revision
    [sched] = remote.db.iter_scheduled()
    with remote.db.transaction("Missed") as txn:
        edited = ScheduledTransaction.from_dict(sched.serialize())
        edited.remind_days = 2
        remote.db.commit_scheduled(edited, txn)
    seen: list[Change] = []
    stop = client.subscribe(seen.append)
    _until(lambda: len(seen) == 1)
    stop()
    assert seen == [Change(start + 1, frozenset(), None)]


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


def test_posting_without_a_review_answers_alike_everywhere(tmp_path, capsys):
    """``scheduled --post --json`` and the browser's post-scheduled share one form."""
    path = tmp_path / "post.breadsched"
    create_sample_book(path, as_of=date(2026, 3, 15))
    command = ["scheduled", str(path), "--post", "--all", "--as-of", "2026-05-01", "--json"]
    assert main(command) == 0
    cli = json.loads(capsys.readouterr().out)
    assert cli["posted"] == len(cli["transactions"]) > 0
    assert set(cli["transactions"][0]) == {"transaction", "schedule", "date", "description"}

    db = DbSQLite()
    db.load(str(path))
    httpd, base = _serve(db)
    try:
        request = urllib.request.Request(
            base + "/api/post-scheduled",
            data=b"{}",
            headers={"X-BreadSched-Token": httpd.token, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            web = json.loads(response.read())
    finally:
        httpd.shutdown()
        httpd.server_close()
        db.close()
    assert set(web) == set(cli) == {"posted", "transactions"}
    assert web["posted"] == len(web["transactions"])


def test_open_book_server_closes_the_book(tmp_path):
    path = tmp_path / "open.breadsched"
    create_sample_book(path, as_of=TODAY)
    with open_book_server(str(path), "r") as served:
        assert served.due_review(TODAY) is not None
        db = served.db
    assert db._conn is None
