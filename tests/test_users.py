"""Decision 0004, phase 1: the book host knows who a request comes from."""

from __future__ import annotations

import base64
import getpass
import io
import json
import os
import stat
import sys
import threading
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta

import pytest
from user_fixtures import new_transaction, person

from breadsched.api import users
from breadsched.api.discovery import publish_server, record_path
from breadsched.api.http_client import HttpBookClient
from breadsched.api.users import UserDirectory, device_token
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.sample_book import create_sample_book
from breadsched.gen.services.imports import ImportBook
from breadsched.web.server import serve

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


def test_the_computer_account_is_the_hosts_first_user_and_its_administrator():
    directory = UserDirectory()
    user = directory.host_user()
    assert (user.name, user.administrator, user.host_account) == (getpass.getuser(), True, True)
    # Made once: the same user every time, the only one.
    assert directory.host_user() == user
    assert directory.users() == (user,)


def test_tokens_are_kept_only_as_digests_and_can_be_revoked():
    directory = UserDirectory()
    user = directory.host_user()
    token = directory.issue_token(user, "Laptop")
    assert token.encode() not in users.users_path().read_bytes()
    caller = directory.authenticate(token)
    assert caller is not None and caller.user == user and caller.host_files is False
    [listed] = directory.tokens(user)
    assert (listed.name, listed.host_files) == ("Laptop", False)
    assert listed.last_used is not None
    assert directory.authenticate("not-a-token") is None
    assert directory.authenticate("") is None
    assert directory.revoke(listed.id) is True
    assert directory.authenticate(token) is None
    assert directory.revoke(listed.id) is False


def test_the_device_token_is_made_once_and_replaced_when_revoked():
    directory = UserDirectory()
    token = device_token()
    assert device_token() == token
    caller = directory.authenticate(token)
    assert caller is not None and caller.host_files is True and caller.user.host_account
    [listed] = directory.tokens(caller.user)
    directory.revoke(listed.id)
    replaced = device_token()
    assert replaced != token and directory.authenticate(replaced) is not None


@POSIX
def test_the_directory_and_the_device_token_are_private():
    device_token()
    for path in (users.users_path(), users.device_token_path()):
        assert path.stat().st_mode & 0o077 == 0, path
        assert path.parent.stat().st_mode & 0o077 == 0, path.parent


