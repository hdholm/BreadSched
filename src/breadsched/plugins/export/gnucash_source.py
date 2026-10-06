"""Read a GnuCash SQLite or XML book into one neutral view for write-back.

``SourceBook`` holds the transactions, splits, and accounts write-back compares
with BreadSched; ``fingerprint`` and ``record_fingerprint`` identify the exact book
last imported, and ``preflight`` refuses a locked, different, or changed book.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money
from ..importer.gnucash_common import (
    money_from_fraction,
    money_from_pair,
    parse_gnc_date,
    parse_gnc_sql_posting_date,
)

__all__ = [
    "FINGERPRINT_KEY",
    "NEUTRAL_TIME",
    "SourceAccount",
    "SourceBook",
    "SourceFingerprint",
    "SourceSplit",
    "SourceTxn",
    "WritebackError",
    "book_format",
    "book_spans",
    "file_digest",
    "fingerprint",
    "load_xml",
    "parse_block",
    "preflight",
    "qualified",
    "read_book",
    "record_fingerprint",
    "shown_value",
    "table_names",
    "xml_namespaces",
    "xml_text",
]


#: Book metadata: the GnuCash book last imported and its exact bytes.
FINGERPRINT_KEY = "gnucash.writeback.source"
#: GnuCash stores date-only postings at 10:59:00 UTC, its neutral time.
NEUTRAL_TIME = "10:59:00"


class WritebackError(Exception):
    """A refusal or failure with a stable ``code`` for the service layer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    path: str
    sha256: str
    book_guid: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256, "book_guid": self.book_guid}


@dataclass(slots=True)
class SourceSplit:
    guid: str
    account: str
    value: Money
    quantity: Money
    memo: str = ""
    action: str = ""
    state: str = "n"
    lot: str = ""


@dataclass(slots=True)
class SourceTxn:
    guid: str
    currency: str
    num: str
    post_date: date | None
    description: str
    splits: dict[str, SourceSplit]

    @property
    def reconciled(self) -> bool:
        return any(split.state == "y" for split in self.splits.values())

    @property
    def in_lot(self) -> bool:
        return any(split.lot for split in self.splits.values())


@dataclass(frozen=True, slots=True)
class SourceAccount:
    #: ``SPACE:MNEMONIC`` of the account's commodity, e.g. ``CURRENCY:USD``.
    commodity: str
    scu: int


@dataclass(slots=True)
class SourceBook:
    """What a GnuCash book holds, read the same way from SQLite and XML."""

    format: str
    path: Path
    book_guid: str
    root_guid: str
    accounts: dict[str, SourceAccount]
    #: Currency mnemonic -> (SQLite commodity GUID or "", smallest fraction).
    currencies: dict[str, tuple[str, int]]
    transactions: dict[str, SourceTxn]
    #: SQLite only: ``compact`` (YYYYMMDDhhmmss) or ``iso`` timestamps.
    date_style: str = "iso"


def book_format(path: str | Path) -> str:
    """``sqlite`` or ``xml``, from the file's first bytes."""
    with open(path, "rb") as handle:
        head = handle.read(16)
    return "sqlite" if head.startswith(b"SQLite format 3") else "xml"


def read_book(path: str | Path) -> SourceBook:
    resolved = Path(path)
    if book_format(resolved) == "sqlite":
        return _read_sqlite(resolved)
    return _read_xml(resolved)


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _read_sqlite(path: Path) -> SourceBook:
    conn = _connect_ro(path)
    try:
        tables = table_names(conn)
        commodities = {
            row["guid"]: (
                f"{row['namespace']}:{row['mnemonic']}",
                row["mnemonic"],
                int(row["fraction"] or 100),
                row["namespace"],
            )
            for row in conn.execute("SELECT guid, namespace, mnemonic, fraction FROM commodities")
        }
        currencies = {
            mnemonic: (guid, fraction)
            for guid, (_key, mnemonic, fraction, space) in commodities.items()
            if space in ("CURRENCY", "ISO4217")
        }
        accounts: dict[str, SourceAccount] = {}
        root_guid = ""
        for row in conn.execute("SELECT * FROM accounts"):
            commodity = commodities.get(row["commodity_guid"])
            accounts[row["guid"]] = SourceAccount(
                commodity[0] if commodity else "",
                int(row["commodity_scu"] or (commodity[2] if commodity else 100)),
            )
            if row["account_type"] == "ROOT" and not row["parent_guid"] and not root_guid:
                root_guid = row["guid"]
        book_guid = ""
        if "books" in tables:
            book = conn.execute("SELECT guid, root_account_guid FROM books LIMIT 1").fetchone()
            if book is not None:
                book_guid = str(book["guid"])
                root_guid = str(book["root_account_guid"] or root_guid)
        transactions: dict[str, SourceTxn] = {}
        sample = ""
        for row in conn.execute("SELECT * FROM transactions"):
            currency = commodities.get(row["currency_guid"])
            transactions[row["guid"]] = SourceTxn(
                row["guid"],
                currency[1] if currency else "",
                row["num"] or "",
                _safe_date(row["post_date"], sql=True),
                row["description"] or "",
                {},
            )
            sample = sample or str(row["post_date"] or "")
        for row in conn.execute("SELECT * FROM splits"):
            owner = transactions.get(row["tx_guid"])
            if owner is None:
                continue  # a template split behind a scheduled transaction
            owner.splits[row["guid"]] = SourceSplit(
                row["guid"],
                row["account_guid"],
                money_from_pair(row["value_num"], row["value_denom"]),
                money_from_pair(row["quantity_num"], row["quantity_denom"]),
                row["memo"] or "",
                row["action"] or "",
                row["reconcile_state"] or "n",
                row["lot_guid"] or "",
            )
    finally:
        conn.close()
    style = "compact" if len(sample.strip()) == 14 and sample.strip().isdigit() else "iso"
    return SourceBook(
        "sqlite", path, book_guid, root_guid, accounts, currencies, transactions, style
    )


