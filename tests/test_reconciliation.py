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
    ReconciliationAction,
    StartReconciliation,
    UpdateReconciliation,
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
    assert [item.description for item in state.candidates] == [
        "Cleared expense",
        "Pending expense",
    ]
    assert session.selected_splits == [cleared.split_for(bank.handle).handle]
    assert all(item.transaction != _future.handle for item in state.candidates)

    balanced = reconciliation.set_selection(
        db,
        session.handle,
        [item.split for item in state.candidates],
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
            selected_splits=tuple(item.split for item in started.value.candidates),
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
    reconciliation.set_selection(db, session.handle, [item.split for item in state.candidates])

    completed = reconciliation.complete(db, session.handle)

    assert completed.status is ReconciliationStatus.COMPLETED
    assert completed.completed_at is not None
    assert [event.action for event in completed.audit_events] == ["opened", "completed"]
    for candidate in state.candidates:
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
    assert all(item.state is ReconcileState.CLEARED for item in reopened_state.candidates)


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
