"""Escrow planning recognition stays separate from balanced ledger facts."""

from breadsched.gen.engine.escrow import EscrowEffectKind, recognition
from breadsched.gen.lib import Account, AccountType, Money


def _accounts():
    accounts = [
        Account(name="Checking", atype=AccountType.BANK),
        Account(name="Escrow A", atype=AccountType.ESCROW),
        Account(name="Escrow B", atype=AccountType.ESCROW),
        Account(name="Insurance", atype=AccountType.EXPENSE),
        Account(name="Loan", atype=AccountType.LOAN),
        Account(name="Opening balances", atype=AccountType.EQUITY),
    ]
    return accounts, {account.handle: account for account in accounts}


def test_vendor_credit_restored_to_escrow_is_not_new_funding_or_refund():
    (cash, escrow, _other, expense, _loan, _equity), accounts = _accounts()

    result = recognition([(expense.handle, Money("-25")), (escrow.handle, Money("25"))], accounts)

    assert result.restorations == {escrow.handle: Money("25")}
    assert result.restored_expenses == {expense.handle: Money("25")}
    assert result.planning_expense_adjustment == Money("25")
    assert result.planning_flows == {}
    assert cash.handle not in result.movements


def test_refund_from_escrow_to_cash_reverses_prior_planning_expense():
    (cash, escrow, _other, _expense, _loan, _equity), accounts = _accounts()

    result = recognition([(escrow.handle, Money("-30")), (cash.handle, Money("30"))], accounts)

    assert result.refunds == {escrow.handle: Money("30")}
    assert result.planning_expense_adjustment == Money("-30")
    assert result.planning_flows == {escrow.handle: Money("-30")}


def test_manual_adjustments_and_internal_transfers_are_neutral():
    (_cash, escrow, other, _expense, _loan, equity), accounts = _accounts()

    correction = recognition(
        [(escrow.handle, Money("40")), (equity.handle, Money("-40"))], accounts
    )
    transfer = recognition([(escrow.handle, Money("-15")), (other.handle, Money("15"))], accounts)

    assert correction.adjustments == {escrow.handle: Money("40")}
    assert correction.planning_expense_adjustment == Money(0)
    assert correction.planning_flows == {}
    assert {effect.kind for effect in transfer.effects} == {EscrowEffectKind.TRANSFER}
    assert transfer.planning_expense_adjustment == Money(0)


def test_combined_loan_payment_separates_principal_interest_and_escrow():
    (cash, escrow, _other, expense, loan, _equity), accounts = _accounts()

    result = recognition(
        [
            (loan.handle, Money("800")),
            (expense.handle, Money("200")),
            (escrow.handle, Money("300")),
            (cash.handle, Money("-1300")),
        ],
        accounts,
    )

    assert result.funding == {escrow.handle: Money("300")}
    assert result.combined_debt_principal == Money("800")
    assert result.combined_expense == Money("200")
    assert result.planning_expense_adjustment == Money("300")
    assert any(
        "principal only reduces the liability" in line for line in result.explanations(accounts)
    )
