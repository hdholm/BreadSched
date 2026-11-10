"""Actuals count toward planned occurrences by split and amount."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from breadsched.gen.db.sqlite import SCHEMA_VERSION, DbSQLite
from breadsched.gen.db.verification import verify_domain
from breadsched.gen.engine import planning
from breadsched.gen.engine.budget_jars import budget_jars
from breadsched.gen.engine.plan_allocations import account_classes, occurrence_actuals, plan_legs
from breadsched.gen.engine.schedule import due_occurrences, post_occurrences
from breadsched.gen.lib import (
    Account,
    AccountType,
    Money,
    PeriodType,
    PlanningResolution,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    Split,
    Transaction,
)
from breadsched.gen.lib.transaction import PlanAllocation, fitting_allocations
from breadsched.gen.services.review import ReviewOccurrence, match_review

JUNE = date(2026, 6, 1)


def _schedule(db, name, splits, *, placeholder=False, start=JUNE):
    schedule = ScheduledTransaction(
        name=name,
        recurrence=Recurrence(PeriodType.MONTH, interval=1, start=start),
        splits=splits,
    )
    schedule.placeholder = placeholder
    with db.transaction(name) as txn:
        db.add_scheduled(schedule, txn)
    return schedule


def _groceries(db, book):
    """About 600 of groceries a month, an estimate dated the 1st."""
    return _schedule(
        db,
        "Groceries",
        [ScheduledSplit(book.groceries, Money(600)), ScheduledSplit(book.checking, Money(-600))],
        placeholder=True,
    )


def _purchase(db, book, when, *legs, description="Market"):
    """A card-free purchase from checking: ``legs`` are (account, amount) pairs."""
    txn = Transaction(post_date=when, description=description)
    total = sum((Money(amount) for _account, amount in legs), Money(0))
    txn.splits = [Split(account, Money(amount)) for account, amount in legs]
    txn.splits.append(Split(book.checking, -total))
    with db.transaction(description) as handle:
        db.add_transaction(txn, handle)
    return txn


# ----------------------------------------------------------------------- storage


def test_an_allocation_round_trips_and_an_unallocated_split_stores_nothing_new():
    plain = Split("groceries", Money("50"))
    assert "plan_allocations" not in plain.serialize()
    plain.plan_allocations.append(
        PlanAllocation("scheduled:g:2026-06-01", Money("30"), JUNE, Money("600"))
    )
    restored = Split.from_dict(json.loads(json.dumps(plain.serialize())))
    assert restored.plan_allocations == [
        PlanAllocation("scheduled:g:2026-06-01", Money("30"), JUNE, Money("600"))
    ]


def test_allocations_are_cut_to_fit_a_lowered_split():
    first = PlanAllocation("a", Money("30"))
    second = PlanAllocation("b", Money("20"))
    assert fitting_allocations([first, second], Money("50")) == [first, second]
    assert fitting_allocations([first, second], Money("40")) == [
        first,
        PlanAllocation("b", Money("10")),
    ]
    assert fitting_allocations([first, second], Money("25")) == [PlanAllocation("a", Money("25"))]
    # The other sign: a split turned into a refund keeps none.
    assert fitting_allocations([first], Money("-30")) == []


def test_closing_an_occurrence_is_stored_on_its_schedule_only_when_set(db, book):
    schedule = _groceries(db, book)
    assert "closed_occurrences" not in schedule.serialize()
    schedule.closed_occurrences.append(JUNE)
    with db.transaction("Close") as txn:
        db.commit_scheduled(schedule, txn)
    assert db.get_scheduled(schedule.handle).closed_occurrences == [JUNE]


# ------------------------------------------------------------- an estimate fills


def test_an_estimate_fills_from_several_matched_purchases_and_keeps_its_remainder(db, book):
    groceries = _groceries(db, book)
    key = groceries.occurrence_key(JUNE)
    for when, amount in ((date(2026, 6, 3), "120"), (date(2026, 6, 10), "180")):
        purchase = _purchase(db, book, when, (book.groceries, amount))
        result = match_review(db, ReviewOccurrence(purchase.handle, key))
        assert result.errors == ()
        stored = db.get_transaction(purchase.handle)
        assert stored.planning_resolution is PlanningResolution.MATCHED
        # The category leg is allocated in full; the checking leg is not.
        [leg] = [split for split in stored.splits if split.plan_allocations]
        assert leg.account == book.groceries
        assert leg.plan_allocations == [PlanAllocation(key, Money(amount), JUNE, Money(600))]

    event = planning.event_by_key(db, key, today=date(2026, 6, 15))
    assert [item.amount for item in event.actuals] == [Money(120), Money(180)]
    assert (event.expected_amount, event.applied_amount, event.remaining_amount) == (
        Money(600),
        Money(300),
        Money(300),
    )
    assert event.closed is False and event.matchable
    # Projection still expects the other 300, after the last purchase.
    rest = planning.remainder_event(event)
    assert rest is not None
    assert (rest.planned_date, rest.expected_amount, rest.remainder_of) == (
        date(2026, 6, 10),
        Money(300),
        key,
    )
    assert {split.account: split.amount for split in rest.expected_splits} == {
        book.groceries: Money(300),
        book.checking: Money(-300),
    }
    events = planning.scheduled_events(
        db, JUNE, date(2026, 6, 30), remainders=True, today=date(2026, 6, 15)
    )
    assert [item.key for item in events] == [key, f"{key}#remaining"]
    # Two purchases on one estimate are not a duplicate occurrence.
    assert not [issue for issue in verify_domain(db) if issue.code.startswith("scheduled.")]


def test_an_estimate_lapses_when_its_period_ends_but_takes_a_late_purchase(db, book):
    groceries = _groceries(db, book)
    key = groceries.occurrence_key(JUNE)
    purchase = _purchase(db, book, date(2026, 6, 3), (book.groceries, "100"))
    match_review(db, ReviewOccurrence(purchase.handle, key))

    event = planning.event_by_key(db, key, today=date(2026, 7, 1))
    assert event.closed and event.matchable
    assert planning.remainder_event(event) is None
    assert key not in {
        item.key
        for item in planning.scheduled_events(db, JUNE, date(2026, 6, 30), today=date(2026, 7, 1))
        if not item.closed
    }
    late = _purchase(db, book, date(2026, 7, 1), (book.groceries, "40"))
    assert match_review(db, ReviewOccurrence(late.handle, key)).errors == ()


def test_a_commitment_matched_as_a_whole_is_closed_even_when_paid_less(db, book):
    rent = _schedule(
        db,
        "Rent",
        [ScheduledSplit(book.rent, Money(1500)), ScheduledSplit(book.checking, Money(-1500))],
    )
    key = rent.occurrence_key(JUNE)
    paid = _purchase(db, book, date(2026, 6, 2), (book.rent, "1400"), description="Rent")
    assert match_review(db, ReviewOccurrence(paid.handle, key)).errors == ()
    event = planning.event_by_key(db, key, today=date(2026, 6, 3))
    assert event.closed and not event.matchable
    assert event.remaining_amount == Money(100)
    other = _purchase(db, book, date(2026, 6, 3), (book.rent, "100"), description="Rent")
    assert [
        error.code for error in match_review(db, ReviewOccurrence(other.handle, key)).errors
    ] == ["review.occurrence.resolved"]


def test_a_receipt_split_between_two_estimates_counts_its_part_toward_each(db, book):
    groceries = _groceries(db, book)
    household = _schedule(
        db,
        "Household",
        [ScheduledSplit(book.utilities, Money(200)), ScheduledSplit(book.checking, Money(-200))],
        placeholder=True,
    )
    receipt = _purchase(
        db,
        book,
        date(2026, 6, 5),
        (book.groceries, "30"),
        (book.utilities, "20"),
        description="Store",
    )
    g_key, h_key = groceries.occurrence_key(JUNE), household.occurrence_key(JUNE)
    receipt = db.get_transaction(receipt.handle)
    for split in receipt.splits:
        if split.account == book.groceries:
            split.plan_allocations = [PlanAllocation(g_key, split.value, JUNE, Money(600))]
        elif split.account == book.utilities:
            split.plan_allocations = [PlanAllocation(h_key, split.value, JUNE, Money(200))]
    with db.transaction("Allocate") as txn:
        db.commit_transaction(receipt, txn)

    found = occurrence_actuals(db)
    assert [part.amount for part in found[g_key]] == [Money(30)]
    assert [part.amount for part in found[h_key]] == [Money(20)]
    assert planning.event_by_key(db, g_key, today=date(2026, 6, 6)).remaining_amount == Money(570)

    # Each jar draws only its own leg, and nothing is left unmatched.
    report = budget_jars(db, JUNE, date(2026, 6, 30), today=date(2026, 6, 6))
    drawn = {
        jar.name: sum((period.actual for period in jar.periods), Money(0))
        for bundle in report.accounts
        for jar in bundle.jars
    }
    assert drawn["Groceries"] == Money(30)
    assert drawn["Household"] == Money(20)


def test_a_receipt_allocated_in_part_leaves_the_rest_to_unmatched_spending(db, book):
    groceries = _groceries(db, book)
    key = groceries.occurrence_key(JUNE)
    receipt = _purchase(db, book, date(2026, 6, 5), (book.groceries, "50"))
    receipt = db.get_transaction(receipt.handle)
    [leg] = [split for split in receipt.splits if split.account == book.groceries]
    leg.plan_allocations = [PlanAllocation(key, Money(30), JUNE, Money(600))]
    with db.transaction("Allocate") as txn:
        db.commit_transaction(receipt, txn)
    [part] = occurrence_actuals(db)[key]
    assert (part.amount, part.whole) == (Money(30), False)

    report = budget_jars(db, JUNE, date(2026, 6, 30), today=date(2026, 6, 6))
    [jar] = [j for bundle in report.accounts for j in bundle.jars if j.name == "Groceries"]
    # The occurrence draws its 30; the other 20 is unmatched spending, which the
    # account's only estimate takes, as any unmatched posting to it (#312).
    assert sorted(
        (event.occurrence, event.amount) for event in jar.events if event.kind == "actual"
    ) == [("", Money(20)), (key, Money(30))]


def test_editing_a_split_lower_cuts_its_allocation_to_fit(db, book):
    from breadsched.gen.lib.amount import Amount
    from breadsched.gen.services.transactions import (
        SaveTransaction,
        TransactionInput,
        TransactionSplitInput,
        save_transaction,
        transaction_currency,
    )

    groceries = _groceries(db, book)
    key = groceries.occurrence_key(JUNE)
    purchase = _purchase(db, book, date(2026, 6, 3), (book.groceries, "120"))
    match_review(db, ReviewOccurrence(purchase.handle, key))
    stored = db.get_transaction(purchase.handle)
    currency = transaction_currency(db, stored.currency)
    request = SaveTransaction(
        TransactionInput(
            post_date=stored.post_date,
            description=stored.description,
            splits=tuple(
                TransactionSplitInput(
                    split.account,
                    Amount(Money(80) if split.value > 0 else Money(-80), currency),
                    handle=split.handle,
                )
                for split in stored.splits
            ),
        ),
        existing_handle=stored.handle,
    )
    assert save_transaction(db, request).errors == ()
    [leg] = [s for s in db.get_transaction(purchase.handle).splits if s.plan_allocations]
    assert leg.plan_allocations[0].amount == Money(80)


# ----------------------------------------------------------------------- posting


def test_posting_a_schedule_allocates_its_plan_legs(db, book):
    pay = _schedule(
        db,
        "Pay",
        [
            ScheduledSplit(book.checking, Money(1200)),
            ScheduledSplit(book.utilities, Money(300)),
            ScheduledSplit(book.salary, Money(-1500)),
        ],
    )
    [occurrence] = [
        item for item in due_occurrences(db, as_of=JUNE) if item.schedule.handle == pay.handle
    ]
    [posted] = post_occurrences(db, [occurrence])
    stored = db.get_transaction(posted.handle)
    key = pay.occurrence_key(JUNE)
    allocated = {
        split.account: split.plan_allocations[0].amount
        for split in stored.splits
        if split.plan_allocations
    }
    # The income and the withholding are what the paycheck was for.
    assert allocated == {book.salary: Money(-1500), book.utilities: Money(300)}
    [part] = occurrence_actuals(db)[key]
    assert (part.amount, part.whole) == (Money(1500), True)
    assert planning.event_by_key(db, key).closed


def test_whole_transaction_legs_fall_back_to_positive_legs_for_a_transfer(db, book):
    txn = Transaction.simple(JUNE, "Save", book.savings, book.checking, "100")
    legs = plan_legs(txn, account_classes(db))
    assert [(split.account, split.value) for split in legs] == [(book.savings, Money(100))]


# --------------------------------------------------------------------- migration


def test_a_schema_13_book_writes_its_plan_links_as_allocations(tmp_path):
    path = tmp_path / "plan.breadsched"
    db = DbSQLite()
    db.load(str(path))
    with db.transaction("Setup") as txn:
        root = Account(name="Root", atype=AccountType.ROOT)
        db.add_account(root, txn)
        checking = Account(name="Checking", atype=AccountType.BANK, parent=root.handle)
        food = Account(name="Food", atype=AccountType.EXPENSE, parent=root.handle)
        pay = Account(name="Pay", atype=AccountType.INCOME, parent=root.handle)
        for account in (checking, food, pay):
            db.add_account(account, txn)
        groceries = ScheduledTransaction(
            name="Groceries",
            recurrence=Recurrence(PeriodType.MONTH, interval=1, start=JUNE),
            splits=[
                ScheduledSplit(food.handle, Money(600)),
                ScheduledSplit(checking.handle, Money(-600)),
            ],
        )
        groceries.placeholder = True
        db.add_scheduled(groceries, txn)
        matched = Transaction.simple(date(2026, 6, 3), "Market", food.handle, checking.handle, "55")
        matched.planned_occurrence = groceries.occurrence_key(JUNE)
        matched.planned_for = JUNE
        matched.planned_amount = Money(600)
        matched.planning_resolution = PlanningResolution.MATCHED
        posted = Transaction.simple(
            date(2026, 6, 1), "Paycheck", checking.handle, pay.handle, "900"
        )
        posted.scheduled_from = "payroll"
        plain = Transaction.simple(date(2026, 6, 4), "Cash", food.handle, checking.handle, "7")
        for item in (matched, posted, plain):
            db.add_transaction(item, txn)
    db.close()
    with sqlite3.connect(path) as raw:
        for (handle,) in raw.execute("SELECT handle FROM txn").fetchall():
            data = json.loads(
                raw.execute("SELECT blob FROM txn WHERE handle=?", (handle,)).fetchone()[0]
            )
            for split in data["splits"]:
                split.pop("plan_allocations", None)
            raw.execute("UPDATE txn SET blob=? WHERE handle=?", (json.dumps(data), handle))
        raw.execute("UPDATE metadata SET value='13' WHERE key='schema_version'")
        raw.execute("DELETE FROM schema_migration")
        raw.execute("INSERT INTO schema_migration(version, applied_at) VALUES (13, 'test')")

    migrated = DbSQLite()
    migrated.load(str(path))
    try:
        assert migrated.get_metadata("schema_version") == SCHEMA_VERSION == 14
        assert migrated.migration_backup is not None
        food_leg = next(
            split
            for split in migrated.get_transaction(matched.handle).splits
            if split.account == food.handle
        )
        assert food_leg.plan_allocations == [
            PlanAllocation(groceries.occurrence_key(JUNE), Money(55), JUNE, Money(600))
        ]
        income_leg = next(
            split
            for split in migrated.get_transaction(posted.handle).splits
            if split.plan_allocations
        )
        assert income_leg.account == pay.handle
        assert income_leg.plan_allocations[0].occurrence == "scheduled:payroll:2026-06-01"
        assert not any(
            split.plan_allocations for split in migrated.get_transaction(plain.handle).splits
        )
        # The occurrence counts what it counted before.
        event = planning.event_by_key(migrated, groceries.occurrence_key(JUNE), today=JUNE)
        assert event.applied_amount == Money(55)
    finally:
        migrated.close()


# ------------------------------------------------------- ranking


def test_a_purchase_late_in_the_month_is_offered_its_estimate_first(db, book):
    from breadsched.gen.engine import review_explain

    groceries = _groceries(db, book)
    # A commitment on the same funding account, nearer in date and amount.
    _schedule(
        db,
        "Phone",
        [ScheduledSplit(book.utilities, Money(80)), ScheduledSplit(book.checking, Money(-80))],
        start=date(2026, 6, 24),
    )
    purchase = _purchase(db, book, date(2026, 6, 25), (book.groceries, "80"))
    found = planning.match_candidates(db, purchase)
    # The estimate dated the 1st is 24 days away, outside the seven-day window, but
    # its period holds the date and it shares the category.
    assert [candidate.event.key for candidate in found][0] == groceries.occurrence_key(JUNE)
    first = found[0]
    assert (first.same_category, first.in_period, first.fits, first.same_funding) == (
        True,
        True,
        True,
        True,
    )
    assert first.period_end == date(2026, 6, 30)
    [explained, *_rest] = review_explain.explain_candidates(db, purchase)
    assert explained.confidence == review_explain.CLOSE
    assert explained.reasons[:4] == (
        "Same category: Groceries",
        "An estimate, which many purchases fill",
        "In its period, 2026-06-01 to 2026-06-30",
        "Fits what remains: 600.00 of 600.00 remains",
    )


def test_an_estimate_ranks_above_a_commitment_of_the_same_category(db, book):
    groceries = _groceries(db, book)
    delivery = _schedule(
        db,
        "Grocery box",
        [ScheduledSplit(book.groceries, Money(60)), ScheduledSplit(book.checking, Money(-60))],
        start=date(2026, 6, 10),
    )
    purchase = _purchase(db, book, date(2026, 6, 10), (book.groceries, "60"))
    keys = [candidate.event.key for candidate in planning.match_candidates(db, purchase)]
    assert keys == [groceries.occurrence_key(JUNE), delivery.occurrence_key(date(2026, 6, 10))]


def test_what_remains_decides_between_two_estimates_and_shared_funding_alone_ranks_last(db, book):
    groceries = _groceries(db, book)
    small = _schedule(
        db,
        "Snacks",
        [ScheduledSplit(book.groceries, Money(50)), ScheduledSplit(book.checking, Money(-50))],
        placeholder=True,
    )
    other = _schedule(
        db,
        "Utilities allowance",
        [ScheduledSplit(book.utilities, Money(90)), ScheduledSplit(book.checking, Money(-90))],
        placeholder=True,
    )
    purchase = _purchase(db, book, date(2026, 6, 12), (book.groceries, "90"))
    found = planning.match_candidates(db, purchase)
    # 90 fits what remains of groceries (600), not of snacks (50); utilities shares
    # only the funding account.
    assert [candidate.event.source_handle for candidate in found] == [
        groceries.handle,
        small.handle,
        other.handle,
    ]
    assert [candidate.fits for candidate in found] == [True, False, True]
    assert [candidate.same_category for candidate in found] == [True, True, False]


def test_the_queue_shows_what_each_actual_was_for_and_its_notes(db, book):
    from breadsched.gen.services.review import review_detail, review_queue

    purchase = _purchase(
        db,
        book,
        date(2026, 6, 5),
        (book.groceries, "30"),
        (book.utilities, "20"),
        description="Store",
    )
    stored = db.get_transaction(purchase.handle)
    stored.notes = "Bulbs and bread"
    stored.splits[0].memo = "bread"
    with db.transaction("Note") as txn:
        db.commit_transaction(stored, txn)
    [row] = [item for item in review_queue(db) if item.handle == purchase.handle]
    assert row.categories == ("Expenses:Groceries", "Expenses:Utilities")
    assert row.funding == ("Assets:Checking",)
    assert row.notes == "Bulbs and bread"
    detail = review_detail(db, purchase.handle).value
    assert [(split.account_name, split.amount, split.memo) for split in detail.splits] == [
        ("Expenses:Groceries", Money(30), "bread"),
        ("Expenses:Utilities", Money(20), ""),
        ("Assets:Checking", Money(-50), ""),
    ]
