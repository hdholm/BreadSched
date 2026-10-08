"""Reimbursable expenses: an expense and what a payer owes, tracked apart.

A receivable never rewrites or erases the expense splits it points at, and a
reimbursement is never counted as new income: it is an ordinary expense-class
split crediting the same account the money was spent from, exactly like a
refund. Status (open, partial, disputed, written off, settled) is always
recomputed from those linked splits plus any recorded dispute or write-off,
never stored.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.engine.receivables import ReceivableStatus, receivable_summary
from breadsched.gen.lib import Money, Transaction
from breadsched.gen.services.receivables import (
    RecordWriteOff,
    SaveReceivable,
    accept_reimbursements,
    attach_expense_split,
    attach_reimbursement_split,
    clear_dispute,
    delete_receivable,
    detach_split,
    list_receivables,
    mark_disputed,
    receivable_candidates,
    record_write_off,
    reimbursement_proposals,
    save_receivable,
)


def _expense(db, book, when, description, amount="150.00"):
    """An ordinary out-of-pocket cost: the debit lands in ``book.groceries``."""
    transaction = Transaction.simple(when, description, book.groceries, book.checking, amount)
    with db.transaction("Spend") as txn:
        db.add_transaction(transaction, txn)
    split = next(item for item in transaction.splits if item.account == book.groceries)
    return transaction.handle, split.handle


def _reimbursement(db, book, when, description, amount):
    """A credit back into the same expense account, like an ordinary refund."""
    transaction = Transaction.simple(when, description, book.checking, book.groceries, amount)
    with db.transaction("Reimburse") as txn:
        db.add_transaction(transaction, txn)
    split = next(item for item in transaction.splits if item.account == book.groceries)
    return transaction.handle, split.handle


def _save(db, payer="Acme Insurance", incurred=date(2026, 9, 1), **kwargs):
    return save_receivable(db, SaveReceivable(incurred_date=incurred, payer=payer, **kwargs)).value


def test_status_moves_from_open_through_partial_to_settled(db, book):
    receivable = _save(db)
    txn, split = _expense(db, book, date(2026, 9, 1), "Doctor visit", "150.00")
    assert attach_expense_split(db, receivable.handle, txn, split).ok

    open_summary = receivable_summary(db, db.get_receivable(receivable.handle))
    assert (open_summary.status, open_summary.expense_total, open_summary.remaining) == (
        ReceivableStatus.OPEN,
        Money("150.00"),
        Money("150.00"),
    )

    rtxn, rsplit = _reimbursement(db, book, date(2026, 9, 15), "Partial reimbursement", "100.00")
    assert attach_reimbursement_split(db, receivable.handle, rtxn, rsplit).ok
    partial = receivable_summary(db, db.get_receivable(receivable.handle))
    assert (partial.status, partial.reimbursed, partial.remaining) == (
        ReceivableStatus.PARTIAL,
        Money("100.00"),
        Money("50.00"),
    )

    rtxn2, rsplit2 = _reimbursement(db, book, date(2026, 9, 20), "Final reimbursement", "50.00")
    assert attach_reimbursement_split(db, receivable.handle, rtxn2, rsplit2).ok
    settled = receivable_summary(db, db.get_receivable(receivable.handle))
    assert (settled.status, settled.remaining) == (ReceivableStatus.SETTLED, Money("0.00"))

    # The original expense is never rewritten by any of this.
    stored = db.get_transaction(txn)
    assert stored.description == "Doctor visit"
    assert any(item.value == Money("150.00") for item in stored.splits)


def test_a_write_off_closes_the_balance_even_after_a_partial_reimbursement(db, book):
    receivable = _save(db)
    txn, split = _expense(db, book, date(2026, 9, 1), "Doctor visit", "150.00")
    attach_expense_split(db, receivable.handle, txn, split)
    rtxn, rsplit = _reimbursement(db, book, date(2026, 9, 10), "Partial", "100.00")
    attach_reimbursement_split(db, receivable.handle, rtxn, rsplit)

    written = record_write_off(
        db, RecordWriteOff(receivable.handle, Money("50.00"), date(2026, 10, 1), "Deductible")
    )
    assert written.ok, written.errors

    summary = receivable_summary(db, db.get_receivable(receivable.handle))
    assert (summary.status, summary.written_off, summary.remaining) == (
        ReceivableStatus.WRITTEN_OFF,
        Money("50.00"),
        Money("0.00"),
    )


def test_a_dispute_holds_the_status_while_a_balance_remains(db, book):
    receivable = _save(db)
    txn, split = _expense(db, book, date(2026, 9, 1), "Doctor visit", "150.00")
    attach_expense_split(db, receivable.handle, txn, split)

    disputed = mark_disputed(db, receivable.handle, date(2026, 9, 5), "Insurer denied the claim")
    assert disputed.ok
    summary = receivable_summary(db, db.get_receivable(receivable.handle))
    assert summary.status == ReceivableStatus.DISPUTED
    assert db.get_receivable(receivable.handle).dispute_note == "Insurer denied the claim"

    cleared = clear_dispute(db, receivable.handle)
    assert cleared.ok
    reopened = receivable_summary(db, db.get_receivable(receivable.handle))
    assert reopened.status == ReceivableStatus.OPEN

    # A dispute never masks a receivable that is already fully reimbursed.
    rtxn, rsplit = _reimbursement(db, book, date(2026, 9, 15), "Paid in full", "150.00")
    attach_reimbursement_split(db, receivable.handle, rtxn, rsplit)
    mark_disputed(db, receivable.handle, date(2026, 9, 16))
    settled_anyway = receivable_summary(db, db.get_receivable(receivable.handle))
    assert settled_anyway.status == ReceivableStatus.SETTLED


def test_linking_rejects_the_wrong_kind_of_split(db, book):
    receivable = _save(db)
    txn, split = _expense(db, book, date(2026, 9, 1), "Doctor visit", "150.00")

    # An income-class split can never be linked at all.
    salary_txn = Transaction.simple(date(2026, 9, 1), "Salary", book.checking, book.salary, "10.00")
    with db.transaction("Pay") as write:
        db.add_transaction(salary_txn, write)
    salary_split = next(item for item in salary_txn.splits if item.account == book.salary)
    assert [
        e.code
        for e in attach_expense_split(
            db, receivable.handle, salary_txn.handle, salary_split.handle
        ).errors
    ] == ["receivable.split.not_expense"]

    # The expense split itself cannot be linked as a reimbursement (it is
    # positive), and a reimbursement split cannot be linked as an expense.
    assert [
        e.code for e in attach_reimbursement_split(db, receivable.handle, txn, split).errors
    ] == ["receivable.split.not_a_credit"]
    rtxn, rsplit = _reimbursement(db, book, date(2026, 9, 10), "Refund", "50.00")
    assert [e.code for e in attach_expense_split(db, receivable.handle, rtxn, rsplit).errors] == [
        "receivable.split.not_a_cost"
    ]

    assert attach_expense_split(db, receivable.handle, txn, split).ok
    assert [e.code for e in attach_expense_split(db, receivable.handle, txn, split).errors] == [
        "receivable.split.duplicate"
    ]


def test_detaching_unlinks_without_touching_the_ledger(db, book):
    receivable = _save(db)
    txn, split = _expense(db, book, date(2026, 9, 1), "Doctor visit", "150.00")
    attach_expense_split(db, receivable.handle, txn, split)

    detached = detach_split(db, receivable.handle, txn, split)
    assert detached.ok
    summary = receivable_summary(db, db.get_receivable(receivable.handle))
    assert summary.expense_total == Money("0.00")
    assert db.get_transaction(txn) is not None

    assert [e.code for e in detach_split(db, receivable.handle, txn, split).errors] == [
        "receivable.link.not_found"
    ]


def test_rejected_writes_leave_the_book_unchanged(db, book):
    receivable = _save(db)
    before = [(item.handle, item.payer) for item in db.iter_receivables()]

    for result, code in [
        (save_receivable(db, SaveReceivable(date(2026, 9, 1), "   ")), "receivable.payer.required"),
        (
            save_receivable(db, SaveReceivable(date(2026, 9, 1), "Other", handle="missing")),
            "receivable.not_found",
        ),
        (
            save_receivable(
                db, SaveReceivable(date(2026, 9, 1), "Other", expected_amount=Money("-1"))
            ),
            "receivable.expected_amount.negative",
        ),
        (mark_disputed(db, "missing", date(2026, 9, 1)), "receivable.not_found"),
        (clear_dispute(db, "missing"), "receivable.not_found"),
        (
            record_write_off(db, RecordWriteOff("missing", Money("1"), date(2026, 9, 1))),
            "receivable.not_found",
        ),
        (
            record_write_off(db, RecordWriteOff(receivable.handle, Money("0"), date(2026, 9, 1))),
            "receivable.write_off.amount_not_positive",
        ),
        (delete_receivable(db, "missing"), "receivable.not_found"),
        (
            attach_expense_split(db, "missing", "t", "s"),
            "receivable.not_found",
        ),
        (
            attach_expense_split(db, receivable.handle, "missing", "s"),
            "receivable.transaction.not_found",
        ),
    ]:
        assert [error.code for error in result.errors] == [code]

    assert [(item.handle, item.payer) for item in db.iter_receivables()] == before


def test_deleting_a_receivable_leaves_the_linked_transactions_alone(db, book):
    receivable = _save(db)
    txn, split = _expense(db, book, date(2026, 9, 1), "Doctor visit", "150.00")
    attach_expense_split(db, receivable.handle, txn, split)

    deleted = delete_receivable(db, receivable.handle)
    assert deleted.value == receivable.handle
    assert db.get_receivable(receivable.handle) is None
    assert db.get_transaction(txn) is not None

    assert db.undo() is True
    assert db.get_receivable(receivable.handle) is not None


def test_list_receivables_reports_every_summary(db, book):
    first = _save(db, payer="Acme Insurance", incurred=date(2026, 9, 1))
    second = _save(db, payer="Employer", incurred=date(2026, 9, 10))

    summaries = list_receivables(db, as_of=date(2026, 9, 30)).value
    assert {item.receivable.handle for item in summaries} == {first.handle, second.handle}
    by_handle = {item.receivable.handle: item for item in summaries}
    assert by_handle[first.handle].age_days == 29
    assert by_handle[second.handle].age_days == 20


def test_deleting_a_linked_transaction_is_refused(db, book):
    """The database itself refuses a write that would orphan a receivable."""
    from breadsched.gen.db.base import DbError

    receivable = _save(db)
    txn, split = _expense(db, book, date(2026, 9, 1), "Doctor visit", "150.00")
    attach_expense_split(db, receivable.handle, txn, split)

    with pytest.raises(DbError, match="receivable.missing_transaction"):
        with db.transaction("Remove") as write:
            db.remove_transaction(txn, write)

    # The refused write leaves both the transaction and the receivable intact.
    assert db.get_transaction(txn) is not None
    assert db.get_receivable(receivable.handle) is not None


def test_schema_8_book_migrates_to_receivables(tmp_path):
    path = tmp_path / "schema-8.breadsched"
    sql = (Path(__file__).parent / "fixtures" / "native" / "schema-8.sql").read_text()
    with sqlite3.connect(path) as raw:
        raw.executescript(sql)

    db = DbSQLite()
    db.load(str(path))
    try:
        assert db.get_metadata("schema_version") == 11
        assert db.get_metadata("fixture_marker") == "schema-8"
        assert list(db.iter_receivables()) == []
        saved = save_receivable(db, SaveReceivable(date(2026, 9, 1), "Acme Insurance"))
        assert saved.ok
        assert [
            row[0]
            for row in db._require().execute(
                "SELECT version FROM schema_migration ORDER BY version"
            )
        ] == [7, 8, 9, 10, 11]
        assert db.integrity_problems() == []
    finally:
        db.close()
    assert (tmp_path / "schema-8.breadsched.pre-migration-v8.bak").exists()


def test_cli_adds_links_and_resolves(tmp_path, capsys):
    import json

    from breadsched.cli.main import main

    path = tmp_path / "cli.breadsched"
    assert main(["init", str(path)]) == 0
    capsys.readouterr()

    from breadsched.gen.db.sqlite import DbSQLite as _Db
    from breadsched.gen.lib import Account, AccountType

    db = _Db()
    db.load(str(path))
    with db.transaction("Setup") as txn:
        checking = Account(name="Checking", atype=AccountType.BANK)
        db.add_account(checking, txn)
        medical = Account(name="Medical", atype=AccountType.EXPENSE)
        db.add_account(medical, txn)
    expense = Transaction.simple(
        date(2026, 9, 1), "Doctor visit", medical.handle, checking.handle, "150.00"
    )
    with db.transaction("Spend") as txn:
        db.add_transaction(expense, txn)
    db.close()

    assert (
        main(
            [
                "receivables",
                str(path),
                "--add",
                "Acme Insurance",
                "--incurred",
                "2026-09-01",
                "--expected",
                "150.00",
                "--json",
            ]
        )
        == 0
    )
    saved = json.loads(capsys.readouterr().out)
    handle = saved["handle"]

    assert (
        main(
            [
                "receivables",
                str(path),
                "--attach-expense",
                handle,
                "--transaction",
                expense.handle,
                "--split-index",
                "1",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["receivables", str(path), "--json"]) == 0
    [listed] = json.loads(capsys.readouterr().out)
    assert listed["status"] in ("open", "settled")

    assert (
        main(
            [
                "receivables",
                str(path),
                "--write-off",
                handle,
                "--amount",
                "150.00",
                "--on",
                "2026-10-01",
                "--reason",
                "uncollectible",
            ]
        )
        == 0
    )
    assert "Wrote off" in capsys.readouterr().out

    assert main(["receivables", str(path), "--delete", handle]) == 0
    assert "Deleted receivable" in capsys.readouterr().out
    assert main(["receivables", str(path), "--delete", handle]) == 2
    assert "no receivable matches" in capsys.readouterr().err


def test_gnucash_reimport_refuses_to_delete_a_linked_transaction(db, book):
    from breadsched.plugins.importer.gnucash_common import _protected_transaction_references

    receivable = _save(db)
    txn, split = _expense(db, book, date(2026, 9, 1), "Doctor visit", "150.00")
    attach_expense_split(db, receivable.handle, txn, split)

    stored = db.get_transaction(txn)
    references = _protected_transaction_references(db, stored)
    assert any("receivable" in item for item in references)


def test_candidates_offer_unlinked_costs_and_credits_newest_first(db, book):
    """GTK and web pickers share one rule: expense-account splits, split by sign."""
    early = _expense(db, book, date(2026, 9, 1), "Clinic", "150.00")
    late = _expense(db, book, date(2026, 9, 5), "Pharmacy", "40.00")
    credit = _reimbursement(db, book, date(2026, 9, 20), "Insurer", "100.00")
    receivable = _save(db)
    attach_expense_split(db, receivable.handle, *early)

    costs, credits = receivable_candidates(db, receivable.handle).value
    assert [(item.transaction, item.split) for item in costs][:1] == [late]
    assert early not in [(item.transaction, item.split) for item in costs]
    assert [(item.transaction, item.split) for item in credits] == [credit]
    assert all(item.value > 0 for item in costs) and all(item.value < 0 for item in credits)
    assert all(item.account == book.groceries for item in (*costs, *credits))

    everything, _ = receivable_candidates(db).value
    assert early in [(item.transaction, item.split) for item in everything]
    assert receivable_candidates(db, "missing").errors[0].code == "receivable.not_found"


def _proposed(db):
    return [
        (item.receivable, item.transaction, item.split)
        for item in reimbursement_proposals(db).value
    ]


def test_a_matching_credit_is_proposed_and_accepted_as_a_partial_reimbursement(db, book):
    receivable = _save(db)
    attach_expense_split(db, receivable.handle, *_expense(db, book, date(2026, 9, 1), "Clinic"))
    credit = _reimbursement(db, book, date(2026, 9, 20), "ACME INSURANCE PMT 0042", "100.00")

    [proposal] = reimbursement_proposals(db).value
    assert (proposal.receivable, proposal.transaction, proposal.split) == (
        receivable.handle,
        *credit,
    )
    assert proposal.amount == Money("100.00")
    assert proposal.remaining_after == Money("50.00")
    assert "payer named" in proposal.reason

    result = accept_reimbursements(db, tuple(_proposed(db))).value
    assert (result.linked, result.unchanged) == (1, 0)
    summary = receivable_summary(db, db.get_receivable(receivable.handle))
    assert summary.status is ReceivableStatus.PARTIAL
    assert _proposed(db) == []


def test_credits_that_do_not_fit_are_never_proposed(db, book):
    receivable = _save(db, incurred=date(2026, 9, 10))
    attach_expense_split(db, receivable.handle, *_expense(db, book, date(2026, 9, 10), "Clinic"))
    _reimbursement(db, book, date(2026, 9, 1), "Before the expense", "10.00")
    _reimbursement(db, book, date(2026, 9, 20), "Too large", "500.00")
    assert _proposed(db) == []


def test_an_ambiguous_credit_needs_the_payer_named(db, book):
    acme = _save(db, payer="Acme Insurance")
    attach_expense_split(db, acme.handle, *_expense(db, book, date(2026, 9, 1), "Clinic"))
    other = _save(db, payer="Employer Wellness")
    attach_expense_split(db, other.handle, *_expense(db, book, date(2026, 9, 2), "Gym"))
    _reimbursement(db, book, date(2026, 9, 20), "Deposit", "50.00")
    assert _proposed(db) == []

    named = _reimbursement(db, book, date(2026, 9, 21), "Employer Wellness credit", "60.00")
    assert _proposed(db) == [(other.handle, *named)]


def test_proposals_never_promise_more_than_remains(db, book):
    receivable = _save(db)
    attach_expense_split(db, receivable.handle, *_expense(db, book, date(2026, 9, 1), "Clinic"))
    first = _reimbursement(db, book, date(2026, 9, 10), "Acme 1", "100.00")
    _reimbursement(db, book, date(2026, 9, 11), "Acme 2", "100.00")
    assert _proposed(db) == [(receivable.handle, *first)]


def test_stale_choices_and_closed_receivables_are_skipped(db, book):
    receivable = _save(db)
    attach_expense_split(db, receivable.handle, *_expense(db, book, date(2026, 9, 1), "Clinic"))
    credit = _reimbursement(db, book, date(2026, 9, 10), "Acme", "40.00")
    record_write_off(db, RecordWriteOff(receivable.handle, Money("150.00"), date(2026, 9, 5)))
    assert _proposed(db) == []

    result = accept_reimbursements(db, ((receivable.handle, *credit),)).value
    assert (result.linked, result.unchanged) == (0, 1)
    assert db.get_receivable(receivable.handle).reimbursements == []


def test_cli_lists_and_accepts_reimbursement_proposals(tmp_path, capsys):
    import json

    from breadsched.cli.main import main
    from breadsched.gen.db.sqlite import DbSQLite as _Db
    from breadsched.gen.lib import Account, AccountType

    path = tmp_path / "proposals.breadsched"
    assert main(["init", str(path)]) == 0
    db = _Db()
    db.load(str(path))
    with db.transaction("Setup") as txn:
        checking = Account(name="Checking", atype=AccountType.BANK)
        db.add_account(checking, txn)
        medical = Account(name="Medical", atype=AccountType.EXPENSE)
        db.add_account(medical, txn)
    expense = Transaction.simple(
        date(2026, 9, 1), "Doctor visit", medical.handle, checking.handle, "150.00"
    )
    refund = Transaction.simple(
        date(2026, 9, 15), "Acme Insurance EOB", checking.handle, medical.handle, "90.00"
    )
    with db.transaction("Spend and refund") as txn:
        db.add_transaction(expense, txn)
        db.add_transaction(refund, txn)
    receivable = save_receivable(
        db, SaveReceivable(incurred_date=date(2026, 9, 1), payer="Acme Insurance")
    ).value
    cost = next(s for s in expense.splits if s.account == medical.handle)
    attach_expense_split(db, receivable.handle, expense.handle, cost.handle)
    db.close()
    capsys.readouterr()

    # An import while a proposal is waiting says so (the notice counts every
    # waiting proposal, not only the imported rows).
    statement = tmp_path / "statement.csv"
    statement.write_text("Date,Description,Amount\n2026-09-30,Coffee,-3.00\n", encoding="utf-8")
    assert (
        main(
            [
                "import-csv",
                str(path),
                str(statement),
                "--account",
                "Checking",
                "--date",
                "Date",
                "--amount",
                "Amount",
                "--description",
                "Description",
            ]
        )
        == 0
    )
    assert "1 credit looks like money back" in capsys.readouterr().out

    assert main(["receivables", str(path), "--proposals", "--json"]) == 0
    [proposal] = json.loads(capsys.readouterr().out)
    assert (proposal["transaction"], proposal["amount"]) == (refund.handle, "90.00")
    assert proposal["remaining_after"] == "60.00"

    assert main(["receivables", str(path), "--accept-proposals"]) == 0
    assert "Linked 1 reimbursement(s)" in capsys.readouterr().out
    assert main(["receivables", str(path), "--json"]) == 0
    [listed] = json.loads(capsys.readouterr().out)
    assert (listed["status"], listed["remaining"]) == ("partial", "60.00")
    assert main(["receivables", str(path), "--proposals"]) == 0
    assert "No unlinked credits" in capsys.readouterr().out


def test_a_credit_in_another_currency_is_never_proposed(db, book):
    """Currency evidence: an amount in another currency is not comparable."""
    from breadsched.gen.lib import Commodity

    euro = Commodity(namespace="CURRENCY", mnemonic="EUR", fullname="Euro", fraction=100)
    with db.transaction("Currency fixture") as txn:
        db.add_commodity(euro, txn)
    receivable = _save(db)
    attach_expense_split(db, receivable.handle, *_expense(db, book, date(2026, 9, 1), "Clinic"))
    refund = Transaction.simple(
        date(2026, 9, 20),
        "Acme Insurance",
        book.checking,
        book.groceries,
        "50.00",
        currency=euro.handle,
    )
    with db.transaction("Foreign refund") as txn:
        db.add_transaction(refund, txn)
    assert _proposed(db) == []


def test_proposals_can_be_limited_to_one_account_and_named_in_a_notice(db, book):
    from breadsched.presentation import reimbursement_notice

    receivable = _save(db)
    attach_expense_split(db, receivable.handle, *_expense(db, book, date(2026, 9, 1), "Clinic"))
    _reimbursement(db, book, date(2026, 9, 20), "Acme", "40.00")
    assert len(reimbursement_proposals(db, account=book.checking).value) == 1
    assert reimbursement_proposals(db, account=book.rent).value == ()

    assert reimbursement_notice(0) is None
    assert reimbursement_notice(1).startswith("1 credit looks like money back")
    assert reimbursement_notice(2).startswith("2 credits look like money back")
