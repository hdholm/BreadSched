"""FSA claim corrections: over-reimbursement and repayment, late EOB changes, and
closing or reopening a claim."""

import json
from datetime import date

import pytest

from breadsched.gen.engine import dashboard, fsa, fsa_claims
from breadsched.gen.engine.fsa_claim_report import claim_report
from breadsched.gen.engine.import_review import deletion_references
from breadsched.gen.lib import (
    Account,
    AccountType,
    FsaClaim,
    FsaClaimAllocation,
    FsaClaimEvent,
    FsaClaimRejection,
    FsaClaimSplitLink,
    FsaFundingYear,
    Money,
    Transaction,
)
from breadsched.gen.services import (
    ClaimAllocationInput,
    ClaimInput,
    ClaimLinkInput,
    CloseClaim,
    ReopenClaim,
    SaveClaim,
    close_claim,
    reopen_claim,
    save_claim,
)
from breadsched.gen.services.review import ReviewClaimAttachment, attach_review_claim

FsaClaimStatus = fsa_claims.FsaClaimStatus
YEAR = date(2026, 1, 1)
RUNOUT = date(2027, 3, 31)


@pytest.fixture
def fsa_account(db, book):
    account = Account(name="Health FSA", atype=AccountType.FSA, parent=book.assets)
    account.fsa_years = [FsaFundingYear(YEAR, date(2026, 12, 31), Money("1000.00"), RUNOUT)]
    with db.transaction("Add FSA") as txn:
        db.add_account(account, txn)
    return account


def _post(db, transaction):
    with db.transaction("Post") as txn:
        db.add_transaction(transaction, txn)
    return transaction


def _paid(db, book, when, amount):
    payment = _post(db, Transaction.simple(when, "Dentist", book.groceries, book.checking, amount))
    return FsaClaimSplitLink(payment.handle, payment.splits[0].handle)


def _reimbursed(db, book, account, when, amount):
    credit = _post(
        db, Transaction.simple(when, "FSA reimbursement", book.checking, account.handle, amount)
    )
    return FsaClaimSplitLink(credit.handle, credit.splits[1].handle)


def _repaid(db, book, account, when, amount):
    """Money paid back from checking into the FSA."""
    repayment = _post(
        db, Transaction.simple(when, "Repay FSA", account.handle, book.checking, amount)
    )
    return FsaClaimSplitLink(repayment.handle, repayment.splits[0].handle)


def _link(link):
    return ClaimLinkInput(link.transaction, link.split)


def _save(db, definition, handle=None, changed_on=None):
    return save_claim(db, SaveClaim(definition, existing_handle=handle, changed_on=changed_on))


def _definition(payment, reimbursements=(), repayments=(), eob="200.00", note="", account=None):
    return ClaimInput(
        service_date=date(2026, 3, 1),
        provider="Dentist",
        eob_responsibility=Money(eob) if eob is not None else None,
        payments=(_link(payment),),
        allocations=(
            ClaimAllocationInput(
                account.handle,
                YEAR,
                reimbursements=tuple(_link(item) for item in reimbursements),
                repayments=tuple(_link(item) for item in repayments),
            ),
        ),
        eob_note=note,
    )


@pytest.fixture
def reimbursed_claim(db, book, fsa_account):
    """A 200.00 service the FSA reimbursed in full."""
    payment = _paid(db, book, date(2026, 3, 2), "200.00")
    credit = _reimbursed(db, book, fsa_account, date(2026, 3, 20), "200.00")
    saved = _save(db, _definition(payment, [credit], account=fsa_account))
    assert saved.value is not None
    return saved.value.handle, payment, credit


def _summary(db, handle, as_of=date(2026, 6, 1)):
    return fsa_claims.claim_summary(db, db.get_fsa_claim(handle), as_of=as_of)


