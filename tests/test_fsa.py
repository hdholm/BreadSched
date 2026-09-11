"""FSA benefit-year availability is separate from custodial cash balance."""

from datetime import date

from breadsched.gen.engine import fsa
from breadsched.gen.lib import (
    Account,
    AccountPlanningRole,
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
    account.planning_role = AccountPlanningRole.FSA
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
                date(2026, 7, 15), "First payroll funding",
                account.handle, book.salary, "125.00",
            ),
            txn,
        )
        db.add_transaction(
            Transaction.simple(
                date(2026, 8, 1), "Medical claim",
                book.groceries, account.handle, "400.00",
            ),
            txn,
        )

    status = fsa.year_status(
        db, account, account.fsa_years[0], as_of=date(2026, 8, 2)
    )
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

    status = fsa.year_status(
        db, account, account.fsa_years[0], as_of=date(2027, 8, 20)
    )
    assert status.phase == "run-out"
    assert status.used == Money("250.00")
    assert status.remaining == Money("2750.00")


def test_closed_year_reports_forfeited_remaining_funds(db, book):
    account = _fsa_account(db, book)
    status = fsa.year_status(
        db, account, account.fsa_years[0], as_of=date(2027, 10, 1)
    )
    assert status.phase == "closed"
    assert status.remaining == Money(0)
    assert status.forfeited == Money("3000.00")


def test_fsa_years_round_trip_with_account_serialization():
    account = Account(name="FSA", atype=AccountType.ASSET)
    account.planning_role = AccountPlanningRole.FSA
    account.fsa_years = [
        FsaFundingYear(
            date(2026, 1, 1), date(2026, 12, 31), Money("3200"), date(2027, 3, 31)
        )
    ]
    restored = Account.from_dict(account.serialize())
    assert restored.fsa_years == account.fsa_years
