"""Rendered web views, checked in a real browser where one is available.

The JSON API tests cannot see how ``app.js`` turns data into DOM. These tests run
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
    assert panel.locator("tbody tr").first.locator("td").nth(6).inner_text() == "New"
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
    page.get_by_role("button", name="Reimbursables", exact=True).first.click()
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
    page.get_by_role("button", name="Reimbursables", exact=True).first.click()
    page.wait_for_selector("text=Proposed reimbursements")
    page.get_by_role("button", name="Accept selected").click()
    page.wait_for_selector("text=Linked 1 reimbursement(s)")
    assert len(db.get_receivable(receivable.handle).reimbursements) == 1


def test_dashboard_shows_net_worth_history_and_regroups_it(page):
    page.wait_for_selector("text=Pending bills")
    page.wait_for_selector("h2:has-text('Net worth history')")
    rows = page.locator(".net-worth-history tbody tr")
    assert rows.count() >= 1
    assert page.locator("svg.net-worth-chart path").count() == 1
    months = rows.count()
    page.locator(".net-worth-history select").select_option("year")
    page.wait_for_function(
        "(count) => document.querySelectorAll('.net-worth-history tbody tr').length < count"
        " || document.querySelectorAll('.net-worth-history tbody tr').length <= 2",
        arg=months,
    )
    assert "20" in page.locator(".net-worth-history tbody tr").first.inner_text()


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
    periods = page.locator(".spending-over-time tbody tr")
    assert periods.count() >= 2
    assert page.locator("svg.spending-chart path").count() == 2
    label = periods.nth(1).locator("button").inner_text()
    periods.nth(1).locator("button").click()
    page.wait_for_selector(f".spending-over-time tbody tr.selected:has-text('{label}')")
    selected = page.locator(".expense-explorer select").first.evaluate("(node) => node.value")
    assert selected == "1"
    # Income over time has its own chart and table over the same periods, and
    # selecting a period there selects it for the whole explorer.
    income = page.locator(".income-over-time tbody tr")
    assert income.count() == periods.count()
    assert page.locator("svg.income-chart path").count() == 2
    # The income detail lists the selected period's dated receipts by payer.
    page.wait_for_selector(".income-detail h3")
    assert page.locator(".income-detail select option").count() >= 1
    assert "Payer" in page.locator(".income-detail").inner_text()
    income.nth(0).locator("button").click()
    page.wait_for_selector(".spending-over-time tbody tr.selected >> nth=0")
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
    page.get_by_role("button", name="Goals", exact=True).first.click()
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

    page.get_by_role("button", name="Goals", exact=True).first.click()
    page.wait_for_selector("text=Set aside for goals")
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
    page.get_by_role("button", name="Goals", exact=True).first.click()
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
