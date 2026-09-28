"""One expense split between a payer and an FSA claim (issue #192).

An insurer pays part of a bill and the FSA the patient's share. The claim names
the receivable whose payer covers part of the same expense; BreadSched then
shows one allocation: the payer's share, the FSA's share, and yours. The FSA
share stays zero until an EOB responsibility is entered, and never exceeds what
the payer leaves. When the payer and the EOB together claim more than was paid
the claim needs review; nothing is refused, rewritten, or posted.
"""

from __future__ import annotations

from datetime import date

from breadsched.gen.engine.fsa_claims import (
    FsaClaimStatus,
    claim_summary,
    shared_costs_for_receivable,
)
from breadsched.gen.engine.ledger import balance
from breadsched.gen.engine.receivables import receivable_summary
from breadsched.gen.lib import Money, Transaction
from breadsched.gen.lib.fsa_claim import FsaClaim
from breadsched.gen.services.claims import ClaimInput, ClaimLinkInput, SaveClaim, save_claim
from breadsched.gen.services.receivables import (
    SaveReceivable,
    attach_expense_split,
    attach_reimbursement_split,
    delete_receivable,
    save_receivable,
)

WHEN = date(2026, 9, 1)
TODAY = date(2026, 10, 1)


def _bill(db, book, *, expected="100.00", amount="150.00"):
    """A 150 bill the insurer is expected to cover 100 of."""
    transaction = Transaction.simple(WHEN, "Clinic", book.groceries, book.checking, amount)
    with db.transaction("Spend") as txn:
        db.add_transaction(transaction, txn)
    split = next(s for s in transaction.splits if s.account == book.groceries)
    receivable = save_receivable(
        db,
        SaveReceivable(
            WHEN,
            "Acme Insurance",
            "Visit",
            expected_amount=Money(expected) if expected is not None else None,
        ),
    ).value
    assert attach_expense_split(db, receivable.handle, transaction.handle, split.handle).ok
    return db.get_receivable(receivable.handle), (transaction.handle, split.handle)


def _claim(db, expense, *, receivable=None, eob=None, handle=None):
    result = save_claim(
        db,
        SaveClaim(
            ClaimInput(
                WHEN,
                "Clinic",
                payments=(ClaimLinkInput(*expense),),
                eob_responsibility=Money(eob) if eob is not None else None,
                receivable=receivable,
            ),
            existing_handle=handle,
        ),
    )
    return result


def _summary(db, handle):
    return claim_summary(db, db.get_fsa_claim(handle), as_of=TODAY)


def _insurer_pays(db, book, receivable, amount):
    transaction = Transaction.simple(
        date(2026, 9, 20), "Acme Insurance", book.checking, book.groceries, amount
    )
    with db.transaction("Insurer") as txn:
        db.add_transaction(transaction, txn)
    split = next(s for s in transaction.splits if s.account == book.groceries)
    assert attach_reimbursement_split(db, receivable.handle, transaction.handle, split.handle).ok


def test_the_fsa_share_stays_zero_until_the_eob_is_entered(db, book):
    receivable, expense = _bill(db, book)
    unlinked = _claim(db, expense).value.handle
    # Unlinked, a claim waiting for its EOB still expects the whole net paid.
    assert _summary(db, unlinked).reimbursable == Money(150)
    db_claim = db.get_fsa_claim(unlinked)
    with db.transaction("Drop") as txn:
        db.remove_fsa_claim(db_claim.handle, txn)

    handle = _claim(db, expense, receivable=receivable.handle).value.handle
    summary = _summary(db, handle)
    shared = summary.shared
    assert summary.status is FsaClaimStatus.WAITING_EOB
    assert summary.reimbursable == Money(0)
    assert (shared.expense, shared.payer_share, shared.fsa_share, shared.your_share) == (
        Money(150),
        Money(100),
        Money(0),
        Money(50),
    )
    assert shared.waiting_eob and not shared.needs_review
    assert shared.payer == "Acme Insurance"
    # The explicit link replaces the overlap warning on the receivable.
    stored = db.get_receivable(receivable.handle)
    assert receivable_summary(db, stored, as_of=TODAY).fsa_claims == ()
    assert shared_costs_for_receivable(db, stored, as_of=TODAY) == [shared]


def test_the_eob_responsibility_is_what_the_fsa_covers(db, book):
    receivable, expense = _bill(db, book)
    handle = _claim(db, expense, receivable=receivable.handle, eob="50.00").value.handle
    summary = _summary(db, handle)
    # No FSA account is funded here, so the claim reads "closed, no funds", not review.
    assert summary.status is FsaClaimStatus.CLOSED_NO_FUNDS
    assert summary.reimbursable == summary.remaining_reimbursable == Money(50)
    assert (summary.shared.fsa_share, summary.shared.your_share) == (Money(50), Money(0))
    assert not summary.shared.waiting_eob


