"""Fuzz-style malformed statement imports.

A statement file from a bank, an older application, or a damaged download can be
truncated, reordered, or mangled in ways no fixture names. Whatever arrives, an
import must either finish with what it could read (reporting the rest as skipped
records or warnings) or refuse with a ValueError, and either way leave a book that
verifies: no unbalanced or half-written transaction, and nothing written at all
when the import refuses.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from breadsched.gen.db.sqlite import DbSQLite
from breadsched.plugins.importer import ofx, qif

hypothesis = pytest.importorskip("hypothesis")
st = pytest.importorskip("hypothesis.strategies")
given = hypothesis.given
settings = hypothesis.settings
HealthCheck = hypothesis.HealthCheck

_NOISE = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), max_codepoint=0x2FF),
    max_size=12,
)
_AMOUNT = st.one_of(
    st.decimals(min_value=-(10**7), max_value=10**7, places=2, allow_nan=False).map(str),
    st.sampled_from(["", "-", "1,234.56", "1.234,56", "12,5", "--3", "1e5", "NaN", "∞"]),
    _NOISE,
)
_DATE = st.one_of(
    st.sampled_from(
        ["01/15/2026", "15/01/2026", "1/2'26", "2026-01-15", "02/30/2026", "13/13/13", ""]
    ),
    _NOISE,
)


def _qif_line(draw):
    code = draw(st.sampled_from(list("DTUPMLNCSE$A^!") + ["!Type:Bank", "!Account"]))
    if code == "D":
        return "D" + draw(_DATE)
    if code in "TU$":
        return code + draw(_AMOUNT)
    if code.startswith("!"):
        return draw(
            st.sampled_from(
                [
                    "!Type:Bank",
                    "!Type:CCard",
                    "!Type:Invst",
                    "!Type:Security",
                    "!Type:Prices",
                    "!Account",
                    "!Option:AutoSwitch",
                    "!Type:" + draw(_NOISE),
                ]
            )
        )
    if code == "^":
        return "^"
    return code + draw(_NOISE)


@st.composite
def qif_files(draw):
    lines = [_qif_line(draw) for _ in range(draw(st.integers(0, 40)))]
    return "\n".join(lines)


@st.composite
def ofx_files(draw):
    rows = []
    for _ in range(draw(st.integers(0, 8))):
        tags = {
            "TRNTYPE": draw(st.sampled_from(["DEBIT", "CREDIT", "", "XFER"])),
            "DTPOSTED": draw(st.sampled_from(["20260115120000", "2026", "20261340", ""])),
            "TRNAMT": draw(_AMOUNT),
            "FITID": draw(st.sampled_from(["a1", "a1", "", "z"])),
            "NAME": draw(_NOISE),
        }
        kept = draw(st.lists(st.sampled_from(list(tags)), unique=True))
        rows.append("<STMTTRN>" + "".join(f"<{k}>{tags[k]}" for k in kept) + "</STMTTRN>")
    head = draw(
        st.sampled_from(
            [
                "OFXHEADER:100\nDATA:OFXSGML\n\n<OFX>",
                "<?xml version='1.0'?><OFX>",
                "<OFX>",
                "",
            ]
        )
    )
    account = draw(
        st.sampled_from(
            [
                "<BANKACCTFROM><BANKID>1<ACCTID>00001234<ACCTTYPE>CHECKING</BANKACCTFROM>",
                "<CCACCTFROM><ACCTID>4111</CCACCTFROM>",
                "",
            ]
        )
    )
    currency = draw(st.sampled_from(["<CURDEF>USD", "<CURDEF>EUR", "<CURDEF>", ""]))
    body = (
        f"{head}<BANKMSGSRSV1><STMTTRNRS><STMTRS>{currency}{account}<BANKTRANLIST>"
        + "".join(rows)
        + "</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1>"
    )
    cut = draw(st.integers(0, len(body)))
    return body if draw(st.booleans()) else body[:cut]


def _import(importer, text: str, suffix: str):
    db = DbSQLite.create_memory()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / f"statement{suffix}"
        path.write_text(text, encoding="utf-8")
        before = len(list(db.iter_transactions()))
        try:
            importer.import_book(db, path)
        except ValueError:
            # A refused import writes nothing.
            assert len(list(db.iter_transactions())) == before
        assert db.verify_book() == []
        # Re-importing the same file never adds transactions.
        count = len(list(db.iter_transactions()))
        try:
            importer.import_book(db, path)
        except ValueError:
            pass
        assert len(list(db.iter_transactions())) == count
        assert db.verify_book() == []
    db.close()


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(qif_files())
def test_malformed_qif_never_corrupts_the_book(text):
    _import(qif, text, ".qif")


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ofx_files())
def test_malformed_ofx_never_corrupts_the_book(text):
    _import(ofx, text, ".ofx")


@st.composite
def csv_files(draw):
    delimiter = draw(st.sampled_from([",", ";", "\t"]))
    header = delimiter.join(["Date", "Description", "Amount"])
    rows = []
    for _ in range(draw(st.integers(0, 12))):
        cells = [draw(_DATE), draw(_NOISE), draw(_AMOUNT)]
        cells = cells[: draw(st.integers(0, 3))] + draw(st.lists(_NOISE, max_size=2))
        quoted = [f'"{cell}"' if draw(st.booleans()) else cell for cell in cells]
        rows.append(delimiter.join(quoted))
    return "\n".join([header, *rows]) + draw(st.sampled_from(["", "\n", '\n"unterminated']))


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(csv_files())
def test_malformed_csv_is_refused_or_imported_whole(text):
    from breadsched.gen.lib import Account, AccountType
    from breadsched.gen.services.csv_import import CsvImportRequest, CsvMapping, import_csv

    db = DbSQLite.create_memory()
    with db.transaction("Checking") as txn:
        checking = Account(name="Checking", atype=AccountType.BANK)
        db.add_account(checking, txn)
    mapping = CsvMapping(date="Date", description="Description", amount="Amount")
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "statement.csv"
        path.write_text(text, encoding="utf-8")
        request = CsvImportRequest(str(path), checking.handle, mapping)
        first = import_csv(db, request)
        if not first.ok:
            # A refused import writes nothing.
            assert list(db.iter_transactions()) == []
        assert db.verify_book() == []
        count = len(list(db.iter_transactions()))
        import_csv(db, request)
        assert len(list(db.iter_transactions())) == count
        assert db.verify_book() == []
    db.close()
