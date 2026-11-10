"""The Match windows' one write: allocate by split and amount."""

from __future__ import annotations

from datetime import date

from breadsched.gen.engine import planning
from breadsched.gen.lib import (
    Money,
    PeriodType,
    PlanningResolution,
    Recurrence,
    ScheduledSplit,
    ScheduledTransaction,
    Split,
    Transaction,
)
from breadsched.gen.services import plan_matching as matching
from breadsched.gen.services.plan_matching import AllocateSplit, OccurrenceRequest, SplitNotPlanned

JUNE = date(2026, 6, 1)


def _estimate(db, book, name, account, amount):
    schedule = ScheduledTransaction(
        name=name,
        recurrence=Recurrence(PeriodType.MONTH, interval=1, start=JUNE),
        splits=[
            ScheduledSplit(account, Money(amount)),
            ScheduledSplit(book.checking, -Money(amount)),
        ],
    )
    schedule.placeholder = True
    with db.transaction(name) as txn:
        db.add_scheduled(schedule, txn)
    return schedule


def _receipt(db, book, when, *legs, description="Store", notes=""):
    txn = Transaction(post_date=when, description=description)
    total = Money(0)
    for account, amount, memo in legs:
        txn.add_split(Split(account, Money(amount), memo=memo))
        total = total + Money(amount)
    txn.add_split(Split(book.checking, -total))
    txn.notes = notes
    with db.transaction(description) as handle:
        db.add_transaction(txn, handle)
    return txn


def _leg(db, transaction, account):
    return next(s for s in db.get_transaction(transaction.handle).splits if s.account == account)


def test_a_receipt_counts_toward_two_estimates_and_is_matched_once_both_legs_are(db, book):
    groceries = _estimate(db, book, "Groceries", book.groceries, "600")
    household = _estimate(db, book, "Household", book.utilities, "200")
    receipt = _receipt(
        db, book, date(2026, 6, 5), (book.groceries, "30", "bread"), (book.utilities, "20", "bulbs")
    )
    g_key, h_key = groceries.occurrence_key(JUNE), household.occurrence_key(JUNE)

    food = _leg(db, receipt, book.groceries)
    result = matching.allocate(db, AllocateSplit(receipt.handle, food.handle, g_key))
    assert result.errors == ()
    # One leg of two: still in Review.
    assert result.value.resolution is PlanningResolution.UNRESOLVED
    bulbs = _leg(db, receipt, book.utilities)
    result = matching.allocate(db, AllocateSplit(receipt.handle, bulbs.handle, h_key))
    assert result.value.resolution is PlanningResolution.MATCHED
    assert [leg.unallocated for leg in result.value.legs] == [Money(0), Money(0)]

    view = matching.occurrence_matching(db, g_key).value
    assert (view.expected, view.applied, view.remaining) == (Money(600), Money(30), Money(570))
    assert [(row.split, row.amount) for row in view.allocated] == [(food.handle, Money(30))]
    assert view.period_end == date(2026, 6, 30)


def test_part_of_a_split_counts_and_the_rest_is_offered_again(db, book):
    groceries = _estimate(db, book, "Groceries", book.groceries, "600")
    key = groceries.occurrence_key(JUNE)
    receipt = _receipt(db, book, date(2026, 6, 20), (book.groceries, "50", ""), notes="Party")
    leg = _leg(db, receipt, book.groceries)
    result = matching.allocate(db, AllocateSplit(receipt.handle, leg.handle, key, Money(30)))
    [only] = result.value.legs
    assert (only.unallocated, result.value.resolution) == (Money(20), PlanningResolution.UNRESOLVED)

    view = matching.occurrence_matching(db, key).value
    [candidate] = view.candidates
    assert (candidate.unallocated, candidate.allocated, candidate.notes) == (
        Money(20),
        Money(30),
        "Party",
    )
    assert candidate.other_accounts == ("Assets:Checking",)
    # Changing it to the whole split, then removing it.
    matching.allocate(db, AllocateSplit(receipt.handle, leg.handle, key, Money(50)))
    assert db.get_transaction(receipt.handle).planning_resolution is PlanningResolution.MATCHED
    matching.allocate(db, AllocateSplit(receipt.handle, leg.handle, key, Money(0)))
    stored = db.get_transaction(receipt.handle)
    assert _leg(db, stored, book.groceries).plan_allocations == []
    assert stored.planning_resolution is PlanningResolution.UNRESOLVED


