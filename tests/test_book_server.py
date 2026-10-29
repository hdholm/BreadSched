"""The book server: one API over an open book for every interface (#354-#357).

The same contract runs against the in-process server and against the HTTP client
talking to a running web server's ``/api/v1``: each observes every change to its
book, refuses a bad write without changing anything, and reports failures in one
error envelope. The command line and the browser give the same JSON.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

from breadsched.api import ApiError, Change, LocalBookServer, open_book_server, to_json
from breadsched.api.http_client import HttpBookClient
from breadsched.api.views import change_json
from breadsched.cli.main import main
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.lib import (
    Money,
    PeriodType,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    Transaction,
)
from breadsched.gen.lib.amount import Amount
from breadsched.gen.sample_book import create_sample_book
from breadsched.gen.services import (
    DueDecision,
    DuplicateSchedule,
    FixedScheduleInput,
    FormulaScheduleInput,
    PostDue,
    ResolveDue,
    ResolveNext,
    SaveFixedSchedule,
    SaveTransaction,
    ServiceError,
    SetAdvanceDays,
    TransactionInput,
    TransactionSplitInput,
)
from breadsched.web.server import serve

TODAY = date(2026, 10, 1)


def _add_rent(db, book) -> None:
    sched = ScheduledTransaction(
        name="Rent",
        recurrence=Recurrence(period=PeriodType.MONTH, interval=1, start=date(2026, 10, 6)),
        splits=[
            ScheduledSplit(book.rent, Money("1800.00")),
            ScheduledSplit(book.checking, Money("-1800.00")),
        ],
    )
    with db.transaction("Add rent") as txn:
        db.add_scheduled(sched, txn)


def _serve(db):
    httpd = serve(db, host="127.0.0.1", port=0)
    httpd.RequestHandlerClass.EVENT_KEEPALIVE = 0.2
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}"


@pytest.fixture(params=["in-process", "http"])
def target(request, db, book):
    """The book API over a small book with one rent schedule, either way."""
    _add_rent(db, book)
    if request.param == "in-process":
        server = LocalBookServer(db)
        yield SimpleNamespace(api=server, db=db, remote=False)
        server.close()
        return
    httpd, base = _serve(db)
    try:
        yield SimpleNamespace(
            # A program on this computer, as the server record makes it (#358).
            api=HttpBookClient(base, httpd.token, local_token=httpd.local_token),
            db=db,
            remote=True,
            base=base,
            token=httpd.token,
        )
    finally:
        httpd.shutdown()
        httpd.server_close()


def _snapshot(db):
    return (
        sorted(txn.handle for txn in db.iter_transactions()),
        sorted(repr(item.serialize()) for item in db.iter_scheduled()),
    )


def _until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for change events")
        time.sleep(0.02)


def _follow(api) -> tuple[list[Change], object]:
    seen: list[Change] = []
    stop = api.subscribe(seen.append)
    return seen, stop


def test_a_write_is_one_change_every_subscriber_sees(target):
    api = target.api
    start = api.revision
    seen, stop = _follow(api)
    [sched] = target.db.iter_scheduled()

    outcome = api.resolve_next(ResolveNext(sched.handle, DueDecision.POST, as_of=TODAY))

    assert outcome.posted == 1 and len(outcome.transactions) == 1
    assert api.revision == start + 1
    _until(lambda: len(seen) == 1)
    stop()
    [change] = seen
    assert change.revision == start + 1
    assert {"transaction", "scheduled"} <= change.kinds
    assert change.label == "Enter next occurrence of Rent"
    assert change_json(change)["kinds"] == sorted(change.kinds)
    # It names the transactions it wrote, so a register rereads only those (#319).
    assert change.transactions == frozenset(outcome.transactions)
    # And whether a step is left to undo or redo, for the window's buttons.
    assert (change.can_undo, change.can_redo) == (True, False)
    assert change_json(change)["can_undo"] is True


def test_undo_redo_and_writes_from_other_code_are_changes_too(target, book):
    api = target.api
    start = api.revision
    seen, stop = _follow(api)
    [sched] = target.db.iter_scheduled()
    api.resolve_next(ResolveNext(sched.handle, DueDecision.SKIP, as_of=TODAY))
    assert api.undo() is True
    assert api.redo() is True
    with target.db.transaction("Elsewhere") as txn:
        target.db.add_transaction(
            Transaction.simple(TODAY, "Shop", book.groceries, book.checking, "12.00"), txn
        )
    _until(lambda: len(seen) == 4)
    stop()
    assert [change.revision - start for change in seen] == [1, 2, 3, 4]
    # After the undo, a step waits to be redone; the redo leaves none (and the book's
    # earlier steps can still be undone throughout).
    assert [(change.can_undo, change.can_redo) for change in seen[1:3]] == [
        (True, True),
        (True, False),
    ]
    assert seen[-1].kinds == frozenset({"transaction"})
    assert seen[-1].transactions == frozenset(
        item.handle for item in target.db.iter_transactions() if item.description == "Shop"
    )
    assert api.revision == start + 4


def test_a_refused_write_changes_nothing_and_notifies_no_one(target):
    api = target.api
    start = api.revision
    seen, stop = _follow(api)
    [sched] = target.db.iter_scheduled()
    before = sched.serialize()

    with pytest.raises(ApiError) as caught:
        api.set_advance_days(SetAdvanceDays(sched.handle, remind_days=1000))
    with pytest.raises(ApiError) as stale:
        api.resolve_due(ResolveDue(((sched.handle, TODAY, DueDecision.POST),), TODAY))
    with pytest.raises(ApiError) as deferred:
        api.resolve_next(ResolveNext(sched.handle, DueDecision.DEFER))

    assert (caught.value.status, caught.value.code) == (400, "schedule.remind_days.invalid")
    assert "0 to 999" in str(caught.value)
    assert (stale.value.status, stale.value.code) == (400, "schedule.due.not_pending")
    assert deferred.value.status == 400
    assert target.db.get_scheduled(sched.handle).serialize() == before
    time.sleep(0.3)
    stop()
    assert seen == [] and api.revision == start


def test_schedules_are_found_by_name_prefix_or_handle(target):
    api = target.api
    [sched] = target.db.iter_scheduled()
    assert api.find_schedule("rent").handle == sched.handle
    assert api.find_schedule(sched.handle[:6]).serialize() == sched.serialize()
    with pytest.raises(ApiError) as missing:
        api.find_schedule("Gym")
    assert (missing.value.status, missing.value.code) == (404, "schedule.not_found")
    assert str(missing.value) == "no schedule matches 'Gym'"
    twin = ScheduledTransaction.from_dict({**sched.serialize(), "handle": "other"})
    with target.db.transaction("Twin") as txn:
        target.db.add_scheduled(twin, txn)
    with pytest.raises(ApiError) as ambiguous:
        api.find_schedule("Rent")
    assert (ambiguous.value.status, ambiguous.value.code) == (400, "schedule.reference.ambiguous")


def test_due_review_and_next_occurrence_read_alike(target):
    api = target.api
    [sched] = target.db.iter_scheduled()
    api.set_advance_days(SetAdvanceDays(sched.handle, remind_days=7))
    [review] = api.due_review(TODAY)
    assert review.schedule == sched.handle and review.total == Money("1800.00")
    assert [(item.when, item.stage) for item in review.items] == [(date(2026, 10, 6), "reminder")]
    upcoming = api.next_occurrence(sched.handle, TODAY)
    assert upcoming is not None and (upcoming.when, upcoming.amount) == (
        date(2026, 10, 6),
        Money("1800.00"),
    )
    assert api.next_occurrence("missing") is None
    outcome = api.resolve_due(
        ResolveDue(((sched.handle, date(2026, 10, 6), DueDecision.DEFER),), TODAY)
    )
    assert (outcome.posted, outcome.deferred) == (0, 1)


def test_advance_days_read_and_write_alike(target):
    api = target.api
    [sched] = target.db.iter_scheduled()
    saved = api.set_advance_days(SetAdvanceDays(sched.handle, advance_days=2, remind_days=9))
    assert (saved.advance_days, saved.remind_days) == (2, 9)
    assert api.advance_days(sched.handle) == saved
    kept = api.set_advance_days(SetAdvanceDays(sched.handle, remind_days=4))
    assert (kept.advance_days, kept.remind_days) == (2, 4)
    with pytest.raises(ApiError) as gone:
        api.advance_days("missing")
    assert gone.value.status == 404


def test_duplicating_and_deleting_a_schedule_alike(target):
    api = target.api
    [sched] = target.db.iter_scheduled()
    start = api.revision

    copy = api.duplicate_schedule(DuplicateSchedule(sched.handle, name="Rent (flat 2)"))
    assert copy.name == "Rent (flat 2)" and copy.handle != sched.handle

    def legs(item):
        return [(split.account, split.amount) for split in item.splits]

    assert legs(target.db.get_scheduled(copy.handle)) == legs(sched)
    deleted = api.delete_schedule(copy.handle)
    assert (deleted.handle, deleted.name) == (copy.handle, "Rent (flat 2)")
    assert target.db.get_scheduled(copy.handle) is None
    assert api.revision == start + 2

    before = _snapshot(target.db)
    for call, code, status in (
        (lambda: api.delete_schedule("missing"), "schedule.not_found", 404),
        (lambda: api.duplicate_schedule(DuplicateSchedule("missing")), "schedule.not_found", 404),
        (
            lambda: api.duplicate_schedule(DuplicateSchedule(sched.handle, name="  ")),
            "schedule.name.required",
            400,
        ),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert (refused.value.code, refused.value.status) == (code, status)
    assert _snapshot(target.db) == before


def test_posting_due_without_a_review_alike(target, book):
    api = target.api
    [manual] = target.db.iter_scheduled()
    automatic = ScheduledTransaction.from_dict({**manual.serialize(), "handle": "auto"})
    automatic.name = "Gym"
    automatic.auto_create = True
    with target.db.transaction("Automatic") as txn:
        target.db.add_scheduled(automatic, txn)
    october_7 = date(2026, 10, 7)

    only_automatic = api.post_due(PostDue(as_of=october_7))
    assert [(item.schedule, item.when) for item in only_automatic.posted] == [
        ("auto", date(2026, 10, 6))
    ]
    everything = api.post_due(PostDue(as_of=october_7, only_automatic=False))
    assert [(item.schedule, item.description) for item in everything.posted] == [
        (manual.handle, "Rent")
    ]
    posted = target.db.get_transaction(everything.posted[0].transaction)
    assert posted.post_date == date(2026, 10, 6) and posted.scheduled_from == manual.handle
    assert api.post_due(PostDue(as_of=october_7, only_automatic=False)).posted == ()


def _fixed(book, name="Gym", **overrides):
    values = dict(
        name=name,
        recurrence=Recurrence(period=PeriodType.MONTH, interval=1, start=date(2026, 10, 9)),
        category=book.groceries,
        funding=book.checking,
        amount=Money("45.00"),
    )
    values.update(overrides)
    return FixedScheduleInput(**values)


def test_saving_a_fixed_schedule_from_an_editor_alike(target, book):
    api = target.api
    start = api.revision
    created = api.save_fixed_schedule(SaveFixedSchedule(_fixed(book, remind_days=5)))
    stored = target.db.get_scheduled(created.handle)
    assert (stored.name, stored.amount(), stored.remind_days) == ("Gym", Money("45.00"), 5)

    edited = api.save_fixed_schedule(
        SaveFixedSchedule(
            _fixed(book, name="Gym (family)", amount=Money("60.00")),
            existing_handle=created.handle,
        )
    )
    assert edited.handle == created.handle
    stored = target.db.get_scheduled(created.handle)
    assert (stored.name, stored.amount(), stored.remind_days) == ("Gym (family)", Money("60.00"), 5)
    assert api.revision == start + 2

    before = _snapshot(target.db)
    for request, code in (
        (SaveFixedSchedule(_fixed(book, category="missing")), "schedule.account.not_found"),
        (SaveFixedSchedule(_fixed(book), existing_handle="missing"), "schedule.not_found"),
        (SaveFixedSchedule(_fixed(book, advance_days=1000)), "schedule.advance_days.invalid"),
    ):
        with pytest.raises(ApiError) as refused:
            api.save_fixed_schedule(request)
        assert refused.value.code == code
    assert _snapshot(target.db) == before


def test_saving_a_formula_schedule_from_an_editor_alike(target, book):
    api = target.api
    loan = ScheduledTransaction(
        name="Formula loan",
        recurrence=Recurrence(period=PeriodType.MONTH, start=date(2026, 1, 1)),
        splits=[
            ScheduledSplit(book.utilities, formula="payment"),
            ScheduledSplit(book.checking, formula="-payment"),
        ],
    )
    loan.variables = {"payment": "100"}
    with target.db.transaction("Formula fixture") as txn:
        target.db.add_scheduled(loan, txn)

    api.save_formula_schedule(
        FormulaScheduleInput(
            existing_handle=loan.handle,
            name="Adjusted loan",
            recurrence=loan.recurrence,
            formulas={0: "payment + 25", 1: "-(payment + 25)"},
            variables={"payment": "100"},
            enabled=True,
            auto_create=False,
            placeholder=False,
            growth_policy=loan.growth_policy,
        )
    )
    stored = target.db.get_scheduled(loan.handle)
    assert stored.name == "Adjusted loan"
    assert [split.formula for split in stored.splits] == ["payment + 25", "-(payment + 25)"]


def _transaction(book, description="Shop", amount="12.00", **overrides):
    values = dict(
        post_date=date(2026, 10, 2),
        description=description,
        currency=book.currency,
        splits=(
            TransactionSplitInput(
                book.groceries, Amount(Money(amount), book.currency), memo="milk"
            ),
            TransactionSplitInput(book.checking, Amount(-Money(amount), book.currency)),
        ),
        tags=("food",),
    )
    values.update(overrides)
    return TransactionInput(**values)


def test_saving_and_deleting_a_transaction_alike(target, book):
    from breadsched.gen.services import transaction_currency

    book.currency = transaction_currency(target.db)
    api = target.api
    start = api.revision
    added = api.save_transaction(SaveTransaction(_transaction(book)))
    stored = target.db.get_transaction(added.handle)
    assert (added.description, added.post_date) == ("Shop", date(2026, 10, 2))
    assert stored.tags == ["food"] and stored.splits[0].memo == "milk"

    edited = api.save_transaction(
        SaveTransaction(
            _transaction(book, description="Corner shop", amount="15.50"),
            existing_handle=added.handle,
            source=stored,
        )
    )
    assert edited.handle == added.handle
    stored = target.db.get_transaction(added.handle)
    assert stored.description == "Corner shop"
    assert sorted(split.value for split in stored.splits) == [Money("-15.50"), Money("15.50")]

    removed = api.delete_transaction(added.handle)
    assert removed.description == "Corner shop"
    assert target.db.get_transaction(added.handle) is None
    assert api.revision == start + 3

    before = _snapshot(target.db)
    unbalanced = _transaction(
        book,
        splits=(
            TransactionSplitInput(book.groceries, Amount(Money("5"), book.currency)),
            TransactionSplitInput(book.checking, Amount(Money("-4"), book.currency)),
        ),
    )
    for call, status in (
        (lambda: api.save_transaction(SaveTransaction(unbalanced)), 400),
        (lambda: api.save_transaction(SaveTransaction(_transaction(book), "missing")), 404),
        (lambda: api.delete_transaction("missing"), 404),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert refused.value.status == status
    assert _snapshot(target.db) == before


def _post(db, handle, when, description, debit, credit, amount, tags=()):
    transaction = Transaction.simple(when, description, debit, credit, amount)
    transaction.handle = handle
    transaction.tags = list(tags)
    with db.transaction(f"Add {description}") as txn:
        db.add_transaction(transaction, txn)


def test_accounts_and_transactions_are_found_alike(target, book):
    from breadsched.gen.services import transaction_currency

    api, db = target.api, target.db
    _post(db, "ab01" + "0" * 28, date(2026, 10, 2), "Shop", book.groceries, book.checking, "12")
    _post(db, "ab02" + "0" * 28, date(2026, 10, 3), "Pay", book.checking, book.salary, "900")

    assert api.transaction_currency() == transaction_currency(db)
    for reference in ("Assets:Checking", "Checking", book.checking):
        found = api.find_account(reference)
        assert (found.handle, found.full_name, found.type) == (
            book.checking,
            "Assets:Checking",
            "BANK",
        )
    stored = db.get_transaction("ab01" + "0" * 28)
    assert api.find_transaction("ab01").serialize() == stored.serialize()

    editable = api.editable_transaction(stored.handle)
    assert (editable.post_date, editable.description) == (date(2026, 10, 2), "Shop")
    assert [(split.account, split.value.value) for split in editable.splits] == [
        (book.groceries, Money("12")),
        (book.checking, Money("-12")),
    ]
    assert editable.splits[0].handle == stored.splits[0].handle

    for call, status, code, wording in (
        (lambda: api.find_account("Nowhere"), 404, "account.not_found", "'Nowhere'"),
        (lambda: api.find_transaction("ff"), 404, "transaction.not_found", "'ff'"),
        (lambda: api.find_transaction("ab0"), 400, "transaction.reference.ambiguous", "more"),
        (lambda: api.editable_transaction("missing"), 404, "transaction.not_found", ""),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert (refused.value.status, refused.value.code) == (status, code)
        assert wording in str(refused.value)


def test_registers_and_balances_read_alike(target, book):
    from breadsched.gen.services import BalanceQuery, RegisterQuery

    api, db = target.api, target.db
    _post(db, "cd01" + "0" * 28, date(2026, 10, 2), "Shop", book.groceries, book.checking, "12")
    _post(db, "cd02" + "0" * 28, date(2026, 10, 3), "Pay", book.checking, book.salary, "900.5")
    _post(
        db,
        "cd03" + "0" * 28,
        date(2026, 10, 9),
        "Market",
        book.groceries,
        book.checking,
        "30.25",
        tags=("Food",),
    )

    page = api.register(RegisterQuery(book.checking))
    assert page.account.full_name == "Assets:Checking"
    assert [(line.description, line.amount, line.balance) for line in page.lines] == [
        ("Shop", Money("-12"), Money("-12")),
        ("Pay", Money("900.5"), Money("888.5")),
        ("Market", Money("-30.25"), Money("858.25")),
    ]
    assert page.lines[0].transfer == "Expenses:Groceries"
    # Filtering keeps each line's running balance.
    tagged = api.register(RegisterQuery(book.checking, tag="food"))
    assert [(line.description, line.balance) for line in tagged.lines] == [
        ("Market", Money("858.25"))
    ]
    last = api.register(RegisterQuery(book.checking, limit=1, end=date(2026, 10, 5)))
    assert [line.description for line in last.lines] == ["Pay"]

    balance = api.account_balance(BalanceQuery(book.checking, as_of=date(2026, 10, 5)))
    assert (balance.account.handle, balance.amount) == (book.checking, Money("888.5"))
    expenses = api.account_balance(BalanceQuery(book.expenses, recursive=True))
    assert expenses.amount == Money("42.25") and expenses.recursive
    assert api.account_balance(BalanceQuery(book.expenses)).amount == Money(0)

    totals = api.balance_summary()
    assert totals.cash == Money("858.25")
    assert totals.cash_missing_quotes == ()
    # The Accounts summary's totals travel too, as the engine values them (#358).
    from breadsched.gen.engine import valuation
    from breadsched.gen.lib.account import AccountClass

    for kind, total in (
        (AccountClass.ASSET, totals.assets),
        (AccountClass.LIABILITY, totals.liabilities),
    ):
        valued = valuation.aggregate_value(
            db, accounts=[a for a in db.iter_accounts() if a.account_class == kind]
        )
        assert total == (valued.amount.value if valued.amount is not None else None)
    assert totals.assets == Money("858.25")

    for call, code in (
        (lambda: api.register(RegisterQuery("missing")), "account.not_found"),
        (lambda: api.account_balance(BalanceQuery("missing")), "account.not_found"),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert (refused.value.status, refused.value.code) == (404, code)
    with pytest.raises(ApiError) as refused:
        api.register(RegisterQuery(book.checking, limit=-1))
    assert refused.value.status == 400


def test_accounts_read_and_save_alike(target, book):
    from breadsched.gen.lib import Account, AccountType
    from breadsched.gen.services import SaveAccount

    api, db = target.api, target.db
    records = api.accounts()
    assert [record.full_name for record in records] == sorted(r.full_name for r in records)
    checking = api.account(book.checking)
    assert checking.full_name == "Assets:Checking"
    assert checking.account.serialize() == db.get_account(book.checking).serialize()

    chart = api.chart()
    assert [line.record.full_name for line in chart][:2] == ["Assets", "Assets:Brokerage"]
    assert [line.depth for line in chart][:2] == [0, 1]
    line = next(line for line in chart if line.record.account.handle == book.checking)
    assert line.balance == Money(0) and line.book_balance == Money(0)
    assert (line.quote_evidence, line.missing_quotes, line.quote_date) == ("", (), None)

    start = api.revision
    added = api.save_account(
        SaveAccount(
            Account(name=" Travel ", atype=AccountType.EXPENSE, parent=book.expenses),
        )
    )
    assert added.name == "Travel"
    assert api.account(added.handle).full_name == "Expenses:Travel"
    stored = api.account(added.handle).account
    source = type(stored).from_dict(stored.serialize())
    stored.name = "Holidays"
    api.save_account(SaveAccount(stored, existing_handle=stored.handle, source=source))
    assert db.get_account(added.handle).name == "Holidays"
    assert api.delete_account(added.handle).name == "Holidays"
    assert db.get_account(added.handle) is None
    assert api.revision == start + 3

    before = sorted(repr(a.serialize()) for a in db.iter_accounts())
    for call, status, code in (
        (lambda: api.account("missing"), 404, "account.not_found"),
        (lambda: api.delete_account("missing"), 404, "account.not_found"),
        (lambda: api.delete_account(book.root), 400, "account.root.protected"),
        (
            lambda: api.save_account(
                SaveAccount(Account(name="", atype=AccountType.EXPENSE, parent=book.expenses))
            ),
            400,
            None,
        ),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert refused.value.status == status
        assert code is None or refused.value.code == code
    assert sorted(repr(a.serialize()) for a in db.iter_accounts()) == before


def test_the_chart_and_totals_carry_quote_evidence_alike(target, book):
    """A foreign balance is never shown as converted without its quote (AGENTS 10)."""
    from breadsched.gen.lib import Account, AccountType, Commodity, CommodityPrice, Split
    from breadsched.gen.services import transaction_currency

    api, db = target.api, target.db
    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    account = Account(
        name="Foreign cash", atype=AccountType.BANK, parent=book.assets, commodity=euro.handle
    )
    transaction = Transaction(post_date=date(2026, 1, 2), description="Opening cash")
    transaction.currency = euro.handle
    transaction.splits = [Split(account.handle, Money(10)), Split(book.opening, Money(-10))]
    with db.transaction("Foreign cash") as txn:
        db.add_commodity(euro, txn)
        db.add_account(account, txn)
        db.add_transaction(transaction, txn)

    def foreign():
        return next(line for line in api.chart() if line.record.account.handle == account.handle)

    missing = foreign()
    assert (missing.balance, missing.missing_quotes) == (None, (account.handle,))
    assert api.balance_summary().cash_missing_quotes == (account.handle,)
    assert api.balance_summary().cash is None

    with db.transaction("Quote") as txn:
        db.add_price(
            CommodityPrice(
                commodity=transaction_currency(db),
                currency=euro.handle,
                quote_date=date(2026, 1, 3),
                value=Money("0.5"),
                source="sample-source",
            ),
            txn,
        )
    quoted = foreign()
    assert quoted.balance == Money("20")
    assert (quoted.quote_date, quoted.quote_source, quoted.conversion_path) == (
        date(2026, 1, 3),
        "sample-source",
        "inverse",
    )
    assert quoted.quote_evidence.endswith("sample-source · inverse rate")
    assert quoted.book_balance == Money(10)


def test_categorization_rules_alike(target, book, tmp_path):
    from breadsched.gen.services.categorization import AddRule
    from breadsched.gen.services.csv_import import CsvImportRequest, CsvMapping, import_csv

    api, db = target.api, target.db
    statement = tmp_path / "statement.csv"
    statement.write_text(
        "Date,Description,Amount\n"
        "2026-09-01,CORNER GROCER #1234,-42.10\n"
        "2026-09-03,City Power,-60.00\n",
        encoding="utf-8",
    )
    mapping = CsvMapping(date="Date", description="Description", amount="Amount")
    imported = import_csv(db, CsvImportRequest(str(statement), book.checking, mapping))
    assert imported.ok, imported.errors
    seen, stop = _follow(api)
    try:
        power = api.add_rule(AddRule(book.utilities, "CITY POWER"))
        grocer = api.add_rule(AddRule(book.groceries, "Corner Grocer 99", position=1))
        _until(lambda: len(seen) == 2)
    finally:
        stop()
    assert all(change.kinds == {"categorization-rule"} for change in seen)
    # The service refuses a second rule for one key, but a migrated book can hold
    # one; the later rule is then a conflict.
    stored = db.get_metadata("categorization_rules", [])
    with db.transaction("Rules from a migrated book") as txn:
        db.set_metadata(
            "categorization_rules",
            [*stored, {"handle": "late", "category": book.rent, "key": grocer.key}],
            txn,
        )

    view = api.categorization_rules()
    assert [(line.position, line.category_name) for line in view.rules] == [
        (1, "Expenses:Groceries"),
        (2, "Expenses:Utilities"),
        (3, "Expenses:Rent"),
    ]
    [power_line, grocer_line] = view.proposals
    # The amount is the placeholder split's: the expense side of the payment.
    assert (power_line.category_name, power_line.proposal.amount) == (
        "Expenses:Utilities",
        Money("60.00"),
    )
    assert (grocer_line.category_name, grocer_line.proposal.rule_position) == (
        "Expenses:Groceries",
        1,
    )
    assert [c.rule_position for c in grocer_line.proposal.conflicts] == [3]
    assert grocer_line.conflict_names == ("Expenses:Rent",)
    assert grocer_line.proposal.rule.handle == grocer.handle
    assert "Expenses:Groceries" in [choice.name for choice in view.categories]
    assert "Expenses" not in [choice.name for choice in view.categories]

    assert api.move_rule("late", 1).handle == "late"
    assert api.categorization_rules().proposals[1].category_name == "Expenses:Rent"
    assert api.delete_rule("late").category == book.rent

    before = _snapshot(db), db.get_metadata("categorization_rules", None)
    for call, status, code in (
        (lambda: api.add_rule(AddRule(book.groceries, "!!!")), 400, "rule.match.empty"),
        (lambda: api.add_rule(AddRule(book.expenses, "new shop")), 400, "rule.category.invalid"),
        (lambda: api.add_rule(AddRule(book.rent, "city power")), 400, "rule.match.duplicate"),
        (lambda: api.add_rule(AddRule(book.rent, "x", position=9)), 400, "rule.position.invalid"),
        (lambda: api.move_rule(power.handle, 0), 400, "rule.position.invalid"),
        (lambda: api.delete_rule("missing"), 404, "rule.not_found"),
        (
            lambda: api.apply_category_proposals(("missing",)),
            404,
            "rule.transaction.not_found",
        ),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert (refused.value.status, refused.value.code) == (status, code)
    assert (_snapshot(db), db.get_metadata("categorization_rules", None)) == before

    applied = api.apply_category_proposals()
    assert (applied.assigned, applied.unchanged) == (2, 0)
    assert api.categorization_rules().proposals == ()


def test_tags_read_and_change_alike(target, book):
    api, db = target.api, target.db
    _post(db, "ef01" + "0" * 28, date(2026, 10, 2), "Shop", book.groceries, book.checking, "5")
    _post(db, "ef02" + "0" * 28, date(2026, 10, 3), "Cafe", book.groceries, book.checking, "4")
    tagged = api.set_tags("ef01" + "0" * 28, ["Food ", " food", "Weekly  shop"])
    assert tagged.tags == ["Food", "Weekly shop"]
    # The book's first spelling of a tag is kept.
    assert api.set_tags("ef02" + "0" * 28, ["FOOD"]).tags == ["Food"]
    assert [(item.tag, item.transactions) for item in api.tag_counts()] == [
        ("Food", 2),
        ("Weekly shop", 1),
    ]
    found = api.transactions_with_tag("food")
    assert sorted(item.handle[:4] for item in found) == ["ef01", "ef02"]
    assert found[0].serialize() == db.get_transaction(found[0].handle).serialize()

    before = _snapshot(db), db.get_transaction("ef01" + "0" * 28).tags
    for call, status, code in (
        (lambda: api.set_tags("ef01" + "0" * 28, ["a,b"]), 400, "tag.invalid"),
        (lambda: api.set_tags("missing", ["x"]), 404, "transaction.not_found"),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert (refused.value.status, refused.value.code) == (status, code)
    assert (_snapshot(db), db.get_transaction("ef01" + "0" * 28).tags) == before


def test_savings_goals_alike(target, book):
    from breadsched.gen.lib import Scenario
    from breadsched.gen.services.savings_goals import (
        AllocateToGoal,
        SaveSavingsGoal,
        SetGoalOverride,
    )

    api, db = target.api, target.db
    lean = Scenario(name="Lean")
    with db.transaction("Scenario") as txn:
        db.add_scenario(lean, txn)
    start, by = date(2026, 1, 1), date(2026, 12, 31)
    goal = api.save_savings_goal(
        SaveSavingsGoal("Holiday", book.savings, Money("1200"), by, start, "Trip")
    )
    assert api.find_savings_goal("holiday").handle == goal.handle
    assert api.find_savings_goal(goal.handle[:6]).serialize() == goal.serialize()
    funded = api.allocate_to_goal(AllocateToGoal(goal.handle, Money("300.25"), start, "Gift"))
    assert funded.allocated(date.max) == Money("300.25")
    changed = api.set_goal_override(
        SetGoalOverride(lean.handle, goal.handle, target_amount=Money("900"))
    )
    assert changed.goal_overrides[goal.handle].target_amount == Money("900")

    view = api.savings_goals(date(2026, 6, 30))
    [item] = view.report.goals
    assert (item.goal.name, item.account_name, item.target) == (
        "Holiday",
        "Assets:Savings",
        Money("1200"),
    )
    assert item.allocated == Money("300.25") and item.status == "saving"
    assert view.report.set_aside == item.set_aside
    [line] = view.overrides[goal.handle]
    assert (line.scenario_name, line.override.target_amount) == ("Lean", Money("900"))
    assert "Assets:Savings" in [choice.name for choice in view.accounts]
    assert "Expenses:Groceries" in [choice.name for choice in view.purchase_accounts]
    assert [choice.name for choice in view.scenarios] == ["Lean"]

    closed = api.close_savings_goal(goal.handle, date(2026, 7, 1))
    assert closed.closed_on == date(2026, 7, 1)
    assert api.savings_goals(date(2026, 7, 2)).report.goals == ()
    assert len(api.savings_goals(date(2026, 7, 2), include_closed=True).report.goals) == 1
    assert api.reopen_savings_goal(goal.handle).closed_on is None

    before = _snapshot(db), sorted(repr(g.serialize()) for g in db.iter_savings_goals())
    for call, status, code in (
        (
            lambda: api.save_savings_goal(SaveSavingsGoal("", book.savings, Money("5"), by, start)),
            400,
            "savings_goal.name.required",
        ),
        (
            lambda: api.save_savings_goal(
                SaveSavingsGoal("Car", book.groceries, Money("5"), by, start)
            ),
            400,
            "savings_goal.account.invalid",
        ),
        (
            lambda: api.allocate_to_goal(AllocateToGoal(goal.handle, Money("5000"), start)),
            400,
            "savings_goal.allocation.exceeds_target",
        ),
        (lambda: api.reopen_savings_goal(goal.handle), 400, "savings_goal.open"),
        (lambda: api.find_savings_goal("nothing"), 404, "savings_goal.not_found"),
        (lambda: api.delete_savings_goal("missing"), 404, "savings_goal.not_found"),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert (refused.value.status, refused.value.code) == (status, code)
    assert (_snapshot(db), sorted(repr(g.serialize()) for g in db.iter_savings_goals())) == before

    assert api.delete_savings_goal(goal.handle) == goal.handle
    assert db.get_savings_goal(goal.handle) is None
    assert goal.handle not in db.get_scenario(lean.handle).goal_overrides


def test_reconciliation_alike(target, book):
    from breadsched.gen.lib.transaction import ReconcileState
    from breadsched.gen.services.reconciliations import (
        StartReconciliation,
        ToggleCleared,
        UpdateReconciliation,
    )

    api, db = target.api, target.db
    _post(db, "ab10" + "0" * 28, date(2026, 9, 2), "Pay", book.checking, book.salary, "500")
    _post(db, "ab11" + "0" * 28, date(2026, 9, 9), "Shop", book.groceries, book.checking, "120.5")
    pay = db.get_transaction("ab10" + "0" * 28)
    [pay_split] = [split for split in pay.splits if split.account == book.checking]

    assert api.toggle_cleared(ToggleCleared(pay.handle, pay_split.handle)) is ReconcileState.CLEARED
    empty = api.reconciliation(book.checking)
    assert (empty.account_name, empty.current, empty.history) == ("Assets:Checking", None, ())

    started = api.start_reconciliation(
        StartReconciliation(book.checking, date(2026, 9, 30), Money("379.5"))
    )
    session = started.reconciliation.handle
    # The cleared deposit starts checked; the purchase does not.
    assert [(c.description, c.selected) for c in started.candidates] == [
        ("Pay", True),
        ("Shop", False),
    ]
    assert started.difference == Money("-120.5") and not started.balanced
    view = api.reconciliation(book.checking)
    assert view.current is not None and view.current.reconciliation.handle == session
    assert [item.handle for item in view.history] == [session]

    splits = tuple(candidate.split for candidate in started.candidates)
    balanced = api.update_reconciliation(UpdateReconciliation(session, selected_splits=splits))
    assert balanced.balanced and balanced.selected_balance == Money("379.5")
    finished = api.complete_reconciliation(session)
    assert finished.status.value == "completed"
    assert api.reconciliation(book.checking).current is None
    reopened = api.reopen_reconciliation(session)
    assert reopened.status.value == "open"
    assert api.cancel_reconciliation(session).status.value == "cancelled"

    before = _snapshot(db), sorted(repr(r.serialize()) for r in db.iter_reconciliations())
    for call, status, code in (
        (lambda: api.reconciliation("missing"), 404, "reconciliation.account.not_found"),
        (
            lambda: api.start_reconciliation(
                StartReconciliation("missing", date(2026, 9, 30), Money(0))
            ),
            404,
            "reconciliation.account.not_found",
        ),
        (lambda: api.complete_reconciliation("missing"), 404, "reconciliation.not_found"),
        (lambda: api.reopen_reconciliation(session), 400, "reconciliation.status.not_completed"),
        (
            lambda: api.toggle_cleared(ToggleCleared("missing", "x")),
            404,
            "transaction.not_found",
        ),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert (refused.value.status, refused.value.code) == (status, code)
    assert (_snapshot(db), sorted(repr(r.serialize()) for r in db.iter_reconciliations())) == before


def test_statement_entries_and_starting_balances_alike(target, book):
    """#362: the reconcile window's starting balance and quick add, either way."""
    from breadsched.gen.services.reconciliations import (
        AddStatementEntry,
        StartReconciliation,
    )

    api, db = target.api, target.db
    _post(db, "ab20" + "0" * 28, date(2026, 9, 2), "Pay", book.checking, book.salary, "500")
    _post(db, "ab21" + "0" * 28, date(2026, 10, 9), "Later", book.groceries, book.checking, "20")
    assert api.reconciled_balance(book.checking, date(2026, 9, 30)) == Money(0)
    started = api.start_reconciliation(
        StartReconciliation(book.checking, date(2026, 9, 30), Money("498"))
    )
    # The later purchase is listed, marked, and not checked; each side says its column.
    assert [
        (c.description, c.after_statement, c.funds_in, c.selected) for c in started.candidates
    ] == [
        ("Pay", False, True, False),
        ("Later", True, False, False),
    ]
    saved = api.add_statement_entry(
        AddStatementEntry(book.checking, book.groceries, date(2026, 9, 30), "Fee", Money("-2"))
    )
    state = api.reconciliation(book.checking).current
    fee = next(c for c in state.candidates if c.transaction == saved.handle)
    assert fee.selected and not fee.funds_in
    assert (state.selected_in, state.selected_out) == (Money(0), Money(2))

    before = _snapshot(db)
    for call, status, code in (
        (
            lambda: api.reconciled_balance("missing", date(2026, 9, 30)),
            404,
            "reconciliation.account.not_found",
        ),
        (
            lambda: api.add_statement_entry(
                AddStatementEntry(book.checking, book.groceries, date(2026, 9, 30), "Fee", Money(0))
            ),
            400,
            "reconciliation.entry.amount",
        ),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert (refused.value.status, refused.value.code) == (status, code)
    assert _snapshot(db) == before


def test_receivables_alike(target, book):
    from breadsched.gen.lib import Scenario
    from breadsched.gen.services.receivables import RecordWriteOff, SaveReceivable
    from breadsched.gen.services.scenarios import SetReimbursementOverride

    api, db = target.api, target.db
    _post(db, "cc01" + "0" * 28, date(2026, 3, 2), "Clinic", book.groceries, book.checking, "80")
    _post(db, "cc02" + "0" * 28, date(2026, 3, 9), "Refund", book.checking, book.groceries, "30")
    lean = Scenario(name="Lean")
    with db.transaction("Scenario") as txn:
        db.add_scenario(lean, txn)
    clinic = db.get_transaction("cc01" + "0" * 28)
    [cost] = [split for split in clinic.splits if split.account == book.groceries]

    saved = api.save_receivable(
        SaveReceivable(
            date(2026, 3, 2),
            "Mom",
            "Clinic visit",
            expected_amount=Money("50"),
            expected_cash_date=date.today() + timedelta(days=60),
        )
    )
    assert api.find_receivable(saved.handle[:6]).payer == "Mom"
    costs, _credits = api.receivable_candidates(saved.handle)
    assert (clinic.handle, cost.handle) in [
        (c.candidate.transaction, c.candidate.split) for c in costs
    ]
    assert {line.account_name for line in costs} >= {"Expenses:Groceries"}
    api.link_receivable_split(saved.handle, clinic.handle, cost.handle, "expense")

    view = api.receivables_view(date(2026, 3, 31))
    [line] = view.receivables
    assert (line.summary.owed, line.summary.expense_total) == (Money("50"), Money("80"))
    assert [(link.description, link.account_name, link.value) for link in line.expenses] == [
        ("Clinic", "Expenses:Groceries", Money("80"))
    ]
    assert [name for _handle, name in view.scenarios] == ["Lean"]
    # Linking the first expense made the default Receivable account.
    assert [name for _handle, name in view.accounts] == ["Assets:Reimbursements Receivable"]
    assert line.account_name == "Assets:Reimbursements Receivable"
    [proposal] = view.proposals
    assert (proposal.proposal.amount, proposal.account_name) == (Money("30"), "Expenses:Groceries")
    assert api.reimbursement_proposals(date(2026, 3, 31)) == (proposal.proposal,)
    accepted = api.accept_reimbursements(
        ((saved.handle, proposal.proposal.transaction, proposal.proposal.split),)
    )
    assert (accepted.linked, accepted.unchanged) == (1, 0)

    assert (
        api.mark_disputed(saved.handle, date(2026, 4, 1), "Says paid").dispute_note == "Says paid"
    )
    assert api.clear_dispute(saved.handle).disputed_on is None
    written = api.record_write_off(RecordWriteOff(saved.handle, Money("5"), date(2026, 4, 2)))
    assert written.handle == saved.handle
    changed = api.set_reimbursement_override(
        SetReimbursementOverride(lean.handle, saved.handle, Money("0"))
    )
    assert changed.reimbursement_overrides[saved.handle].amount == Money("0")
    [line] = api.receivables_view(date(2026, 4, 30)).receivables
    assert line.summary.reimbursed == Money("30") and line.summary.written_off == Money("5")
    assert [change.scenario_name for change in line.scenario_changes] == ["Lean"]
    detached = api.detach_receivable_split(saved.handle, clinic.handle, cost.handle)
    assert detached.expenses == []

    before = _snapshot(db), sorted(repr(r.serialize()) for r in db.iter_receivables())
    for call, status, code in (
        (lambda: api.save_receivable(SaveReceivable(date(2026, 3, 2), " ")), 400, None),
        (lambda: api.find_receivable("zz"), 404, "receivable.not_found"),
        (
            lambda: api.link_receivable_split(saved.handle, clinic.handle, cost.handle, "gift"),
            400,
            None,
        ),
        (lambda: api.delete_receivable("missing"), 404, "receivable.not_found"),
        (
            lambda: api.record_write_off(
                RecordWriteOff(saved.handle, Money("-1"), date(2026, 4, 2))
            ),
            400,
            "receivable.write_off.amount_not_positive",
        ),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert refused.value.status == status
        assert code is None or refused.value.code == code
    assert (_snapshot(db), sorted(repr(r.serialize()) for r in db.iter_receivables())) == before
    assert api.delete_receivable(saved.handle) == saved.handle


def test_scenarios_and_assumptions_alike(target, book):
    from breadsched.gen.lib import Scenario
    from breadsched.gen.lib.money import Rate
    from breadsched.gen.lib.scenario import AssumptionPeriod, Assumptions
    from breadsched.gen.services.assumptions import SaveBaseAssumptions, SaveScenarioAssumptions
    from breadsched.gen.services.scenarios import (
        DuplicateScenario,
        SaveDrawdown,
        SaveScenario,
        SuppressScenarioSchedule,
    )

    api, db = target.api, target.db
    base = api.save_base_assumptions(SaveBaseAssumptions(Assumptions(income_growth="0.041")))
    assert api.base_assumptions().income_growth == base.income_growth == Rate("0.041")

    lean = Scenario(name=" Lean ", years=3)
    saved = api.save_scenario(SaveScenario(lean))
    assert saved.name == "Lean"
    stored = api.find_scenario("Lean")
    assert api.find_scenario(saved.handle).serialize() == stored.serialize()
    stored.years = 4
    api.save_scenario_assumptions(SaveScenarioAssumptions(stored, existing_handle=stored.handle))
    assert db.get_scenario(saved.handle).years == 4

    copy = api.duplicate_scenario(DuplicateScenario(api.find_scenario(saved.handle)))
    assert copy.name != "Lean" and {s.name for s in api.scenarios()} == {"Lean", copy.name}

    [rent] = db.iter_scheduled()
    api.suppress_scenario_schedule(SuppressScenarioSchedule(saved.handle, rent.handle))
    assert [o.source_schedule for o in db.get_scenario(saved.handle).schedule_overrides] == [
        rent.handle
    ]

    sources, targets = api.drawdown_accounts()
    assert book.brokerage in sources and book.checking in targets
    drawdown = api.save_drawdown(
        SaveDrawdown(saved.handle, book.brokerage, book.checking, date(2027, 1, 1), Money("12000"))
    )
    assert [d.handle for d in db.get_scenario(saved.handle).drawdowns] == [drawdown.handle]
    assert api.remove_drawdown(saved.handle, drawdown.handle).handle == drawdown.handle

    period = AssumptionPeriod(start=date(2027, 1, 1), expense_inflation=Rate("0.05"))
    api.save_assumption_period(saved.handle, period)
    assert len(db.get_scenario(saved.handle).assumption_periods) == 1
    api.delete_assumption_period(saved.handle, 0)
    assert db.get_scenario(saved.handle).assumption_periods == []

    before = sorted(repr(s.serialize()) for s in db.iter_scenarios())
    for call, status, code in (
        (lambda: api.save_scenario(SaveScenario(Scenario(name="Lean"))), 400, None),
        (lambda: api.find_scenario("Nowhere"), 404, "scenario.not_found"),
        (lambda: api.delete_scenario("missing"), 404, "scenario.not_found"),
        (lambda: api.delete_assumption_period(saved.handle, 3), 404, None),
        (
            lambda: api.save_drawdown(
                SaveDrawdown(saved.handle, book.checking, book.checking, date(2027, 1, 1))
            ),
            400,
            "scenario.drawdown.account",
        ),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert refused.value.status == status
        assert code is None or refused.value.code == code
    assert sorted(repr(s.serialize()) for s in db.iter_scenarios()) == before
    assert api.delete_scenario(copy.handle).name == copy.name


def test_payroll_alike(target, book):
    from breadsched.gen.engine.payroll import PayLegKind, PayrollLine, PayrollTemplate
    from breadsched.gen.lib import Account, AccountType
    from breadsched.gen.services.payroll import CreatePaycheck, PayChange, SavePayrollTemplate

    api, db = target.api, target.db
    with db.transaction("Payroll accounts") as txn:
        taxes = Account(name="Taxes", atype=AccountType.EXPENSE, parent=book.expenses)
        db.add_account(taxes, txn)
        federal = Account(name="Federal", atype=AccountType.EXPENSE, parent=taxes.handle)
        db.add_account(federal, txn)
    template = PayrollTemplate(
        " Acme ",
        book.salary,
        book.checking,
        Money("3000.00"),
        (PayrollLine(federal.handle, percent=Decimal("12")),),
    )
    saved = api.save_payroll_template(SavePayrollTemplate(template))
    assert saved.name == "Acme"
    view = api.payroll(date(2026, 1, 2))
    assert [item.name for item in view.templates] == ["Acme"]
    assert view.name(federal.handle) == "Expenses:Taxes:Federal"
    assert book.salary in {item.handle for item in view.income_accounts}
    assert federal.handle in {item.handle for item in view.line_accounts}

    start = date(2026, 1, 2)
    recurrence = Recurrence(PeriodType.WEEK, interval=2, start=start)
    created = api.create_paycheck(CreatePaycheck("acme", "Acme pay", recurrence))
    paycheck = api.paycheck(created.handle, start)
    assert (paycheck.gross, paycheck.net) == (Money("3000.00"), Money("2640.00"))
    assert created.handle in {item.schedule for item in api.payroll(start).paychecks}
    drafted = api.template_from_schedule(created.handle, "Copy")
    assert drafted.lines == (PayrollLine(federal.handle, percent=Decimal("12")),)

    change = PayChange(created.handle, date(2026, 3, 1), Money("3300.00"), amounts={})
    before = db.get_scheduled(created.handle).serialize()
    preview = api.preview_pay_change(change)
    assert preview.total(PayLegKind.NET) == Money("2904.00")
    assert db.get_scheduled(created.handle).serialize() == before
    applied = api.apply_pay_change(change)
    assert applied.as_dict() == preview.as_dict()
    assert db.get_scheduled(created.handle).serialize() != before

    stored = db.get_metadata("payroll_templates")
    for call, status, code in (
        (lambda: api.save_payroll_template(SavePayrollTemplate(template)), 400, None),
        (lambda: api.delete_payroll_template("Nobody"), 404, "payroll.template.not_found"),
        (lambda: api.paycheck(book.checking), 404, "schedule.not_found"),
        (
            lambda: api.create_paycheck(CreatePaycheck("Nobody", "x", recurrence)),
            404,
            "payroll.template.not_found",
        ),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert refused.value.status == status
        assert code is None or refused.value.code == code
    assert db.get_metadata("payroll_templates") == stored
    assert api.delete_payroll_template("ACME").name == "Acme"
    assert api.payroll().templates == ()


def test_fsa_claims_alike(target, book):
    from breadsched.gen.engine.fsa_claims import FsaClaimStatus
    from breadsched.gen.lib import Account, AccountType, FsaFundingYear
    from breadsched.gen.services.claims import (
        ClaimAllocationInput,
        ClaimInput,
        ClaimLinkInput,
        ClaimReportQuery,
        CloseClaim,
        ReopenClaim,
        SaveClaim,
    )

    api, db = target.api, target.db
    year = date(2026, 1, 1)
    fsa = Account(name="Health FSA", atype=AccountType.FSA, parent=book.assets)
    fsa.fsa.years = [FsaFundingYear(year, date(2026, 12, 31), Money("1500.00"))]
    medical = Account(name="Medical", atype=AccountType.EXPENSE, parent=book.expenses)
    with db.transaction("FSA") as txn:
        db.add_account(fsa, txn)
        db.add_account(medical, txn)
    paid = Transaction.simple(date(2026, 3, 8), "Dentist", medical.handle, book.card, "200")
    back = Transaction.simple(
        date(2026, 3, 25), "Dentist claim paid", book.checking, fsa.handle, "200"
    )
    with db.transaction("Dentist") as txn:
        db.add_transaction(paid, txn)
        db.add_transaction(back, txn)
    [cost] = [split for split in paid.splits if split.account == medical.handle]

    saved = api.save_fsa_claim(
        SaveClaim(
            ClaimInput(
                date(2026, 3, 8),
                provider="Dentist",
                eob_responsibility=Money("200"),
                payments=(ClaimLinkInput(paid.handle, cost.handle),),
                allocations=(ClaimAllocationInput(fsa.handle, year),),
            )
        )
    )
    view = api.fsa_claims(date(2026, 4, 1))
    [summary] = view.claims
    assert (summary.claim.handle, summary.paid) == (saved.handle, Money("200.00"))
    assert view.name(fsa.handle) == "Assets:Health FSA"
    assert paid.handle in {item.transaction for item in view.candidates.payments}
    assert back.handle in {item.transaction for item in view.candidates.reimbursements}
    # A claim's standing, as the desktop editor shows it (#358).
    standing = api.fsa_claim_standing(saved.handle)
    assert standing.problem == "" and standing.summary is not None
    assert standing.summary.paid == Money("200.00")
    with pytest.raises(ApiError) as refused:
        api.fsa_claim_standing("no-such-claim")
    assert (refused.value.status, refused.value.code) == (404, "claim.not_found")
    assert [item.handle for item in view.candidates.fsa_accounts] == [fsa.handle]
    assert api.find_fsa_claim(saved.handle[:10]).provider == "Dentist"

    [proposal] = api.claim_link_proposals()
    assert (proposal.claim, proposal.transaction) == (saved.handle, back.handle)
    assert api.claim_link_proposals(book.savings) == ()
    accepted = api.accept_claim_links(((proposal.claim, proposal.transaction, proposal.split),))
    assert (accepted.linked, accepted.unchanged) == (1, 0)
    report = api.fsa_claim_report(ClaimReportQuery(as_of=date(2026, 4, 1), by="provider"))
    assert [group.label for group in report.groups] == ["Dentist"]
    assert report.lines[0].summary.reimbursed == Money("200.00")
    assert api.fsa_claim_report(ClaimReportQuery(status=FsaClaimStatus.OPEN)).lines == ()
    years = api.benefit_years(date(2026, 4, 1))
    assert [years.name(item.account.handle) for item in years.years] == ["Assets:Health FSA"]

    api.close_fsa_claim(CloseClaim(saved.handle, date(2026, 4, 2), "done"))
    assert db.get_fsa_claim(saved.handle).closed_on == date(2026, 4, 2)
    api.reopen_fsa_claim(ReopenClaim(saved.handle, date(2026, 4, 3), "again"))
    assert db.get_fsa_claim(saved.handle).closed_on is None

    before = db.get_fsa_claim(saved.handle).serialize()
    for call, status, code in (
        (lambda: api.find_fsa_claim("nothing"), 404, "claim.not_found"),
        (lambda: api.delete_fsa_claim("missing"), 404, "claim.not_found"),
        (lambda: api.close_fsa_claim(CloseClaim("missing", date(2026, 4, 2))), 404, None),
        (lambda: api.fsa_claim_report(ClaimReportQuery(by="colour")), 400, None),
        (
            lambda: api.save_fsa_claim(
                SaveClaim(ClaimInput(date(2026, 3, 8)), existing_handle="missing")
            ),
            404,
            "claim.not_found",
        ),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert refused.value.status == status
        assert code is None or refused.value.code == code
    assert db.get_fsa_claim(saved.handle).serialize() == before
    assert api.delete_fsa_claim(saved.handle).handle == saved.handle
    assert db.get_fsa_claim(saved.handle) is None


def test_plan_review_alike(target, book):
    from breadsched.gen.lib.transaction import PlanningResolution
    from breadsched.gen.services.review import ReviewOccurrence

    api, db = target.api, target.db

    def actual(when, amount):
        found = Transaction.simple(when, "RENT CO", book.rent, book.checking, amount)
        found.planning_resolution = PlanningResolution.UNRESOLVED
        with db.transaction("Import") as txn:
            db.add_transaction(found, txn)
        return found

    first, second = actual(date(2026, 10, 7), "1800.00"), actual(date(2026, 11, 9), "1850.00")
    assert [item.handle for item in api.review_queue()] == [first.handle, second.handle]
    assert api.review_queue()[0].amount == Money("1800.00")
    detail = api.review_detail(first.handle)
    [explained] = detail.candidates[:1]
    key = explained.candidate.event.key
    assert explained.candidate.event.planned_date == date(2026, 10, 6)
    assert detail.no_candidate_reason is None and detail.claims == ()
    assert [(role.role, role.account_name) for role in detail.roles] == [
        ("payment", "Expenses:Rent")
    ]
    assert api.plan_occurrence(key).expected_amount == Money("1800.00")
    events = api.unresolved_occurrences(date(2026, 10, 1), date(2026, 11, 30))
    assert key in {event.key for event in events}
    assert key in {item.event.key for item in api.match_candidates(first.handle, 3)}

    matched = api.match_review(ReviewOccurrence(first.handle, key))
    assert (matched.transaction, matched.occurrence) == (first.handle, key)
    assert db.get_transaction(first.handle).planning_resolution is PlanningResolution.MATCHED
    assert [item.handle for item in api.review_queue()] == [second.handle]
    november = api.review_detail(second.handle).candidates[0].candidate.event.key
    rejected = api.reject_review(ReviewOccurrence(second.handle, november))
    assert rejected.occurrence == november
    assert november in db.get_transaction(second.handle).rejected_plan_occurrences
    unexpected = api.mark_review_unexpected(second.handle)
    assert unexpected.resolution is PlanningResolution.UNEXPECTED
    assert api.review_queue() == ()

    before = _snapshot(db)
    for call, status, code in (
        (lambda: api.review_detail(first.handle), 400, "review.transaction.not_unresolved"),
        (lambda: api.review_detail("missing"), 404, "review.transaction.not_found"),
        (lambda: api.plan_occurrence("nothing"), 404, "review.occurrence.not_found"),
        (lambda: api.match_review(ReviewOccurrence(first.handle, key)), 400, None),
        (
            lambda: api.unresolved_occurrences(date(2026, 11, 1), date(2026, 10, 1)),
            400,
            "review.range.invalid",
        ),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert refused.value.status == status
        assert code is None or refused.value.code == code
    assert _snapshot(db) == before


def test_loans_alike(target, book):
    from decimal import Decimal

    from breadsched.gen.engine.loans import LoanTerms
    from breadsched.gen.services.loans import SaveLoan

    api, db = target.api, target.db
    choices = api.loan_choices()
    assert book.card in {item.handle for item in choices.liabilities}
    assert book.checking in {item.handle for item in choices.payment_accounts}
    # A hidden account is a choice only while hidden accounts are shown (#393).
    for hidden in (True, False):
        card = db.get_account(book.card)
        card.hidden = hidden
        with db.transaction("Hide or show the card") as txn:
            db.commit_account(card, txn)
        if hidden:
            assert book.card not in {item.handle for item in api.loan_choices().liabilities}
            shown = api.loan_choices(include_hidden=True)
            assert book.card in {item.handle for item in shown.liabilities}
    terms = LoanTerms(
        name=" Car ",
        principal=Money("12000"),
        annual_rate=Decimal("0.06"),
        years=5,
        start=date(2026, 11, 1),
        liability=book.card,
        interest_account=book.utilities,
        payment_account=book.checking,
        fraction=choices.fraction,
    )
    preview = api.preview_loan(terms)
    assert preview.payment == Money("231.99") and len(preview.rows) == 12
    assert preview.rows[0].period == 1 and preview.rows[0].interest == Money("60.00")
    before = _snapshot(db)
    from dataclasses import replace

    with pytest.raises(ApiError) as refused:
        api.save_loan(SaveLoan(replace(terms, payment_account=book.rent)))
    assert refused.value.status == 400
    with pytest.raises(ApiError):
        api.preview_loan(replace(terms, years=0))
    assert _snapshot(db) == before
    saved = api.save_loan(SaveLoan(terms, opening_balance=False))
    assert (saved.name, saved.payment) == ("Car", Money("231.99"))
    assert db.get_scheduled(saved.handle).name == "Car"


def test_exchange_rates_alike(target, book):
    from breadsched.gen.lib import Commodity
    from breadsched.gen.services.exchange_rates import SaveExchangeRate

    api, db = target.api, target.db
    usd = db.get_commodity_by_mnemonic("USD")
    if usd is None:
        usd = Commodity(namespace="CURRENCY", mnemonic="USD", fullname="US Dollar")
        with db.transaction("USD") as txn:
            db.add_commodity(usd, txn)
    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro")
    fund = Commodity(namespace="FUND", mnemonic="INDEX")
    with db.transaction("Currencies") as txn:
        db.add_commodity(euro, txn)
        db.add_commodity(fund, txn)
    assert {"EUR", "USD"} <= {item.mnemonic for item in api.currencies()}
    assert api.find_currency("eur").handle == euro.handle
    assert api.find_currency(usd.handle).mnemonic == "USD"
    saved = api.save_exchange_rate(
        SaveExchangeRate(euro.handle, usd.handle, date(2026, 1, 2), Money("1.0825"))
    )
    assert (saved.value, saved.source) == (Money("1.0825"), "breadsched")
    again = api.save_exchange_rate(
        SaveExchangeRate(euro.handle, usd.handle, date(2026, 1, 2), Money("1.09"))
    )
    assert again.handle == saved.handle
    assert [price.value for price in db.iter_prices(commodity=euro.handle)] == [Money("1.09")]
    latest = api.latest_exchange_rate(euro.handle, usd.handle)
    assert latest is not None and latest.value == Money("1.09")
    assert api.latest_exchange_rate(euro.handle, "missing") is None

    before = sorted(repr(price.serialize()) for price in db.iter_prices())
    for call, status, code in (
        (lambda: api.find_currency("XYZ"), 404, "currency.not_found"),
        (lambda: api.find_currency(fund.handle), 400, "currency.not_currency"),
        (
            lambda: api.save_exchange_rate(
                SaveExchangeRate(euro.handle, euro.handle, date(2026, 1, 2), Money("1"))
            ),
            400,
            "rate.currency.same",
        ),
        (
            lambda: api.save_exchange_rate(
                SaveExchangeRate(fund.handle, usd.handle, date(2026, 1, 2), Money("1"))
            ),
            400,
            "rate.source.invalid",
        ),
        (
            lambda: api.save_exchange_rate(
                SaveExchangeRate(euro.handle, usd.handle, date(2026, 1, 2), Money("0"))
            ),
            400,
            "rate.value.invalid",
        ),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert (refused.value.status, refused.value.code) == (status, code)
    assert sorted(repr(price.serialize()) for price in db.iter_prices()) == before


def test_dashboard_settings_alike(target, book):
    from breadsched.gen.services import DashboardGroupInput, SaveDashboardConfig

    api, db = target.api, target.db
    first = api.dashboard_settings()
    assert first.liquidity_days == 30 and first.emergency_months == 6
    saved = api.save_dashboard_settings(
        SaveDashboardConfig(
            groups=(
                DashboardGroupInput("Cash", "liquid", (book.checking, book.savings)),
                DashboardGroupInput("Debt", "liability", (book.card,)),
            ),
            liquidity_days=14,
            emergency_months=999,
        )
    )
    assert [(group.name, group.accounts) for group in saved.groups] == [
        ("Cash", (book.checking, book.savings)),
        ("Debt", (book.card,)),
    ]
    assert (saved.liquidity_days, saved.emergency_months) == (14, 36)
    # Each person's own arrangement (decision 0004): the book's accounts keep theirs,
    # and the accounts as this caller reads them show this one.
    assert db.get_account(book.checking).group == ""
    assert api.account(book.checking).account.group == "Cash"
    assert api.dashboard_settings() == saved
    # Sections travel with the settings and the board (#381).
    assert saved.sections["reimbursables"] is False
    shown = api.save_dashboard_settings(SaveDashboardConfig(sections={"reimbursables": True}))
    assert shown.sections["reimbursables"] is True and shown.groups == saved.groups
    assert api.dashboard().config.shows("reimbursables") is True

    before = (repr(db.get_metadata("dashboard")), db.get_account(book.card).group)
    for groups in (
        (DashboardGroupInput("Cash", "liquid", (book.checking,)), DashboardGroupInput("Cash")),
        (
            DashboardGroupInput("One", "asset", (book.checking,)),
            DashboardGroupInput("Two", "asset", (book.checking,)),
        ),
        (DashboardGroupInput("Odd", "colour"),),
    ):
        with pytest.raises(ApiError) as refused:
            api.save_dashboard_settings(SaveDashboardConfig(groups=groups))
        assert refused.value.status == 400
    assert (repr(db.get_metadata("dashboard")), db.get_account(book.card).group) == before
    assert api.dashboard_settings() == shown


def test_find_reads_alike(target, book):
    """#382 on the book server: one typed result, in process or over HTTP."""
    from breadsched.gen.engine.transaction_query import TransactionQuery
    from breadsched.gen.services.transaction_search import FindResult, find_transactions

    api, db = target.api, target.db
    with db.transaction("Shop") as txn:
        for day, amount in ((3, "67"), (9, "1067"), (20, "12.50")):
            db.add_transaction(
                Transaction.simple(
                    date(2026, 3, day), f"Shop {day}", book.groceries, book.checking, amount
                ),
                txn,
            )
    query = TransactionQuery(
        accounts=(book.expenses,), amount_min=Money("10"), group_by="month", sort="amount"
    )
    found = api.find(query)
    assert isinstance(found, FindResult)
    assert found == find_transactions(db, query).value
    assert [row.description for row in found.rows] == ["Shop 20", "Shop 3", "Shop 9"]
    assert found.splits == 3 and found.transactions == 3
    [march] = [group for group in found.groups if group.key == "2026-03"]
    assert march.totals == {"USD": Money("1146.50")}
    exact = api.find(TransactionQuery(amount=Money("67")))
    assert {row.description for row in exact.rows} == {"Shop 3"}
    before = db.summary()
    for bad, code in (
        (TransactionQuery(start=date(2026, 4, 1), end=date(2026, 3, 1)), "find.dates.order"),
        (TransactionQuery(amount_min=Money("5"), amount_max=Money("1")), "find.amounts.order"),
        (TransactionQuery(accounts=("nowhere",)), "find.account.missing"),
        (TransactionQuery(sort="colour"), "find.sort.invalid"),
    ):
        with pytest.raises(ApiError) as refused:
            api.find(bad)
        assert (refused.value.status, refused.value.code) == (400, code)
    assert db.summary() == before


def test_the_schedules_page_reads_alike(target, book):
    """#358: the browser's Schedules page reads one view through the server."""
    from breadsched.api.typed_codec import to_data
    from breadsched.gen.engine import schedule as engine
    from breadsched.gen.services.schedule_editing import schedules_view

    api, db = target.api, target.db
    view = api.schedules_view(days=400)
    assert to_data(view) == to_data(schedules_view(db, 400).value)
    [rent] = [item for item in view.definitions if item.schedule.name == "Rent"]
    assert rent.projection == engine.schedule_edit_projection(db, rent.schedule)
    assert view.next[rent.schedule.handle] == api.next_occurrence(rent.schedule.handle)
    assert view.names[book.rent] == db.full_name(db.get_account(book.rent))
    assert [line.when for line in view.upcoming] == [
        item.when for item in engine.upcoming_occurrences(db, horizon_days=400)
    ]
    assert {choice.handle for choice in view.accounts} == {
        account.handle
        for account in db.iter_accounts()
        if not account.is_root and not account.placeholder
    }
    assert api.schedule_editor(rent.schedule.handle) == rent
    # Any definition, saved or not, is laid out the same way.
    copy = ScheduledTransaction.from_dict(rent.schedule.serialize())
    assert api.schedule_layout(copy).projection == rent.projection
    # A transaction makes a monthly draft from a month later to review; nothing is saved.
    with db.transaction("Pay") as txn:
        paid = Transaction.simple(date(2026, 9, 1), "Gym", book.rent, book.checking, "40")
        db.add_transaction(paid, txn)
    before = len(list(db.iter_scheduled()))
    draft = api.schedule_draft(paid.handle)
    assert draft.schedule.name == "Gym" and draft.schedule.recurrence.start == date(2026, 10, 1)
    assert len(list(db.iter_scheduled())) == before
    for call, status, code in (
        (lambda: api.schedule_editor("no-such-schedule"), 404, "schedule.not_found"),
        (lambda: api.schedule_draft("no-such-transaction"), 404, "transaction.not_found"),
        (lambda: api.schedules_view(-1), 400, None),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert refused.value.status == status
        if code:
            assert refused.value.code == code


def test_scenario_estimates_read_and_save_alike(target, book):
    """#358: a scenario's estimates are read and saved through the server."""
    from breadsched.api.typed_codec import to_data
    from breadsched.gen.lib import Recurrence, Scenario
    from breadsched.gen.services import FixedScheduleInput, SaveFixedScenarioSchedule
    from breadsched.gen.services.schedule_editing import scenario_events

    api, db = target.api, target.db
    scenario = Scenario(name="Move")
    with db.transaction("Scenario") as txn:
        db.add_scenario(scenario, txn)
    rent = next(item for item in db.iter_scheduled() if item.name == "Rent")
    view = api.scenario_events(scenario.handle)
    assert to_data(view) == to_data(scenario_events(db, scenario.handle).value)
    assert [item.schedule.handle for item in view.baseline] == [rent.handle]
    assert view.changes == ()
    saved = api.save_fixed_scenario_schedule(
        SaveFixedScenarioSchedule(
            scenario_handle=scenario.handle,
            source_schedule=rent.handle,
            definition=FixedScheduleInput(
                name="Smaller rent",
                recurrence=Recurrence(period=PeriodType.MONTH, interval=1, start=date(2026, 11, 6)),
                category=book.rent,
                funding=book.checking,
                amount=Money("1500"),
            ),
        )
    )
    assert (saved.scenario_handle, saved.source_schedule) == (scenario.handle, rent.handle)
    [change] = api.scenario_events(scenario.handle).changes
    assert change.schedule.name == "Smaller rent" and change.source_name == "Rent"
    assert db.get_scheduled(rent.handle).amount() == rent.amount()  # the baseline is kept
    # A draft is checked and laid out but not saved (an editor's preview).
    draft_request = SaveFixedScenarioSchedule(
        scenario_handle=scenario.handle,
        definition=FixedScheduleInput(
            name="Parking",
            recurrence=Recurrence(period=PeriodType.MONTH, interval=1, start=date(2026, 11, 1)),
            category=book.rent,
            funding=book.checking,
            amount=Money("60"),
        ),
    )
    draft = api.draft_fixed_scenario_schedule(draft_request)
    assert draft.name == "Parking"
    assert sorted(abs(split.amount) for split in draft.splits) == [Money("60"), Money("60")]
    before = repr(db.get_scenario(scenario.handle).serialize())
    assert len(db.get_scenario(scenario.handle).schedule_overrides) == 1
    with pytest.raises(ApiError) as refused:
        api.draft_fixed_scenario_schedule(
            SaveFixedScenarioSchedule(
                scenario_handle=scenario.handle,
                definition=FixedScheduleInput(
                    name="Same",
                    recurrence=draft_request.definition.recurrence,
                    category=book.checking,
                    funding=book.checking,
                    amount=Money("60"),
                ),
            )
        )
    assert refused.value.code == "schedule.accounts.same"
    with pytest.raises(ApiError) as refused:
        api.scenario_events("no-such-scenario")
    assert (refused.value.status, refused.value.code) == (404, "scenario.not_found")
    with pytest.raises(ApiError):
        api.save_fixed_scenario_schedule(
            SaveFixedScenarioSchedule(
                scenario_handle=scenario.handle,
                definition=FixedScheduleInput(
                    name="",
                    recurrence=Recurrence(
                        period=PeriodType.MONTH, interval=1, start=date(2026, 11, 6)
                    ),
                    category=book.rent,
                    funding=book.checking,
                    amount=Money("1500"),
                ),
            )
        )
    assert repr(db.get_scenario(scenario.handle).serialize()) == before


def test_imports_run_alike(target, book, tmp_path):
    """#358: statement and book imports go through the server, by a path it reads."""
    from breadsched.gen.services.csv_import import CsvImportRequest, CsvMapping, InspectCsv

    api, db = target.api, target.db
    statement = tmp_path / "statement.csv"
    statement.write_text(
        "Date,Amount,Description\n2026-03-01,-12.50,Coffee\n2026-03-02,-40.00,Groceries\n",
        encoding="utf-8",
    )
    inspection = api.inspect_csv(InspectCsv(str(statement)))
    assert inspection.columns == ("Date", "Amount", "Description")
    request = CsvImportRequest(
        source=str(statement),
        account=book.checking,
        mapping=CsvMapping(date="Date", amount="Amount", description="Description"),
    )
    before = db.summary()["txn"]
    preview = api.preview_csv(request)
    assert preview.count("new") == 2 and db.summary()["txn"] == before
    imported = api.import_csv(request)
    assert imported.result.transactions_new == 2
    assert db.summary()["txn"] == before + 2
    assert api.preview_csv(request).count("imported") == 2
    for call in (
        lambda: api.inspect_csv(InspectCsv(str(tmp_path / "missing.csv"))),
        lambda: api.import_csv(CsvImportRequest("", book.checking, request.mapping)),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert refused.value.status in (400, 404)
    assert db.summary()["txn"] == before + 2
    assert api.import_source() is None or isinstance(api.import_source(), str)

    # A whole file through its importer, with the importer's full result back.
    from breadsched.gen.services.imports import ImportBook

    qif = tmp_path / "statement.qif"
    qif.write_bytes(
        b"!Account\nNImported checking\nTBank\n^\n!Type:Bank\nD03/04/2026\nT12.50\nPExample\n^\n"
    )
    book_import = api.import_book(ImportBook(str(qif)))
    assert book_import.source == str(qif) and "QIF" in book_import.format_name
    assert book_import.result.transactions_new == 1
    assert book_import.result.detail(limit=5)
    assert db.summary()["txn"] == before + 3
    assert api.import_source() == str(qif)


def test_a_plan_cell_is_explained_alike(target, book):
    from breadsched.api.typed_codec import to_data
    from breadsched.gen.services.plan_cells import PlanCellQuery, explain_plan_cell

    api, db = target.api, target.db
    with db.transaction("Rent") as txn:
        db.add_transaction(
            Transaction.simple(date(2026, 10, 6), "Rent", book.rent, book.checking, "1800"), txn
        )
    query = PlanCellQuery(book.rent, date(2026, 10, 1), date(2026, 10, 31))
    explained = api.explain_plan_cell(query)
    assert to_data(explained) == to_data(explain_plan_cell(db, query).value)
    assert explained.scenario_name == "Base scenario"
    assert explained.category is not None and explained.category.actual == Money("1800")
    assert explained.flow is None and explained.mortgage is None
    for bad, status, code in (
        (PlanCellQuery("no-such-account", date(2026, 10, 1), date(2026, 10, 31)), 404, None),
        (PlanCellQuery(book.rent, date(2026, 10, 31), date(2026, 10, 1)), 400, None),
        (
            PlanCellQuery(book.rent, date(2026, 10, 1), date(2026, 10, 31), scenario="nope"),
            404,
            "scenario.not_found",
        ),
        (
            PlanCellQuery(
                book.rent, date(2026, 10, 1), date(2026, 10, 31), requirement_kind="tuition"
            ),
            400,
            "plan.requirement_kind.invalid",
        ),
    ):
        with pytest.raises(ApiError) as refused:
            api.explain_plan_cell(bad)
        assert refused.value.status == status
        if code:
            assert refused.value.code == code


def test_currencies_in_book_order_alike(target, book):
    from breadsched.gen.lib import Commodity

    api, db = target.api, target.db
    with db.transaction("Currencies") as txn:
        db.add_commodity(Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro"), txn)
        db.add_commodity(Commodity(namespace="CURRENCY", mnemonic="CAD", fullname="Dollar"), txn)
    by_code = [item.mnemonic for item in api.currencies()]
    held = [item.mnemonic for item in api.currencies(book_order=True)]
    assert by_code == sorted(by_code)
    assert held == [item.mnemonic for item in db.iter_commodities() if item.is_currency]
    assert sorted(held) == by_code
    # Every commodity, currencies and securities, by namespace and code.
    every = api.commodities()
    assert [item.handle for item in every] == [
        item.handle
        for item in sorted(db.iter_commodities(), key=lambda c: (c.namespace, c.mnemonic))
    ]


def test_the_transaction_editor_reads_alike(target, book):
    """#358: one read gives the editor its accounts, documents, and open claims."""
    from breadsched.api.typed_codec import to_data
    from breadsched.gen.services.transaction_editing import transaction_editor

    api, db = target.api, target.db
    with db.transaction("Rent") as txn:
        paid = Transaction.simple(date(2026, 9, 6), "Rent", book.rent, book.checking, "1800")
        db.add_transaction(paid, txn)
    api.link_attachment(paid.handle, "https://example.com/lease")
    fresh = api.transaction_editor()
    assert to_data(fresh) == to_data(transaction_editor(db).value)
    assert {record.account.handle for record in fresh.accounts} == {
        account.handle for account in db.iter_accounts()
    }
    assert fresh.documents == () and fresh.escrow_explanations == ()
    stored = api.transaction_editor(paid.handle)
    assert [item.location for item in stored.documents] == ["https://example.com/lease"]
    with pytest.raises(ApiError) as refused:
        api.transaction_editor("no-such-transaction")
    assert (refused.value.status, refused.value.code) == (404, "transaction.not_found")


def test_the_scheduled_page_reads_alike(target, book):
    """#358: GTK's Scheduled and Upcoming views read one page through the server."""
    from breadsched.api.typed_codec import to_data
    from breadsched.gen.engine import schedule as engine
    from breadsched.gen.services.schedule_editing import scheduled_page

    api, db = target.api, target.db
    card = db.get_account(book.card)
    card.card.pays_in_full = False
    card.card.usual_payment = Money("100")
    card.card.payment_day = 15
    card.card.payment_account = book.checking
    with db.transaction("Card") as txn:
        db.commit_account(card, txn)
        db.add_transaction(
            Transaction.simple(date(2026, 10, 2), "Fuel", book.rent, book.card, "60"), txn
        )
    as_of = date(2026, 10, 3)
    page = api.scheduled_page(days=60, as_of=as_of)
    assert to_data(page) == to_data(scheduled_page(db, 60, as_of).value)
    assert [item.name for item in page.schedules] == ["Rent"]
    assert page.editability[page.schedules[0].handle].editable
    assert page.names[book.rent] == db.full_name(db.get_account(book.rent))
    expected = engine.upcoming_occurrences(db, as_of=as_of, horizon_days=60)
    assert [(item.when, item.name) for item in page.upcoming] == [
        (item.when, item.name) for item in expected
    ]
    assert len(page.payments) == len(engine.account_payment_definitions(db))
    # Without days, no occurrences are worked out.
    assert api.scheduled_page().upcoming == []
    with pytest.raises(ApiError) as refused:
        api.scheduled_page(days=-1)
    assert refused.value.status == 400


def test_attachments_alike(target, book, tmp_path):
    api, db = target.api, target.db
    paid = Transaction.simple(date(2026, 3, 8), "Dentist", book.rent, book.checking, "200")
    with db.transaction("Dentist") as txn:
        db.add_transaction(paid, txn)
    folder = tmp_path / "documents"
    assert api.set_attachment_folder(str(folder)) == str(folder)
    receipt = tmp_path / "receipt.pdf"
    receipt.write_bytes(b"%PDF-1.4")
    linked = api.link_attachment(paid.handle, "https://example.com/bill")
    assert linked.attachments == ["https://example.com/bill"]
    copied = api.attach_file(paid.handle, str(receipt))
    stored = copied.attachments[-1]
    assert (folder / stored).read_bytes() == b"%PDF-1.4"
    report = api.attachment_report(transaction=paid.handle)
    assert report.folder == folder
    assert {(item.location, item.kind, item.present) for item in report.attachments} == {
        ("https://example.com/bill", "web", None),
        (stored, "file", True),
    }
    (folder / stored).unlink()
    [missing] = api.attachment_report(missing_only=True).attachments
    assert missing.location == stored
    (folder / "moved.pdf").write_bytes(b"%PDF-1.4")
    moved = api.relink_attachment(paid.handle, stored, "moved.pdf")
    assert moved.attachments == ["https://example.com/bill", "moved.pdf"]
    assert api.unlink_attachment(paid.handle, "moved.pdf").attachments == [
        "https://example.com/bill"
    ]
    assert receipt.exists() and (folder / "moved.pdf").exists()

    before = repr(db.get_transaction(paid.handle).serialize())
    for call, status, code in (
        (lambda: api.link_attachment(paid.handle, "https://example.com/bill"), 400, None),
        (lambda: api.unlink_attachment(paid.handle, "nothing"), 404, "attachment.not_found"),
        (lambda: api.link_attachment("missing", "x"), 404, "transaction.not_found"),
        (lambda: api.attach_file(paid.handle, str(tmp_path / "none.pdf")), 404, None),
        (lambda: api.set_source_link_folder("relative/folder"), 400, None),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert refused.value.status == status
        assert code is None or refused.value.code == code
    assert repr(db.get_transaction(paid.handle).serialize()) == before


def test_net_worth_reads_alike(target, book):
    from breadsched.gen.engine.activity import ReportingPeriod
    from breadsched.gen.services.net_worth import (
        query_net_worth_change,
        query_net_worth_history,
    )

    api, db = target.api, target.db
    with db.transaction("Activity") as txn:
        for when, description, to, source, amount in (
            (date(2026, 1, 1), "Opening", book.checking, book.opening, "5000"),
            (date(2026, 1, 15), "Pay", book.checking, book.salary, "3000"),
            (date(2026, 2, 3), "Card", book.groceries, book.card, "120.45"),
        ):
            db.add_transaction(Transaction.simple(when, description, to, source, amount), txn)
    start, end, today = date(2026, 1, 1), date(2026, 3, 31), date(2026, 3, 15)
    history = api.net_worth_history(start, end, ReportingPeriod.MONTH, today)
    assert history == query_net_worth_history(db, start, end, "month", today).value
    assert [point.net_worth for point in history.points] == [
        Money("8000.00"),
        Money("7879.55"),
        Money("7879.55"),
    ]
    assert history.points[-1].partial
    change = api.net_worth_change(date(2026, 2, 1), date(2026, 2, 28), today)
    assert change == query_net_worth_change(db, date(2026, 2, 1), date(2026, 2, 28), today).value
    assert change.change == Money("-120.45")
    assert [posting.description for posting in change.postings] == ["Card"]
    with pytest.raises(ApiError) as refused:
        api.net_worth_history(end, start)
    assert refused.value.status == 400


def test_plan_and_spending_alike(target, book):
    from breadsched.gen.services.expense_explorer import query_expense_explorer
    from breadsched.gen.services.plan import PlanQuery, query_plan

    api, db = target.api, target.db
    with db.transaction("Activity") as txn:
        for when, description, to, source, amount in (
            (date(2026, 9, 1), "Opening", book.checking, book.opening, "5000"),
            (date(2026, 10, 2), "Groceries", book.groceries, book.checking, "84.10"),
            (date(2026, 10, 6), "Rent", book.rent, book.checking, "1800"),
        ):
            db.add_transaction(Transaction.simple(when, description, to, source, amount), txn)
    query = PlanQuery(start=date(2026, 10, 1), end=date(2027, 1, 31), today=date(2026, 10, 15))
    plan = api.plan(query)
    assert plan == query_plan(db, query).value
    assert api.reporting_currency() in ("", "USD")
    explorer = api.expense_explorer(query, book.groceries, 0)
    assert (
        explorer == query_expense_explorer(db, query, account=book.groceries, period_index=0).value
    )
    assert explorer.drilldown is not None
    assert [item.description for item in explorer.drilldown.actual_transactions] == ["Groceries"]
    with pytest.raises(ApiError) as refused:
        api.plan(PlanQuery(start=date(2027, 2, 1), end=date(2027, 1, 1), today=date(2026, 10, 15)))
    assert (refused.value.status, refused.value.code) == (400, "plan.end.before_start")
    with pytest.raises(ApiError) as missing:
        api.plan(PlanQuery(scenario="missing", today=date(2026, 10, 15)))
    assert missing.value.status == 404


def test_projection_alike(target, book):
    from breadsched.api.typed_codec import to_data
    from breadsched.gen.engine import projection
    from breadsched.gen.lib import Scenario

    api, db = target.api, target.db
    with db.transaction("Opening") as txn:
        db.add_transaction(
            Transaction.simple(date(2026, 9, 1), "Opening", book.checking, book.opening, "9000"),
            txn,
        )
    draft = Scenario(name="Draft", start=date(2026, 10, 1), years=1)
    result = api.project(draft)
    assert result == projection.project(db, draft)
    assert len(result.rows) == 12
    month = api.projection_month(draft, 2)
    assert to_data(month) == to_data(projection.explain_month(db, projection.project(db, draft), 2))
    with pytest.raises(ApiError) as refused:
        api.projection_month(draft, 99)
    assert (refused.value.status, refused.value.code) == (404, "projection.month.not_found")
    # A projection already calculated is explained as it stands (in process; over
    # HTTP it is not sent and the server projects again, to the same answer).
    assert to_data(api.projection_month(draft, 2, result)) == to_data(month)
    with pytest.raises(ApiError) as refused:
        api.projection_month(draft, 12, result)
    assert refused.value.code == "projection.month.not_found"


# ------------------------------------------------------------ in process only


def test_projection_progress_is_heard_in_process(db, book):
    from breadsched.gen.lib import Scenario

    heard = []
    LocalBookServer(db).project(
        Scenario(name="Draft", start=date(2026, 10, 1), years=1), progress=heard.append
    )
    assert heard and heard[-1].fraction == 1.0


def test_closing_the_server_stops_notifications(db, book):
    _add_rent(db, book)
    server = LocalBookServer(db)
    seen: list[Change] = []
    stop = server.subscribe(seen.append)
    [sched] = db.iter_scheduled()
    server.set_advance_days(SetAdvanceDays(sched.handle, advance_days=2))
    stop()
    server.set_advance_days(SetAdvanceDays(sched.handle, advance_days=3))
    server.subscribe(seen.append)
    server.close()
    server.set_advance_days(SetAdvanceDays(sched.handle, advance_days=4))
    assert len(seen) == 1


def test_the_revision_is_stored_with_the_book(tmp_path):
    """Every process agrees on the revision: it is kept in the book, moves forward on
    undo, and a read-only snapshot sees the committed value."""
    path = tmp_path / "revision.breadsched"
    create_sample_book(path, as_of=TODAY)
    with open_book_server(str(path)) as server:
        start = server.revision
        [sched] = [item for item in server.db.iter_scheduled() if item.name == "Sample rent"]
        server.set_advance_days(SetAdvanceDays(sched.handle, advance_days=3))
        assert server.undo() is True
        assert server.revision == start + 2
        reader = DbSQLite()
        reader.load(str(path), mode="r")
        try:
            assert reader.revision() == start + 2
        finally:
            reader.close()
    with open_book_server(str(path), "r") as reopened:
        assert reopened.revision == start + 2


# ------------------------------------------------------------------ HTTP only


@pytest.fixture
def remote(db, book):
    _add_rent(db, book)
    httpd, base = _serve(db)
    # ``client`` is a device elsewhere (no local token); ``local`` a program here.
    yield SimpleNamespace(
        base=base,
        token=httpd.token,
        db=db,
        client=HttpBookClient(base, httpd.token),
        local=HttpBookClient(base, httpd.token, local_token=httpd.local_token),
    )
    httpd.shutdown()
    httpd.server_close()


def _raw(remote, method, path, body=None, headers=None):
    request = urllib.request.Request(
        remote.base + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "X-BreadSched-Token": remote.token,
            "Content-Type": "application/json",
            **(headers or {}),
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read()), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read()), dict(error.headers)


def test_every_response_names_the_revision_and_a_stale_write_is_refused(remote):
    [sched] = remote.db.iter_scheduled()
    status, book, headers = _raw(remote, "GET", "/api/v1/book")
    assert status == 200 and headers["ETag"] == f'"{book["revision"]}"'
    path = f"/api/v1/schedules/{sched.handle}/advance-days"
    before = remote.db.get_scheduled(sched.handle).serialize()

    stale = f'"{book["revision"] - 1}"'
    status, refused, _ = _raw(remote, "PATCH", path, {"remind_days": 5}, {"If-Match": stale})
    assert (status, refused["code"]) == (412, "book.revision.stale")
    assert remote.db.get_scheduled(sched.handle).serialize() == before

    current = f'"{book["revision"]}"'
    status, saved, headers = _raw(remote, "PATCH", path, {"remind_days": 5}, {"If-Match": current})
    assert status == 200 and saved["remind_days"] == 5
    assert headers["ETag"] == f'"{book["revision"] + 1}"'

    client = remote.client
    client.require_revision = book["revision"]
    with pytest.raises(ApiError) as caught:
        client.set_advance_days(SetAdvanceDays(sched.handle, remind_days=6))
    assert caught.value.status == 412
    client.require_revision = client.seen_revision
    assert client.set_advance_days(SetAdvanceDays(sched.handle, remind_days=6)).remind_days == 6


def test_historical_estimates_are_proposed_and_accepted_alike(target, book):
    from breadsched.api.typed_codec import to_data
    from breadsched.gen.engine import estimates
    from breadsched.gen.services.historical_estimates import (
        AcceptHistoricalEstimate,
        historical_estimates,
    )

    api, db = target.api, target.db
    with db.transaction("One-time history") as txn:
        db.add_transaction(
            Transaction.simple(date(2026, 3, 5), "One-time", book.utilities, book.checking, "600"),
            txn,
        )
    as_of = date(2026, 4, 20)
    found = api.historical_estimates(3, 1, as_of=as_of)
    expected = historical_estimates(db, 3, 1, as_of=as_of).value
    # Each draft is a new, unsaved schedule, so only its handle differs between calls.
    assert to_data(found.proposals) == to_data(expected.proposals)
    assert [to_data(item.projection) for item in found.drafts] == [
        to_data(item.projection) for item in expected.drafts
    ]
    engine = estimates.propose_historical_estimates(db, as_of=as_of, months=3, min_active_months=1)
    assert [item.key for item in found.proposals] == [item.key for item in engine]
    index = next(i for i, item in enumerate(found.proposals) if item.category == book.utilities)
    proposal, draft = found.proposals[index], found.drafts[index]
    assert proposal.amount == Money("600")
    assert draft.schedule.placeholder
    assert draft.projection.editability.editable
    assert set(draft.classes) == {split.account for split in draft.schedule.splits}
    assert [target.name for target in found.targets][0] == "Base"

    before = len(list(db.iter_scheduled()))
    for request, code in (
        (
            AcceptHistoricalEstimate(key="nothing", months=3, min_active_months=1, as_of=as_of),
            "estimate.proposal.unavailable",
        ),
        (
            AcceptHistoricalEstimate(key=proposal.key, months=0, as_of=as_of),
            "estimate.months.invalid",
        ),
        (
            AcceptHistoricalEstimate(
                key=proposal.key, months=3, min_active_months=1, scenario="gone", as_of=as_of
            ),
            "estimate.scenario.not_found",
        ),
    ):
        with pytest.raises(ApiError) as refused:
            api.accept_historical_estimate(request)
        assert refused.value.code == code
    assert len(list(db.iter_scheduled())) == before

    # A scenario's draft of the same proposal, for an editor to adjust, saves nothing.
    from breadsched.gen.lib import Scenario

    scenario = Scenario(name="Leaner")
    with db.transaction("Scenario") as txn:
        db.add_scenario(scenario, txn)
    snapshot = _snapshot(db)
    drafted = api.draft_scenario_estimate(
        AcceptHistoricalEstimate(
            key=proposal.key, months=3, min_active_months=1, scenario=scenario.handle, as_of=as_of
        )
    )
    assert drafted.placeholder and drafted.name == proposal.estimate_name
    assert _snapshot(db) == snapshot

    accepted = api.accept_historical_estimate(
        AcceptHistoricalEstimate(key=proposal.key, months=3, min_active_months=1, as_of=as_of)
    )
    stored = db.get_scheduled(accepted.handle)
    assert stored is not None and stored.placeholder
    assert accepted.category_name == proposal.category_name


def test_plan_settings_are_kept_alike(target, book):
    from breadsched.gen.engine.activity import PlanMeasure, PlanSettings, ReportingPeriod

    api, db = target.api, target.db
    defaults = (date(2026, 1, 1), date(2027, 12, 31))
    assert api.plan_settings(*defaults) == PlanSettings(*defaults)
    chosen = PlanSettings(
        date(2026, 3, 1), date(2026, 8, 31), ReportingPeriod.QUARTER, PlanMeasure.ACTUAL
    )
    assert api.save_plan_settings(chosen) == chosen
    assert api.plan_settings(*defaults) == chosen
    # Each person's own (decision 0004): the book itself is left as it was.
    assert PlanSettings.load(db, *defaults) == PlanSettings(*defaults)
    for refused_settings, code in (
        (PlanSettings(date(2026, 9, 1), date(2026, 8, 31)), "plan.end.before_start"),
        (PlanSettings(*defaults, scenario="gone"), "plan.scenario.not_found"),
    ):
        with pytest.raises(ApiError) as refused:
            api.save_plan_settings(refused_settings)
        assert refused.value.code == code
    assert api.plan_settings(*defaults) == chosen


def test_register_views_read_alike(target, book, tmp_path):
    from test_specific_lots import _fund, _trade

    from breadsched.api.typed_codec import to_data
    from breadsched.gen.engine import ledger
    from breadsched.gen.services.register_view import register_view

    api, db = target.api, target.db
    with db.transaction("Activity") as txn:
        for when, description, to, source, amount in (
            (date(2026, 9, 1), "Opening", book.checking, book.opening, "5000"),
            (date(2026, 9, 6), "Rent", book.rent, book.checking, "1800"),
        ):
            db.add_transaction(Transaction.simple(when, description, to, source, amount), txn)
    rent = next(item for item in db.iter_transactions() if item.description == "Rent")
    api.link_attachment(rent.handle, "https://example.com/lease")
    view = api.register_view(book.checking, 1)
    assert to_data(view) == to_data(register_view(db, book.checking, 1).value)
    [row] = view.rows
    assert row.transaction.handle == rent.handle
    assert row.running == ledger.register(db, book.checking)[-1].running
    assert view.transfer(row) == db.full_name(db.get_account(book.rent))
    assert [item.location for item in view.documents_of(rent.handle)] == [
        "https://example.com/lease"
    ]
    assert view.holding is None
    assert len(api.register_view(book.checking).rows) == 2
    # Every row, as the desktop register reads it (#389): ``limit=all`` over HTTP.
    every = api.register_view(book.checking, None)
    assert [item.transaction.handle for item in every.rows] == [
        item.transaction.handle for item in ledger.register(db, book.checking)
    ]
    # The register filter reaches the server: 1800 is the rent's amount (#382).
    assert [
        row.transaction.handle for row in api.register_view(book.checking, 250, "1800").rows
    ] == [rent.handle]

    account, fund, usd = _fund(db, book)
    _trade(db, account, usd, book.checking, date(2026, 9, 10), "4", "400")
    held = api.register_view(account.handle)
    assert held.holding is not None
    assert (held.holding.security.mnemonic, held.holding.held) == ("INDEX", Money("4"))
    assert account.handle in held.security_accounts
    assert held.rows[-1].share_balance == Money("4")
    for call, status in (
        (lambda: api.register_view("no-such-account"), 404),
        (lambda: api.register_view(book.checking, 0), 400),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert refused.value.status == status


def test_register_changes_merge_like_a_full_read(target, book):
    """A register holding every row merges the changed transactions' rows and gets
    what reading the whole register again gives (#319, #389)."""
    from breadsched.api.typed_codec import to_data
    from breadsched.gen.engine import ledger

    api, db = target.api, target.db
    with db.transaction("Activity") as txn:
        for when, description, to, source, amount in (
            (date(2026, 9, 1), "Opening", book.checking, book.opening, "5000"),
            (date(2026, 9, 6), "Rent", book.rent, book.checking, "1800"),
            (date(2026, 9, 20), "Groceries", book.rent, book.checking, "75"),
        ):
            db.add_transaction(Transaction.simple(when, description, to, source, amount), txn)
    rows = list(api.register_view(book.checking, None).rows)
    rent = next(item for item in db.iter_transactions() if item.description == "Rent")
    groceries = next(item for item in db.iter_transactions() if item.description == "Groceries")
    with db.transaction("Back-dated and removed") as txn:
        db.add_transaction(
            Transaction.simple(date(2026, 9, 3), "Refund", book.checking, book.rent, "40"), txn
        )
        db.remove_transaction(groceries.handle, txn)
    refund = next(item for item in db.iter_transactions() if item.description == "Refund")
    changes = api.register_changes(book.checking, [refund.handle, groceries.handle])
    assert changes.changed == tuple(sorted((refund.handle, groceries.handle)))
    assert [row.transaction.handle for row in changes.rows] == [refund.handle]
    assert changes.names[book.rent] == db.full_name(book.rent)
    merged = changes.apply(rows)
    assert to_data(tuple(merged)) == to_data(tuple(ledger.register(db, book.checking)))
    assert merged[-1].running == Money("3240")
    # Changes elsewhere leave the register's own rows as they were.
    assert api.register_changes(book.checking, [rent.handle]).apply(merged)[-1].running == (
        Money("3240")
    )
    with pytest.raises(ApiError) as refused:
        api.register_changes("no-such-account", [rent.handle])
    assert refused.value.status == 404


def test_account_suggestions_are_listed_and_applied_alike(target, book):
    from breadsched.gen.engine import inference
    from breadsched.gen.lib import Account, AccountType
    from breadsched.gen.services.account_inference import ApplyAccountSuggestions

    api, db = target.api, target.db
    liabilities = db.get_account(book.card).parent
    home = Account(name="Home Evans", atype=AccountType.ASSET, parent=book.assets)
    mortgage = Account(name="Mortgage Evans", atype=AccountType.LIABILITY, parent=liabilities)
    with db.transaction("Accounts") as txn:
        db.add_account(home, txn)
        db.add_account(mortgage, txn)
    found = api.account_suggestions()
    engine = inference.infer_all(db).suggestions
    assert [(item.account, item.field, item.confidence) for item in found] == [
        (item.account, item.field, Decimal(str(round(item.confidence, 2)))) for item in engine
    ]
    link = next(item for item in found if item.account == mortgage.handle)
    # Shown as the account's full name; only a shared place name, so not confident.
    assert link.value == db.full_name(home.handle) and link.confidence < 0.6
    assert api.apply_account_suggestions(ApplyAccountSuggestions()).applied == len(
        [item for item in engine if item.confidence >= 0.6]
    )
    assert db.get_account(mortgage.handle).liability.linked_asset is None
    applied = api.apply_account_suggestions(ApplyAccountSuggestions(min_confidence=Decimal(0)))
    assert (applied.applied, applied.considered) == (len(engine), len(engine))
    assert db.get_account(mortgage.handle).liability.linked_asset == home.handle
    with pytest.raises(ApiError) as refused:
        api.apply_account_suggestions(ApplyAccountSuggestions(min_confidence=Decimal("1.5")))
    assert (refused.value.status, refused.value.code) == (400, "inference.confidence.invalid")


def test_counts_plan_bounds_and_dashboard_defaults_read_alike(target, book):
    from breadsched.api.typed_codec import to_data
    from breadsched.gen.engine.dashboard import default_config
    from breadsched.gen.services.dashboard_config import settings_of
    from breadsched.gen.services.plan import PlanQuery

    api, db = target.api, target.db
    with db.transaction("Opening") as txn:
        db.add_transaction(
            Transaction.simple(date(2024, 3, 9), "Opening", book.checking, book.opening, "50"),
            txn,
        )
    counts = api.book_counts()
    summary = db.summary()
    assert (counts.accounts, counts.transactions) == (summary["account"], summary["txn"])
    bounds = api.plan_bounds(TODAY)
    # The first month with book data, through the month before the 150th anniversary.
    assert bounds.minimum == date(2024, 3, 1)
    plan = api.plan(PlanQuery(today=TODAY))
    assert (bounds.minimum, bounds.maximum) == (plan.minimum, plan.maximum)
    assert to_data(api.default_dashboard_settings()) == to_data(settings_of(default_config(db)))


def test_the_book_summary_and_verification_read_alike(target, book):
    from breadsched.api.typed_codec import to_data
    from breadsched.gen.engine import valuation

    api, db = target.api, target.db
    with db.transaction("Opening") as txn:
        db.add_transaction(
            Transaction.simple(date(2026, 9, 1), "Opening", book.checking, book.opening, "5000"),
            txn,
        )
    summary = api.book_summary()
    counts = db.summary()
    assert (summary.accounts, summary.transactions) == (counts["account"], counts["txn"])
    assert summary.path == db.path
    assert to_data(summary.net_worth) == to_data(valuation.aggregate_value(db, net_worth=True))
    assert summary.cash.amount is not None and summary.cash.amount.value == Money("5000")
    assert summary.scenarios == tuple(item.name for item in db.iter_scenarios())
    verification = api.verify_book()
    assert verification.ok
    assert verification.as_dict() == db.verification_report().as_dict()


def test_security_prices_are_listed_and_saved_alike(target, book):
    from breadsched.gen.services.security_prices import SaveSecurityPrice

    api, db = target.api, target.db
    usd = db.get_commodity_by_mnemonic("USD")
    before = sorted(repr(price.serialize()) for price in db.iter_prices())
    for request, code in (
        (
            SaveSecurityPrice(None, usd.handle, date(2026, 3, 1), Money("0")),
            "security.price.invalid",
        ),
        (
            SaveSecurityPrice(None, usd.handle, date(2026, 3, 1), Money("1")),
            "security.symbol.required",
        ),
        (SaveSecurityPrice(usd.handle, None, date(2026, 3, 1), Money("1")), "security.is_currency"),
        (
            SaveSecurityPrice("gone", usd.handle, date(2026, 3, 1), Money("1")),
            "security.not_found",
        ),
        (
            SaveSecurityPrice(None, "gone", date(2026, 3, 1), Money("1"), mnemonic="IDX"),
            "security.currency.invalid",
        ),
        (
            SaveSecurityPrice(
                None, usd.handle, date(2026, 3, 1), Money("1"), mnemonic="IDX", namespace="ISO4217"
            ),
            "security.namespace.invalid",
        ),
    ):
        with pytest.raises(ApiError) as refused:
            api.save_security_price(request)
        status = 404 if code.endswith("not_found") else 400
        assert (refused.value.status, refused.value.code) == (status, code)
    # A refused price leaves the book as it was.
    assert sorted(repr(price.serialize()) for price in db.iter_prices()) == before
    assert db.get_commodity_by_mnemonic("IDX") is None

    saved = api.save_security_price(
        SaveSecurityPrice(
            None, usd.handle, date(2026, 3, 1), Money("125.25"), mnemonic="idx", fullname="Index"
        )
    )
    assert (saved.security.mnemonic, saved.currency.mnemonic) == ("IDX", "USD")
    assert saved.price.value == Money("125.25")
    [quote] = [item for item in api.securities() if item.security.mnemonic == "IDX"]
    assert (quote.latest.quote_date, quote.latest.value) == (date(2026, 3, 1), Money("125.25"))
    again = api.save_security_price(
        SaveSecurityPrice(saved.security.handle, usd.handle, date(2026, 3, 1), Money("126"))
    )
    assert again.price.handle == saved.price.handle  # the same day's price is replaced
    with pytest.raises(ApiError) as refused:
        api.save_security_price(
            SaveSecurityPrice(None, usd.handle, date(2026, 3, 2), Money("1"), mnemonic="IDX")
        )
    assert refused.value.code == "security.symbol.duplicate"


def test_entry_input_reads_alike(target, book):
    from test_specific_lots import _fund

    from breadsched.api.typed_codec import to_data
    from breadsched.gen.services import entry_input
    from breadsched.gen.services.autocomplete import SuggestEntry, suggest_entry
    from breadsched.gen.services.entry_input import ReadEntryTrade

    api, db = target.api, target.db
    with db.transaction("Rent") as txn:
        db.add_transaction(
            Transaction.simple(date(2026, 9, 6), "Rent", book.rent, book.checking, "1800"),
            txn,
        )
    proposal = api.suggest_entry(SuggestEntry(description="rent", account=book.checking))
    assert to_data(proposal) == to_data(
        suggest_entry(db, SuggestEntry(description="rent", account=book.checking)).value
    )
    assert proposal.suggestion is not None
    assert proposal.name(proposal.suggestion.transfer_account) == db.full_name(
        db.get_account(book.rent)
    )
    assert api.suggest_entry(SuggestEntry(description="nothing like it")).suggestion is None

    assert api.entry_amount("12.5+3").value == Decimal("15.50")
    assert api.entry_amount("").value is None
    with pytest.raises(ApiError) as refused:
        api.entry_amount("12..5")
    assert refused.value.status == 400

    account, _fund_commodity, usd = _fund(db, book)
    filled = api.entry_trade(
        ReadEntryTrade(
            account.handle, shares="10", price="25", changed="price", currency=usd.handle
        )
    )
    assert filled.amount == Decimal("250.00")
    with pytest.raises(ApiError) as refused:
        api.entry_trade(ReadEntryTrade(book.checking, shares="1", price="1"))
    assert refused.value.code == "entry.account.not_security"

    names = [item.full_name for item in api.entry_accounts("Ex:Re")]
    expected = entry_input.complete_entry_account(db, "Ex:Re").value
    assert names == [item.full_name for item in expected]
    assert db.full_name(db.get_account(book.rent)) in names
    assert api.next_num(book.checking, "100", 1) == "101"
    with pytest.raises(ApiError) as refused:
        api.next_num("no-such-account", "1")
    assert refused.value.status == 404


def test_due_occurrences_and_reimbursable_costs_read_alike(target, book):
    from breadsched.api.typed_codec import to_data
    from breadsched.gen.engine.category_report import build_category_report
    from breadsched.gen.engine.schedule import due_occurrences

    api, db = target.api, target.db
    as_of = date(2026, 11, 10)
    due = api.due_occurrences(as_of, 30)
    expected = due_occurrences(db, as_of=as_of, horizon_days=30)
    assert [(item.name, item.when, item.amount) for item in due] == [
        (item.name, item.when, item.amount) for item in expected
    ]
    assert [item.when for item in due] == [date(2026, 10, 6), date(2026, 11, 6), date(2026, 12, 6)]
    with pytest.raises(ApiError) as refused:
        api.due_occurrences(as_of, -1)
    assert (refused.value.status, refused.value.code) == (400, "schedule.horizon.invalid")

    start, end = date(2026, 1, 1), date(2026, 12, 31)
    costs = api.reimbursable_costs(start, end)
    assert to_data(costs) == to_data(build_category_report(db, start, end).reimbursable_categories)
    with pytest.raises(ApiError) as refused:
        api.reimbursable_costs(end, start)
    assert (refused.value.status, refused.value.code) == (400, "receivable.costs.range.invalid")


def test_activity_reads_alike(target, book):
    from breadsched.api.typed_codec import to_data
    from breadsched.gen.engine.activity import ReportingPeriod, build_activity_report
    from breadsched.gen.services.activity import activity_view

    api, db = target.api, target.db
    with db.transaction("Activity") as txn:
        db.add_transaction(
            Transaction.simple(date(2026, 10, 6), "Rent", book.rent, book.checking, "1800"),
            txn,
        )
    start, end, as_of = date(2026, 10, 1), date(2026, 12, 31), date(2026, 10, 15)
    view = api.activity(start, end, ReportingPeriod.MONTH, as_of)
    assert to_data(view) == to_data(activity_view(db, start, end, "month", as_of).value)
    expected = build_activity_report(db, start, end, as_of=as_of)
    assert view.report.as_dict() == expected.as_dict()
    assert [period.planned_amount for period in view.report.periods] == [
        period.planned_amount for period in expected.periods
    ]
    with pytest.raises(ApiError) as refused:
        api.activity(end, start)
    assert (refused.value.status, refused.value.code) == (400, "activity.range.invalid")


def test_the_dashboard_reads_alike(target, book):
    from breadsched.api.typed_codec import to_data
    from breadsched.gen.engine import dashboard as dashboard_engine

    api, db = target.api, target.db
    with db.transaction("Opening") as txn:
        db.add_transaction(
            Transaction.simple(date(2026, 9, 1), "Opening", book.checking, book.opening, "5000"),
            txn,
        )
    as_of = date(2026, 10, 1)
    board = api.dashboard(as_of)
    expected = dashboard_engine.build(db, as_of=as_of)
    assert to_data(board) == to_data(expected)
    assert board.report_summary() == expected.report_summary()
    assert [bill.name for bill in board.display_bills] == [
        bill.name for bill in expected.display_bills
    ]
    saved = board.config.liquidity_days
    wider = api.dashboard(as_of, liquidity_days=saved + 30, emergency_months=9)
    assert (wider.config.liquidity_days, wider.config.emergency_months) == (saved + 30, 9)
    # Other horizons are looked at, never saved.
    assert api.dashboard_settings().liquidity_days == saved
    with pytest.raises(ApiError) as refused:
        api.dashboard(as_of, liquidity_days=-1)
    assert (refused.value.status, refused.value.code) == (400, "dashboard.liquidity_days.invalid")


def test_budget_jars_read_alike(target, book):
    from test_budget_jars import TODAY, _groceries, _pay

    from breadsched.api.typed_codec import to_data
    from breadsched.gen.engine.activity import ReportingPeriod
    from breadsched.gen.engine.budget_jars import budget_jars
    from breadsched.gen.services.budget_jars import jars_view

    api, db = target.api, target.db
    _pay(db, book)
    _groceries(db, book)
    start, end = date(2026, 6, 1), date(2026, 7, 31)
    view = api.budget_jars(start, end, ReportingPeriod.MONTH, TODAY)
    assert to_data(view) == to_data(jars_view(db, start, end, "month", TODAY).value)
    assert to_data(view.report) == to_data(budget_jars(db, start, end, today=TODAY))
    assert [jar.name for jar in view.report.jars]
    assert len(view.charts()) == 2 * len(view.report.accounts)
    with pytest.raises(ApiError) as refused:
        api.budget_jars(end, start)
    assert (refused.value.status, refused.value.code) == (400, "jars.range.invalid")


def test_investment_reports_read_alike(target, book):
    from test_specific_lots import _fund, _trade

    from breadsched.api.typed_codec import to_data
    from breadsched.gen.engine.cost_basis import holdings_cost_basis
    from breadsched.gen.engine.realized_gains import realized_gains
    from breadsched.gen.engine.tax_year import tax_year
    from breadsched.gen.services.investment_reports import (
        holdings_view,
        realized_gains_view,
        tax_year_view,
    )

    api, db = target.api, target.db
    account, _fund_commodity, usd = _fund(db, book)
    _trade(db, account, usd, book.checking, date(2024, 1, 10), "10", "1000")
    _trade(db, account, usd, book.checking, date(2025, 3, 10), "-4", "-600")
    _trade(db, account, usd, book.checking, date(2026, 2, 1), "-2", "-260")

    held = api.holdings(date(2025, 12, 31))
    assert to_data(held) == to_data(holdings_view(db, date(2025, 12, 31)))
    [item] = held.holdings
    assert item.quantity == holdings_cost_basis(db, as_of=date(2025, 12, 31))[0].quantity
    assert held.name(account.handle) == db.full_name(account)
    assert [chart.currency for chart in held.charts] == ["USD"]

    gains = api.realized_gains(2025, account.handle)
    assert to_data(gains) == to_data(realized_gains_view(db, 2025, account.handle))
    assert [sale.sale.gain for sale in gains.report.sales] == [
        sale.sale.gain for sale in realized_gains(db, year=2025).sales
    ]
    assert gains.available_years == (2025, 2026)
    assert gains.labels() == {usd.handle: "USD"}
    assert gains.report.as_dict(gains.mnemonics)["years"][0]["currency"] == "USD"
    assert api.realized_gains().available_years == (2025, 2026)
    with pytest.raises(ApiError) as refused:
        api.realized_gains(account="no-such-account")
    assert refused.value.status == 404

    year = api.tax_year(2025)
    assert to_data(year) == to_data(tax_year_view(db, 2025))
    assert to_data(year.report) == to_data(tax_year(db, 2025))
    assert year.labels()[usd.handle] == "USD"
    # Without a year, the latest year with a transaction.
    assert api.tax_year().report.year == year.available_years[0]


def test_documents_served_over_http_stay_in_the_attachment_folder(remote, book, tmp_path):
    """A token holder may not link (and so read back) a file outside the folder."""
    client, db = remote.client, remote.db
    folder = tmp_path / "documents"
    folder.mkdir()
    (folder / "receipt.pdf").write_bytes(b"%PDF-1.4")
    secret = tmp_path / "secret.txt"
    secret.write_text("not a document", encoding="utf-8")
    # Only a program on this computer may choose the folder (#358).
    with pytest.raises(ApiError) as refused:
        client.set_attachment_folder(str(tmp_path))
    assert (refused.value.status, refused.value.code) == (403, "attachment.folder.local_only")
    remote.local.set_attachment_folder(str(folder))
    with db.transaction("Card") as txn:
        card = Transaction.simple(date(2026, 9, 3), "Card", book.groceries, book.card, "12")
        db.add_transaction(card, txn)
    for location in (str(secret), "../secret.txt", str(folder / ".." / "secret.txt")):
        with pytest.raises(ApiError) as refused:
            client.link_attachment(card.handle, location)
        assert (refused.value.status, refused.value.code) == (400, "attachment.outside_folder")
    assert db.get_transaction(card.handle).attachments == []
    linked = client.link_attachment(card.handle, "receipt.pdf")
    assert linked.attachments == ["receipt.pdf"]
    with pytest.raises(ApiError) as refused:
        client.relink_attachment(card.handle, "receipt.pdf", str(secret))
    assert refused.value.code == "attachment.outside_folder"
    assert db.get_transaction(card.handle).attachments == ["receipt.pdf"]
    assert client.link_attachment(card.handle, "https://example.com/r").attachments[-1] == (
        "https://example.com/r"
    )
    # In process (GTK, a command without a server), or from a program on this computer,
    # the user may link any file.
    assert LocalBookServer(db).link_attachment(card.handle, str(secret)).attachments[-1] == str(
        secret
    )
    assert remote.local.relink_attachment(card.handle, str(secret), str(folder)).attachments


def test_a_device_elsewhere_sends_files_and_may_name_only_its_uploads(tmp_path):
    """#358: without the local token the server reads no file a client names; the HTTP
    client sends the file's bytes, and imports and attaches the server's copy."""
    from breadsched.gen.services.imports import ImportBook

    path = tmp_path / "book.breadsched"
    create_sample_book(path, as_of=TODAY)
    db = DbSQLite()
    db.load(str(path))
    httpd, base = _serve(db)
    try:
        device = HttpBookClient(base, httpd.token)
        local = HttpBookClient(base, httpd.token, local_token=httpd.local_token)
        statement = tmp_path / "away" / "statement.qif"
        statement.parent.mkdir()
        statement.write_bytes(
            b"!Account\nNTravel card\nTBank\n^\n!Type:Bank\nD03/04/2026\nT-9.50\nPTaxi\n^\n"
        )
        before = db.summary()["txn"]
        imported = device.import_book(ImportBook(str(statement)))
        assert imported.result.transactions_new == 1 and db.summary()["txn"] == before + 1
        # The server imported its own copy, kept beside the book.
        assert imported.source != str(statement)
        assert Path(imported.source).parent == path.parent / "book.breadsched.uploads"
        assert Path(imported.source).read_bytes() == statement.read_bytes()
        # Naming a file directly, as a raw request would, is refused.
        raw = urllib.request.Request(
            base + "/api/v1/imports",
            data=json.dumps({"source": str(statement)}).encode(),
            headers={"X-BreadSched-Token": httpd.token, "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(raw, timeout=10)
        assert refused.value.code == 403
        assert json.loads(refused.value.read())["code"] == "file.local_only"
        # The same file from a program on this computer is read where it is.
        assert local.import_book(ImportBook(str(statement))).source == str(statement)

        # A document is sent too, and copied into the attachment folder.
        folder = tmp_path / "documents"
        local.set_attachment_folder(str(folder))
        [taxi] = [txn for txn in db.iter_transactions() if txn.description == "Taxi"][:1]
        receipt = tmp_path / "away" / "receipt.pdf"
        receipt.write_bytes(b"%PDF-1.4 taxi")
        attached = device.attach_file(taxi.handle, str(receipt))
        assert (folder / attached.attachments[-1]).read_bytes() == b"%PDF-1.4 taxi"
        for bad in ("", "../escape.pdf"):
            with pytest.raises(ApiError) as refused_name:
                device.attach_content(taxi.handle, bad or "x.pdf", b"" if not bad else b"x")
            assert refused_name.value.status == 400
    finally:
        httpd.shutdown()
        httpd.server_close()
        db.close()


def test_v1_refuses_what_it_does_not_serve(remote):
    [sched] = remote.db.iter_scheduled()
    before = remote.db.get_scheduled(sched.handle).serialize()
    path = f"/api/v1/schedules/{sched.handle}/advance-days"
    cases = [
        ("GET", "/api/v1/nothing", None, 404, "resource.not_found"),
        ("POST", "/api/v1/book", {}, 405, "request.method.not_allowed"),
        ("PATCH", path, {"remind_days": "7"}, 400, "request.invalid"),
        ("PATCH", path, {"colour": "red"}, 400, "request.invalid"),
        (
            "PATCH",
            "/api/v1/schedules/missing/advance-days",
            {"remind_days": 1},
            404,
            "schedule.not_found",
        ),
        (
            "POST",
            f"/api/v1/schedules/{sched.handle}/next",
            {"decision": "later"},
            400,
            "request.invalid",
        ),
        ("GET", "/api/v1/due-review?as_of=soon", None, 400, "query.date.invalid"),
        ("GET", "/api/v1/due-review?colour=red", None, 400, "query.unknown"),
        ("POST", "/api/v1/undo", [], 400, "request.body.object_required"),
        ("GET", "/api/v1/accounts/lookup", None, 400, "query.missing"),
        ("GET", "/api/v1/transactions/lookup?reference=", None, 400, "query.missing"),
        ("GET", f"/api/v1/accounts/{sched.handle}/register", None, 404, "account.not_found"),
        ("GET", "/api/v1/accounts/x/register?limit=two", None, 400, "query.integer.invalid"),
        ("GET", "/api/v1/accounts/x/register?limit=-1", None, 400, "query.integer.out_of_range"),
        ("GET", "/api/v1/accounts/x/balance?recursive=yes", None, 400, "query.invalid"),
        ("GET", "/api/v1/balances?as_of=soon", None, 400, "query.date.invalid"),
        ("POST", "/api/v1/balances", {}, 405, "request.method.not_allowed"),
        ("GET", "/api/v1/chart?hidden=maybe", None, 400, "query.invalid"),
        ("POST", "/api/v1/accounts", {"account": "Travel"}, 400, "request.invalid"),
        ("POST", "/api/v1/accounts", {"account": {}}, 400, "request.invalid"),
        ("POST", "/api/v1/accounts", {"colour": "red"}, 400, "request.invalid"),
        (
            "POST",
            "/api/v1/accounts",
            {"account": {}, "opening_balance": 5},
            400,
            "request.invalid",
        ),
        ("DELETE", "/api/v1/accounts/missing", None, 404, "account.not_found"),
        ("POST", "/api/v1/rules", {"category": 5, "description": "x"}, 400, "request.invalid"),
        ("POST", "/api/v1/rules", {"description": "x"}, 400, "request.invalid"),
        ("PATCH", "/api/v1/rules/x", {"position": "1"}, 400, "request.invalid"),
        ("PATCH", "/api/v1/rules/x", {}, 400, "request.invalid"),
        ("POST", "/api/v1/rules/accept", {"transactions": "all"}, 400, "request.invalid"),
        ("DELETE", "/api/v1/rules/missing", None, 404, "rule.not_found"),
        ("GET", "/api/v1/tags/transactions", None, 400, "query.missing"),
        ("PATCH", "/api/v1/transactions/x/tags", {"tags": "food"}, 400, "request.invalid"),
        ("PATCH", "/api/v1/transactions/x/tags", {"tags": [1]}, 400, "request.invalid"),
        ("GET", "/api/v1/goals?closed=1", None, 400, "query.invalid"),
        ("POST", "/api/v1/goals", {"name": "Car"}, 400, "request.invalid"),
        (
            "POST",
            "/api/v1/goals",
            {
                "name": "Car",
                "account": "x",
                "target_amount": 500,
                "target_date": "2027-01-01",
                "start_date": "2026-01-01",
            },
            400,
            "request.invalid",
        ),
        ("POST", "/api/v1/goals/x/allocations", {"amount": "5"}, 400, "request.invalid"),
        ("POST", "/api/v1/goals/x/close", {"date": "soon"}, 400, "request.invalid"),
        (
            "POST",
            "/api/v1/goals/x/override",
            {"scenario": "s", "excluded": 1},
            400,
            "request.invalid",
        ),
        ("DELETE", "/api/v1/goals/missing", None, 404, "savings_goal.not_found"),
        ("POST", "/api/v1/reconciliations", {"account": "x"}, 400, "request.invalid"),
        (
            "PATCH",
            "/api/v1/reconciliations/x",
            {"selected_splits": "all"},
            400,
            "request.invalid",
        ),
        ("POST", "/api/v1/reconciliations/x/complete", {"now": True}, 400, "request.invalid"),
        ("GET", "/api/v1/accounts/x/reconciliation?as_of=2026-01-01", None, 400, "query.unknown"),
        ("POST", "/api/v1/receivables", {"payer": "Mom"}, 400, "request.invalid"),
        (
            "POST",
            "/api/v1/receivables",
            {"incurred_date": "2026-01-01", "payer": "Mom", "expected_amount": 5},
            400,
            "request.invalid",
        ),
        ("POST", "/api/v1/receivables/x/splits", {"transaction": "t"}, 400, "request.invalid"),
        ("POST", "/api/v1/receivables/x/dispute", {"on": "soon"}, 400, "request.invalid"),
        (
            "POST",
            "/api/v1/reimbursement-proposals/accept",
            {"chosen": [["a"]]},
            400,
            "request.invalid",
        ),
        ("DELETE", "/api/v1/receivables/missing", None, 404, "receivable.not_found"),
        ("POST", "/api/v1/scenarios", {"scenario": "Lean"}, 400, "request.invalid"),
        ("POST", "/api/v1/scenarios", {}, 400, "request.invalid"),
        ("POST", "/api/v1/drawdowns", {"scenario": "x"}, 400, "request.invalid"),
        ("DELETE", "/api/v1/scenarios/x/periods/first", None, 400, "request.invalid"),
        ("GET", "/api/v1/scenarios/lookup", None, 400, "query.missing"),
        ("GET", "/api/v1/payroll?as_of=soon", None, 400, "query.date.invalid"),
        ("GET", "/api/v1/fsa-claims/report?by=colour", None, 400, "query.invalid"),
        ("GET", "/api/v1/review/missing", None, 404, "review.transaction.not_found"),
        ("GET", "/api/v1/transactions/x/splits/y/lots", None, 404, "lots.transaction.not_found"),
        ("POST", "/api/v1/transactions/x/splits/y/lots", {"picks": "all"}, 400, "request.invalid"),
        ("PATCH", "/api/v1/tax-marks", {"accounts": {"a": "yes"}}, 400, "request.invalid"),
        ("POST", "/api/v1/quotes", {"quotes": [{"symbol": "G"}]}, 400, "request.invalid"),
        ("POST", "/api/v1/loans/preview", {"name": "Car"}, 400, "request.invalid"),
        ("POST", "/api/v1/exchange-rates", {"source": "a", "target": "b"}, 400, "request.invalid"),
        ("GET", "/api/v1/currencies/lookup", None, 400, "query.missing"),
        ("PATCH", "/api/v1/dashboard/settings", {"liquidity_days": "14"}, 400, "request.invalid"),
        ("PATCH", "/api/v1/attachment-folders", {}, 400, "request.invalid"),
        ("GET", "/api/v1/net-worth?start=2026-01-01", None, 400, "query.missing"),
        ("GET", "/api/v1/holdings?as_of=soon", None, 400, "query.date.invalid"),
        ("GET", "/api/v1/dashboard?liquidity_days=soon", None, 400, "query.integer.invalid"),
        ("GET", "/api/v1/plan/settings", None, 400, "query.missing"),
        ("GET", "/api/v1/historical-estimates?months=0", None, 400, "estimate.months.invalid"),
        ("POST", "/api/v1/historical-estimates", {"months": "twelve"}, 400, "request.invalid"),
        ("PATCH", "/api/v1/plan/settings", {"start": "soon"}, 400, "request.invalid"),
        ("GET", "/api/v1/activity?start=2026-01-01", None, 400, "query.missing"),
        ("GET", "/api/v1/schedules/due?days=many", None, 400, "query.integer.invalid"),
        ("GET", "/api/v1/entry/trade", None, 400, "query.missing"),
        ("POST", "/api/v1/security-prices", {"security": None}, 400, "request.invalid"),
        ("GET", "/api/v1/gnucash/writeback", None, 400, "writeback.source.unknown"),
        ("POST", "/api/v1/gnucash/writeback", {"transactions": "all"}, 400, "request.invalid"),
        (
            "PATCH",
            "/api/v1/gnucash/writeback/settings",
            {"keep_backups": 0},
            400,
            "writeback.keep_backups.invalid",
        ),
        ("GET", "/api/v1/entry/amount?number_format=dash", None, 400, "query.invalid"),
        ("GET", "/api/v1/entry/num?account=x&step=lots", None, 400, "query.integer.invalid"),
        ("GET", "/api/v1/receivables/costs?start=2026-01-01", None, 400, "query.missing"),
        (
            "GET",
            "/api/v1/activity?start=2026-01-01&end=2026-02-01&period=week",
            None,
            400,
            "query.invalid",
        ),
        (
            "GET",
            "/api/v1/dashboard?emergency_months=-2",
            None,
            400,
            "dashboard.emergency_months.invalid",
        ),
        ("GET", "/api/v1/budget-jars?start=2026-01-01", None, 400, "query.missing"),
        (
            "GET",
            "/api/v1/budget-jars?start=2026-02-01&end=2026-01-01",
            None,
            400,
            "jars.range.invalid",
        ),
        ("GET", "/api/v1/realized-gains?year=last", None, 400, "query.integer.invalid"),
        ("GET", "/api/v1/realized-gains?account=nope", None, 404, "account.not_found"),
        ("GET", "/api/v1/tax-year?year=x", None, 400, "query.integer.invalid"),
        ("POST", "/api/v1/plan", {"period": "fortnight"}, 400, "request.invalid"),
        ("POST", "/api/v1/projection", {}, 400, "request.invalid"),
        ("POST", "/api/v1/expense-explorer", {"rollover": "yes"}, 400, "request.invalid"),
        (
            "GET",
            "/api/v1/net-worth?start=2026-01-01&end=2026-02-01&period=week",
            None,
            400,
            "query.invalid",
        ),
        ("POST", "/api/v1/transactions/x/attachments", {}, 400, "request.invalid"),
        ("GET", "/api/v1/attachments?missing=maybe", None, 400, "query.invalid"),
        ("POST", "/api/v1/loans", {"terms": {}, "opening_balance": "yes"}, 400, "request.invalid"),
        ("PATCH", "/api/v1/commodities/x/quote-source", {}, 400, "request.invalid"),
        (
            "PATCH",
            "/api/v1/commodities/x/quote-source",
            {"source": "tsp"},
            404,
            "quotes.commodity.not_found",
        ),
        ("POST", "/api/v1/held-imports/resolve", {"decisions": [["x"]]}, 400, "request.invalid"),
        (
            "POST",
            "/api/v1/held-imports/resolve",
            {"decisions": [["x", "maybe"]]},
            400,
            "request.invalid",
        ),
        ("POST", "/api/v1/review/x/match", {}, 400, "request.invalid"),
        ("POST", "/api/v1/review/x/unexpected", {"why": 1}, 400, "request.invalid"),
        ("GET", "/api/v1/plan/occurrences?start=2026-01-01", None, 400, "query.missing"),
        (
            "GET",
            "/api/v1/transactions/x/plan-matches?window_days=-1",
            None,
            400,
            "query.integer.out_of_range",
        ),
        ("GET", "/api/v1/fsa-claims/report?status=lost", None, 400, "query.invalid"),
        ("POST", "/api/v1/fsa-claims", {"definition": {}}, 400, "request.invalid"),
        ("POST", "/api/v1/fsa-claims/x/close", {"on": "soon"}, 400, "request.invalid"),
        ("DELETE", "/api/v1/fsa-claims/missing", None, 404, "claim.not_found"),
        (
            "POST",
            "/api/v1/fsa-claim-proposals/accept",
            {"chosen": [["a", "b"]]},
            400,
            "request.invalid",
        ),
        ("POST", "/api/v1/payroll/templates", {"template": "Acme"}, 400, "request.invalid"),
        ("POST", "/api/v1/payroll/templates/draft", {"schedule": "x"}, 400, "request.invalid"),
        ("DELETE", "/api/v1/payroll/templates/Nobody", None, 404, "payroll.template.not_found"),
        (
            "POST",
            "/api/v1/payroll/pay-changes/preview",
            {"schedule": "x", "start": "2026-01-01", "gross": "1", "amounts": {"a": 1}},
            400,
            "request.invalid",
        ),
    ]
    for method, route, body, status, code in cases:
        got, payload, _ = _raw(remote, method, route, body)
        assert (got, payload["code"]) == (status, code), route
    assert remote.db.get_scheduled(sched.handle).serialize() == before

    request = urllib.request.Request(remote.base + "/api/v1/book")
    with pytest.raises(urllib.error.HTTPError) as untrusted:
        urllib.request.urlopen(request, timeout=10)
    assert untrusted.value.code == 403


def test_a_delete_needs_no_body_but_any_body_must_be_json(remote):
    [sched] = remote.db.iter_scheduled()
    copy = remote.client.duplicate_schedule(DuplicateSchedule(sched.handle))
    request = urllib.request.Request(
        f"{remote.base}/api/v1/schedules/{copy.handle}",
        headers={"X-BreadSched-Token": remote.token},
        method="DELETE",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert json.loads(response.read())["schedule"] == copy.handle
    assert remote.db.get_scheduled(copy.handle) is None
    status, payload, _ = _raw(
        remote,
        "DELETE",
        f"/api/v1/schedules/{sched.handle}",
        body={"colour": "red"},
    )
    assert (status, payload["code"]) == (400, "request.invalid")
    form = urllib.request.Request(
        f"{remote.base}/api/v1/schedules/{sched.handle}",
        data=b"x=1",
        headers={"X-BreadSched-Token": remote.token, "Content-Type": "text/plain"},
        method="DELETE",
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(form, timeout=10)
    assert caught.value.code == 403
    assert remote.db.get_scheduled(sched.handle) is not None


def test_a_malformed_editor_save_is_refused_without_writing(remote):
    before = _snapshot(remote.db)
    for route, body in (
        ("/api/v1/schedules/fixed", {"definition": {"name": "Gym", "amount": 45}}),
        ("/api/v1/schedules/fixed", {"definition": []}),
        ("/api/v1/schedules/formula", {"formulas": {"first": "1"}}),
    ):
        status, payload, _ = _raw(remote, "POST", route, body)
        assert (status, payload["code"]) == (400, "request.invalid"), route
    assert _snapshot(remote.db) == before


def test_a_client_that_missed_changes_is_told_to_reload(remote):
    client = remote.client
    start = client.revision
    [sched] = remote.db.iter_scheduled()
    with remote.db.transaction("Missed") as txn:
        edited = ScheduledTransaction.from_dict(sched.serialize())
        edited.remind_days = 2
        remote.db.commit_scheduled(edited, txn)
    seen: list[Change] = []
    stop = client.subscribe(seen.append)
    _until(lambda: len(seen) == 1)
    stop()
    assert seen == [Change(start + 1, frozenset(), None)]


def test_one_error_envelope_and_one_leaf_encoding():
    missing = ApiError.from_service(ServiceError("schedule.not_found", ("schedule",)))
    invalid = ApiError.from_service(ServiceError("schedule.next.decision", ("decision",)))
    assert (missing.status, invalid.status) == (404, 400)
    assert missing.envelope() == {
        "error": "The scheduled transaction no longer exists",
        "code": "schedule.not_found",
        "fields": ["schedule"],
    }

    class Kind(Enum):
        A = "a"

    assert to_json(
        {"m": Money("1.50"), "d": date(2026, 1, 2), "x": Decimal("0.25"), "k": Kind.A, "t": (1,)}
    ) == {"m": "1.50", "d": "2026-01-02", "x": "0.25", "k": "a", "t": [1]}
    with pytest.raises(TypeError):
        to_json(object())


def test_money_is_written_exactly_and_reads_back_unchanged():
    from breadsched.api.contract import exact_money

    for text, written in (
        ("1800", "1800.00"),
        ("-0.5", "-0.50"),
        ("12.3456", "12.3456"),
        ("0.000001", "0.000001"),
    ):
        assert exact_money(Money(text)) == written
        assert Money(written) == Money(text)
    third = Money(1, 3)
    assert exact_money(third) == "1/3" and Money(exact_money(third)) == third


def test_the_web_error_is_the_book_api_error():
    from breadsched.web.controls import ResourceError, service_error

    assert ResourceError is ApiError
    assert isinstance(service_error(ServiceError("x.not_found")), ApiError)


def test_the_command_line_and_the_browser_give_the_same_json(tmp_path, capsys):
    """One serializer: ``due-review --json`` is the browser's due review, and a
    decision answers the same way through either interface.

    The command line runs while no server holds the book: today a running server
    keeps the book's writer lock, which phase 4 (#358) lets the command line share.
    """
    today = date.today()
    path = tmp_path / "parity.breadsched"
    create_sample_book(path, as_of=(today - timedelta(days=70)))

    assert main(["due-review", str(path), "--as-of", today.isoformat(), "--json"]) == 0
    cli_review = json.loads(capsys.readouterr().out)
    rent = next(item for item in cli_review["schedules"] if item["name"] == "Sample rent")
    assert main(["scheduled-next", str(path), rent["schedule"], "--json"]) == 0
    cli_next = json.loads(capsys.readouterr().out)

    db = DbSQLite()
    db.load(str(path))
    httpd = serve(db, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def call(route, payload=None):
        request = urllib.request.Request(
            base + route,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"X-BreadSched-Token": httpd.token, "Content-Type": "application/json"},
            method="GET" if payload is None else "POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    try:
        assert call("/api/due-review") == cli_review
        listed = call("/api/scheduled")
        definition = next(row for row in listed["definitions"] if row["handle"] == rent["schedule"])
        assert definition["next"] == cli_next
        first = {"schedule": rent["schedule"], "date": rent["items"][0]["date"], "decision": "skip"}
        web_outcome = call("/api/due-review", {"decisions": [first]})
    finally:
        httpd.shutdown()
        httpd.server_close()
        db.close()

    assert main(["due-review", str(path), "--skip", "Sample rent", "--json"]) == 0
    cli_outcome = json.loads(capsys.readouterr().out)
    assert set(cli_outcome) == set(web_outcome) == {"posted", "skipped", "deferred", "transactions"}
    assert (web_outcome["skipped"], cli_outcome["skipped"]) == (1, len(rent["items"]) - 1)


def test_posting_without_a_review_answers_alike_everywhere(tmp_path, capsys):
    """``scheduled --post --json`` and the browser's post-scheduled share one form."""
    path = tmp_path / "post.breadsched"
    create_sample_book(path, as_of=date(2026, 3, 15))
    command = ["scheduled", str(path), "--post", "--all", "--as-of", "2026-05-01", "--json"]
    assert main(command) == 0
    cli = json.loads(capsys.readouterr().out)
    assert cli["posted"] == len(cli["transactions"]) > 0
    assert set(cli["transactions"][0]) == {"transaction", "schedule", "date", "description"}

    db = DbSQLite()
    db.load(str(path))
    httpd, base = _serve(db)
    try:
        request = urllib.request.Request(
            base + "/api/post-scheduled",
            data=b"{}",
            headers={"X-BreadSched-Token": httpd.token, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            web = json.loads(response.read())
    finally:
        httpd.shutdown()
        httpd.server_close()
        db.close()
    assert set(web) == set(cli) == {"posted", "transactions"}
    assert web["posted"] == len(web["transactions"])


def test_open_book_server_closes_the_book(tmp_path):
    path = tmp_path / "open.breadsched"
    create_sample_book(path, as_of=TODAY)
    with open_book_server(str(path), "r") as served:
        assert served.due_review(TODAY) is not None
        db = served.db
    assert db._conn is None


def test_editors_previews_save_nothing_alike(target, book):
    """GTK's editors check what a save would store through the server (decision 0004),
    so a book the host holds previews the same way; nothing is saved."""
    from breadsched.gen.services import transaction_currency

    api, db = target.api, target.db
    book.currency = transaction_currency(db)
    loan = ScheduledTransaction(
        name="Formula loan",
        recurrence=Recurrence(period=PeriodType.MONTH, start=date(2026, 1, 1)),
        splits=[
            ScheduledSplit(book.utilities, formula="payment"),
            ScheduledSplit(book.checking, formula="-payment"),
        ],
    )
    loan.variables = {"payment": "100"}
    with db.transaction("Formula fixture") as txn:
        db.add_scheduled(loan, txn)
    before = _snapshot(db)
    schedule = api.draft_fixed_schedule(SaveFixedSchedule(_fixed(book, remind_days=5)))
    assert (schedule.name, schedule.amount(), schedule.remind_days) == ("Gym", Money("45.00"), 5)
    transaction = api.draft_transaction(SaveTransaction(_transaction(book)))
    assert transaction.description == "Shop" and transaction.is_balanced()
    formula = api.draft_formula_schedule(
        FormulaScheduleInput(
            existing_handle=loan.handle,
            name="Previewed",
            recurrence=loan.recurrence,
            formulas={0: "payment + 25", 1: "-(payment + 25)"},
            variables={"payment": "100"},
            enabled=True,
            auto_create=False,
            placeholder=False,
            growth_policy=loan.growth_policy,
        )
    )
    assert formula.name == "Previewed"
    assert [split.formula for split in formula.splits] == ["payment + 25", "-(payment + 25)"]
    for call, code in (
        (
            lambda: api.draft_fixed_schedule(SaveFixedSchedule(_fixed(book, category="missing"))),
            "schedule.account.not_found",
        ),
        (
            lambda: api.draft_transaction(
                SaveTransaction(_transaction(book), existing_handle="missing")
            ),
            "transaction.not_found",
        ),
    ):
        with pytest.raises(ApiError) as refused:
            call()
        assert refused.value.code == code
    assert _snapshot(db) == before
