"""Security price quotes from QIF and OFX go through the shared quote contract.

A quote must name a security already in the book (by symbol), be in a known
currency, and be positive. It is stored with its own source, so a quote entered in
BreadSched is never replaced, and re-importing the same file updates rather than
duplicates it.
"""

from __future__ import annotations

from datetime import date

from breadsched.gen.engine.valuation import save_security_price
from breadsched.gen.lib import Money
from breadsched.gen.lib.commodity import Commodity
from breadsched.plugins.importer import ofx, qif


def _security(db, symbol="VTSAX"):
    fund = Commodity(namespace="FUND", mnemonic=symbol, fullname=symbol, fraction=10000)
    with db.transaction("Security") as txn:
        db.add_commodity(fund, txn)
    return fund


def _quotes(db, security):
    return sorted(
        (item.quote_date, item.value, item.source) for item in db.iter_prices(commodity=security)
    )


def test_qif_prices_for_known_securities_are_imported_once(db, tmp_path):
    fund = _security(db)
    path = tmp_path / "prices.qif"
    path.write_text(
        '!Type:Prices\n"VTSAX",104.25,"1/15/26"\n^\n"NOPE",5.00,"1/15/26"\n^\n', encoding="utf-8"
    )

    result = qif.import_book(db, path)

    assert _quotes(db, fund.handle) == [(date(2026, 1, 15), Money("104.25"), "qif")]
    assert result.prices == 1
    assert result.reasons() == {"QIF price for a security not in the book": 1}
    qif.import_book(db, path)
    assert _quotes(db, fund.handle) == [(date(2026, 1, 15), Money("104.25"), "qif")]


def test_an_imported_quote_never_replaces_one_entered_in_breadsched(db, tmp_path):
    fund = _security(db)
    save_security_price(
        db,
        security_handle=fund.handle,
        currency_handle=None,
        quote_date=date(2026, 1, 15),
        value=Money("100"),
    )
    path = tmp_path / "prices.qif"
    path.write_text('!Type:Prices\n"VTSAX",104.25,"1/15/26"\n^\n', encoding="utf-8")
    qif.import_book(db, path)
    sources = {source: value for _when, value, source in _quotes(db, fund.handle)}
    assert sources["qif"] == Money("104.25")
    assert Money("100") in sources.values()


OFX_INVESTMENT = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX>
<INVSTMTMSGSRSV1><INVSTMTTRNRS><INVSTMTRS>
<CURDEF>USD
<INVPOSLIST><POSMF><INVPOS>
<SECID><UNIQUEID>922908728<UNIQUEIDTYPE>CUSIP</SECID>
<HELDINACCT>CASH<POSTYPE>LONG<UNITS>10<UNITPRICE>105.50<MKTVAL>1055.00
<DTPRICEASOF>20260120
</INVPOS></POSMF></INVPOSLIST>
</INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1>
<SECLISTMSGSRSV1><SECLIST><MFINFO><SECINFO>
<SECID><UNIQUEID>922908728<UNIQUEIDTYPE>CUSIP</SECID>
<SECNAME>Total Stock Market<TICKER>VTSAX<UNITPRICE>104.00<DTASOF>20260115
</SECINFO></MFINFO></SECLIST></SECLISTMSGSRSV1>
</OFX>
"""


def test_ofx_investment_statements_bring_security_prices(db, tmp_path):
    fund = _security(db)
    path = tmp_path / "positions.ofx"
    path.write_text(OFX_INVESTMENT, encoding="utf-8")

    result = ofx.import_book(db, path)

    assert _quotes(db, fund.handle) == [
        (date(2026, 1, 15), Money("104.00"), "ofx"),
        (date(2026, 1, 20), Money("105.50"), "ofx"),
    ]
    assert result.prices == 2
    ofx.import_book(db, path)
    assert len(_quotes(db, fund.handle)) == 2


def test_ofx_prices_for_unknown_securities_are_skipped(db, tmp_path):
    path = tmp_path / "positions.ofx"
    path.write_text(OFX_INVESTMENT, encoding="utf-8")
    result = ofx.import_book(db, path)
    assert result.prices == 0
    assert result.reasons() == {"OFX price for a security not in the book": 2}
