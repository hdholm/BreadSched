"""The book server: one API over an open book for every interface (#354-#357).

**Experimental** (branch ``claude/experimental-book-server``). The same contract
runs against the in-process server and against the HTTP client talking to a running
web server's ``/api/v1``: each observes every change to its book, refuses a bad
write without changing anything, and reports failures in one error envelope. The
command line and the browser give the same JSON.
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
            api=HttpBookClient(base, httpd.token), db=db, remote=True, base=base, token=httpd.token
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
    assert seen[-1].kinds == frozenset({"transaction"})
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


# ------------------------------------------------------------ in process only


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
    yield SimpleNamespace(
        base=base, token=httpd.token, db=db, client=HttpBookClient(base, httpd.token)
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