_TXN_BLOCK = re.compile(r'<gnc:transaction version="2\.0\.0">.*?</gnc:transaction>(?:\r?\n)?', re.S)


_TEMPLATE_BLOCK = re.compile(r"<gnc:template-transactions>.*?</gnc:template-transactions>", re.S)


_XMLNS = re.compile(r'xmlns:([\w.-]+)="([^"]+)"')


def load_xml(path: Path) -> tuple[str, bool]:
    raw = path.read_bytes()
    gzipped = raw[:2] == b"\x1f\x8b"
    if gzipped:
        raw = gzip.decompress(raw)
    return raw.decode("utf-8"), gzipped


def xml_namespaces(text: str) -> dict[str, str]:
    head = text[: text.find(">", text.find("<gnc-v2")) + 1]
    return {prefix: uri for prefix, uri in _XMLNS.findall(head)}


def book_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every real (non-template) transaction block."""
    templates = [(m.start(), m.end()) for m in _TEMPLATE_BLOCK.finditer(text)]
    return [
        (m.start(), m.end())
        for m in _TXN_BLOCK.finditer(text)
        if not any(start <= m.start() < end for start, end in templates)
    ]


def parse_block(block: str, namespaces: dict[str, str]) -> ET.Element:
    declarations = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in namespaces.items())
    return ET.fromstring(f"<wrap {declarations}>{block}</wrap>")[0]


def qualified(namespaces: dict[str, str], name: str) -> str:
    prefix, local = name.split(":", 1)
    return f"{{{namespaces[prefix]}}}{local}"


def _find(element: ET.Element, path: str, ns: dict[str, str]) -> ET.Element | None:
    return element.find(path, ns)


def xml_text(element: ET.Element, path: str, ns: dict[str, str], default: str = "") -> str:
    found = element.find(path, ns)
    return (found.text or default) if found is not None else default


def _read_xml(path: Path) -> SourceBook:
    text, _gzipped = load_xml(path)
    ns = xml_namespaces(text)
    body = _TEMPLATE_BLOCK.sub("", text)
    root = ET.fromstring(body)
    book = root.find("gnc:book", ns)
    scope = book if book is not None else root
    book_guid = xml_text(scope, "book:id", ns) if "book" in ns else ""
    accounts: dict[str, SourceAccount] = {}
    root_guid = ""
    for element in scope.findall("gnc:account", ns):
        guid = xml_text(element, "act:id", ns)
        space = xml_text(element, "act:commodity/cmdty:space", ns)
        mnemonic = xml_text(element, "act:commodity/cmdty:id", ns)
        scu = xml_text(element, "act:commodity-scu", ns)
        accounts[guid] = SourceAccount(f"{space}:{mnemonic}" if mnemonic else "", int(scu or 100))
        if xml_text(element, "act:type", ns) == "ROOT" and not root_guid:
            root_guid = guid
    currencies: dict[str, tuple[str, int]] = {}
    for element in scope.findall("gnc:commodity", ns):
        space = xml_text(element, "cmdty:space", ns)
        mnemonic = xml_text(element, "cmdty:id", ns)
        if space in ("CURRENCY", "ISO4217"):
            fraction = xml_text(element, "cmdty:fraction", ns)
            currencies[mnemonic] = ("", int(fraction) if fraction else 0)
    transactions: dict[str, SourceTxn] = {}
    for element in scope.findall("gnc:transaction", ns):
        parsed = _xml_transaction(element, ns)
        transactions[parsed.guid] = parsed
    # A currency need not be listed as a commodity; any an account or transaction
    # uses is in the book. Its fraction then comes from BreadSched's commodity.
    for account in accounts.values():
        space, _sep, mnemonic = account.commodity.partition(":")
        if space in ("CURRENCY", "ISO4217") and mnemonic not in currencies:
            currencies[mnemonic] = ("", 0)
    for parsed in transactions.values():
        if parsed.currency and parsed.currency not in currencies:
            currencies[parsed.currency] = ("", 0)
    return SourceBook("xml", path, book_guid, root_guid, accounts, currencies, transactions)


def _xml_transaction(element: ET.Element, ns: dict[str, str]) -> SourceTxn:
    splits: dict[str, SourceSplit] = {}
    for split in element.findall("trn:splits/trn:split", ns):
        value = xml_text(split, "split:value", ns, "0/1")
        guid = xml_text(split, "split:id", ns)
        splits[guid] = SourceSplit(
            guid,
            xml_text(split, "split:account", ns),
            money_from_fraction(value),
            money_from_fraction(xml_text(split, "split:quantity", ns, value)),
            xml_text(split, "split:memo", ns),
            xml_text(split, "split:action", ns),
            xml_text(split, "split:reconciled-state", ns, "n"),
            xml_text(split, "split:lot", ns),
        )
    return SourceTxn(
        xml_text(element, "trn:id", ns),
        xml_text(element, "trn:currency/cmdty:id", ns),
        xml_text(element, "trn:num", ns),
        _safe_date(xml_text(element, "trn:date-posted/ts:date", ns)),
        xml_text(element, "trn:description", ns),
        splits,
    )


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _book_guid(path: Path) -> str:
    if book_format(path) == "sqlite":
        conn = _connect_ro(path)
        try:
            if "books" not in table_names(conn):
                return ""
            row = conn.execute("SELECT guid FROM books LIMIT 1").fetchone()
            return str(row[0]) if row else ""
        finally:
            conn.close()
    text, _gzipped = load_xml(path)
    found = re.search(r'<book:id type="guid">([0-9a-f]+)</book:id>', text)
    return found.group(1) if found else ""


def fingerprint(path: str | Path) -> SourceFingerprint:
    """The book's resolved path, content digest, and book GUID."""
    resolved = Path(path).expanduser().resolve()
    return SourceFingerprint(str(resolved), file_digest(resolved), _book_guid(resolved))


