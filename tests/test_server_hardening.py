"""What the security review of the shared book server found, kept fixed (decision 0004).

Each test holds one finding of the review closed:

- a document a person attached is never served as something that can run;
- sign-in attempts sent together are throttled and checked one at a time, so a code
  is never used twice;
- a change stream ends once its caller may no longer read the book;
- the HTTP client never sends its token in the clear to another computer, and never
  to an address a server record could not have named;
- the book's data is never kept in a browser's or proxy's cache.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime

import pytest
from user_fixtures import person

from breadsched.api import users
from breadsched.api.discovery import find_server, publish_server
from breadsched.api.http_client import HttpBookClient
from breadsched.api.users import UserDirectory
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.sample_book import create_sample_book
from breadsched.web.server import serve


@pytest.fixture
def hosted(tmp_path):
    """A book served from a file, as ``breadsched serve`` holds it."""
    path = tmp_path / "book.breadsched"
    create_sample_book(path, as_of=date(2026, 10, 1))
    db = DbSQLite()
    db.load(str(path))
    httpd = serve(db, host="127.0.0.1", port=0)
    httpd.RequestHandlerClass.EVENT_KEEPALIVE = 0.2
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    yield type("Hosted", (), {"db": db, "base": base, "httpd": httpd, "path": path})
    httpd.shutdown()
    httpd.server_close()
    db.close()


def _request(base, method, path, *, token, body=None, content_type="application/json"):
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": content_type, "X-BreadSched-Token": token},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as refused:
        return refused.code, refused.read(), refused.headers


# ------------------------------------------------------------------ documents


def test_a_document_that_could_run_is_only_ever_saved(hosted):
    """An editor's attached web page, opened by the owner at its address in the signed-in
    app, would have run as the app with the owner's session."""
    token = hosted.httpd.token
    transaction = next(iter(hosted.db.iter_transactions())).handle

    def attach(filename, content):
        query = urllib.parse.urlencode({"transaction": transaction, "filename": filename})
        status, _, _ = _request(
            hosted.base,
            "POST",
            "/api/attachment/upload?" + query,
            token=token,
            body=content,
            content_type="application/octet-stream",
        )
        assert status == 200

    def opened(location):
        query = urllib.parse.urlencode({"transaction": transaction, "location": location})
        status, content, headers = _request(
            hosted.base, "GET", "/api/attachment/content?" + query, token=token
        )
        assert status == 200
        return content, headers

    page = b"<script src='/api/attachment/content?location=steal.js'></script>"
    for name, content in (
        ("statement.html", page),
        ("steal.js", b"fetch('/api/v1/undo')"),
        ("chart.svg", b"<svg onload='alert(1)'/>"),
    ):
        attach(name, content)
        served, headers = opened(name)
        assert served == content
        assert headers["Content-Type"] == "application/octet-stream"
        assert headers["Content-Disposition"].startswith("attachment;")
        assert "sandbox" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
    # A receipt opens in place, still sandboxed.
    attach("receipt.pdf", b"%PDF-1.4")
    _content, headers = opened("receipt.pdf")
    assert headers["Content-Type"] == "application/pdf"
    assert headers["Content-Disposition"] == "inline; filename*=UTF-8''receipt.pdf"
    assert "sandbox" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"


# ------------------------------------------------------------------ signing in


def test_sign_ins_sent_together_are_throttled_one_at_a_time():
    directory = UserDirectory()
    directory.add_user("alice", "temporary-pass")
    start = datetime(2026, 11, 1, tzinfo=UTC)

    def attempt(_):
        try:
            directory.sign_in("alice", "wrong-password", client="10.0.0.9", now=start)
        except users.UserError as refused:
            return refused.code
        return "signed in"

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(attempt, range(24)))
    # Only the free failures are checked at all; every other attempt waits its turn
    # and is then told to wait.
    assert outcomes.count("signin.failed") == users.FREE_FAILURES
    assert outcomes.count("signin.throttled") == 24 - users.FREE_FAILURES