def test_a_lower_eob_after_reimbursement_is_an_over_reimbursement_to_repay(
    db, book, fsa_account, reimbursed_claim
):
    handle, payment, credit = reimbursed_claim
    assert _summary(db, handle).status is FsaClaimStatus.FULLY_REIMBURSED

    corrected = _save(
        db,
        _definition(payment, [credit], eob="150.00", note="Corrected EOB", account=fsa_account),
        handle,
        changed_on=date(2026, 5, 1),
    )
    assert corrected.value is not None
    claim = db.get_fsa_claim(handle)
    assert claim.events == [
        FsaClaimEvent(
            FsaClaimEvent.EOB_CHANGED,
            date(2026, 5, 1),
            Money("200.00"),
            Money("150.00"),
            "Corrected EOB",
        )
    ]
    summary = _summary(db, handle)
    assert summary.status is FsaClaimStatus.OVER_REIMBURSED
    assert (summary.reimbursed, summary.over_reimbursed) == (Money("200.00"), Money("50.00"))
    assert summary.remaining_reimbursable == Money(0)
    line = claim_report(db, as_of=date(2026, 6, 1)).lines[0]
    assert [(item.code, item.text) for item in line.attention] == [
        (
            "over",
            "Reimbursed 50.00 more than the claim allows since the EOB changed on "
            "2026-05-01 from 200.00 to 150.00: repay the FSA and link the repayment, "
            "or correct the claim",
        )
    ]


def test_linking_a_repayment_settles_the_claim_and_gives_the_election_back(
    db, book, fsa_account, reimbursed_claim
):
    handle, payment, credit = reimbursed_claim
    year = fsa_account.fsa_years[0]
    before = fsa.year_status(db, fsa_account, year, as_of=date(2026, 6, 1))
    assert (before.used, before.funded) == (Money("200.00"), Money(0))

    repayment = _repaid(db, book, fsa_account, date(2026, 5, 10), "50.00")
    saved = _save(
        db,
        _definition(payment, [credit], [repayment], eob="150.00", account=fsa_account),
        handle,
    )
    assert saved.value is not None
    summary = _summary(db, handle)
    assert summary.status is FsaClaimStatus.FULLY_REIMBURSED
    assert (summary.reimbursed, summary.repaid, summary.over_reimbursed) == (
        Money("150.00"),
        Money("50.00"),
        Money(0),
    )
    assert claim_report(db, as_of=date(2026, 6, 1)).needing_attention == ()
    # The repayment is the year's own money coming back, not payroll funding.
    after = fsa.year_status(db, fsa_account, year, as_of=date(2026, 6, 1))
    assert (after.used, after.funded, after.repaid) == (
        Money("150.00"),
        Money(0),
        Money("50.00"),
    )
    assert after.remaining == Money("850.00")
    transaction = db.get_transaction(repayment.transaction)
    assert transaction.splits[0].fsa_year_start == YEAR
    # A transaction the claim points at cannot be deleted silently.
    assert any("FSA claim" in text for text in deletion_references(db, transaction))


def test_a_raised_eob_after_reimbursement_reopens_the_claim(
    db, book, fsa_account, reimbursed_claim
):
    handle, _payment, _credit = reimbursed_claim
    claim = db.get_fsa_claim(handle)
    # The provider billed more; a corrected EOB raises the responsibility.
    extra = _paid(db, book, date(2026, 4, 1), "60.00")
    raised = ClaimInput(
        service_date=claim.service_date,
        provider=claim.provider,
        eob_responsibility=Money("260.00"),
        payments=tuple(_link(item) for item in (*claim.payments, extra)),
        allocations=(
            ClaimAllocationInput(
                fsa_account.handle,
                YEAR,
                reimbursements=tuple(_link(item) for item in claim.allocations[0].reimbursements),
            ),
        ),
    )
    assert _save(db, raised, handle, changed_on=date(2026, 4, 2)).value is not None
    summary = _summary(db, handle)
    assert summary.status is FsaClaimStatus.PARTIAL
    assert summary.remaining_reimbursable == Money("60.00")
    assert summary.reopened_by is not None and summary.reopened_by.kind == "eob_changed"
    line = claim_report(db, as_of=date(2026, 6, 1)).lines[0]
    assert [(item.code, item.text) for item in line.attention] == [
        (
            "reopened",
            "Reopened: the EOB changed on 2026-04-02 from 200.00 to 260.00; "
            "60.00 still to reimburse",
        )
    ]


