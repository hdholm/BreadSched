"""An import can be limited to a date range and to chosen accounts.

A GnuCash book has rent and groceries, each paid from checking in January and in
March. A limited import takes only the transactions inside the dates and accounts
asked for. Every other source transaction is left exactly as the book has it: not
added, not refreshed, and never taken for one deleted in GnuCash. Without dates,
an import can start at the day the source was last imported.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from gnucash_fixtures import create_book, new_guid, write_transaction

from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.plug import ImportLimit, last_import_date
from breadsched.gen.services.imports import ImportBook, import_book, import_start_date
from breadsched.presentation import service_error_message

JANUARY, MARCH = date(2026, 1, 10), date(2026, 3, 10)


@pytest.fixture
def source(tmp_path):
    return create_book(
        tmp_path / "source.gnucash",
        [
            ("root", "Root Account", "ROOT", None, 0),
            ("assets", "Assets", "ASSET", "root", 1),
            ("bank", "Checking", "BANK", "assets", 0),
            ("expenses", "Expenses", "EXPENSE", "root", 1),
            ("rent", "Rent", "EXPENSE", "expenses", 0),
            ("food", "Groceries", "EXPENSE", "expenses", 0),
        ],
        [
            (JANUARY, "January rent", [("rent", 90000, 100, ""), ("bank", -90000, 100, "")]),
            (JANUARY, "January food", [("food", 5000, 100, ""), ("bank", -5000, 100, "")]),
            (MARCH, "March rent", [("rent", 90000, 100, ""), ("bank", -90000, 100, "")]),
            (MARCH, "March food", [("food", 6000, 100, ""), ("bank", -6000, 100, "")]),
        ],
    )


@pytest.fixture
def db():
    database = DbSQLite.create_memory()
    yield database
    database.close()


def _descriptions(db):
    return sorted(txn.description for txn in db.iter_transactions())


def _run(db, source, **limits):
    outcome = import_book(db, ImportBook(str(source.path), notify=False, **limits))
    assert outcome.ok, outcome.errors
    return outcome.value.result


def test_the_limit_takes_dates_inclusively_and_any_chosen_account():
    limit = ImportLimit(date(2026, 2, 1), date(2026, 2, 28), frozenset({"a"}))
    assert limit.takes(date(2026, 2, 1), ["a", "b"]) and limit.takes(date(2026, 2, 28), ["a"])
    assert not limit.takes(date(2026, 1, 31), ["a"])
    assert not limit.takes(date(2026, 3, 1), ["a"])
    assert not limit.takes(date(2026, 2, 10), ["b"])
    assert limit.describe() == "dated 2026-02-01 through 2026-02-28 in 1 chosen account"
    assert ImportLimit(date(2026, 2, 1)).describe() == "dated 2026-02-01 and later"
    assert not ImportLimit() and ImportLimit(until=date(2026, 1, 1))


def test_a_date_range_takes_only_what_is_dated_inside_it(db, source):
    result = _run(db, source, since=date(2026, 3, 1))

    assert _descriptions(db) == ["March food", "March rent"]
    assert (result.transactions_new, result.transactions_outside) == (2, 2)
    assert result.limited_to == "dated 2026-03-01 and later"
    assert "2 other(s) in the source were left as they are" in result.detail()
    # The chart of accounts is imported whatever the limit.
    assert {account.name for account in db.iter_accounts()} >= {"Checking", "Rent", "Groceries"}

    earlier = _run(db, source, until=date(2026, 1, 31))
    assert earlier.transactions_new == 2 and len(_descriptions(db)) == 4


def test_chosen_accounts_take_their_descendants_too(db, source):
    _run(db, source, until=date(2025, 1, 1))  # the chart only
    assert _descriptions(db) == []

    _run(db, source, accounts=(source.rent,))
    assert _descriptions(db) == ["January rent", "March rent"]

    _run(db, source, accounts=(source.expenses,), since=date(2026, 3, 1))
    assert _descriptions(db) == ["January rent", "March food", "March rent"]


def test_what_is_outside_the_limit_is_neither_refreshed_nor_taken_for_deleted(db, source):
    _run(db, source)
    assert len(_descriptions(db)) == 4
    with sqlite3.connect(source.path) as raw:
        raw.execute("UPDATE transactions SET description='Edited' WHERE description='January rent'")
        raw.execute("UPDATE transactions SET description='Changed' WHERE description='March rent'")
        gone = raw.execute(
            "SELECT guid FROM transactions WHERE description='March food'"
        ).fetchone()[0]
        raw.execute("DELETE FROM splits WHERE tx_guid=?", (gone,))
        raw.execute("DELETE FROM transactions WHERE guid=?", (gone,))

    result = _run(db, source, since=date(2026, 3, 1))

    # March's edit arrives and its deletion is mirrored; January is untouched.
    assert _descriptions(db) == ["Changed", "January food", "January rent"]
    assert (result.transactions_refreshed, result.transactions_removed) == (1, 1)
    assert result.transactions_outside == 2

    # A later import of everything catches January up, and removes nothing more.
    full = _run(db, source)
    assert _descriptions(db) == ["Changed", "Edited", "January food"]
    assert full.transactions_removed == 0 and full.limited_to == ""


def test_an_import_can_start_at_the_day_the_source_was_last_imported(db, source):
    assert import_start_date(db, str(source.path)) is None
    # Never imported: "since the last import" takes everything.
    first = _run(db, source, since_last_import=True)
    assert first.transactions_new == 4 and first.limited_to == ""
    today = date.today()
    assert import_start_date(db, str(source.path)) == today
    path = Path(source.path)
    assert last_import_date(db, path.with_name("other.gnucash")) is None
    # An upload of the same file under another folder is recognised by its name.
    assert last_import_date(db, path.parent / "uploads" / path.name) == today

    with sqlite3.connect(source.path) as raw:
        write_transaction(
            raw,
            new_guid(),
            source.currency,
            today,
            "Today",
            [(source.food, 700, 100, ""), (source.bank, -700, 100, "")],
        )
    again = _run(db, source, since_last_import=True)
    assert again.transactions_new == 1 and again.transactions_outside == 4
    assert again.limited_to == f"dated {today.isoformat()} and later"
    # An explicit first date wins over the remembered one.
    assert _run(db, source, since=JANUARY, since_last_import=True).transactions_outside == 0


@pytest.mark.parametrize(
    ("limits", "code"),
    [
        (dict(since=MARCH, until=JANUARY), "import.dates.reversed"),
        (dict(accounts=("missing",)), "import.account.not_found"),
    ],
)
def test_a_limit_that_cannot_apply_is_refused_before_anything_is_read(db, source, limits, code):
    outcome = import_book(db, ImportBook(str(source.path), notify=False, **limits))

    assert [error.code for error in outcome.errors] == [code]
    assert service_error_message(outcome.errors[0]) != code
    assert list(db.iter_accounts()) == [] and import_start_date(db, str(source.path)) is None


def test_the_command_line_limits_an_import_and_starts_at_the_last_one(source, tmp_path, capsys):
    import json

    from breadsched.cli.main import main

    book = str(tmp_path / "cli.breadsched")
    assert main(["init", book]) == 0
    capsys.readouterr()
    command = ["import", book, str(source.path), "--no-infer", "--json"]

    assert main([*command, "--since", "2026-02-01", "--until", "2026-03-31"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert (first["transactions_new"], first["transactions_outside"]) == (2, 2)
    assert first["limited_to"] == "dated 2026-02-01 through 2026-03-31"

    # The default starts at today, the day of that import: nothing older is taken.
    assert main(command) == 0
    again = json.loads(capsys.readouterr().out)
    assert (again["transactions_new"], again["transactions_outside"]) == (0, 4)

    assert main([*command, "--since", "all", "--account", "Rent"]) == 0
    rent = json.loads(capsys.readouterr().out)
    assert (rent["transactions_new"], rent["limited_to"]) == (1, "in 1 chosen account")
    assert main([*command, "--since", "all"]) == 0
    assert json.loads(capsys.readouterr().out)["transactions_new"] == 1

    assert main([*command, "--since", "2026-03-01", "--until", "2026-01-01"]) != 0
    assert "before the first" in capsys.readouterr().err


def test_the_browser_and_the_book_api_limit_an_import_alike(source, tmp_path):
    import json
    import threading
    import urllib.error
    import urllib.request

    from breadsched.api.http_client import HttpBookClient
    from breadsched.web.server import serve

    book = DbSQLite()
    book.load(str(tmp_path / "web.breadsched"))
    httpd = serve(book, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def call(path, payload=None):
        request = urllib.request.Request(
            base + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "X-BreadSched-Token": httpd.token,
                "X-BreadSched-Local": httpd.local_token,
            },
            method="GET" if payload is None else "POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        path = str(source.path)
        assert call("/api/import")[1] == {"path": "", "last_imported": None}
        status, first = call("/api/import", {"path": path, "since": "2026-03-01"})
        assert status == 200
        assert (first["limited_to"], first["outside"]) == ("dated 2026-03-01 and later", 2)
        assert "left as they are" in first["detail"]
        today = date.today().isoformat()
        assert call("/api/import")[1] == {"path": path, "last_imported": today}

        # The default starts at that import; "all" compares every date.
        status, default = call("/api/import", {"path": path})
        assert status == 200 and default["outside"] == 4
        rent = next(a.handle for a in book.iter_accounts() if a.name == "Rent")
        status, chosen = call("/api/import", {"path": path, "since": "all", "accounts": [rent]})
        assert status == 200 and chosen["limited_to"] == "in 1 chosen account"
        assert sorted(t.description for t in book.iter_transactions()) == [
            "January rent",
            "March food",
            "March rent",
        ]

        # Refused limits change nothing.
        before = len(list(book.iter_transactions()))
        for payload in (
            {"path": path, "since": "soon"},
            {"path": path, "accounts": "Rent"},
            {"path": path, "since": "2026-03-01", "until": "2026-01-01"},
        ):
            assert call("/api/import", payload)[0] == 400, payload
        # An account that is not in the book is a missing object.
        missing = {"path": path, "since": "all", "accounts": ["missing"]}
        assert call("/api/import", missing)[0] == 404
        assert len(list(book.iter_transactions())) == before

        client = HttpBookClient(base, httpd.token, local_token=httpd.local_token)
        assert client.import_start_date(path) == date.today()
        assert client.import_start_date(str(tmp_path / "never.gnucash")) is None
        everything = client.import_book(ImportBook(path, notify=False))
        assert everything.result.transactions_new == 1 and everything.result.limited_to == ""
    finally:
        httpd.shutdown()
        httpd.server_close()
        book.close()