def test_one_code_sent_twice_at_once_lets_in_only_once():
    directory = UserDirectory()
    directory.add_user("alice", "temporary-pass")
    directory.set_password("alice", "alices-own-password", current="temporary-pass")
    alice = directory.user("alice")
    start = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    setup = directory.start_two_step(alice)
    step = int(start.timestamp()) // users.TOTP_STEP
    directory.confirm_two_step(alice, users.totp_code(setup.secret, step - 1), now=start)
    code = users.totp_code(setup.secret, step)

    def attempt(_):
        try:
            directory.sign_in("alice", "alices-own-password", client="phone", code=code, now=start)
        except users.UserError as refused:
            return refused.code
        return "signed in"

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, range(6)))
    assert outcomes.count("signed in") == 1
    assert set(outcomes) == {"signed in", "signin.code.invalid"}


# --------------------------------------------------------------- change streams


def _follow(base: str, token: str) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    parsed = urllib.parse.urlparse(base)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    connection.request("GET", "/api/v1/events", headers={"X-BreadSched-Token": token})
    response = connection.getresponse()
    assert response.status == 200
    assert response.readline().startswith(b"event: hello")
    return connection, response


def _ends(response: http.client.HTTPResponse, within: float = 8.0) -> bool:
    """Whether the stream closes (keep-alives aside) before ``within`` seconds pass."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            line = response.readline()
        except TimeoutError:
            return False
        if not line:
            return True
    return False


@pytest.mark.parametrize("ending", ["sign out", "revoke the token", "take the role away"])
def test_a_change_stream_ends_once_its_caller_may_not_read(hosted, ending):
    directory = UserDirectory()
    book_id = hosted.db.get_metadata("book.id")
    if ending == "sign out":
        person(directory, "vera", "viewer", book_id)
        status, signed, _ = _request(
            hosted.base,
            "POST",
            "/api/v1/session",
            token="",
            body={"name": "vera", "password": "vera-own-password"},
        )
        assert status == 200
        token = json.loads(signed)["token"]
    else:
        token = person(directory, "vera", "viewer", book_id)
    connection, response = _follow(hosted.base, token)
    try:
        # While she may read, the stream stays open past several keep-alives.
        assert not _ends(response, within=1.0)
        if ending == "sign out":
            assert _request(hosted.base, "DELETE", "/api/v1/session", token=token)[0] == 200
        elif ending == "revoke the token":
            [device] = directory.tokens(directory.user("vera"))
            assert directory.revoke(device.id)
        else:
            assert directory.revoke_grant(book_id, "vera")
        assert _ends(response)
    finally:
        connection.close()


# ------------------------------------------------------------------ the client


def test_the_client_never_sends_its_token_in_the_clear_to_another_computer():
    for address in ("http://books.example.lan:8080", "http://192.168.1.20:8765"):
        with pytest.raises(ValueError, match="https://"):
            HttpBookClient(address, "token")
    for address in ("http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765"):
        assert HttpBookClient(address, "token").host
    assert HttpBookClient("https://books.example.lan", "token").port == 443


def test_a_server_record_naming_another_address_is_not_trusted(hosted, tmp_path, monkeypatch):
    """Only a server writes a record, always at a loopback address; any other address
    would be sent the computer account's device token."""
    monkeypatch.setattr("breadsched.api.discovery.runtime_directory", lambda: tmp_path / "run")
    lock = hosted.db.writer_lock_token or ""
    book = str(hosted.path)
    for address in ("http://collector.example:80", "https://127.0.0.1:8443"):
        publish_server(book, address, lock)
        assert find_server(book) is None
    publish_server(book, hosted.base, lock)
    found = find_server(book)
    assert found is not None and found.url == hosted.base


# --------------------------------------------------------------------- caching


def test_the_books_data_is_never_kept_in_a_cache(hosted):
    token = hosted.httpd.token
    for path in ("/api/summary", "/api/v1/book"):
        status, _, headers = _request(hosted.base, "GET", path, token=token)
        assert status == 200
        assert headers["Cache-Control"] == "no-store", path
        assert headers["Referrer-Policy"] == "no-referrer"
    status, _, headers = _request(hosted.base, "GET", "/api/summary", token="wrong")
    assert status == 403 and headers["Cache-Control"] == "no-store"