def test_first_eob_and_unchanged_saves_record_no_history(db, book, fsa_account):
    payment = _paid(db, book, date(2026, 3, 2), "200.00")
    first = _save(db, _definition(payment, eob=None, account=fsa_account))
    handle = first.value.handle
    assert _save(db, _definition(payment, account=fsa_account), handle).value is not None
    assert _save(db, _definition(payment, account=fsa_account), handle).value is not None
    assert db.get_fsa_claim(handle).events == []


def test_closing_gives_up_the_rest_and_reopening_pursues_it_again(db, book, fsa_account):
    payment = _paid(db, book, date(2026, 3, 2), "300.00")
    claim = FsaClaim(
        service_date=date(2026, 3, 1),
        provider="Clinic",
        eob_responsibility=Money("300.00"),
        payments=[payment],
        allocations=[
            FsaClaimAllocation(
                fsa_account.handle,
                YEAR,
                rejections=[FsaClaimRejection(date(2026, 4, 1), Money("300.00"), "No bill")],
            )
        ],
    )
    fsa_claims.save_claim(db, claim)
    as_of = date(2026, 6, 1)
    assert _summary(db, claim.handle).status is FsaClaimStatus.OPEN

    closed = close_claim(db, CloseClaim(claim.handle, date(2026, 5, 1), "Not worth appealing"))
    assert closed.value is not None
    summary = _summary(db, claim.handle)
    assert summary.status is FsaClaimStatus.CLOSED and summary.status.settled
    assert (summary.remaining_reimbursable, summary.forgone) == (Money(0), Money("300.00"))
    assert claim_report(db, as_of=as_of).needing_attention == ()
    assert dashboard.build(db, as_of=as_of).summary()["fsa_claims_attention"] == 0

    # Editing a closed claim keeps it closed, with its history.
    stored = db.get_fsa_claim(claim.handle)
    stored.description = "Annual cleaning"
    fsa_claims.save_claim(db, stored)
    assert db.get_fsa_claim(claim.handle).closed_on == date(2026, 5, 1)

    reopened = reopen_claim(db, ReopenClaim(claim.handle, date(2026, 5, 20), "Appeal won"))
    assert reopened.value is not None
    summary = _summary(db, claim.handle)
    assert summary.status is FsaClaimStatus.OPEN
    assert summary.remaining_reimbursable == Money("300.00")
    codes = [(item.code, item.text) for item in claim_report(db, as_of=as_of).lines[0].attention]
    assert ("reopened", "Reopened on 2026-05-20 (Appeal won): 300.00 still to reimburse") in codes
    assert [event.kind for event in db.get_fsa_claim(claim.handle).events] == [
        "closed",
        "reopened",
    ]


def test_closing_and_reopening_refuse_impossible_requests_without_changes(
    db, book, fsa_account, reimbursed_claim
):
    handle, _payment, _credit = reimbursed_claim
    before = db.get_fsa_claim(handle).serialize()

    def code(result):
        assert result.value is None
        return result.errors[0].code

    assert code(reopen_claim(db, ReopenClaim(handle, date(2026, 5, 1)))) == (
        "claim.reopen.not_closed"
    )
    assert code(close_claim(db, CloseClaim(handle, date(2026, 2, 1)))) == (
        "claim.close.before_service"
    )
    assert code(close_claim(db, CloseClaim("missing", date(2026, 5, 1)))) == "claim.not_found"
    assert db.get_fsa_claim(handle).serialize() == before
    assert close_claim(db, CloseClaim(handle, date(2026, 5, 1))).value is not None
    closed = db.get_fsa_claim(handle).serialize()
    assert code(close_claim(db, CloseClaim(handle, date(2026, 5, 2)))) == (
        "claim.close.already_closed"
    )
    assert code(reopen_claim(db, ReopenClaim(handle, date(2026, 4, 30)))) == (
        "claim.reopen.before_close"
    )
    assert db.get_fsa_claim(handle).serialize() == closed


