"""Track as FSA Claim: a new claim's defaults inferred from one transaction.

The draft is read-only: it proposes a claim from the transaction's healthcare
cost and its accounts, and only saving it through the claim service writes.
"""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.engine import fsa_claims
from breadsched.gen.lib import (
    Account,
    AccountType,
    FsaClaim,
    FsaClaimAllocation,
    FsaFundingYear,
    Money,
    Receivable,
    Split,
    SplitLink,
    Transaction,
)
from breadsched.gen.services import ClaimLinkInput, SaveClaim, claim_draft, save_claim
from breadsched.presentation.messages import service_error_codes

YEAR = date(2026, 1, 1)


def _fsa(db, book, name, years):
    account = Account(name=name, atype=AccountType.FSA, parent=book.assets)
    account.fsa.years = years
    with db.transaction(name) as txn:
        db.add_account(account, txn)
    return account


def _year(start=YEAR, **extra):
    through = date(start.year, 12, 31)
    return FsaFundingYear(start, through, Money("1500.00"), **extra)


@pytest.fixture
def medical(db, book):
    account = Account(name="Medical", atype=AccountType.EXPENSE, parent=book.expenses)
    with db.transaction("Medical") as txn:
        db.add_account(account, txn)
    return account


def _post(db, transaction):
    with db.transaction(transaction.description) as txn:
        db.add_transaction(transaction, txn)
    return transaction


def _split(transaction, account):
    return next(split for split in transaction.splits if split.account == account)


def _snapshot(db):
    return (
        sorted(repr(item.serialize()) for item in db.iter_fsa_claims()),
        sorted(repr(item.serialize()) for item in db.iter_transactions()),
    )


def test_a_card_payment_starts_a_claim_on_the_only_fsa_covering_its_date(db, book, medical):
    fsa = _fsa(db, book, "Health FSA", [_year()])
    paid = _post(
        db, Transaction.simple(date(2026, 3, 8), "Clinic", medical.handle, book.card, "120")
    )
    before = _snapshot(db)

    draft = claim_draft(db, paid.handle).value

    assert draft is not None and draft.existing is None and draft.claim is not None
    claim = draft.claim
    assert (claim.service_date, claim.provider) == (date(2026, 3, 8), "Clinic")
    assert claim.payments == (ClaimLinkInput(paid.handle, _split(paid, medical.handle).handle),)
    [allocation] = claim.allocations
    assert (allocation.account, allocation.funding_year_start) == (fsa.handle, YEAR)
    assert allocation.reimbursements == ()
    assert claim.eob_responsibility is None and claim.receivable is None
    assert any("only FSA account" in note for note in draft.notes)
    # Drafting writes nothing.
    assert _snapshot(db) == before


def test_a_payment_from_the_fsa_card_uses_that_fsa_and_links_its_side(db, book, medical):
    _fsa(db, book, "Other FSA", [_year()])
    card = _fsa(db, book, "Card FSA", [_year()])
    paid = _post(
        db, Transaction.simple(date(2026, 4, 2), "Pharmacy", medical.handle, card.handle, "40")
    )

    draft = claim_draft(db, paid.handle).value

    assert draft is not None and draft.claim is not None
    [allocation] = draft.claim.allocations
    assert allocation.account == card.handle
    assert allocation.reimbursements == (
        ClaimLinkInput(paid.handle, _split(paid, card.handle).handle),
    )
    # Saved as drafted, the claim counts the FSA card payment as its reimbursement.
    saved = save_claim(db, SaveClaim(draft.claim)).value
    assert saved is not None
    summary = fsa_claims.claim_summary(db, db.get_fsa_claim(saved.handle))
    assert (summary.paid, summary.reimbursed) == (Money("40.00"), Money("40.00"))


