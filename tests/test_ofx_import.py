"""Open Financial Exchange importer tests."""

from __future__ import annotations

from breadsched.gen.engine import ledger
from breadsched.gen.lib import Money, PlanningResolution
from breadsched.gen.plug import IMPORTER, PluginManager
from breadsched.plugins.importer import ofx

_SAMPLE = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252

<OFX>
<SIGNONMSGSRSV1><SONRS><FI><ORG>Sample Bank</FI></SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>USD
<BANKACCTFROM><BANKID>123456789<ACCTID>00001234<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260115120000<TRNAMT>-45.67<FITID>txn-1
<NAME>Grocery Store</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260125120000<TRNAMT>2000.00<FITID>txn-2<NAME>Employer</STMTTRN>
</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


def test_ofx_plugin_is_detected_by_content(tmp_path):
    path = tmp_path / "statement.dat"
    path.write_text(_SAMPLE)

    plugin = PluginManager.instance().for_file(str(path), IMPORTER)

    assert plugin is not None
    assert plugin.id == "ofx"


def test_ofx_notify_false_suppresses_the_complete_importer_boundary(db, tmp_path):
    path = tmp_path / "statement.ofx"
    path.write_text(_SAMPLE)
    seen = []
    db.connect("database-changed", lambda *_: seen.append("changed"))

    ofx.import_book(db, path, notify=False)

    assert seen == []


def test_ofx_imports_bank_statement_as_historical_activity(db, book, tmp_path):
    path = tmp_path / "statement.ofx"
    path.write_text(_SAMPLE)

    result = ofx.import_book(db, path)

    assert result.transactions == 2
    transactions = sorted(db.iter_transactions(), key=lambda item: item.post_date)
    assert [item.description for item in transactions] == ["Grocery Store", "Employer"]
    assert all(item.planning_resolution is PlanningResolution.HISTORICAL for item in transactions)
    source = db.get_account_by_name("Sample Bank Checking 1234")
    assert source is not None
    assert ledger.balance(db, source.handle, natural_sign=False) == Money("1954.33")
    expense = db.get_account_by_name("Expenses:Uncategorized OFX")
    assert expense is not None


def test_ofx_fitid_makes_reimport_idempotent(db, book, tmp_path):
    path = tmp_path / "statement.qfx"
    path.write_text(_SAMPLE)

    first = ofx.import_book(db, path)
    second = ofx.import_book(db, path)

    assert first.transactions == 2
    assert second.transactions == 2
    assert len(list(db.iter_transactions())) == 2


def test_ofx_without_fitid_uses_content_stable_fallback(db, book, tmp_path):
    path = tmp_path / "statement.ofx"
    without_fitid = _SAMPLE.replace("<FITID>txn-1", "").replace("<FITID>txn-2", "")
    path.write_text(without_fitid)
    ofx.import_book(db, path)

    extra = "<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260110120000<TRNAMT>-10.00<NAME>Coffee</STMTTRN>"
    path.write_text(without_fitid.replace("<BANKTRANLIST>", f"<BANKTRANLIST>{extra}"))
    ofx.import_book(db, path)

    assert len(list(db.iter_transactions())) == 3


def test_ofx_detects_comma_decimal_amounts(db, book, tmp_path):
    path = tmp_path / "comma-decimal.ofx"
    text = _SAMPLE.replace("-45.67", "-45,67").replace("2000.00", "2.000,00")
    path.write_text(text)

    result = ofx.import_book(db, path)

    assert result.transactions == 2
    source = db.get_account_by_name("Sample Bank Checking 1234")
    assert source is not None
    assert ledger.balance(db, source.handle, natural_sign=False) == Money("1954.33")


def test_ofx_rejects_conflicting_number_conventions(db, tmp_path):
    path = tmp_path / "mixed.ofx"
    path.write_text(_SAMPLE.replace("2000.00", "2000,00"))

    result = ofx.import_book(db, path)

    assert result.transactions == 0
    assert any("conflicting decimal number formats" in warning for warning in result.warnings)
