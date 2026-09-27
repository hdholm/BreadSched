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
def page(tmp_path):
    book = tmp_path / "browser.breadsched"
    create_sample_book(book, as_of=date.today())
    db = DbSQLite()
    db.load(str(book))
    httpd = serve(db, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with sync_api.sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(executable_path=_chromium())
            except Exception as exc:  # noqa: BLE001 - no usable browser here
                pytest.skip(f"Chromium is not available: {exc}")
            view = browser.new_page()
            errors: list[str] = []
            view.on("pageerror", lambda error: errors.append(str(error)))
            view.goto(f"http://127.0.0.1:{httpd.server_port}/#token={httpd.token}")
            yield view
            browser.close()
            assert errors == []
    finally:
        httpd.shutdown()
        httpd.server_close()
        db.close()


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
