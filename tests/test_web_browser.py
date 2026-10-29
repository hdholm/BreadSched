"""Rendered web views, checked in a real browser where one is available.

The JSON API tests cannot see how the page's scripts turn data into DOM. These tests run
headless Chromium through Playwright and skip when Playwright or a Chromium build is
not installed (set ``BREADSCHED_CHROMIUM`` to an executable to choose one).
"""

from __future__ import annotations

import os
import re
import threading
from datetime import date
from decimal import Decimal
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


def test_every_view_loads_from_its_area_script(page):
    """Each navigation button renders its view; none reports a load failure."""
    page.wait_for_selector("#nav button")
    names = page.locator("#nav button").all_inner_texts()
    assert len(names) >= 15
    for name in names:
        page.evaluate(
            "(name) => [...document.querySelectorAll('#nav button')]"
            ".find((button) => button.textContent === name).click()",
            name,
        )
        page.wait_for_function(
            "(name) => document.body.dataset.view === name"
            " && document.getElementById('view').textContent.trim() !== 'Loading…'",
            arg=name,
        )
        assert page.locator("#view").inner_text().strip(), name
        assert "Could not load" not in page.locator("#view").inner_text(), name


def test_verify_reports_the_book_and_runs_again(page):
    """The Verify page used to fail with "main is not defined" before showing a result."""
    page.wait_for_selector("#nav button")
    page.evaluate(
        "() => [...document.querySelectorAll('#nav button')]"
        ".find((button) => button.textContent === 'Verify').click()"
    )
    page.wait_for_selector("text=No problems found")
    page.get_by_role("button", name="Verify again").click()
    page.wait_for_selector("text=No problems found")
    assert "Could not load" not in page.locator("#view").inner_text()


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
    assert panel.locator("tbody tr").first.locator("td").nth(5).inner_text() == "New"
    assert len(list(db.iter_transactions())) == before

    panel.locator("button.primary").click()
    page.wait_for_selector("text=Already imported: 2")
    assert len(list(db.iter_transactions())) == before + 2


def test_csv_split_columns_preview_and_import(page, served, tmp_path):
    db, _httpd = served
    statement = tmp_path / "split.csv"
    statement.write_text(
        "Date,Description,Amount,Cat 1,Amt 1,Cat 2,Amt 2\n"
        "2026-09-01,Warehouse,-30.00,Expenses:Groceries,-20.00,Expenses:Utilities,-10.00\n",
        encoding="utf-8",
    )
    page.get_by_role("button", name="Import", exact=True).first.click()
    page.wait_for_selector("text=CSV statement")
    panel = page.locator("div.panel:has(h2:text('CSV statement'))")
    panel.locator("input[type=file]").set_input_files(str(statement))
    panel.get_by_role("button", name="Read columns").click()
    page.wait_for_selector("text=Check the suggested columns")
    for number in (1, 2):
        panel.get_by_role("button", name="Add split columns").click()
        panel.get_by_label(f"Split {number} category column").select_option(f"Cat {number}")
        panel.get_by_label(f"Split {number} amount column").select_option(f"Amt {number}")

    panel.get_by_role("button", name="Preview").click()
    page.wait_for_selector("text=New: 1")
    category = panel.locator("tbody tr").first.locator("td").nth(4).inner_text()
    assert category == "Expenses:Groceries (20.00); Expenses:Utilities (10.00)"

    panel.locator("button.primary").click()
    page.wait_for_selector("text=Already imported: 1")
    [posted] = [item for item in db.iter_transactions() if item.description == "Warehouse"]
    assert len(posted.splits) == 3


def test_help_opens_the_workflow_section_of_the_guide_in_a_new_tab(page):
    page.get_by_role("button", name="Import", exact=True).first.click()
    page.wait_for_selector("text=CSV statement")
    panel = page.locator("div.panel:has(h2:text('CSV statement'))")
    with page.context.expect_page() as opened:
        panel.locator("button[data-help=csv-import]").click()
    guide = opened.value
    guide.wait_for_selector("#guide-import-a-csv-statement")
    assert guide.evaluate("() => document.body.dataset.view") == "Guide"
    assert "Browser" in guide.locator("[aria-selected=true]").inner_text()
    # The page with the form stays where it was.
    assert page.evaluate("() => document.body.dataset.view") == "Import"


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


def test_payroll_view_fills_a_template_creates_a_paycheck_and_previews_a_raise(page, served):
    from breadsched.gen.engine.payroll import paycheck_breakdown

    db, _httpd = served
    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Payroll", exact=True).first.click()
    page.wait_for_selector("text=Sample wages")
    page.get_by_role("button", name="Fill from paycheck").click()
    page.wait_for_selector("text=Filled the template from the paycheck")
    page.fill("input[aria-label='Template name']", "Job")
    page.get_by_role("button", name="Save template").click()
    page.wait_for_selector("td:has-text('Job')")

    page.fill("input[aria-label='Paycheck schedule name']", "Job pay")
    page.fill("input[aria-label='Gross (optional)']", "5000")
    page.get_by_role("button", name="Create paycheck").click()
    page.wait_for_selector("text=Added paycheck Job pay")
    page.wait_for_selector("h2:has-text('Pay change: Job pay')")
    page.fill("input[aria-label='New gross pay']", "5500")
    page.get_by_role("button", name="Preview").click()
    page.wait_for_selector("text=take-home 5,500.00")
    [schedule] = [item for item in db.iter_scheduled() if item.name == "Job pay"]
    assert all(not split.amount_changes for split in schedule.splits)

    page.get_by_role("button", name="Save pay change").click()
    page.wait_for_selector("text=Saved the pay change")
    stored = db.get_scheduled(schedule.handle)
    later = paycheck_breakdown(db, stored, max(date.today(), stored.recurrence.start))
    assert later.gross.format() == "5,500.00"


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


def test_the_blank_row_reads_shortcuts_arithmetic_and_account_paths(page, served):
    """Register typing goes through the same shared rules as the desktop register."""
    from breadsched.gen.services.entry_input import complete_entry_account

    db, _httpd = served
    _open_register(page)
    register = page.locator("select").first.input_value()
    target = next(
        a
        for a in sorted(db.iter_accounts(), key=db.full_name)
        if not (a.placeholder or a.hidden or a.is_root) and a.handle != register
    )
    typed = ":".join(segment[:2] for segment in db.full_name(target).split(":"))
    found = complete_entry_account(db, typed).value
    expected = next(item for item in found if item.handle != register)

    date_field = _blank(page, "date")
    date_field.fill("2026-01-31")
    date_field.press("+")
    page.wait_for_function(
        "() => document.querySelector(\"[aria-label='New transaction date']\").value"
        " === '2026-02-01'"
    )
    date_field.press("]")
    page.wait_for_function(
        "() => document.querySelector(\"[aria-label='New transaction date']\").value"
        " === '2026-03-01'"
    )
    _blank(page, "description").fill("Browser arithmetic")
    transfer = _blank(page, "transfer account")
    transfer.focus()
    for key in typed:
        transfer.press(key)
    page.wait_for_selector(f"text={expected.full_name}")
    assert transfer.input_value() == expected.handle
    amount = page.locator("tr.entry-row input.num").nth(1)
    amount.fill("100/3")
    amount.press("Enter")
    page.wait_for_selector("text=Posted Browser arithmetic.")
    posted = next(t for t in db.iter_transactions() if t.description == "Browser arithmetic")
    assert posted.post_date == date(2026, 3, 1)
    assert sorted(str(split.value.to_decimal()) for split in posted.splits) == [
        "-33.33",
        "33.33",
    ]
    assert {split.account for split in posted.splits} == {register, expected.handle}


def test_register_rows_edit_on_click_move_with_arrows_and_toggle_cleared(page, served):
    """The browser register edits like the desktop one: click, Up/Down, R."""
    from breadsched.gen.lib import ReconcileState

    db, _httpd = served
    _open_register(page)
    register = page.locator("select").first.input_value()
    rows = [r for r in page.locator("tbody tr.editable").all()]
    assert len(rows) >= 2
    last_desc = rows[-1].locator("td").nth(2).locator("div").first.inner_text()

    # From the blank row, Up edits the last transaction.
    _blank(page, "description").press("ArrowUp")
    field = page.locator("[aria-label='Edited transaction description']")
    field.wait_for()
    assert field.input_value() == last_desc
    field.fill(f"{last_desc} (moved)")
    field.press("ArrowUp")
    page.wait_for_selector(f"text=Saved changes to {last_desc} (moved).")
    page.locator("[aria-label='Edited transaction description']").wait_for()
    assert any(t.description == f"{last_desc} (moved)" for t in db.iter_transactions())

    # Clicking another row's date edits that row instead.
    page.locator("tbody tr.editable").first.locator("td").first.click()
    page.wait_for_function(
        "() => document.querySelectorAll(\"[aria-label='Edited transaction description']\")"
        ".length === 1"
    )
    page.locator("[aria-label='Edited transaction description']").press("Escape")

    # The R column toggles cleared through the shared service.
    button = page.locator("tbody tr.editable button.reconcile").first
    label = button.get_attribute("aria-label")
    description = label.split(" for ", 1)[1]
    button.click()
    page.wait_for_selector(f"text=Marked {description} cleared.")
    transaction = next(t for t in db.iter_transactions() if t.description == description)
    assert transaction.split_for(register).reconcile is ReconcileState.CLEARED


