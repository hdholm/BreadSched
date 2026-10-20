"""FSA claims and reimbursable expenses check their ledger links alike (#340).

Both records point at ordinary splits with one ``SplitLink`` type and resolve,
total, and refuse links through ``engine.split_links``. A receivable's
reclassification transaction is BreadSched's own, rewritten from the receivable
on every change, so neither record may link it; before #340 a claim could take
one as a provider refund.
"""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.engine import fsa_claims, receivables, split_links
from breadsched.gen.lib import Money, SplitLink, Transaction
from breadsched.gen.services.claims import ClaimInput, ClaimLinkInput, SaveClaim, save_claim
from breadsched.gen.services.receivables import (
    SaveReceivable,
    attach_expense_split,
    attach_reimbursement_split,
    save_receivable,
)
from breadsched.presentation import service_error_message

WHEN = date(2026, 9, 1)


@pytest.fixture
def billed(db, book):
    """A 150 grocery bill a payer is expected to cover 100 of, and its reclassification."""
    transaction = Transaction.simple(WHEN, "Clinic", book.groceries, book.checking, "150.00")
    with db.transaction("Spend") as txn:
        db.add_transaction(transaction, txn)
    split = next(s for s in transaction.splits if s.account == book.groceries)
    receivable = save_receivable(
        db, SaveReceivable(WHEN, "Acme Insurance", "Visit", expected_amount=Money("100.00"))
    ).value
    assert attach_expense_split(db, receivable.handle, transaction.handle, split.handle).ok
    receivable = db.get_receivable(receivable.handle)
    [posting_handle] = receivable.postings
    posting = db.get_transaction(posting_handle)
    credit = next(s for s in posting.splits if s.account == book.groceries)
    assert credit.value < 0  # a credit to the expense, shaped like a provider refund
    return transaction, split, posting, credit


def test_both_records_raise_the_one_link_error_with_their_own_codes():
    assert issubclass(fsa_claims.FsaClaimError, split_links.LinkError)
    assert issubclass(receivables.ReceivableError, split_links.LinkError)
    for prefix, error in (
        ("claim", fsa_claims.FsaClaimError),
        ("receivable", receivables.ReceivableError),
    ):
        missing = split_links.missing_link(prefix, error)
        assert isinstance(missing("split"), error)
        assert missing("split").code == f"{prefix}.link.split.not_found"
        assert missing("transaction").code == f"{prefix}.link.transaction.not_found"


def test_both_records_share_one_owned_posting_list(db, billed):
    _transaction, _split, posting, _credit = billed
    assert receivables.owned_postings(db) == split_links.owned_postings(db) == {posting.handle}


def test_a_claim_cannot_take_a_reclassification_as_a_refund(db, billed):
    transaction, split, posting, credit = billed
    saved = save_claim(
        db,
        SaveClaim(
            ClaimInput(WHEN, "Clinic", payments=(ClaimLinkInput(transaction.handle, split.handle),))
        ),
    ).value
    assert saved is not None
    before = db.get_fsa_claim(saved.handle).serialize()

    refused = save_claim(
        db,
        SaveClaim(
            ClaimInput(
                WHEN,
                "Clinic",
                payments=(ClaimLinkInput(transaction.handle, split.handle),),
                refunds=(ClaimLinkInput(posting.handle, credit.handle),),
            ),
            existing_handle=saved.handle,
        ),
    )

    assert [error.code for error in refused.errors] == ["claim.split.owned_posting"]
    assert service_error_message(refused.errors[0]) == service_error_message(
        type(refused.errors[0])("receivable.split.owned_posting", ("split",))
    )
    assert db.get_fsa_claim(saved.handle).serialize() == before


def test_a_reclassification_offers_no_claim_roles(db, billed):
    transaction, _split, posting, _credit = billed
    assert fsa_claims.attachment_roles(db, posting) == []
    assert [role.role for role in fsa_claims.attachment_roles(db, transaction)] == ["payment"]


def test_a_receivable_refuses_the_same_transaction(db, billed):
    _transaction, _split, posting, credit = billed
    [receivable] = db.iter_receivables()
    result = attach_reimbursement_split(db, receivable.handle, posting.handle, credit.handle)
    assert [error.code for error in result.errors] == ["receivable.split.owned_posting"]


def test_links_total_their_magnitudes(db, billed):
    transaction, split, posting, credit = billed
    missing = split_links.missing_link("claim", fsa_claims.FsaClaimError)
    links = [SplitLink(transaction.handle, split.handle), SplitLink(posting.handle, credit.handle)]
    assert split_links.sum_links(db, links, missing) == abs(split.value) + abs(credit.value)
    with pytest.raises(fsa_claims.FsaClaimError) as caught:
        split_links.sum_links(db, [SplitLink(transaction.handle, "gone")], missing)
    assert caught.value.code == "claim.link.split.not_found"
