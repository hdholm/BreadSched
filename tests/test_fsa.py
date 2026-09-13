"""FSA benefit-year availability is separate from custodial cash balance."""

from datetime import date, timedelta

from breadsched.gen.engine import fsa
from breadsched.gen.lib import (
    Account,
    AccountKind,
    AccountType,
    FsaFundingYear,
    Money,
    Split,
    Transaction,
)


def _fsa_account(db, book):
    account = Account(
        name="Health FSA",
        atype=AccountType.ASSET,
        parent=book.assets,
    )
    account.kind = AccountKind.FSA
    account.fsa_years = [
        FsaFundingYear(
            date(2026, 7, 1),
            date(2027, 6, 30),
            Money("3000.00"),
            date(2027, 9, 30),
        )
    ]
    with db.transaction("Add FSA") as txn:
        db.add_account(account, txn)
    return account


def test_election_not_ledger_balance_controls_available_benefit(db, book):
    account = _fsa_account(db, book)
    with db.transaction("FSA activity") as txn:
        db.add_transaction(
            Transaction.simple(
                date(2026, 7, 15),
                "First payroll funding",
                account.handle,
                book.salary,
                "125.00",
            ),
            txn,
        )
        db.add_transaction(
            Transaction.simple(
                date(2026, 8, 1),
                "Medical claim",
                book.groceries,
                account.handle,
                "400.00",
            ),
            txn,
        )

    status = fsa.year_status(db, account, account.fsa_years[0], as_of=date(2026, 8, 2))
    assert status.funded == Money("125.00")
    assert status.used == Money("400.00")
    assert status.remaining == Money("2600.00")
    assert status.overage == Money(0)


def test_runout_claim_can_be_explicitly_assigned_to_prior_year(db, book):
    account = _fsa_account(db, book)
    claim = Transaction(post_date=date(2027, 8, 15), description="Prior-year claim")
    claim.add_split(Split(book.groceries, Money("250.00")))
    claim.add_split(
        Split(
            account.handle,
            Money("-250.00"),
            fsa_year_start=date(2026, 7, 1),
        )
    )
    with db.transaction("Post run-out claim") as txn:
        db.add_transaction(claim, txn)

    status = fsa.year_status(db, account, account.fsa_years[0], as_of=date(2027, 8, 20))
    assert status.phase == "run-out"
    assert status.used == Money("250.00")
    assert status.remaining == Money("2750.00")


def test_closed_year_reports_forfeited_remaining_funds(db, book):
    account = _fsa_account(db, book)
    status = fsa.year_status(db, account, account.fsa_years[0], as_of=date(2027, 10, 1))
    assert status.phase == "closed"
    assert status.remaining == Money(0)
    assert status.forfeited == Money("3000.00")


def test_fsa_years_round_trip_with_account_serialization():
    account = Account(name="FSA", atype=AccountType.ASSET)
    account.kind = AccountKind.FSA
    account.fsa_years = [
        FsaFundingYear(date(2026, 1, 1), date(2026, 12, 31), Money("3200"), date(2027, 3, 31))
    ]
    restored = Account.from_dict(account.serialize())
    assert restored.fsa_years == account.fsa_years


def test_dashboard_limits_recent_closed_year_to_90_days_after_runout(db, book):
    account = _fsa_account(db, book)
    last_day = fsa.dashboard_statuses(db, as_of=date(2027, 9, 30))
    assert [(item.account.handle, item.phase) for item in last_day] == [(account.handle, "run-out")]
    deadline = date(2027, 9, 30)
    history = fsa.dashboard_statuses(db, as_of=deadline + timedelta(days=90))
    assert history[0].phase == "closed"
    assert history[0].forfeited == account.fsa_years[0].election
    assert fsa.dashboard_statuses(db, as_of=deadline + timedelta(days=91)) == []
    assert fsa.dashboard_statuses(db, as_of=deadline + timedelta(days=1), recent_closed=0) == []