def test_typed_account_paths_list_their_matches(page, served):
    db, _httpd = served
    _open_register(page)
    transfer = _blank(page, "transfer account")
    transfer.focus()
    transfer.press("E")
    listing = page.locator("[aria-label='New transaction transfer account: matching accounts']")
    listing.locator("button").first.wait_for()
    names = listing.locator("button").all_inner_texts()
    # Levels that begin with "E" first, then names holding an "e" anywhere (#325).
    starting = [name for name in names if name.startswith("E")]
    assert starting and names[: len(starting)] == starting
    assert all("e" in name.casefold() for name in names)
    chosen = names[-1]
    listing.locator("button").last.click()
    expected = next(a.handle for a in db.iter_accounts() if db.full_name(a) == chosen)
    assert transfer.input_value() == expected
    assert listing.is_hidden()


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

    page.locator("tr:has-text('Edit me')").get_by_role("button", name="Edit", exact=True).click()
    field = page.locator("[aria-label='Edited transaction description']")
    field.wait_for()
    field.fill("Edited in place")
    field.press("Enter")
    page.wait_for_selector("text=Saved changes to Edited in place.")

    stored = db.get_transaction(original.handle)
    assert stored.description == "Edited in place"
    assert {s.handle for s in stored.splits} == {s.handle for s in original.splits}

    page.locator("tr:has-text('Edited in place')").get_by_role(
        "button", name="Edit", exact=True
    ).click()
    field = page.locator("[aria-label='Edited transaction description']")
    field.wait_for()
    field.fill("Never saved")
    field.press("Escape")
    page.wait_for_selector("tr:has-text('Edited in place')")
    assert db.get_transaction(original.handle).description == "Edited in place"


def test_an_edited_split_moves_a_transaction_to_another_account(page, served):
    """#364: an edit may move every split out of the register's account."""
    db, _httpd = served
    _open_register(page)
    register = page.locator("select").first.input_value()
    _blank(page, "description").fill("Wrong card")
    page.locator("tr.entry-row input.num").nth(1).fill("9.00")
    page.locator("tr.entry-row input.num").nth(1).press("Enter")
    page.wait_for_selector("text=Posted Wrong card.")
    original = next(t for t in db.iter_transactions() if t.description == "Wrong card")
    others = {split.account for split in original.splits} - {register}
    target = next(
        a.handle
        for a in db.iter_accounts()
        if not a.placeholder
        and not a.is_root
        and not a.hidden
        and a.handle != register
        and a.handle not in others
    )

    page.locator("tr:has-text('Wrong card')").get_by_role("button", name="Edit", exact=True).click()
    page.locator("[aria-label='Edited transaction description']").wait_for()
    page.locator("tr:has([aria-label='Edited transaction description'])").get_by_role(
        "button", name="Split", exact=True
    ).click()
    page.wait_for_selector("[aria-label='Edited split 1 account']")
    page.locator("[aria-label='Edited split 1 account']").select_option(target)
    page.locator("[aria-label='Edited transaction description']").press("Enter")
    page.wait_for_selector("text=Saved Wrong card; it moved out of this register.")

    stored = db.get_transaction(original.handle)
    assert register not in {split.account for split in stored.splits}
    assert target in {split.account for split in stored.splits}
    assert page.locator("tr:has-text('Wrong card')").count() == 0


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


def test_an_open_receivable_shows_its_fsa_allocation(page, served):
    # Issue #192: an FSA claim linked to cover the rest of the bill.
    from breadsched.gen.lib import AccountClass, Money, Transaction
    from breadsched.gen.services.claims import (
        ClaimInput,
        ClaimLinkInput,
        SaveClaim,
        save_claim,
    )
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
    with db.transaction("Browser fixture") as txn:
        db.add_transaction(visit, txn)
    receivable = save_receivable(
        db,
        SaveReceivable(
            incurred_date=date(2026, 1, 5), payer="Acme Insurance", expected_amount=Money("60")
        ),
    ).value
    cost = next(s for s in visit.splits if s.account == expense.handle)
    attach_expense_split(db, receivable.handle, visit.handle, cost.handle)
    assert save_claim(
        db,
        SaveClaim(
            ClaimInput(
                date(2026, 1, 5),
                "Clinic",
                payments=(ClaimLinkInput(visit.handle, cost.handle),),
                receivable=receivable.handle,
            )
        ),
    ).ok

    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Reimbursable Expenses", exact=True).first.click()
    page.locator("tr:has-text('Acme Insurance')").get_by_role("button", name="Open").click()
    page.wait_for_selector("text=Acme Insurance pays 60.00")
    assert page.locator("text=until the EOB is entered").count() == 1
    assert page.locator("text=is also on an FSA claim").count() == 0


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
    page.get_by_role("button", name="Reimbursable Expenses", exact=True).first.click()
    page.wait_for_selector("text=Proposed reimbursements")
    page.get_by_role("button", name="Accept selected").click()
    page.wait_for_selector("text=Linked 1 reimbursement(s)")
    assert len(db.get_receivable(receivable.handle).reimbursements) == 1


def test_dashboard_shows_net_worth_history_and_regroups_it(page):
    page.wait_for_selector("text=Pending bills")
    page.wait_for_selector("h2:has-text('Net worth history')")
    rows = page.locator(".net-worth-points tbody tr")
    assert rows.count() >= 1
    # Shared-model charts: assets, debts, and net worth lines, then the groups stacked.
    charts = page.locator(".net-worth-history figure.model-chart")
    assert [charts.nth(i).get_attribute("data-key") for i in range(charts.count())] == [
        "net_worth",
        "net_worth_composition",
    ]
    assert charts.nth(0).locator("svg path[stroke]").count() == 3
    assert charts.nth(1).locator("svg path[fill^='var(--series-']").count() >= 1
    assert "Total" in charts.nth(1).locator("details.chart-values").text_content()
    months = rows.count()
    page.locator(".net-worth-history select").select_option("year")
    page.wait_for_selector(".net-worth-points tbody tr >> nth=0")
    page.wait_for_function(
        "(count) => document.querySelectorAll('.net-worth-points tbody tr').length < count"
        " || document.querySelectorAll('.net-worth-points tbody tr').length <= 2",
        arg=months,
    )
    assert "20" in page.locator(".net-worth-points tbody tr").first.inner_text()


def test_dashboard_explains_a_net_worth_change_and_downloads_it(page):
    page.wait_for_selector("h2:has-text('Net worth history')")
    page.locator(".net-worth-explain").last.click()
    section = page.locator(".net-worth-change")
    section.wait_for()
    assert "Market and exchange-rate changes" in section.inner_text()
    change = section.locator("tr.total td").last.inner_text()
    with page.expect_download() as caught:
        section.get_by_role("button", name="Download CSV").click()
    exported = Path(caught.value.path()).read_text(encoding="utf-8").splitlines()
    assert exported[0].startswith("date,description,accounts")
    last = exported[-1].split(",")
    assert last[1] == "Change"
    # The page and the export show the same total.
    assert re.sub(r"[^0-9.]", "", change) == f"{abs(Decimal(last[4])):.2f}"
    section.get_by_role("button", name="Close").click()
    assert page.locator(".net-worth-change").count() == 0


def test_expense_explorer_shows_spending_over_time_and_selects_a_period(page):
    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Plan", exact=True).first.click()
    page.wait_for_selector("h3:has-text('Spending over time')")
    periods = page.locator(".spending-over-time .spending-periods tbody tr")
    assert periods.count() >= 2
    # Three shared-model charts: plan and actual lines, actual stacked by category,
    # and each category's share, the selected period shaded in each.
    charts = page.locator(".spending-over-time figure.model-chart")
    assert charts.count() == 3
    assert charts.nth(0).locator("svg path[stroke]").count() == 2
    assert charts.nth(1).locator("svg path[fill^='var(--series-']").count() >= 1
    assert "%" in charts.nth(2).locator("svg").text_content()
    assert page.locator(".spending-over-time rect.chart-selected").count() == 3
    label = periods.nth(1).locator("button").inner_text()
    periods.nth(1).locator("button").click()
    page.wait_for_selector(
        f".spending-over-time .spending-periods tbody tr.selected:has-text('{label}')"
    )
    selected = page.locator(".expense-explorer select").first.evaluate("(node) => node.value")
    assert selected == "1"
    # A period in a chart is a keyboard-operable button that selects it too.
    choice = page.locator(".spending-over-time figure.model-chart").nth(1)
    choice.locator("rect.chart-choice").nth(0).focus()
    page.keyboard.press("Enter")
    page.wait_for_selector(".spending-over-time .spending-periods tbody tr:first-child.selected")
    # Clicking a drawn column selects its period: the band over it takes the click.
    stacked = page.locator(".spending-over-time figure.model-chart").nth(1)
    column = stacked.locator("svg path[fill^='var(--series-']").last.bounding_box()
    x = column["x"] + column["width"] / 2
    bands = [
        box for box in (hit.bounding_box() for hit in stacked.locator("rect.chart-choice").all())
    ]
    period = next(i for i, box in enumerate(bands) if box["x"] <= x < box["x"] + box["width"])
    page.locator(".spending-over-time .spending-periods tbody tr").nth(
        1 if period == 0 else 0
    ).locator("button").click()
    page.wait_for_selector(
        ".spending-over-time .spending-periods tbody tr:nth-child("
        f"{2 if period == 0 else 1}).selected"
    )
    stacked = page.locator(".spending-over-time figure.model-chart").nth(1)
    column = stacked.locator("svg path[fill^='var(--series-']").last.bounding_box()
    page.mouse.click(x, column["y"] + column["height"] / 2)
    page.wait_for_selector(
        f".spending-over-time .spending-periods tbody tr:nth-child({period + 1}).selected"
    )
    page.locator(".spending-over-time .spending-periods tbody tr").nth(1).locator("button").click()
    page.wait_for_selector(
        f".spending-over-time .spending-periods tbody tr.selected:has-text('{label}')"
    )
    # The selected category's trend is drawn from the same model.
    assert page.locator("figure.model-chart[data-key='category_trend']").count() == 1
    # Income over time has its own chart and table over the same periods, and
    # selecting a period there selects it for the whole explorer.
    income = page.locator(".income-over-time .spending-periods tbody tr")
    assert income.count() == periods.count()
    assert (
        page.locator(".income-over-time figure.model-chart")
        .nth(0)
        .locator("svg path[stroke]")
        .count()
        == 2
    )
    # The income detail lists the selected period's dated receipts by payer.
    page.wait_for_selector(".income-detail h3")
    assert page.locator(".income-detail select option").count() >= 1
    assert "Payer" in page.locator(".income-detail").inner_text()
    income.nth(0).locator("button").click()
    page.wait_for_selector(".spending-over-time .spending-periods tbody tr.selected >> nth=0")
    selected = page.locator(".expense-explorer select").first.evaluate("(node) => node.value")
    assert selected == "0"


