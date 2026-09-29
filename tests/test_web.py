"""The locally-hosted web interface.

Served from the same engines as the CLI and the GTK interface, so these tests are
about the transport and the safety properties rather than the arithmetic: that the
routes answer, that the database survives being used from a request thread, and
that a tool with no authentication refuses to listen on anything but loopback.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from decimal import Decimal

import pytest
from gnucash_fixtures import create_book

from breadsched.cli.main import main as cli
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.lib import (
    Account,
    AccountType,
    Commodity,
    CommodityPrice,
    InvestmentActivityKind,
    Money,
    PeriodType,
    PlanningFlowKind,
    Recurrence,
    ScheduledAmountChange,
    ScheduledOccurrenceAdjustment,
    ScheduledSplit,
    ScheduledTransaction,
    Split,
    Transaction,
)
from breadsched.web.dashboard_resource import dashboard_report
from breadsched.web.projection_resource import (
    projection_comparison_report,
    projection_month_report,
    projection_report,
)
from breadsched.web.resources import GET_ROUTES, QueryParams
from breadsched.web.scenario_resource import scenarios_report
from breadsched.web.server import serve


def raw_http(client, request: bytes) -> tuple[int, dict]:
    """Send one exact HTTP/1.0 request without urllib normalizing its framing."""
    parsed = urllib.parse.urlparse(client.base_url)
    assert parsed.hostname is not None and parsed.port is not None
    with socket.create_connection((parsed.hostname, parsed.port), timeout=10) as connection:
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        response = b""
        while chunk := connection.recv(65536):
            response += chunk
    head, body = response.split(b"\r\n\r\n", 1)
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    return status, json.loads(body)


@pytest.fixture
def book_path(tmp_path, capsys):
    source = create_book(
        tmp_path / "source.gnucash",
        [
            ("root", "Root Account", "ROOT", None, 0),
            ("assets", "Assets", "ASSET", "root", 1),
            ("bank", "Checking", "BANK", "assets", 0),
            ("retirement", "401(k)", "ASSET", "assets", 0),
            ("income", "Income", "INCOME", "root", 1),
            ("wages", "Salary", "INCOME", "income", 0),
            ("expenses", "Expenses", "EXPENSE", "root", 1),
            ("rent", "Rent", "EXPENSE", "expenses", 0),
        ],
        [
            (
                date(2026, 1, 25),
                "Payroll",
                [("bank", 420000, 100, ""), ("wages", -420000, 100, "")],
            ),
            (date(2026, 1, 2), "Rent", [("rent", 180000, 100, ""), ("bank", -180000, 100, "")]),
        ],
    )
    path = tmp_path / "book.breadsched"
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
        token = httpd.token
        database = db

        def raw(self, path: str):
            with urllib.request.urlopen(base + path, timeout=10) as response:
                return response.status, response.read(), response.headers

        def get(self, path: str):
            request = urllib.request.Request(
                base + path, headers={"X-BreadSched-Token": self.token}
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())

        def post(self, path: str, payload: dict):
            request = urllib.request.Request(
                base + path,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-BreadSched-Token": self.token,
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())

        def upload(self, filename: str, content: bytes, **options):
            query = urllib.parse.urlencode({"filename": filename, **options})
            request = urllib.request.Request(
                base + "/api/import/upload?" + query,
                data=content,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-BreadSched-Token": self.token,
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read())

    try:
        yield Client()
    finally:
        httpd.shutdown()
        httpd.server_close()
        db.close()


@pytest.fixture
def review_client(book_path):
    """A web book with one unresolved actual and one nearby planned bill."""
    db = DbSQLite()
    db.load(str(book_path))
    bank = db.get_account_by_name("Checking")
    rent = db.get_account_by_name("Rent")
    assert bank is not None and rent is not None
    planned = ScheduledTransaction(
        name="Estimated rent",
        description="Estimated rent",
        recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 2, 5)),
        splits=[
            ScheduledSplit(rent.handle, Money("1800.00")),
            ScheduledSplit(bank.handle, Money("-1800.00")),
        ],
    )
    actual = Transaction(post_date=date(2026, 2, 6), description="Actual rent")
    actual.add_split(Split(rent.handle, Money("1825.00")))
    actual.add_split(Split(bank.handle, Money("-1825.00")))
    with db.transaction("review fixture") as txn:
        db.add_scheduled(planned, txn)
        db.add_transaction(actual, txn)

    httpd = serve(db, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    class Client:
        actual_handle = actual.handle
        occurrence = planned.occurrence_key(date(2026, 2, 5))
        token = httpd.token

        def get(self, path: str):
            request = urllib.request.Request(
                base + path, headers={"X-BreadSched-Token": self.token}
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())

        def post(self, path: str, payload: dict):
            request = urllib.request.Request(
                base + path,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-BreadSched-Token": self.token,
                },
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
        policy = headers["Content-Security-Policy"]
        assert "default-src 'none'" in policy
        assert "script-src 'self'" in policy
        assert "style-src 'self'" in policy
        assert "frame-ancestors 'none'" in policy
        assert "'unsafe-inline'" not in policy

    def test_the_current_view_has_a_printable_browser_presentation(self, client):
        _status, body, _headers = client.raw("/")
        page = body.decode("utf-8")
        _status, body, _headers = client.raw("/app.js")
        script = body.decode("utf-8")
        _status, body, _headers = client.raw("/style.css")
        style = body.decode("utf-8")

        assert 'id="print-view"' in page
        assert 'src="/app.js"' in page
        assert 'href="/style.css"' in page
        assert "window.print()" in script
        assert "@media print" in style
        assert "document.body.dataset.view = current" in script
        assert 'document.getElementById("print-title").textContent = current' in script
        assert ".plan-table { max-height: none; }" in style
        assert "Include category detail when printing" in script
        assert "body.include-plan-detail .plan-detail" in style
        assert "header { display: none !important; }" in style
        assert ".plan-table thead th { position: static; }" in style

    def test_static_assets_need_no_inline_code_or_html_svg_interpolation(self, client):
        _status, body, _headers = client.raw("/")
        page = body.decode("utf-8")
        _status, body, _headers = client.raw("/app.js")
        script = body.decode("utf-8")

        assert "<style" not in page
        assert "<script>" not in page
        assert "onclick=" not in page
        assert "style=" not in page
        assert "innerHTML" not in script
        assert 'createElementNS("http://www.w3.org/2000/svg"' in script

    def test_tables_accept_value_rows_and_group_totals_format_amounts(self, client):
        """Issue #132 guard for runtimes without a browser (see test_web_browser.py)."""
        _status, body, _headers = client.raw("/app.js")
        script = body.decode("utf-8")

        assert "Array.isArray(row)" in script
        assert "dashboardMoney(g.equity === null ? g.total : g.equity)" not in script

    def test_accounts_offer_read_only_imported_metadata_details(self, client):
        _status, body, _headers = client.raw("/app.js")
        page = body.decode("utf-8")
        assert "openAccountDetails" in page
        assert "Read-only GnuCash provenance" in page

    def test_schedule_occurrence_controls_are_structured(self, client):
        _status, body, _headers = client.raw("/app.js")
        text = body.decode("utf-8")
        assert "timelineEditor" in text
        assert "occurrenceTimelineEditor" in text
        assert "Add date and amount" in text
        assert "Add skipped occurrence" in text
        assert "Add occurrence amount" in text
        assert "Future amounts use YYYY-MM-DD=amount" not in text

    def test_the_summary_reports_the_book(self, client):
        status, payload = client.get("/api/summary")
        assert status == 200
        assert payload

    def test_verify_reports_a_clean_book(self, client):
        from breadsched import __version__

        status, payload = client.get("/api/verify")
        assert status == 200
        assert payload == {
            "ok": True,
            "application_version": __version__,
            "native_schema_version": 10,
            "supported_schema_versions": {"minimum": 6, "maximum": 10},
            "sqlite": [],
            "issues": [],
        }

    def test_accounts_carry_balances(self, client):
        _status, payload = client.get("/api/accounts")
        by_name = {row["name"]: row for row in payload}
        assert "Checking" in by_name
        assert Money(by_name["Checking"]["balance"]) == Money("2400.00")

    def test_accounts_disclose_direct_foreign_currency_quote_and_missing_fallback(self, client):
        assets = client.database.get_account_by_name("Assets")
        usd = client.database.get_commodity_by_mnemonic("USD")
        assert assets is not None and usd is not None
        euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
        account = Account(
            name="Foreign cash",
            atype=AccountType.BANK,
            parent=assets.handle,
            commodity=euro.handle,
        )
        clearing = Account(
            name="Foreign clearing",
            atype=AccountType.BANK,
            parent=assets.handle,
            commodity=euro.handle,
        )
        transaction = Transaction(post_date=date(2026, 1, 2), description="Foreign transfer")
        transaction.currency = euro.handle
        transaction.splits = [
            Split(account.handle, Money(10)),
            Split(clearing.handle, Money(-10)),
        ]
        with client.database.transaction("Foreign cash") as txn:
            client.database.add_commodity(euro, txn)
            client.database.add_account(account, txn)
            client.database.add_account(clearing, txn)
            client.database.add_transaction(transaction, txn)

        _status, rows = client.get("/api/accounts")
        missing = next(row for row in rows if row["handle"] == account.handle)
        parent = next(row for row in rows if row["handle"] == assets.handle)
        assert missing["missing_quote"] is True
        assert missing["currency"] == "EUR"
        assert parent["balance"] is None
        _status, summary = client.get("/api/summary")
        assert summary["net_worth"] is None
        assert summary["cash"] is None
        assert account.handle in summary["cash_missing_quotes"]
        assert account.handle in summary["net_worth_missing_quotes"]

        with client.database.transaction("Reverse exchange quote") as txn:
            client.database.add_price(
                CommodityPrice(
                    commodity=usd.handle,
                    currency=euro.handle,
                    quote_date=date(2026, 2, 2),
                    value=Money("0.5"),
                    source="reverse-book",
                ),
                txn,
            )
        _status, rows = client.get("/api/accounts")
        inverse = next(row for row in rows if row["handle"] == account.handle)
        assert inverse["balance"] == "20.00"
        assert inverse["conversion_path"] == "inverse"
        assert inverse["price_date"] == "2026-02-02"
        assert inverse["price_source"] == "reverse-book"
        assert inverse["quote_age_days"] == (date.today() - date(2026, 2, 2)).days
        _status, summary = client.get("/api/summary")
        assert summary["net_worth_missing_quotes"] == []

        with client.database.transaction("Direct exchange quote") as txn:
            client.database.add_price(
                CommodityPrice(
                    commodity=euro.handle,
                    currency=usd.handle,
                    quote_date=date(2026, 2, 1),
                    value=Money(2),
                    source="imported-book",
                ),
                txn,
            )
        _status, rows = client.get("/api/accounts")
        converted = next(row for row in rows if row["handle"] == account.handle)
        assert converted["valuation_source"] == "currency"
        assert converted["price_date"] == "2026-02-01"
        assert converted["price_source"] == "imported-book"
        assert converted["quote_age_days"] == (date.today() - date(2026, 2, 1)).days
        assert converted["conversion_path"] == "direct"
        assert converted["missing_quote"] is False
        assert Money(converted["balance"]) == Money(20)
        _status, summary = client.get("/api/summary")
        assert summary["net_worth"] is not None
        assert summary["cash"] is not None
        assert summary["net_worth_missing_quotes"] == []

    def test_accounts_disclose_missing_and_imported_security_quote(self, client):
        assets = client.database.get_account_by_name("Assets")
        checking = client.database.get_account_by_name("Assets:Checking")
        usd = client.database.get_commodity_by_mnemonic("USD")
        assert assets is not None and checking is not None and usd is not None
        security = Commodity(namespace="FUND", mnemonic="FUNDX", fullname="Sample fund")
        holding = Account(
            name="Sample holding",
            atype=AccountType.INVESTMENT,
            parent=assets.handle,
            commodity=security.handle,
        )
        purchase = Transaction(post_date=date(2026, 2, 1), description="Sample purchase")
        purchase.currency = usd.handle
        purchase.splits = [
            Split(holding.handle, Money("100"), quantity=Money("2")),
            Split(checking.handle, Money("-100")),
        ]
        with client.database.transaction("Sample security holding") as txn:
            client.database.add_commodity(security, txn)
            client.database.add_account(holding, txn)
            client.database.add_transaction(purchase, txn)

        _status, rows = client.get("/api/accounts")
        missing = next(row for row in rows if row["handle"] == holding.handle)
        assert missing["missing_quote"] is True
        assert missing["price_source"] is None
        assert missing["valuation_source"] == "ledger"

        with client.database.transaction("Imported quote") as txn:
            client.database.add_price(
                CommodityPrice(
                    commodity=security.handle,
                    currency=usd.handle,
                    quote_date=date(2026, 3, 1),
                    value=Money("65"),
                    source="imported-book",
                ),
                txn,
            )
        _status, rows = client.get("/api/accounts")
        valued = next(row for row in rows if row["handle"] == holding.handle)
        assert valued["missing_quote"] is False
        assert valued["price_source"] == "imported-book"
        assert valued["price_date"] == "2026-03-01"
        assert Money(valued["balance"]) == Money("130")

    def test_accounts_disclose_a_security_quoted_in_another_currency(self, client):
        assets = client.database.get_account_by_name("Assets")
        checking = client.database.get_account_by_name("Assets:Checking")
        usd = client.database.get_commodity_by_mnemonic("USD")
        assert assets is not None and checking is not None and usd is not None
        euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
        security = Commodity(namespace="FUND", mnemonic="EUFND", fullname="Euro fund")
        holding = Account(
            name="Euro holding",
            atype=AccountType.INVESTMENT,
            parent=assets.handle,
            commodity=security.handle,
        )
        purchase = Transaction(post_date=date(2026, 2, 1), description="Euro purchase")
        purchase.currency = usd.handle
        purchase.splits = [
            Split(holding.handle, Money("100"), quantity=Money("2")),
            Split(checking.handle, Money("-100")),
        ]
        with client.database.transaction("Euro-quoted holding") as txn:
            client.database.add_commodity(euro, txn)
            client.database.add_commodity(security, txn)
            client.database.add_account(holding, txn)
            client.database.add_transaction(purchase, txn)
            client.database.add_price(
                CommodityPrice(
                    commodity=security.handle,
                    currency=euro.handle,
                    quote_date=date(2026, 3, 1),
                    value=Money("60"),
                    source="ofx",
                ),
                txn,
            )

        _status, rows = client.get("/api/accounts")
        missing = next(row for row in rows if row["handle"] == holding.handle)
        assert missing["missing_quote"] is True
        assert missing["balance"] is None
        assert missing["quote_evidence"] == (
            "2026-03-01 · ofx in EUR; no EUR→USD rate, value shown in EUR"
        )

        with client.database.transaction("Rate") as txn:
            client.database.add_price(
                CommodityPrice(
                    commodity=euro.handle,
                    currency=usd.handle,
                    quote_date=date(2026, 3, 2),
                    value=Money("1.25"),
                    source="bank",
                ),
                txn,
            )
        _status, rows = client.get("/api/accounts")
        valued = next(row for row in rows if row["handle"] == holding.handle)
        assert valued["missing_quote"] is False
        assert valued["currency"] == "EUR"
        assert Money(valued["balance"]) == Money("150")
        assert valued["quote_evidence"] == "2026-03-01 · ofx; EUR→USD 2026-03-02 · bank"

    def test_accounts_api_exposes_exact_imported_provenance(self, client):
        _status, payload = client.get("/api/accounts")
        checking = next(row for row in payload if row["name"] == "Checking")

        assert checking["source_guid"]
        assert checking["source_type"] == "BANK"
        assert checking["notes"] == ""
        assert checking["source_notes"] == ""
        assert checking["commodity_scu"] == 100
        assert checking["source_fields"] == [
            {
                "name": "account:non-standard-scu",
                "value_type": "boolean",
                "value": "false",
            }
        ]

    def test_plan_settings_and_totals_are_shared_through_the_book(self, client):
        _status, initial = client.get("/api/plan")
        status, controls = client.post(
            "/api/plan/settings",
            {
                "from": initial["controls"]["from"],
                "through": initial["controls"]["through"],
                "period": "quarter",
                "measure": "variance",
                "scenario": None,
                "compare": None,
            },
        )
        assert status == 200
        assert controls["from"] == initial["controls"]["from"]
        assert controls["through"] == initial["controls"]["through"]
        assert controls["period"] == "quarter"
        assert controls["measure"] == "variance"

        _status, plan = client.get("/api/plan")

        assert plan["controls"]["from"] == initial["controls"]["from"]
        assert plan["controls"]["through"] == initial["controls"]["through"]
        assert plan["controls"]["period"] == "quarter"
        assert plan["controls"]["measure"] == "variance"
        assert set(plan["column_totals"]) == {
            "income",
            "expense",
            "operating_net",
            "mortgage_payments",
            "planning_flows",
            "cash_bridge",
            "net_cash",
        }
        for row in plan["categories"]:
            assert set(row["totals"]) == {"planned", "actual", "variance"}

    def test_account_type_can_be_changed_without_losing_source_type(self, client):
        _status, accounts = client.get("/api/accounts")
        retirement = next(row for row in accounts if row["name"] == "401(k)")
        source_type = retirement["source_type"]

        status, payload = client.post(
            "/api/account/type",
            {"handle": retirement["handle"], "type": "RETIREMENT"},
        )
        assert status == 200
        assert payload["type"] == "RETIREMENT"

        _status, accounts = client.get("/api/accounts")
        retirement = next(row for row in accounts if row["name"] == "401(k)")
        assert retirement["type"] == "RETIREMENT"
        assert retirement["source_type"] == source_type

    def test_fsa_funding_years_can_be_configured(self, client):
        _status, accounts = client.get("/api/accounts")
        retirement = next(row for row in accounts if row["name"] == "401(k)")
        status, migrated = client.post(
            "/api/account/type",
            {"handle": retirement["handle"], "type": "FSA"},
        )
        assert status == 200
        assert migrated == {
            "handle": retirement["handle"],
            "type": "FSA",
        }
        status, payload = client.post(
            "/api/account/fsa-years",
            {
                "handle": retirement["handle"],
                "years": [
                    {
                        "start": "2026-07-01",
                        "through": "2027-06-30",
                        "election": "3000.00",
                        "runout_through": "2027-09-30",
                    }
                ],
            },
        )
        assert status == 200
        assert payload["years"][0]["start"] == "2026-07-01"
        _status, accounts = client.get("/api/accounts")
        fsa_account = next(row for row in accounts if row["name"] == "401(k)")
        assert fsa_account["fsa_years"][0]["election"] == "3000.00"

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
        assert payload["debit_label"] == "Deposit"
        assert payload["credit_label"] == "Withdrawal"
        assert len(payload["rows"]) == 2

    def test_reconciliation_uses_the_shared_statement_workflow(self, client):
        _status, accounts = client.get("/api/accounts")
        checking = next(a for a in accounts if a["name"] == "Checking")

        status, started = client.post(
            "/api/reconciliation/start",
            {
                "account": checking["handle"],
                "statement_date": date.today().isoformat(),
                "ending_balance": "0",
            },
        )
        assert status == 200
        assert started["status"] == "open"

        _status, state = client.get(
            "/api/reconciliation?" + urllib.parse.urlencode({"account": checking["handle"]})
        )
        current = state["open"]
        target = Money(current["opening_balance"]) + sum(
            (Money(item["amount"]) for item in current["candidates"]), Money(0)
        )
        status, updated = client.post(
            "/api/reconciliation/update",
            {
                "handle": current["handle"],
                "ending_balance": str(target.to_decimal()),
                "selected_splits": [item["split"] for item in current["candidates"]],
            },
        )
        assert status == 200
        assert updated["balanced"] is True

        status, completed = client.post(
            "/api/reconciliation/complete", {"handle": current["handle"]}
        )
        assert status == 200
        assert completed["status"] == "completed"

        status, reopened = client.post("/api/reconciliation/reopen", {"handle": current["handle"]})
        assert status == 200
        assert reopened["status"] == "open"

    def test_register_query_and_rejected_entries_keep_their_contract(self, client):
        def refused(call, *args) -> tuple[int, dict]:
            with pytest.raises(urllib.error.HTTPError) as caught:
                call(*args)
            return caught.value.code, json.loads(caught.value.read())

        assert refused(client.get, "/api/register")[0] == 400
        assert refused(client.get, "/api/register?account=nope")[0] == 404
        query = urllib.parse.urlencode({"account": "x", "limit": "0"})
        assert refused(client.get, f"/api/register?{query}")[0] == 400
        _status, accounts = client.get("/api/accounts")
        checking = next(a for a in accounts if a["name"] == "Checking")
        _status, register = client.get(
            "/api/register?" + urllib.parse.urlencode({"account": checking["handle"], "limit": 1})
        )
        assert len(register["rows"]) == 1

        before = sorted(item.handle for item in client.database.iter_transactions())
        for payload, code in (
            ({"from": "Checking", "to": "Expenses:Rent", "amount": "-5"}, 400),
            ({"from": "Checking", "to": "Nowhere", "amount": "5"}, 400),
            ({"description": "missing accounts"}, 400),
            ({"from": "Checking", "to": "Expenses:Rent", "amount": "5", "date": "x"}, 400),
        ):
            status, _error = refused(client.post, "/api/transaction", payload)
            assert status == code, payload
        assert sorted(item.handle for item in client.database.iter_transactions()) == before

    def test_rejected_reconciliation_requests_leave_the_statement_unchanged(self, client):
        def refused(call, *args) -> int:
            with pytest.raises(urllib.error.HTTPError) as caught:
                call(*args)
            return caught.value.code

        _status, accounts = client.get("/api/accounts")
        checking = next(a for a in accounts if a["name"] == "Checking")
        assert refused(client.get, "/api/reconciliation") == 400
        query = urllib.parse.urlencode({"account": checking["handle"], "extra": "1"})
        assert refused(client.get, f"/api/reconciliation?{query}") == 400

        bad_start = {
            "account": checking["handle"],
            "statement_date": "not a date",
            "ending_balance": "0",
        }
        assert refused(client.post, "/api/reconciliation/start", bad_start) == 400
        assert list(client.database.iter_reconciliations(checking["handle"])) == []

        status, started = client.post(
            "/api/reconciliation/start",
            {
                "account": checking["handle"],
                "statement_date": date.today().isoformat(),
                "ending_balance": "10",
            },
        )
        assert status == 200
        before = client.database.get_reconciliation(started["handle"])
        for payload in (
            {"handle": started["handle"], "selected_splits": "not a list"},
            {"handle": started["handle"], "selected_splits": ["no-such-split"]},
            {"handle": started["handle"], "selected_splits": [], "ending_balance": "abc"},
        ):
            assert refused(client.post, "/api/reconciliation/update", payload) in (400, 404, 409)
            after = client.database.get_reconciliation(started["handle"])
            assert after.selected_splits == before.selected_splits
            assert after.ending_balance == before.ending_balance
        unbalanced = {"handle": started["handle"]}
        assert refused(client.post, "/api/reconciliation/complete", unbalanced) in (400, 409)
        assert client.database.get_reconciliation(started["handle"]).status.value == "open"

    def test_scheduled_transactions_are_listed(self, client):
        status, payload = client.get("/api/scheduled")
        assert status == 200
        assert set(payload) == {"definitions", "accounts", "upcoming"}

    def test_card_payment_settings_supply_scheduled_and_upcoming_rows(self, client):
        db = client.database
        liabilities = db.get_account_by_name("Liabilities")
        checking = db.get_account_by_name("Assets:Checking")
        rent = db.get_account_by_name("Expenses:Rent")
        assert liabilities is not None and checking is not None and rent is not None
        card = Account(name="Household card", atype=AccountType.CREDIT, parent=liabilities.handle)
        with db.transaction("Add card with balance") as txn:
            db.add_account(card, txn)
            db.add_transaction(
                Transaction.simple(
                    date.today(), "Card purchase", rent.handle, card.handle, "125.00"
                ),
                txn,
            )

        status, saved = client.post(
            "/api/account/card",
            {
                "handle": card.handle,
                "pays_in_full": True,
                "usual_payment": "",
                "payment_day": "20",
                "payment_account": checking.handle,
            },
        )
        assert status == 200
        assert saved["card_payment_account"] == checking.handle

        _status, data = client.get("/api/scheduled?days=90")
        definition = next(
            item for item in data["definitions"] if item["linked_account"] == card.handle
        )
        assert definition["account_linked"] is True
        assert Money(definition["amount"]) == Money("125.00")
        occurrence = next(
            item for item in data["upcoming"] if item["linked_account"] == card.handle
        )
        assert occurrence["account_linked"] is True
        assert Money(occurrence["amount"]) == Money("125.00")

        _status, dashboard_data = client.get("/api/dashboard")
        pending = next(item for item in dashboard_data["bills"] if item["account"] == card.handle)
        assert pending["monthly"] is None
        assert pending["annual"] is None
        assert Money(pending["hold"]) == Money("125.00")

        status, changed = client.post(
            "/api/account/type",
            {"handle": card.handle, "type": "LIABILITY"},
        )
        assert status == 200
        assert changed["type"] == "LIABILITY"
        assert db.get_account(card.handle).card_payment_account is None

    def test_web_loan_preview_and_creation_use_the_shared_formula_engine(self, client):
        db = client.database
        liabilities = db.get_account_by_name("Liabilities")
        assert liabilities is not None
        liability = Account(
            name="Household loan", atype=AccountType.LOAN, parent=liabilities.handle
        )
        with db.transaction("Add loan account") as txn:
            db.add_account(liability, txn)

        _status, options = client.get("/api/loan/options")
        interest = next(item for item in options["expenses"] if item["name"].endswith(":Rent"))
        payment = next(
            item for item in options["payment_accounts"] if item["name"].endswith(":Checking")
        )
        values = {
            "name": "One-year household loan",
            "principal": "1200.00",
            "annual_rate": "0",
            "years": "1",
            "start": "2026-10-01",
            "liability": liability.handle,
            "interest_account": interest["handle"],
            "payment_account": payment["handle"],
            "opening_balance": True,
        }

        status, preview = client.post("/api/loan/preview", values)
        assert status == 200
        assert Money(preview["payment"]) == Money("100.00")
        assert len(preview["rows"]) == 12

        status, created = client.post("/api/loan/save", values)
        assert status == 200
        saved = db.get_scheduled(created["handle"])
        assert saved is not None
        assert all(split.formula for split in saved.splits)
        from breadsched.gen.engine import ledger

        assert ledger.balance(db, liability.handle) == Money("1200.00")

    def test_web_loan_mutation_exposes_stable_service_errors(self, client):
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post(
                "/api/loan/save",
                {
                    "name": " ",
                    "principal": "1200.00",
                    "annual_rate": "0",
                    "years": "1",
                    "start": "2026-10-01",
                    "liability": "missing",
                    "interest_account": "missing",
                    "payment_account": "missing",
                    "opening_balance": True,
                },
            )

        payload = json.loads(caught.value.read())
        assert caught.value.code == 400
        assert payload["code"] == "loan.name.required"
        assert payload["fields"] == ["name"]

    def test_scheduled_active_state_can_be_edited(self, client):
        _status, data = client.get("/api/scheduled")
        category = next(a for a in data["accounts"] if a["name"].endswith(":Rent"))
        funding = next(a for a in data["accounts"] if a["name"].endswith(":Checking"))
        values = {
            "name": "Recurring expense",
            "category": category["handle"],
            "funding": funding["handle"],
            "amount": "25.00",
            "frequency": "monthly",
            "start": "2026-01-01",
            "enabled": False,
        }
        status, created = client.post("/api/scheduled/save", values)
        assert status == 200
        _status, data = client.get("/api/scheduled")
        item = next(row for row in data["definitions"] if row["handle"] == created["handle"])
        assert item["enabled"] is False
        assert all(row["name"] != values["name"] for row in data["upcoming"])

        values.update(handle=created["handle"], enabled=True)
        status, _updated = client.post("/api/scheduled/save", values)
        assert status == 200
        _status, data = client.get("/api/scheduled")
        item = next(row for row in data["definitions"] if row["handle"] == created["handle"])
        assert item["enabled"] is True

    @pytest.mark.parametrize(
        ("frequency", "start", "period", "expected"),
        [
            (
                "nth_weekday",
                "2026-01-13",
                PeriodType.NTH_WEEKDAY,
                [date(2026, 1, 13), date(2026, 2, 10), date(2026, 3, 10)],
            ),
            (
                "last_weekday",
                "2026-01-06",
                PeriodType.LAST_WEEKDAY,
                [date(2026, 1, 27), date(2026, 2, 24), date(2026, 3, 31)],
            ),
        ],
    )
    def test_advanced_monthly_frequency_can_be_created_and_edited(
        self, client, frequency, start, period, expected
    ):
        _status, data = client.get("/api/scheduled")
        category = next(a for a in data["accounts"] if a["name"].endswith(":Rent"))
        funding = next(a for a in data["accounts"] if a["name"].endswith(":Checking"))
        values = {
            "name": "Weekday schedule",
            "category": category["handle"],
            "funding": funding["handle"],
            "amount": "25.00",
            "frequency": frequency,
            "start": start,
            "count": "3",
        }

        status, created = client.post("/api/scheduled/save", values)
        assert status == 200
        saved = client.database.get_scheduled(created["handle"])
        assert saved is not None
        assert saved.recurrence.period is period
        assert saved.recurrence.occurrences(date(2026, 3, 31)) == expected

        values.update(handle=created["handle"], amount="30.00")
        status, _updated = client.post("/api/scheduled/save", values)
        assert status == 200
        restored = client.database.get_scheduled(created["handle"])
        assert restored is not None
        assert restored.recurrence.period is period
        assert restored.recurrence.occurrences(date(2026, 3, 31)) == expected

    def test_scheduled_review_preserves_seasonal_amounts(self, client):
        _status, data = client.get("/api/scheduled")
        category = next(a for a in data["accounts"] if a["name"].endswith(":Rent"))
        funding = next(a for a in data["accounts"] if a["name"].endswith(":Checking"))
        status, created = client.post(
            "/api/scheduled/save",
            {
                "name": "Seasonal estimate",
                "category": category["handle"],
                "funding": funding["handle"],
                "amount": "25.00",
                "frequency": "monthly",
                "start": "2026-01-01",
                "placeholder": True,
                "seasonal_amounts": [
                    {"month": 1, "amount": "40.00"},
                    {"month": 7, "amount": "10.00"},
                ],
            },
        )
        assert status == 200
        _status, data = client.get("/api/scheduled")
        item = next(row for row in data["definitions"] if row["handle"] == created["handle"])
        assert item["seasonal_amounts"] == [
            {"month": 1, "amount": "40.00"},
            {"month": 7, "amount": "10.00"},
        ]
        assert all(row["schedule"] != created["handle"] for row in data["upcoming"])

    def test_occurrence_options_follow_recurrence_and_weekend_adjustment(self, client):
        status, payload = client.post(
            "/api/scheduled/occurrences",
            {
                "frequency": "monthly",
                "start": "2026-02-15",
                "count": "3",
                "weekend": "previous",
            },
        )
        assert status == 200
        assert payload["occurrences"] == [
            "2026-02-13",
            "2026-03-13",
            "2026-04-15",
        ]

    def test_occurrence_options_can_preview_effective_amounts_and_exceptions(self, client):
        status, payload = client.post(
            "/api/scheduled/occurrences",
            {
                "frequency": "monthly",
                "start": "2026-01-15",
                "count": "5",
                "weekend": "none",
                "amount": "1800.00",
                "amount_changes": [{"start": "2026-03-01", "amount": "1950.00"}],
                "skipped": ["2026-04-15"],
                "occurrence_adjustments": [{"when": "2026-05-15", "amount": "2300.00"}],
            },
        )
        assert status == 200
        assert [(row["when"], row["amount"], row["status"]) for row in payload["preview"]] == [
            ("2026-01-15", "1800.00", "Normal"),
            ("2026-02-15", "1800.00", "Normal"),
            ("2026-03-15", "1950.00", "Future amount"),
            ("2026-04-15", "1950.00", "Skipped"),
            ("2026-05-15", "2300.00", "One-time amount"),
        ]

    def test_simple_scheduled_transaction_can_be_created_and_edited(self, client):
        _status, data = client.get("/api/scheduled")
        category = next(a for a in data["accounts"] if a["name"].endswith(":Rent"))
        funding = next(a for a in data["accounts"] if a["name"].endswith(":Checking"))
        status, created = client.post(
            "/api/scheduled/save",
            {
                "name": "Internet",
                "placeholder": False,
                "growth_policy": "income",
                "category": category["handle"],
                "funding": funding["handle"],
                "category_memo": "service category",
                "funding_memo": "payment account",
                "amount": "95.00",
                "frequency": "monthly",
                "start": "2026-02-15",
                "end": "2026-12-31",
                "weekend": "next",
                "auto": False,
            },
        )
        assert status == 200
        handle = created["handle"]

        status, _updated = client.post(
            "/api/scheduled/save",
            {
                "handle": handle,
                "name": "Internet service",
                "placeholder": True,
                "growth_policy": "none",
                "category": category["handle"],
                "funding": funding["handle"],
                "planning_flow": "benefit_funding",
                "amount": "110.00",
                "amount_changes": [
                    {"start": "2026-05-15", "amount": "125.00"},
                    {"start": "2026-07-15", "amount": "140.00"},
                ],
                "split_amount_changes": [
                    {
                        "account": category["handle"],
                        "start": "2026-06-15",
                        "amount": "130.00",
                    },
                    {
                        "account": funding["handle"],
                        "start": "2026-06-15",
                        "amount": "-130.00",
                    },
                ],
                "skipped": ["2026-03-13"],
                "occurrence_adjustments": [{"when": "2026-04-15", "amount": "150.00"}],
                "frequency": "monthly",
                "start": "2026-02-15",
                "count": "6",
                "weekend": "previous",
                "auto": True,
            },
        )
        assert status == 200
        _status, refreshed = client.get("/api/scheduled")
        item = next(row for row in refreshed["definitions"] if row["handle"] == handle)
        assert item["name"] == "Internet service"
        assert Money(item["amount"]) == Money("110.00")
        assert item["placeholder"] is True
        assert item["auto"] is False
        assert item["growth_policy"] == "none"
        assert item["count"] == 6
        assert item["end"] is None
        assert item["weekend"] == "previous"
        assert item["planning_flow"] == "benefit_funding"
        assert item["category_memo"] == "service category"
        assert item["funding_memo"] == "payment account"
        assert item["amount_changes"] == [
            {"start": "2026-05-15", "amount": "125.00"},
            {"start": "2026-07-15", "amount": "140.00"},
        ]
        assert item["split_amount_changes"] == [
            {
                "account": category["handle"],
                "start": "2026-06-15",
                "amount": "130.00",
            },
            {
                "account": funding["handle"],
                "start": "2026-06-15",
                "amount": "-130.00",
            },
        ]
        restored = client.database.get_scheduled(handle)
        assert restored is not None
        assert dict(restored.resolved_splits(when=date(2026, 6, 15))) == {
            category["handle"]: Money("125.00"),
            funding["handle"]: Money("-125.00"),
        }
        assert item["skipped"] == ["2026-03-13"]
        assert item["occurrence_adjustments"] == [{"when": "2026-04-15", "amount": "150.00"}]
        _status, plan = client.get("/api/plan?from=2026-01&through=2026-12&period=month")
        benefit = next(row for row in plan["planning_flows"] if row["kind"] == "benefit_funding")
        assert Money(benefit["planned"][1]) == Money("-110.00")

        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post(
                "/api/scheduled/save",
                {
                    "handle": handle,
                    "name": "Changed name",
                    "category": category["handle"],
                    "funding": funding["handle"],
                    "amount": "110.00",
                    "frequency": "monthly",
                    "start": "2026-02-15",
                    "split_amount_changes": [
                        {"account": category["handle"], "start": "2026-06-15", "amount": "130"},
                        {"account": category["handle"], "start": "2026-06-15", "amount": "135"},
                    ],
                },
            )
        assert caught.value.code == 400
        unchanged = client.database.get_scheduled(handle)
        assert unchanged is not None
        assert unchanged.name == "Internet service"
        assert unchanged.serialize() == restored.serialize()

    def test_an_actual_can_seed_a_reviewable_unsaved_schedule(self, client):
        _status, accounts = client.get("/api/accounts")
        checking = next(account for account in accounts if account["name"] == "Checking")
        _status, register = client.get(
            "/api/register?" + urllib.parse.urlencode({"account": checking["handle"]})
        )
        rent = next(row for row in register["rows"] if row["description"] == "Rent")

        status, draft = client.post("/api/scheduled/draft", {"transaction": rent["handle"]})

        assert status == 200
        assert draft["handle"] is None
        assert draft["name"] == "Rent"
        assert draft["frequency_key"] == "once"
        assert draft["start"] == rent["date"]
        assert draft["account_handles"] == [draft["category"], draft["funding"]]

    def test_schedule_rejects_an_unknown_growth_policy(self, client):
        _status, data = client.get("/api/scheduled")
        category = next(a for a in data["accounts"] if a["name"].endswith(":Rent"))
        funding = next(a for a in data["accounts"] if a["name"].endswith(":Checking"))
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post(
                "/api/scheduled/save",
                {
                    "name": "Growth validation fixture",
                    "growth_policy": "not-a-policy",
                    "category": category["handle"],
                    "funding": funding["handle"],
                    "amount": "75.00",
                    "frequency": "monthly",
                    "start": "2026-02-01",
                    "weekend": "none",
                },
            )
        assert caught.value.code == 400
        payload = json.loads(caught.value.read())
        assert "growth policy" in payload["error"]

    def test_fixed_multisplit_schedule_balances_payroll_and_classifies_saving(self, client):
        _status, data = client.get("/api/scheduled")
        salary = next(a for a in data["accounts"] if a["name"].endswith(":Salary"))
        rent = next(a for a in data["accounts"] if a["name"].endswith(":Rent"))
        checking = next(a for a in data["accounts"] if a["name"].endswith(":Checking"))
        retirement = next(a for a in data["accounts"] if a["name"].endswith(":401(k)"))
        retirement_account = client.database.get_account(retirement["handle"])
        assert retirement_account is not None
        retirement_account.atype = AccountType.RETIREMENT
        with client.database.transaction("Classify retirement account") as txn:
            client.database.commit_account(retirement_account, txn)
        status, created = client.post(
            "/api/scheduled/save",
            {
                "name": "Payroll plan",
                "placeholder": True,
                "category": salary["handle"],
                "funding": checking["handle"],
                "amount": "5000.00",
                "additional_splits": [
                    {
                        "account": rent["handle"],
                        "amount": "1000.00",
                        "planning_flow": None,
                        "memo": "deduction",
                    },
                    {
                        "account": retirement["handle"],
                        "amount": "500.00",
                        "planning_flow": "retirement_saving",
                        "investment_activity": "contribution",
                        "memo": "employee contribution",
                    },
                ],
                "frequency": "monthly",
                "start": "2026-02-01",
                "count": "2",
                "weekend": "none",
            },
        )
        assert status == 200
        handle = created["handle"]
        _status, refreshed = client.get("/api/scheduled")
        item = next(row for row in refreshed["definitions"] if row["handle"] == handle)
        assert item["simple"] is True
        assert item["additional_splits"] == [
            {
                "account": rent["handle"],
                "amount": "1000.00",
                "memo": "deduction",
                "planning_flow": None,
            },
            {
                "account": retirement["handle"],
                "amount": "500.00",
                "memo": "employee contribution",
                "planning_flow": "retirement_saving",
                "investment_activity": "contribution",
            },
        ]

        _status, plan = client.get("/api/plan?from=2026-02&through=2026-03&period=month")
        retirement_flow = next(
            row for row in plan["planning_flows"] if row["kind"] == "retirement_saving"
        )
        assert Money(retirement_flow["planned"][0]) == Money("500.00")

    def test_fixed_schedule_can_directly_classify_an_investment_contribution(self, client):
        _status, data = client.get("/api/scheduled")
        checking = next(a for a in data["accounts"] if a["name"].endswith(":Checking"))
        holding = next(a for a in data["accounts"] if a["name"].endswith(":401(k)"))
        account = client.database.get_account(holding["handle"])
        assert account is not None
        account.atype = AccountType.INVESTMENT
        with client.database.transaction("Classify investment fixture") as txn:
            client.database.commit_account(account, txn)

        status, created = client.post(
            "/api/scheduled/save",
            {
                "name": "Direct contribution",
                "category": holding["handle"],
                "funding": checking["handle"],
                "amount": "250.00",
                "investment_activity": "contribution",
                "frequency": "monthly",
                "start": "2026-02-01",
            },
        )

        assert status == 200
        scheduled = client.database.get_scheduled(created["handle"])
        assert scheduled is not None
        assert scheduled.splits[0].amount == Money("250.00")
        assert scheduled.splits[0].investment_activity is InvestmentActivityKind.CONTRIBUTION
        _status, refreshed = client.get("/api/scheduled")
        item = next(row for row in refreshed["definitions"] if row["handle"] == created["handle"])
        assert item["simple"] is True
        assert item["investment_activity"] == "contribution"

    def test_two_leg_investment_income_keeps_the_holding_as_the_edit_anchor(self, client):
        _status, data = client.get("/api/scheduled")
        income = next(a for a in data["accounts"] if a["name"].endswith(":Salary"))
        holding = next(a for a in data["accounts"] if a["name"].endswith(":401(k)"))
        account = client.database.get_account(holding["handle"])
        assert account is not None
        account.atype = AccountType.INVESTMENT
        with client.database.transaction("Classify investment fixture") as txn:
            client.database.commit_account(account, txn)

        _status, created = client.post(
            "/api/scheduled/save",
            {
                "name": "Reinvested income",
                "category": holding["handle"],
                "funding": income["handle"],
                "amount": "18.00",
                "investment_activity": "dividend",
                "frequency": "quarterly",
                "start": "2026-03-31",
            },
        )
        _status, refreshed = client.get("/api/scheduled")
        item = next(row for row in refreshed["definitions"] if row["handle"] == created["handle"])

        assert item["category"] == holding["handle"]
        assert item["funding"] == income["handle"]
        assert item["investment_activity"] == "dividend"

    def test_scheduled_definition_can_be_deleted_without_removing_posted_history(self, client):
        _status, data = client.get("/api/scheduled")
        category = next(a for a in data["accounts"] if a["name"].endswith(":Rent"))
        funding = next(a for a in data["accounts"] if a["name"].endswith(":Checking"))
        _status, created = client.post(
            "/api/scheduled/save",
            {
                "name": "Temporary schedule",
                "category": category["handle"],
                "funding": funding["handle"],
                "amount": "40.00",
                "frequency": "monthly",
                "start": "2026-03-01",
            },
        )
        before_transactions = client.database.summary()["txn"]

        status, deleted = client.post("/api/scheduled/delete", {"handle": created["handle"]})

        assert status == 200
        assert deleted["handle"] == created["handle"]
        _status, refreshed = client.get("/api/scheduled")
        assert created["handle"] not in {item["handle"] for item in refreshed["definitions"]}
        assert client.database.summary()["txn"] == before_transactions

    def test_a_protected_schedule_can_be_duplicated_exactly(self, client):
        _status, data = client.get("/api/scheduled")
        category = next(a for a in data["accounts"] if a["name"].endswith(":Rent"))
        funding = next(a for a in data["accounts"] if a["name"].endswith(":Checking"))
        source = ScheduledTransaction(
            name="Protected source",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 3, 1)),
            splits=[
                ScheduledSplit(category["handle"], formula="external_value"),
                ScheduledSplit(funding["handle"], formula="-(external_value)"),
            ],
        )
        source.source_recurrence = {"period": "custom-cycle"}
        source.unsupported_reason = "custom source recurrence"
        with client.database.transaction("Protected source") as txn:
            client.database.add_scheduled(source, txn)

        status, definitions = client.get("/api/scheduled")
        row = next(item for item in definitions["definitions"] if item["handle"] == source.handle)
        assert status == 200
        assert row["editable"] is False
        assert row["editor_mode"] == "read_only"
        assert row["editability_reason"] == "custom source recurrence"

        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post("/api/scheduled/save", {"handle": source.handle})
        assert caught.value.code == 400
        assert json.loads(caught.value.read())["error"] == "custom source recurrence"

        status, copied = client.post(
            "/api/scheduled/duplicate",
            {"handle": source.handle, "name": "Reviewed protected copy"},
        )

        assert status == 200
        duplicate = client.database.get_scheduled(copied["handle"])
        assert duplicate is not None
        assert duplicate.handle != source.handle
        assert duplicate.source_recurrence == source.source_recurrence
        assert [split.formula for split in duplicate.splits] == [
            "external_value",
            "-(external_value)",
        ]
        assert duplicate.last_posted is None
        assert duplicate.skipped == []

    def test_web_fixed_editor_uses_shared_planning_and_balance_sheet_projection(self, client):
        _status, data = client.get("/api/scheduled")
        bank = next(a for a in data["accounts"] if a["name"].endswith(":Checking"))
        reserve = next(a for a in data["accounts"] if a["name"].endswith(":401(k)"))
        planning = ScheduledTransaction(
            name="Benefit funding",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 3, 1)),
            splits=[
                ScheduledSplit(
                    reserve["handle"],
                    "125",
                    memo="benefit",
                    planning_flow=PlanningFlowKind.BENEFIT_FUNDING,
                ),
                ScheduledSplit(bank["handle"], "-125", memo="cash"),
            ],
        )
        transfer = ScheduledTransaction(
            name="Reserve transfer",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 4, 1)),
            splits=[
                ScheduledSplit(bank["handle"], "-90", memo="source"),
                ScheduledSplit(reserve["handle"], "90", memo="destination"),
            ],
        )
        with client.database.transaction("Add shared projection fixtures") as txn:
            client.database.add_scheduled(planning, txn)
            client.database.add_scheduled(transfer, txn)

        _status, refreshed = client.get("/api/scheduled")
        planning_row = next(
            row for row in refreshed["definitions"] if row["handle"] == planning.handle
        )
        transfer_row = next(
            row for row in refreshed["definitions"] if row["handle"] == transfer.handle
        )
        assert planning_row["simple"] is True
        assert planning_row["category"] == reserve["handle"]
        assert transfer_row["simple"] is True
        assert transfer_row["category"] == reserve["handle"]

        for row, amount in ((planning_row, "140"), (transfer_row, "95")):
            status, _saved = client.post(
                "/api/scheduled/save",
                {
                    "handle": row["handle"],
                    "name": row["name"],
                    "category": row["category"],
                    "funding": row["funding"],
                    "amount": amount,
                    "frequency": row["frequency_key"],
                    "start": row["start"],
                    "weekend": row["weekend"],
                },
            )
            assert status == 200

        restored_planning = client.database.get_scheduled(planning.handle)
        restored_transfer = client.database.get_scheduled(transfer.handle)
        assert restored_planning is not None and restored_transfer is not None
        assert restored_planning.splits[0].planning_flow is PlanningFlowKind.BENEFIT_FUNDING
        assert [split.amount for split in restored_planning.splits] == [Money("140"), Money("-140")]
        assert [split.amount for split in restored_transfer.splits] == [Money("95"), Money("-95")]

    def test_web_formula_editor_validates_inputs_and_protects_owned_fields(self, client):
        _status, data = client.get("/api/scheduled")
        category = next(a for a in data["accounts"] if a["name"].endswith(":Rent"))
        funding = next(a for a in data["accounts"] if a["name"].endswith(":Checking"))
        source = ScheduledTransaction(
            name="Formula fixture",
            recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 1, 15)),
            splits=[
                ScheduledSplit(category["handle"], formula="base"),
                ScheduledSplit(funding["handle"], formula="-base"),
            ],
            amount_changes=[ScheduledAmountChange(date(2026, 7, 15), "300")],
            occurrence_adjustments=[ScheduledOccurrenceAdjustment(date(2026, 5, 15), "275")],
        )
        source.variables = {"base": "250"}
        with client.database.transaction("Add formula fixture") as txn:
            client.database.add_scheduled(source, txn)

        _status, definitions = client.get("/api/scheduled")
        row = next(item for item in definitions["definitions"] if item["handle"] == source.handle)
        assert row["editor_mode"] == "formula"
        assert [item["index"] for item in row["formula_splits"]] == [0, 1]

        status, _saved = client.post(
            "/api/scheduled/formula-save",
            {
                "handle": source.handle,
                "name": "Formula fixture updated",
                "formulas": [
                    {"index": 0, "formula": "base * factor"},
                    {"index": 1, "formula": "-(base * factor)"},
                ],
                "variables": {"base": "120", "factor": "2"},
                "frequency": "monthly",
                "start": "2026-02-15",
                "weekend": "none",
                "splits": [{"account": "crafted", "amount": "999"}],
                "amount_changes": [],
                "occurrence_adjustments": [],
            },
        )
        assert status == 200
        restored = client.database.get_scheduled(source.handle)
        assert restored is not None
        assert restored.name == "Formula fixture updated"
        assert restored.recurrence.start == date(2026, 2, 15)
        assert [split.account for split in restored.splits] == [
            category["handle"],
            funding["handle"],
        ]
        assert restored.variables == {"base": "120", "factor": "2"}
        assert restored.amount_changes[0].amount == Money("300")
        assert restored.occurrence_adjustments[0].amount == Money("275")

        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post(
                "/api/scheduled/formula-save",
                {
                    "handle": source.handle,
                    "name": restored.name,
                    "formulas": [{"index": 0, "formula": "unknown + 1"}],
                    "variables": {},
                    "frequency": "monthly",
                    "start": "2026-02-15",
                    "weekend": "none",
                },
            )
        assert caught.value.code == 400

    def test_a_projection_is_computed(self, client):
        _status, payload = client.get("/api/projection?years=3")
        assert len(payload["rows"]) == 36
        assert payload["scenario"]["name"] == "Base scenario"
        assert payload["scenario"]["years"] == 3

    def test_projection_report_resource_preserves_controls_and_summary(self, client):
        from breadsched.web.server import Api

        expected = projection_report(
            client.database, Api(client.database)._projection_draft(years=2), base=True
        )
        status, actual = client.get("/api/projection?years=2")
        assert status == 200
        assert actual == json.loads(json.dumps(expected, default=str))
        assert len(actual["rows"]) == 24

    def test_comparison_resource_preserves_accounting_deltas(self, client):
        from breadsched.web.server import Api

        _status, clone = client.post("/api/scenario/duplicate", {"handle": None})
        api = Api(client.database)
        primary = api._projection_draft(years=1)
        comparison = api._projection_draft(clone["handle"], years=1)
        expected = projection_comparison_report(
            client.database, primary, comparison, primary_base=True, comparison_base=False
        )
        status, actual = client.post(
            "/api/projection/compare",
            {"handle": None, "compare_handle": clone["handle"], "years": 1},
        )
        assert status == 200
        assert actual == json.loads(json.dumps(expected, default=str))
        assert actual["comparison"]["rows"][-1]["net_worth_delta"] == "0.00"

    def test_a_projection_month_can_be_explained(self, client):
        status, payload = client.post(
            "/api/projection/explain",
            {
                "handle": None,
                "years": 3,
                "assumptions": {
                    "income_growth": "0.03",
                    "expense_inflation": "0.025",
                    "investment_return": "0.06",
                    "cash_interest": "0.01",
                    "liability_interest": "0.0",
                },
                "month_index": 11,
            },
        )
        assert status == 200
        assert payload["label"].endswith("2026")
        assert set(payload["cash"]) == {"opening", "flow", "interest", "closing"}
        assert "accounts" in payload["holdings"]
        assert "accounts" in payload["liabilities"]
        assert payload["assumptions"]["investment_return"] == "0.06"
        assert payload["escrow_explanations"] == []

    def test_month_explanation_resource_matches_route_and_leaves_book_unchanged(self, client):
        from breadsched.web.server import Api

        api = Api(client.database)
        scenario = api._projection_draft(years=2)
        before = scenario.effective_assumptions().serialize()
        expected = projection_month_report(client.database, scenario, 5)
        status, actual = client.post(
            "/api/projection/explain",
            {"handle": None, "years": 2, "month_index": 5},
        )
        assert status == 200
        assert actual == json.loads(json.dumps(expected, default=str))
        assert actual["index"] == 5
        assert actual["cash"]["closing"] is not None
        assert api._projection_draft(years=2).effective_assumptions().serialize() == before

    def test_projection_draft_calculation_does_not_persist_saved_changes(self, client):
        _status, scenario = client.post("/api/scenario/duplicate", {"handle": None})
        handle = scenario["handle"]
        _status, calculated = client.post(
            "/api/projection/calculate",
            {
                "handle": handle,
                "years": 12,
                "assumptions": {
                    "income_growth": "0.01",
                    "expense_inflation": "0.02",
                    "investment_return": "0.09",
                    "cash_interest": "0.015",
                    "liability_interest": "0.03",
                },
            },
        )
        assert calculated["scenario"]["years"] == 12
        assert calculated["scenario"]["assumptions"]["investment_return"] == "0.09"

        _status, persisted = client.get(f"/api/projection?scenario={handle}")
        assert persisted["scenario"]["years"] == 10
        assert persisted["scenario"]["assumptions"]["investment_return"] == "0.06"

    def test_projection_compares_a_draft_with_another_scenario(self, client):
        _status, scenario = client.post("/api/scenario/duplicate", {"handle": None})
        handle = scenario["handle"]
        client.post(
            "/api/projection/save",
            {
                "handle": handle,
                "years": 10,
                "assumptions": {
                    "income_growth": "0.0",
                    "expense_inflation": "0.0",
                    "investment_return": "0.0",
                    "cash_interest": "0.0",
                    "liability_interest": "0.0",
                },
            },
        )
        status, payload = client.post(
            "/api/projection/compare",
            {
                "handle": None,
                "compare_handle": handle,
                "years": 3,
                "assumptions": {
                    "income_growth": "0.02",
                    "expense_inflation": "0.03",
                    "investment_return": "0.07",
                    "cash_interest": "0.01",
                    "liability_interest": "0.0",
                },
            },
        )
        assert status == 200
        assert payload["primary"]["scenario"]["name"] == "Base scenario"
        assert payload["comparison"]["scenario"]["handle"] == handle
        assert len(payload["comparison"]["rows"]) == 36
        assert "ending_net_worth" in payload["comparison"]["summary_delta"]
        assert "net_worth_delta" in payload["comparison"]["rows"][-1]

    def test_projection_can_compare_a_saved_scenario_with_base(self, client):
        _status, scenario = client.post("/api/scenario/duplicate", {"handle": None})
        handle = scenario["handle"]
        status, payload = client.post(
            "/api/projection/compare",
            {
                "handle": handle,
                "compare_handle": None,
                "years": 2,
                "assumptions": scenario["assumptions"],
            },
        )
        assert status == 200
        assert payload["primary"]["scenario"]["handle"] == handle
        assert payload["comparison"]["scenario"]["handle"] is None
        assert payload["comparison"]["scenario"]["name"] == "Base scenario"
        assert "assumptions" in payload["comparison"]["scenario"]
        assert "assumption_sources" in payload["comparison"]["scenario"]
        assert len(payload["comparison"]["rows"]) == 24

    def test_projection_compare_rejects_the_same_scenario(self, client):
        _status, scenario = client.post("/api/scenario/duplicate", {"handle": None})
        handle = scenario["handle"]
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post(
                "/api/projection/compare",
                {
                    "handle": handle,
                    "compare_handle": handle,
                    "years": 3,
                    "assumptions": scenario["assumptions"],
                },
            )
        assert caught.value.code == 400

    def test_projection_save_persists_saved_scenario_controls(self, client):
        _status, scenario = client.post("/api/scenario/duplicate", {"handle": None})
        handle = scenario["handle"]
        _status, saved = client.post(
            "/api/projection/save",
            {
                "handle": handle,
                "years": 14,
                "assumptions": {
                    "income_growth": "0.015",
                    "expense_inflation": "0.0275",
                    "investment_return": "0.055",
                    "cash_interest": "0.0125",
                    "liability_interest": "0.01",
                },
            },
        )
        assert saved["scenario"]["years"] == 14
        _status, persisted = client.get(f"/api/projection?scenario={handle}")
        assert persisted["scenario"]["years"] == 14
        assert persisted["scenario"]["assumptions"]["investment_return"] == "0.055"

    def test_projection_save_persists_base_assumptions_only(self, client):
        _status, saved = client.post(
            "/api/projection/save",
            {
                "handle": None,
                "years": 20,
                "assumptions": {
                    "income_growth": "0.02",
                    "expense_inflation": "0.03",
                    "investment_return": "0.07",
                    "cash_interest": "0.01",
                    "liability_interest": "0.0",
                },
            },
        )
        assert saved["scenario"]["years"] == 20
        _status, reopened = client.get("/api/projection")
        assert reopened["scenario"]["years"] == 10
        assert reopened["scenario"]["assumptions"]["investment_return"] == "0.07"

    def test_an_unknown_route_is_not_a_crash(self, client):
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.get("/api/nonsense")
        assert caught.value.code == 404