def test_refusals_leave_the_transaction_as_it_was(db, book):
    groceries = _estimate(db, book, "Groceries", book.groceries, "600")
    key = groceries.occurrence_key(JUNE)
    receipt = _receipt(db, book, date(2026, 6, 5), (book.groceries, "30", ""))
    leg = _leg(db, receipt, book.groceries)
    before = db.get_transaction(receipt.handle).serialize()
    cases = {
        "matching.amount.too_large": AllocateSplit(receipt.handle, leg.handle, key, Money(31)),
        "matching.amount.sign": AllocateSplit(receipt.handle, leg.handle, key, Money(-5)),
        "matching.split.not_found": AllocateSplit(receipt.handle, "nope", key),
        "matching.occurrence.not_found": AllocateSplit(receipt.handle, leg.handle, "scheduled:x:1"),
        "matching.transaction.not_found": AllocateSplit("nope", leg.handle, key),
    }
    for code, request in cases.items():
        assert [error.code for error in matching.allocate(db, request).errors] == [code]
    assert db.get_transaction(receipt.handle).serialize() == before


def test_a_split_marked_not_planned_leaves_review_and_a_whole_receipt_so_is_unexpected(db, book):
    receipt = _receipt(db, book, date(2026, 6, 5), (book.groceries, "30", ""))
    leg = _leg(db, receipt, book.groceries)
    result = matching.mark_split_not_planned(db, SplitNotPlanned(receipt.handle, leg.handle))
    assert result.value.resolution is PlanningResolution.UNEXPECTED
    assert result.value.legs[0].not_planned
    result = matching.mark_split_not_planned(
        db, SplitNotPlanned(receipt.handle, leg.handle, not_planned=False)
    )
    assert result.value.resolution is PlanningResolution.UNRESOLVED


def test_closing_an_estimate_early_stops_what_remains_and_reopening_restores_it(db, book):
    groceries = _estimate(db, book, "Groceries", book.groceries, "600")
    key = groceries.occurrence_key(JUNE)
    receipt = _receipt(db, book, date(2026, 6, 5), (book.groceries, "100", ""))
    matching.allocate(
        db, AllocateSplit(receipt.handle, _leg(db, receipt, book.groceries).handle, key)
    )
    today = date(2026, 6, 10)
    assert planning.remainder_event(planning.event_by_key(db, key, today=today)) is not None

    closed = matching.close_occurrence(db, OccurrenceRequest(key)).value
    assert closed.closed and closed.closed_early
    event = planning.event_by_key(db, key, today=today)
    assert event.closed and planning.remainder_event(event) is None
    # Closed early: no more is matched to it until it is reopened.
    late = _receipt(db, book, date(2026, 6, 12), (book.groceries, "10", ""))
    refused = matching.allocate(
        db, AllocateSplit(late.handle, _leg(db, late, book.groceries).handle, key)
    )
    assert [error.code for error in refused.errors] == ["matching.occurrence.closed"]

    reopened = matching.reopen_occurrence(db, OccurrenceRequest(key)).value
    assert not reopened.closed_early
    assert planning.remainder_event(planning.event_by_key(db, key, today=today)) is not None


def test_the_window_from_a_transaction_offers_each_leg_its_own_occurrences(db, book):
    groceries = _estimate(db, book, "Groceries", book.groceries, "600")
    household = _estimate(db, book, "Household", book.utilities, "200")
    receipt = _receipt(
        db, book, date(2026, 6, 5), (book.groceries, "30", "bread"), (book.utilities, "20", "")
    )
    view = matching.transaction_matching(db, receipt.handle).value
    offers = {leg.account_name: [item.key for item in leg.candidates] for leg in view.legs}
    assert offers["Expenses:Groceries"][0] == groceries.occurrence_key(JUNE)
    assert offers["Expenses:Utilities"][0] == household.occurrence_key(JUNE)
    assert [leg.memo for leg in view.legs] == ["bread", ""]