def test_earlier_claims_on_the_expense_account_choose_between_fsas(db, book, medical):
    health = _fsa(db, book, "Health FSA", [_year()])
    _fsa(db, book, "Care FSA", [_year()])
    earlier = _post(
        db, Transaction.simple(date(2026, 2, 1), "Dentist", medical.handle, book.card, "90")
    )
    fsa_claims.save_claim(
        db,
        FsaClaim(
            service_date=date(2026, 2, 1),
            payments=[SplitLink(earlier.handle, _split(earlier, medical.handle).handle)],
            allocations=[FsaClaimAllocation(health.handle, YEAR)],
        ),
    )
    paid = _post(
        db, Transaction.simple(date(2026, 5, 1), "Clinic", medical.handle, book.checking, "60")
    )

    draft = claim_draft(db, paid.handle).value

    assert draft is not None and draft.claim is not None
    assert [item.account for item in draft.claim.allocations] == [health.handle]
    assert any("earlier claims" in note for note in draft.notes)


def test_two_fsas_and_no_history_leave_the_account_to_choose(db, book, medical):
    _fsa(db, book, "Health FSA", [_year()])
    _fsa(db, book, "Care FSA", [_year()])
    paid = _post(
        db, Transaction.simple(date(2026, 5, 1), "Clinic", medical.handle, book.checking, "60")
    )

    draft = claim_draft(db, paid.handle).value

    assert draft is not None and draft.claim is not None
    assert draft.claim.allocations == ()
    assert draft.claim.payments  # the cost is still linked
    assert any(note.startswith("Choose the FSA account") for note in draft.notes)


def test_a_grace_period_service_uses_the_earlier_funding_year(db, book, medical):
    fsa = _fsa(
        db,
        book,
        "Health FSA",
        [
            _year(
                date(2025, 1, 1), grace_through=date(2026, 3, 15), runout_through=date(2026, 3, 31)
            ),
            _year(),
        ],
    )
    paid = _post(
        db, Transaction.simple(date(2026, 3, 1), "Clinic", medical.handle, book.card, "75")
    )

    draft = claim_draft(db, paid.handle).value

    assert draft is not None and draft.claim is not None
    [allocation] = draft.claim.allocations
    assert (allocation.account, allocation.funding_year_start) == (fsa.handle, date(2025, 1, 1))
    assert any("grace period" in note for note in draft.notes)


def test_no_funding_year_covering_the_date_leaves_no_allocation(db, book, medical):
    _fsa(db, book, "Health FSA", [_year(date(2025, 1, 1))])
    paid = _post(
        db, Transaction.simple(date(2026, 6, 1), "Clinic", medical.handle, book.card, "75")
    )

    draft = claim_draft(db, paid.handle).value

    assert draft is not None and draft.claim is not None
    assert draft.claim.allocations == ()
    assert any("none has a funding year covering 2026-06-01" in note for note in draft.notes)


def test_a_cost_already_claimed_names_that_claim(db, book, medical):
    fsa = _fsa(db, book, "Health FSA", [_year()])
    paid = _post(
        db, Transaction.simple(date(2026, 3, 8), "Clinic", medical.handle, book.card, "120")
    )
    claim = fsa_claims.save_claim(
        db,
        FsaClaim(
            service_date=date(2026, 3, 8),
            provider="Clinic",
            payments=[SplitLink(paid.handle, _split(paid, medical.handle).handle)],
            allocations=[FsaClaimAllocation(fsa.handle, YEAR)],
        ),
    )

    draft = claim_draft(db, paid.handle).value

    assert draft is not None
    assert (draft.claim, draft.existing) == (None, claim.handle)
    assert "Already on the claim 2026-03-08 Clinic" in draft.notes[0]


def test_a_reimbursable_expense_on_the_same_cost_is_the_payer(db, book, medical):
    _fsa(db, book, "Health FSA", [_year()])
    paid = _post(
        db, Transaction.simple(date(2026, 3, 8), "Clinic", medical.handle, book.card, "300")
    )
    receivable = Receivable(
        incurred_date=date(2026, 3, 8),
        payer="Insurer A",
        expected_amount=Money("200"),
        expenses=[SplitLink(paid.handle, _split(paid, medical.handle).handle)],
    )
    with db.transaction("Receivable") as txn:
        db.add_receivable(receivable, txn)

    draft = claim_draft(db, paid.handle).value

    assert draft is not None and draft.claim is not None
    assert draft.claim.receivable == receivable.handle
    assert any("Insurer A covers part" in note for note in draft.notes)