def test_the_import_page_writes_ticked_changes_back_to_gnucash(page, served, gnucash_sqlite_path):
    import sqlite3

    from breadsched.gen.services.imports import ImportBook, import_book

    db, _httpd = served
    # The served book may already hold a "Rent" of its own; take the imported one.
    before = {item.handle for item in db.iter_transactions()}
    assert import_book(db, ImportBook(source=gnucash_sqlite_path.path, notify=False)).ok
    rent = next(
        item
        for item in db.iter_transactions()
        if item.description == "Rent" and item.handle not in before
    )
    rent.description = "Rent via browser"
    with db.transaction("Edit") as txn:
        db.commit_transaction(rent, txn)

    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Import", exact=True).first.click()
    page.wait_for_selector("h2:has-text('Write changes to GnuCash')")
    panel = page.locator(".writeback")
    panel.get_by_label("Rent via browser", exact=False).check()
    panel.get_by_role("button", name="Write selected").click()
    page.wait_for_selector("text=Wrote 1 transaction(s) to GnuCash")
    conn = sqlite3.connect(gnucash_sqlite_path.path)
    [(description,)] = conn.execute(
        "SELECT description FROM transactions WHERE guid=?", (rent.handle,)
    ).fetchall()
    conn.close()
    assert description == "Rent via browser"


def test_the_guide_page_switches_parts_and_follows_links(page):
    page.wait_for_selector("text=Pending bills")
    page.locator("#nav").get_by_role("button", name="Guide", exact=True).click()
    page.wait_for_selector("article.guide h2:has-text('BreadSched User Guide')")
    page.get_by_role("tab", name="Command line").click()
    page.wait_for_selector("article.guide h2:has-text('Command-line guide')")
    assert page.locator("article.guide pre code").count() > 5
    # A link into the overview opens it at that heading.
    page.locator("article.guide a", has_text="Reimbursable expenses").first.click()
    page.wait_for_selector("article.guide h2:has-text('BreadSched User Guide')")
    heading = page.locator("#guide-reimbursable-expenses")
    assert heading.count() == 1
    # Scrolling to a heading can leave it a fraction of a pixel above the top.
    page.wait_for_function(
        "() => { const r = document.getElementById('guide-reimbursable-expenses')"
        ".getBoundingClientRect(); return r.top > -1 && r.top < window.innerHeight; }"
    )
    assert page.get_by_role("tab", name="Overview").get_attribute("aria-selected") == "true"


def test_dashboard_shows_what_savings_goals_set_aside(page, served):
    from breadsched.gen.lib import Money
    from breadsched.gen.services import (
        AllocateToGoal,
        SaveSavingsGoal,
        allocate_to_goal,
        goal_accounts,
        save_savings_goal,
    )

    db, httpd = served
    today = date.today()
    cash = next(
        handle for handle, _name in goal_accounts(db) if db.get_account(handle).atype.is_cash_like
    )
    goal = save_savings_goal(
        db,
        SaveSavingsGoal("Holiday", cash, Money(1200), today.replace(year=today.year + 1), today),
    ).value
    assert allocate_to_goal(db, AllocateToGoal(goal.handle, Money(250), today)).ok
    page.goto(f"http://127.0.0.1:{httpd.server_port}/?goals#token={httpd.token}")
    page.wait_for_selector("text=Set aside for goals")
    tile = page.locator(".card", has_text="Set aside for goals")
    assert "250" in tile.inner_text()


def test_goals_page_adds_funds_and_closes_a_goal(page, served):
    db, _httpd = served
    today = date.today()
    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Savings Goals", exact=True).first.click()
    page.wait_for_selector("text=No savings goals yet.")
    form = page.locator("form.goal-form")
    form.locator("input[name=name]").fill("Roof")
    form.locator("input[name=target_amount]").fill("900")
    form.locator("input[name=target_date]").fill(today.replace(year=today.year + 1).isoformat())
    form.get_by_role("button", name="Add goal").click()
    page.wait_for_selector("text=Saved Roof.")
    [goal] = list(db.iter_savings_goals())
    assert goal.name == "Roof" and goal.target_amount.to_decimal() == 900

    page.get_by_label("Amount to allocate to Roof").fill("150")
    page.get_by_role("button", name="Allocate", exact=True).click()
    page.wait_for_selector("text=Allocated 150 to Roof.")
    assert db.get_savings_goal(goal.handle).allocated(today).to_decimal() == 150

    page.get_by_role("button", name="Dashboard", exact=True).first.click()
    page.wait_for_selector("h2:has-text('Savings goals (1)')")
    assert "Roof" in page.locator(".dashboard-goals").inner_text()

    page.get_by_role("button", name="Savings Goals", exact=True).first.click()
    page.wait_for_selector("text=Set aside for goals")
    # The goal's set-aside is charted under what is still to save toward its target.
    chart = page.locator(".savings-goals figure.model-chart[data-key='goals']")
    assert chart.count() == 1
    assert "Roof (" in chart.locator("details.chart-values").text_content()
    page.get_by_role("button", name="Close", exact=True).click()
    page.wait_for_selector("text=Closed Roof.")
    assert db.get_savings_goal(goal.handle).closed_on == today


def test_goals_page_changes_a_goal_in_a_scenario_and_projection_shows_it(page, served):
    from breadsched.gen.lib import Money, Scenario
    from breadsched.gen.services import SaveSavingsGoal, goal_accounts, save_savings_goal

    db, httpd = served
    today = date.today()
    handle = next(h for h, _name in goal_accounts(db) if db.get_account(h).atype.is_cash_like)
    goal = save_savings_goal(
        db, SaveSavingsGoal("Car", handle, Money(3000), today.replace(year=today.year + 1), today)
    ).value
    scenario = Scenario(name="Lean", start=today.replace(day=1), years=2)
    with db.transaction("Scenario") as txn:
        db.add_scenario(scenario, txn)
    page.goto(f"http://127.0.0.1:{httpd.server_port}/?goals#token={httpd.token}")
    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Savings Goals", exact=True).first.click()
    page.wait_for_selector("text=Scenario changes")
    page.get_by_role("button", name="Edit", exact=True).first.click()
    overrides = page.locator("form.goal-override")
    overrides.locator("select[name=scenario]").select_option(label="Lean")
    overrides.locator("input[name=override_target]").fill("4000")
    overrides.get_by_role("button", name="Apply to scenario").click()
    page.wait_for_selector("text=Lean: target 4,000.00.")
    assert db.get_scenario(scenario.handle).goal_overrides[goal.handle].target_amount == Money(4000)
    assert "Lean: target 4,000.00" in page.locator("td.goal-changes").first.inner_text()

    page.get_by_role("button", name="Projection", exact=True).first.click()
    page.wait_for_selector(".projection-goals")
    assert "Goal Car" in page.locator(".projection-goals").inner_text()


def test_a_register_row_is_tagged_and_links_documents(page, served, tmp_path):
    db, _httpd = served
    _open_register(page)
    page.get_by_role("button", name="Tags & documents…").first.click()
    heading = page.locator(".detail-dialog h2").inner_text()
    description = heading.removeprefix("Tags & documents: ")
    page.fill("input[aria-label='Tags']", "Tax, home repair")
    page.get_by_role("button", name="Save tags").click()
    page.wait_for_selector("text=Tags saved.")
    receipt = tmp_path / "receipt.pdf"
    receipt.write_bytes(b"%PDF-1.4")
    page.set_input_files("input[aria-label='Document file']", str(receipt))
    page.get_by_role("button", name="Attach file").click()
    page.wait_for_selector(".detail-dialog span:has-text('receipt.pdf')")
    page.fill("input[aria-label='Document address']", "https://example.com/invoice")
    page.get_by_role("button", name="Link", exact=True).click()
    page.wait_for_selector(".detail-dialog span:has-text('https://example.com/invoice')")

    (tmp_path / "browser attachments" / "receipt.pdf").unlink()
    page.get_by_role("button", name="Done").click()
    page.wait_for_selector("text=Tags: Tax, home repair")
    page.wait_for_selector("text=Documents: 2 (1 missing)")
    [stored] = [item for item in db.iter_transactions() if item.tags]
    assert stored.description == description
    assert stored.attachments == ["receipt.pdf", "https://example.com/invoice"]


