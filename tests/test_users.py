"""Decision 0004, phase 1: the book host knows who a request comes from."""

from __future__ import annotations

import getpass
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import date

import pytest

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
    assert set(record) == {"book", "url", "pid", "host", "lock"}
    for token in (hosted.httpd.token, hosted.httpd.local_token):
        assert token not in json.dumps(record)
