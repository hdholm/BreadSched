"""Dependent care FSAs: pay as contributed, no EOB, no carryover.

A health care FSA makes the whole election available on the first day of the plan
year and waits for an EOB before it says what a claim is owed. A dependent care
FSA pays only what has been contributed so far, a daycare bill has no EOB, and
the plan carries nothing over.
"""

from __future__ import annotations

from datetime import date

import pytest

from breadsched.gen.engine import fsa, fsa_claims
from breadsched.gen.engine.fsa_claims import FsaClaimStatus
from breadsched.gen.lib import (
    Account,
    AccountType,
    FsaClaim,
    FsaClaimAllocation,
    FsaClaimSplitLink,
    FsaFundingYear,
    Money,
    Transaction,
)
from breadsched.gen.services import SaveAccount, save_account

YEAR = date(2026, 1, 1)


def _post(db, transaction):
    with db.transaction(transaction.description) as txn:
        db.add_transaction(transaction, txn)
    return transaction


@pytest.fixture
def dependent_care(db, book):
    account = Account(name="Dependent care FSA", atype=AccountType.FSA, parent=book.assets)
    account.fsa_dependent_care = True
    account.fsa_years = [
        FsaFundingYear(YEAR, date(2026, 12, 31), Money("5000.00"), date(2027, 3, 31))
    ]
    with db.transaction("FSA") as txn:
        db.add_account(account, txn)
    for month in (1, 2):
        _post(
            db,
            Transaction.simple(
                date(2026, month, 15), "Payroll DCFSA", account.handle, book.salary, "416.67"
            ),
        )
    return db.get_account(account.handle)


def test_only_what_has_been_contributed_is_available(db, book, dependent_care):
    status = fsa.year_status(
        db, dependent_care, dependent_care.fsa_years[0], as_of=date(2026, 3, 1)
    )
    assert status.dependent_care
    assert status.funded == Money("833.34")
    assert status.remaining == Money("833.34")  # not the 5,000.00 election

    _post(
        db,
        Transaction.simple(
            date(2026, 3, 2), "DCFSA reimbursement", book.checking, dependent_care.handle, "600.00"
        ),
    )
    status = fsa.year_status(
        db, dependent_care, dependent_care.fsa_years[0], as_of=date(2026, 3, 5)
    )
    assert (status.used, status.remaining) == (Money("600.00"), Money("233.34"))


def test_a_health_fsa_still_offers_the_whole_election(db, book, dependent_care):
    health = Account.from_dict(dependent_care.serialize())
    health.fsa_dependent_care = False
    status = fsa.year_status(db, health, health.fsa_years[0], as_of=date(2026, 3, 1))
    assert status.remaining == Money("5000.00") and not status.dependent_care


def test_a_dependent_care_claim_needs_no_eob_and_waits_for_contributions(db, book, dependent_care):
    bill = _post(
        db,
        Transaction.simple(date(2026, 3, 1), "Daycare", book.utilities, book.checking, "1200.00"),
    )
    claim = FsaClaim(
        service_date=date(2026, 3, 1),
        provider="Daycare",
        payments=[FsaClaimSplitLink(bill.handle, bill.splits[0].handle)],
        allocations=[FsaClaimAllocation(dependent_care.handle, YEAR)],
    )
    fsa_claims.save_claim(db, claim)
    summary = fsa_claims.claim_summary(db, db.get_fsa_claim(claim.handle), as_of=date(2026, 3, 5))
    assert summary.status is FsaClaimStatus.OPEN
    assert summary.reimbursable == Money("1200.00")
    assert summary.remaining_reimbursable == Money("1200.00")

    # Contributions used up: the claim waits for the next ones while the year is open.
    _post(
        db,
        Transaction.simple(
            date(2026, 3, 6), "DCFSA reimbursement", book.checking, dependent_care.handle, "833.34"
        ),
    )
    summary = fsa_claims.claim_summary(db, db.get_fsa_claim(claim.handle), as_of=date(2026, 3, 10))
    assert summary.status is FsaClaimStatus.OPEN
    assert summary.remaining_reimbursable == Money("1200.00")


def test_a_dependent_care_fsa_cannot_carry_over(db, book, dependent_care):
    changed = Account.from_dict(dependent_care.serialize())
    changed.fsa_years = [
        FsaFundingYear(
            YEAR, date(2026, 12, 31), Money("5000.00"), date(2027, 3, 31), Money("500.00")
        )
    ]
    before = dependent_care.serialize()
    result = save_account(
        db, SaveAccount(changed, existing_handle=changed.handle, source=dependent_care)
    )
    assert not result.ok
    assert result.errors[0].code == "account.fsa.dependent_care.carryover"
    assert db.get_account(dependent_care.handle).serialize() == before


def test_the_flag_round_trips_and_older_accounts_are_health_fsas(dependent_care):
    again = Account.from_dict(dependent_care.serialize())
    assert again.fsa_dependent_care
    data = dependent_care.serialize()
    del data["fsa_dependent_care"]
    assert not Account.from_dict(data).fsa_dependent_care


def test_cli_marks_an_fsa_as_dependent_care(tmp_path, capsys):
    from breadsched.cli.main import main

    book = tmp_path / "dc.breadsched"
    main(["init", str(book)])
    assert main(["account", str(book), "add", "--name", "DCFSA", "--type", "FSA",
                 "--parent", "Assets", "--dependent-care", "yes"]) == 0  # fmt: skip
    assert main(["account", str(book), "list", "--json"]) == 0
    capsys.readouterr()
    assert main(["account", str(book), "add", "--name", "Cash2", "--type", "BANK",
                 "--parent", "Assets", "--dependent-care", "yes"]) == 2  # fmt: skip
    assert "applies only to an FSA account" in capsys.readouterr().err
    from breadsched.gen.db.sqlite import DbSQLite

    db = DbSQLite()
    db.load(str(book), mode="r")
    try:
        assert db.get_account_by_name("Assets:DCFSA").fsa_dependent_care
    finally:
        db.close()
