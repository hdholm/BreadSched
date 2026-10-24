"""Statement reconciliation is shared, exact, atomic, and auditable."""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.engine import reconciliation
from breadsched.gen.lib import (
    Account,
    AccountType,
    Money,
    ReconcileState,
    ReconciliationStatus,
    Transaction,
)
from breadsched.gen.services import (
    AddStatementEntry,
    ReconciliationAction,
    StartReconciliation,
    UpdateReconciliation,
    add_statement_entry,
    cancel_reconciliation,
    complete_reconciliation,
    start_reconciliation,
    update_reconciliation,
)
from breadsched.gen.services.contracts import ServiceError


@pytest.fixture
def statement_book(db):
    root = Account(name="Root", atype=AccountType.ROOT)
    bank = Account(name="Household account", atype=AccountType.BANK, parent=root.handle)
    expense = Account(name="Household expense", atype=AccountType.EXPENSE, parent=root.handle)
    income = Account(name="Household income", atype=AccountType.INCOME, parent=root.handle)
    opening = Transaction.simple(date(2026, 1, 1), "Opening", bank.handle, income.handle, "1000")
    opening.split_for(bank.handle).reconcile = ReconcileState.RECONCILED
    opening.split_for(bank.handle).reconcile_date = date(2025, 12, 31)
    cleared = Transaction.simple(
        date(2026, 1, 10), "Cleared expense", expense.handle, bank.handle, "200"
    )
    cleared.split_for(bank.handle).reconcile = ReconcileState.CLEARED
    pending = Transaction.simple(
        date(2026, 1, 20), "Pending expense", expense.handle, bank.handle, "50"
    )
    future = Transaction.simple(
        date(2026, 2, 1), "Future expense", expense.handle, bank.handle, "25"
    )
    with db.transaction("Statement fixture") as txn:
        for account in (root, bank, expense, income):
            db.add_account(account, txn)
        for transaction in (opening, cleared, pending, future):
            db.add_transaction(transaction, txn)
    return bank, cleared, pending, future


def test_start_selects_cleared_activity_and_calculates_exact_difference(db, statement_book):
    bank, cleared, pending, _future = statement_book

    session = reconciliation.start(db, bank.handle, date(2026, 1, 31), "750")
    state = reconciliation.summary(db, session)

    assert session.status is ReconciliationStatus.OPEN
    assert state.opening_balance == Money("1000")
    assert state.selected_balance == Money("800")
    assert state.difference == Money("-50")
    # #362: a later entry is listed, marked, in case its date is wrong.
    assert [(item.description, item.after_statement) for item in state.candidates] == [
        ("Cleared expense", False),
        ("Pending expense", False),
        ("Future expense", True),
    ]
    assert session.selected_splits == [cleared.split_for(bank.handle).handle]

    balanced = reconciliation.set_selection(
        db,
        session.handle,
        [item.split for item in state.candidates if not item.after_statement],
    )
    assert balanced.balanced
    assert balanced.selected_balance == Money("750")
    assert pending.split_for(bank.handle).reconcile is ReconcileState.NOT_RECONCILED


def test_typed_reconciliation_service_preserves_start_update_complete_workflow(db, statement_book):
    bank, _cleared, _pending, _future = statement_book

    started = start_reconciliation(
        db,
        StartReconciliation(bank.handle, date(2026, 1, 31), Money("750")),
    )
    assert started.value is not None
    updated = update_reconciliation(
        db,
        UpdateReconciliation(
            started.value.reconciliation.handle,
            selected_splits=tuple(
                item.split for item in started.value.candidates if not item.after_statement
            ),
        ),
    )
    assert updated.value is not None and updated.value.balanced

    completed = complete_reconciliation(
        db,
        ReconciliationAction(started.value.reconciliation.handle),
    )

    assert completed.value is not None
    assert completed.value.status is ReconciliationStatus.COMPLETED