def test_the_command_line_allocates_part_of_a_leg_and_closes_an_occurrence(
    db, book, tmp_path, capsys
):
    import json

    from breadsched.cli.main import main

    groceries = _estimate(db, book, "Groceries", book.groceries, "600")
    receipt = _receipt(db, book, date(2026, 6, 5), (book.groceries, "50", ""))
    key = groceries.occurrence_key(JUNE)
    path = tmp_path / "match.breadsched"
    db.backup_to(str(path))

    assert main(["plan-allocate", str(path), receipt.handle[:10], key, "--amount", "30"]) == 0
    out = capsys.readouterr().out
    assert "[0] Expenses:Groceries  50.00  unallocated 20.00" in out
    assert f"→ {key}  30.00" in out
    assert main(["plan-allocate", str(path), f"{receipt.handle}:0", key, "--json"]) == 0
    [leg] = json.loads(capsys.readouterr().out)["legs"]
    assert leg["unallocated"] == "0.00"
    assert main(["plan-allocate", str(path), receipt.handle, key, "--amount", "99"]) != 0
    capsys.readouterr()

    assert main(["plan-close", str(path), key]) == 0
    assert "closed; expected 600.00, applied 50.00, remaining 550.00" in capsys.readouterr().out
    assert main(["plan-close", str(path), key, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["closed_early"] is True
    # Reopened, it is no longer closed early, though June's period has ended.
    assert main(["plan-close", str(path), key, "--reopen", "--json"]) == 0
    reopened = json.loads(capsys.readouterr().out)
    assert (reopened["closed_early"], reopened["closed"]) == (False, True)


def test_a_split_counted_in_part_is_still_offered_its_occurrence_for_the_rest(db, book):
    groceries = _estimate(db, book, "Groceries", book.groceries, "600")
    key = groceries.occurrence_key(JUNE)
    receipt = _receipt(db, book, date(2026, 6, 20), (book.groceries, "50", ""))
    leg = _leg(db, receipt, book.groceries)
    matching.allocate(db, AllocateSplit(receipt.handle, leg.handle, key, Money(30)))
    [offered] = matching.transaction_matching(db, receipt.handle).value.legs
    assert [item.key for item in offered.candidates][:1] == [key]
    matching.allocate(db, AllocateSplit(receipt.handle, leg.handle, key))
    [whole] = matching.transaction_matching(db, receipt.handle).value.legs
    assert whole.unallocated == Money(0)


def test_a_schedule_opens_on_its_latest_occurrence_not_after_today():
    schedule = ScheduledTransaction(
        name="Groceries",
        recurrence=Recurrence(PeriodType.MONTH, interval=1, start=JUNE),
        splits=[],
    )
    assert schedule.current_occurrence(date(2026, 7, 15)) == date(2026, 7, 1)
    schedule.skipped = [date(2026, 7, 1)]
    assert schedule.current_occurrence(date(2026, 7, 15)) == JUNE
    # Before it begins: its first occurrence.
    assert schedule.current_occurrence(date(2026, 5, 1)) == JUNE


def test_review_groups_actuals_by_their_first_category_uncategorized_last():
    from types import SimpleNamespace

    from breadsched.presentation import UNCATEGORIZED_REVIEW_GROUP, review_groups

    queue = [
        SimpleNamespace(handle="a", categories=("Expenses:Groceries",)),
        SimpleNamespace(handle="b", categories=()),
        SimpleNamespace(handle="c", categories=("Expenses:Dining", "Expenses:Groceries")),
        SimpleNamespace(handle="d", categories=("Expenses:Groceries",)),
    ]
    assert [
        (name, [item.handle for item in members]) for name, members in review_groups(queue)
    ] == [
        ("Expenses:Dining", ["c"]),
        ("Expenses:Groceries", ["a", "d"]),
        (UNCATEGORIZED_REVIEW_GROUP, ["b"]),
    ]