@pytest.fixture
def hosted(tmp_path):
    """A book served from a file, as ``breadsched serve`` holds it."""
    path = tmp_path / "book.breadsched"
    create_sample_book(path, as_of=date(2026, 10, 1))
    db = DbSQLite()
    db.load(str(path))
    httpd = serve(db, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    yield type("Hosted", (), {"db": db, "base": base, "httpd": httpd, "path": path})
    httpd.shutdown()
    httpd.server_close()
    db.close()


def _status(base: str, token: str) -> int:
    request = urllib.request.Request(base + "/api/v1/book", headers={"X-BreadSched-Token": token})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as refused:
        return refused.code


def test_the_server_knows_the_device_token_and_refuses_any_other(hosted, tmp_path):
    token = device_token()
    assert _status(hosted.base, token) == 200
    # The server's own token is still the computer account (the page it opens).
    assert _status(hosted.base, hosted.httpd.token) == 200
    # Another computer account keeps its own directory: its token means nothing here.
    elsewhere = UserDirectory(tmp_path / "other" / "users.sqlite")
    stranger = elsewhere.issue_token(elsewhere.host_user(), "Theirs", host_files=True)
    assert _status(hosted.base, stranger) == 403
    # A revoked token stops working at once.
    directory = UserDirectory()
    caller = directory.authenticate(token)
    assert caller is not None
    [listed] = directory.tokens(caller.user)
    directory.revoke(listed.id)
    assert _status(hosted.base, token) == 403


def test_the_computer_accounts_token_names_files_and_others_send_them(hosted, tmp_path):
    statement = tmp_path / "statement.qif"
    statement.write_bytes(
        b"!Account\nNHosted checking\nTBank\n^\n!Type:Bank\nD03/04/2026\nT-4.00\nPTea\n^\n"
    )
    # The account's own programs name the file where it is (#358).
    own = HttpBookClient(hosted.base, device_token(), host_files=True)
    assert own.import_book(ImportBook(str(statement))).source == str(statement)
    # A token for a device elsewhere (not host files) is sent the file's bytes.
    directory = UserDirectory()
    phone = directory.issue_token(directory.host_user(), "Phone")
    request = urllib.request.Request(
        hosted.base + "/api/v1/imports",
        data=json.dumps({"source": str(statement)}).encode(),
        headers={"X-BreadSched-Token": phone, "Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=10)
    assert refused.value.code == 403
    sent = HttpBookClient(hosted.base, phone).import_book(ImportBook(str(statement)))
    assert sent.source != str(statement)


def test_the_server_record_carries_no_token(hosted, tmp_path, monkeypatch):
    monkeypatch.setattr("breadsched.api.discovery.runtime_directory", lambda: tmp_path / "run")
    publish_server(str(hosted.path), hosted.base, hosted.db.writer_lock_token or "")
    record = json.loads(record_path(str(hosted.path)).read_text(encoding="utf-8"))
    assert set(record) == {"book", "url", "pid", "host", "lock", "book_id"}
    for token in (hosted.httpd.token, hosted.httpd.local_token):
        assert token not in json.dumps(record)


# ------------------------------------------------------------ phase 2: accounts


def test_an_administrator_adds_users_with_temporary_passwords():
    directory = UserDirectory()
    alice = directory.add_user("alice", "temporary-pass", display_name="Alice")
    assert (alice.display_name, alice.administrator, alice.must_change) == ("Alice", False, True)
    stored = users.users_path().read_bytes()
    assert b"temporary-pass" not in stored and b"scrypt$" in stored
    for name, password, code in (
        ("alice", "another-password", "users.name.taken"),
        ("../alice", "another-password", "users.name.invalid"),
        ("", "another-password", "users.name.invalid"),
        ("bob", "short", "users.password.too_short"),
    ):
        with pytest.raises(users.UserError) as refused:
            directory.add_user(name, password)
        assert refused.value.code == code
    assert {user.name for user in directory.users()} == {getpass.getuser(), "alice"}


def test_the_computer_account_and_the_last_administrator_stay():
    directory = UserDirectory()
    host = directory.host_user()
    with pytest.raises(users.UserError) as refused:
        directory.remove_user(host.name)
    assert refused.value.code == "users.user.host_account"
    directory.add_user("carol", "temporary-pass", administrator=True)
    directory.remove_user("carol")
    assert [user.name for user in directory.users()] == [host.name]
    with pytest.raises(users.UserError) as missing:
        directory.remove_user("carol")
    assert missing.value.code == "users.user.not_found"


def test_signing_in_makes_a_session_that_lapses_unless_used():
    directory = UserDirectory()
    directory.add_user("alice", "temporary-pass")
    start = datetime(2026, 11, 1, tzinfo=UTC)
    signed = directory.sign_in("alice", "temporary-pass", client="10.0.0.2", now=start)
    assert signed.user.must_change is True
    assert directory.authenticate(signed.token, now=start) is not None
    # Used within the window, it lasts another SESSION_DAYS from then.
    later = start + timedelta(days=users.SESSION_DAYS - 1)
    caller = directory.authenticate(signed.token, now=later)
    assert caller is not None and caller.host_files is False
    lapsed = later + timedelta(days=users.SESSION_DAYS)
    assert directory.authenticate(signed.token, now=lapsed) is None
    assert directory.tokens(caller.user) == ()


def test_changing_a_password_needs_the_old_one_and_ends_the_sessions():
    directory = UserDirectory()
    directory.add_user("alice", "temporary-pass")
    signed = directory.sign_in("alice", "temporary-pass", client="10.0.0.2")
    with pytest.raises(users.UserError) as wrong:
        directory.set_password("alice", "a-new-password", current="not-it")
    assert wrong.value.code == "users.password.wrong"
    changed = directory.set_password("alice", "a-new-password", current="temporary-pass")
    assert changed.must_change is False
    assert directory.authenticate(signed.token) is None
    directory.sign_in("alice", "a-new-password", client="10.0.0.2")
    # An administrator's reset ends the sessions too, and must be changed again.
    reset = directory.reset_password("alice", "another-temporary")
    assert reset.must_change is True
    with pytest.raises(users.UserError):
        directory.sign_in("alice", "a-new-password", client="10.0.0.2")
    # The computer account sets its first password without a current one.
    host = directory.set_password(getpass.getuser(), "host-password")
    assert host.has_password


def test_failed_sign_ins_say_nothing_and_are_throttled():
    directory = UserDirectory()
    directory.add_user("alice", "temporary-pass")
    start = datetime(2026, 11, 1, tzinfo=UTC)
    # A wrong password and a name that does not exist fail alike.
    for name in ("alice", "nobody"):
        with pytest.raises(users.UserError) as refused:
            directory.sign_in(name, "wrong-password", client="10.0.0.1", now=start)
        assert refused.value.code == "signin.failed"
    # Failures for one name, from anywhere, make that name wait...
    for attempt in range(2, users.FREE_FAILURES + 1):
        with pytest.raises(users.UserError):
            directory.sign_in("alice", "wrong", client=f"10.0.1.{attempt}", now=start)
    with pytest.raises(users.UserError) as waiting:
        directory.sign_in("alice", "temporary-pass", client="10.0.2.1", now=start)
    assert waiting.value.code == "signin.throttled"
    # ...and failures from one address, for any names, make that address wait.
    for attempt in range(users.FREE_FAILURES):
        with pytest.raises(users.UserError):
            directory.sign_in(f"guess{attempt}", "wrong", client="10.0.3.1", now=start)
    with pytest.raises(users.UserError) as address:
        directory.sign_in("someone", "wrong", client="10.0.3.1", now=start)
    assert address.value.code == "signin.throttled"
    # After the wait it is allowed again, and a success clears the count.
    after = start + users.MAX_DELAY
    assert directory.sign_in("alice", "temporary-pass", client="10.0.2.1", now=after).token
    assert directory.sign_in("alice", "temporary-pass", client="10.0.2.1", now=after).token


def test_the_user_commands_manage_users_and_tokens(capsys, monkeypatch):
    from breadsched.cli.main import main

    monkeypatch.setattr("sys.stdin", io.StringIO("temporary-pass\n"))
    assert main(["users", "add", "alice", "--display-name", "Alice", "--password-stdin"]) == 0
    capsys.readouterr()
    assert main(["users", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert {item["name"] for item in listed} == {getpass.getuser(), "alice"}
    assert main(["users", "remove", getpass.getuser()]) == 2
    assert "cannot be removed" in capsys.readouterr().err
    assert main(["token", "create", "Backup script", "--json"]) == 0
    made = json.loads(capsys.readouterr().out)
    assert UserDirectory().authenticate(made["token"]) is not None
    assert main(["token", "list", "--json"]) == 0
    [token] = [
        item for item in json.loads(capsys.readouterr().out) if item["name"] == "Backup script"
    ]
    assert main(["token", "revoke", token["id"]]) == 0
    assert UserDirectory().authenticate(made["token"]) is None
    monkeypatch.setattr("sys.stdin", io.StringIO("host-password\n"))
    assert main(["password", "--password-stdin"]) == 0
    assert UserDirectory().host_user().has_password
    assert main(["users", "remove", "alice"]) == 0


# ------------------------------------------------------------- phase 2: over HTTP


def _call(base, method, path, body=None, headers=None):
    request = urllib.request.Request(
        base + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read() or b"null"), response.headers
    except urllib.error.HTTPError as refused:
        return refused.code, json.loads(refused.read() or b"null"), refused.headers


def test_signing_in_over_http_gives_a_session_and_a_private_cookie(hosted):
    directory = UserDirectory()
    directory.add_user("alice", "temporary-pass", display_name="Alice")
    directory.grant(hosted.db.get_metadata("book.id"), "alice", "viewer")
    status, signed, headers = _call(
        hosted.base, "POST", "/api/v1/session", {"name": "alice", "password": "temporary-pass"}
    )
    assert status == 200 and signed["name"] == "alice" and signed["must_change"] is True
    cookie = headers["Set-Cookie"]
    assert cookie.startswith(f"breadsched_session={signed['token']};")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" not in cookie
    session = {"X-BreadSched-Token": signed["token"]}
    # Nothing but choosing a password until the temporary one is changed.
    assert _call(hosted.base, "GET", "/api/v1/book", headers=session)[0] == 403
    status, me, _ = _call(hosted.base, "GET", "/api/v1/me", headers=session)
    assert (status, me["must_change"], me["this_computer"]) == (200, True, False)
    status, refused, _ = _call(
        hosted.base,
        "POST",
        "/api/v1/me/password",
        {"current": "wrong", "new": "x" * 12},
        headers=session,
    )
    assert (status, refused["code"]) == (400, "users.password.wrong")
    status, me, _ = _call(
        hosted.base,
        "POST",
        "/api/v1/me/password",
        {"current": "temporary-pass", "new": "alices-own-password"},
        headers=session,
    )
    assert (status, me["must_change"]) == (200, False)
    # The session that changed it goes on; the browser's cookie works as well.
    assert _call(hosted.base, "GET", "/api/v1/book", headers=session)[0] == 200
    cookie_only = {"Cookie": f"breadsched_session={signed['token']}"}
    assert _call(hosted.base, "GET", "/api/v1/book", headers=cookie_only)[0] == 200
    # Signing out ends it and clears the cookie.
    status, ended, headers = _call(hosted.base, "DELETE", "/api/v1/session", headers=session)
    assert (status, ended["signed_out"]) == (200, True)
    assert "Max-Age=0" in headers["Set-Cookie"]
    assert _call(hosted.base, "GET", "/api/v1/book", headers=session)[0] == 403


def test_failed_and_foreign_sign_ins_are_refused(hosted, monkeypatch):
    # One moment throughout, so the waits after failures cannot pass mid-test.
    moment = datetime(2026, 11, 1, tzinfo=UTC)
    monkeypatch.setattr("breadsched.api.users._now", lambda: moment)
    UserDirectory().add_user("alice", "temporary-pass")
    attempt = {"name": "alice", "password": "wrong-password"}
    status, refused, _ = _call(hosted.base, "POST", "/api/v1/session", attempt)
    assert (status, refused["code"]) == (401, "signin.failed")
    # A page from another site may not sign anyone in.
    status, _, _ = _call(
        hosted.base, "POST", "/api/v1/session", attempt, {"Origin": "https://evil.example"}
    )
    assert status == 403
    # Behind the proxy, the client's address comes from X-Real-IP.
    for _ in range(users.FREE_FAILURES):
        _call(
            hosted.base,
            "POST",
            "/api/v1/session",
            {"name": "x", "password": "y" * 12},
            {"X-Real-IP": "192.0.2.7"},
        )
    status, waiting, _ = _call(
        hosted.base,
        "POST",
        "/api/v1/session",
        {"name": "z", "password": "y" * 12},
        {"X-Real-IP": "192.0.2.7"},
    )
    assert (status, waiting["code"]) == (429, "signin.throttled")
    assert (
        _call(
            hosted.base,
            "POST",
            "/api/v1/session",
            {"name": "z", "password": "y" * 12},
            {"X-Real-IP": "192.0.2.8"},
        )[0]
        == 401
    )
    # Without a token or cookie, who-am-I is refused.
    assert _call(hosted.base, "GET", "/api/v1/me")[0] == 403


def test_the_cookie_is_secure_through_the_tls_proxy(tmp_path):
    path = tmp_path / "book.breadsched"
    create_sample_book(path, as_of=date(2026, 10, 1))
    db = DbSQLite()
    db.load(str(path))
    httpd = serve(db, host="127.0.0.1", port=0, trusted_origins=("https://books.example",))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        UserDirectory().add_user("alice", "temporary-pass")
        status, _, headers = _call(
            f"http://127.0.0.1:{httpd.server_port}",
            "POST",
            "/api/v1/session",
            {"name": "alice", "password": "temporary-pass"},
            {"Origin": "https://books.example"},
        )
        assert status == 200 and "; Secure" in headers["Set-Cookie"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        db.close()


# ------------------------------------------------------------ phase 3: roles


def test_each_role_may_do_what_it_is_given_and_no_more(hosted):
    from dataclasses import replace

    from breadsched.api import ApiError

    directory = UserDirectory()
    book_id = hosted.db.get_metadata("book.id")
    assert book_id and [book.id for book in directory.books()] == [book_id]

    def client(name, role):
        return HttpBookClient(hosted.base, person(directory, name, role, book_id))

    def refused(call, code="book.permission.denied"):
        with pytest.raises(ApiError) as caught:
            call()
        assert (caught.value.status, caught.value.code) == (403, code)

    viewer = client("vera", "viewer")
    entry = client("eli", "entry")
    editor = client("eddie", "editor")
    stranger = client("nora", None)

    # A viewer reads anything and changes nothing.
    assert viewer.chart() and viewer.plan_bounds()
    refused(lambda: viewer.save_transaction(new_transaction(hosted.db, viewer)))
    refused(lambda: viewer.undo())

    # An entry user sees the accounts without values and adds transactions, nothing else.
    assert entry.account_outline()
    added = entry.save_transaction(new_transaction(hosted.db, entry))
    assert hosted.db.get_transaction(added.handle) is not None
    refused(lambda: entry.chart())
    refused(lambda: entry.register_view(next(iter(hosted.db.iter_accounts())).handle))
    refused(lambda: entry.undo())
    change = new_transaction(hosted.db, entry)
    refused(lambda: entry.save_transaction(replace(change, existing_handle=added.handle)))
    refused(lambda: entry.delete_transaction(added.handle))

    # An editor writes and undoes anyone's change, but not what only the owner may.
    assert editor.undo() is True
    assert hosted.db.get_transaction(added.handle) is None
    refused(lambda: editor.set_attachment_folder("/tmp"))
    refused(lambda: editor.writeback_preview())

    # Someone known to the host but given no role here may not use the book at all.
    refused(lambda: stranger.revision, "request.untrusted")
    # The computer account owns every book it holds.
    assert HttpBookClient(hosted.base, device_token()).writeback_keep_backups() >= 0


def test_the_routes_that_reach_the_book_file_check_the_role_too(hosted):
    """Documents and import uploads are read or kept by the web layer itself, around
    the guarded book, so they check the role before touching the file."""
    directory = UserDirectory()
    book_id = hosted.db.get_metadata("book.id")
    viewer = person(directory, "vera", "viewer", book_id)
    entry = person(directory, "eli", "entry", book_id)
    document = "/api/attachment/content?transaction=missing&location=receipt.pdf"
    status, body, _ = _call(hosted.base, "GET", document, headers={"X-BreadSched-Token": entry})
    assert (status, body["code"]) == (403, "book.permission.denied")
    status, body, _ = _call(hosted.base, "GET", document, headers={"X-BreadSched-Token": viewer})
    assert (status, body["code"]) == (404, "transaction.not_found")
    request = urllib.request.Request(
        hosted.base + "/api/import/upload?filename=statement.qif",
        data=b"!Type:Bank\nD10/01/2026\nT-5.00\n^\n",
        headers={"Content-Type": "application/octet-stream", "X-BreadSched-Token": viewer},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=20)
    assert refused.value.code == 403
    assert json.loads(refused.value.read())["code"] == "book.permission.denied"
    assert not any(hosted.path.parent.glob("*.uploads"))


def test_each_person_keeps_their_own_plan_view(hosted):
    """Decision 0004: display choices are personal. A viewer may keep theirs, and
    neither person's choice changes the other's or the book."""
    from breadsched.gen.engine.activity import PlanMeasure, PlanSettings, ReportingPeriod

    directory = UserDirectory()
    book_id = hosted.db.get_metadata("book.id")
    vera = HttpBookClient(hosted.base, person(directory, "vera", "viewer", book_id))
    owner = HttpBookClient(hosted.base, device_token())
    defaults = (date(2026, 1, 1), date(2027, 12, 31))
    quarters = PlanSettings(*defaults, ReportingPeriod.QUARTER, PlanMeasure.ACTUAL)
    years = PlanSettings(date(2026, 1, 1), date(2030, 12, 31), ReportingPeriod.YEAR)
    revision = owner.revision
    assert vera.save_plan_settings(quarters) == quarters
    assert owner.save_plan_settings(years) == years
    assert vera.plan_settings(*defaults) == quarters
    assert owner.plan_settings(*defaults) == years
    # Saved with the person, not in the book: its revision and stored view are as they were.
    assert owner.revision == revision
    assert hosted.db.get_metadata(PlanSettings.KEY) is None
    assert directory.preference(directory.user("vera"), book_id, PlanSettings.KEY) == (
        quarters.serialize()
    )
    # The Plan itself opens on each person's own view.
    from breadsched.gen.services.plan import PlanQuery

    assert vera.plan(PlanQuery(use_saved=True)).period is ReportingPeriod.QUARTER
    assert owner.plan(PlanQuery(use_saved=True)).period is ReportingPeriod.YEAR
    # Removing a person removes what they kept.
    directory.remove_user("vera")
    assert directory.preference(directory.host_user(), book_id, PlanSettings.KEY) == (
        years.serialize()
    )


def test_each_person_arranges_their_own_dashboard(hosted):
    """Decision 0004: the whole Dashboard configuration is personal, its groups'
    members included; the book's arrangement is where anyone starts."""

    from breadsched.gen.lib import AccountType
    from breadsched.gen.services import DashboardGroupInput, SaveAccount, SaveDashboardConfig

    directory = UserDirectory()
    book_id = hosted.db.get_metadata("book.id")
    vera = HttpBookClient(hosted.base, person(directory, "vera", "viewer", book_id))
    eddie = HttpBookClient(hosted.base, person(directory, "eddie", "editor", book_id))
    owner = HttpBookClient(hosted.base, device_token())
    bank = next(
        account.handle
        for account in hosted.db.iter_accounts()
        if account.atype is AccountType.BANK and not account.placeholder
    )
    book = owner.dashboard_settings()
    revision = owner.revision

    # A viewer may arrange their own; nobody else's view, nor the book, changes.
    mine = vera.save_dashboard_settings(
        SaveDashboardConfig(
            groups=(DashboardGroupInput("Spending money", "liquid", (bank,)),),
            liquidity_days=14,
        )
    )
    assert [(group.name, group.accounts) for group in mine.groups] == [("Spending money", (bank,))]
    assert vera.dashboard_settings() == mine
    assert vera.dashboard().config.liquidity_days == 14
    assert vera.account(bank).account.group == "Spending money"
    assert owner.dashboard_settings() == book
    assert owner.account(bank).account.group == hosted.db.get_account(bank).group
    assert owner.revision == revision

    # An editor moving an account between groups moves it in their own arrangement;
    # the book keeps its group, and the viewer keeps hers.
    record = eddie.account(bank)
    book_group = record.account.group
    moved = record.account
    moved.group = "Everyday"
    eddie.save_account(SaveAccount(moved, existing_handle=bank))
    assert eddie.account(bank).account.group == "Everyday"
    assert any(
        group.name == "Everyday" and bank in group.accounts
        for group in eddie.dashboard_settings().groups
    )
    assert hosted.db.get_account(bank).group == book_group
    assert vera.account(bank).account.group == "Spending money"


def test_each_change_and_undo_step_names_who_made_it(hosted):
    """Decision 0004: a change made for someone names them, and so does the undo step
    it leaves; an editor or owner may undo anyone's."""
    import queue

    directory = UserDirectory()
    book_id = hosted.db.get_metadata("book.id")
    directory.add_user("eddie", "temporary-pass", display_name="Eddie")
    directory.set_password("eddie", "eddie-own-password", current="temporary-pass")
    directory.grant(book_id, "eddie", "editor")
    eddie = HttpBookClient(hosted.base, directory.issue_token(directory.user("eddie"), "phone"))
    owner = HttpBookClient(hosted.base, device_token())
    heard: queue.Queue = queue.Queue()
    stop = owner.subscribe(heard.put)
    try:
        eddie.save_transaction(new_transaction(hosted.db, eddie))
        change = heard.get(timeout=20)
        assert (change.by, change.undo_by) == ("Eddie", "Eddie")
        assert hosted.db.undo_author() == "Eddie"
        assert owner.undo() is True
        undone = heard.get(timeout=20)
        assert undone.by == directory.host_user().display_name
        assert undone.can_redo
    finally:
        stop()
    # In process no one is named: the book is the computer account's own.
    from breadsched.api.local import book_server

    local = book_server(hosted.db)
    seen = []
    stop = local.subscribe(seen.append)
    try:
        local.redo()
    finally:
        stop()
    assert seen and seen[-1].by is None


def test_grants_name_known_users_and_real_roles(hosted):
    directory = UserDirectory()
    book_id = hosted.db.get_metadata("book.id")
    directory.add_user("vera", "temporary-pass")
    for name, role, code in (
        ("vera", "boss", "users.role.invalid"),
        ("nobody", "viewer", "users.user.not_found"),
        (getpass.getuser(), "viewer", "users.grant.host_account"),
    ):
        with pytest.raises(users.UserError) as refused:
            directory.grant(book_id, name, role)
        assert refused.value.code == code
    directory.grant(book_id, "vera", "viewer")
    directory.grant(book_id, "vera", "editor")
    [grant] = directory.grants(book_id)
    assert (grant.user.name, grant.role) == ("vera", "editor")
    assert directory.role(book_id, directory.user("vera")) == "editor"
    assert directory.revoke_grant(book_id, "vera") is True
    assert directory.grants(book_id) == ()
    # Removing a user removes their grants with them.
    directory.grant(book_id, "vera", "viewer")
    directory.remove_user("vera")
    assert directory.grants(book_id) == ()


def test_the_share_command_grants_changes_and_removes_roles(tmp_path, capsys, monkeypatch):
    from breadsched.cli.main import main

    path = tmp_path / "book.breadsched"
    create_sample_book(path, as_of=date(2026, 10, 1))
    UserDirectory().add_user("alice", "temporary-pass", display_name="Alice")
    book = str(path)
    assert main(["share", book, "alice", "--role", "entry"]) == 0
    assert "Alice now has the entry role in this book" in capsys.readouterr().out
    assert main(["share", book, "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed == [
        {"user": getpass.getuser(), "role": "owner"},
        {"user": "alice", "role": "entry"},
    ]
    # The book now carries its id, by which the grant is kept.
    reader = DbSQLite()
    reader.load(book, "r")
    try:
        book_id = reader.get_metadata("book.id")
    finally:
        reader.close()
    assert UserDirectory().role(book_id, UserDirectory().user("alice")) == "entry"
    with pytest.raises(SystemExit):
        main(["share", book, "alice", "--role", "boss"])
    capsys.readouterr()
    assert main(["share", book, "alice", "--remove"]) == 0
    assert main(["share", book, "alice", "--remove"]) == 2
    assert "had no role" in capsys.readouterr().err


# ------------------------------------------------------------ two-step sign-in (phase 5)


def test_totp_codes_follow_rfc_6238():
    # RFC 6238's SHA-1 key, "12345678901234567890", at 59 s and 1111111109 s.
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert users.totp_code(secret, 59 // 30) == "287082"
    assert users.totp_code(secret, 1111111109 // 30) == "081804"


def test_two_step_sign_in_asks_for_a_code_after_the_password():
    directory = UserDirectory()
    directory.add_user("alice", "temporary-pass")
    directory.set_password("alice", "alices-own-password", current="temporary-pass")
    alice = directory.user("alice")
    start = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

    def at(seconds):
        return start + timedelta(seconds=seconds)

    def code(setup, seconds):
        return users.totp_code(setup.secret, int(at(seconds).timestamp()) // 30)

    setup = directory.start_two_step(alice)
    wrong_code = "000000" if code(setup, 0) != "000000" else "111111"
    assert setup.uri.startswith("otpauth://totp/BreadSched:alice?secret=")
    # Nothing changes until a code from the app confirms it.
    assert not directory.two_step_on(alice)
    with pytest.raises(users.UserError) as wrong:
        directory.confirm_two_step(alice, wrong_code, now=at(0))
    assert wrong.value.code == "two_step.code.invalid"
    recovery = directory.confirm_two_step(alice, code(setup, 0), now=at(0))
    assert len(recovery) == 10 and len(set(recovery)) == 10
    assert directory.two_step_on(alice)
    with pytest.raises(users.UserError) as again:
        directory.start_two_step(alice)
    assert again.value.code == "two_step.already_on"

    def sign_in(**kwargs):
        return directory.sign_in("alice", "alices-own-password", client="phone", **kwargs)

    # The right password alone asks for the code, and is not counted as a failure.
    for _ in range(users.FREE_FAILURES + 2):
        with pytest.raises(users.UserError) as asked:
            sign_in(now=at(60))
        assert asked.value.code == "signin.code_required"
    assert sign_in(code=code(setup, 60), now=at(60)).user.name == "alice"
    # A code is never used twice, nor one from long ago.
    for used in (code(setup, 60), code(setup, 0)):
        with pytest.raises(users.UserError) as replayed:
            sign_in(code=used, now=at(61))
        assert replayed.value.code == "signin.code.invalid"
    # Each recovery code lets one sign-in through, once, written any way.
    assert sign_in(code=recovery[0].upper().replace("-", " "), now=at(120))
    with pytest.raises(users.UserError) as spent:
        sign_in(code=recovery[0], now=at(150))
    assert spent.value.code == "signin.code.invalid"
    # Wrong codes are failures like a wrong password: they are throttled.
    refusals = []
    for _ in range(users.FREE_FAILURES + 1):
        with pytest.raises(users.UserError) as refused:
            sign_in(code="not-a-code", now=at(150))
        refusals.append(refused.value.code)
    # The replayed and spent codes above were failures too, so the wait comes sooner.
    assert refusals[0] == "signin.code.invalid" and refusals[-1] == "signin.throttled"

    # Turning it off needs the password; an administrator can reset a lost phone.
    with pytest.raises(users.UserError) as refused:
        directory.stop_two_step(alice, "wrong-password")
    assert refused.value.code == "users.password.wrong"
    directory.stop_two_step(alice, "alices-own-password")
    assert not directory.two_step_on(alice)
    setup = directory.start_two_step(alice)
    directory.confirm_two_step(alice, code(setup, 3600), now=at(3600))
    directory.reset_two_step("alice")
    assert not directory.two_step_on(alice)


def test_two_step_sign_in_over_http(hosted):
    directory = UserDirectory()
    book_id = hosted.db.get_metadata("book.id")
    token = person(directory, "alice", "viewer", book_id)
    header = {"X-BreadSched-Token": token}
    status, me, _ = _call(hosted.base, "GET", "/api/v1/me", headers=header)
    assert (status, me["two_step"]) == (200, False)
    status, setup, _ = _call(hosted.base, "POST", "/api/v1/me/two-step", {}, header)
    assert status == 200 and setup["uri"].startswith("otpauth://totp/")
    now = int(datetime.now(UTC).timestamp()) // 30
    status, turned, _ = _call(
        hosted.base,
        "POST",
        "/api/v1/me/two-step/confirm",
        {"code": users.totp_code(setup["secret"], now)},
        header,
    )
    assert status == 200 and turned["two_step"] and len(turned["recovery_codes"]) == 10

    signing = {"name": "alice", "password": "alice-own-password"}
    status, asked, _ = _call(hosted.base, "POST", "/api/v1/session", signing)
    assert (status, asked["code"]) == (401, "signin.code_required")
    status, wrong, _ = _call(hosted.base, "POST", "/api/v1/session", {**signing, "code": "x"})
    assert (status, wrong["code"]) == (401, "signin.code.invalid")
    status, signed, _ = _call(
        hosted.base,
        "POST",
        "/api/v1/session",
        {**signing, "code": users.totp_code(setup["secret"], now + 1)},
    )
    assert status == 200 and signed["name"] == "alice"
    status, off, _ = _call(
        hosted.base, "POST", "/api/v1/me/two-step/off", {"password": "nope"}, header
    )
    assert (status, off["code"]) == (400, "users.password.wrong")
    status, off, _ = _call(
        hosted.base,
        "POST",
        "/api/v1/me/two-step/off",
        {"password": "alice-own-password"},
        header,
    )
    assert (status, off["two_step"]) == (200, False)


def test_the_two_step_commands_turn_it_on_and_off(capsys, monkeypatch, tmp_path):
    from breadsched.cli.main import main

    fixed = b"12345678901234567890"
    secret = base64.b32encode(fixed).decode().rstrip("=")
    real = users.secrets.token_bytes
    # Only the 20-byte secret is fixed; recovery codes stay random.
    monkeypatch.setattr(
        users.secrets, "token_bytes", lambda size: fixed if size == 20 else real(size)
    )
    assert main(["two-step", "status"]) == 0
    assert "off" in capsys.readouterr().out
    step = int(datetime.now(UTC).timestamp()) // users.TOTP_STEP
    monkeypatch.setattr("sys.stdin", io.StringIO(users.totp_code(secret, step) + "\n"))
    assert main(["two-step", "on", "--code-stdin"]) == 2
    assert "--setup-file" in capsys.readouterr().err
    setup_file = tmp_path / "two-step.txt"
    command = ["two-step", "on", "--code-stdin", "--json", "--setup-file", str(setup_file)]
    assert main(command) == 0
    shown = capsys.readouterr().out
    # The secret and the recovery codes reach only the private file, never the terminal.
    assert json.loads(shown) == {"two_step": True, "setup_file": str(setup_file)}
    written = setup_file.read_text(encoding="utf-8")
    assert f"secret: {secret}" in written and secret not in shown
    recovery = written.split("without the app:\n", 1)[1].split()
    assert len(recovery) == 10 and not any(code in shown for code in recovery)
    if os.name == "posix":
        assert stat.S_IMODE(setup_file.stat().st_mode) == 0o600
    assert UserDirectory().two_step_on(UserDirectory().host_user())
    # The computer account has no password yet, so turning it off asks for none.
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert main(["two-step", "off", "--code-stdin"]) == 0
    assert not UserDirectory().two_step_on(UserDirectory().host_user())
    # An administrator turns off someone else's, for a lost phone.
    directory = UserDirectory()
    directory.add_user("alice", "temporary-pass")
    directory.start_two_step(directory.user("alice"))
    assert main(["users", "reset-two-step", "alice"]) == 0
    assert "Turned off alice's two-step sign-in" in capsys.readouterr().out
