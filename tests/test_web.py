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

from breadsched.cli.main import main as cli
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.lib import (
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    Split,
    Transaction,
)
from breadsched.web.server import serve


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

    def test_schedule_occurrence_controls_are_structured(self, client):
        _status, body, _headers = client.raw("/")
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

    def test_accounts_carry_balances(self, client):
        _status, payload = client.get("/api/accounts")
        by_name = {row["name"]: row for row in payload}
        assert "Checking" in by_name
        assert Money(by_name["Checking"]["balance"]) == Money("2400.00")

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
            "planning_flows",
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
        assert len(payload["rows"]) == 2

    def test_scheduled_transactions_are_listed(self, client):
        status, payload = client.get("/api/scheduled")
        assert status == 200
        assert set(payload) == {"definitions", "accounts", "upcoming"}

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
        assert item["skipped"] == ["2026-03-13"]
        assert item["occurrence_adjustments"] == [{"when": "2026-04-15", "amount": "150.00"}]
        _status, plan = client.get("/api/plan?from=2026-01&through=2026-12&period=month")
        benefit = next(row for row in plan["planning_flows"] if row["kind"] == "benefit_funding")
        assert Money(benefit["planned"][1]) == Money("-110.00")

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
            },
        ]

        _status, plan = client.get("/api/plan?from=2026-02&through=2026-03&period=month")
        retirement_flow = next(
            row for row in plan["planning_flows"] if row["kind"] == "retirement_saving"
        )
        assert Money(retirement_flow["planned"][0]) == Money("500.00")

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

    def test_a_projection_is_computed(self, client):
        _status, payload = client.get("/api/projection?years=3")
        assert len(payload["rows"]) == 36
        assert payload["scenario"]["name"] == "Base scenario"
        assert payload["scenario"]["years"] == 3

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
        assert payload["comparison"]["scenario"] == {
            "handle": None,
            "name": "Base scenario",
        }
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

    def test_the_endpoint_answers_with_derived_categories(self, client):
        status, payload = client.get("/api/plan")
        assert status == 200
        assert set(payload) == {
            "controls",
            "periods",
            "summary",
            "comparison",
            "categories",
            "planning_flows",
            "column_totals",
        }
        by_name = {row["full_name"]: row for row in payload["categories"]}
        assert "Income:Salary" in by_name
        assert "Expenses:Rent" in by_name
        assert Money(by_name["Income:Salary"]["actual"][0]) == Money("4200.00")
        assert Money(by_name["Expenses:Rent"]["actual"][0]) == Money("1800.00")

    def test_grouping_changes_display_buckets(self, client):
        _status, payload = client.get("/api/plan?from=2026-01&through=2026-12&period=quarter")
        assert payload["controls"]["period"] == "quarter"
        assert [row["label"] for row in payload["periods"]] == [
            "Q1 2026",
            "Q2 2026",
            "Q3 2026",
            "Q4 2026",
        ]

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
        _status, body, _headers = client.raw("/")
        page = body.decode()
        assert '"Scheduled", "Plan", "Review", "Projection"' in page
        assert "async function showPlan" in page
        assert "async function showBudget" not in page

    def test_plan_values_are_keyboard_accessible_buttons(self, client):
        _status, body, _headers = client.raw("/")
        page = body.decode()
        assert 'class: "plan-cell-button"' in page
        assert 'type: "button"' in page
        assert '"aria-label": `Explain ${category.full_name}' in page
        assert ".plan-cell-button:focus-visible" in page


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
            },
        )
        assert status == 200
        assert payload.get("ok") or payload.get("handle")

        _status, accounts = client.get("/api/accounts")
        balance = next(a["balance"] for a in accounts if a["name"] == "Checking")
        assert Money(balance) == Money("2275.00")

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

    def test_an_unbalanced_request_is_refused_cleanly(self, client):
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.post("/api/transaction", {"description": "nonsense"})
        assert caught.value.code in (400, 404, 500)


class TestImportApi:
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

    def test_foreign_host_is_rejected(self, client):
        request = urllib.request.Request(
            client.base_url + "/",
            headers={"Host": "example.invalid"},
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        assert caught.value.code == 403


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

    def test_the_endpoint_answers(self, client):
        status, payload = client.get("/api/dashboard")
        assert status == 200
        assert set(payload) == {"summary", "config", "groups", "pending"}

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
        ):
            assert key in payload["summary"], f"missing {key}"

    def test_pending_cash_flow_exposes_income_without_a_hold(self, client):
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
        income = [item for item in payload["pending"] if item["income"]]
        assert income
        assert all(item["hold"] is None for item in income)

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
        assert float(twelve["summary"]["emergency_fund"]) >= float(six["summary"]["emergency_fund"])

    def test_amounts_are_strings_the_browser_can_parse(self, client):
        _status, payload = client.get("/api/dashboard")
        assert isinstance(payload["summary"]["net_worth"], str)
        float(payload["summary"]["net_worth"])

    def test_the_page_opens_on_the_dashboard(self, client):
        _status, body, _headers = client.raw("/")
        page = body.decode()
        assert 'let current = "Dashboard"' in page
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

    def test_marking_unexpected_removes_the_actual_from_the_queue(self, review_client):
        status, payload = review_client.post(
            "/api/review/unexpected", {"transaction": review_client.actual_handle}
        )
        assert status == 200
        assert payload["resolution"] == "unexpected"
        _status, review = review_client.get("/api/review")
        assert review["actuals"] == []

    def test_page_exposes_review_between_plan_and_projection(self, client):
        _status, body, _headers = client.raw("/")
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

    def test_base_can_be_duplicated_into_an_independent_saved_scenario(self, client):
        client.post(
            "/api/scenario/save",
            {"handle": None, "assumptions": self.assumptions(income_growth="0.041")},
        )
        _status, clone = client.post("/api/scenario/duplicate", {"handle": None})
        assert clone["base"] is False
        assert clone["name"] == "Base scenario copy"
        assert clone["assumptions"]["income_growth"] == "0.041"

        clone["name"] = "Retire 2035"
        clone["description"] = "Reduced work scenario"
        clone["assumptions"]["income_growth"] = "0.01"
        _status, saved = client.post("/api/scenario/save", clone)
        assert saved["name"] == "Retire 2035"

        _status, listing = client.get("/api/scenarios")
        base = listing["scenarios"][0]
        retire = next(item for item in listing["scenarios"] if item["name"] == "Retire 2035")
        assert base["assumptions"]["income_growth"] == "0.041"
        assert retire["assumptions"]["income_growth"] == "0.01"

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
        _status, body, _headers = client.raw("/")
        page = body.decode()
        assert '"Manage scenarios…"' in page
        assert "async function showScenarios" in page
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
        _status, body, _headers = client.raw("/")
        page = body.decode()
        assert ".plan-table th:first-child, .plan-table td:first-child" in page
        assert "max-height: calc(100vh - 310px)" in page
        assert 'class:"balance-groups"' in page
        assert '"Add estimate…"' in page
        assert '"Alter baseline…"' in page
        assert '"Suppress baseline…"' in page


def test_historical_estimate_proposals_and_acceptance(client):
    status, data = client.get("/api/historical-estimates?months=12&min_active_months=1")
    assert status == 200
    rent = next(item for item in data["proposals"] if item["category_name"].endswith("Rent"))
    assert rent["funding_name"].endswith("Checking")
    assert Money(rent["amount"]) == Money("1800.00")
    assert data["targets"][0]["name"] == "Base"

    status, result = client.post(
        "/api/historical-estimate/accept",
        {
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