def test_a_scenario_projection_opens_in_its_own_browser_tab(page, served):
    """Like GTK's scenario tabs, each browser tab keeps the scenario it opened with."""
    from breadsched.gen.lib import Scenario

    db, httpd = served
    scenario = Scenario(name="Sabbatical", start=date.today().replace(day=1), years=2)
    with db.transaction("Scenario") as txn:
        db.add_scenario(scenario, txn)
    page.goto(f"http://127.0.0.1:{httpd.server_port}/?view=Projection#token={httpd.token}")
    page.wait_for_selector("select[name=scenario]")
    page.locator("select[name=scenario]").select_option(label="Sabbatical")
    chosen = "document.querySelector('select[name=scenario]')?.selectedOptions[0]?.textContent"
    picked = f"() => {chosen}"
    page.wait_for_function(f"() => {chosen} === 'Sabbatical'")
    with page.context.expect_page() as opened:
        page.get_by_role("button", name="Open in new tab").click()
    other = opened.value
    other.wait_for_selector("select[name=scenario]")
    assert "token=" not in other.url
    assert other.evaluate(picked) == "Sabbatical"
    # Moving the first tab back to Base leaves the scenario tab where it was.
    page.locator("select[name=scenario]").select_option(label="Base scenario")
    page.wait_for_function(f"() => {chosen} === 'Base scenario'")
    assert other.evaluate(picked) == "Sabbatical"


def test_projection_shows_how_each_balance_reconciles(page, served):
    _db, httpd = served
    page.goto(f"http://127.0.0.1:{httpd.server_port}/?view=Projection#token={httpd.token}")
    page.wait_for_selector("summary:has-text('How the projection reconciles')")
    page.locator("summary:has-text('How the projection reconciles')").click()
    bridge = page.locator("details.projection-bridge")
    for label in ("Cash", "Investments", "Debts", "Net worth"):
        assert bridge.locator(f"h4:has-text('{label}')").count() == 1
    assert bridge.locator("td:has-text('Reconciles exactly')").count() == 4

    page.locator("button.plan-cell-button").first.click()
    dialog = page.locator(".detail-dialog")
    dialog.locator("h3:has-text('How this month reconciles')").wait_for()
    assert dialog.locator("td:has-text('Reconciles exactly')").count() == 4
    assert dialog.locator("td:has-text('Planned events, net of transfers')").count() == 1


def test_a_register_opens_in_its_own_browser_tab(page, served):
    db, httpd = served
    page.get_by_role("button", name="Register", exact=True).first.click()
    page.wait_for_selector("text=Open in new tab")
    account = page.evaluate("() => state.account")
    with page.context.expect_page() as opened:
        page.get_by_role("button", name="Open in new tab").click()
    other = opened.value
    other.wait_for_selector("text=Open in new tab")
    assert other.evaluate("() => state.account") == account
    assert other.evaluate("() => current") == "Register"


def test_fsa_claims_needing_attention_show_on_both_dashboards(page, served):
    from datetime import timedelta

    from breadsched.gen.engine import fsa_claims
    from breadsched.gen.lib import (
        Account,
        AccountType,
        FsaClaim,
        FsaClaimAllocation,
        FsaFundingYear,
        Money,
    )

    db, httpd = served
    today = date.today()
    start = today.replace(month=1, day=1)
    assets = next(a for a in db.iter_accounts() if a.name == "Assets")
    account = Account(name="Health FSA", atype=AccountType.FSA, parent=assets.handle)
    account.fsa.years = [
        FsaFundingYear(start, start.replace(month=12, day=31), Money("1000"), None)
    ]
    with db.transaction("FSA") as txn:
        db.add_account(account, txn)
    for provider in ("Clinic", "Dentist"):
        fsa_claims.save_claim(
            db,
            FsaClaim(
                service_date=today - timedelta(days=45),
                provider=provider,
                allocations=[FsaClaimAllocation(account.handle, start)],
            ),
        )
    page.goto(f"http://127.0.0.1:{httpd.server_port}/?claims#token={httpd.token}")
    page.wait_for_selector("text=FSA claims needing attention")
    page.get_by_role("button", name="FSA Dashboard", exact=True).first.click()
    page.wait_for_selector("text=2 claims need attention.")
    assert page.locator("text=No EOB entered 45 days after the service").count() == 2
    page.locator("select[name=claims_by]").select_option("provider")
    page.wait_for_selector("th:text('Provider') >> nth=1")
    rows = page.locator("table").last.locator("tbody tr").all_inner_texts()
    assert [row.split("\t")[0] for row in rows] == ["Clinic", "Dentist", "All claims"]


def test_a_claim_is_closed_and_reopened_in_the_claims_editor(page, served):
    from breadsched.gen.engine import fsa_claims
    from breadsched.gen.lib import (
        Account,
        AccountType,
        FsaClaim,
        FsaClaimAllocation,
        FsaFundingYear,
        Money,
    )

    db, httpd = served
    today = date.today()
    start = today.replace(month=1, day=1)
    assets = next(a for a in db.iter_accounts() if a.name == "Assets")
    account = Account(name="Health FSA", atype=AccountType.FSA, parent=assets.handle)
    account.fsa.years = [
        FsaFundingYear(start, start.replace(month=12, day=31), Money("1000"), None)
    ]
    with db.transaction("FSA") as txn:
        db.add_account(account, txn)
    claim = FsaClaim(
        service_date=start,
        provider="Clinic",
        allocations=[FsaClaimAllocation(account.handle, start)],
    )
    fsa_claims.save_claim(db, claim)
    page.goto(f"http://127.0.0.1:{httpd.server_port}/?claims#token={httpd.token}")
    page.get_by_role("button", name="FSA Dashboard", exact=True).first.click()
    page.get_by_role("button", name="Manage FSA claims…").click()
    page.wait_for_selector("h2:text('FSA claims')")
    page.get_by_label("Why close").fill("Not worth appealing")
    page.get_by_role("button", name="Close claim").click()
    page.wait_for_selector("text=FSA claim closed.")
    page.wait_for_selector("text=Not worth appealing")
    assert db.get_fsa_claim(claim.handle).close_reason == "Not worth appealing"
    page.get_by_label("Why reopen").fill("Appeal won")
    page.get_by_role("button", name="Reopen").click()
    page.wait_for_selector("text=FSA claim reopened.")
    page.wait_for_selector("text=Reopened: Appeal won")
    assert db.get_fsa_claim(claim.handle).closed_on is None
    # Each allocation can link money paid back into the FSA.
    page.get_by_role("button", name="Edit", exact=True).click()
    page.wait_for_selector("text=Repaid to the FSA")


def test_a_withheld_net_worth_names_what_it_leaves_out(page, served):
    """#236: the label is text beside the value, with the evidence one click away."""
    from breadsched.gen.lib import Account, AccountType, Commodity, Money, Split, Transaction

    db, _httpd = served
    assets = db.get_account_by_name("Assets")
    equity = next(account for account in db.iter_accounts() if account.name == "Equity")
    assert assets is not None
    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    cash = Account(name="Euro cash", atype=AccountType.BANK, parent=assets.handle)
    cash.commodity = euro.handle
    opening = Transaction(post_date=date.today().replace(day=1), description="Euro opening")
    opening.currency = euro.handle
    opening.splits = [Split(cash.handle, Money(25)), Split(equity.handle, Money(-25))]
    with db.transaction("Euro cash") as txn:
        if db.get_commodity_by_mnemonic("EUR") is None:
            db.add_commodity(euro, txn)
        else:
            cash.commodity = db.get_commodity_by_mnemonic("EUR").handle
            opening.currency = cash.commodity
        db.add_account(cash, txn)
        db.add_transaction(opening, txn)

    # Leave and return so the Dashboard is fetched again with the new account.
    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Accounts", exact=True).first.click()
    page.get_by_role("button", name="Dashboard", exact=True).first.click()
    summary = page.locator("details.completeness-details summary").first
    summary.wait_for()
    assert summary.inner_text() == "Net worth: Unavailable: 1 amount lacks a quote"
    summary.click()
    detail = page.locator("details.completeness-details li").first.inner_text()
    assert detail.startswith("Assets:Euro cash 25.00 EUR")
    assert detail.endswith("no exchange rate")
    page.wait_for_selector("h2:has-text('Net worth history')")
    # The withheld point is left out of the charts, which say so, never drawn as zero.
    page.wait_for_selector(".net-worth-history .chart-partial-note")
    assert "Missing quote, so left out" in (
        page.locator(".net-worth-history .chart-partial-note").first.inner_text()
    )


def test_a_scenario_drawdown_is_added_from_manage_scenarios(page, served):
    from decimal import Decimal

    from breadsched.gen.lib import Account, AccountType, Scenario
    from breadsched.gen.services import drawdown_accounts

    db, httpd = served
    today = date.today()
    assets = next(item for item in db.iter_accounts() if item.name == "Assets")
    scenario = Scenario(name="Retire early", start=today.replace(day=1), years=5)
    with db.transaction("Retirement account and scenario") as txn:
        db.add_account(Account(name="IRA", atype=AccountType.RETIREMENT, parent=assets.handle), txn)
        db.add_scenario(scenario, txn)
    sources, targets = drawdown_accounts(db)
    page.goto(f"http://127.0.0.1:{httpd.server_port}/#token={httpd.token}")
    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Plan", exact=True).first.click()
    page.get_by_role("button", name="Manage scenarios…").click()
    page.wait_for_selector("h2:has-text('Manage scenarios')")
    page.locator(".toolbar select").first.select_option(label="Retire early")
    page.get_by_role("button", name="Add drawdown…").click()
    editor = page.locator("form.drawdown-editor")
    editor.locator("select[name=account]").select_option(value=sources[0])
    editor.locator("select[name=into]").select_option(value=targets[0])
    editor.locator("input[name=method][value=rate]").check()
    editor.locator("input[name=annual_rate]").fill("4.1")
    editor.get_by_role("button", name="Save drawdown").click()
    page.wait_for_selector("text=Drawdown saved.")
    page.wait_for_selector("text=4.1% of the balance a year")
    (stored,) = db.get_scenario(scenario.handle).drawdowns
    assert (stored.account, stored.into, stored.annual_rate) == (
        sources[0],
        targets[0],
        Decimal("0.041"),
    )


