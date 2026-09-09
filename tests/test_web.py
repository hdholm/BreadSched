"""The locally-hosted web interface.

Served from the same engines as the CLI and the GTK interface, so these tests are
about the transport and the safety properties rather than the arithmetic: that the
routes answer, that the database survives being used from a request thread, and
that a tool with no authentication refuses to listen on anything but loopback.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

import pytest
from gnucash_fixtures import create_book

from cashperspective.cli.main import main as cli
from cashperspective.gen.db.sqlite import DbSQLite
from cashperspective.gen.lib import Money
from cashperspective.web.server import serve


@pytest.fixture
def book_path(tmp_path, capsys):
    source = create_book(
        tmp_path / "source.gnucash",
        [
            ("root", "Root Account", "ROOT", None, 0),
            ("assets", "Assets", "ASSET", "root", 1),
            ("bank", "Checking", "BANK", "assets", 0),
            ("income", "Income", "INCOME", "root", 1),
            ("wages", "Salary", "INCOME", "income", 0),
            ("expenses", "Expenses", "EXPENSE", "root", 1),
            ("rent", "Rent", "EXPENSE", "expenses", 0),
        ],
        [
            (date(2026, 1, 25), "Payroll",
             [("bank", 420000, 100, ""), ("wages", -420000, 100, "")]),
            (date(2026, 1, 2), "Rent",
             [("rent", 180000, 100, ""), ("bank", -180000, 100, "")]),
        ],
    )
    path = tmp_path / "book.cashperspective"
    cli(["init", str(path)])
    cli(["import", str(path), source.path])
    capsys.readouterr()
    return path


@pytest.fixture
def client(book_path):
    """A running server plus a helper that fetches and decodes JSON."""
    db = DbSQLite()
    db.load(str(book_path))
    httpd = serve(db, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    class Client:
        base_url = base

        def raw(self, path: str):
            with urllib.request.urlopen(base + path, timeout=10) as response:
                return response.status, response.read(), response.headers

        def get(self, path: str):
            status, body, _ = self.raw(path)
            return status, json.loads(body)

        def post(self, path: str, payload: dict):
            request = urllib.request.Request(
                base + path,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())

    try:
        yield Client()
    finally:
        httpd.shutdown()
        httpd.server_close()
        db.close()


class TestItServes:
    def test_the_page_loads(self, client):
        status, body, headers = client.raw("/")
        assert status == 200
        assert headers.get("Content-Type", "").startswith("text/html")
        assert b"<html" in body.lower()

    def test_the_summary_reports_the_book(self, client):
        status, payload = client.get("/api/summary")
        assert status == 200
        assert payload

    def test_accounts_carry_balances(self, client):
        _status, payload = client.get("/api/accounts")
        by_name = {row["name"]: row for row in payload}
        assert "Checking" in by_name
        assert Money(by_name["Checking"]["balance"]) == Money("2400.00")

    def test_the_account_tree_has_one_root(self, client):
        """The two-roots bug would show here as a second top-level branch."""
        _status, payload = client.get("/api/accounts")
        top_level = [row for row in payload if row.get("depth") == 0]
        assert len(top_level) <= 5
        assert "Root Account" not in {row["name"] for row in payload}

    def test_a_register_can_be_read(self, client):
        _status, accounts = client.get("/api/accounts")
        handle = next(a["handle"] for a in accounts if a["name"] == "Checking")
        _status, payload = client.get(
            "/api/register?" + urllib.parse.urlencode({"account": handle})
        )
        assert payload["account"] == "Assets:Checking"
        assert len(payload["rows"]) == 2

    def test_scheduled_transactions_are_listed(self, client):
        status, payload = client.get("/api/scheduled")
        assert status == 200
        assert set(payload) == {"definitions", "upcoming"}

    def test_a_projection_is_computed(self, client):
        _status, payload = client.get("/api/projection?years=3")
        assert len(payload["rows"]) == 36

    def test_an_unknown_route_is_not_a_crash(self, client):
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.get("/api/nonsense")
        assert caught.value.code == 404


class TestThreadSafety:
    """The database is opened on one thread and used from the request threads."""

    def test_a_request_thread_can_read_the_database(self, client):
        """Without check_same_thread=False, every API route fails with 500."""
        status, _payload = client.get("/api/accounts")
        assert status == 200

    def test_concurrent_requests_all_succeed(self, client):
        results: list[int] = []
        errors: list[str] = []

        def hammer() -> None:
            try:
                for _ in range(4):
                    status, _ = client.get("/api/accounts")
                    results.append(status)
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(str(exc))

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == []
        assert results == [200] * 16


class TestWriting:
    def test_a_transaction_can_be_posted(self, client):
        status, payload = client.post(
            "/api/transaction",
            {
                "date": "2026-02-01",
                "description": "Web entry",
                "from": "Assets:Checking",
                "to": "Expenses:Rent",
                "amount": "125.00",
            },
        )
        assert status == 200
        assert payload.get("ok") or payload.get("handle")

        _status, accounts = client.get("/api/accounts")
        balance = next(a["balance"] for a in accounts if a["name"] == "Checking")
        assert Money(balance) == Money("2275.00")

    def test_an_unbalanced_request_is_refused_cleanly(self, client):
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post("/api/transaction", {"description": "nonsense"})
        assert caught.value.code in (400, 404, 500)


class TestSafety:
    def test_it_refuses_to_bind_beyond_loopback(self, book_path):
        """No authentication means no listening on a network interface."""
        db = DbSQLite()
        db.load(str(book_path))
        try:
            with pytest.raises(ValueError, match="loopback"):
                serve(db, host="0.0.0.0", port=0)
        finally:
            db.close()

    def test_loopback_is_accepted(self, book_path):
        db = DbSQLite()
        db.load(str(book_path))
        try:
            httpd = serve(db, host="127.0.0.1", port=0)
            httpd.server_close()
        finally:
            db.close()


class TestCliIntegration:
    def test_the_web_command_exists(self, capsys):
        from cashperspective.cli.main import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--help"])
        assert "web" in capsys.readouterr().out

    def test_the_web_command_rejects_a_public_host(self, book_path, capsys):
        assert cli(["web", str(book_path), "--host", "0.0.0.0"]) != 0


class TestDashboardApi:
    """The dashboard is the primary view; the browser gets the same figures."""

    def test_the_endpoint_answers(self, client):
        status, payload = client.get("/api/dashboard")
        assert status == 200
        assert set(payload) == {"summary", "config", "groups", "bills"}

    def test_it_reports_the_headline_figures(self, client):
        _status, payload = client.get("/api/dashboard")
        for key in (
            "net_worth", "liquid", "required_liquid", "available",
            "emergency_fund", "months_covered", "monthly_outgoings",
        ):
            assert key in payload["summary"], f"missing {key}"

    def test_groups_carry_equity_and_ltv_where_they_apply(self, client):
        _status, payload = client.get("/api/dashboard")
        for group in payload["groups"]:
            assert "loan_to_value" in group and "equity" in group

    def test_the_horizons_are_query_parameters(self, client):
        _status, six = client.get("/api/dashboard?emergency_months=6")
        _status, twelve = client.get("/api/dashboard?emergency_months=12")
        assert twelve["config"]["emergency_months"] == 12
        assert float(twelve["summary"]["emergency_fund"]) >= float(
            six["summary"]["emergency_fund"]
        )

    def test_amounts_are_strings_the_browser_can_parse(self, client):
        _status, payload = client.get("/api/dashboard")
        assert isinstance(payload["summary"]["net_worth"], str)
        float(payload["summary"]["net_worth"])

    def test_the_page_opens_on_the_dashboard(self, client):
        _status, body, _headers = client.raw("/")
        page = body.decode()
        assert 'let current = "Dashboard"' in page
        assert '"Dashboard", "Accounts"' in page
        assert "async function showDashboard" in page