def test_dashboard_keeps_current_year_when_prior_year_closes(db, book):
    account = _fsa_account(db, book)
    current = FsaFundingYear(date(2027, 7, 1), date(2028, 6, 30), Money("2400"))
    account.fsa_years.append(current)
    with db.transaction("Add next benefit year") as txn:
        db.commit_account(account, txn)

    statuses = fsa.dashboard_statuses(db, as_of=date(2028, 1, 1))

    assert [item.year for item in statuses] == [current]
    assert statuses[0].remaining == current.election


def test_dashboard_uses_year_end_when_no_runout_and_keeps_only_latest_closed(db, book):
    account = _fsa_account(db, book)
    first = FsaFundingYear(date(2025, 1, 1), date(2025, 12, 31), Money("1200"))
    second = FsaFundingYear(date(2026, 1, 1), date(2026, 1, 31), Money("100"))
    account.fsa_years = [first, second]
    with db.transaction("Set generic benefit years") as txn:
        db.commit_account(account, txn)

    statuses = fsa.dashboard_statuses(db, as_of=date(2026, 2, 1))

    assert [item.year for item in statuses] == [second]
    assert fsa.dashboard_statuses(db, as_of=second.through + timedelta(days=90))[0].year == second
    assert fsa.dashboard_statuses(db, as_of=second.through + timedelta(days=91)) == []


def _second_fsa_account(db, book):
    account = Account(
        name="Spouse FSA",
        atype=AccountType.ASSET,
        parent=book.assets,
    )
    account.kind = AccountKind.FSA
    account.fsa_years = [
        FsaFundingYear(
            date(2026, 1, 1),
            date(2026, 12, 31),
            Money("2000.00"),
            date(2027, 3, 31),
        )
    ]
    with db.transaction("Add second FSA") as txn:
        db.add_account(account, txn)
    return account


def test_claim_can_coordinate_multiple_fsa_funding_sources(db, book):
    from breadsched.gen.engine import fsa_claims
    from breadsched.gen.lib import FsaClaim, FsaClaimAllocation, FsaClaimSplitLink

    primary = _fsa_account(db, book)
    secondary = _second_fsa_account(db, book)
    payment = Transaction.simple(
        date(2026, 8, 5), "Orthodontist", book.groceries, book.checking, "900.00"
    )
    reimbursement_one = Transaction.simple(
        date(2026, 8, 15), "Primary FSA", book.checking, primary.handle, "500.00"
    )
    reimbursement_two = Transaction.simple(
        date(2026, 8, 20), "Spouse FSA", book.checking, secondary.handle, "300.00"
    )
    with db.transaction("Claim activity") as txn:
        db.add_transaction(payment, txn)
        db.add_transaction(reimbursement_one, txn)
        db.add_transaction(reimbursement_two, txn)

    claim = FsaClaim(
        service_date=date(2026, 8, 1),
        provider="Orthodontist",
        eob_responsibility=Money("900.00"),
        payments=[FsaClaimSplitLink(payment.handle, payment.splits[0].handle)],
        allocations=[
            FsaClaimAllocation(
                primary.handle,
                primary.fsa_years[0].start,
                Money("500.00"),
                [FsaClaimSplitLink(reimbursement_one.handle, reimbursement_one.splits[1].handle)],
            ),
            FsaClaimAllocation(
                secondary.handle,
                secondary.fsa_years[0].start,
                Money("400.00"),
                [FsaClaimSplitLink(reimbursement_two.handle, reimbursement_two.splits[1].handle)],
            ),
        ],
    )

    fsa_claims.save_claim(db, claim)
    summary = fsa_claims.claim_summary(db, claim, as_of=date(2026, 8, 21))

    assert summary.paid == Money("900.00")
    assert summary.reimbursed == Money("800.00")
    assert summary.remaining_reimbursable == Money("100.00")
    assert summary.status is fsa_claims.FsaClaimStatus.PARTIAL
    stored = fsa_claims.iter_claims(db)[0]
    assert len(stored.allocations) == 2