def test_typed_reconciliation_service_returns_stable_errors_without_writing(db, statement_book):
    bank, _cleared, _pending, _future = statement_book
    before = len(db.undo_stack)

    missing = start_reconciliation(
        db,
        StartReconciliation("missing", date(2026, 1, 31), Money("750")),
    )
    assert missing.errors == (ServiceError("reconciliation.account.not_found", ("account",)),)
    assert len(db.undo_stack) == before

    started = start_reconciliation(
        db,
        StartReconciliation(bank.handle, date(2026, 1, 31), Money("750")),
    )
    assert started.value is not None
    unbalanced = complete_reconciliation(
        db,
        ReconciliationAction(started.value.reconciliation.handle),
    )
    assert unbalanced.errors == (
        ServiceError("reconciliation.unbalanced", ("selected_splits", "ending_balance")),
    )


def test_completion_reconciles_selected_splits_atomically_and_can_be_reopened(db, statement_book):
    bank, _cleared, _pending, _future = statement_book
    session = reconciliation.start(db, bank.handle, date(2026, 1, 31), "750")
    state = reconciliation.summary(db, session)
    reconciliation.set_selection(
        db, session.handle, [item.split for item in state.candidates if not item.after_statement]
    )

    completed = reconciliation.complete(db, session.handle)

    assert completed.status is ReconciliationStatus.COMPLETED
    assert completed.completed_at is not None
    assert [event.action for event in completed.audit_events] == ["opened", "completed"]
    for candidate in state.candidates:
        if candidate.after_statement:
            continue
        transaction = db.get_transaction(candidate.transaction)
        split = next(item for item in transaction.splits if item.handle == candidate.split)
        assert split.reconcile is ReconcileState.RECONCILED
        assert split.reconcile_date == date(2026, 1, 31)
    assert db.verify_book() == []

    reopened = reconciliation.reopen(db, session.handle)
    assert reopened.status is ReconciliationStatus.OPEN
    assert [event.action for event in reopened.audit_events] == [
        "opened",
        "completed",
        "reopened",
    ]
    reopened_state = reconciliation.summary(db, reopened)
    assert reopened_state.balanced
    assert all(
        item.state is ReconcileState.CLEARED
        for item in reopened_state.candidates
        if not item.after_statement
    )


def test_unbalanced_completion_is_refused(db, statement_book):
    bank, _cleared, _pending, _future = statement_book
    session = reconciliation.start(db, bank.handle, date(2026, 1, 31), "750")

    with pytest.raises(ValueError, match="out of balance"):
        reconciliation.complete(db, session.handle)

    assert db.get_reconciliation(session.handle).status is ReconciliationStatus.OPEN


def test_cancel_retains_auditable_session_without_changing_splits(db, statement_book):
    bank, cleared, _pending, _future = statement_book
    bank_split = cleared.split_for(bank.handle)
    session = reconciliation.start(db, bank.handle, date(2026, 1, 31), "800")

    cancelled = reconciliation.cancel(db, session.handle)

    assert cancelled.status is ReconciliationStatus.CANCELLED
    assert cancelled.cancelled_at is not None
    assert [event.action for event in cancelled.audit_events] == ["opened", "cancelled"]
    stored = db.get_transaction(cleared.handle).split_for(bank.handle)
    assert stored.reconcile is bank_split.reconcile


def test_only_one_open_session_and_monotonic_completed_statements(db, statement_book):
    bank, _cleared, _pending, _future = statement_book
    session = reconciliation.start(db, bank.handle, date(2026, 1, 31), "800")
    with pytest.raises(ValueError, match="open reconciliation"):
        reconciliation.start(db, bank.handle, date(2026, 2, 28), "800")

    reconciliation.complete(db, session.handle)
    with pytest.raises(ValueError, match="must follow"):
        reconciliation.start(db, bank.handle, date(2026, 1, 31), "800")


