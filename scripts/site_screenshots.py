"""Screenshots of the browser interface for the website, from the synthetic sample book.

Run from the repository root with Playwright and Chromium available::

    PYTHONPATH=src python scripts/site_screenshots.py

It creates a fresh synthetic sample book (no real financial data), serves it on
localhost, and writes one PNG per view to ``website/images/``. Rerun it when an
interface changes enough that the pictures would mislead.
"""

from __future__ import annotations

import sys
import tempfile
import threading
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "website" / "images"

#: (file stem, navigation button, optional step run on the page before the picture)
SHOTS = (
    ("dashboard", "Dashboard", None),
    ("register", "Register", None),
    ("scheduled", "Scheduled", None),
    ("plan", "Plan", None),
    ("reports", "Reports", "spending"),
    ("projection", "Projection", None),
)


def main() -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from playwright.sync_api import sync_playwright

    from breadsched.gen.db.sqlite import DbSQLite
    from breadsched.gen.sample_book import create_sample_book
    from breadsched.web.server import serve

    today = date.today()
    with tempfile.TemporaryDirectory() as folder:
        book = Path(folder) / "sample.breadsched"
        create_sample_book(book, as_of=today)
        db = DbSQLite()
        db.load(str(book))
        httpd = serve(db, host="127.0.0.1", port=0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(
                    viewport={"width": 1200, "height": 760}, device_scale_factor=1
                )
                page.goto(f"http://127.0.0.1:{httpd.server_port}/#token={httpd.token}")
                page.wait_for_selector("#nav button")
                OUT.mkdir(parents=True, exist_ok=True)
                for stem, view, step in SHOTS:
                    page.get_by_role("button", name=view, exact=True).first.click()
                    page.wait_for_function(
                        "(name) => document.body.dataset.view === name"
                        " && document.getElementById('view').textContent.trim() !== 'Loading…'",
                        arg=view,
                    )
                    if step == "spending":
                        previous = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
                        page.get_by_label("Report").select_option("spending-by-category")
                        page.locator(".breadlet-options input[name=start]").fill(
                            previous.isoformat()
                        )
                        page.get_by_role("button", name="Run", exact=True).click()
                        page.wait_for_selector(".breadlet-document svg")
                    page.wait_for_timeout(400)
                    page.screenshot(path=str(OUT / f"{stem}.png"))
                    print(f"wrote {OUT / f'{stem}.png'}")
                browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
