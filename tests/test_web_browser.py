"""Rendered web views, checked in a real browser where one is available.

The JSON API tests cannot see how ``app.js`` turns data into DOM. These tests run
headless Chromium through Playwright and skip when Playwright or a Chromium build is
not installed (set ``BREADSCHED_CHROMIUM`` to an executable to choose one).
"""

from __future__ import annotations

import os
import threading
from datetime import date
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from breadsched.gen.db.sqlite import DbSQLite  # noqa: E402
from breadsched.gen.sample_book import create_sample_book  # noqa: E402
from breadsched.web.server import serve  # noqa: E402


def _chromium() -> str | None:
    configured = os.environ.get("BREADSCHED_CHROMIUM")
    if configured:
        return configured
    for candidate in sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome")):
        return str(candidate)
    return None


@pytest.fixture
def served(tmp_path):
    book = tmp_path / "browser.breadsched"
    create_sample_book(book, as_of=date.today())
    db = DbSQLite()
    db.load(str(book))
    httpd = serve(db, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield db, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        db.close()


@pytest.fixture
def page(served):
    _db, httpd = served
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(executable_path=_chromium())
        except Exception as exc:  # noqa: BLE001 - no usable browser here
            pytest.skip(f"Chromium is not available: {exc}")
        view = browser.new_page(viewport={"width": 1280, "height": 1600})
        errors: list[str] = []
        view.on("pageerror", lambda error: errors.append(str(error)))
        view.goto(f"http://127.0.0.1:{httpd.server_port}/#token={httpd.token}")
        yield view
        browser.close()
        assert errors == []


def test_dashboard_tables_have_rows_and_group_totals_are_amounts(page):
    """Issue #132: bill/income tables were raw text and group totals read NaN."""
    page.wait_for_selector("text=Pending bills")

    bodies = page.evaluate(
        "() => [...document.querySelectorAll('tbody')].map((body) => ({"
        " rows: body.querySelectorAll(':scope > tr').length,"
        " loose: [...body.childNodes].filter((node) => node.nodeName !== 'TR').length }))"
    )
    assert bodies and all(item["loose"] == 0 for item in bodies)
    assert sum(item["rows"] for item in bodies) >= 3  # two bills and one income
    first_bill = page.locator("tbody tr").first.locator("td")
    assert first_bill.count() == 9
    assert "Sample" in first_bill.nth(0).inner_text()

    totals = page.locator(".balance-group dd").all_inner_texts()
    assert totals and not any("NaN" in value for value in totals)
    assert page.locator("text=NaN").count() == 0


def test_csv_statement_maps_previews_and_imports(page, served, tmp_path):
    db, _httpd = served
    statement = tmp_path / "statement.csv"
    statement.write_text(
        "Date,Description,Amount,Memo\n2026-09-01,Corner Grocer,-42.10,card\n"
        "2026-09-02,Refund,5.00,\n",
        encoding="utf-8",
    )
    before = len(list(db.iter_transactions()))
    page.get_by_role("button", name="Import", exact=True).first.click()
    page.wait_for_selector("text=CSV statement")
    panel = page.locator("div.panel:has(h2:text('CSV statement'))")

    panel.locator("input[type=file]").set_input_files(str(statement))
    panel.get_by_role("button", name="Read columns").click()
    page.wait_for_selector("text=Check the suggested columns")
    assert panel.locator("tbody tr").count() == 2
    panel.get_by_role("button", name="Preview").click()
    page.wait_for_selector("text=New: 2")
    assert panel.locator("tbody tr").first.locator("td").nth(4).inner_text() == "New"
    assert len(list(db.iter_transactions())) == before

    panel.locator("button.primary").click()
    page.wait_for_selector("text=Already imported: 2")
    assert len(list(db.iter_transactions())) == before + 2


def test_payees_view_adds_a_payee_and_accepts_proposals(page, served):
    db, _httpd = served
    transaction = next(iter(db.iter_transactions()))
    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Payees", exact=True).first.click()
    page.wait_for_selector("text=No payees yet.")

    page.fill("input[name=name]", "Known payee")
    page.fill("textarea[name=matches]", transaction.description)
    page.get_by_role("button", name="Add payee").click()
    # The accept button is always shown; wait for the refreshed payee table instead.
    page.wait_for_selector("td:has-text('Known payee')")
    assert db.get_transaction(transaction.handle).payee is None

    page.get_by_role("button", name="Accept selected").click()
    page.wait_for_selector("text=No transactions without a payee match a payee.")
    stored = db.get_transaction(transaction.handle)
    assert stored.payee is not None
    assert stored.description == transaction.description