def test_a_scenario_expects_less_of_a_reimbursement(page, served):
    from datetime import timedelta

    from breadsched.gen.lib import AccountClass, Money, Scenario, Transaction
    from breadsched.gen.services.receivables import (
        SaveReceivable,
        attach_expense_split,
        save_receivable,
    )

    db, _httpd = served
    today = date.today()
    expense = next(
        a
        for a in db.iter_accounts()
        if a.account_class is AccountClass.EXPENSE and not a.placeholder and not a.hidden
    )
    bank = next(a for a in db.iter_accounts() if a.name == "Checking")
    visit = Transaction.simple(today, "Clinic", expense.handle, bank.handle, Money("90"))
    scenario = Scenario(name="Careful", start=today.replace(day=1), years=1)
    with db.transaction("Browser fixture") as txn:
        db.add_transaction(visit, txn)
        db.add_scenario(scenario, txn)
    receivable = save_receivable(
        db,
        SaveReceivable(
            incurred_date=today,
            payer="Acme Insurance",
            expected_cash_date=today + timedelta(days=30),
        ),
    ).value
    cost = next(s for s in visit.splits if s.account == expense.handle)
    assert attach_expense_split(db, receivable.handle, visit.handle, cost.handle).ok

    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Reimbursable Expenses", exact=True).first.click()
    page.get_by_role("button", name="Open", exact=True).first.click()
    page.wait_for_selector("text=Every scenario expects what this receivable says.")
    page.fill("input[aria-label='Amount expected in scenario']", "40")
    page.locator("select[aria-label='Scenario']").select_option(label="Careful")
    page.get_by_role("button", name="Apply to scenario").click()
    page.wait_for_selector("text=Scenario changes: Careful: 40.00 expected back")
    stored = db.get_scenario(scenario.handle).reimbursement_overrides[receivable.handle]
    assert (stored.amount, stored.on) == (Money(40), None)


def test_online_quotes_choose_a_source_and_fetch(page, served, monkeypatch, tmp_path):
    from decimal import Decimal

    from breadsched.gen.lib import Commodity
    from breadsched.gen.services.quotes import FetchedQuote
    from breadsched.gen.utils.settings import Settings
    from breadsched.web import quote_resource

    db, _httpd = served
    monkeypatch.setattr(quote_resource, "settings", lambda: Settings(directory=tmp_path / "cfg"))

    class Fetcher:
        def __init__(self, _key):
            pass

        def fetch(self, requests, reporting):
            return (
                [FetchedQuote("G", "tsp", Decimal("18.4521"), "USD", date.today(), "tsp.gov")],
                [],
            )

    monkeypatch.setattr(quote_resource, "make_fetcher", Fetcher)
    with db.transaction("Fund") as txn:
        db.add_commodity(Commodity(namespace="TSP", mnemonic="G", fullname="G Fund"), txn)

    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Accounts", exact=True).first.click()
    page.get_by_role("button", name="Online quotes…").click()
    source = page.locator("[aria-label='Quote source for G']")
    source.fill("tsp")
    page.get_by_role("button", name="Get quotes").click()
    page.wait_for_selector("text=G: 18.4521 USD")
    fund = db.get_commodity_by_mnemonic("G")
    assert fund.quote_source == "tsp"
    assert [price.source for price in db.iter_prices(fund.handle)] == ["Online: tsp.gov"]


def test_a_sale_names_its_lots_and_the_gains_report_prints_alone(page, served):
    from breadsched.gen.lib import Account, AccountType, Commodity, Money, Split, Transaction

    db, _httpd = served
    assets = next(item for item in db.iter_accounts() if item.name == "Assets")
    checking = next(item for item in db.iter_accounts() if item.atype is AccountType.BANK)
    fund = Commodity(namespace="FUND", mnemonic="IDX", fullname="Index", fraction=1000)
    account = Account(
        name="Index",
        atype=AccountType.INVESTMENT,
        parent=assets.handle,
        commodity=fund.handle,
        commodity_scu=1000,
    )
    trades = []
    with db.transaction("Trades") as txn:
        db.add_commodity(fund, txn)
        db.add_account(account, txn)
        for when, quantity, value in (
            (date(2024, 1, 10), "10", "1000"),
            (date(2025, 1, 10), "10", "1500"),
            (date(2026, 2, 1), "-5", "-1000"),
        ):
            trade = Transaction(post_date=when, description="Trade")
            trade.currency = checking.commodity
            trade.splits = [
                Split(account.handle, Money(value), quantity=Money(quantity)),
                Split(checking.handle, -Money(value)),
            ]
            db.add_transaction(trade, txn)
            trades.append(trade)

    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Accounts", exact=True).first.click()
    page.get_by_role("button", name="Holdings and cost basis…").click()
    page.locator("details.holding summary").first.click()
    page.get_by_role("button", name="Choose the lots sold on 2026-02-01").click()
    page.locator("[aria-label='Shares to sell from the lot bought 2025-01-10']").fill("5")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector("text=named lots")
    assert db.get_transaction(trades[2].handle).splits[0].lot_picks[0].lot == trades[1].handle

    page.get_by_role("button", name="Realized gains…").click()
    dialog = page.locator(".gains-dialog")
    dialog.wait_for()
    assert "bought 2025-01-10: 5 shares, cost 750.00" in dialog.inner_text()
    page.evaluate("window.print = () => { window.__printed = document.body.className; }")
    dialog.get_by_role("button", name="Print").click()
    assert "printing-dialog" in page.evaluate("window.__printed")
    page.emulate_media(media="print")
    assert page.locator("main").is_hidden()
    assert dialog.is_visible()
    page.emulate_media(media="screen")


def test_the_tax_year_marks_accounts_and_tags_and_prints_alone(page, served):
    from breadsched.gen.lib import AccountType, Money, Split, Transaction

    db, _httpd = served
    checking = next(item for item in db.iter_accounts() if item.atype is AccountType.BANK)
    expense = next(item for item in db.iter_accounts() if item.atype is AccountType.EXPENSE)
    expense_name = db.full_name(expense)
    gift = Transaction(post_date=date(2025, 4, 1), description="Gift")
    gift.currency = checking.commodity
    gift.splits = [Split(expense.handle, Money(80)), Split(checking.handle, Money(-80))]
    gift.tags = ["Charity"]
    with db.transaction("Gift") as txn:
        db.add_transaction(gift, txn)

    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Accounts", exact=True).first.click()
    page.get_by_role("button", name="Tax year…").click()
    dialog = page.locator(".tax-dialog")
    dialog.wait_for()
    dialog.get_by_label("Tax year").select_option("2025")
    dialog = page.locator(".tax-dialog")
    page.wait_for_selector(".tax-dialog h2:has-text('Tax year 2025')")
    assert "No account is marked tax-relevant." in dialog.inner_text()
    assert "Income by source" in dialog.inner_text()

    dialog.get_by_role("button", name="Tax-relevant accounts and tags…").click()
    page.get_by_label("Tag Charity is tax-relevant").check()
    page.get_by_label(f"{expense_name} is tax-relevant").check()
    page.locator(".tax-marks-backdrop").get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".tax-dialog >> text=BreadSched")
    dialog = page.locator(".tax-dialog")
    text = dialog.inner_text()
    assert expense_name in text and "Charity" in text and "80.00" in text
    assert db.get_account(expense.handle).planning.tax_relevant is True
    assert db.get_metadata("tax.tags") == ["Charity"]

    page.evaluate("window.print = () => { window.__printed = document.body.className; }")
    dialog.get_by_role("button", name="Print").click()
    assert "printing-dialog" in page.evaluate("window.__printed")
    page.emulate_media(media="print")
    assert page.locator("main").is_hidden()
    assert dialog.is_visible()
    page.emulate_media(media="screen")


def test_budget_jars_open_from_the_plan_and_print_alone(page, served):
    from breadsched.gen.lib import (
        AccountType,
        Money,
        PeriodType,
        Recurrence,
        ScheduledSplit,
        ScheduledTransaction,
    )

    db, _httpd = served
    checking = next(item for item in db.iter_accounts() if item.atype is AccountType.BANK)
    expense = next(
        item
        for item in db.iter_accounts()
        if item.atype is AccountType.EXPENSE and not item.placeholder
    )
    estimate = ScheduledTransaction(
        name="Market estimate",
        recurrence=Recurrence(PeriodType.MONTH, interval=1, start=date(2026, 1, 20)),
        splits=[
            ScheduledSplit(expense.handle, Money(600)),
            ScheduledSplit(checking.handle, Money(-600)),
        ],
    )
    estimate.placeholder = True
    with db.transaction("Estimate") as txn:
        db.add_scheduled(estimate, txn)

    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Plan", exact=True).first.click()
    page.get_by_role("button", name="Budget jars…").click()
    dialog = page.locator(".jars-dialog")
    dialog.wait_for()
    assert "Market estimate (estimate)" in dialog.inner_text()
    assert db.full_name(expense) in dialog.inner_text()
    # Each account's charts are drawn with their tables; a column names its amount.
    figure = dialog.locator(".model-chart").first
    assert figure.locator("svg path").count() >= 1
    assert "Planned" in figure.locator("svg path title").first.text_content()
    assert figure.locator("table").count() == 1
    dialog.get_by_label("Group by").select_option("year")
    page.wait_for_selector(".jars-dialog >> text=Market estimate (estimate)")
    dialog = page.locator(".jars-dialog")
    page.evaluate("window.print = () => { window.__printed = document.body.className; }")
    dialog.get_by_role("button", name="Print").click()
    assert "printing-dialog" in page.evaluate("window.__printed")
    page.emulate_media(media="print")
    assert page.locator("main").is_hidden()
    assert dialog.is_visible()
    page.emulate_media(media="screen")