def test_claim_waits_for_eob_and_closes_when_no_fsa_funds_remain(db, book):
    from breadsched.gen.engine import fsa_claims
    from breadsched.gen.lib import FsaClaim, FsaClaimAllocation, FsaClaimSplitLink

    account = _fsa_account(db, book)
    payment = Transaction.simple(
        date(2027, 8, 5), "Dental service", book.groceries, book.checking, "450.00"
    )
    with db.transaction("Claim payment") as txn:
        db.add_transaction(payment, txn)
    claim = FsaClaim(
        service_date=date(2027, 6, 20),
        provider="Dentist",
        payments=[FsaClaimSplitLink(payment.handle, payment.splits[0].handle)],
        allocations=[FsaClaimAllocation(account.handle, account.fsa_years[0].start)],
    )
    fsa_claims.save_claim(db, claim)

    waiting = fsa_claims.claim_summary(db, claim, as_of=date(2027, 8, 6))
    assert waiting.status is fsa_claims.FsaClaimStatus.WAITING_EOB

    claim.eob_responsibility = Money("450.00")
    closed = fsa_claims.claim_summary(db, claim, as_of=date(2027, 10, 1))
    assert closed.status is fsa_claims.FsaClaimStatus.CLOSED_NO_FUNDS


def test_claim_refunds_reduce_net_paid_and_rejections_do_not_reimburse(db, book):
    from breadsched.gen.engine import fsa_claims
    from breadsched.gen.lib import (
        FsaClaim,
        FsaClaimAllocation,
        FsaClaimRejection,
        FsaClaimSplitLink,
    )

    account = _fsa_account(db, book)
    payment = Transaction.simple(
        date(2026, 4, 1), "Medical payment", book.groceries, book.checking, "500.00"
    )
    refund = Transaction.simple(
        date(2026, 4, 20), "Provider refund", book.checking, book.groceries, "100.00"
    )
    with db.transaction("Claim adjustment") as txn:
        db.add_transaction(payment, txn)
        db.add_transaction(refund, txn)
    claim = FsaClaim(
        service_date=date(2026, 3, 15),
        provider="Clinic",
        eob_responsibility=Money("450.00"),
        payments=[FsaClaimSplitLink(payment.handle, payment.splits[0].handle)],
        refunds=[FsaClaimSplitLink(refund.handle, refund.splits[1].handle)],
        allocations=[
            FsaClaimAllocation(
                account.handle,
                account.fsa_years[0].start,
                rejections=[FsaClaimRejection(date(2026, 4, 10), Money("200.00"), "Denied")],
            )
        ],
    )
    fsa_claims.save_claim(db, claim)
    summary = fsa_claims.claim_summary(db, claim, as_of=date(2026, 4, 21))
    assert summary.paid == Money("500.00")
    assert summary.refunds == Money("100.00")
    assert summary.net_paid == Money("400.00")
    assert summary.reimbursable == Money("400.00")
    assert summary.rejected == Money("200.00")
    assert summary.reimbursed == Money(0)
    assert summary.status is fsa_claims.FsaClaimStatus.OPEN