def test_the_register_toggles_cleared_and_keeps_an_open_statement_in_step(db, statement_book):
    from breadsched.gen.services import ToggleCleared, toggle_cleared

    bank, cleared, pending, future = statement_book
    split = pending.split_for(bank.handle).handle
    result = toggle_cleared(db, ToggleCleared(pending.handle, split))
    assert result.value is ReconcileState.CLEARED
    assert db.get_transaction(pending.handle).split_for(bank.handle).reconcile is (
        ReconcileState.CLEARED
    )
    assert toggle_cleared(db, ToggleCleared(pending.handle, split)).value is (
        ReconcileState.NOT_RECONCILED
    )

    # With a statement open, clearing a candidate selects it and unclearing drops it;
    # an entry after the statement date is cleared but not selected.
    session = reconciliation.start(db, bank.handle, date(2026, 1, 31), "750")
    assert toggle_cleared(db, ToggleCleared(pending.handle, split)).ok
    assert split in db.get_reconciliation(session.handle).selected_splits
    assert reconciliation.summary(db, session.handle).balanced
    assert toggle_cleared(db, ToggleCleared(pending.handle, split)).ok
    assert split not in db.get_reconciliation(session.handle).selected_splits
    later = future.split_for(bank.handle).handle
    assert toggle_cleared(db, ToggleCleared(future.handle, later)).ok
    assert later not in db.get_reconciliation(session.handle).selected_splits


def test_reconciled_entries_change_only_through_their_statement(db, statement_book):
    from breadsched.gen.services import ToggleCleared, toggle_cleared

    bank, *_rest = statement_book
    opening = next(t for t in db.iter_transactions() if t.description == "Opening")
    before = opening.serialize()
    refused = toggle_cleared(
        db, ToggleCleared(opening.handle, opening.split_for(bank.handle).handle)
    )
    assert refused.errors == (ServiceError("reconciliation.split.locked", ("split",)),)
    assert db.get_transaction(opening.handle).serialize() == before
    missing = toggle_cleared(db, ToggleCleared(opening.handle, "no-such-split"))
    assert missing.errors[0].code == "reconciliation.split.missing"
    gone = toggle_cleared(db, ToggleCleared("no-such-transaction", "x"))
    assert gone.errors[0].code == "transaction.not_found"


def test_funds_in_and_out_total_the_checked_entries_separately(db, statement_book):
    """#362: GnuCash-style columns, each with its own checked total."""
    bank, _cleared, pending, _future = statement_book
    session = reconciliation.start(db, bank.handle, date(2026, 1, 31), "750")
    deposit = Transaction.simple(
        date(2026, 1, 25), "Refund", bank.handle, pending.splits[0].account, "30"
    )
    with db.transaction("Refund") as txn:
        db.add_transaction(deposit, txn)
    state = reconciliation.summary(db, session.handle)
    assert {item.description: item.funds_in for item in state.candidates} == {
        "Cleared expense": False,
        "Pending expense": False,
        "Refund": True,
        "Future expense": False,
    }
    state = reconciliation.set_selection(
        db, session.handle, [item.split for item in state.candidates if not item.after_statement]
    )
    assert state.selected_in == Money("30")
    assert state.selected_out == Money("250")
    assert state.selected_balance == Money("780")


def test_a_checked_later_entry_is_reconciled_with_the_statement(db, statement_book):
    """#362: an entry dated after the statement can still belong on it."""
    bank, _cleared, pending, future = statement_book
    session = reconciliation.start(db, bank.handle, date(2026, 1, 31), "725")
    state = reconciliation.set_selection(
        db, session.handle, [item.split for item in reconciliation.summary(db, session).candidates]
    )
    assert state.balanced
    reconciliation.complete(db, session.handle)
    stored = db.get_transaction(future.handle).split_for(bank.handle)
    assert stored.reconcile is ReconcileState.RECONCILED
    assert stored.reconcile_date == date(2026, 1, 31)
    # It counts toward the reconciled balance as of that statement, not after it.
    assert reconciliation.reconciled_balance(db, bank.handle, date(2026, 1, 31)) == Money("725")
    assert reconciliation.reconciled_balance(db, bank.handle, date(2026, 1, 15)) == Money("800")
    nxt = reconciliation.start(db, bank.handle, date(2026, 2, 28), "725")
    assert reconciliation.summary(db, nxt).opening_balance == Money("725")
    assert reconciliation.summary(db, nxt).candidates == ()


