"""FSA movements from an administrator's statement are proposed for their claims."""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.engine import fsa_claims
from breadsched.gen.engine.fsa_claim_proposals import propose_claim_links
from breadsched.gen.lib import (
    Account,
    AccountType,
    FsaClaim,
    FsaClaimAllocation,
    FsaFundingYear,
    Money,
    Transaction,
)
from breadsched.gen.services import accept_claim_links, claim_link_proposals
from breadsched.presentation import claim_link_notice

YEAR = date(2026, 1, 1)


@pytest.fixture
def fsa(db, book):
    account = Account(name="Health FSA", atype=AccountType.FSA, parent=book.assets)
    account.fsa_years = [
        FsaFundingYear(YEAR, date(2026, 12, 31), Money("1500.00"), date(2027, 3, 31))
    ]
    medical = Account(name="Medical", atype=AccountType.EXPENSE, parent=book.expenses)
    with db.transaction("FSA") as txn:
        db.add_account(account, txn)
        db.add_account(medical, txn)
    return account.handle, medical.handle


def _claim(db, account, when, provider, eob):
    claim = FsaClaim(
        service_date=when,
        provider=provider,
        eob_responsibility=Money(eob),
        allocations=[FsaClaimAllocation(account, YEAR)],
    )
    return fsa_claims.save_claim(db, claim)


def _post(db, when, description, to, source, amount):
    transaction = Transaction.simple(when, description, to, source, amount)
    with db.transaction(description) as txn:
        db.add_transaction(transaction, txn)
    return transaction


def test_each_kind_of_statement_line_is_proposed_for_its_claim(db, book, fsa):
    account, medical = fsa
    clinic = _claim(db, account, date(2026, 3, 5), "Clinic", "80")
    dentist = _claim(db, account, date(2026, 3, 8), "Dentist", "200")
    # The household paid the dentist by card and put that payment on the claim.
    charge = _post(db, date(2026, 3, 8), "Dentist", medical, book.card, "200")
    fsa_claims.attach_transaction_to_claim(db, dentist.handle, charge.handle, role="payment")
    direct = _post(db, date(2026, 3, 6), "Clinic card", medical, account, "80.00")
    reimbursed = _post(db, date(2026, 3, 25), "Dentist claim paid", book.checking, account, "200")
    _post(db, date(2026, 3, 1), "Payroll FSA", account, book.salary, "100")  # needs no claim

    proposals = propose_claim_links(db)

    assert [(p.claim, p.transaction, p.role) for p in proposals] == [
        (clinic.handle, direct.handle, "direct_payment"),
        (dentist.handle, reimbursed.handle, "reimbursement"),
    ]
    assert proposals[0].claim_label == "2026-03-05 Clinic"
    assert proposals[1].amount == Money("200.00")
    assert "amount match" in proposals[1].reason
    assert claim_link_notice(len(proposals)) == (
        "2 FSA transactions look like they belong on claims; review the proposed claim "
        "links on the FSA Dashboard."
    )

    chosen = tuple((p.claim, p.transaction, p.split) for p in proposals)
    result = accept_claim_links(db, (*chosen, ("gone", "gone", "gone")))
    assert (result.value.linked, result.value.unchanged) == (2, 1)
    clinic_summary = fsa_claims.claim_summary(db, db.get_fsa_claim(clinic.handle))
    assert (clinic_summary.paid, clinic_summary.reimbursed) == (Money("80.00"), Money("80.00"))
    dentist_summary = fsa_claims.claim_summary(db, db.get_fsa_claim(dentist.handle))
    assert dentist_summary.reimbursed == Money("200.00")
    # Linked movements are never proposed again.
    assert propose_claim_links(db) == []


def test_a_provider_refund_is_proposed_once_the_payment_is_linked(db, book, fsa):
    account, medical = fsa
    clinic = _claim(db, account, date(2026, 3, 5), "Clinic", "80")
    direct = _post(db, date(2026, 3, 6), "Clinic card", medical, account, "80.00")
    fsa_claims.attach_transaction_to_claim(db, clinic.handle, direct.handle, role="direct_payment")
    refund = _post(db, date(2026, 3, 20), "Clinic refund", account, medical, "80.00")

    [proposal] = propose_claim_links(db, account=account)
    assert (proposal.transaction, proposal.role) == (refund.handle, "direct_refund")


def test_ambiguous_or_unmatched_lines_are_left_to_review(db, book, fsa):
    account, medical = fsa
    _claim(db, account, date(2026, 3, 5), "Clinic", "200")
    _claim(db, account, date(2026, 3, 6), "Lab", "200")
    _post(db, date(2026, 3, 25), "FSA claim paid", book.checking, account, "200")
    _post(db, date(2026, 3, 26), "FSA claim paid", book.checking, account, "37.13")
    assert propose_claim_links(db) == []
    assert claim_link_proposals(db).value == ()
    assert claim_link_notice(0) is None


def test_one_claim_is_proposed_for_one_line_per_batch(db, book, fsa):
    account, _medical = fsa
    dentist = _claim(db, account, date(2026, 3, 8), "Dentist", "200")
    charge = _post(db, date(2026, 3, 8), "Dentist", _medical, book.card, "200")
    fsa_claims.attach_transaction_to_claim(db, dentist.handle, charge.handle, role="payment")
    first = _post(db, date(2026, 3, 25), "Dentist claim paid", book.checking, account, "200")
    _post(db, date(2026, 3, 26), "Dentist claim paid", book.checking, account, "200")
    [proposal] = propose_claim_links(db)
    assert (proposal.claim, proposal.transaction) == (dentist.handle, first.handle)


def test_a_book_without_an_fsa_has_no_proposals(db, book):
    assert propose_claim_links(db) == []


def test_the_command_line_lists_and_links_proposals(db, book, fsa, tmp_path, capsys):
    import json

    from breadsched.cli.main import main

    account, medical = fsa
    clinic = _claim(db, account, date(2026, 3, 5), "Clinic", "80")
    _post(db, date(2026, 3, 6), "Clinic card", medical, account, "80.00")
    path = tmp_path / "proposals.breadsched"
    db.backup_to(str(path))

    assert main(["claims", str(path), "--proposals"]) == 0
    out = capsys.readouterr().out
    assert "2026-03-05 Clinic" in out and "Paid from the FSA card" in out
    assert main(["claims", str(path), "--link-proposals", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["linked"] == 1 and payload["proposals"][0]["claim"] == clinic.handle
    assert main(["claims", str(path), "--proposals"]) == 0
    assert "No proposed claim links." in capsys.readouterr().out
