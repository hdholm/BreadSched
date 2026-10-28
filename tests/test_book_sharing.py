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
    record = publish_server(
        str(book_path), base, httpd.token, db.writer_lock_token, local_token=httpd.local_token
    )
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


def test_tags_work_through_the_server_holding_the_book(running, book_path, capsys):
    path = str(book_path)
    target = next(iter(running.db.iter_transactions()))
    assert main(["tags", path, "--transaction", target.handle[:12], "--set", "Trip, Work"]) == 0
    assert "Tags: Trip, Work" in capsys.readouterr().out
    assert running.db.get_transaction(target.handle).tags == ["Trip", "Work"]
    assert main(["tags", path, "--json"]) == 0
    assert {"tag": "Trip", "transactions": 1} in json.loads(capsys.readouterr().out)
    assert main(["tags", path, "trip", "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert row["handle"] == target.handle


def test_goals_work_through_the_server_holding_the_book(running, book_path, capsys):
    path = str(book_path)
    add = ["goals", path, "--add", "Holiday", "--account", "Savings", "--target", "1000"]
    assert main([*add, "--by", "2026-12-31", "--start", "2026-03-01", "--json"]) == 0
    handle = json.loads(capsys.readouterr().out)["handle"]
    assert running.db.get_savings_goal(handle).name == "Holiday"
    assert (
        main(["goals", path, "--allocate", "holiday", "--amount", "50", "--on", "2026-03-15"]) == 0
    )
    assert "Allocated 50 to Holiday" in capsys.readouterr().out
    assert main(["goals", path, "--as-of", "2026-03-15", "--json"]) == 0
    [goal] = json.loads(capsys.readouterr().out)["goals"]
    assert (goal["account_name"], goal["allocated"]) == ("Assets:Savings", "50.00")
    assert main(["goals", path, "--delete", handle[:8]]) == 0
    assert running.db.get_savings_goal(handle) is None


def test_receivables_work_through_the_server_holding_the_book(running, book_path, capsys):
    path = str(book_path)
    assert main(["receivables", path, "--add", "Mom", "--incurred", "2026-03-01", "--json"]) == 0
    handle = json.loads(capsys.readouterr().out)["handle"]
    assert running.db.get_receivable(handle).payer == "Mom"
    assert main(["receivables", path, "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert (row["handle"], row["payer"]) == (handle, "Mom")
    assert main(["receivables", path, "--dispute", handle[:8], "--on", "2026-03-05"]) == 0
    assert running.db.get_receivable(handle).disputed_on == date(2026, 3, 5)
    assert main(["receivables", path, "--delete", handle]) == 0
    assert running.db.get_receivable(handle) is None
    capsys.readouterr()


def test_scenarios_work_through_the_server_holding_the_book(running, book_path, capsys):
    path = str(book_path)
    assert main(["scenario", path, "save", "--name", "Lean", "--years", "3"]) == 0
    assert "Saved scenario 'Lean'" in capsys.readouterr().out
    assert main(["scenario", path, "list", "--json"]) == 0
    assert "Lean" in [row["name"] for row in json.loads(capsys.readouterr().out)]
    assert main(["drawdown", path, "list", "--scenario", "Lean", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["scenario", path, "delete", "--name", "Lean"]) == 0
    assert all(item.name != "Lean" for item in running.db.iter_scenarios())
    assert main(["scenario", path, "delete", "--name", "Lean"]) == 2
    assert "no scenario named 'Lean'" in capsys.readouterr().err


def test_payroll_works_through_the_server_holding_the_book(running, book_path, capsys):
    path = str(book_path)
    save = ["payroll", path, "--save-template", "Wages", "--from-schedule", "Sample wages"]
    assert main([*save, "--as-of", "2026-03-15"]) == 0
    assert "Saved payroll template 'Wages'" in capsys.readouterr().out
    assert [item["name"] for item in running.db.get_metadata("payroll_templates")] == ["Wages"]
    assert main(["payroll", path, "--as-of", "2026-03-15", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["name"] for item in listed["paychecks"]] == ["Sample wages"]
    assert [item["name"] for item in listed["templates"]] == ["Wages"]
    assert main(["payroll", path, "--show", "Sample wages", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["gross"] == "4200.00"
    change = ["payroll", path, "--pay-change", "Sample wages", "--gross", "4400"]
    before = repr([s.serialize() for s in running.db.iter_scheduled()])
    assert main([*change, "--start", "2027-01-01", "--preview"]) == 0
    assert "Pay change preview" in capsys.readouterr().out
    assert repr([s.serialize() for s in running.db.iter_scheduled()]) == before
    assert main(["payroll", path, "--delete-template", "wages"]) == 0
    assert running.db.get_metadata("payroll_templates") == []
    capsys.readouterr()


def test_claims_work_through_the_server_holding_the_book(running, book_path, capsys):
    from breadsched.gen.engine import fsa_claims
    from breadsched.gen.lib import (
        Account,
        AccountType,
        FsaClaim,
        FsaClaimAllocation,
        FsaFundingYear,
    )

    db, year = running.db, date(2026, 1, 1)
    assets = db.get_account_by_name("Assets")
    fsa = Account(name="Health FSA", atype=AccountType.FSA, parent=assets.handle)
    fsa.fsa.years = [FsaFundingYear(year, date(2026, 12, 31), Money("1500.00"))]
    with db.transaction("FSA") as txn:
        db.add_account(fsa, txn)
    claim = fsa_claims.save_claim(
        db,
        FsaClaim(
            service_date=date(2026, 3, 5),
            provider="Clinic",
            eob_responsibility=Money("80"),
            allocations=[FsaClaimAllocation(fsa.handle, year)],
        ),
    )
    path = str(book_path)
    assert main(["claims", path, "--as-of", "2026-03-15", "--json"]) == 0
    [line] = json.loads(capsys.readouterr().out)["claims"]
    assert (line["handle"], line["provider"]) == (claim.handle, "Clinic")
    assert main(["claims", path, "--years", "--as-of", "2026-03-15", "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)["years"]
    assert row["account"] == "Assets:Health FSA"
    assert main(["claims", path, "--close", claim.handle[:8], "--on", "2026-03-20"]) == 0
    assert "Closed the Clinic claim" in capsys.readouterr().out
    assert db.get_fsa_claim(claim.handle).closed_on == date(2026, 3, 20)
    assert main(["claims", path, "--history", claim.handle, "--json"]) == 0
    assert [e["kind"] for e in json.loads(capsys.readouterr().out)["events"]] == ["closed"]
    assert main(["claims", path, "--proposals", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["proposals"] == []


def test_review_works_through_the_server_holding_the_book(running, book_path, capsys):
    from breadsched.gen.lib.transaction import PlanningResolution

    path = str(book_path)
    assert main(["review", path, "--json"]) == 0
    pending = json.loads(capsys.readouterr().out)["transactions"]
    assert pending and all(item["no_candidate_reason"] for item in pending[:3])
    first = pending[0]["handle"]
    assert main(["plan-matches", path, first[:10], "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["plan-unexpected", path, first[:10]]) == 0
    assert f"Marked {first[:8]} as unexpected" in capsys.readouterr().out
    marked = running.db.get_transaction(first).planning_resolution
    assert marked is PlanningResolution.UNEXPECTED
    command = ["plan-unresolved", path, "--start", "2026-04-01", "--end", "2026-04-30"]
    assert main([*command, "--json"]) == 0
    assert "Sample wages" in {item["description"] for item in json.loads(capsys.readouterr().out)}


def test_held_imports_work_through_the_server_holding_the_book(running, book_path, capsys):
    assert main(["import-review", str(book_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["import-review", str(book_path)]) == 0
    assert "No GnuCash changes are held" in capsys.readouterr().out


def test_tax_marks_work_through_the_server_holding_the_book(running, book_path, capsys):
    path = str(book_path)
    assert main(["tax-marks", path, "--account", "Assets:Checking=on", "--json"]) == 0
    marks = json.loads(capsys.readouterr().out)["accounts"]
    [checking_mark] = [mark for mark in marks if mark["name"] == "Assets:Checking"]
    assert (checking_mark["relevant"], checking_mark["breadsched"]) == (True, True)
    checking = running.db.get_account_by_name("Assets:Checking")
    assert checking.planning.tax_relevant is True
    assert main(["tax-marks", path, "--tag", "medical=on"]) == 0
    assert "Tax-relevant tags: medical" in capsys.readouterr().out


def test_quotes_work_through_the_server_holding_the_book(running, book_path, capsys, monkeypatch):
    """The command fetches; the server holding the book only stores what it fetched."""
    from decimal import Decimal

    from breadsched.cli import quote_commands
    from breadsched.gen.services.quotes import FetchedQuote

    class Fetcher:
        def fetch(self, requests, currency):
            return [
                FetchedQuote(
                    item.symbol, item.source, Decimal("10.5"), currency, date(2026, 3, 14), "test"
                )
                for item in requests
            ], []

    monkeypatch.setattr(quote_commands, "_fetcher", Fetcher)
    path = str(book_path)
    from breadsched.gen.lib import Commodity

    commodity = Commodity(namespace="TSP", mnemonic="G", fraction=10000)
    with running.db.transaction("Commodity") as txn:
        running.db.add_commodity(commodity, txn)
    assert main(["quote-source", path, commodity.mnemonic, "tsp"]) == 0
    assert running.db.get_commodity(commodity.handle).quote_source == "tsp"
    capsys.readouterr()
    assert main(["quotes", path, "--json"]) == 0
    [stored] = json.loads(capsys.readouterr().out)["stored"]
    assert (stored["symbol"], stored["changed"]) == (commodity.mnemonic, True)
    assert [price.value for price in running.db.iter_prices(commodity.handle)][-1] == Money("10.5")


def test_rates_work_through_the_server_holding_the_book(running, book_path, capsys):
    from breadsched.gen.lib import Commodity

    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    with running.db.transaction("Euro") as txn:
        running.db.add_commodity(euro, txn)
    command = ["rate", str(book_path), "--from", "EUR", "--to", "USD", "--date", "2026-03-01"]
    assert main([*command, "--value", "1.08", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rate"] == "27/25"
    assert [price.value for price in running.db.iter_prices(commodity=euro.handle)] == [
        Money("1.08")
    ]
    assert main([*command, "--value", "0"]) == 2
    assert "The rate must be greater than zero" in capsys.readouterr().err


def test_attachments_work_through_the_server_holding_the_book(running, book_path, capsys, tmp_path):
    path = str(book_path)
    transaction = next(iter(running.db.iter_transactions()))
    receipt = tmp_path / "receipt.pdf"
    receipt.write_bytes(b"%PDF-1.4")
    add = ["attachments", path, "--transaction", transaction.handle, "--add", str(receipt)]
    assert main([*add, "--json"]) == 0
    stored = json.loads(capsys.readouterr().out)["attachments"][-1]
    assert running.db.get_transaction(transaction.handle).attachments[-1] == stored
    assert main(["attachments", path, "--transaction", transaction.handle, "--json"]) == 0
    [document] = json.loads(capsys.readouterr().out)["attachments"]
    assert (document["location"], document["present"]) == (stored, True)
    remove = ["attachments", path, "--transaction", transaction.handle, "--remove", stored]
    assert main(remove) == 0
    assert running.db.get_transaction(transaction.handle).attachments == []
    capsys.readouterr()


def test_net_worth_works_through_the_server_holding_the_book(running, book_path, capsys):
    path = str(book_path)
    command = ["net-worth", path, "--start", "2026-01-01", "--end", "2026-03-31"]
    assert main([*command, "--as-of", "2026-03-15", "--json"]) == 0
    points = json.loads(capsys.readouterr().out)["points"]
    assert [point["valued_on"] for point in points][-1] == "2026-03-15"
    change = ["net-worth-change", path, "--start", "2026-02-01", "--end", "2026-02-28"]
    assert main([*change, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["closing_on"] == "2026-02-28"


def test_projection_works_through_the_server_holding_the_book(running, book_path, capsys):
    path = str(book_path)
    assert main(["project", path, "--years", "1", "--json"]) == 0
    projected = json.loads(capsys.readouterr().out)
    assert projected
    assert main(["scenario", path, "save", "--name", "Lean", "--years", "1"]) == 0
    assert main(["scenario", path, "save", "--name", "Rich", "--years", "1"]) == 0
    capsys.readouterr()
    assert main(["compare", path, "Lean", "Rich", "--json"]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_gnucash_writeback_works_through_the_server_holding_the_book(running, book_path, capsys):
    path = str(book_path)
    assert main(["gnucash-writeback", path, "--keep-backups", "4", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"keep_backups": 4}
    assert running.db.get_metadata("gnucash.writeback.keep_backups") == 4
    # The sample book was not imported from GnuCash, so there is nothing to write to.
    assert main(["gnucash-writeback", path]) != 0
    assert "GnuCash" in capsys.readouterr().err


def test_scheduled_and_costs_work_through_the_server_holding_the_book(running, book_path, capsys):
    path = str(book_path)
    assert main(["scheduled", path, "--as-of", "2026-11-10", "--json"]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)
    costs = ["receivables", path, "--costs", "2026-01-01", "2026-12-31", "--json"]
    assert main(costs) == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_activity_works_through_the_server_holding_the_book(running, book_path, capsys):
    command = ["activity", str(book_path), "--start", "2026-01-01", "--end", "2026-03-31"]
    assert main([*command, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["start"] == "2026-01-01"


def test_the_dashboard_works_through_the_server_holding_the_book(running, book_path, capsys):
    command = ["dashboard", str(book_path), "--liquidity-days", "45", "--json"]
    assert main(command) == 0
    board = json.loads(capsys.readouterr().out)
    assert "summary" in board
    assert running.db.get_metadata("dashboard", {}).get("liquidity_days") != 45


def test_budget_jars_work_through_the_server_holding_the_book(running, book_path, capsys):
    command = ["jars", str(book_path), "--start", "2026-01-01", "--end", "2026-03-31"]
    assert main([*command, "--json"]) == 0
    jars = json.loads(capsys.readouterr().out)
    assert (jars["start"], jars["end"]) == ("2026-01-01", "2026-03-31")
    assert main([*command[:3], "2026-03-01", "--end", "2026-01-01"]) != 0
    capsys.readouterr()


def test_investment_reports_work_through_the_server_holding_the_book(running, book_path, capsys):
    path = str(book_path)
    assert main(["holdings", path, "--lots", "--json"]) == 0
    held = json.loads(capsys.readouterr().out)
    assert isinstance(held, list)
    assert main(["realized-gains", path, "--json"]) == 0
    assert set(json.loads(capsys.readouterr().out)) >= {"sales", "years", "problems"}
    assert main(["tax-year", path, "--year", "2026", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["year"] == 2026
    assert main(["realized-gains", path, "--account", "No Such Account"]) != 0
    assert "No Such Account" in capsys.readouterr().err


def test_import_and_infer_work_through_the_server_holding_the_book(
    running, book_path, capsys, tmp_path
):
    statement = tmp_path / "statement.qif"
    statement.write_bytes(
        b"!Account\nNShared checking\nTBank\n^\n!Type:Bank\nD03/04/2026\nT12.50\nPExample\n^\n"
    )
    path = str(book_path)
    assert main(["import", path, str(statement), "--json"]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["transactions_new"] == 1
    assert imported["source"] == str(statement)
    assert main(["infer", path, "--json"]) == 0
    suggestions = json.loads(capsys.readouterr().out)
    assert all(isinstance(item["confidence"], float) for item in suggestions)
    assert main(["infer", path, "--apply", "--min-confidence", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["considered"] == len(suggestions)


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
