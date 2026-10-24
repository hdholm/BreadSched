"""Several interfaces share one book through the server holding it (#358).

**Experimental** (branch ``claude/experimental-book-server``). A writable
``breadsched serve`` or ``breadsched web`` publishes a user-only server record;
book-server commands for the same book find it and work through that server
instead of failing on the writer lock. A record whose process is gone, or no
longer holds the lock, is ignored and removed.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
from datetime import date
from pathlib import Path

import pytest

from breadsched.api.connect import open_book_api
from breadsched.api.discovery import (
    ServerRecord,
    find_server,
    publish_server,
    record_path,
    withdraw_server,
)
from breadsched.api.http_client import HttpBookClient
from breadsched.api.local import LocalBookServer
from breadsched.cli.main import main
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.lib import Money
from breadsched.gen.sample_book import create_sample_book
from breadsched.gen.utils.user_paths import runtime_directory
from breadsched.web.server import serve

AS_OF = date(2026, 9, 27)


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    """Keep every server record in this test's own runtime directory."""
    root = tmp_path / "runtime"
    monkeypatch.setattr("breadsched.api.discovery.runtime_directory", lambda: root)
    return root


@pytest.fixture
def book_path(tmp_path):
    path = tmp_path / "shared.breadsched"
    create_sample_book(path, as_of=date(2026, 3, 15))
    return path


@pytest.fixture
def running(book_path):
    """The book held by a web server in this process, published as ``serve`` does."""
    db = DbSQLite()
    db.load(str(book_path))
    httpd = serve(db, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    record = publish_server(str(book_path), base, httpd.token, db.writer_lock_token)
    yield type("Running", (), {"db": db, "base": base, "token": httpd.token, "record": record})
    withdraw_server(record)
    httpd.shutdown()
    httpd.server_close()
    db.close()


def test_the_runtime_directory_is_per_user_and_per_platform(tmp_path):
    home = tmp_path / "home"
    linux = {"XDG_RUNTIME_DIR": "/run/user/1000"}
    assert runtime_directory(environ=linux, home=home, platform="linux") == Path(
        "/run/user/1000/breadsched"
    )
    assert runtime_directory(environ={}, home=home, platform="linux") == (
        home / ".local" / "share" / "breadsched" / "run"
    )
    windows = {"LOCALAPPDATA": str(tmp_path / "local"), "XDG_RUNTIME_DIR": "/ignored"}
    assert runtime_directory(environ=windows, home=home, platform="win32") == (
        tmp_path / "local" / "breadsched" / "run"
    )


def test_records_live_in_the_runtime_directory(book_path, runtime):
    assert record_path(str(book_path)).parent == runtime / "servers"


def test_a_published_server_is_found_while_it_holds_the_book(running, book_path):
    found = find_server(str(book_path))
    assert found == running.record
    assert found.url == running.base and found.pid == os.getpid()
    # The same book reached by another spelling finds the same record.
    other = book_path.parent / "." / book_path.name
    assert find_server(str(other)) == running.record


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_record_is_readable_only_by_its_user(running, book_path):
    path = record_path(str(book_path))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert running.token not in book_path.parent.joinpath(f"{book_path.name}.lock").read_text()


def test_a_stale_record_is_ignored_and_removed(book_path):
    path = record_path(str(book_path))
    # No process holds the book's writer lock.
    publish_server(str(book_path), "http://127.0.0.1:9", "secret", "no-such-lock")
    assert find_server(str(book_path)) is None
    assert not path.exists()
    # A process that has exited.
    path.parent.mkdir(parents=True, exist_ok=True)
    gone = {
        "book": str(book_path.resolve()),
        "url": "http://127.0.0.1:9",
        "token": "secret",
        "pid": 2**22 + 12345,
        "host": socket.gethostname(),
        "lock": "no-such-lock",
    }
    path.write_text(json.dumps(gone), encoding="utf-8")
    assert find_server(str(book_path)) is None and not path.exists()
    # Unreadable records are ignored.
    path.write_text("not json", encoding="utf-8")
    assert find_server(str(book_path)) is None


def test_the_holder_is_the_lock_token_not_the_host_name(running, book_path, monkeypatch):
    """A host name can change while a server runs; the lock token still names it."""
    monkeypatch.setattr(socket, "gethostname", lambda: "renamed-host")
    assert find_server(str(book_path)) == running.record
    # A record naming another lock token is stale even though its process is alive.
    path = record_path(str(book_path))
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**data, "lock": "another-writer"}), encoding="utf-8")
    assert find_server(str(book_path)) is None and not path.exists()