def record_fingerprint(db: DbSQLite, path: str | Path, digest: str, txn=None) -> None:
    """Remember the book as imported, before BreadSched changed anything locally."""
    resolved = Path(path).expanduser().resolve()
    db.set_metadata(
        FINGERPRINT_KEY,
        SourceFingerprint(str(resolved), digest, _book_guid(resolved)).as_dict(),
        txn,
    )


def _recorded(db: DbSQLite) -> SourceFingerprint:
    raw = db.get_metadata(FINGERPRINT_KEY)
    if not isinstance(raw, dict) or not all(
        isinstance(raw.get(key), str) for key in ("path", "sha256", "book_guid")
    ):
        raise WritebackError(
            "writeback.source.unknown",
            "import the GnuCash book into BreadSched before writing back to it",
        )
    return SourceFingerprint(raw["path"], raw["sha256"], raw["book_guid"])


def _locked(path: Path) -> bool:
    if book_format(path) == "xml":
        return Path(f"{path}.LCK").exists()
    conn = _connect_ro(path)
    try:
        return "gnclock" in table_names(conn) and bool(
            conn.execute("SELECT COUNT(*) FROM gnclock").fetchone()[0]
        )
    finally:
        conn.close()


def preflight(db: DbSQLite) -> tuple[SourceFingerprint, Path]:
    recorded = _recorded(db)
    path = Path(recorded.path)
    if not path.is_file():
        raise WritebackError("writeback.source.missing", f"the GnuCash book {path} is not there")
    current = fingerprint(path)
    if current.book_guid != recorded.book_guid:
        raise WritebackError(
            "writeback.source.other_book", "the file at that path is a different GnuCash book"
        )
    if current.sha256 != recorded.sha256:
        raise WritebackError(
            "writeback.source.changed",
            "the GnuCash book changed since it was last imported; import it first",
        )
    if _locked(path):
        raise WritebackError(
            "writeback.source.locked", "GnuCash has the book open; close it in GnuCash first"
        )
    return recorded, path


def _safe_date(raw: object, *, sql: bool = False) -> date | None:
    """The date GnuCash stored, read the way the importer read it; ``None`` if unreadable."""
    try:
        if not raw:
            return None
        return parse_gnc_sql_posting_date(str(raw)) if sql else parse_gnc_date(str(raw))
    except ValueError:
        return None


def shown_value(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if value is None:
        return "(blank)"
    text = str(value)
    return repr(text) if text else "(blank)"
