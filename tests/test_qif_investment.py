"""QIF investment accounts, security lists, and investment actions."""

from __future__ import annotations

from breadsched.gen.engine import ledger, valuation
from breadsched.gen.lib import Money
from breadsched.plugins.importer import qif

QIF = """!Type:Security
NTotal Stock Market Fund
SVTSAX
TMutual Fund
^
NAcme Corp
SACME
TStock
^
!Account
NChecking
TBank
^
!Type:Bank
D1/2/26
T-5,000.00
PFund brokerage
L[Brokerage]
^
!Account
NBrokerage
TInvst
^
!Type:Invst
D1/2/26
NXIn
T5,000.00
L[Checking]
^
D1/5/26
NBuy
YTotal Stock Market Fund
I100.00
Q20
O4.95
T2,004.95
^
D1/31/26
NReinvDiv
YTotal Stock Market Fund
I103.00
Q0.2913
T30.00
^
D2/10/26
NSell
YAcme Corp
I50.00
Q5
O1.00
T249.00
^
D2/28/26
NIntInc
T2.50
^
D3/1/26
NMiscExp
T12.00
LBank Fees
^
D3/2/26
NShrsIn
YAcme Corp
Q10
^
D3/3/26
NBuyX
YAcme Corp
I40.00
Q1
T40.00
L[Checking]
$40.00
^
!Type:Prices
"VTSAX",104.00,"3/31/26"
^
"""


def _account(db, name):
    account = db.get_account_by_name(name)
    assert account is not None, name
    return account


def test_qif_investment_account_imports_trades_income_and_transfers(db, book, tmp_path):
    path = tmp_path / "quicken.qif"
    path.write_text(QIF, encoding="utf-8")

    result = qif.import_book(db, path)

    assert result.reasons() == {"QIF ShrsIn investment actions are not imported yet": 1}
    cash = _account(db, "Assets:Brokerage:Cash")
    fund = _account(db, "Assets:Brokerage:VTSAX")
    stock = _account(db, "Assets:Brokerage:ACME")
    # 5000 in - 2004.95 buy + 249 sale + 2.50 interest - 12 fees (and the
    # checking register's own copy of the 5000 transfer; see #176).
    assert ledger.balance(db, cash.handle) == Money("8234.55")
    assert valuation.quantity_balance(db, fund) == Money("20.2913")
    assert valuation.quantity_balance(db, stock) == Money(-4)
    assert ledger.balance(db, _account(db, "Expenses:Investment Fees").handle) == Money("5.95")
    assert ledger.balance(db, _account(db, "Expenses:Bank Fees").handle) == Money(12)
    assert ledger.balance(db, _account(db, "Income:Investment Income").handle) == Money("32.50")
    assert db.get_account_by_name("Assets:Brokerage Cash") is None
    for transaction in db.iter_transactions():
        assert not transaction.imbalance()
    assert valuation.account_value(db, fund).price == Money(104)

    again = qif.import_book(db, path)
    assert again.transactions_new == 0