def test_the_projection_chart_is_the_shared_line_chart_with_its_values(page):
    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Projection", exact=True).first.click()
    figure = page.locator(".model-chart").first
    figure.wait_for()
    assert "Projected cash, investments, and net worth" in figure.locator("figcaption").inner_text()
    # One line per series, a hover title per month, and the exact values behind a toggle.
    assert figure.locator("svg path").count() >= 3
    hit = figure.locator("svg rect.chart-hit title").first.text_content()
    assert hit.splitlines()[1].startswith("Cash: ")
    values = figure.locator("details.chart-values")
    assert values.locator("tbody tr").count() >= 12
    values.locator("summary").click()
    assert values.locator("table").is_visible()
    # Year-end balances by account, stacked, with each year's net worth as its total.
    balances = page.locator("figure.model-chart[data-key='projection_balances']")
    assert balances.count() == 1
    assert balances.locator("svg path[fill^='var(--series-']").count() >= 1
    assert "Total" in balances.locator("details.chart-values thead").text_content()


def _add_fund(db):
    """A security account with one imported buy, as a GnuCash book would bring it (#323)."""
    from breadsched.gen.lib import Account, AccountType, Commodity, Money, Split, Transaction

    usd = next(item for item in db.iter_commodities() if item.is_currency)
    parent = next(a for a in db.iter_accounts() if db.full_name(a) == "Assets")
    cash = next(
        a
        for a in db.iter_accounts()
        if a.atype is AccountType.BANK and not a.hidden and not a.placeholder
    )
    fund = Commodity(namespace="FUND", mnemonic="VTTHX", fullname="Target 2035", fraction=1000)
    account = Account(
        name="VTTHX",
        atype=AccountType.INVESTMENT,
        parent=parent.handle,
        commodity=fund.handle,
        commodity_scu=1000,
    )
    buy = Transaction(post_date=date(2026, 2, 3), description="Fund buy")
    buy.currency = usd.handle
    buy.splits = [
        Split(account.handle, Money("250"), quantity=Money("10.123")),
        Split(cash.handle, Money("-250")),
    ]
    with db.transaction("Fund") as txn:
        db.add_commodity(fund, txn)
        db.add_account(account, txn)
        db.add_transaction(buy, txn)
    return account, cash


def test_a_security_register_shows_and_takes_shares(page, served):
    from breadsched.gen.lib import Money

    db, httpd = served
    account, cash = _add_fund(db)
    # The page drops its token from the address once read, so open it afresh.
    page.goto("about:blank")
    page.goto(f"http://127.0.0.1:{httpd.server_port}/#token={httpd.token}")
    _open_register(page)
    page.locator("select").first.select_option(account.handle)
    page.wait_for_selector("th:has-text('Shares Held')")
    row = page.locator("tr:has-text('Fund buy')").first
    assert "10.123" in row.inner_text() and "24.696236" in row.inner_text()
    page.wait_for_selector("text=10.123 VTTHX shares · no quote; ledger amount 250.00")

    _blank(page, "description").fill("Browser fund buy")
    _blank(page, "transfer account").select_option(cash.handle)
    _blank(page, "shares").fill("8")
    _blank(page, "price").fill("30.25")
    _blank(page, "price").press("Tab")
    page.wait_for_function(
        "() => [...document.querySelectorAll('tr.entry-row input.num')]"
        ".some((node) => node.value === '242.00')"
    )
    _blank(page, "price").press("Enter")
    page.wait_for_selector("text=Posted Browser fund buy.")
    posted = next(t for t in db.iter_transactions() if t.description == "Browser fund buy")
    mine = posted.split_for(account.handle)
    assert (mine.value, mine.quantity) == (Money("242.00"), Money(8))
    page.wait_for_selector("text=18.123 VTTHX shares")

    # Enter straight from the price cell still works the amount out first.
    _blank(page, "description").fill("Quick fund buy")
    _blank(page, "transfer account").select_option(cash.handle)
    _blank(page, "shares").fill("2")
    _blank(page, "price").fill("25")
    _blank(page, "price").press("Enter")
    page.wait_for_selector("text=Posted Quick fund buy.")
    quick = next(t for t in db.iter_transactions() if t.description == "Quick fund buy")
    assert (quick.split_for(account.handle).value, quick.split_for(account.handle).quantity) == (
        Money(50),
        Money(2),
    )

    before = sorted(t.handle for t in db.iter_transactions())
    _blank(page, "description").fill("No shares")
    _blank(page, "transfer account").select_option(cash.handle)
    page.locator("tr.entry-row [aria-label='New transaction buy']").first.fill("10")
    page.locator("tr.entry-row [aria-label='New transaction buy']").first.press("Enter")
    page.wait_for_selector("text=Enter the shares bought or sold, or a price and an amount.")
    assert sorted(t.handle for t in db.iter_transactions()) == before


def test_deleting_a_rule_asks_first(page, served):
    # #335: every delete asks before it removes anything, in the browser too.
    from breadsched.gen.services.categorization import list_rules

    db, _httpd = served
    page.wait_for_selector("text=Pending bills")
    page.get_by_role("button", name="Rules", exact=True).first.click()
    page.wait_for_selector("text=No rules yet.")
    page.fill("input[name=description]", "Corner Store")
    page.get_by_role("button", name="Add rule").click()
    page.wait_for_selector("td:has-text('Description corner store')")
    assert len(list_rules(db)) == 1

    asked: list[str] = []

    def answer(accept):
        def handle(dialog):
            asked.append(dialog.message)
            dialog.accept() if accept else dialog.dismiss()

        return handle

    page.once("dialog", answer(False))
    page.get_by_role("button", name="Delete").click()
    page.wait_for_timeout(300)
    assert asked == ["Delete the rule for “corner store”?"]
    assert len(list_rules(db)) == 1
    page.once("dialog", answer(True))
    page.get_by_role("button", name="Delete").click()
    page.wait_for_selector("text=No rules yet.")
    assert list_rules(db) == []


def test_reconciliation_marks_entries_after_the_statement(page, served):
    """#362: later entries are listed, marked, and never checked by default."""
    db, _httpd = served
    _open_register(page)
    register = page.locator("select").first.input_value()
    page.get_by_role("button", name="Reconcile…").click()
    early = min(t.post_date for t in db.iter_transactions(account=register))
    page.locator(".detail-dialog input[type=date]").fill(early.isoformat())
    page.locator(".detail-dialog input[inputmode=decimal]").fill("0")
    page.get_by_role("button", name="Start reconciliation").click()
    later = page.locator(".detail-dialog tr.after-statement")
    later.first.wait_for()
    assert later.count() >= 1
    assert "after statement" in later.first.inner_text()
    assert not later.first.locator("input[type=checkbox]").is_checked()
    page.wait_for_selector(".detail-dialog >> text=Funds in")


def test_a_reconciliation_row_opens_its_transaction_in_the_register(page, served):
    """#374: Edit on a statement row saves the checks and edits the row in place."""
    db, _httpd = served
    _open_register(page)
    register = page.locator("select").first.input_value()
    page.get_by_role("button", name="Reconcile…").click()
    latest = max(t.post_date for t in db.iter_transactions(account=register))
    page.locator(".detail-dialog input[type=date]").fill(latest.isoformat())
    page.locator(".detail-dialog input[inputmode=decimal]").fill("0")
    page.get_by_role("button", name="Start reconciliation").click()
    first = page.locator(".detail-dialog tbody tr").first
    first.wait_for()
    first.locator("input[type=checkbox]").check()
    split = first.locator("input[type=checkbox]").get_attribute("value")
    first.get_by_role("button", name="Edit").click()
    page.locator(".detail-dialog").wait_for(state="detached")
    page.locator("tr.entry-row").first.wait_for()
    from breadsched.gen.lib import ReconciliationStatus

    session = next(
        item
        for item in db.iter_reconciliations(register)
        if item.status is ReconciliationStatus.OPEN
    )
    assert split in session.selected_splits
    page.get_by_role("button", name="Back to reconciliation").click()
    page.locator(".detail-dialog").wait_for()
    assert page.locator(f".detail-dialog input[value='{split}']").is_checked()


def test_reconciliation_headings_sort_the_entries_and_keep_checks(page, served):
    """#377: a column heading orders the statement's entries; again reverses them."""
    db, _httpd = served
    _open_register(page)
    register = page.locator("select").first.input_value()
    page.get_by_role("button", name="Reconcile…").click()
    latest = max(t.post_date for t in db.iter_transactions(account=register))
    page.locator(".detail-dialog input[type=date]").fill(latest.isoformat())
    page.locator(".detail-dialog input[inputmode=decimal]").fill("0")
    page.get_by_role("button", name="Start reconciliation").click()
    rows = page.locator(".detail-dialog tbody tr")
    rows.first.wait_for()
    first = rows.first.locator("input[type=checkbox]")
    first.uncheck()
    checked_split = first.get_attribute("value")
    first.check()

    def amounts() -> list[float]:
        cells = page.locator(".detail-dialog tbody tr td.num").all_inner_texts()
        return [abs(float(text.replace(",", "").replace("(", "-").strip(")"))) for text in cells]

    page.get_by_role("button", name="Amount").click()
    page.get_by_role("button", name="Amount ▲").wait_for()
    assert amounts() == sorted(amounts())
    page.get_by_role("button", name="Amount ▲").click()
    page.get_by_role("button", name="Amount ▼").wait_for()
    assert amounts() == sorted(amounts(), reverse=True)
    # The check made before sorting was saved and is still shown.
    assert page.locator(f".detail-dialog input[value='{checked_split}']").is_checked()
    page.get_by_role("button", name="✓").click()
    page.get_by_role("button", name="✓ ▲").wait_for()
    assert rows.first.locator("input[type=checkbox]").is_checked()