def test_a_bad_repayment_is_refused_and_the_stored_claim_is_kept(
    db, book, fsa_account, reimbursed_claim
):
    handle, payment, credit = reimbursed_claim
    before = db.get_fsa_claim(handle).serialize()
    other = Account(name="Other FSA", atype=AccountType.FSA, parent=book.assets)
    other.fsa_years = list(fsa_account.fsa_years)
    with db.transaction("Other FSA") as txn:
        db.add_account(other, txn)
    elsewhere = _repaid(db, book, other, date(2026, 5, 10), "50.00")

    def refused(repayments):
        result = _save(
            db,
            _definition(payment, [credit], repayments, eob="150.00", account=fsa_account),
            handle,
        )
        assert result.value is None
        return result.errors[0].code

    # A reimbursement is money out of the FSA, not a repayment into it.
    assert refused([credit]) == "claim.repayment.duplicate"
    outgoing = _reimbursed(db, book, fsa_account, date(2026, 5, 11), "10.00")
    assert refused([outgoing]) == "claim.repayment.direction"
    assert refused([elsewhere]) == "claim.repayment.account.mismatch"
    assert db.get_fsa_claim(handle).serialize() == before


def test_review_attaches_a_repayment_to_the_claims_year_even_after_run_out(
    db, book, fsa_account, reimbursed_claim
):
    handle, payment, credit = reimbursed_claim
    claim = db.get_fsa_claim(handle)
    claim.eob_responsibility = Money("150.00")
    fsa_claims.save_claim(db, claim)
    late = _repaid(db, book, fsa_account, date(2027, 6, 1), "50.00")
    transaction = db.get_transaction(late.transaction)
    suggestions = fsa_claims.suggest_claims_for_transaction(db, transaction)
    assert [(item.claim.handle, item.role) for item in suggestions] == [(handle, "repayment")]
    result = attach_review_claim(
        db, ReviewClaimAttachment(late.transaction, handle, "repayment", late.split)
    )
    assert result.value is not None
    stored = db.get_fsa_claim(handle)
    assert stored.allocations[0].repayments == [late]
    assert _summary(db, handle, as_of=date(2027, 7, 1)).status is FsaClaimStatus.FULLY_REIMBURSED


def test_repayments_are_suggested_only_for_over_reimbursed_claims(
    db, book, fsa_account, reimbursed_claim
):
    _handle, _payment, _credit = reimbursed_claim
    funding = _repaid(db, book, fsa_account, date(2026, 5, 1), "40.00")
    transaction = db.get_transaction(funding.transaction)
    assert fsa_claims.suggest_claims_for_transaction(db, transaction) == []


def test_new_claim_fields_round_trip_and_old_claims_still_load(db, book, fsa_account):
    claim = FsaClaim(
        service_date=date(2026, 3, 1),
        allocations=[
            FsaClaimAllocation(fsa_account.handle, YEAR, repayments=[FsaClaimSplitLink("t", "s")])
        ],
        closed_on=date(2026, 5, 1),
        close_reason="Gave up",
        events=[FsaClaimEvent(FsaClaimEvent.CLOSED, date(2026, 5, 1), note="Gave up")],
    )
    copy = FsaClaim.from_dict(json.loads(json.dumps(claim.serialize())))
    assert copy.serialize() == claim.serialize()
    old = claim.serialize()
    for key in ("closed_on", "close_reason", "events"):
        del old[key]
    del old["allocations"][0]["repayments"]
    legacy = FsaClaim.from_dict(old)
    assert (legacy.closed_on, legacy.events, legacy.allocations[0].repayments) == (None, [], [])


def test_the_command_line_closes_reopens_and_lists_history(
    db, book, fsa_account, reimbursed_claim, tmp_path, capsys
):
    from breadsched.cli.main import main

    handle, _payment, _credit = reimbursed_claim
    path = tmp_path / "claims.breadsched"
    db.backup_to(str(path))
    prefix = handle[:10]
    assert (
        main(["claims", str(path), "--close", prefix, "--on", "2026-05-01", "--reason", "Done"])
        == 0
    )
    assert "Closed the Dentist claim of 2026-03-01" in capsys.readouterr().out
    assert main(["claims", str(path), "--close", prefix]) != 0
    assert "already closed" in capsys.readouterr().err
    assert main(["claims", str(path), "--reopen", prefix, "--on", "2026-05-02"]) == 0
    capsys.readouterr()
    assert main(["claims", str(path), "--history", prefix]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "2026-05-01 Closed: Done",
        "2026-05-02 Reopened",
    ]
    assert main(["claims", str(path), "--as-of", "2026-06-01", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claims"][0]["repaid"] == "0.00"
    assert payload["claims"][0]["closed_on"] is None