def test_the_starting_balance_is_the_reconciled_balance_on_the_statement_date(db, statement_book):
    """#362: shown before starting, to compare with the statement's previous balance."""
    bank, *_ = statement_book
    assert reconciliation.reconciled_balance(db, bank.handle, date(2026, 1, 31)) == Money("1000")
    assert reconciliation.reconciled_balance(db, bank.handle, date(2025, 12, 1)) == Money("0")


def test_a_statement_entry_is_added_cleared_and_checked_in_one_change(db, statement_book):
    """#362: a quick add for interest or a fee found on the statement."""
    bank, cleared, pending, _future = statement_book
    expense = pending.splits[0].account
    session = reconciliation.start(db, bank.handle, date(2026, 1, 31), "740")
    undo_depth = len(db.undo_stack)
    result = add_statement_entry(
        db, AddStatementEntry(bank.handle, expense, date(2026, 1, 31), "Fee", Money("-10"))
    )
    assert result.value is not None
    added = db.get_transaction(result.value.handle)
    own = added.split_for(bank.handle)
    assert own.value == Money("-10") and own.reconcile is ReconcileState.CLEARED
    assert added.split_for(expense).value == Money("10")
    state = reconciliation.summary(db, session.handle)
    assert own.handle in state.reconciliation.selected_splits
    assert state.selected_balance == Money("790")
    assert len(db.undo_stack) == undo_depth + 1
    db.undo()
    assert db.get_transaction(result.value.handle) is None
    assert own.handle not in db.get_reconciliation(session.handle).selected_splits


def test_a_statement_entry_on_a_card_raises_what_it_owes(db):
    """Interest charged is a positive amount in the card's own terms."""
    root = Account(name="Root", atype=AccountType.ROOT)
    card = Account(name="Card", atype=AccountType.CREDIT, parent=root.handle)
    interest = Account(name="Interest", atype=AccountType.EXPENSE, parent=root.handle)
    with db.transaction("Card") as txn:
        for account in (root, card, interest):
            db.add_account(account, txn)
    result = add_statement_entry(
        db,
        AddStatementEntry(
            card.handle, interest.handle, date(2026, 1, 31), "Interest", Money("12.34")
        ),
    )
    assert result.value is not None
    added = db.get_transaction(result.value.handle)
    assert added.split_for(card.handle).value == Money("-12.34")
    assert added.split_for(interest.handle).value == Money("12.34")
    assert reconciliation.reconciled_balance(db, card.handle, date(2026, 1, 31)) == Money("0")
    session = reconciliation.start(db, card.handle, date(2026, 1, 31), "12.34")
    assert reconciliation.summary(db, session).balanced


def test_a_rejected_statement_entry_writes_nothing(db, statement_book):
    bank, *_ = statement_book
    before = len(list(db.iter_transactions()))
    for request in (
        AddStatementEntry(bank.handle, bank.handle, date(2026, 1, 31), "Fee", Money("5")),
        AddStatementEntry(bank.handle, "missing", date(2026, 1, 31), "Fee", Money("5")),
        AddStatementEntry(bank.handle, bank.handle, date(2026, 1, 31), "Fee", Money("0")),
        AddStatementEntry("missing", bank.handle, date(2026, 1, 31), "Fee", Money("5")),
    ):
        assert add_statement_entry(db, request).value is None
    assert len(list(db.iter_transactions())) == before


def test_cancelling_abandons_the_statement_without_touching_the_ledger(db, statement_book):
    """#362: abandon leaves every split as it was."""
    bank, cleared, pending, _future = statement_book
    session = reconciliation.start(db, bank.handle, date(2026, 1, 31), "750")
    reconciliation.set_selection(db, session.handle, [pending.split_for(bank.handle).handle])
    before = {t.handle: t.serialize() for t in db.iter_transactions()}
    assert cancel_reconciliation(db, ReconciliationAction(session.handle)).value is not None
    assert {t.handle: t.serialize() for t in db.iter_transactions()} == before
    assert reconciliation.open_for_account(db, bank.handle) is None