def test_withdrawing_removes_only_the_record_it_published(running, book_path):
    impostor = ServerRecord(
        running.record.book, running.base, "other", os.getpid(), "elsewhere", "lock"
    )
    withdraw_server(impostor)
    assert find_server(str(book_path)) == running.record
    withdraw_server(running.record)
    assert find_server(str(book_path)) is None


def test_open_book_api_prefers_the_server_holding_the_book(running, book_path):
    with open_book_api(str(book_path)) as api:
        assert isinstance(api, HttpBookClient)
        assert api.revision == running.db.revision()
    withdraw_server(running.record)
    with open_book_api(str(book_path), "r") as api:
        assert isinstance(api, LocalBookServer)


def test_commands_work_through_the_server_holding_the_book(running, book_path, capsys):
    path = str(book_path)
    as_of = ["--as-of", AS_OF.isoformat()]
    start = running.db.revision()

    assert main(["due-review", path, *as_of, "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert any(item["name"] == "Sample rent" for item in listed["schedules"])

    # A write goes to the server's book: no second writer is opened.
    assert main(["scheduled-next", path, "Sample rent", *as_of, "--skip", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["skipped"] == 1
    assert running.db.revision() == start + 1
    rent = next(item for item in running.db.iter_scheduled() if item.name == "Sample rent")
    assert date(2026, 4, 6) in rent.skipped

    assert main(["scheduled-advance", path, "Sample rent", "--remind-days", "3", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["remind_days"] == 3

    # Without the record the command opens the book itself, and the lock refuses it.
    withdraw_server(running.record)
    assert main(["scheduled-next", path, "Sample rent", *as_of, "--skip"]) == 2
    assert "already open for writing" in capsys.readouterr().err


def test_ledger_commands_work_through_the_server_holding_the_book(running, book_path, capsys):
    path = str(book_path)
    start = running.db.revision()
    add = ["add", path, "--date", "2026-03-20", "--description", "Corner shop"]
    add += ["--from", "Assets:Checking", "--to", "Groceries", "--amount", "14.50", "--json"]
    assert main(add) == 0
    handle = json.loads(capsys.readouterr().out)["handle"]
    assert running.db.get_transaction(handle).description == "Corner shop"
    assert running.db.revision() == start + 1

    assert main(["register", path, "Expenses:Groceries", "--limit", "1", "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert (row["handle"], row["amount"], row["transfer"]) == (handle, "14.50", "Assets:Checking")

    assert main(["edit", path, handle[:10], "--amount", "16", "--description", "Shop"]) == 0
    assert "-> 2026-03-20 'Shop'" in capsys.readouterr().out
    edited = running.db.get_transaction(handle)
    assert sorted(split.value for split in edited.splits) == [Money("-16"), Money("16")]

    assert main(["balance", path, "Groceries", "--as-of", "2026-03-20", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["account"] == "Expenses:Groceries"
    assert main(["balance", path, "--json"]) == 0
    assert set(json.loads(capsys.readouterr().out)) >= {"cash", "net_worth"}

    assert main(["delete", path, handle]) == 0
    assert "Deleted 2026-03-20 'Shop'" in capsys.readouterr().out
    assert running.db.get_transaction(handle) is None
    assert running.db.revision() == start + 3


def test_account_commands_work_through_the_server_holding_the_book(running, book_path, capsys):
    path = str(book_path)
    add = ["account", path, "add", "--name", "Travel", "--type", "EXPENSE"]
    assert main([*add, "--parent", "Expenses", "--json"]) == 0
    handle = json.loads(capsys.readouterr().out)["handle"]
    assert running.db.full_name(running.db.get_account(handle)) == "Expenses:Travel"

    assert main(["account", path, "edit", "--name", "Expenses:Travel", "--rename", "Trips"]) == 0
    assert "Edited Expenses:Trips" in capsys.readouterr().out
    assert main(["account", path, "list", "--json"]) == 0
    assert "Expenses:Trips" in [row["name"] for row in json.loads(capsys.readouterr().out)]
    assert main(["accounts", path, "--json"]) == 0
    chart = json.loads(capsys.readouterr().out)
    assert next(row for row in chart if row["handle"] == handle)["balance"] == "0.00"

    assert main(["account", path, "remove", "--name", "Expenses:Trips"]) == 0
    assert running.db.get_account(handle) is None
    assert main(["account", path, "remove", "--name", "Assets:Checking"]) == 2
    assert "breadsched:" in capsys.readouterr().err


def test_rules_work_through_the_server_holding_the_book(running, book_path, capsys):
    path = str(book_path)
    add = ["rules", path, "--add-description", "Corner Grocer", "--category", "Groceries"]
    assert main(add) == 0
    assert "Added rule: corner grocer -> Expenses:Groceries" in capsys.readouterr().out
    assert main(["rules", path, "--json"]) == 0
    [rule] = json.loads(capsys.readouterr().out)
    assert (rule["position"], rule["category_name"]) == (1, "Expenses:Groceries")
    assert main(["rules", path, "--preview", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["rules", path, "--move", "1", "--to", "2"]) == 2
    assert "position" in capsys.readouterr().err
    assert main(["rules", path, "--delete", "1"]) == 0
    capsys.readouterr()
    assert running.db.get_metadata("categorization_rules", []) == []


def test_a_server_can_be_named_explicitly(running, book_path, tmp_path, capsys, monkeypatch):
    withdraw_server(running.record)
    token_file = tmp_path / "token"
    token_file.write_text(running.token + "\n", encoding="utf-8")
    command = ["scheduled-next", str(book_path), "Sample rent", "--as-of", AS_OF.isoformat()]

    assert (
        main([*command, "--server", running.base, "--token-file", str(token_file), "--json"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["date"] == "2026-04-06"

    monkeypatch.setenv("BREADSCHED_SERVER_TOKEN", running.token)
    assert main([*command, "--server", running.base, "--json"]) == 0
    capsys.readouterr()

    monkeypatch.delenv("BREADSCHED_SERVER_TOKEN")
    assert main([*command, "--server", running.base]) == 2
    assert "needs a token" in capsys.readouterr().err
    monkeypatch.setenv("BREADSCHED_SERVER_TOKEN", "wrong")
    assert main([*command, "--server", running.base]) == 2
    assert "request.untrusted" in capsys.readouterr().err


def test_starting_a_server_does_no_reverse_name_lookup(book_path, monkeypatch):
    """``HTTPServer`` looked up the loopback address's name on every start, which
    stalls where reverse DNS is slow; serve's record then appeared too late."""

    def refuse(*_args):
        raise AssertionError("socket.getfqdn was called")

    monkeypatch.setattr(socket, "getfqdn", refuse)
    db = DbSQLite()
    db.load(str(book_path))
    try:
        httpd = serve(db, host="127.0.0.1", port=0)
        assert httpd.server_name == "127.0.0.1" and httpd.server_port > 0
        httpd.server_close()
    finally:
        db.close()


def test_serve_publishes_its_record_until_it_stops(book_path, monkeypatch, capsys):
    import breadsched.web.transport as transport

    started = []
    real_serve = transport.serve

    def capture(*args, **kwargs):
        httpd = real_serve(*args, **kwargs)
        started.append(httpd)
        return httpd

    monkeypatch.setattr(transport, "serve", capture)
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(main(["serve", str(book_path), "--port", "0"])), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 30
    while find_server(str(book_path)) is None:
        assert thread.is_alive(), (result, capsys.readouterr())
        assert time.monotonic() < deadline, "the server never published its record"
        time.sleep(0.05)
    record = find_server(str(book_path))
    assert record.url.endswith(f":{started[0].server_port}")
    with open_book_api(str(book_path)) as api:
        assert [review.name for review in api.due_review(AS_OF)][:1] == ["Sample wages"]

    started[0].shutdown()
    thread.join(timeout=10)
    assert result == [0]
    assert find_server(str(book_path)) is None
    assert "Other BreadSched commands for this book use this server" in capsys.readouterr().out