def test_only_expenses_on_accounts_claimed_before_are_linked(db, book, medical):
    fsa = _fsa(db, book, "Health FSA", [_year()])
    other = Account(name="Component B", atype=AccountType.EXPENSE, parent=book.expenses)
    with db.transaction("Other") as txn:
        db.add_account(other, txn)
    earlier = _post(
        db, Transaction.simple(date(2026, 2, 1), "Dentist", medical.handle, book.card, "90")
    )
    fsa_claims.save_claim(
        db,
        FsaClaim(
            service_date=date(2026, 2, 1),
            payments=[SplitLink(earlier.handle, _split(earlier, medical.handle).handle)],
            allocations=[FsaClaimAllocation(fsa.handle, YEAR)],
        ),
    )
    mixed = Transaction.simple(date(2026, 5, 1), "Store", medical.handle, book.card, "30")
    [card] = [split for split in mixed.splits if split.account == book.card]
    card.value = card.quantity = Money("-50")
    mixed.splits.append(Split(other.handle, Money("20"), Money("20")))
    _post(db, mixed)

    draft = claim_draft(db, mixed.handle).value

    assert draft is not None and draft.claim is not None
    assert draft.claim.payments == (
        ClaimLinkInput(mixed.handle, _split(mixed, medical.handle).handle),
    )


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("missing", "transaction.not_found"),
        ("no_cost", "claim.draft.no_cost"),
        ("no_fsa", "claim.draft.no_fsa_account"),
    ],
)
def test_what_starts_no_claim_is_refused_with_a_reason(db, book, medical, kind, code):
    if kind != "no_fsa":
        _fsa(db, book, "Health FSA", [_year()])
    if kind == "no_cost":
        moved = Transaction.simple(date(2026, 3, 1), "Transfer", book.savings, book.checking, "50")
        handle = _post(db, moved).handle
    elif kind == "no_fsa":
        paid = Transaction.simple(date(2026, 3, 1), "Clinic", medical.handle, book.card, "50")
        handle = _post(db, paid).handle
    else:
        handle = "no-such-transaction"
    before = _snapshot(db)

    result = claim_draft(db, handle)

    assert result.value is None
    assert result.errors[0].code == code
    assert code in service_error_codes()
    assert _snapshot(db) == before


def test_a_receivables_own_reclassification_starts_no_claim(db, book, medical, monkeypatch):
    from breadsched.gen.engine import split_links

    _fsa(db, book, "Health FSA", [_year()])
    owned = Transaction.simple(date(2026, 3, 1), "Reclass", medical.handle, book.checking, "10")
    _post(db, owned)
    monkeypatch.setattr(split_links, "owned_postings", lambda _db: {owned.handle})
    result = claim_draft(db, owned.handle)
    assert result.value is None
    assert result.errors[0].code == "claim.split.owned_posting"


def test_history_never_picks_an_fsa_without_a_year_for_the_date(db, book, medical):
    old = _fsa(db, book, "Old FSA", [_year(date(2025, 1, 1))])
    current = _fsa(db, book, "Current FSA", [_year()])
    earlier = _post(
        db, Transaction.simple(date(2025, 6, 1), "Dentist", medical.handle, book.card, "90")
    )
    fsa_claims.save_claim(
        db,
        FsaClaim(
            service_date=date(2025, 6, 1),
            payments=[SplitLink(earlier.handle, _split(earlier, medical.handle).handle)],
            allocations=[FsaClaimAllocation(old.handle, date(2025, 1, 1))],
        ),
    )
    paid = _post(
        db, Transaction.simple(date(2026, 5, 1), "Clinic", medical.handle, book.card, "60")
    )

    draft = claim_draft(db, paid.handle).value

    assert draft is not None and draft.claim is not None
    assert [item.account for item in draft.claim.allocations] == [current.handle]