def test_more_than_was_paid_needs_review_and_is_never_refused(db, book):
    receivable, expense = _bill(db, book)
    handle = _claim(db, expense, receivable=receivable.handle, eob="60.00").value.handle
    summary = _summary(db, handle)
    assert summary.status is FsaClaimStatus.NEEDS_REVIEW
    assert summary.shared.needs_review and summary.shared.over_allocated == Money(10)
    # The FSA is never asked for more than the payer leaves.
    assert summary.reimbursable == Money(50)
    assert summary.shared.your_share == Money(0)


def test_a_later_insurer_payment_moves_the_payer_share(db, book):
    receivable, expense = _bill(db, book)
    handle = _claim(db, expense, receivable=receivable.handle, eob="50.00").value.handle
    _insurer_pays(db, book, receivable, "80.00")
    # Partly paid: the payer is still expected to cover 100.
    assert _summary(db, handle).shared.payer_share == Money(100)
    _insurer_pays(db, book, receivable, "40.00")
    summary = _summary(db, handle)
    # The insurer paid 120 after all: the FSA's 50 no longer fits.
    assert summary.shared.payer_share == Money(120)
    assert summary.shared.fsa_share == Money(30)
    assert summary.shared.over_allocated == Money(20)
    assert summary.status is FsaClaimStatus.NEEDS_REVIEW


def test_unlinking_restores_the_ordinary_claim_and_the_warning(db, book):
    receivable, expense = _bill(db, book)
    handle = _claim(db, expense, receivable=receivable.handle).value.handle
    assert _claim(db, expense, handle=handle).ok
    summary = _summary(db, handle)
    assert summary.shared is None
    assert summary.reimbursable == Money(150)
    stored = db.get_receivable(receivable.handle)
    assert receivable_summary(db, stored, as_of=TODAY).fsa_claims == (handle,)


def test_deleting_the_receivable_unlinks_the_claim(db, book):
    receivable, expense = _bill(db, book)
    handle = _claim(db, expense, receivable=receivable.handle, eob="50.00").value.handle
    assert delete_receivable(db, receivable.handle).ok
    assert db.get_fsa_claim(handle).receivable is None
    assert db.verify_book() == []
    assert _summary(db, handle).reimbursable == Money(50)


def test_rejected_links_leave_the_stored_claim_unchanged(db, book):
    receivable, expense = _bill(db, book)
    handle = _claim(db, expense, eob="50.00").value.handle
    before = db.get_fsa_claim(handle).serialize()

    missing = _claim(db, expense, receivable="no-such-receivable", handle=handle)
    assert [error.code for error in missing.errors] == ["claim.receivable.not_found"]
    assert db.get_fsa_claim(handle).serialize() == before

    assert _claim(db, expense, receivable=receivable.handle).ok
    taken = _claim(db, expense, receivable=receivable.handle, handle=handle)
    assert [error.code for error in taken.errors] == ["claim.receivable.taken"]
    assert db.get_fsa_claim(handle).serialize() == before


def test_the_ledger_is_unchanged_by_the_allocation(db, book):
    receivable, expense = _bill(db, book)
    before = (balance(db, book.groceries), balance(db, receivable.account))
    _claim(db, expense, receivable=receivable.handle, eob="50.00")
    assert (balance(db, book.groceries), balance(db, receivable.account)) == before


def test_the_link_round_trips_and_older_claims_have_none():
    claim = FsaClaim(WHEN, "Clinic", receivable="r1")
    assert FsaClaim.from_dict(claim.serialize()).receivable == "r1"
    data = claim.serialize()
    del data["receivable"]
    assert FsaClaim.from_dict(data).receivable is None


def test_the_command_line_lists_the_allocation(tmp_path, capsys):
    import json

    from breadsched.cli.main import main
    from breadsched.gen.db.sqlite import DbSQLite
    from breadsched.gen.lib import Account, AccountType

    path = tmp_path / "shared.breadsched"
    assert main(["init", str(path)]) == 0
    db = DbSQLite()
    db.load(str(path))
    with db.transaction("Setup") as txn:
        checking = Account(name="Checking", atype=AccountType.BANK)
        medical = Account(name="Medical", atype=AccountType.EXPENSE)
        db.add_account(checking, txn)
        db.add_account(medical, txn)
    book = type("Book", (), {"groceries": medical.handle, "checking": checking.handle})
    receivable, expense = _bill(db, book)
    assert _claim(db, expense, receivable=receivable.handle, eob="60.00").ok
    db.close()
    capsys.readouterr()

    assert main(["receivables", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Acme Insurance Visit and its FSA claim: Of 150.00: Acme Insurance pays 100.00" in out
    assert "the FSA 50.00, you 0.00. Needs review: 10.00 more than was paid" in out
    assert "also claimed from the FSA" not in out

    assert main(["receivables", str(path), "--json"]) == 0
    [listed] = json.loads(capsys.readouterr().out)
    [shared] = listed["shared_costs"]
    assert (shared["fsa_share"], shared["needs_review"]) == ("50.00", True)
