"""Decision 0004, phase 3: one book host serves several books on one address."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import date

import pytest
from user_fixtures import new_transaction, person

from breadsched.api import ApiError
from breadsched.api.connect import open_book_api
from breadsched.api.discovery import find_server
from breadsched.api.host import HostedBook
from breadsched.api.http_client import HttpBookClient
from breadsched.api.users import UserDirectory, device_token
from breadsched.cli.main import main
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.sample_book import create_sample_book
from breadsched.web.server import serve


def _book(path) -> DbSQLite:
    create_sample_book(path, as_of=date(2026, 10, 1))
    db = DbSQLite()
    db.load(str(path))
    return db


@pytest.fixture
def host(tmp_path):
    """Two books, ``Home`` and ``Rental``, served by one host."""
    home = _book(tmp_path / "Home.breadsched")
    rental = _book(tmp_path / "Rental.breadsched")
    httpd = serve([home, rental], host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    ids = {
        "home": home.get_metadata("book.id"),
        "rental": rental.get_metadata("book.id"),
    }
    yield type(
        "Host", (), {"home": home, "rental": rental, "base": base, "httpd": httpd, "ids": ids}
    )
    httpd.shutdown()
    httpd.server_close()
    home.close()
    rental.close()


def _refused(call) -> tuple[int, str]:
    with pytest.raises(ApiError) as caught:
        call()
    return caught.value.status, caught.value.code


def test_one_host_serves_several_books_each_named_by_id(host):
    assert host.ids["home"] and host.ids["rental"] and host.ids["home"] != host.ids["rental"]
    assert sorted(host.httpd.books.values()) == sorted(host.ids.values())
    token = device_token()
    # A request that names no book, or one the host does not hold, is turned away.
    assert _refused(lambda: HttpBookClient(host.base, token).revision) == (400, "book.required")
    stranger = HttpBookClient(host.base, token, book_id="no-such-book")
    assert _refused(lambda: stranger.revision) == (404, "book.not_found")
    # Each request reaches the book it names, and only that book.
    home = HttpBookClient(host.base, token, book_id=host.ids["home"])
    rental = HttpBookClient(host.base, token, book_id=host.ids["rental"])
    assert home.book_id() == host.ids["home"] and rental.book_id() == host.ids["rental"]
    before = {item.handle for item in host.home.iter_transactions()}
    rental_before = {item.handle for item in host.rental.iter_transactions()}
    added = rental.save_transaction(new_transaction(host.rental, rental))
    assert host.rental.get_transaction(added.handle) is not None
    assert {item.handle for item in host.home.iter_transactions()} == before
    assert {item.handle for item in host.rental.iter_transactions()} == rental_before | {
        added.handle
    }
    # The computer account owns both.
    assert home.hosted_books() == (
        HostedBook(host.ids["home"], "Home", "owner"),
        HostedBook(host.ids["rental"], "Rental", "owner"),
    )


def test_each_book_keeps_its_own_change_stream(host):
    token = device_token()
    home = HttpBookClient(host.base, token, book_id=host.ids["home"])
    rental = HttpBookClient(host.base, token, book_id=host.ids["rental"])
    heard = []
    arrived = threading.Event()

    def listen(change):
        heard.append(change)
        arrived.set()

    stop = home.subscribe(listen)
    try:
        rental.save_transaction(new_transaction(host.rental, rental))
        home.save_transaction(new_transaction(host.home, home))
        assert arrived.wait(20)
        # The rental's change came first, but only the home book's reaches its stream.
        assert heard[0].revision == home.revision
        assert "transaction" in heard[0].kinds
    finally:
        stop()


def test_a_person_sees_and_uses_only_the_books_shared_with_them(host):
    directory = UserDirectory()
    token = person(directory, "vera", "viewer", host.ids["rental"])
    # The host holds two, so Vera names hers; she is listed only that one.
    vera = HttpBookClient(host.base, token, book_id=host.ids["rental"])
    assert vera.hosted_books() == (HostedBook(host.ids["rental"], "Rental", "viewer"),)
    assert vera.chart()
    assert _refused(lambda: vera.save_transaction(new_transaction(host.rental, vera))) == (
        403,
        "book.permission.denied",
    )
    # A book she has no role in is refused like any other request she may not make.
    other = HttpBookClient(host.base, token, book_id=host.ids["home"])
    assert _refused(lambda: other.revision) == (403, "request.untrusted")


def test_whom_a_host_does_not_know_learns_nothing_of_its_books(host):
    request = urllib.request.Request(
        host.base + "/api/v1/books", headers={"X-BreadSched-Token": "not-a-token"}
    )
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=10)
    assert refused.value.code == 403
    assert json.loads(refused.value.read())["code"] == "request.untrusted"
    # Nor which books exist: a stranger naming a missing book is refused the same way.
    request = urllib.request.Request(
        host.base + "/api/v1/book",
        headers={"X-BreadSched-Token": "not-a-token", "X-BreadSched-Book": "no-such-book"},
    )
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=10)
    assert refused.value.code == 403


def test_a_temporary_password_lists_no_books(host):
    directory = UserDirectory()
    directory.add_user("tess", "temporary-pass")
    token = directory.issue_token(directory.user("tess"), "Tess's phone")
    directory.grant(host.ids["home"], "tess", "viewer")
    assert _refused(lambda: HttpBookClient(host.base, token).hosted_books()) == (
        403,
        "request.untrusted",
    )


def test_a_book_cannot_be_served_twice_by_one_host(tmp_path):
    writer = _book(tmp_path / "Home.breadsched")
    reader = DbSQLite()
    reader.load(str(tmp_path / "Home.breadsched"), mode="r")
    try:
        with pytest.raises(ValueError, match="already served"):
            serve([writer, reader], host="127.0.0.1", port=0)
    finally:
        reader.close()
        writer.close()


def test_serve_holds_several_books_and_commands_reach_each_through_it(
    tmp_path, monkeypatch, capsys
):
    import breadsched.web.transport as transport

    home, rental = tmp_path / "Home.breadsched", tmp_path / "Rental.breadsched"
    for path in (home, rental):
        create_sample_book(path, as_of=date(2026, 10, 1))
    started = []
    real_serve = transport.serve

    def capture(*args, **kwargs):
        httpd = real_serve(*args, **kwargs)
        started.append(httpd)
        return httpd

    monkeypatch.setattr(transport, "serve", capture)
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(main(["serve", str(home), str(rental), "--port", "0"])),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 30
    while find_server(str(rental)) is None:
        assert thread.is_alive(), (result, capsys.readouterr())
        assert time.monotonic() < deadline, "the server never published its records"
        time.sleep(0.05)
    assert find_server(str(home)) is not None
    # One address; each record names its own book, and a command reaches that one.
    assert find_server(str(home)).url == find_server(str(rental)).url
    ids = {}
    for path in (home, rental):
        with open_book_api(str(path)) as api:
            assert isinstance(api, HttpBookClient)
            ids[path] = api.book_id()
            assert ids[path] == find_server(str(path)).book_id
    assert ids[home] != ids[rental]

    started[0].shutdown()
    thread.join(timeout=10)
    assert result == [0]
    assert find_server(str(home)) is None and find_server(str(rental)) is None
    out = capsys.readouterr().out
    assert "BreadSched is serving 2 books at" in out
    assert f"{rental}: book {ids[rental]}" in out


def test_a_host_opens_books_for_the_accounts_own_commands_and_lets_them_go(host, tmp_path):
    from breadsched.api.host import JoinedBooks

    third = tmp_path / "Cabin.breadsched"
    create_sample_book(third, as_of=date(2026, 10, 1))
    token = device_token()
    client = HttpBookClient(host.base, token)
    # A host that may not be joined (GTK's sharing) says there is nothing here.
    assert _refused(lambda: client.host_open(str(third))) == (404, "resource.not_found")
    host.httpd.joined = JoinedBooks(host.httpd, host.base)

    cabin = client.host_open(str(third))
    assert find_server(str(third)).book_id == cabin
    assert find_server(str(third)).url == host.base
    assert [book.name for book in client.hosted_books()] == ["Cabin", "Home", "Rental"]
    with open_book_api(str(third)) as api:
        assert api.book_id() == cabin
    client.host_close(cabin)
    assert [book.name for book in client.hosted_books()] == ["Home", "Rental"]
    assert find_server(str(third)) is None
    # The host let go of the book itself: it opens for writing here now.
    again = DbSQLite()
    again.load(str(third))
    again.close()

    # A book the host was started with stays; one already held, or missing, is refused.
    assert _refused(lambda: client.host_close(host.ids["home"])) == (404, "host.book.not_joined")
    assert _refused(lambda: client.host_open(host.home.path)) == (409, "host.book.held")
    missing = str(tmp_path / "missing.breadsched")
    assert _refused(lambda: client.host_open(missing)) == (404, "host.book.missing")
    # Nobody but the computer account may change what its host serves.
    phone = person(UserDirectory(), "vera", "owner", host.ids["home"])
    stranger = HttpBookClient(host.base, phone)
    assert _refused(lambda: stranger.host_open(str(third))) == (403, "host.local_only")


def test_a_second_serve_joins_the_running_host_until_it_stops(tmp_path, monkeypatch, capsys):
    import breadsched.cli.book_commands as book_commands
    import breadsched.web.transport as transport
    from breadsched.api.discovery import find_host

    home, rental = tmp_path / "Home.breadsched", tmp_path / "Rental.breadsched"
    for path in (home, rental):
        create_sample_book(path, as_of=date(2026, 10, 1))
    monkeypatch.setattr(book_commands, "HOST_CHECK_SECONDS", 0.05)
    started = []
    real_serve = transport.serve

    def capture(*args, **kwargs):
        httpd = real_serve(*args, **kwargs)
        started.append(httpd)
        return httpd

    monkeypatch.setattr(transport, "serve", capture)
    first: list[int] = []
    second: list[int] = []
    hosting = threading.Thread(
        target=lambda: first.append(main(["serve", str(home), "--port", "0"])), daemon=True
    )
    hosting.start()
    deadline = time.monotonic() + 30
    while find_host() is None:
        assert hosting.is_alive() and time.monotonic() < deadline, capsys.readouterr()
        time.sleep(0.05)
    joining = threading.Thread(
        target=lambda: second.append(main(["serve", str(rental), "--port", "9"])), daemon=True
    )
    joining.start()
    while find_server(str(rental)) is None:
        assert joining.is_alive() and time.monotonic() < deadline, capsys.readouterr()
        time.sleep(0.05)
    # One server for both: the second command started none of its own.
    assert len(started) == 1
    assert find_server(str(rental)).url == find_host().url
    with open_book_api(str(rental)) as api:
        assert api.book_id() == find_server(str(rental)).book_id
        assert [book.name for book in api.hosted_books()] == ["Home", "Rental"]

    # When the host stops, the joining command ends too, and the book is let go.
    started[0].shutdown()
    hosting.join(timeout=10)
    joining.join(timeout=10)
    assert first == [0] and second == [0]
    assert find_host() is None and find_server(str(rental)) is None
    out = capsys.readouterr().out
    assert f"now serves {rental}" in out
    assert "It keeps its own settings; --port did not apply." in out
    assert "The server stopped." in out
    again = DbSQLite()
    again.load(str(rental))
    again.close()
