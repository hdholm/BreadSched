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


def test_register_payee_picker_sets_the_payee(page, served):
    from breadsched.gen.services.payees import SavePayee, save_payee

    db, _httpd = served
    payee = save_payee(db, SavePayee("Known payee")).value
    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Register", exact=True).first.click()
    picker = page.locator("select[aria-label^='Payee for']").first
    picker.wait_for()
    label = picker.get_attribute("aria-label")
    description = label.removeprefix("Payee for ")

    picker.select_option(label="Known payee")
    page.wait_for_selector("text=Payee saved.")

    [transaction] = [
        item
        for item in db.iter_transactions()
        if item.description == description and item.payee == payee.handle
    ]
    assert transaction.description == description


def test_rules_view_adds_a_rule_and_accepts_its_proposal(page, served, tmp_path):
    from breadsched.gen.services.csv_import import CsvImportRequest, CsvMapping, import_csv

    db, _httpd = served
    checking = next(item for item in db.iter_accounts() if item.name == "Checking")
    path = tmp_path / "statement.csv"
    path.write_text("Date,Description,Amount\n2026-09-02,City Power,-60.00\n", encoding="utf-8")
    mapping = CsvMapping(date="Date", description="Description", amount="Amount")
    imported = import_csv(db, CsvImportRequest(str(path), checking.handle, mapping))
    [row] = imported.value.preview.rows
    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Rules", exact=True).first.click()
    page.wait_for_selector("text=No rules yet.")

    page.fill("input[name=description]", "City Power")
    category = page.locator("select[name=category] option").first.inner_text()
    page.get_by_role("button", name="Add rule").click()
    page.wait_for_selector("td:has-text('Description city power')")
    page.get_by_role("button", name="Accept selected").click()
    page.wait_for_selector("text=No uncategorized imported transactions match a rule.")

    stored = db.get_transaction(row.identity)
    names = {db.full_name(split.account) for split in stored.splits}
    assert category in names
    assert stored.description == "City Power"


def _open_register(page):
    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Register", exact=True).first.click()
    page.wait_for_selector("tr.entry-row input[aria-label='New transaction description']")


def _blank(page, label):
    return page.locator(f"tr.entry-row [aria-label='New transaction {label}']").first


def test_the_blank_row_posts_on_enter_and_proposes_the_latest_match(page, served):
    """#158: type into the register's last row; Enter saves; nothing posts by itself."""
    db, _httpd = served
    _open_register(page)
    _blank(page, "description").fill("Browser Bakery")
    amounts = page.locator("tr.entry-row input.num")
    amounts.nth(1).fill("12.34")
    amounts.nth(1).press("Enter")
    page.wait_for_selector("text=Posted Browser Bakery.")
    posted = next(t for t in db.iter_transactions() if t.description == "Browser Bakery")
    assert sorted(str(split.value.to_decimal()) for split in posted.splits) == ["-12.34", "12.34"]
    before = len(list(db.iter_transactions()))

    page.wait_for_selector("tr.entry-row input[aria-label='New transaction description']")
    _blank(page, "description").fill("BROWSER BAKERY #2")
    page.locator("tr.entry-row input.num").nth(0).focus()
    page.wait_for_selector("text=Proposed from")

    assert page.locator("tr.entry-row input.num").nth(1).input_value() == "12.34"
    assert len(list(db.iter_transactions())) == before


def test_the_blank_row_enters_balanced_split_lines(page, served):
    db, _httpd = served
    _open_register(page)
    accounts = [a for a in db.iter_accounts() if not a.placeholder and not a.is_root]
    register = page.locator("select").first.input_value()
    others = [a.handle for a in accounts if a.handle != register and not a.hidden][:2]
    _blank(page, "description").fill("Browser split")
    page.get_by_role("button", name="Split", exact=True).click()
    page.wait_for_selector("tr.entry-line")
    page.locator("[aria-label='New split 1 account']").select_option(register)
    page.locator("tr.entry-line input.num").nth(1).fill("30.00")
    page.locator("[aria-label='New split 2 account']").select_option(others[0])
    page.locator("tr.entry-line input.num").nth(2).fill("20.00")
    page.locator("[aria-label='New split 3 account']").select_option(others[1])
    page.locator("tr.entry-line input.num").nth(4).fill("10.00")
    page.wait_for_selector("td:has-text('Balanced')")
    page.locator("tr.entry-line input.num").nth(4).press("Enter")
    page.wait_for_selector("text=Posted Browser split.")

    posted = next(t for t in db.iter_transactions() if t.description == "Browser split")
    assert sorted(str(split.value.to_decimal()) for split in posted.splits) == [
        "-30.00",
        "10.00",
        "20.00",
    ]