class TestPlanApi:
    """The web Plan is the same derived event-driven report as GTK."""

    def test_net_worth_history_values_each_month_end_from_the_shared_service(self, client):
        status, report = client.get("/api/net-worth-history?from=2026-01&through=2026-02")
        assert status == 200
        assert report["period"] == "month"
        assert [point["label"] for point in report["points"]][:1] == ["Jan 2026"]
        january = report["points"][0]
        assert Money(january["net_worth"]) == Money(january["assets"]) - Money(january["debts"])
        assets = sum(
            (Money(line["value"]) for line in january["lines"] if line["kind"] == "asset"),
            Money(0),
        )
        assert assets == Money(january["assets"])
        assert january["missing"] == []

        def refused(path: str) -> tuple[int, dict]:
            with pytest.raises(urllib.error.HTTPError) as caught:
                client.get(path)
            return caught.value.code, json.loads(caught.value.read())

        status, error = refused("/api/net-worth-history?from=2026-03&through=2026-01")
        assert status == 400 and error["code"] == "net_worth.range.invalid"
        status, error = refused("/api/net-worth-history?from=March")
        assert status == 400 and error["fields"] == ["from"]

    def test_net_worth_change_lists_the_postings_behind_a_history_point(self, client):
        _status, history = client.get("/api/net-worth-history?from=2026-01&through=2026-02")
        february = history["points"][1]
        status, change = client.get(
            f"/api/net-worth-change?from={february['start']}&through={february['end']}"
        )
        assert status == 200
        assert Money(change["change"]) == Money(february["change"])
        assert change["opening_on"] == "2026-01-31"
        assert Money(change["posted"]) + Money(change["revaluation"]) == Money(change["change"])
        assert sum((Money(item["effect"]) for item in change["postings"]), Money(0)) == Money(
            change["posted"]
        )
        assert change["csv"].splitlines()[-1].startswith(f"{change['closing_on']},Change,")

        def refused(path: str) -> tuple[int, dict]:
            with pytest.raises(urllib.error.HTTPError) as caught:
                client.get(path)
            return caught.value.code, json.loads(caught.value.read())

        status, error = refused("/api/net-worth-change?from=2026-02-01")
        assert status == 400 and error["fields"] == ["through"]
        status, error = refused("/api/net-worth-change?from=2026-02-28&through=2026-02-01")
        assert status == 400 and error["code"] == "net_worth.range.invalid"

    def test_expense_explorer_reconciles_category_and_merchant_actual(self, client):
        status, report = client.get("/api/expense-explorer?from=2026-01&through=2026-01")
        assert status == 200
        rent = next(row for row in report["categories"] if row["full_name"] == "Expenses:Rent")
        assert Money(rent["periods"][0]["actual"]) == Money(1800)
        assert Money(rent["periods"][0]["actual_to_date"]) == Money(1800)
        assert Money(rent["periods"][0]["remaining"]) == (
            Money(rent["periods"][0]["planned"]) - Money(1800)
        )
        assert Money(report["totals"][0]["actual"]) == Money(1800)
        [point] = report["spending"]
        assert Money(point["actual"]) == Money(1800)
        assert sum((Money(part["actual"]) for part in point["categories"]), Money(0)) == Money(1800)
        assert {part["name"] for part in point["categories"]} >= {"Expenses:Rent"}
        assert point["currency_incomplete"] is False
        # Income over time uses the same periods and reconciles to its own total.
        [earned] = report["income"]
        assert earned["label"] == point["label"]
        assert Money(earned["actual"]) > Money(0)
        assert sum((Money(part["actual"]) for part in earned["categories"]), Money(0)) == Money(
            earned["actual"]
        )
        assert all(part["name"].startswith("Income") for part in earned["categories"])
        # An income category drills down to its dated receipts, grouped by payer.
        source = report["income_categories"][-1]
        params = urllib.parse.urlencode(
            {"from": "2026-01", "through": "2026-01", "account": source["account"], "index": "0"}
        )
        status, received = client.get(f"/api/expense-explorer?{params}")
        assert status == 200
        detail = received["drilldown"]
        assert detail["income"] is True
        assert sum((Money(group["amount"]) for group in detail["merchants"]), Money(0)) == Money(
            detail["period"]["actual"]
        )
        assert all(
            item["date"].startswith("2026-01")
            for g in detail["merchants"]
            for item in g["transactions"]
        )
        assert report["as_of"]
        params = urllib.parse.urlencode(
            {"from": "2026-01", "through": "2026-01", "account": rent["account"], "index": "0"}
        )
        status, detail = client.get(f"/api/expense-explorer?{params}")
        assert status == 200
        merchants = detail["drilldown"]["merchants"]
        assert [(item["name"], Money(item["amount"])) for item in merchants] == [
            ("Rent", Money(1800))
        ]
        assert sum((Money(item["amount"]) for item in merchants), Money(0)) == Money(
            detail["drilldown"]["period"]["actual"]
        )

        status, rolled = client.get("/api/expense-explorer?from=2026-01&through=2026-01&rollover=1")
        assert status == 200
        assert rolled["rollover"] is True
        rent_rolled = next(row for row in rolled["categories"] if row["account"] == rent["account"])
        assert Money(rent_rolled["periods"][0]["carry_in"]) == Money(0)

    def test_the_endpoint_answers_with_derived_categories(self, client):
        status, payload = client.get("/api/plan")
        assert status == 200
        assert set(payload) == {
            "controls",
            "periods",
            "summary",
            "comparison",
            "currency",
            "categories",
            "mortgage_payments",
            "planning_flows",
            "cash_bridge",
            "column_totals",
        }
        assert payload["currency"]["notes"] == []
        assert set(payload["currency"]) == {"as_of", "conversions", "unconverted", "notes"}
        by_name = {row["full_name"]: row for row in payload["categories"]}
        assert "Income:Salary" in by_name
        assert "Expenses:Rent" in by_name
        assert Money(by_name["Income:Salary"]["actual"][0]) == Money("4200.00")
        assert Money(by_name["Expenses:Rent"]["actual"][0]) == Money("1800.00")
        assert set(payload["summary"]) == {
            "planned_cash",
            "actual_cash",
            "variance",
            "opening_cash",
            "ending_cash",
            "minimum_cash",
            "minimum_cash_date",
            "unresolved_expected",
            "unresolved_actuals",
        }
        assert payload["cash_bridge"]
        assert (
            payload["column_totals"]["cash_bridge"]["planned"]
            == payload["column_totals"]["net_cash"]["planned"]
        )

    def test_grouping_changes_display_buckets(self, client):
        _status, payload = client.get("/api/plan?from=2026-01&through=2026-12&period=quarter")
        assert payload["controls"]["period"] == "quarter"
        assert [row["label"] for row in payload["periods"]] == [
            "Q1 2026",
            "Q2 2026",
            "Q3 2026",
            "Q4 2026",
        ]

    def test_mortgage_cash_requirement_is_shared_with_detail_and_comparison(self, client):
        db = client.database
        accounts = {account.name: account for account in db.iter_accounts()}
        root = db.root_account()
        assert root is not None
        liabilities = Account(name="Liabilities", atype=AccountType.LIABILITY, parent=root.handle)
        mortgage = Account(name="Mortgage", atype=AccountType.LOAN, parent=liabilities.handle)
        escrow = Account(name="Escrow", atype=AccountType.ESCROW, parent=accounts["Assets"].handle)
        payment = ScheduledTransaction(
            name="Mortgage payment",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 15)),
            splits=[
                ScheduledSplit(mortgage.handle, Money("800")),
                ScheduledSplit(accounts["Rent"].handle, Money("1150")),
                ScheduledSplit(escrow.handle, Money("450")),
                ScheduledSplit(accounts["Checking"].handle, Money("-2400")),
            ],
        )
        with db.transaction("Web mortgage") as txn:
            db.add_account(liabilities, txn)
            db.add_account(mortgage, txn)
            db.add_account(escrow, txn)
            db.add_scheduled(payment, txn)

        _status, scenario = client.post("/api/scenario/duplicate", {"handle": None})
        query = urllib.parse.urlencode(
            {"from": "2026-01", "through": "2026-01", "compare": scenario["handle"]}
        )
        _status, payload = client.get(f"/api/plan?{query}")

        row = payload["mortgage_payments"][0]
        assert row["name"] == "Mortgage payment — Liabilities:Mortgage"
        assert Money(row["planned"][0]) == Money("2400")
        assert Money(payload["column_totals"]["mortgage_payments"]["planned"]["total"]) == Money(
            "2400"
        )
        compared = payload["comparison"]["mortgage_payments"][0]
        assert Money(compared["planned_delta"][0]) == Money(0)

        detail_query = urllib.parse.urlencode(
            {
                "account": mortgage.handle,
                "start": "2026-01-01",
                "end": "2026-01-31",
                "requirement_kind": "mortgage",
            }
        )
        _status, detail = client.get(f"/api/plan/detail?{detail_query}")
        assert detail["category"]["class"] == "cash_requirement"
        assert Money(detail["summary"]["planned"]) == Money("2400")
        explanation = " ".join(detail["planned"][0]["explanation"])
        assert "non-additive" in explanation

    def test_plan_reports_base_and_saved_scenario_choices(self, client):
        _status, payload = client.get("/api/plan")
        choices = payload["controls"]["scenarios"]
        assert choices[0] == {"handle": None, "name": "Base scenario"}

    def test_plan_compares_category_flows_with_a_saved_scenario(self, client):
        _status, scenario = client.post("/api/scenario/duplicate", {"handle": None})
        query = urllib.parse.urlencode({"compare": scenario["handle"]})
        _status, payload = client.get(f"/api/plan?{query}")

        comparison = payload["comparison"]
        assert comparison["handle"] == scenario["handle"]
        assert comparison["name"] == scenario["name"]
        assert Money(comparison["summary"]["planned_cash_delta"]) == Money(0)
        salary = next(
            row
            for row in comparison["categories"]
            if row["account"]
            == next(
                item["account"]
                for item in payload["categories"]
                if item["full_name"] == "Income:Salary"
            )
        )
        assert all(Money(value) == Money(0) for value in salary["planned_delta"])

        # A duplicate with no edited assumptions or events must retain the
        # complete comparison contract across every Plan reporting section.
        for section in ("categories", "cash_bridge", "mortgage_payments", "planning_flows"):
            for row in comparison[section]:
                for measure in ("planned", "actual", "variance"):
                    assert len(row[measure]) == len(row[f"{measure}_delta"])
                    assert all(
                        Money(value) == Money(0)
                        for value in row[f"{measure}_delta"]
                        if value is not None
                    )

    def test_saved_plan_can_compare_with_base(self, client):
        _status, scenario = client.post("/api/scenario/duplicate", {"handle": None})
        query = urllib.parse.urlencode({"scenario": scenario["handle"], "compare": "__base__"})
        _status, payload = client.get(f"/api/plan?{query}")
        assert payload["comparison"]["handle"] is None
        assert payload["comparison"]["name"] == "Base scenario"

    def test_saved_scenario_and_comparison_are_restored(self, client):
        _status, scenario = client.post("/api/scenario/duplicate", {"handle": None})
        _status, initial = client.get("/api/plan")
        status, controls = client.post(
            "/api/plan/settings",
            {
                "from": initial["controls"]["from"],
                "through": initial["controls"]["through"],
                "period": "month",
                "measure": "planned",
                "scenario": scenario["handle"],
                "compare": "__base__",
            },
        )
        assert status == 200
        assert controls["scenario"] == scenario["handle"]
        assert controls["compare"] == "__base__"

        _status, restored = client.get("/api/plan")

        assert restored["controls"]["scenario"] == scenario["handle"]
        assert restored["controls"]["compare"] == "__base__"
        assert restored["comparison"]["name"] == "Base scenario"

    def test_plan_rejects_comparison_with_the_active_scenario(self, client):
        _status, scenario = client.post("/api/scenario/duplicate", {"handle": None})
        query = urllib.parse.urlencode(
            {"scenario": scenario["handle"], "compare": scenario["handle"]}
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.get(f"/api/plan?{query}")
        assert caught.value.code == 400

    def test_plan_detail_reconciles_a_category_period(self, review_client):
        _status, plan = review_client.get("/api/plan?from=2026-02&through=2026-02")
        rent = next(row for row in plan["categories"] if row["full_name"] == "Expenses:Rent")
        query = urllib.parse.urlencode(
            {
                "account": rent["account"],
                "start": "2026-02-01",
                "end": "2026-02-28",
            }
        )
        status, detail = review_client.get(f"/api/plan/detail?{query}")

        assert status == 200
        assert detail["category"]["full_name"] == "Expenses:Rent"
        assert Money(detail["summary"]["planned"]) == Money("1800.00")
        assert Money(detail["summary"]["actual"]) == Money("1825.00")
        assert Money(detail["summary"]["variance"]) == Money("25.00")
        assert detail["planned"][0]["source"] == "scheduled"
        assert detail["planned"][0]["status"] == "expected"
        assert detail["actuals"][0]["resolution"] == "unresolved"
        assert "type EXPENSE" in " ".join(detail["planned"][0]["explanation"])
        assert "remains pending" in " ".join(detail["planned"][0]["explanation"])
        assert "Use Resolve actuals" in " ".join(detail["actuals"][0]["explanation"])

    def test_plan_detail_rejects_missing_scenario_and_unknown_requirement(self, client):
        account = client.database.get_account_by_name("Expenses")
        assert account is not None
        base = {
            "account": account.handle,
            "start": "2026-01-01",
            "end": "2026-01-31",
        }
        for extra, expected_status in (
            ({"scenario": "missing-scenario"}, 404),
            ({"requirement_kind": "unknown"}, 400),
        ):
            query = urllib.parse.urlencode(base | extra)
            with pytest.raises(urllib.error.HTTPError) as caught:
                client.get(f"/api/plan/detail?{query}")
            assert caught.value.code == expected_status

    def test_plan_detail_exposes_shared_escrow_treatment(self, client):
        assets = client.database.get_account_by_name("Assets")
        checking = client.database.get_account_by_name("Assets:Checking")
        assert assets is not None and checking is not None
        escrow = Account(
            name="Property escrow",
            atype=AccountType.ESCROW,
            parent=assets.handle,
        )
        funding = ScheduledTransaction(
            name="Fund property escrow",
            recurrence=Recurrence(PeriodType.ONCE, start=date(2026, 1, 15)),
            splits=[
                ScheduledSplit(escrow.handle, Money("100")),
                ScheduledSplit(checking.handle, Money("-100")),
            ],
        )
        with client.database.transaction("Escrow fixture") as txn:
            client.database.add_account(escrow, txn)
            client.database.add_scheduled(funding, txn)

        query = urllib.parse.urlencode(
            {
                "account": escrow.handle,
                "start": "2026-01-01",
                "end": "2026-01-31",
                "flow_kind": "escrow_funding",
            }
        )
        status, detail = client.get(f"/api/plan/detail?{query}")

        assert status == 200
        assert Money(detail["summary"]["planned"]) == Money("100")
        assert "recognized now" in " ".join(detail["planned"][0]["explanation"])

    def test_plan_detail_exposes_matched_variance_and_timing(self, review_client):
        review_client.post(
            "/api/review/match",
            {
                "transaction": review_client.actual_handle,
                "occurrence": review_client.occurrence,
            },
        )
        _status, plan = review_client.get("/api/plan?from=2026-02&through=2026-02")
        rent = next(row for row in plan["categories"] if row["full_name"] == "Expenses:Rent")
        query = urllib.parse.urlencode(
            {
                "account": rent["account"],
                "start": "2026-02-01",
                "end": "2026-02-28",
            }
        )
        _status, detail = review_client.get(f"/api/plan/detail?{query}")

        assert detail["planned"][0]["status"] == "actualized"
        assert Money(detail["planned"][0]["actual"]) == Money("1825.00")
        assert Money(detail["planned"][0]["variance"]) == Money("25.00")
        assert detail["actuals"][0]["resolution"] == "matched"
        assert Money(detail["actuals"][0]["expected"]) == Money("1800.00")
        assert Money(detail["actuals"][0]["variance"]) == Money("25.00")
        assert detail["actuals"][0]["date_variance_days"] == 1

    def test_plan_detail_rolls_up_descendant_categories(self, client):
        _status, plan = client.get("/api/plan?from=2026-01&through=2026-01")
        expenses = next(row for row in plan["categories"] if row["full_name"] == "Expenses")
        query = urllib.parse.urlencode(
            {
                "account": expenses["account"],
                "start": "2026-01-01",
                "end": "2026-01-31",
            }
        )
        _status, detail = client.get(f"/api/plan/detail?{query}")

        assert Money(detail["summary"]["actual"]) == Money("1800.00")
        assert detail["actuals"][0]["description"] == "Rent"

    def test_invalid_range_is_a_bad_request(self, client):
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.get("/api/plan?from=2027-01&through=2026-12")
        assert caught.value.code == 400

    def test_page_exposes_plan_not_the_legacy_budget_view(self, client):
        _status, body, _headers = client.raw("/app.js")
        page = body.decode()
        assert '"Scheduled", "Plan", "Review", "Projection"' in page
        assert "async function showPlan" in page
        assert "async function showBudget" not in page

    def test_plan_values_are_keyboard_accessible_buttons(self, client):
        _status, body, _headers = client.raw("/app.js")
        script = body.decode()
        _status, body, _headers = client.raw("/style.css")
        style = body.decode()
        assert 'class: "plan-cell-button"' in script
        assert 'type: "button"' in script
        assert '"aria-label": `Explain ${category.full_name}' in script
        assert ".plan-cell-button:focus-visible" in style


class TestThreadSafety:
    """Reads use snapshots while writes remain serialized on one connection."""

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

    def test_a_long_projection_does_not_block_a_write_and_new_reads_see_it(
        self, client, monkeypatch
    ):
        _status, accounts = client.get("/api/accounts")
        expense = next(account for account in accounts if account["type"] == "EXPENSE")
        assert expense["emergency_fund_included"] is True
        read_started = threading.Event()
        release_read = threading.Event()
        write_finished = threading.Event()

        projection = GET_ROUTES["/api/projection"]

        def held_projection(api, query):
            assert api.db.readonly is True
            read_started.set()
            assert release_read.wait(timeout=10)
            return projection(api, query)

        monkeypatch.setitem(GET_ROUTES, "/api/projection", held_projection)
        held_result: list[tuple[int, object]] = []
        write_result: list[tuple[int, object]] = []
        errors: list[BaseException] = []

        def read() -> None:
            try:
                held_result.append(client.get("/api/projection?years=1"))
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        def write() -> None:
            try:
                write_result.append(
                    client.post(
                        "/api/account/emergency-fund",
                        {"handle": expense["handle"], "included": False},
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                write_finished.set()

        read_thread = threading.Thread(target=read)
        write_thread = threading.Thread(target=write)
        read_thread.start()
        assert read_started.wait(timeout=10)
        write_thread.start()
        write_finished_while_reading = write_finished.wait(timeout=5)
        release_read.set()
        read_thread.join(timeout=10)
        write_thread.join(timeout=10)

        assert write_finished_while_reading is True
        assert errors == []
        assert write_result[0][0] == 200
        assert held_result[0][0] == 200
        _status, refreshed = client.get("/api/accounts")
        refreshed_expense = next(
            account for account in refreshed if account["handle"] == expense["handle"]
        )
        assert refreshed_expense["emergency_fund_included"] is False

    def test_read_snapshots_close_without_disturbing_the_writer_lock(self, book_path, monkeypatch):
        db = DbSQLite()
        db.load(str(book_path))
        lock_path = DbSQLite._writer_lock_path(str(book_path))
        lock_contents = lock_path.read_text(encoding="utf-8")
        closed_readers: list[DbSQLite] = []
        close = DbSQLite.close

        def record_close(candidate: DbSQLite) -> None:
            if candidate.readonly:
                closed_readers.append(candidate)
            close(candidate)

        monkeypatch.setattr(DbSQLite, "close", record_close)
        httpd = serve(db, host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/api/accounts",
            headers={"X-BreadSched-Token": httpd.token},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                assert response.status == 200
            assert len(closed_readers) == 1
            assert closed_readers[0].is_open is False
            assert lock_path.read_text(encoding="utf-8") == lock_contents
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=10)

        assert lock_path.read_text(encoding="utf-8") == lock_contents
        db.close()
        assert not lock_path.exists()


class TestWriting:
    def test_manual_currency_quote_preserves_import_and_rejected_writes(self, client):
        _status, commodities = client.get("/api/commodities")
        usd = next(item for item in commodities["currencies"] if item["mnemonic"] == "USD")
        euro = Commodity(namespace="CURRENCY", mnemonic="EUR")
        imported = CommodityPrice(
            commodity=euro.handle,
            currency=usd["handle"],
            quote_date=date(2026, 3, 1),
            value=Money("1.1"),
            source="sample-import",
        )
        with client.database.transaction("Fixture rate") as txn:
            client.database.add_commodity(euro, txn)
            client.database.add_price(imported, txn)

        request = {"from": euro.handle, "to": usd["handle"], "date": "2026-03-01"}
        status, first = client.post("/api/currency/quote", {**request, "rate": "1.234567"})
        assert status == 200
        assert first["rate"] == "1234567/1000000"
        assert first["source"] == "breadsched"
        status, second = client.post("/api/currency/quote", {**request, "rate": "1.25"})
        assert status == 200
        assert second["handle"] == first["handle"]
        assert second["rate"] == "5/4"

        original = list(client.database.iter_prices(commodity=euro.handle))
        for rejected in (
            {**request, "rate": "0"},
            {**request, "rate": "not a rate"},
            {**request, "rate": 1.5},
            {**request, "to": euro.handle, "rate": "1.25"},
            {**request, "date": "invalid", "rate": "1.25"},
        ):
            with pytest.raises(urllib.error.HTTPError) as caught:
                client.post("/api/currency/quote", rejected)
            assert caught.value.code == 400
            assert list(client.database.iter_prices(commodity=euro.handle)) == original
        assert client.database.get_price(imported.handle) == imported
        assert len(original) == 2

    def test_a_security_and_exact_dated_price_can_be_entered(self, client):
        _status, commodities = client.get("/api/commodities")
        usd = next(item for item in commodities["currencies"] if item["mnemonic"] == "USD")

        status, saved = client.post(
            "/api/commodity/price",
            {
                "mnemonic": "INDEX",
                "fullname": "Generic index fund",
                "namespace": "FUND",
                "fraction": "10000",
                "currency": usd["handle"],
                "date": "2026-03-01",
                "price": "125,25",
                "number_format": "comma",
            },
        )

        assert status == 200
        assert saved["price"] == "125.25"
        _status, commodities = client.get("/api/commodities")
        security = next(item for item in commodities["securities"] if item["mnemonic"] == "INDEX")
        assert security["price"] == "125.25"
        assert security["price_date"] == "2026-03-01"

    def test_a_transaction_can_be_posted(self, client):
        status, payload = client.post(
            "/api/transaction",
            {
                "date": "2026-02-01",
                "description": "Web entry",
                "from": "Assets:Checking",
                "to": "Expenses:Rent",
                "amount": "125.00",
                "notes": "Entered from the web",
            },
        )
        assert status == 200
        assert payload.get("ok") or payload.get("handle")

        _status, accounts = client.get("/api/accounts")
        balance = next(a["balance"] for a in accounts if a["name"] == "Checking")
        assert Money(balance) == Money("2275.00")
        checking = next(a["handle"] for a in accounts if a["name"] == "Checking")
        _status, register = client.get(
            "/api/register?" + urllib.parse.urlencode({"account": checking})
        )
        posted = next(row for row in register["rows"] if row["description"] == "Web entry")
        assert posted["notes"] == "Entered from the web"
        assert posted["source_notes"] == ""

    def test_a_transaction_accepts_comma_decimal_browser_input(self, client):
        status, payload = client.post(
            "/api/transaction",
            {
                "date": "2026-02-02",
                "description": "Localized web entry",
                "from": "Assets:Checking",
                "to": "Expenses:Rent",
                "amount": "45,67",
                "number_format": "comma",
            },
        )
        assert status == 200
        assert payload.get("ok") or payload.get("handle")

        _status, accounts = client.get("/api/accounts")
        balance = next(a["balance"] for a in accounts if a["name"] == "Checking")
        assert Money(balance) == Money("2354.33")

    def test_a_transaction_can_classify_an_investment_contribution(self, client):
        retirement = client.database.get_account_by_name("Assets:401(k)")
        assert retirement is not None
        retirement.atype = AccountType.RETIREMENT
        with client.database.transaction("Classify retirement account") as txn:
            client.database.commit_account(retirement, txn)
        status, payload = client.post(
            "/api/transaction",
            {
                "date": "2026-02-02",
                "description": "Investment contribution",
                "from": "Assets:Checking",
                "to": "Assets:401(k)",
                "amount": "125.00",
                "investment_activity": "contribution",
            },
        )
        assert status == 200
        transaction = client.database.get_transaction(payload["handle"])
        assert transaction is not None
        holding_split = next(split for split in transaction.splits if split.value > 0)
        assert holding_split.investment_activity is InvestmentActivityKind.CONTRIBUTION

    def test_hidden_accounts_are_reported_but_refused_for_new_entries(self, client):
        hidden = client.database.get_account_by_name("Expenses:Rent")
        assert hidden is not None
        hidden.hidden = True
        with client.database.transaction("Hide an old category") as txn:
            client.database.commit_account(hidden, txn)

        _status, accounts = client.get("/api/accounts")
        rent = next(account for account in accounts if account["full_name"] == "Expenses:Rent")
        assert rent["hidden"] is True
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post(
                "/api/transaction",
                {
                    "date": "2026-02-03",
                    "description": "Must not revive an archived category",
                    "from": "Assets:Checking",
                    "to": "Expenses:Rent",
                    "amount": "10.00",
                },
            )
        assert caught.value.code == 400

    @pytest.mark.parametrize(
        "changes",
        [
            {"amount": "0"},
            {"amount": "-10"},
            {"description": "   "},
        ],
    )
    def test_basic_entry_requires_a_positive_amount_and_description(self, client, changes):
        payload = {
            "date": "2026-02-03",
            "description": "Ordinary entry",
            "from": "Assets:Checking",
            "to": "Expenses:Rent",
            "amount": "10.00",
        }
        payload.update(changes)

        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post("/api/transaction", payload)

        assert caught.value.code == 400

    def test_an_unbalanced_request_is_refused_cleanly(self, client):
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post("/api/transaction", {"description": "nonsense"})
        assert caught.value.code in (400, 404, 500)


class TestImportApi:
    def test_browser_upload_preserves_source_for_reimport(self, client, book_path):
        content = (
            b"!Account\nNUploaded checking\nTBank\n^\n"
            b"!Type:Bank\nD03/04/2026\nT12,50\nPExample\n^\n"
        )
        status, result = client.upload(
            "statement.qif", content, number_format="comma", date_format="day-first"
        )
        assert status == 200
        assert "QIF" in result["format"]
        _status, defaults = client.get("/api/import")
        source = defaults["path"]
        assert source.startswith(str(book_path) + ".uploads")
        assert source.endswith(".qif")
        transactions = len(list(client.database.iter_transactions()))
        assert (
            client.upload("statement.qif", content, number_format="comma", date_format="day-first")[
                0
            ]
            == 200
        )
        assert client.get("/api/import")[1]["path"] == source
        assert len(list(client.database.iter_transactions())) == transactions
        assert len(list((book_path.parent / (book_path.name + ".uploads")).glob("*.qif"))) == 1

    def test_browser_upload_rejects_invalid_filename_and_restores_prior_source(self, client):
        content = b"!Type:Bank\nD01/01/2026\nT1.00\nPExample\n^\n"
        client.upload("statement.qif", content)
        source = client.get("/api/import")[1]["path"]
        from pathlib import Path

        for filename in ("../statement.qif", "folder\\statement.qif"):
            with pytest.raises(urllib.error.HTTPError) as caught:
                client.upload(filename, content)
            assert caught.value.code == 400
            assert json.loads(caught.value.read())["code"] == "import.filename.invalid"
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.upload("statement.qif", b"invalid input", number_format="guess-hard")
        assert caught.value.code == 400
        assert Path(source).read_bytes() == content

    def test_browser_upload_requires_token_and_bounds_body(self, client):
        query = "/api/import/upload?filename=statement.qif"
        status, payload = raw_http(
            client,
            (
                f"POST {query} HTTP/1.0\r\nHost: 127.0.0.1\r\n"
                "Content-Type: application/octet-stream\r\nContent-Length: 1\r\n\r\nX"
            ).encode(),
        )
        assert (status, payload["code"]) == (403, "request.untrusted")
        status, payload = raw_http(
            client,
            (
                f"POST {query} HTTP/1.0\r\nHost: 127.0.0.1\r\n"
                f"X-BreadSched-Token: {client.token}\r\n"
                "Content-Type: application/octet-stream\r\nContent-Length: 33554433\r\n\r\n"
            ).encode(),
        )
        assert (status, payload["code"]) == (413, "request.body.too_large")

    def test_qif_import_accepts_explicit_ambiguous_formats(self, client, tmp_path):
        path = tmp_path / "ambiguous.qif"
        path.write_text(
            "!Account\nNImported checking\nTBank\n^\n!Type:Bank\nD03/04/2026\nT12,50\nPExample\n^\n"
        )

        status, payload = client.post(
            "/api/import",
            {
                "path": str(path),
                "number_format": "comma",
                "date_format": "day-first",
                "include_scheduled": True,
            },
        )

        assert status == 200
        assert "QIF" in payload["format"]
        assert "transactions" in payload["detail"]
        _status, defaults = client.get("/api/import")
        assert defaults["path"] == str(path.resolve())

    def test_import_rejects_unknown_format_choice(self, client, tmp_path):
        path = tmp_path / "sample.qif"
        path.write_text("!Type:Bank\nD01/01/2026\nT1.00\nPExample\n^\n")
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post(
                "/api/import",
                {"path": str(path), "number_format": "guess-hard"},
            )
        assert caught.value.code == 400
        payload = json.loads(caught.value.read())
        assert payload["code"] == "import.number_format.invalid"
        assert payload["fields"] == ["number_format"]


class TestImportReview:
    """Issue #117: held GnuCash changes to reconciled transactions over HTTP."""

    def test_held_changes_are_listed_and_resolved_atomically(self, client, tmp_path):
        import sqlite3

        from breadsched.gen.engine import import_review, ledger
        from breadsched.gen.engine import reconciliation as reconcile_engine

        db = client.database
        source = tmp_path / "source.gnucash"
        bank = db.get_account_by_name("Checking")
        transactions = list(db.iter_transactions())
        statement = max(item.post_date for item in transactions)
        reconciliation = reconcile_engine.start(
            db, bank.handle, statement, ledger.balance(db, bank.handle, statement)
        )
        reconcile_engine.set_selection(
            db,
            reconciliation.handle,
            [s.handle for item in transactions for s in item.splits if s.account == bank.handle],
        )
        reconcile_engine.complete(db, reconciliation.handle)
        rent = next(item for item in transactions if item.description == "Rent")
        payroll = next(item for item in transactions if item.description == "Payroll")
        with sqlite3.connect(source) as gnucash:
            gnucash.execute(
                "UPDATE transactions SET description='Rent (edited)' WHERE guid=?", (rent.handle,)
            )
            gnucash.execute(
                "UPDATE splits SET value_num=value_num+100, quantity_num=quantity_num+100 "
                "WHERE tx_guid=? AND value_num>0",
                (payroll.handle,),
            )
            gnucash.execute(
                "UPDATE splits SET value_num=value_num-100, quantity_num=quantity_num-100 "
                "WHERE tx_guid=? AND value_num<0",
                (payroll.handle,),
            )

        status, imported = client.post("/api/import", {"path": str(source)})
        assert status == 200 and imported["held"] == 2
        _status, listed = client.get("/api/import/review")
        by_handle = {item["transaction"]: item for item in listed["changes"]}
        assert by_handle[rent.handle]["changes"] == ["Description: Rent -> Rent (edited)"]
        assert by_handle[rent.handle]["can_use_gnucash"] is True
        assert by_handle[payroll.handle]["can_use_gnucash"] is False
        assert by_handle[payroll.handle]["blocked_by"]

        before = (
            repr(db.get_transaction(rent.handle).serialize()),
            db.get_metadata(import_review.REVIEW_KEY),
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post(
                "/api/import/review",
                {
                    "decisions": [
                        {"transaction": rent.handle, "decision": "use-source"},
                        {"transaction": payroll.handle, "decision": "use-source"},
                    ]
                },
            )
        assert caught.value.code == 400
        assert json.loads(caught.value.read())["code"] == "import.review.reconciliation_blocks"
        assert (
            repr(db.get_transaction(rent.handle).serialize()),
            db.get_metadata(import_review.REVIEW_KEY),
        ) == before

        status, outcome = client.post(
            "/api/import/review",
            {
                "decisions": [
                    {"transaction": rent.handle, "decision": "use-source"},
                    {"transaction": payroll.handle, "decision": "keep"},
                ]
            },
        )
        assert status == 200 and outcome == {"kept": 1, "applied": 1, "deferred": 0}
        assert db.get_transaction(rent.handle).description == "Rent (edited)"
        assert client.get("/api/import/review")[1] == {"changes": []}

    def test_malformed_decision_is_rejected(self, client):
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post("/api/import/review", {"decisions": [{"decision": "guess"}]})
        assert caught.value.code == 400


class TestDueReview:
    """Reviewed, duplicate-safe batch decisions for due scheduled occurrences."""

    def test_due_review_lists_groups_and_applies_or_refuses_atomically(self, tmp_path):
        from breadsched.gen.engine import schedule as schedule_engine
        from breadsched.gen.sample_book import create_sample_book

        today = date.today()
        start = (today.replace(day=1) - timedelta(days=100)).replace(day=15)
        path = tmp_path / "due.breadsched"
        create_sample_book(path, as_of=start)
        db = DbSQLite()
        db.load(str(path))
        httpd = serve(db, host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_port}/api/due-review"
        headers = {"X-BreadSched-Token": httpd.token}

        def call(payload=None):
            data = None if payload is None else json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                base,
                data=data,
                headers={**headers, "Content-Type": "application/json"},
                method="GET" if payload is None else "POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read())

        try:
            listed = call()["schedules"]
            rent = next(item for item in listed if item["name"] == "Sample rent")
            assert len(rent["items"]) >= 2
            assert rent["frequency"] == "every month"
            first, second = rent["items"][0]["date"], rent["items"][1]["date"]

            # Post the first date elsewhere; a batch naming it is refused whole.
            occurrence = next(
                item
                for item in schedule_engine.due_occurrences(db, horizon_days=0)
                if item.schedule.handle == rent["schedule"] and item.when.isoformat() == first
            )
            schedule_engine.post_occurrences(db, [occurrence])
            before = db.summary()["txn"]
            with pytest.raises(urllib.error.HTTPError) as caught:
                call(
                    {
                        "decisions": [
                            {"schedule": rent["schedule"], "date": first, "decision": "post"},
                            {"schedule": rent["schedule"], "date": second, "decision": "skip"},
                        ]
                    }
                )
            assert caught.value.code == 400
            assert json.loads(caught.value.read())["code"] == "schedule.due.not_pending"
            assert db.summary()["txn"] == before

            outcome = call(
                {"decisions": [{"schedule": rent["schedule"], "date": second, "decision": "post"}]}
            )
            assert outcome == {"posted": 1, "skipped": 0, "deferred": 0}
            assert db.summary()["txn"] == before + 1
            remaining = next(item for item in call()["schedules"] if item["name"] == "Sample rent")
            assert second not in [item["date"] for item in remaining["items"]]
        finally:
            httpd.shutdown()
            httpd.server_close()
            db.close()


class TestSafety:
    def test_only_named_static_assets_are_served(self, client, tmp_path, monkeypatch):
        from breadsched.web import transport

        static = tmp_path / "static"
        static.mkdir()
        (static / "style.css").write_text("body { color: black; }", encoding="utf-8")
        (static / "private.txt").write_text("not public", encoding="utf-8")
        monkeypatch.setattr(transport, "STATIC", static)

        status, body, headers = client.raw("/style.css")
        assert status == 200
        assert body == b"body { color: black; }"
        assert headers.get_content_type() == "text/css"
        for requested in ("/private.txt", "/../private.txt", "/%2e%2e/private.txt"):
            status, payload = raw_http(
                client,
                f"GET {requested} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".encode(),
            )
            assert status == 404
            assert payload["code"] == "resource.not_found"

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows CI cannot create test symlinks")
    def test_static_symlink_outside_asset_root_is_rejected(self, client, tmp_path, monkeypatch):
        from breadsched.web import transport

        static = tmp_path / "static"
        static.mkdir()
        secret = tmp_path / "private.txt"
        secret.write_text("not public", encoding="utf-8")
        (static / "style.css").symlink_to(secret)
        monkeypatch.setattr(transport, "STATIC", static)

        with pytest.raises(urllib.error.HTTPError) as caught:
            client.raw("/style.css")
        assert caught.value.code == 404

    def test_an_empty_query_is_valid_on_every_supported_python(self):
        QueryParams("").finish()

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

    def test_api_requires_the_startup_token(self, client):
        request = urllib.request.Request(client.base_url + "/api/accounts")
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        assert caught.value.code == 403

    def test_foreign_origin_cannot_write_even_with_the_token(self, client):
        request = urllib.request.Request(
            client.base_url + "/api/transaction",
            data=json.dumps({"description": "blocked request"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": "https://example.invalid",
                "X-BreadSched-Token": client.token,
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        assert caught.value.code == 403

    def test_non_json_write_is_rejected(self, client):
        request = urllib.request.Request(
            client.base_url + "/api/transaction",
            data=b"{}",
            headers={
                "Content-Type": "text/plain",
                "X-BreadSched-Token": client.token,
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        assert caught.value.code == 403

    @pytest.mark.parametrize(
        ("path", "content_type", "status"),
        (
            ("/api/transaction", "text/plain", 403),
            ("/api/no-such-route", "application/json", 404),
            ("/api/import/upload?filename=a.csv", "text/plain", 403),
            ("/api/import/upload", "application/octet-stream", 400),
        ),
    )
    def test_rejected_write_reads_its_body_before_closing(self, client, path, content_type, status):
        # Closing with the request body unread makes the OS reset the connection
        # (RST), and a Windows client then loses the error response (WinError
        # 10053). A clean close lets the client write once more without error;
        # after a reset, that write fails.
        port = int(client.base_url.rsplit(":", 1)[1])
        body = b"x" * 60000
        head = (
            f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\n"
            f"X-BreadSched-Token: {client.token}\r\n\r\n"
        ).encode("ascii")
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(head + body)
            response = b""
            while chunk := sock.recv(65536):
                response += chunk
            assert response.split(b"\r\n", 1)[0].split()[1] == str(status).encode()
            time.sleep(0.2)
            sock.send(b"x")

    def test_foreign_host_is_rejected(self, client):
        request = urllib.request.Request(
            client.base_url + "/",
            headers={"Host": "example.invalid"},
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        assert caught.value.code == 403

    @pytest.mark.parametrize(
        ("headers", "status", "code"),
        (
            (b"", 411, "request.content_length.required"),
            (b"Content-Length: nope\r\n", 400, "request.content_length.invalid"),
            (b"Content-Length: -1\r\n", 400, "request.content_length.invalid"),
            (
                b"Content-Length: 2\r\nContent-Length: 2\r\n",
                400,
                "request.content_length.repeated",
            ),
            (
                b"Content-Length: 65537\r\n",
                413,
                "request.body.too_large",
            ),
            (
                b"Transfer-Encoding: chunked\r\n",
                400,
                "request.transfer_encoding.unsupported",
            ),
        ),
    )
    def test_post_framing_is_rejected_before_reading(self, client, headers, status, code):
        request = (
            b"POST /api/post-scheduled HTTP/1.0\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            + f"X-BreadSched-Token: {client.token}\r\n".encode()
            + headers
            + b"\r\n"
        )

        actual_status, payload = raw_http(client, request)

        assert actual_status == status
        assert payload["code"] == code

    @pytest.mark.parametrize(
        ("query", "code", "fields"),
        (
            ("days=abc", "query.integer.invalid", ["days"]),
            ("days=-1", "query.integer.out_of_range", ["days"]),
            ("days=1&days=2", "query.repeated", ["days"]),
            ("unknown=1", "query.unknown", ["unknown"]),
        ),
    )
    def test_query_contract_rejects_invalid_fields_consistently(self, client, query, code, fields):
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.get(f"/api/scheduled?{query}")

        assert caught.value.code == 400
        payload = json.loads(caught.value.read())
        assert payload["code"] == code
        assert payload["fields"] == fields

    def test_service_errors_keep_their_stable_code_and_fields(self, client):
        _status, data = client.get("/api/scheduled")
        account = next(a for a in data["accounts"] if a["name"].endswith(":Rent"))

        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post(
                "/api/scheduled/save",
                {
                    "name": "Invalid same-account schedule",
                    "category": account["handle"],
                    "funding": account["handle"],
                    "amount": "75.00",
                    "frequency": "monthly",
                    "start": "2026-02-01",
                    "weekend": "none",
                },
            )

        payload = json.loads(caught.value.read())
        assert caught.value.code == 400
        assert payload["code"] == "schedule.accounts.same"
        assert payload["fields"] == ["category", "funding"]

    def test_unexpected_failures_return_only_a_correlation_id(
        self, client, monkeypatch, breadsched_logs
    ):
        def explode(_api, query):
            query.finish()
            raise RuntimeError("private failure detail")

        monkeypatch.setitem(GET_ROUTES, "/api/test-boom", explode)

        with pytest.raises(urllib.error.HTTPError) as caught:
            client.get("/api/test-boom")

        payload = json.loads(caught.value.read())
        assert caught.value.code == 500
        assert payload["code"] == "internal.error"
        assert "private failure detail" not in json.dumps(payload)
        assert payload["correlation_id"]
        assert breadsched_logs.containing(payload["correlation_id"])

    def test_unexpected_failure_still_responds_when_logger_is_broken(self, client, monkeypatch):
        from breadsched.web import transport

        def explode(_api, query):
            query.finish()
            raise RuntimeError("private failure detail")

        def broken_log(*_args, **_kwargs):
            raise ValueError("closed log stream")

        monkeypatch.setitem(GET_ROUTES, "/api/test-broken-log", explode)
        monkeypatch.setattr(transport.LOG, "exception", broken_log)
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.get("/api/test-broken-log")
        payload = json.loads(caught.value.read())
        assert caught.value.code == 500
        assert payload["code"] == "internal.error"
        assert "private failure detail" not in json.dumps(payload)


class TestCliIntegration:
    def test_the_web_command_exists(self, capsys):
        from breadsched.cli.main import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--help"])
        assert "web" in capsys.readouterr().out

    def test_the_web_command_rejects_a_public_host(self, book_path, capsys):
        assert cli(["web", str(book_path), "--host", "0.0.0.0"]) != 0


class TestDashboardApi:
    """The dashboard is the primary view; the browser gets the same figures."""

    def test_imported_first_view_and_missing_cash_quote_explain_setup(self, client):
        status, first = client.get("/api/dashboard")
        assert status == 200
        assert first["groups"] == []
        assert first["summary"]["net_worth"] == "2400.00"
        assert "assets" not in first["unavailable_reasons"]

        assets = client.database.get_account_by_name("Assets")
        income = client.database.get_account_by_name("Income")
        assert assets is not None and income is not None
        euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
        foreign_cash = Account(
            name="Foreign cash",
            atype=AccountType.BANK,
            parent=assets.handle,
            commodity=euro.handle,
        )
        entry = Transaction(post_date=date(2026, 1, 15), description="Foreign opening")
        entry.currency = euro.handle
        entry.splits = [Split(foreign_cash.handle, Money(10)), Split(income.handle, Money(-10))]
        with client.database.transaction("Foreign cash") as txn:
            client.database.add_commodity(euro, txn)
            client.database.add_account(foreign_cash, txn)
            client.database.add_transaction(entry, txn)

        status, missing = client.get("/api/dashboard")
        assert status == 200
        for field in ("net_worth", "liquid", "available"):
            assert missing["summary"][field] is None
            assert missing["unavailable_reasons"][field] == "Missing reporting-currency quote"
        assert missing["missing_quotes"] == [foreign_cash.handle]
        assert missing["liquid_missing_quotes"] == [foreign_cash.handle]
        assert missing["unavailable_reasons"]["months_covered"] == "No committed outgoings"

    def test_imported_security_and_unset_card_first_view(self, tmp_path, gnucash_household_path):
        path = tmp_path / "commitments.breadsched"
        cli(["init", str(path)])
        cli(["import", str(path), gnucash_household_path.path, "--no-infer"])
        db = DbSQLite()
        db.load(str(path))
        httpd = serve(db, host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{httpd.server_port}/api/dashboard",
                headers={"X-BreadSched-Token": httpd.token},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                board = json.loads(response.read())
        finally:
            httpd.shutdown()
            httpd.server_close()
            db.close()
        assert board["summary"]["net_worth"] == "109950.00"
        assert board["unavailable_reasons"]["next_income"] == "No scheduled income"
        [note] = board["coverage_notes"]
        assert note.startswith("Card payments not set up: 1 credit card owes a balance")

    def test_missed_occurrences_arrive_grouped_with_details(self, tmp_path):
        from breadsched.gen.sample_book import create_sample_book

        today = date.today()
        start = (today.replace(day=1) - timedelta(days=100)).replace(day=15)
        path = tmp_path / "missed.breadsched"
        create_sample_book(path, as_of=start)
        db = DbSQLite()
        db.load(str(path))
        httpd = serve(db, host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{httpd.server_port}/api/dashboard",
                headers={"X-BreadSched-Token": httpd.token},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                board = json.loads(response.read())
        finally:
            httpd.shutdown()
            httpd.server_close()
            db.close()

        rent = next(item for item in board["display_bills"] if item["name"] == "Sample rent")
        assert rent["missed"] >= 2
        assert len(rent["occurrences"]) == rent["missed"]
        assert rent["next_due"] == rent["occurrences"][0]["date"]
        assert rent["last_due"] == rent["occurrences"][-1]["date"]
        total = sum(Decimal(entry["amount"]) for entry in rent["occurrences"])
        assert Decimal(rent["amount"]) == total
        assert rent["frequency"] == "every month"
        assert sum(1 for item in board["bills"] if item["name"] == "Sample rent") == rent["missed"]

    def test_the_endpoint_answers(self, client):
        status, payload = client.get("/api/dashboard")
        assert status == 200
        assert set(payload) == {
            "summary",
            "config",
            "groups",
            "bills",
            "income",
            "display_bills",
            "display_income",
            "missing_quotes",
            "liquid_missing_quotes",
            "coverage_notes",
            "unavailable_reasons",
        }
        assert payload["unavailable_reasons"]["months_covered"] == "No committed outgoings"

    def test_missing_group_quote_suppresses_position_and_preserves_bills(self, client):
        from breadsched.gen.engine import dashboard

        assets = client.database.get_account_by_name("Assets")
        equity = client.database.get_account_by_name("Equity")
        assert assets is not None and equity is not None
        euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
        account = Account(
            name="Foreign cash", atype=AccountType.BANK, parent=assets.handle, commodity=euro.handle
        )
        transaction = Transaction(post_date=date(2026, 1, 2), description="Foreign cash")
        transaction.currency = euro.handle
        transaction.splits = [Split(account.handle, Money(10)), Split(equity.handle, Money(-10))]
        with client.database.transaction("Foreign cash") as txn:
            client.database.add_commodity(euro, txn)
            client.database.add_account(account, txn)
            client.database.add_transaction(transaction, txn)
        dashboard.DashboardConfig(
            groups=[dashboard.GroupConfig("Cash", [account.handle], "liquid")]
        ).save(client.database)
        status, payload = client.get("/api/dashboard")
        assert status == 200
        assert payload["summary"]["net_worth"] is None
        assert payload["summary"]["liquid"] is None
        assert payload["summary"]["available"] is None
        assert payload["missing_quotes"] == [account.handle]
        assert payload["groups"][0]["total"] is None
        assert payload["groups"][0]["accounts"][0]["balance"] is None
        assert isinstance(payload["bills"], list)

    def test_resource_matches_route_and_query_does_not_save_horizons(self, client):
        _status, original = client.get("/api/dashboard")
        _status, requested = client.get("/api/dashboard?liquidity_days=45&emergency_months=9")
        expected = dashboard_report(client.database, 45, 9)
        assert requested == json.loads(json.dumps(expected, default=str))
        assert requested["config"]["liquidity_days"] == 45
        assert requested["config"]["emergency_months"] == 9
        _status, restored = client.get("/api/dashboard")
        assert restored["config"] == original["config"]

    def test_fsa_information_has_its_own_dashboard_endpoint(self, client):
        status, payload = client.get("/api/fsa/dashboard")
        assert status == 200
        assert set(payload) == {"years", "claims"}

    def test_it_reports_the_headline_figures(self, client):
        _status, payload = client.get("/api/dashboard")
        for key in (
            "net_worth",
            "liquid",
            "required_liquid",
            "available",
            "emergency_fund",
            "months_covered",
            "monthly_outgoings",
            "monthly_outgoings_with_estimates",
            "income_per_month_with_estimates",
        ):
            assert key in payload["summary"], f"missing {key}"

    def test_dashboard_exposes_income_in_a_separate_list(self, client):
        _status, scheduled = client.get("/api/scheduled")
        salary = next(item for item in scheduled["accounts"] if item["name"].endswith(":Salary"))
        checking = next(
            item for item in scheduled["accounts"] if item["name"].endswith(":Checking")
        )
        status, _created = client.post(
            "/api/scheduled/save",
            {
                "name": "Payday",
                "category": salary["handle"],
                "funding": checking["handle"],
                "amount": "1000.00",
                "frequency": "monthly",
                "start": "2027-01-01",
                "enabled": True,
            },
        )
        assert status == 200
        _status, payload = client.get("/api/dashboard")
        assert payload["income"]
        assert all(item["name"] == "Payday" for item in payload["income"])
        assert all("hold" not in item for item in payload["income"])
        assert all(item["name"] != "Payday" for item in payload["bills"])

    def test_groups_carry_equity_and_ltv_where_they_apply(self, client):
        _status, payload = client.get("/api/dashboard")
        for group in payload["groups"]:
            assert {"loan_to_value", "loan_end", "equity"} <= set(group)
            assert {"path", "depth", "heading", "note"} <= set(group)

    def test_group_paths_can_be_configured_and_are_returned_as_a_hierarchy(self, client):
        _status, accounts = client.get("/api/accounts")
        checking = next(account for account in accounts if account["name"] == "Checking")
        retirement = next(account for account in accounts if account["name"] == "401(k)")

        status, saved = client.post(
            "/api/dashboard/config",
            {
                "groups": [
                    {
                        "name": "Holdings:Cash",
                        "kind": "liquid",
                        "accounts": [checking["handle"], checking["handle"]],
                    },
                    {
                        "name": "Holdings:Retirement",
                        "kind": "retirement",
                        "accounts": [retirement["handle"]],
                    },
                ]
            },
        )

        assert status == 200
        assert saved["groups"][0]["name"] == "Holdings:Cash"
        assert saved["groups"][0]["accounts"] == [checking["handle"]]
        _status, payload = client.get("/api/dashboard")
        assert [(group["path"], group["depth"]) for group in payload["groups"]] == [
            ("Holdings", 0),
            ("Holdings:Cash", 1),
            ("Holdings:Retirement", 1),
        ]
        assert payload["config"]["groups"][0]["name"] == "Holdings:Cash"
        assert payload["config"]["accounts"]

    def test_the_horizons_are_query_parameters(self, client):
        _status, six = client.get("/api/dashboard?emergency_months=6")
        _status, twelve = client.get("/api/dashboard?emergency_months=12")
        assert twelve["config"]["emergency_months"] == 12
        assert twelve["summary"]["emergency_fund"] is None
        assert six["summary"]["emergency_fund"] is None
        assert twelve["unavailable_reasons"]["emergency_fund"] == "No committed outgoings"

    def test_amounts_are_strings_the_browser_can_parse(self, client):
        _status, payload = client.get("/api/dashboard")
        assert isinstance(payload["summary"]["net_worth"], str)
        float(payload["summary"]["net_worth"])

    def test_account_emergency_fund_choice_round_trips(self, client):
        _status, accounts = client.get("/api/accounts")
        expense = next(account for account in accounts if account["type"] == "EXPENSE")
        bank = next(account for account in accounts if account["type"] == "BANK")
        assert expense["emergency_fund_eligible"] is True
        assert expense["emergency_fund_included"] is True
        assert bank["emergency_fund_eligible"] is False

        status, saved = client.post(
            "/api/account/emergency-fund",
            {"handle": expense["handle"], "included": False},
        )

        assert status == 200
        assert saved["emergency_fund_included"] is False
        _status, refreshed = client.get("/api/accounts")
        changed = next(account for account in refreshed if account["handle"] == expense["handle"])
        assert changed["emergency_fund_included"] is False

    def test_the_page_opens_on_the_dashboard(self, client):
        _status, body, _headers = client.raw("/app.js")
        page = body.decode()
        assert ': "Dashboard"' in page
        assert 'launchParams.get("view")' in page
        assert '"Dashboard", "FSA Dashboard", "Accounts"' in page
        assert "async function showDashboard" in page
        assert "async function showFsaDashboard" in page
        assert "openDashboardGroupsEditor" in page
        assert '"X-BreadSched-Token"' in page


class TestReviewApi:
    """The web Review surface persists the same resolution decisions as GTK."""

    def test_unresolved_actuals_and_candidates_are_exposed(self, review_client):
        status, payload = review_client.get(
            "/api/review?" + urllib.parse.urlencode({"transaction": review_client.actual_handle})
        )
        assert status == 200
        assert [item["handle"] for item in payload["actuals"]] == [review_client.actual_handle]
        assert payload["selected"]["description"] == "Actual rent"
        candidate = payload["candidates"][0]
        assert candidate["key"] == review_client.occurrence
        assert Money(candidate["expected_amount"]) == Money("1800.00")
        assert Money(candidate["amount_variance"]) == Money("25.00")
        assert candidate["date_variance_days"] == 1

    def test_rejecting_a_candidate_remembers_the_decision(self, review_client):
        status, payload = review_client.post(
            "/api/review/reject",
            {
                "transaction": review_client.actual_handle,
                "occurrence": review_client.occurrence,
            },
        )
        assert status == 200
        assert payload["rejected"] == review_client.occurrence
        _status, review = review_client.get(
            "/api/review?" + urllib.parse.urlencode({"transaction": review_client.actual_handle})
        )
        assert review["candidates"] == []

    def test_skipping_a_candidate_updates_schedule_but_keeps_actual_unresolved(self, review_client):
        status, payload = review_client.post(
            "/api/review/skip",
            {
                "transaction": review_client.actual_handle,
                "occurrence": review_client.occurrence,
            },
        )
        assert status == 200
        assert payload["skipped"] == review_client.occurrence
        _status, review = review_client.get(
            "/api/review?" + urllib.parse.urlencode({"transaction": review_client.actual_handle})
        )
        assert review["selected"]["handle"] == review_client.actual_handle
        assert review["candidates"] == []

    def test_matching_removes_the_actual_from_the_review_queue(self, review_client):
        status, payload = review_client.post(
            "/api/review/match",
            {
                "transaction": review_client.actual_handle,
                "occurrence": review_client.occurrence,
            },
        )
        assert status == 200
        assert payload["resolution"] == "matched"
        _status, review = review_client.get("/api/review")
        assert review["actuals"] == []

        with pytest.raises(urllib.error.HTTPError) as caught:
            review_client.post(
                "/api/review/match",
                {
                    "transaction": review_client.actual_handle,
                    "occurrence": review_client.occurrence,
                },
            )
        error = json.loads(caught.value.read())
        assert error["code"] == "review.transaction.not_unresolved"
        assert error["fields"] == ["transaction"]

    def test_marking_unexpected_removes_the_actual_from_the_queue(self, review_client):
        status, payload = review_client.post(
            "/api/review/unexpected", {"transaction": review_client.actual_handle}
        )
        assert status == 200
        assert payload["resolution"] == "unexpected"
        _status, review = review_client.get("/api/review")
        assert review["actuals"] == []

    def test_page_exposes_review_between_plan_and_projection(self, client):
        _status, body, _headers = client.raw("/app.js")
        page = body.decode()
        assert '"Plan", "Review", "Projection"' in page
        assert "async function showReview" in page


class TestScenarioManagementApi:
    """The browser manages the same persisted Base/saved scenario model as GTK."""

    @staticmethod
    def assumptions(**changes):
        values = {
            "income_growth": "0.03",
            "expense_inflation": "0.025",
            "investment_return": "0.06",
            "cash_interest": "0.01",
            "liability_interest": "0.0",
        }
        values.update(changes)
        return values

    def test_list_resource_preserves_inheritance_and_account_order(self, client):
        client.post(
            "/api/scenario/save",
            {"handle": None, "assumptions": self.assumptions(income_growth="0.041")},
        )
        client.post("/api/scenario/duplicate", {"handle": None})

        _status, listing = client.get("/api/scenarios")
        from breadsched.web.server import Api

        expected = scenarios_report(
            client.database, Api(client.database)._management_base_scenario()
        )
        assert listing == json.loads(json.dumps(expected, default=str))
        assert listing["scenarios"][1]["assumption_sources"]["income_growth"] == "Base"
        names = [item["name"].casefold() for item in listing["projection_accounts"]]
        assert names == sorted(names)

    def test_base_assumptions_can_be_saved_and_reloaded(self, client):
        status, payload = client.post(
            "/api/scenario/save",
            {"handle": None, "assumptions": self.assumptions(investment_return="0.0475")},
        )
        assert status == 200
        assert payload["base"] is True
        assert payload["assumptions"]["investment_return"] == "0.0475"

        _status, listing = client.get("/api/scenarios")
        base = listing["scenarios"][0]
        assert base["name"] == "Base scenario"
        assert base["assumptions"]["investment_return"] == "0.0475"

    def test_account_specific_projection_rates_can_be_saved(self, client):
        _status, listing = client.get("/api/scenarios")
        account = listing["projection_accounts"][0]
        assumptions = self.assumptions()
        assumptions["per_account"] = {account["handle"]: "0.0825"}

        status, payload = client.post(
            "/api/scenario/save", {"handle": None, "assumptions": assumptions}
        )

        assert status == 200
        assert payload["assumptions"]["per_account"] == {account["handle"]: "0.0825"}
        _status, reopened = client.get("/api/scenarios")
        assert reopened["scenarios"][0]["assumptions"]["per_account"] == {
            account["handle"]: "0.0825"
        }

    def test_base_duplicate_inherits_untouched_values_and_preserves_overrides(self, client):
        client.post(
            "/api/scenario/save",
            {"handle": None, "assumptions": self.assumptions(income_growth="0.041")},
        )
        _status, clone = client.post("/api/scenario/duplicate", {"handle": None})
        assert clone["base"] is False
        assert clone["name"] == "Base scenario copy"
        assert clone["assumptions"]["income_growth"] == "0.041"
        assert clone["assumption_overrides"] == []
        assert clone["assumption_sources"]["income_growth"] == "Base"

        clone["name"] = "Retire 2035"
        clone["description"] = "Reduced work scenario"
        clone["assumptions"]["income_growth"] = "0.01"
        clone["assumption_overrides"] = ["income_growth"]
        _status, saved = client.post("/api/scenario/save", clone)
        assert saved["name"] == "Retire 2035"

        client.post(
            "/api/scenario/save",
            {
                "handle": None,
                "assumptions": self.assumptions(income_growth="0.08", expense_inflation="0.045"),
            },
        )

        _status, listing = client.get("/api/scenarios")
        base = listing["scenarios"][0]
        retire = next(item for item in listing["scenarios"] if item["name"] == "Retire 2035")
        assert base["assumptions"]["income_growth"] == "0.08"
        assert retire["assumptions"]["income_growth"] == "0.01"
        assert retire["assumptions"]["expense_inflation"] == "0.045"
        assert retire["assumption_sources"]["income_growth"] == "Retire 2035"
        assert retire["assumption_sources"]["expense_inflation"] == "Base"

        query = urllib.parse.urlencode({"scenario": retire["handle"]})
        _status, plan = client.get(f"/api/plan?{query}")
        assert plan["controls"]["assumption_sources"]["income_growth"] == "Retire 2035"
        assert plan["controls"]["assumption_sources"]["expense_inflation"] == "Base"

        _status, projected = client.get(f"/api/projection?{query}")
        assert projected["scenario"]["assumption_sources"]["income_growth"] == "Retire 2035"
        assert projected["scenario"]["assumption_sources"]["expense_inflation"] == "Base"

    def test_saved_scenario_can_inherit_from_and_be_reparented_between_scenarios(self, client):
        _status, parent = client.post("/api/scenario/duplicate", {"handle": None})
        parent["name"] = "Earlier retirement"
        parent["assumptions"]["income_growth"] = "0.01"
        parent["assumption_overrides"] = ["income_growth"]
        _status, parent = client.post("/api/scenario/save", parent)

        _status, child = client.post("/api/scenario/duplicate", {"handle": None})
        child["name"] = "Earlier retirement with lower returns"
        child["parent_handle"] = parent["handle"]
        child["assumptions"]["investment_return"] = "0.035"
        child["assumption_overrides"] = ["investment_return"]
        _status, child = client.post("/api/scenario/save", child)

        assert child["parent_handle"] == parent["handle"]
        assert child["assumptions"]["income_growth"] == "0.01"
        assert child["assumption_sources"]["income_growth"] == "Earlier retirement"
        assert (
            child["assumption_sources"]["investment_return"]
            == "Earlier retirement with lower returns"
        )

        with pytest.raises(urllib.error.HTTPError) as caught:
            parent["parent_handle"] = child["handle"]
            client.post("/api/scenario/save", parent)
        assert caught.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post("/api/scenario/delete", {"handle": parent["handle"]})
        assert caught.value.code == 400

        child["parent_handle"] = None
        _status, child = client.post("/api/scenario/save", child)
        status, deleted = client.post("/api/scenario/delete", {"handle": parent["handle"]})
        assert status == 200
        assert deleted["deleted"] == parent["handle"]

    def test_dated_account_specific_projection_rates_can_be_saved(self, client):
        _status, listing = client.get("/api/scenarios")
        account = listing["projection_accounts"][0]
        _status, scenario = client.post("/api/scenario/duplicate", {"handle": None})

        status, saved = client.post(
            "/api/scenario/period/save",
            {
                "handle": scenario["handle"],
                "start": "2035-01-01",
                "end": "",
                "description": "Account return change",
                "investment_return": "",
                "expense_inflation": "",
                "income_growth": "",
                "cash_interest": "",
                "liability_interest": "",
                "per_account": {account["handle"]: "0.035"},
            },
        )

        assert status == 200
        period = saved["periods"][0]
        assert period["per_account"] == {account["handle"]: "0.035"}

    def test_dated_assumptions_can_be_added_edited_and_deleted(self, client):
        _status, scenario = client.post("/api/scenario/duplicate", {"handle": None})
        handle = scenario["handle"]
        _status, saved = client.post(
            "/api/scenario/period/save",
            {
                "handle": handle,
                "start": "2035-01-01",
                "end": "",
                "description": "Retirement returns",
                "investment_return": "0.04",
                "expense_inflation": "",
                "income_growth": "",
                "cash_interest": "",
                "liability_interest": "",
            },
        )
        assert len(saved["periods"]) == 1
        assert saved["periods"][0]["investment_return"] == "0.04"
        assert saved["periods"][0]["expense_inflation"] is None

        _status, edited = client.post(
            "/api/scenario/period/save",
            {
                "handle": handle,
                "index": saved["periods"][0]["index"],
                "start": "2035-01-01",
                "end": "2040-12-31",
                "description": "Retirement transition",
                "investment_return": "0.035",
                "expense_inflation": "0.045",
                "income_growth": "",
                "cash_interest": "",
                "liability_interest": "",
            },
        )
        assert edited["periods"][0]["end"] == "2040-12-31"
        assert edited["periods"][0]["expense_inflation"] == "0.045"

        _status, deleted = client.post(
            "/api/scenario/period/delete",
            {"handle": handle, "index": edited["periods"][0]["index"]},
        )
        assert deleted["periods"] == []

    def test_saved_scenario_can_be_deleted_but_base_cannot(self, client):
        _status, scenario = client.post("/api/scenario/duplicate", {"handle": None})
        status, payload = client.post("/api/scenario/delete", {"handle": scenario["handle"]})
        assert status == 200
        assert payload["deleted"] == scenario["handle"]
        _status, listing = client.get("/api/scenarios")
        assert [item["name"] for item in listing["scenarios"]] == ["Base scenario"]

        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post("/api/scenario/delete", {"handle": None})
        assert caught.value.code == 400


class TestScenarioManagementPage:
    def test_plan_links_to_scenario_management(self, client):
        _status, body, _headers = client.raw("/app.js")
        page = body.decode()
        assert '"Manage scenarios…"' in page
        assert "async function showScenarios" in page
        assert "Inherit assumptions from" in page
        assert "Dated assumptions and scenario events remain local" in page
        assert "Add dated assumptions…" in page
        assert "Dated assumption periods belong to saved scenarios" in page


@pytest.fixture
def scenario_event_client(book_path):
    """A web book with a baseline schedule available for scenario overrides."""
    db = DbSQLite()
    db.load(str(book_path))
    bank = db.get_account_by_name("Checking")
    rent = db.get_account_by_name("Rent")
    assert bank is not None and rent is not None
    baseline = ScheduledTransaction(
        name="Future rent",
        recurrence=Recurrence(PeriodType.MONTH, start=date(2026, 2, 1)),
        splits=[
            ScheduledSplit(rent.handle, Money("1800.00")),
            ScheduledSplit(bank.handle, Money("-1800.00")),
        ],
    )
    with db.transaction("Add test schedule") as txn:
        db.add_scheduled(baseline, txn)
    httpd = serve(db, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    class Client:
        token = httpd.token
        database = db

        def get(self, path: str):
            request = urllib.request.Request(
                base + path, headers={"X-BreadSched-Token": self.token}
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())

        def post(self, path: str, payload: dict):
            request = urllib.request.Request(
                base + path,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-BreadSched-Token": self.token,
                },
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


class TestScenarioEventWebParity:
    @staticmethod
    def _saved_scenario(client):
        _status, scenario = client.post("/api/scenario/duplicate", {"handle": None})
        return scenario

    def test_rejected_scenario_estimate_preserves_saved_scenario(self, scenario_event_client):
        scenario = self._saved_scenario(scenario_event_client)
        _status, events = scenario_event_client.get(
            "/api/scenario/events?" + urllib.parse.urlencode({"handle": scenario["handle"]})
        )
        rent = next(account for account in events["accounts"] if account["name"].endswith("Rent"))
        bank = next(
            account for account in events["accounts"] if account["name"].endswith("Checking")
        )
        before = scenario_event_client.database.get_scenario(scenario["handle"])
        assert before is not None
        with pytest.raises(urllib.error.HTTPError) as caught:
            scenario_event_client.post(
                "/api/scenario/event/save",
                {
                    "handle": scenario["handle"],
                    "name": "Alternate rent",
                    "category": rent["handle"],
                    "funding": bank["handle"],
                    "amount": "1500",
                    "frequency": "monthly",
                    "start": "2026-03-01",
                    "skipped": ["2026-04-01"],
                    "occurrence_adjustments": [{"when": "2026-04-01", "amount": "1700"}],
                },
            )
        assert caught.value.code == 400
        after = scenario_event_client.database.get_scenario(scenario["handle"])
        assert after is not None
        assert after.serialize() == before.serialize()

    def test_scenario_only_estimate_is_persisted(self, scenario_event_client):
        scenario = self._saved_scenario(scenario_event_client)
        _status, events = scenario_event_client.get(
            "/api/scenario/events?" + urllib.parse.urlencode({"handle": scenario["handle"]})
        )
        rent = next(account for account in events["accounts"] if account["name"].endswith("Rent"))
        bank = next(
            account for account in events["accounts"] if account["name"].endswith("Checking")
        )
        _status, saved = scenario_event_client.post(
            "/api/scenario/event/save",
            {
                "handle": scenario["handle"],
                "name": "Lower rent estimate",
                "growth_policy": "inflation",
                "category": rent["handle"],
                "funding": bank["handle"],
                "planning_flow": "debt_principal",
                "amount": "1500.00",
                "seasonal_amounts": [{"month": 7, "amount": "1600.00"}],
                "frequency": "monthly",
                "start": "2026-03-01",
                "weekend": "none",
            },
        )
        assert len(saved["changes"]) == 1
        assert saved["changes"][0]["source_schedule"] is None
        assert saved["changes"][0]["amount"] == "1500.00"
        assert saved["changes"][0]["planning_flow"] == "debt_principal"
        assert saved["changes"][0]["growth_policy"] == "inflation"
        assert saved["changes"][0]["seasonal_amounts"] == [{"month": 7, "amount": "1600.00"}]

    def test_scenario_estimate_can_carry_fixed_multisplit_classifications(
        self, scenario_event_client
    ):
        scenario = self._saved_scenario(scenario_event_client)
        _status, events = scenario_event_client.get(
            "/api/scenario/events?" + urllib.parse.urlencode({"handle": scenario["handle"]})
        )
        salary = next(
            account for account in events["accounts"] if account["name"].endswith("Salary")
        )
        rent = next(account for account in events["accounts"] if account["name"].endswith("Rent"))
        bank = next(
            account for account in events["accounts"] if account["name"].endswith("Checking")
        )
        retirement = next(
            account for account in events["accounts"] if account["name"].endswith("401(k)")
        )
        status, saved = scenario_event_client.post(
            "/api/scenario/event/save",
            {
                "handle": scenario["handle"],
                "name": "Alternate payroll",
                "category": salary["handle"],
                "funding": bank["handle"],
                "amount": "5000.00",
                "additional_splits": [
                    {
                        "account": rent["handle"],
                        "amount": "1000.00",
                        "planning_flow": None,
                    },
                    {
                        "account": retirement["handle"],
                        "amount": "600.00",
                        "planning_flow": "retirement_saving",
                    },
                ],
                "frequency": "monthly",
                "start": "2026-03-01",
                "weekend": "none",
            },
        )
        assert status == 200
        change = saved["changes"][0]
        assert change["simple"] is True
        assert change["additional_splits"] == [
            {
                "account": rent["handle"],
                "amount": "1000.00",
                "planning_flow": None,
            },
            {
                "account": retirement["handle"],
                "amount": "600.00",
                "planning_flow": "retirement_saving",
            },
        ]

    def test_scenario_can_directly_schedule_an_investment_contribution(self, scenario_event_client):
        scenario = self._saved_scenario(scenario_event_client)
        _status, events = scenario_event_client.get(
            "/api/scenario/events?" + urllib.parse.urlencode({"handle": scenario["handle"]})
        )
        holding = next(
            account for account in events["accounts"] if account["name"].endswith("401(k)")
        )
        bank = next(
            account for account in events["accounts"] if account["name"].endswith("Checking")
        )
        account = scenario_event_client.database.get_account(holding["handle"])
        assert account is not None
        account.atype = AccountType.INVESTMENT
        with scenario_event_client.database.transaction("Classify investment fixture") as txn:
            scenario_event_client.database.commit_account(account, txn)

        status, saved = scenario_event_client.post(
            "/api/scenario/event/save",
            {
                "handle": scenario["handle"],
                "name": "Scenario contribution",
                "category": holding["handle"],
                "funding": bank["handle"],
                "investment_activity": "contribution",
                "amount": "300.00",
                "frequency": "monthly",
                "start": "2026-03-01",
                "weekend": "none",
            },
        )

        assert status == 200
        change = saved["changes"][0]
        assert change["investment_activity"] == "contribution"
        stored = scenario_event_client.database.get_scenario(scenario["handle"])
        assert stored is not None
        assert (
            stored.schedule_overrides[0].splits[0].investment_activity
            is InvestmentActivityKind.CONTRIBUTION
        )

    def test_scenario_estimate_can_skip_and_override_occurrences(self, scenario_event_client):
        scenario = self._saved_scenario(scenario_event_client)
        _status, events = scenario_event_client.get(
            "/api/scenario/events?" + urllib.parse.urlencode({"handle": scenario["handle"]})
        )
        rent = next(account for account in events["accounts"] if account["name"].endswith("Rent"))
        bank = next(
            account for account in events["accounts"] if account["name"].endswith("Checking")
        )
        status, saved = scenario_event_client.post(
            "/api/scenario/event/save",
            {
                "handle": scenario["handle"],
                "name": "Flexible rent estimate",
                "category": rent["handle"],
                "funding": bank["handle"],
                "amount": "1500.00",
                "frequency": "monthly",
                "start": "2026-03-01",
                "skipped": ["2026-05-01"],
                "occurrence_adjustments": [{"when": "2026-06-01", "amount": "1750.00"}],
                "weekend": "none",
            },
        )
        assert status == 200
        change = saved["changes"][0]
        assert change["skipped"] == ["2026-05-01"]
        assert change["occurrence_adjustments"] == [{"when": "2026-06-01", "amount": "1750.00"}]

    def test_scenario_estimate_can_end_after_occurrence_count(self, scenario_event_client):
        scenario = self._saved_scenario(scenario_event_client)
        _status, events = scenario_event_client.get(
            "/api/scenario/events?" + urllib.parse.urlencode({"handle": scenario["handle"]})
        )
        rent = next(account for account in events["accounts"] if account["name"].endswith("Rent"))
        bank = next(
            account for account in events["accounts"] if account["name"].endswith("Checking")
        )
        _status, saved = scenario_event_client.post(
            "/api/scenario/event/save",
            {
                "handle": scenario["handle"],
                "name": "Six month rent estimate",
                "category": rent["handle"],
                "funding": bank["handle"],
                "amount": "1500.00",
                "frequency": "monthly",
                "start": "2026-03-01",
                "count": "6",
                "weekend": "none",
            },
        )
        change = saved["changes"][0]
        assert change["count"] == 6
        assert change["end"] is None

    def test_scenario_estimate_can_have_end_date(self, scenario_event_client):
        scenario = self._saved_scenario(scenario_event_client)
        _status, events = scenario_event_client.get(
            "/api/scenario/events?" + urllib.parse.urlencode({"handle": scenario["handle"]})
        )
        rent = next(account for account in events["accounts"] if account["name"].endswith("Rent"))
        bank = next(
            account for account in events["accounts"] if account["name"].endswith("Checking")
        )
        _status, saved = scenario_event_client.post(
            "/api/scenario/event/save",
            {
                "handle": scenario["handle"],
                "name": "Temporary rent estimate",
                "category": rent["handle"],
                "funding": bank["handle"],
                "amount": "1500.00",
                "frequency": "monthly",
                "start": "2026-03-01",
                "end": "2026-08-31",
                "weekend": "none",
            },
        )
        change = saved["changes"][0]
        assert change["end"] == "2026-08-31"
        assert change["count"] is None

    def test_baseline_can_be_altered_then_suppressed(self, scenario_event_client):
        scenario = self._saved_scenario(scenario_event_client)
        _status, events = scenario_event_client.get(
            "/api/scenario/events?" + urllib.parse.urlencode({"handle": scenario["handle"]})
        )
        source = events["baseline"][0]
        assert source["simple"] is True
        _status, altered = scenario_event_client.post(
            "/api/scenario/event/save",
            {
                "handle": scenario["handle"],
                "source_schedule": source["handle"],
                "name": source["name"],
                "category": source["category"],
                "funding": source["funding"],
                "amount": "1600.00",
                "frequency": source["frequency"],
                "start": source["start"],
                "weekend": source["weekend"],
            },
        )
        assert altered["changes"][0]["enabled"] is True
        assert altered["changes"][0]["source_schedule"] == source["handle"]
        assert altered["changes"][0]["amount"] == "1600.00"

        _status, suppressed = scenario_event_client.post(
            "/api/scenario/event/suppress",
            {"handle": scenario["handle"], "source_schedule": source["handle"]},
        )
        assert len(suppressed["changes"]) == 1
        assert suppressed["changes"][0]["enabled"] is False
        assert suppressed["changes"][0]["source_schedule"] == source["handle"]

    def test_page_has_sticky_plan_context_and_dashboard_group_cards(self, client):
        _status, body, _headers = client.raw("/app.js")
        script = body.decode()
        _status, body, _headers = client.raw("/style.css")
        style = body.decode()
        assert ".plan-table th:first-child, .plan-table td:first-child" in style
        assert "max-height: calc(100vh - 310px)" in style
        assert 'class:"balance-groups"' in script
        assert '"Add estimate…"' in script
        assert '"Alter baseline…"' in script
        assert '"Suppress baseline…"' in script
        assert '"Review…"' in script
        assert "historicalEstimateDialog" in script


def test_historical_estimate_proposals_and_acceptance(client):
    status, data = client.get("/api/historical-estimates?months=12&min_active_months=1")
    assert status == 200
    rent = next(item for item in data["proposals"] if item["category_name"].endswith("Rent"))
    assert rent["purpose_name"].endswith("Rent")
    assert rent["planning_flow"] is None
    assert rent["investment_activity"] is None
    assert rent["funding_name"].endswith("Checking")
    assert Money(rent["amount"]) == Money("1800.00")
    assert rent["frequency_key"] == "once"
    assert rent["seasonal_amounts"] == []
    assert rent["evidence"]["selected_months"] == 1
    assert rent["evidence"]["cadence"]["label"] == "once (single observation)"
    assert rent["evidence"]["funding"]["selected"] == rent["funding"]
    assert rent["evidence"]["confidence"]["score"] == rent["confidence"]
    assert rent["evidence"]["history"][0].keys() == {
        "month",
        "gross",
        "planned",
        "residual",
        "selected",
        "exclusion",
    }
    assert data["targets"][0]["name"] == "Base"

    status, result = client.post(
        "/api/historical-estimate/accept",
        {
            "key": rent["key"],
            "category": rent["category"],
            "months": 12,
            "min_active_months": 1,
            "scenario": None,
        },
    )
    assert status == 200
    assert result["category"].endswith("Rent")
    _status, scheduled = client.get("/api/scheduled")
    accepted = next(item for item in scheduled["definitions"] if item["handle"] == result["handle"])
    assert accepted["placeholder"] is True
    assert Money(accepted["amount"]) == Money("1800.00")


def test_historical_estimate_draft_can_be_adjusted_before_save(client):
    _status, data = client.get("/api/historical-estimates?months=12&min_active_months=1")
    proposal = next(item for item in data["proposals"] if item["category_name"].endswith("Rent"))
    draft = proposal["draft"]
    assert draft["estimate_evidence"] == proposal["evidence"]

    payload = {
        **draft,
        "amount": "1900",
        "frequency": "biweekly",
        "start": "2026-10-02",
        "category_planning_flow": "debt_principal",
        "seasonal_amounts": [
            {"month": 1, "amount": "2100"},
            {"month": 7, "amount": "1750"},
        ],
    }
    status, saved = client.post("/api/scheduled/save", payload)
    assert status == 200
    restored = client.database.get_scheduled(saved["handle"])
    assert restored is not None
    assert restored.amount() == Money("1900")
    assert restored.recurrence.period is PeriodType.WEEK
    assert restored.recurrence.interval == 2
    assert restored.recurrence.start == date(2026, 10, 2)
    assert restored.splits[0].planning_flow is PlanningFlowKind.DEBT_PRINCIPAL
    assert [(item.month, item.amount) for item in restored.seasonal_amounts] == [
        (1, Money("2100")),
        (7, Money("1750")),
    ]
    assert restored.estimate_evidence == proposal["evidence"]


def test_fsa_claim_can_use_multiple_allocations(client):
    _status, accounts = client.get("/api/accounts")
    fsa_account = next(row for row in accounts if row["name"] == "401(k)")
    client.post(
        "/api/account/type",
        {"handle": fsa_account["handle"], "type": "FSA"},
    )
    client.post(
        "/api/account/fsa-years",
        {
            "handle": fsa_account["handle"],
            "years": [
                {
                    "start": "2026-01-01",
                    "through": "2026-12-31",
                    "election": "3000.00",
                    "runout_through": "2027-03-31",
                }
            ],
        },
    )
    client.post(
        "/api/transaction",
        {
            "date": "2026-02-10",
            "description": "Dental service",
            "to": "Expenses:Rent",
            "from": "Assets:Checking",
            "amount": "500.00",
        },
    )
    client.post(
        "/api/transaction",
        {
            "date": "2026-02-20",
            "description": "FSA reimbursement",
            "to": "Assets:Checking",
            "from": "Assets:401(k)",
            "amount": "300.00",
        },
    )
    status, payload = client.get("/api/fsa/claims")
    assert status == 200
    payment = next(
        item
        for item in payload["candidates"]["payments"]
        if item["description"] == "Dental service"
    )
    reimbursement = next(
        item
        for item in payload["candidates"]["reimbursements"]
        if item["description"] == "FSA reimbursement"
    )
    status, saved = client.post(
        "/api/fsa/claim/save",
        {
            "service_date": "2026-02-01",
            "provider": "Dentist",
            "eob_responsibility": "500.00",
            "payments": [
                {
                    "transaction": payment["transaction"],
                    "split": payment["split"],
                }
            ],
            "allocations": [
                {
                    "account": fsa_account["handle"],
                    "funding_year_start": "2026-01-01",
                    "target": [50000, 100],
                    "reimbursements": [
                        {
                            "transaction": reimbursement["transaction"],
                            "split": reimbursement["split"],
                        }
                    ],
                }
            ],
        },
    )
    assert status == 200
    assert saved["handle"]
    _status, claims = client.get("/api/fsa/claims")
    claim = claims["claims"][0]
    assert claim["provider"] == "Dentist"
    assert claim["paid"] == "500.00"
    assert claim["reimbursed"] == "300.00"
    assert claim["remaining"] == "200.00"
    assert claim["status"] == "partial"

    client.post(
        "/api/transaction",
        {
            "date": "2026-02-25",
            "description": "Dental provider refund",
            "to": "Assets:Checking",
            "from": "Expenses:Rent",
            "amount": "50.00",
        },
    )
    _status, refreshed = client.get("/api/fsa/claims")
    refund = next(
        item
        for item in refreshed["candidates"]["refunds"]
        if item["description"] == "Dental provider refund"
    )
    allocation = claim["allocations"][0]
    allocation["rejections"] = [
        {
            "attempted_on": "2026-02-15",
            "amount": [10000, 100],
            "reason": "Receipt required",
        }
    ]
    status, _saved = client.post(
        "/api/fsa/claim/save",
        {
            "handle": claim["handle"],
            "service_date": claim["service_date"],
            "provider": "Dentist updated",
            "eob_responsibility": "450.00",
            "payments": claim["payments"],
            "refunds": [
                {
                    "transaction": refund["transaction"],
                    "split": refund["split"],
                }
            ],
            "allocations": [allocation],
        },
    )
    assert status == 200
    _status, edited = client.get("/api/fsa/claims")
    claim = edited["claims"][0]
    assert claim["provider"] == "Dentist updated"
    assert claim["provider_refunds"] == "50.00"
    assert claim["net_paid"] == "450.00"
    assert claim["rejected"] == "100.00"


def test_review_can_attach_actual_to_existing_fsa_claim(client):
    _status, accounts = client.get("/api/accounts")
    fsa_account = next(row for row in accounts if row["name"] == "401(k)")
    client.post(
        "/api/account/type",
        {"handle": fsa_account["handle"], "type": "FSA"},
    )
    client.post(
        "/api/account/fsa-years",
        {
            "handle": fsa_account["handle"],
            "years": [
                {
                    "start": "2026-01-01",
                    "through": "2026-12-31",
                    "election": "3000.00",
                    "runout_through": "2027-03-31",
                }
            ],
        },
    )
    _status, saved = client.post(
        "/api/fsa/claim/save",
        {
            "service_date": "2026-04-01",
            "provider": "Clinic",
            "eob_responsibility": "250.00",
            "payments": [],
            "allocations": [],
        },
    )
    _status, txn = client.post(
        "/api/transaction",
        {
            "date": "2026-04-02",
            "description": "Clinic payment",
            "to": "Expenses:Rent",
            "from": "Assets:Checking",
            "amount": "250.00",
        },
    )
    _status, review = client.get(f"/api/review?transaction={txn['handle']}")
    suggestion = review["selected"]["fsa"]["claims"][0]
    assert suggestion["handle"] == saved["handle"]
    assert suggestion["suggested_role"] == "payment"
    assert suggestion["suggested_split"]
    assert "likely provider payment" in suggestion["reason"]
    role = next(item for item in review["selected"]["fsa"]["roles"] if item["role"] == "payment")
    status, result = client.post(
        "/api/review/fsa-attach",
        {
            "transaction": txn["handle"],
            "claim": saved["handle"],
            "role": "payment",
            "split": role["split"],
        },
    )
    assert status == 200
    assert result["claim"] == saved["handle"]
    with pytest.raises(urllib.error.HTTPError) as caught:
        client.post(
            "/api/review/fsa-attach",
            {
                "transaction": txn["handle"],
                "claim": saved["handle"],
                "role": "guess",
                "split": role["split"],
            },
        )
    error = json.loads(caught.value.read())
    assert error["code"] == "claim.attachment.role.invalid"
    assert error["fields"] == ["role"]
    _status, claims = client.get("/api/fsa/claims")
    claim = next(item for item in claims["claims"] if item["handle"] == saved["handle"])
    assert claim["paid"] == "250.00"
    assert claim["remaining"] == "250.00"


def test_entry_can_attach_new_healthcare_payment_to_fsa_claim(client):
    _status, saved = client.post(
        "/api/fsa/claim/save",
        {
            "service_date": "2026-06-01",
            "provider": "Physical therapy",
            "eob_responsibility": "120.00",
            "payments": [],
            "allocations": [],
        },
    )
    status, txn = client.post(
        "/api/transaction",
        {
            "date": "2026-06-02",
            "description": "PT payment",
            "to": "Expenses:Rent",
            "from": "Assets:Checking",
            "amount": "120.00",
            "fsa_claim": saved["handle"],
            "fsa_role": "payment",
        },
    )
    assert status == 200
    assert txn["handle"]
    _status, claims = client.get("/api/fsa/claims")
    claim = next(item for item in claims["claims"] if item["handle"] == saved["handle"])
    assert claim["paid"] == "120.00"
    assert claim["remaining"] == "120.00"


def test_fsa_claim_candidates_share_funding_year_window(client):
    _status, accounts = client.get("/api/accounts")
    fsa_account = next(row for row in accounts if row["name"] == "401(k)")
    client.post(
        "/api/account/type",
        {"handle": fsa_account["handle"], "type": "FSA"},
    )
    client.post(
        "/api/account/fsa-years",
        {
            "handle": fsa_account["handle"],
            "years": [
                {
                    "start": "2026-01-01",
                    "through": "2026-12-31",
                    "election": "3000.00",
                    "runout_through": "2027-03-31",
                }
            ],
        },
    )
    for when, description, to_name, from_name in (
        ("2025-12-15", "Old medical payment", "Expenses:Rent", "Assets:Checking"),
        ("2025-12-20", "Old provider refund", "Assets:Checking", "Expenses:Rent"),
        ("2026-02-10", "Current medical payment", "Expenses:Rent", "Assets:Checking"),
        ("2026-02-20", "Current provider refund", "Assets:Checking", "Expenses:Rent"),
        ("2027-04-15", "Late provider refund", "Assets:Checking", "Expenses:Rent"),
    ):
        client.post(
            "/api/transaction",
            {
                "date": when,
                "description": description,
                "to": to_name,
                "from": from_name,
                "amount": "50.00",
            },
        )

    _status, payload = client.get("/api/fsa/claims")
    payment_names = {item["description"] for item in payload["candidates"]["payments"]}
    refund_names = {item["description"] for item in payload["candidates"]["refunds"]}

    assert "Old medical payment" not in payment_names
    assert "Old provider refund" not in refund_names
    assert "Current medical payment" in payment_names
    assert "Current provider refund" in refund_names
    assert "Late provider refund" in refund_names


def test_review_ranks_likely_fsa_claim_first(client):
    _status, close = client.post(
        "/api/fsa/claim/save",
        {
            "service_date": "2026-07-01",
            "provider": "Easton Dental",
            "description": "Crown",
            "payments": [],
            "allocations": [],
        },
    )
    client.post(
        "/api/fsa/claim/save",
        {
            "service_date": "2026-01-01",
            "provider": "Other clinic",
            "payments": [],
            "allocations": [],
        },
    )
    _status, txn = client.post(
        "/api/transaction",
        {
            "date": "2026-07-03",
            "description": "Easton Dental crown payment",
            "to": "Expenses:Rent",
            "from": "Assets:Checking",
            "amount": "400.00",
        },
    )

    _status, review = client.get(f"/api/review?transaction={txn['handle']}")
    claims = review["selected"]["fsa"]["claims"]

    assert claims[0]["handle"] == close["handle"]
    assert claims[0]["score"] > claims[1]["score"]
    assert "description match" in claims[0]["reason"]


class TestCsvImport:
    """Browser CSV mapping: inspect, preview, and import through the shared service."""

    STATEMENT = "Date,Description,Amount\n2026-02-03,Corner Grocer,-42.10\n2026-02-04,Refund,5.00\n"

    def _request(self, client, path, **extra):
        checking = client.database.get_account_by_name("Checking")
        return {
            "path": str(path),
            "account": checking.handle,
            "mapping": {"date": "Date", "amount": "Amount", "description": "Description"},
            **extra,
        }

    def test_category_column_maps_to_an_account_or_refuses_the_row(self, client, tmp_path):
        path = tmp_path / "categorized.csv"
        path.write_text(
            "Date,Description,Amount,Category\n"
            "2026-02-03,Corner Grocer,-42.10,Expenses:Rent\n"
            "2026-02-04,Mystery,-5.00,Nowhere\n",
            encoding="utf-8",
        )
        mapping = {"date": "Date", "amount": "Amount", "description": "Description"}
        request = self._request(client, path, mapping={**mapping, "category": "Category"})
        status, preview = client.post("/api/import/csv/preview", request)
        assert status == 200
        first, second = preview["rows"]
        assert first["category"] == "Expenses:Rent"
        assert (second["status"], second["reason"]) == (
            "invalid",
            "category 'Nowhere' is not an account in the book",
        )
        before = len(list(client.database.iter_transactions()))
        status, imported = client.post("/api/import/csv", request)
        assert status == 200
        assert imported["new"] == 1
        rent = client.database.get_account_by_name("Expenses:Rent")
        added = [
            item
            for item in client.database.iter_transactions()
            if item.description == "Corner Grocer" and item.post_date.isoformat() == "2026-02-03"
        ]
        assert len(list(client.database.iter_transactions())) == before + 1
        assert rent.handle in {split.account for split in added[0].splits}

    def test_inspect_preview_and_import(self, client, tmp_path):
        path = tmp_path / "statement.csv"
        path.write_text(self.STATEMENT, encoding="utf-8")

        status, inspected = client.post("/api/import/csv/inspect", {"path": str(path)})
        assert status == 200
        assert inspected["columns"] == ["Date", "Description", "Amount"]
        assert inspected["sample"][0] == ["2026-02-03", "Corner Grocer", "-42.10"]
        assert (inspected["encoding"], inspected["delimiter"]) == ("utf-8", ",")

        before = len(list(client.database.iter_transactions()))
        status, preview = client.post("/api/import/csv/preview", self._request(client, path))
        assert status == 200
        assert set(preview) == {
            "encoding",
            "delimiter",
            "date_format",
            "number_format",
            "columns",
            "counts",
            "rows",
        }
        assert preview["counts"] == {
            "new": 2,
            "imported": 0,
            "possible_duplicate": 0,
            "possible_transfer": 0,
            "invalid": 0,
        }
        assert preview["rows"][0] == {
            "line": 2,
            "date": "2026-02-03",
            "amount": "-42.10",
            "description": "Corner Grocer",
            "memo": "",
            "status": "new",
            "reason": "",
            "category": None,
            "payee": None,
            "note": "",
        }
        assert len(list(client.database.iter_transactions())) == before

        status, imported = client.post("/api/import/csv", self._request(client, path))
        assert status == 200
        assert imported["new"] == 2 and imported["already_imported"] == 0
        assert len(list(client.database.iter_transactions())) == before + 2

        status, again = client.post("/api/import/csv/preview", self._request(client, path))
        assert again["counts"]["imported"] == 2

    def test_possible_duplicate_needs_explicit_inclusion(self, client, tmp_path):
        path = tmp_path / "rent.csv"
        # The fixture book already has Rent -1800.00 in Checking on 2026-01-02.
        path.write_text("Date,Description,Amount\n2026-01-02,Rent,-1800.00\n", encoding="utf-8")
        before = len(list(client.database.iter_transactions()))

        _status, preview = client.post("/api/import/csv/preview", self._request(client, path))
        assert preview["rows"][0]["status"] == "possible_duplicate"
        _status, held = client.post("/api/import/csv", self._request(client, path))
        assert held["new"] == 0 and held["possible_duplicates"] == 1
        assert len(list(client.database.iter_transactions())) == before

        _status, included = client.post(
            "/api/import/csv", self._request(client, path, include_duplicates=True)
        )
        assert included["new"] == 1

    def test_transfer_side_is_linked_only_when_asked(self, client, tmp_path):
        savings = client.database.get_account_by_name("401(k)")
        out = tmp_path / "savings.csv"
        out.write_text("Date,Description,Amount\n2026-02-10,To checking,-250.00\n", "utf-8")
        payload = self._request(client, out)
        _status, first = client.post("/api/import/csv", {**payload, "account": savings.handle})
        assert first["new"] == 1
        path = tmp_path / "checking.csv"
        path.write_text("Date,Description,Amount\n2026-02-11,From savings,250.00\n", "utf-8")
        before = len(list(client.database.iter_transactions()))

        _status, preview = client.post("/api/import/csv/preview", self._request(client, path))
        assert preview["counts"]["possible_transfer"] == 1
        assert "401(k)" in preview["rows"][0]["reason"]
        status, linked = client.post(
            "/api/import/csv", self._request(client, path, link_transfers=True)
        )
        assert status == 200
        assert (linked["new"], linked["transfers_linked"]) == (0, 1)
        assert len(list(client.database.iter_transactions())) == before

        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post("/api/import/csv", self._request(client, path, link_transfers="yes"))
        assert caught.value.code == 400
        assert "link_transfers" in json.loads(caught.value.read())["error"]

    def test_rejected_requests_leave_the_book_unchanged(self, client, tmp_path):
        path = tmp_path / "ambiguous.csv"
        path.write_text("Date,Amount\n01/02/2026,-1.00\n", encoding="utf-8")
        good = tmp_path / "good.csv"
        good.write_text(self.STATEMENT, encoding="utf-8")
        before = sorted(item.handle for item in client.database.iter_transactions())
        income = client.database.get_account_by_name("Salary")

        rejected = [
            ({"path": str(path)}, "import.csv.date_format.ambiguous", {"description": None}),
            ({"path": str(good), "account": income.handle}, "import.csv.account.invalid", {}),
            ({"path": str(tmp_path / "missing.csv")}, "import.source.not_found", {}),
            ({"path": str(good)}, "import.csv.column.not_found", {"amount": "Total"}),
        ]
        for override, code, mapping in rejected:
            request = self._request(client, good)
            request.update(override)
            request["mapping"] = {
                key: value
                for key, value in {**request["mapping"], **mapping}.items()
                if value is not None
            }
            with pytest.raises(urllib.error.HTTPError) as caught:
                client.post("/api/import/csv", request)
            assert caught.value.code in (400, 404)
            assert json.loads(caught.value.read())["code"] == code

        for malformed in (
            {**self._request(client, good), "mapping": "Date"},
            {**self._request(client, good), "include_duplicates": "yes"},
            {**self._request(client, good), "mapping": {"date": "Date", "amount": 3}},
        ):
            with pytest.raises(urllib.error.HTTPError) as caught:
                client.post("/api/import/csv", malformed)
            assert caught.value.code == 400
        assert sorted(item.handle for item in client.database.iter_transactions()) == before

    def test_uploaded_csv_is_stored_for_mapping_not_imported(self, client):
        before = len(list(client.database.iter_transactions()))

        status, uploaded = client.upload("bank.csv", self.STATEMENT.encode("utf-8"))

        assert status == 200
        assert uploaded["format"] == "csv"
        assert len(list(client.database.iter_transactions())) == before
        _status, preview = client.post(
            "/api/import/csv/preview", self._request(client, uploaded["path"])
        )
        assert preview["counts"]["new"] == 2


class TestPayees:
    """Browser payee management and proposal review through the shared service."""

    def _rent(self, client):
        return next(txn for txn in client.database.iter_transactions() if txn.description == "Rent")

    def test_add_preview_accept_rename_and_delete(self, client):
        rent = self._rent(client)
        status, empty = client.get("/api/payees")
        assert status == 200 and empty == {"payees": [], "proposals": []}

        status, saved = client.post("/api/payee/save", {"name": "Landlord", "matches": ["RENT"]})
        assert status == 200 and saved["match_keys"] == ["rent"]
        _status, listed = client.get("/api/payees")
        [proposal] = listed["proposals"]
        assert (proposal["transaction"], proposal["payee_name"], proposal["key"]) == (
            rent.handle,
            "Landlord",
            "rent",
        )
        assert client.database.get_transaction(rent.handle).payee is None

        status, accepted = client.post("/api/payees/accept", {"transactions": [rent.handle]})
        assert status == 200 and accepted == {"assigned": 1, "unchanged": 0}
        stored = client.database.get_transaction(rent.handle)
        assert (stored.payee, stored.description) == (saved["handle"], "Rent")
        _status, after = client.get("/api/payees")
        assert after["proposals"] == [] and after["payees"][0]["transactions"] == 1

        client.post(
            "/api/payee/save",
            {"name": "Property manager", "matches": ["rent"], "handle": saved["handle"]},
        )
        assert client.database.get_payee(saved["handle"]).name == "Property manager"
        status, deleted = client.post("/api/payee/delete", {"handle": saved["handle"]})
        assert status == 200 and deleted == {"cleared": 1}
        assert client.database.get_transaction(rent.handle).payee is None

    def test_rejected_requests_leave_payees_unchanged(self, client):
        client.post("/api/payee/save", {"name": "Landlord", "matches": ["rent"]})
        before = [(p.handle, p.name, p.match_keys) for p in client.database.iter_payees()]

        for path, body, status, code in [
            ("/api/payee/save", {"name": "landlord"}, 400, "payee.name.duplicate"),
            (
                "/api/payee/save",
                {"name": "Other", "matches": ["RENT"]},
                400,
                "payee.match.conflict",
            ),
            ("/api/payee/save", {"name": "Other", "matches": "rent"}, 400, None),
            ("/api/payee/save", {"name": 5}, 400, None),
            ("/api/payee/delete", {"handle": "missing"}, 404, "payee.not_found"),
            ("/api/payees/accept", {"transactions": ["missing"]}, 404, None),
        ]:
            with pytest.raises(urllib.error.HTTPError) as caught:
                client.post(path, body)
            assert caught.value.code == status
            if code is not None:
                assert json.loads(caught.value.read())["code"] == code

        assert [(p.handle, p.name, p.match_keys) for p in client.database.iter_payees()] == before

    def test_register_shows_and_sets_a_payee(self, client):
        rent = self._rent(client)
        checking = client.database.get_account_by_name("Checking")
        _status, saved = client.post("/api/payee/save", {"name": "Landlord"})

        _status, register = client.get(f"/api/register?account={checking.handle}")
        assert register["payees"] == [{"handle": saved["handle"], "name": "Landlord"}]
        row = next(item for item in register["rows"] if item["handle"] == rent.handle)
        assert row["payee"] is None

        status, set_ = client.post(
            "/api/transaction/payee", {"transaction": rent.handle, "payee": saved["handle"]}
        )
        assert status == 200 and set_ == {"transaction": rent.handle, "payee": saved["handle"]}
        _status, register = client.get(f"/api/register?account={checking.handle}")
        row = next(item for item in register["rows"] if item["handle"] == rent.handle)
        assert row["payee"] == saved["handle"]
        assert client.database.get_transaction(rent.handle).description == "Rent"

        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post("/api/transaction/payee", {"transaction": rent.handle, "payee": "gone"})
        assert caught.value.code == 404
        assert client.database.get_transaction(rent.handle).payee == saved["handle"]

        status, cleared = client.post(
            "/api/transaction/payee", {"transaction": rent.handle, "payee": None}
        )
        assert status == 200 and cleared["payee"] is None

    def test_web_entry_records_a_chosen_payee(self, client):
        _status, saved = client.post("/api/payee/save", {"name": "Landlord"})
        status, posted = client.post(
            "/api/transaction",
            {
                "date": "2026-02-01",
                "description": "Rent",
                "amount": "10.00",
                "from": "Checking",
                "to": "Expenses:Rent",
                "payee": saved["handle"],
            },
        )
        assert status == 200
        assert client.database.get_transaction(posted["handle"]).payee == saved["handle"]


class TestRules:
    """Browser categorization rules and proposal review through the shared service."""

    def _import(self, client, tmp_path):
        path = tmp_path / "statement.csv"
        path.write_text(
            "Date,Description,Amount\n2026-02-03,CORNER GROCER #1,-42.10\n"
            "2026-02-04,City Power,-60.00\n",
            encoding="utf-8",
        )
        checking = client.database.get_account_by_name("Checking")
        status, imported = client.post(
            "/api/import/csv",
            {
                "path": str(path),
                "account": checking.handle,
                "mapping": {"date": "Date", "amount": "Amount", "description": "Description"},
            },
        )
        assert status == 200 and imported["new"] == 2
        return {
            txn.description: txn.handle
            for txn in client.database.iter_transactions()
            if txn.description in {"CORNER GROCER #1", "City Power"}
        }

    def test_add_preview_accept_move_and_delete(self, client, tmp_path):
        handles = self._import(client, tmp_path)
        rent = client.database.get_account_by_name("Expenses:Rent")
        status, empty = client.get("/api/rules")
        assert status == 200 and empty["rules"] == [] and empty["proposals"] == []
        assert rent.handle in {item["handle"] for item in empty["categories"]}

        status, added = client.post(
            "/api/rule/add", {"category": rent.handle, "description": "city power"}
        )
        assert status == 200 and added["key"] == "city power"
        client.post("/api/rule/add", {"category": rent.handle, "description": "corner grocer"})
        _status, listed = client.get("/api/rules")
        assert [rule["position"] for rule in listed["rules"]] == [1, 2]
        assert {item["transaction"] for item in listed["proposals"]} == set(handles.values())
        [power] = [item for item in listed["proposals"] if item["description"] == "City Power"]
        assert (power["rule_position"], power["amount"]) == (1, "60.00")

        status, accepted = client.post(
            "/api/rules/accept", {"transactions": [handles["City Power"]]}
        )
        assert status == 200 and accepted == {"assigned": 1, "unchanged": 0}
        stored = client.database.get_transaction(handles["City Power"])
        assert rent.handle in {split.account for split in stored.splits}
        assert stored.description == "City Power"

        second = listed["rules"][1]["handle"]
        status, _moved = client.post("/api/rule/move", {"handle": second, "position": 1})
        assert status == 200
        _status, moved = client.get("/api/rules")
        assert moved["rules"][0]["handle"] == second
        status, _deleted = client.post("/api/rule/delete", {"handle": second})
        assert status == 200
        _status, after = client.get("/api/rules")
        assert [rule["handle"] for rule in after["rules"]] == [added["handle"]]

    def test_rejected_requests_leave_rules_unchanged(self, client, tmp_path):
        rent = client.database.get_account_by_name("Expenses:Rent")
        checking = client.database.get_account_by_name("Checking")
        client.post("/api/rule/add", {"category": rent.handle, "description": "rent"})
        _status, before = client.get("/api/rules")

        for path, body, status, code in [
            ("/api/rule/add", {"category": rent.handle}, 400, "rule.match.required"),
            (
                "/api/rule/add",
                {"category": checking.handle, "description": "x"},
                400,
                "rule.category.invalid",
            ),
            (
                "/api/rule/add",
                {"category": rent.handle, "description": "RENT"},
                400,
                "rule.match.duplicate",
            ),
            ("/api/rule/add", {"category": rent.handle, "description": 5}, 400, None),
            (
                "/api/rule/move",
                {"handle": before["rules"][0]["handle"], "position": 7},
                400,
                "rule.position.invalid",
            ),
            (
                "/api/rule/move",
                {"handle": before["rules"][0]["handle"], "position": "1"},
                400,
                None,
            ),
            ("/api/rule/delete", {"handle": "missing"}, 404, "rule.not_found"),
            ("/api/rules/accept", {"transactions": ["missing"]}, 404, None),
        ]:
            with pytest.raises(urllib.error.HTTPError) as caught:
                client.post(path, body)
            assert caught.value.code == status
            if code is not None:
                assert json.loads(caught.value.read())["code"] == code

        _status, after = client.get("/api/rules")
        assert after["rules"] == before["rules"]


class TestEntrySuggestion:
    """Entry autocomplete proposals through the shared service; nothing is written."""

    def test_the_latest_matching_entry_is_proposed(self, client):
        checking = client.database.get_account_by_name("Checking")
        before = len(list(client.database.iter_transactions()))

        status, data = client.get(
            f"/api/entry/suggest?account={checking.handle}&description=RENT%20%2312"
        )

        assert status == 200
        suggestion = data["suggestion"]
        assert suggestion["description"] == "Rent"
        assert suggestion["amount"] == "-1800.00"
        assert suggestion["transfer_name"] == "Expenses:Rent"
        assert len(list(client.database.iter_transactions())) == before

    def test_no_match_and_bad_queries(self, client):
        checking = client.database.get_account_by_name("Checking")
        status, data = client.get(
            f"/api/entry/suggest?account={checking.handle}&description=Nothing%20like%20it"
        )
        assert status == 200 and data == {"suggestion": None}

        with pytest.raises(urllib.error.HTTPError) as caught:
            client.get("/api/entry/suggest?account=missing&description=rent")
        assert caught.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.get("/api/entry/suggest?description=rent&surprise=1")
        assert caught.value.code == 400


class TestRegisterEntry:
    """#158 on the web: the register's blank row and in-place edits.

    The adapter only parses JSON; validation and the write stay in the shared
    transaction service, so a rejected request leaves the stored transaction as it
    was.
    """

    def _accounts(self, client):
        db = client.database
        return db.get_account_by_name("Assets:Checking"), db.get_account_by_name("Expenses:Rent")

    def _rent(self, client):
        return next(txn for txn in client.database.iter_transactions() if txn.description == "Rent")

    def test_a_new_entry_posts_balanced_splits_from_exact_pairs(self, client):
        checking, rent = self._accounts(client)
        status, saved = client.post(
            "/api/register/entry",
            {
                "date": "2026-04-02",
                "num": "12",
                "description": "Shared bill",
                "payee": None,
                "splits": [
                    {"account": checking.handle, "value": ["-100000000", "1000000"]},
                    {"account": rent.handle, "value": ["60000000", "1000000"], "memo": "rent"},
                    {"account": rent.handle, "value": "40.00", "memo": "more rent"},
                ],
            },
        )
        assert status == 200
        stored = client.database.get_transaction(saved["handle"])
        assert (stored.description, stored.num, str(stored.post_date)) == (
            "Shared bill",
            "12",
            "2026-04-02",
        )
        assert sorted(split.value for split in stored.splits) == [
            Money("-100"),
            Money("40"),
            Money("60"),
        ]
        assert {split.memo for split in stored.splits} == {"", "rent", "more rent"}

    def test_an_edit_keeps_handles_notes_purposes_and_quantities(self, client):
        from breadsched.gen.lib.transaction import PlanningFlowKind

        db = client.database
        rent_txn = self._rent(client)
        rent_txn.notes = "Lease 12B"
        _checking, rent = self._accounts(client)
        for split in rent_txn.splits:
            if split.account == rent.handle:
                split.planning_flow = PlanningFlowKind.RETIREMENT_SAVING
        with db.transaction("Fixture") as txn:
            db.commit_transaction(rent_txn, txn)

        status, register = client.get(
            f"/api/register?account={db.get_account_by_name('Assets:Checking').handle}"
        )
        row = next(item for item in register["rows"] if item["handle"] == rent_txn.handle)
        assert row["split"] in {split["handle"] for split in row["splits"]}
        splits = [
            {
                "handle": split["handle"],
                "account": split["account"],
                "value": "-1850.00" if split["value"].startswith("-") else "1850.00",
                "memo": split["memo"],
            }
            for split in row["splits"]
        ]
        status, _saved = client.post(
            "/api/register/entry",
            {
                "handle": rent_txn.handle,
                "date": row["date"],
                "description": "Rent, corrected",
                "splits": splits,
            },
        )
        assert status == 200
        stored = db.get_transaction(rent_txn.handle)
        assert stored.description == "Rent, corrected"
        assert stored.notes == "Lease 12B"
        assert {s.handle for s in stored.splits} == {s.handle for s in rent_txn.splits}
        mine = next(s for s in stored.splits if s.account == rent.handle)
        assert mine.planning_flow is PlanningFlowKind.RETIREMENT_SAVING
        assert all(s.quantity == s.value for s in stored.splits)

    def test_rejected_entries_leave_the_stored_transaction_unchanged(self, client):
        db = client.database
        checking, rent = self._accounts(client)
        rent_txn = self._rent(client)
        before = rent_txn.serialize()
        count = len(list(db.iter_transactions()))
        good = [
            {"account": checking.handle, "value": "-5.00", "handle": rent_txn.splits[0].handle},
            {"account": rent.handle, "value": "5.00", "handle": rent_txn.splits[1].handle},
        ]
        for body, status, code in [
            (
                {
                    "handle": rent_txn.handle,
                    "date": "2026-01-01",
                    "description": "Unbalanced",
                    "splits": [dict(good[0], value="-5.00"), dict(good[1], value="4.00")],
                },
                400,
                "transaction.unbalanced",
            ),
            (
                {
                    "handle": rent_txn.handle,
                    "date": "not a date",
                    "description": "x",
                    "splits": good,
                },
                400,
                None,
            ),
            (
                {
                    "handle": rent_txn.handle,
                    "date": "2026-01-01",
                    "description": "x",
                    "splits": [dict(good[0], handle="missing"), good[1]],
                },
                404,
                "transaction.split.not_found",
            ),
            (
                {
                    "date": "2026-01-01",
                    "description": "x",
                    "splits": [dict(good[0], value=["1", "0"]), good[1]],
                },
                400,
                None,
            ),
            ({"date": "2026-01-01", "description": "x", "splits": "nope"}, 400, None),
            (
                {"handle": "missing", "date": "2026-01-01", "description": "x", "splits": good},
                404,
                "transaction.not_found",
            ),
        ]:
            with pytest.raises(urllib.error.HTTPError) as caught:
                client.post("/api/register/entry", body)
            assert caught.value.code == status, body
            if code is not None:
                assert json.loads(caught.value.read())["code"] == code
        assert db.get_transaction(rent_txn.handle).serialize() == before
        assert len(list(db.iter_transactions())) == count


class TestReceivablesRoutes:
    """Reimbursable expenses on the web, entirely through the shared service."""

    def _rent(self, client):
        db = client.database
        rent_txn = next(t for t in db.iter_transactions() if t.description == "Rent")
        rent = db.get_account_by_name("Expenses:Rent")
        cost = next(s for s in rent_txn.splits if s.account == rent.handle)
        return rent_txn, rent, cost

    def test_track_link_dispute_write_off_and_delete(self, client):
        from breadsched.gen.lib import Transaction

        db = client.database
        rent_txn, rent, cost = self._rent(client)
        checking = db.get_account_by_name("Assets:Checking")
        refund = Transaction.simple(
            rent_txn.post_date, "Insurer refund", checking.handle, rent.handle, Money("200")
        )
        with db.transaction("Refund fixture") as txn:
            db.add_transaction(refund, txn)
        ledger_before = [t.serialize() for t in db.iter_transactions()]

        _status, listed = client.get("/api/receivables")
        assert listed["receivables"] == []
        assert {"transaction": rent_txn.handle, "split": cost.handle} in [
            {"transaction": c["transaction"], "split": c["split"]} for c in listed["costs"]
        ]
        status, saved = client.post(
            "/api/receivable/save",
            {
                "payer": "Acme Insurance",
                "incurred_date": str(rent_txn.post_date),
                "expected_amount": "500.00",
                "link_expense": {"transaction": rent_txn.handle, "split": cost.handle},
            },
        )
        assert status == 200 and saved["linked"] is True
        handle = saved["handle"]
        credit = next(s for s in refund.splits if s.account == rent.handle)
        client.post(
            "/api/receivable/link",
            {
                "receivable": handle,
                "role": "reimbursement",
                "transaction": refund.handle,
                "split": credit.handle,
            },
        )
        _status, listed = client.get("/api/receivables")
        [item] = listed["receivables"]
        assert item["status"] == "partial" and item["reimbursed"] == "200.00"
        assert item["expenses"][0]["description"] == "Rent"
        # What is still owed is held in a Receivable account, never in liquidity (#170).
        assert item["owed"] == "500.00" and item["fsa_claims"] == []
        assert item["account_name"].endswith("Reimbursements Receivable")
        assert [account["handle"] for account in listed["accounts"]] == [item["account"]]
        assert client.get("/api/dashboard")[1]["summary"]["receivables_owed"] == "300.00"

        client.post(
            "/api/receivable/dispute",
            {"handle": handle, "disputed_on": "2026-03-01", "note": "Out of network"},
        )
        assert client.get("/api/receivables")[1]["receivables"][0]["status"] == "disputed"
        client.post("/api/receivable/dispute", {"handle": handle, "clear": True})
        client.post(
            "/api/receivable/write-off",
            {"receivable": handle, "amount": "50.00", "written_off_on": "2026-03-02"},
        )
        assert client.get("/api/receivables")[1]["receivables"][0]["written_off"] == "50.00"
        client.post(
            "/api/receivable/unlink",
            {"receivable": handle, "transaction": refund.handle, "split": credit.handle},
        )
        client.post("/api/receivable/delete", {"handle": handle})
        assert list(db.iter_receivables()) == []
        assert [t.serialize() for t in db.iter_transactions()] == ledger_before

    def test_rejected_requests_leave_receivables_unchanged(self, client):
        rent_txn, _rent, cost = self._rent(client)
        _status, saved = client.post(
            "/api/receivable/save", {"payer": "Acme", "incurred_date": "2026-01-02"}
        )
        before = client.database.get_receivable(saved["handle"]).serialize()
        for path, body, status, code in [
            (
                "/api/receivable/save",
                {"payer": " ", "incurred_date": "2026-01-02"},
                400,
                "receivable.payer.required",
            ),
            ("/api/receivable/save", {"payer": "X", "incurred_date": "soon"}, 400, None),
            (
                "/api/receivable/save",
                {"payer": "X", "incurred_date": "2026-01-02", "expected_amount": "-1"},
                400,
                "receivable.expected_amount.negative",
            ),
            (
                "/api/receivable/link",
                {
                    "receivable": saved["handle"],
                    "role": "reimbursement",
                    "transaction": rent_txn.handle,
                    "split": cost.handle,
                },
                400,
                "receivable.split.not_a_credit",
            ),
            (
                "/api/receivable/link",
                {
                    "receivable": saved["handle"],
                    "role": "other",
                    "transaction": rent_txn.handle,
                    "split": cost.handle,
                },
                400,
                None,
            ),
            (
                "/api/receivable/write-off",
                {"receivable": saved["handle"], "amount": "0", "written_off_on": "2026-01-03"},
                400,
                "receivable.write_off.amount_not_positive",
            ),
            (
                "/api/receivable/save",
                {
                    "handle": saved["handle"],
                    "payer": "Acme",
                    "incurred_date": "2026-01-02",
                    "account": rent_txn.splits[0].account,
                },
                400,
                "receivable.account.not_receivable",
            ),
            ("/api/receivable/delete", {"handle": "missing"}, 404, "receivable.not_found"),
        ]:
            with pytest.raises(urllib.error.HTTPError) as caught:
                client.post(path, body)
            assert caught.value.code == status, (path, body)
            if code is not None:
                assert json.loads(caught.value.read())["code"] == code
        assert client.database.get_receivable(saved["handle"]).serialize() == before
        assert len(list(client.database.iter_receivables())) == 1

    def test_an_fsa_claim_covers_the_rest_of_a_receivable(self, client):
        # Issue #192: linked from the claim; the FSA share waits for the EOB.
        db = client.database
        rent_txn, _rent, cost = self._rent(client)
        link = {"transaction": rent_txn.handle, "split": cost.handle}
        _status, saved = client.post(
            "/api/receivable/save",
            {
                "payer": "Acme Insurance",
                "incurred_date": str(rent_txn.post_date),
                "expected_amount": "500.00",
                "link_expense": link,
            },
        )
        receivable = saved["handle"]
        _status, claims = client.get("/api/fsa/claims")
        assert receivable in [item["handle"] for item in claims["candidates"]["receivables"]]
        body = {
            "service_date": str(rent_txn.post_date),
            "provider": "Clinic",
            "payments": [link],
            "allocations": [],
            "receivable": receivable,
        }
        _status, claim = client.post("/api/fsa/claim/save", body)
        _status, claims = client.get("/api/fsa/claims")
        [row] = claims["claims"]
        shared = row["shared"]
        assert row["receivable"] == receivable and row["status"] == "waiting_eob"
        assert (shared["payer_share"], shared["fsa_share"]) == ("500.00", "0.00")
        assert shared["expense"] == str(cost.value.to_decimal().quantize(Decimal("0.01")))
        assert shared["waiting_eob"] and not shared["needs_review"]
        assert "until the EOB is entered" in shared["text"]
        _status, listed = client.get("/api/receivables")
        [item] = listed["receivables"]
        assert item["fsa_claims"] == []
        assert [cost["claim"] for cost in item["shared_costs"]] == [claim["handle"]]

        before = db.get_fsa_claim(claim["handle"]).serialize()
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post(
                "/api/fsa/claim/save",
                {**body, "handle": claim["handle"], "receivable": "missing"},
            )
        assert caught.value.code == 404
        assert json.loads(caught.value.read())["code"] == "claim.receivable.not_found"
        assert db.get_fsa_claim(claim["handle"]).serialize() == before

        client.post("/api/receivable/delete", {"handle": receivable})
        assert db.get_fsa_claim(claim["handle"]).receivable is None
        assert client.get("/api/fsa/claims")[1]["claims"][0]["shared"] is None

    def test_proposals_are_listed_and_accepted_only_when_chosen(self, client):
        from breadsched.gen.lib import Transaction

        db = client.database
        rent_txn, rent, cost = self._rent(client)
        _status, saved = client.post(
            "/api/receivable/save",
            {
                "payer": "Acme Insurance",
                "incurred_date": str(rent_txn.post_date),
                "link_expense": {"transaction": rent_txn.handle, "split": cost.handle},
            },
        )
        checking = db.get_account_by_name("Assets:Checking")
        refund = Transaction.simple(
            rent_txn.post_date, "Acme Insurance", checking.handle, rent.handle, Money("75")
        )
        with db.transaction("Refund fixture") as txn:
            db.add_transaction(refund, txn)
        credit = next(s for s in refund.splits if s.account == rent.handle)

        _status, listed = client.get("/api/receivables")
        [proposal] = listed["proposals"]
        assert (proposal["receivable"], proposal["split"], proposal["amount"]) == (
            saved["handle"],
            credit.handle,
            "75.00",
        )
        _status, none = client.post("/api/receivables/accept", {"links": []})
        assert none == {"linked": 0, "unchanged": 0}
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post("/api/receivables/accept", {"links": "all"})
        assert caught.value.code == 400
        assert db.get_receivable(saved["handle"]).reimbursements == []

        link = [saved["handle"], refund.handle, credit.handle]
        _status, accepted = client.post("/api/receivables/accept", {"links": [link]})
        assert accepted == {"linked": 1, "unchanged": 0}
        assert client.get("/api/receivables")[1]["proposals"] == []

    def test_reconciliation_and_imports_point_at_waiting_proposals(self, client):
        from breadsched.gen.lib import Transaction

        db = client.database
        rent_txn, rent, cost = self._rent(client)
        client.post(
            "/api/receivable/save",
            {
                "payer": "Acme Insurance",
                "incurred_date": str(rent_txn.post_date),
                "link_expense": {"transaction": rent_txn.handle, "split": cost.handle},
            },
        )
        checking = db.get_account_by_name("Assets:Checking")
        _status, before = client.get(f"/api/reconciliation?account={checking.handle}")
        assert before["reimbursement_notice"] is None
        refund = Transaction.simple(
            rent_txn.post_date, "Acme Insurance", checking.handle, rent.handle, Money("75")
        )
        with db.transaction("Refund fixture") as txn:
            db.add_transaction(refund, txn)
        _status, after = client.get(f"/api/reconciliation?account={checking.handle}")
        assert after["reimbursement_notice"].startswith("1 credit looks like money back")
        salary = db.get_account_by_name("Income:Salary")
        _status, elsewhere = client.get(f"/api/reconciliation?account={salary.handle}")
        assert elsewhere["reimbursement_notice"] is None


class TestGnuCashWritebackRoutes:
    """#174: the browser previews and writes through the shared write-back service."""

    def test_preview_write_and_rejections(self, client, gnucash_sqlite_path):
        import sqlite3

        from breadsched.gen.services.imports import ImportBook, import_book

        def refused(call, *args) -> int:
            with pytest.raises(urllib.error.HTTPError) as caught:
                call(*args)
            return caught.value.code

        db = client.database
        # The served book may already hold a "Rent" of its own; take the imported one.
        before = {item.handle for item in db.iter_transactions()}
        assert import_book(db, ImportBook(source=gnucash_sqlite_path.path, notify=False)).ok
        rent = next(
            item
            for item in db.iter_transactions()
            if item.description == "Rent" and item.handle not in before
        )
        rent.description = "Rent from the browser"
        with db.transaction("Edit") as txn:
            db.commit_transaction(rent, txn)
        original = open(gnucash_sqlite_path.path, "rb").read()

        status, preview = client.get("/api/gnucash/writeback")
        assert status == 200 and preview["available"] is True
        assert [item["transaction"] for item in preview["changes"]] == [rent.handle]
        assert preview["changes"][0]["kinds"] == ["edit"]

        assert refused(client.post, "/api/gnucash/writeback", {"transactions": "x"}) == 400
        assert refused(client.post, "/api/gnucash/writeback", {"transactions": []}) == 400
        assert refused(client.post, "/api/gnucash/writeback/settings", {"keep_backups": 0}) == 400
        assert refused(client.get, "/api/gnucash/writeback?extra=1") == 400
        assert open(gnucash_sqlite_path.path, "rb").read() == original
        assert db.get_metadata("gnucash.writeback.keep_backups") is None

        status, saved = client.post("/api/gnucash/writeback/settings", {"keep_backups": 4})
        assert status == 200 and saved == {"keep_backups": 4}
        status, written = client.post("/api/gnucash/writeback", {"transactions": [rent.handle]})
        assert status == 200 and written["written"] == [rent.handle]
        conn = sqlite3.connect(gnucash_sqlite_path.path)
        [(description,)] = conn.execute(
            "SELECT description FROM transactions WHERE guid=?", (rent.handle,)
        ).fetchall()
        conn.close()
        assert description == "Rent from the browser"


class TestGuideRoute:
    """The browser reads every part of the packaged guide (#181)."""

    def test_each_part_and_rejections(self, client):
        from breadsched.user_guide import read_guide

        status, overview = client.get("/api/guide")
        assert status == 200 and overview["part"] == "overview"
        assert overview["markdown"] == read_guide("overview")
        assert [item["part"] for item in overview["parts"]] == ["overview", "desktop", "web", "cli"]
        assert overview["files"]["desktop.md"] == "desktop"
        status, web = client.get("/api/guide?part=web")
        assert status == 200 and web["markdown"].startswith("# Browser guide")
        for query in ("part=missing", "other=1"):
            with pytest.raises(urllib.error.HTTPError) as caught:
                client.get(f"/api/guide?{query}")
            assert caught.value.code == 400
