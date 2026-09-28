"""OFX investment statements: trades, reinvestment, income, and cash."""

from __future__ import annotations

from datetime import date

from breadsched.gen.engine import ledger, valuation
from breadsched.gen.lib import Money
from breadsched.plugins.importer import ofx

STATEMENT = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX>
<SIGNONMSGSRSV1><SONRS><FI><ORG>Sample Brokerage</FI></SONRS></SIGNONMSGSRSV1>
<INVSTMTMSGSRSV1><INVSTMTTRNRS><INVSTMTRS>
<DTASOF>20260331
<CURDEF>USD
<INVACCTFROM><BROKERID>broker.example<ACCTID>98765432</INVACCTFROM>
<INVTRANLIST><DTSTART>20260101<DTEND>20260331
<INVBANKTRAN><STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260102<TRNAMT>5000.00
<FITID>dep-1<NAME>Deposit</STMTTRN><SUBACCTFUND>CASH</INVBANKTRAN>
<BUYMF><INVBUY><INVTRAN><FITID>buy-1<DTTRADE>20260105<MEMO>Buy fund</INVTRAN>
<SECID><UNIQUEID>922908728<UNIQUEIDTYPE>CUSIP</SECID>
<UNITS>20<UNITPRICE>100.00<COMMISSION>4.95<TOTAL>-2004.95
<SUBACCTSEC>CASH<SUBACCTFUND>CASH</INVBUY><BUYTYPE>BUY</BUYMF>
<REINVEST><INVTRAN><FITID>div-1<DTTRADE>20260131<MEMO>Dividend reinvested</INVTRAN>
<SECID><UNIQUEID>922908728<UNIQUEIDTYPE>CUSIP</SECID>
<INCOMETYPE>DIV<TOTAL>-30.00<SUBACCTSEC>CASH<UNITS>0.2913<UNITPRICE>103.00</REINVEST>
<SELLSTOCK><INVSELL><INVTRAN><FITID>sell-1<DTTRADE>20260210</INVTRAN>
<SECID><UNIQUEID>ACME<UNIQUEIDTYPE>TICKER</SECID>
<UNITS>-5<UNITPRICE>50.00<COMMISSION>1.00<TOTAL>249.00
<SUBACCTSEC>CASH<SUBACCTFUND>CASH</INVSELL><SELLTYPE>SELL</SELLSTOCK>
<INCOME><INVTRAN><FITID>int-1<DTTRADE>20260228<MEMO>Cash interest</INVTRAN>
<SECID><UNIQUEID>922908728<UNIQUEIDTYPE>CUSIP</SECID>
<INCOMETYPE>INTEREST<TOTAL>2.50<SUBACCTSEC>CASH<SUBACCTFUND>CASH</INCOME>
<TRANSFER><INVTRAN><FITID>xfer-1<DTTRADE>20260301</INVTRAN>
<SECID><UNIQUEID>ACME<UNIQUEIDTYPE>TICKER</SECID><SUBACCTSEC>CASH<UNITS>10
<TFERACTION>IN<POSTYPE>LONG</TRANSFER>
</INVTRANLIST>
</INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1>
<SECLISTMSGSRSV1><SECLIST>
<MFINFO><SECINFO><SECID><UNIQUEID>922908728<UNIQUEIDTYPE>CUSIP</SECID>
<SECNAME>Total Stock Market Fund<TICKER>VTSAX<UNITPRICE>104.00<DTASOF>20260331
</SECINFO></MFINFO>
<STOCKINFO><SECINFO><SECID><UNIQUEID>ACME<UNIQUEIDTYPE>TICKER</SECID>
<SECNAME>Acme Corp<TICKER>ACME</SECINFO></STOCKINFO>
</SECLIST></SECLISTMSGSRSV1>
</OFX>
"""


def _account(db, name):
    account = db.get_account_by_name(name)
    assert account is not None, name
    return account


def test_ofx_investment_statement_imports_balanced_trades_and_income(db, book, tmp_path):
    path = tmp_path / "brokerage.ofx"
    path.write_text(STATEMENT, encoding="utf-8")

    result = ofx.import_book(db, path)

    assert result.transactions == 5
    assert result.reasons() == {"OFX TRANSFER investment transactions are not imported yet": 1}
    cash = _account(db, "Assets:Sample Brokerage Investment 5432:Cash")
    fund = _account(db, "Assets:Sample Brokerage Investment 5432:VTSAX")
    stock = _account(db, "Assets:Sample Brokerage Investment 5432:ACME")
    # 5000 deposit - 2004.95 buy + 249 sale + 2.50 interest.
    assert ledger.balance(db, cash.handle) == Money("3246.55")
    assert valuation.quantity_balance(db, fund) == Money("20.2913")
    assert valuation.quantity_balance(db, stock) == Money(-5)
    fees = _account(db, "Expenses:Investment Fees")
    assert ledger.balance(db, fees.handle) == Money("5.95")
    income = _account(db, "Income:Investment Income")
    assert ledger.balance(db, income.handle) == Money("32.50")
    for transaction in db.iter_transactions():
        assert not transaction.imbalance()
    buy = next(item for item in db.iter_transactions() if item.description == "Buy fund")
    assert buy.post_date == date(2026, 1, 5)
    security_split = next(split for split in buy.splits if split.account == fund.handle)
    assert (security_split.value, security_split.quantity) == (Money(2000), Money(20))
    # The statement's security list also prices the new fund.
    assert valuation.account_value(db, fund).price == Money(104)


def test_ofx_investment_reimport_refreshes_without_duplicates(db, book, tmp_path):
    path = tmp_path / "brokerage.ofx"
    path.write_text(STATEMENT, encoding="utf-8")
    ofx.import_book(db, path)
    before = sorted(item.handle for item in db.iter_transactions())

    again = ofx.import_book(db, path)

    assert sorted(item.handle for item in db.iter_transactions()) == before
    assert again.transactions_new == 0
    assert len([item for item in db.iter_commodities() if item.mnemonic == "VTSAX"]) == 1


def test_ofx_investment_reimport_keeps_a_recategorized_income(db, book, tmp_path):
    from breadsched.gen.lib import Account, AccountType

    path = tmp_path / "brokerage.ofx"
    path.write_text(STATEMENT, encoding="utf-8")
    ofx.import_book(db, path)
    interest = Account(name="Interest", atype=AccountType.INCOME, parent=book.income)
    income = _account(db, "Income:Investment Income")
    transaction = next(
        item for item in db.iter_transactions() if item.description == "Cash interest"
    )
    for split in transaction.splits:
        if split.account == income.handle:
            split.account = interest.handle
    with db.transaction("Recategorize") as txn:
        db.add_account(interest, txn)
        db.commit_transaction(transaction, txn)

    ofx.import_book(db, path)

    stored = db.get_transaction(transaction.handle)
    assert interest.handle in {split.account for split in stored.splits}
    assert income.handle not in {split.account for split in stored.splits}
