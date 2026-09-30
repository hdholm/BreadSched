"""FSA plan rules: a carryover of unused election into the next plan year, or a
grace period for claiming services after the plan year ends."""

from datetime import date

import pytest

from breadsched.gen.engine import fsa, fsa_claims
from breadsched.gen.lib import (
    Account,
    AccountType,
    FsaClaim,
    FsaClaimSplitLink,
    FsaFundingYear,
    Money,
    Transaction,
)

FIRST = date(2026, 1, 1)
SECOND = date(2027, 1, 1)


def _account(db, book, *, carryover=None, grace=None):
    account = Account(name="Health FSA", atype=AccountType.FSA, parent=book.assets)
    account.fsa_years = [
        FsaFundingYear(
            FIRST,
            date(2026, 12, 31),
            Money("1000.00"),
            date(2027, 3, 31),
            carryover_limit=Money(carryover) if carryover is not None else None,
            grace_through=grace,
        ),
        FsaFundingYear(SECOND, date(2027, 12, 31), Money("500.00"), date(2028, 3, 31)),
    ]
    with db.transaction("FSA") as txn:
        db.add_account(account, txn)
    return account


def _used(db, book, account, when, amount, year=None):
    """A reimbursement out of the FSA, optionally assigned to a funding year."""
    credit = Transaction.simple(when, "FSA pays", book.checking, account.handle, amount)
    credit.splits[1].fsa_year_start = year
    with db.transaction("Use") as txn:
        db.add_transaction(credit, txn)
    return credit


def _status(db, account, index, as_of):
    return fsa.year_status(db, account, account.fsa_years[index], as_of=as_of)


def test_plan_rules_are_checked_and_round_trip():
    through = date(2026, 12, 31)
    runout = date(2027, 3, 31)
    with pytest.raises(ValueError, match="carryover limit"):
        FsaFundingYear(FIRST, through, Money(100), carryover_limit=Money(-1))
    with pytest.raises(ValueError, match="before the funding year"):
        FsaFundingYear(FIRST, through, Money(100), grace_through=date(2026, 12, 1))
    with pytest.raises(ValueError, match="after the run-out"):
        FsaFundingYear(FIRST, through, Money(100), runout, grace_through=date(2027, 4, 1))
    year = FsaFundingYear(FIRST, through, Money(100), runout, carryover_limit=Money(640))
    assert FsaFundingYear.from_dict(year.serialize()) == year
    old = year.serialize()
    del old["carryover_limit"], old["grace_through"]
    legacy = FsaFundingYear.from_dict(old)
    assert (legacy.carryover_limit, legacy.grace_through) == (None, None)
    grace = FsaFundingYear(FIRST, through, Money(100), runout, grace_through=date(2027, 3, 15))
    assert (legacy.service_through, grace.service_through) == (through, date(2027, 3, 15))


def test_unused_election_up_to_the_limit_carries_into_the_next_year(db, book):
    account = _account(db, book, carryover="640.00")
    _used(db, book, account, date(2026, 6, 1), "200.00")
    # During the run-out the unused 800.00 is still the first year's.
    during = _status(db, account, 0, date(2027, 2, 1))
    assert (during.remaining, during.carried_over, during.forfeited) == (
        Money("800.00"),
        Money(0),
        Money(0),
    )
    assert _status(db, account, 1, date(2027, 2, 1)).carried_in == Money(0)
    # Once the run-out ends, 640.00 carries over and only 160.00 is forfeited.
    closed = _status(db, account, 0, date(2027, 4, 1))
    assert (closed.remaining, closed.carried_over, closed.forfeited) == (
        Money(0),
        Money("640.00"),
        Money("160.00"),
    )
    second = _status(db, account, 1, date(2027, 4, 1))
    assert (second.carried_in, second.remaining) == (Money("640.00"), Money("1140.00"))
    # The carried money pays for the second year's claims like its election.
    _used(db, book, account, date(2027, 5, 1), "1000.00")
    assert _status(db, account, 1, date(2027, 6, 1)).remaining == Money("140.00")


def test_less_than_the_limit_carries_in_full(db, book):
    account = _account(db, book, carryover="640.00")
    _used(db, book, account, date(2026, 6, 1), "700.00")
    closed = _status(db, account, 0, date(2027, 4, 1))
    assert (closed.carried_over, closed.forfeited) == (Money("300.00"), Money(0))
    assert _status(db, account, 1, date(2027, 4, 1)).carried_in == Money("300.00")


def test_without_a_carryover_the_unused_election_is_forfeited(db, book):
    account = _account(db, book)
    closed = _status(db, account, 0, date(2027, 4, 1))
    assert (closed.carried_over, closed.forfeited) == (Money(0), Money("1000.00"))
    assert _status(db, account, 1, date(2027, 4, 1)).carried_in == Money(0)


def test_a_grace_period_service_can_be_claimed_against_the_earlier_year(db, book):
    account = _account(db, book, grace=date(2027, 3, 15))
    service = date(2027, 2, 10)
    assert fsa_claims.claim_year_window(db, service) == (FIRST, date(2028, 3, 31))
    payment = Transaction.simple(service, "Clinic", book.groceries, book.checking, "90.00")
    credit = Transaction.simple(date(2027, 3, 1), "FSA pays", book.checking, account.handle, "90")
    with db.transaction("Grace") as txn:
        db.add_transaction(payment, txn)
        db.add_transaction(credit, txn)
    claim = FsaClaim(
        service_date=service,
        provider="Clinic",
        eob_responsibility=Money("90.00"),
        payments=[FsaClaimSplitLink(payment.handle, payment.splits[0].handle)],
    )
    fsa_claims.save_claim(db, claim)
    # A grace-period service could draw on either year, so the year must be chosen.
    with pytest.raises(fsa_claims.FsaClaimError) as refused:
        fsa_claims.attach_transaction_to_claim(
            db, claim.handle, credit.handle, role="reimbursement"
        )
    assert refused.value.code == "claim.attachment.funding_year.required"
    fsa_claims.attach_transaction_to_claim(
        db, claim.handle, credit.handle, role="reimbursement", funding_year_start=FIRST
    )
    assert db.get_fsa_claim(claim.handle).allocations[0].funding_year_start == FIRST
    as_of = date(2027, 3, 20)
    assert _status(db, account, 0, as_of).used == Money("90.00")
    assert _status(db, account, 1, as_of).used == Money(0)


def test_with_both_rules_grace_period_claims_come_before_the_carryover(db, book):
    account = _account(db, book, carryover="640.00", grace=date(2027, 3, 15))
    _used(db, book, account, date(2026, 6, 1), "200.00")
    # A grace-period service claimed against the first year uses its money first.
    _used(db, book, account, date(2027, 3, 1), "300.00", year=FIRST)
    closed = _status(db, account, 0, date(2027, 4, 1))
    assert (closed.used, closed.carried_over, closed.forfeited) == (
        Money("500.00"),
        Money("500.00"),
        Money(0),
    )
    second = _status(db, account, 1, date(2027, 4, 1))
    assert (second.used, second.carried_in, second.remaining) == (
        Money(0),
        Money("500.00"),
        Money("1000.00"),
    )