def test_review_attachment_links_payment_and_reimbursement_to_claim(db, book):
    from breadsched.gen.engine import fsa_claims
    from breadsched.gen.lib import FsaClaim

    account = _fsa_account(db, book)
    payment = Transaction.simple(
        date(2026, 5, 2), "Medical payment", book.groceries, book.checking, "300.00"
    )
    reimbursement = Transaction.simple(
        date(2026, 5, 10), "FSA reimbursement", book.checking, account.handle, "200.00"
    )
    with db.transaction("Claim activity") as txn:
        db.add_transaction(payment, txn)
        db.add_transaction(reimbursement, txn)
    claim = FsaClaim(
        service_date=date(2026, 5, 1),
        provider="Clinic",
        eob_responsibility=Money("300.00"),
    )
    fsa_claims.save_claim(db, claim)

    fsa_claims.attach_transaction_to_claim(db, claim.handle, payment.handle, role="payment")
    fsa_claims.attach_transaction_to_claim(
        db, claim.handle, reimbursement.handle, role="reimbursement"
    )

    stored = fsa_claims.iter_claims(db)[0]
    assert stored.payments[0].transaction == payment.handle
    assert stored.allocations[0].account == account.handle
    assert stored.allocations[0].reimbursements[0].transaction == reimbursement.handle
    summary = fsa_claims.claim_summary(db, stored, as_of=date(2026, 5, 11))
    assert summary.paid == Money("300.00")
    assert summary.reimbursed == Money("200.00")
    assert summary.remaining_reimbursable == Money("100.00")


def test_claim_save_is_atomic_and_undoable(db, book):
    from breadsched.gen.engine import fsa_claims
    from breadsched.gen.lib import FsaClaim, FsaClaimAllocation, FsaClaimSplitLink

    account = _fsa_account(db, book)
    reimbursement = Transaction.simple(
        date(2026, 5, 10), "FSA reimbursement", book.checking, account.handle, "200.00"
    )
    with db.transaction("Add reimbursement") as txn:
        db.add_transaction(reimbursement, txn)
    reimbursement_split = reimbursement.splits[1]
    assert reimbursement_split.fsa_year_start is None

    claim = FsaClaim(
        service_date=date(2026, 5, 1),
        provider="Generic provider",
        allocations=[
            FsaClaimAllocation(
                account.handle,
                account.fsa_years[0].start,
                reimbursements=[
                    FsaClaimSplitLink(reimbursement.handle, reimbursement_split.handle)
                ],
            )
        ],
    )
    fsa_claims.save_claim(db, claim)

    stored_transaction = db.get_transaction(reimbursement.handle)
    stored_split = next(
        split for split in stored_transaction.splits if split.handle == reimbursement_split.handle
    )
    assert db.get_fsa_claim(claim.handle) is not None
    assert stored_split.fsa_year_start == account.fsa_years[0].start

    assert db.undo() is True
    restored_transaction = db.get_transaction(reimbursement.handle)
    restored_split = next(
        split for split in restored_transaction.splits if split.handle == reimbursement_split.handle
    )
    assert db.get_fsa_claim(claim.handle) is None
    assert restored_split.fsa_year_start is None

    assert db.redo() is True
    redone_transaction = db.get_transaction(reimbursement.handle)
    redone_split = next(
        split for split in redone_transaction.splits if split.handle == reimbursement_split.handle
    )
    assert db.get_fsa_claim(claim.handle) is not None
    assert redone_split.fsa_year_start == account.fsa_years[0].start


def test_claim_suggestions_rank_service_context(db, book):
    from breadsched.gen.engine import fsa_claims
    from breadsched.gen.lib import FsaClaim

    close = FsaClaim(
        service_date=date(2026, 5, 1),
        provider="Easton Dental",
        description="Crown",
    )
    distant = FsaClaim(
        service_date=date(2026, 1, 1),
        provider="Unrelated clinic",
    )
    fsa_claims.save_claim(db, close)
    fsa_claims.save_claim(db, distant)
    payment = Transaction.simple(
        date(2026, 5, 3), "Easton Dental crown payment", book.groceries, book.checking, "400.00"
    )

    suggestions = fsa_claims.suggest_claims_for_transaction(db, payment)

    assert suggestions[0].claim.handle == close.handle
    assert suggestions[0].role == "payment"
    assert suggestions[0].split_handle == payment.splits[0].handle
    assert suggestions[0].score > suggestions[1].score
    assert "likely provider payment" in suggestions[0].reason
    assert "description match" in suggestions[0].reason