def test_a_schedule_repeats_every_n_units_with_a_calculated_amount(page, served):
    """#370: Every N weeks/months/years, and arithmetic in the amount."""
    from breadsched.gen.lib import Money

    db, _httpd = served
    page.wait_for_selector("#nav button")
    page.evaluate(
        "() => [...document.querySelectorAll('#nav button')]"
        ".find((button) => button.textContent === 'Scheduled').click()"
    )
    page.get_by_role("button", name="New scheduled…").click()
    form = page.locator(".detail-dialog form")
    form.locator("input[name=name]").fill("Every four weeks")
    form.locator("input[name=amount]").fill("76.84*1.06")
    expense = next(
        a for a in db.iter_accounts() if a.account_class.value == "expense" and not a.placeholder
    )
    bank = next(a for a in db.iter_accounts() if a.atype.value == "BANK" and not a.placeholder)
    form.locator("select[name=category]").select_option(expense.handle)
    form.locator("select[name=funding]").select_option(bank.handle)
    form.locator("select[name=frequency]").select_option("weeks")
    form.locator("input[name=interval]").fill("4")
    form.locator("select[name=frequency]").select_option("once")
    assert form.locator("input[name=interval]").is_hidden()
    form.locator("select[name=frequency]").select_option("weeks")
    assert form.locator("input[name=interval]").is_visible()
    form.locator("input[name=start]").fill("2026-01-06")
    form.get_by_role("button", name="Save").click()
    page.wait_for_function("() => !document.querySelector('.detail-dialog form')")
    saved = next(item for item in db.iter_scheduled() if item.name == "Every four weeks")
    assert (saved.recurrence.period.value, saved.recurrence.interval) == ("week", 4)
    assert saved.amount() == Money("81.45")


def test_the_last_split_line_holds_the_rest_and_tab_skips_to_withdrawal(page, served):
    """#369: an unbalanced split offers the rest on its empty line; an expense
    transfer tabs to the money-out column."""
    from breadsched.gen.lib import AccountClass

    db, _httpd = served
    _open_register(page)
    register = page.locator("select").first.input_value()
    expense = next(
        a
        for a in db.iter_accounts()
        if a.account_class is AccountClass.EXPENSE and not a.placeholder and not a.hidden
    )
    other = next(
        a
        for a in db.iter_accounts()
        if a.account_class is AccountClass.EXPENSE
        and not a.placeholder
        and not a.hidden
        and a.handle != expense.handle
    )
    transfer = page.locator("[aria-label='New transaction transfer account']")
    transfer.select_option(expense.handle)
    transfer.focus()
    page.keyboard.press("Tab")
    focused = page.evaluate("() => document.activeElement.getAttribute('aria-label')")
    # The second amount cell of the new row is the money-out column.
    amounts = page.locator("tr.entry-row:has([aria-label='New transaction description']) input.num")
    decrease_label = amounts.nth(1).get_attribute("aria-label")
    assert focused == decrease_label

    _blank(page, "description").fill("Split rest")
    page.locator(f"[aria-label='{decrease_label}']").fill("9.00")
    page.locator("tr:has([aria-label='New transaction description'])").get_by_role(
        "button", name="Split", exact=True
    ).click()
    page.wait_for_selector("[aria-label='New split 3 account']")
    # Take 4.00 off the expense line: the empty third line shows the 4.00 left.
    lines = page.locator("tr.entry-line")
    expense_row = lines.filter(has=page.locator(f"option[value='{expense.handle}']:checked"))
    for field in expense_row.locator("input.num").all():
        if field.input_value():
            field.fill("5.00")
            field.dispatch_event("input")
    rest = page.locator("tr.entry-line input.rest")
    assert rest.count() == 1 and rest.get_attribute("placeholder") == "4.00"
    page.locator("[aria-label='New split 3 account']").select_option(other.handle)
    assert rest.count() == 0 or rest.input_value() == "4.00"
    page.locator("[aria-label='New transaction description']").press("Enter")
    page.wait_for_selector("text=Posted Split rest.")
    stored = next(t for t in db.iter_transactions() if t.description == "Split rest")
    values = {split.account: split.value for split in stored.splits}
    assert values[register] == -values[expense.handle] - values[other.handle]
    assert abs(values[other.handle].to_decimal()) == 4


def test_configure_dashboard_sets_sections_groups_and_columns(page, served):
    """Configure Dashboard holds the horizons, sections, groups, and columns (#381)."""
    db, _httpd = served
    page.wait_for_selector("text=Pending bills")
    # The horizons moved off the page into the dialog.
    assert page.locator("#view input[type=number]").count() == 0
    page.get_by_role("button", name="Configure Dashboard…").click()
    dialog = page.locator(".dashboard-config")
    dialog.wait_for()
    dialog.locator("input[type=number]").first.fill("45")
    dialog.locator("input[name=section-bills]").uncheck()
    dialog.locator("input[name=section-reimbursables]").check()
    # Expected income: hide Frequency (index 3) for this browser only.
    dialog.locator("input[name=column-income-3]").uncheck()
    assert dialog.locator("input[name=column-income-0]").is_disabled()

    # A new group takes an account typed into its searchable picker.
    dialog.get_by_role("button", name="Add group").click()
    card = dialog.locator(".card").last
    card.locator("input").first.fill("Browser group")
    account = page.evaluate(
        "() => document.querySelector('#dashboard-config-accounts option').value"
    )
    card.get_by_label("Add an account").fill(account)
    card.get_by_role("button", name="Add", exact=True).click()
    assert card.locator(".group-members li").count() == 1
    dialog.get_by_role("button", name="Save").click()
    page.wait_for_selector(".dashboard-config", state="detached")

    page.wait_for_selector("text=Pending reimbursables")
    assert page.locator("h2", has_text="Pending bills").count() == 0
    income = page.locator("h2", has_text="Expected income").locator("xpath=following-sibling::*[1]")
    headers = income.locator("th").all_inner_texts()
    assert "Frequency" not in headers and "Item" in headers
    assert page.locator("text=Needed in 45 days").count() == 1

    from user_fixtures import own_preference

    from breadsched.gen.engine.dashboard import DashboardConfig

    # The page is the computer account's, so its own Dashboard (decision 0004).
    stored = DashboardConfig.load(db, own_preference(db, "dashboard"))
    assert stored.liquidity_days == 45
    assert stored.shows("bills") is False and stored.shows("reimbursables") is True
    [group] = [item for item in stored.groups if item.name == "Browser group"]
    assert len(group.accounts) == 1
    # Columns are the browser's, not the book's.
    assert "Frequency" not in str(db.get_metadata("dashboard"))


def test_the_schedule_editor_folds_away_what_most_schedules_do_not_need(page, served):
    """#383: splits, future changes, and defaults are folded; their summary counts."""
    page.wait_for_selector("#nav button")
    page.evaluate(
        "() => [...document.querySelectorAll('#nav button')]"
        ".find((button) => button.textContent === 'Scheduled').click()"
    )
    page.get_by_role("button", name="New scheduled…").click()
    form = page.locator(".detail-dialog form")
    form.locator("input[name=name]").wait_for()
    summaries = form.locator("details.form-section > summary").all_inner_texts()
    assert summaries == ["Splits and memos", "Future changes", "Advanced"]
    assert form.locator("select[name=growth_policy]").is_hidden()
    assert form.locator("input[name=amount]").is_visible()
    form.locator("summary", has_text="Advanced").click()
    assert form.locator("select[name=growth_policy]").is_visible()
    # Investment activity only for an investment or retirement account.
    db, _httpd = served
    expense = next(
        a for a in db.iter_accounts() if a.account_class.value == "expense" and not a.placeholder
    )
    bank = next(a for a in db.iter_accounts() if a.atype.value == "BANK" and not a.placeholder)
    form.locator("select[name=category]").select_option(expense.handle)
    form.locator("select[name=funding]").select_option(bank.handle)
    assert form.locator("select[name=investment_activity]").is_hidden()
    form.locator("summary", has_text="Future changes").click()
    form.get_by_role("button", name="Add date and amount").first.click()
    form.locator("summary", has_text="Future changes (1)").wait_for()


def test_quick_entry_offers_investment_activity_only_for_investment_accounts(page, served):
    """#383: the browser's two-split entry hides investment activity elsewhere."""
    page.wait_for_selector("#nav button")
    page.evaluate(
        "() => [...document.querySelectorAll('#nav button')]"
        ".find((button) => button.textContent === 'Enter').click()"
    )
    form = page.locator("form.entry")
    form.locator("select[name=from]").wait_for()
    investing = page.evaluate(
        "() => state.accounts.filter((a) => ['INVESTMENT', 'RETIREMENT'].includes(a.type)"
        " && !a.placeholder && !a.hidden && a.depth > 0).map((a) => a.full_name)"
    )
    plain = page.evaluate(
        "() => state.accounts.filter((a) => a.type === 'BANK' && !a.placeholder && !a.hidden"
        " && a.depth > 0).map((a) => a.full_name)"
    )
    form.locator("select[name=from]").select_option(plain[0])
    form.locator("select[name=to]").select_option(plain[-1])
    assert form.locator(".investment-activity").is_hidden()
    if investing:
        form.locator("select[name=to]").select_option(investing[0])
        assert form.locator(".investment-activity").is_visible()