def test_a_register_row_is_edited_in_place(page, served):
    db, _httpd = served
    _open_register(page)
    _blank(page, "description").fill("Edit me")
    page.locator("tr.entry-row input.num").nth(1).fill("7.00")
    page.locator("tr.entry-row input.num").nth(1).press("Enter")
    page.wait_for_selector("text=Posted Edit me.")
    original = next(t for t in db.iter_transactions() if t.description == "Edit me")

    page.locator("tr:has-text('Edit me')").get_by_role("button", name="Edit").click()
    field = page.locator("[aria-label='Edited transaction description']")
    field.wait_for()
    field.fill("Edited in place")
    field.press("Enter")
    page.wait_for_selector("text=Saved changes to Edited in place.")

    stored = db.get_transaction(original.handle)
    assert stored.description == "Edited in place"
    assert {s.handle for s in stored.splits} == {s.handle for s in original.splits}

    page.locator("tr:has-text('Edited in place')").get_by_role("button", name="Edit").click()
    field = page.locator("[aria-label='Edited transaction description']")
    field.wait_for()
    field.fill("Never saved")
    field.press("Escape")
    page.wait_for_selector("tr:has-text('Edited in place')")
    assert db.get_transaction(original.handle).description == "Edited in place"


def test_a_register_expense_becomes_a_reimbursable(page, served):
    from breadsched.gen.lib import AccountClass, Money, Transaction

    db, _httpd = served
    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Register", exact=True).first.click()
    page.wait_for_selector("tr.entry-row")
    account = page.locator("select").first.input_value()
    expense = next(
        a
        for a in db.iter_accounts()
        if a.account_class is AccountClass.EXPENSE and not a.placeholder and not a.hidden
    )
    visit = Transaction.simple(
        date.today(), "Browser dentist", expense.handle, account, Money("80")
    )
    with db.transaction("Browser fixture") as txn:
        db.add_transaction(visit, txn)
    page.get_by_role("button", name="Register", exact=True).first.click()
    page.locator("tr:has-text('Browser dentist')").get_by_role(
        "button", name="Reimbursable…"
    ).click()
    page.wait_for_selector("text=Saving links the")
    page.fill("input[aria-label='Payer']", "Dental plan")
    page.get_by_role("button", name="Add receivable").click()
    page.wait_for_selector("text=The expense is linked.")

    [receivable] = list(db.iter_receivables())
    cost = next(s for s in visit.splits if s.account == expense.handle)
    assert (receivable.payer, receivable.expenses[0].split) == ("Dental plan", cost.handle)
    page.wait_for_selector("td:has-text('Dental plan')")


def test_a_proposed_reimbursement_is_accepted_on_the_page(page, served):
    from breadsched.gen.lib import AccountClass, Money, Transaction
    from breadsched.gen.services.receivables import (
        SaveReceivable,
        attach_expense_split,
        save_receivable,
    )

    db, _httpd = served
    expense = next(
        a
        for a in db.iter_accounts()
        if a.account_class is AccountClass.EXPENSE and not a.placeholder and not a.hidden
    )
    bank = next(a for a in db.iter_accounts() if a.name == "Checking")
    visit = Transaction.simple(date(2026, 1, 5), "Clinic", expense.handle, bank.handle, Money("90"))
    refund = Transaction.simple(
        date(2026, 1, 20), "Acme Insurance", bank.handle, expense.handle, Money("40")
    )
    with db.transaction("Browser fixture") as txn:
        db.add_transaction(visit, txn)
        db.add_transaction(refund, txn)
    receivable = save_receivable(
        db, SaveReceivable(incurred_date=date(2026, 1, 5), payer="Acme Insurance")
    ).value
    cost = next(s for s in visit.splits if s.account == expense.handle)
    attach_expense_split(db, receivable.handle, visit.handle, cost.handle)

    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Reimbursables", exact=True).first.click()
    page.wait_for_selector("text=Proposed reimbursements")
    page.get_by_role("button", name="Accept selected").click()
    page.wait_for_selector("text=Linked 1 reimbursement(s)")
    assert len(db.get_receivable(receivable.handle).reimbursements) == 1