def test_find_searches_every_register_with_subtotals(page, served):
    """#382: the Find view runs the shared query and shows group subtotals."""
    db, _httpd = served
    page.wait_for_selector("#nav button")
    page.evaluate(
        "() => [...document.querySelectorAll('#nav button')]"
        ".find((button) => button.textContent === 'Find').click()"
    )
    form = page.locator("form.find-form")
    form.wait_for()
    expenses = next(a for a in db.iter_accounts() if a.name == "Expenses" and a.placeholder)
    form.locator("select[name=account]").select_option(expenses.handle)
    form.locator("select[name=group_by]").select_option("month")
    form.get_by_role("button", name="Find").click()
    page.wait_for_selector(".find-results tr.total")
    text = page.locator(".find-results").inner_text()
    assert "Subtotal: " in text and "Total: " in text
    first_account = page.locator(".find-results tbody button").first.inner_text()
    assert first_account.startswith("Expenses:")


@pytest.fixture
def phone(served):
    """The quick entry page on a phone-sized screen, touch and all."""
    _db, httpd = served
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(executable_path=_chromium())
        except Exception as exc:  # noqa: BLE001 - no usable browser here
            pytest.skip(f"Chromium is not available: {exc}")
        view = browser.new_page(
            viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
        )
        errors: list[str] = []
        view.on("pageerror", lambda error: errors.append(str(error)))
        view.goto(f"http://127.0.0.1:{httpd.server_port}/quick#token={httpd.token}")
        yield view
        browser.close()
        assert errors == []


def test_quick_entry_saves_from_a_phone(phone, served):
    """A transaction can be entered on a small screen, apart from the full interface."""
    db, _httpd = served
    phone.locator("#quick-account option").nth(1).wait_for(state="attached")
    # One column that fits the screen: nothing scrolls sideways.
    assert phone.evaluate("document.documentElement.scrollWidth") <= 390
    # The amount is ready to type, with the phone's number keyboard.
    assert phone.evaluate("document.activeElement.id") == "quick-amount"
    assert phone.locator("#quick-amount").get_attribute("inputmode") == "decimal"
    expense = next(
        account
        for account in db.iter_accounts()
        if account.account_class.value == "expense" and not account.placeholder
    )
    fund = phone.locator("#quick-account option").nth(1).get_attribute("value")
    before = {item.handle for item in db.iter_transactions()}
    phone.locator("#quick-amount").fill("12.34")
    phone.locator("#quick-description").fill("Corner bakery")
    phone.locator("#quick-account").select_option(fund)
    phone.locator("#quick-category").select_option(expense.handle)
    phone.get_by_role("button", name="Save").tap()
    phone.locator(".quick-recent li").first.wait_for()
    [made] = [item for item in db.iter_transactions() if item.handle not in before]
    assert made.description == "Corner bakery"
    assert made.split_for(expense.handle).value.to_decimal() == Decimal("12.34")
    assert made.split_for(fund).value.to_decimal() == Decimal("-12.34")
    # The form is ready for the next one; the account money came from is kept.
    assert phone.locator("#quick-amount").input_value() == ""
    assert phone.locator("#quick-account").input_value() == fund
    # Received money goes into the account instead.
    phone.get_by_role("button", name="Received").tap()
    assert phone.locator("label[for=quick-account]").inner_text() == "Paid into"
    # A missing category is refused before anything is sent.
    phone.locator("#quick-amount").fill("5")
    phone.get_by_role("button", name="Save").tap()
    phone.wait_for_selector("text=Enter an amount, an account, and a category.")
    assert len(list(db.iter_transactions())) == len(before) + 1
    # The full interface is a link away, keeping the token.
    assert "token=" in phone.locator("#full-view").get_attribute("href")


def test_a_device_without_a_token_signs_in_and_chooses_its_password(served):
    """Decision 0004: a phone opening the page has no token; it signs in, changes a
    temporary password, and then keeps a session cookie across reloads."""
    from breadsched.api.local import book_server
    from breadsched.api.users import UserDirectory

    db, httpd = served
    directory = UserDirectory()
    directory.add_user("alice", "temporary-pass", display_name="Alice")
    directory.grant(book_server(db).book_id() or "", "alice", "editor")
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(executable_path=_chromium())
        except Exception as exc:  # noqa: BLE001 - no usable browser here
            pytest.skip(f"Chromium is not available: {exc}")
        view = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
        errors: list[str] = []
        view.on("pageerror", lambda error: errors.append(str(error)))
        view.goto(f"http://127.0.0.1:{httpd.server_port}/quick")
        view.wait_for_selector("text=Sign in to BreadSched")
        view.fill("input[name=name]", "alice")
        view.fill("input[name=password]", "wrong-password")
        view.click("button:has-text('Sign in')")
        view.wait_for_selector("text=That name and password do not match")
        view.fill("input[name=password]", "temporary-pass")
        view.click("button:has-text('Sign in')")
        view.wait_for_selector("text=Choose your password")
        view.fill("input[name=current]", "temporary-pass")
        view.fill("input[name=new]", "alices-own-password")
        view.click("button:has-text('Change password')")
        view.locator("#quick-amount").wait_for()
        # The session cookie keeps the phone signed in, and script cannot read it.
        assert view.evaluate("document.cookie") == ""
        view.reload()
        view.locator("#quick-amount").wait_for()
        assert errors == []
        browser.close()


def test_an_entry_user_adds_transactions_from_a_phone_and_nothing_else(served):
    """Decision 0004's entry role: quick entry works; the full view's book is refused."""
    from breadsched.api.users import UserDirectory

    db, httpd = served
    directory = UserDirectory()
    directory.add_user("kid", "temporary-pass")
    directory.set_password("kid", "kids-own-password", current="temporary-pass")
    from breadsched.api.local import book_server

    directory.grant(book_server(db).book_id() or "", "kid", "entry")
    base = f"http://127.0.0.1:{httpd.server_port}"
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(executable_path=_chromium())
        except Exception as exc:  # noqa: BLE001 - no usable browser here
            pytest.skip(f"Chromium is not available: {exc}")
        view = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
        view.goto(f"{base}/quick")
        view.fill("input[name=name]", "kid")
        view.fill("input[name=password]", "kids-own-password")
        view.click("button:has-text('Sign in')")
        view.locator("#quick-account option").nth(1).wait_for(state="attached")
        expense = next(
            account
            for account in db.iter_accounts()
            if account.account_class.value == "expense" and not account.placeholder
        )
        before = {item.handle for item in db.iter_transactions()}
        view.locator("#quick-amount").fill("3.50")
        view.locator("#quick-description").fill("Ice cream")
        view.locator("#quick-account").select_option(index=1)
        view.locator("#quick-category").select_option(expense.handle)
        view.get_by_role("button", name="Save").click()
        view.locator(".quick-recent li").first.wait_for()
        [made] = [item for item in db.iter_transactions() if item.handle not in before]
        assert made.description == "Ice cream"
        # The full view reads the book, which an entry user may not.
        status = view.evaluate("async () => (await fetch('/api/v1/chart')).status")
        assert status == 403
        browser.close()


def test_a_person_chooses_among_the_books_a_host_shares_with_them(tmp_path):
    """Decision 0004: one host serves several books; each person chooses among those
    shared with them, the page remembers the choice, and one shared book needs none."""
    from breadsched.api.users import UserDirectory

    books = []
    for name in ("Home", "Rental"):
        path = tmp_path / f"{name}.breadsched"
        create_sample_book(path, as_of=date.today())
        db = DbSQLite()
        db.load(str(path))
        books.append(db)
    httpd = serve(books, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    home_id, rental_id = (db.get_metadata("book.id") for db in books)
    directory = UserDirectory()
    for name in ("vera", "kid"):
        directory.add_user(name, "temporary-pass")
        directory.set_password(name, f"{name}-own-password", current="temporary-pass")
    directory.grant(home_id, "vera", "editor")
    directory.grant(rental_id, "vera", "viewer")
    directory.grant(home_id, "kid", "entry")
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        with sync_api.sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(executable_path=_chromium())
            except Exception as exc:  # noqa: BLE001 - no usable browser here
                pytest.skip(f"Chromium is not available: {exc}")
            view = browser.new_page()
            errors: list[str] = []
            view.on("pageerror", lambda error: errors.append(str(error)))
            view.goto(base + "/")
            view.fill("input[name=name]", "vera")
            view.fill("input[name=password]", "vera-own-password")
            view.click("button:has-text('Sign in')")
            view.wait_for_selector("text=Choose a book")
            choices = view.locator(".book-choice li").all_inner_texts()
            assert [" ".join(choice.split()) for choice in choices] == [
                "Home Editor",
                "Rental Viewer",
            ]
            view.get_by_role("button", name="Rental").click()
            view.locator("#book", has_text="Rental").wait_for()
            assert f"book={rental_id}" in view.url
            assert f"book={rental_id}" in view.locator("#quick-entry").get_attribute("href")
            # A reload keeps the book; Other books goes back to the choice.
            view.reload()
            view.locator("#book", has_text="Rental").wait_for()
            assert view.locator(".book-choice").count() == 0
            view.get_by_role("button", name="Other books").click()
            view.wait_for_selector("text=Choose a book")
            view.get_by_role("button", name="Home").click()
            view.locator("#book", has_text="Home").wait_for()
            assert errors == []
            # Someone given one book goes straight to it.
            phone = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
            phone.goto(base + "/quick")
            phone.fill("input[name=name]", "kid")
            phone.fill("input[name=password]", "kid-own-password")
            phone.click("button:has-text('Sign in')")
            phone.locator("#quick-account option").nth(1).wait_for(state="attached")
            assert phone.locator(".book-choice").count() == 0
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        for db in books:
            db.close()
