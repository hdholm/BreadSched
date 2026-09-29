"""Write BreadSched edits back to the GnuCash book they came from (#174).

Write-back is always previewed first, names what it changes per transaction, and
works on SQLite and XML books alike. It covers

- **new** transactions whose accounts all exist in GnuCash, in the transaction's
  currency, with any number of splits (a split in a security or foreign-currency
  account carries its own quantity);
- **edits** of an imported transaction: date, description, number, split memos and
  actions, amounts, accounts, and added or removed splits, while no GnuCash split
  of it is reconciled and none belongs to a GnuCash lot;
- **reconcile state** (``n``/``c``/``y`` and its date) set in BreadSched; and
- **deletions** of imported transactions deleted in BreadSched, on the same terms.

Anything else is listed as unsupported with its reason and never written. The book
is refused when GnuCash holds its lock (the ``gnclock`` table, or an XML book's
``.LCK`` file), when it is not the book last imported, or when its bytes changed
since that import, so every difference BreadSched sees is a local edit.

Both formats are read into one neutral view (``SourceBook``) that planning
compares with BreadSched. Applying copies the book to a timestamped backup, then
writes: SQLite in one ``BEGIN IMMEDIATE`` transaction, XML by replacing only the
touched ``gnc:transaction`` blocks (every other byte is kept) and swapping the file
in atomically. The book is then read back and every written transaction must
equal what was meant, and read as BreadSched holds it; any mismatch or error
restores the backup.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money
from ...gen.lib.transaction import ReconcileState, Transaction
from ..importer.gnucash_common import money_from_fraction, money_from_pair, parse_gnc_date

__all__ = [
    "FINGERPRINT_KEY",
    "KEEP_BACKUPS_KEY",
    "DEFAULT_KEEP_BACKUPS",
    "SourceBook",
    "SourceFingerprint",
    "WritebackChange",
    "WritebackError",
    "WritebackPlan",
    "WritebackUnsupported",
    "apply_plan",
    "book_format",
    "file_digest",
    "fingerprint",
    "plan_writeback",
    "read_book",
    "record_fingerprint",
    "source_facts",
    "written_facts",
]

#: Book metadata: the GnuCash book last imported and its exact bytes.
FINGERPRINT_KEY = "gnucash.writeback.source"
#: Book metadata: how many write-back backups to keep (the oldest are removed).
KEEP_BACKUPS_KEY = "gnucash.writeback.keep_backups"
DEFAULT_KEEP_BACKUPS = 10
_INVENTORY_KEY = "import.source_inventory"
_SKIPPED_HISTORY_KEY = "import.skipped_history"
_WRITABLE_STATES = {"n", "c", "y"}
#: GnuCash stores date-only postings at 10:59:00 UTC, its neutral time.
_NEUTRAL_TIME = "10:59:00"


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


# ------------------------------------------------------------ neutral book view


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


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _read_sqlite(path: Path) -> SourceBook:
    conn = _connect_ro(path)
    try:
        tables = _tables(conn)
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
                _safe_date(row["post_date"]),
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


# XML: GnuCash's namespaces, and the book-level transaction blocks.

_TXN_BLOCK = re.compile(r'<gnc:transaction version="2\.0\.0">.*?</gnc:transaction>(?:\r?\n)?', re.S)
_TEMPLATE_BLOCK = re.compile(r"<gnc:template-transactions>.*?</gnc:template-transactions>", re.S)
_XMLNS = re.compile(r'xmlns:([\w.-]+)="([^"]+)"')


def _load_xml(path: Path) -> tuple[str, bool]:
    raw = path.read_bytes()
    gzipped = raw[:2] == b"\x1f\x8b"
    if gzipped:
        raw = gzip.decompress(raw)
    return raw.decode("utf-8"), gzipped


def _namespaces(text: str) -> dict[str, str]:
    head = text[: text.find(">", text.find("<gnc-v2")) + 1]
    return {prefix: uri for prefix, uri in _XMLNS.findall(head)}


def _book_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every real (non-template) transaction block."""
    templates = [(m.start(), m.end()) for m in _TEMPLATE_BLOCK.finditer(text)]
    return [
        (m.start(), m.end())
        for m in _TXN_BLOCK.finditer(text)
        if not any(start <= m.start() < end for start, end in templates)
    ]


def _parse_block(block: str, namespaces: dict[str, str]) -> ET.Element:
    declarations = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in namespaces.items())
    return ET.fromstring(f"<wrap {declarations}>{block}</wrap>")[0]


def _q(namespaces: dict[str, str], name: str) -> str:
    prefix, local = name.split(":", 1)
    return f"{{{namespaces[prefix]}}}{local}"


def _find(element: ET.Element, path: str, ns: dict[str, str]) -> ET.Element | None:
    return element.find(path, ns)


def _xml_text(element: ET.Element, path: str, ns: dict[str, str], default: str = "") -> str:
    found = element.find(path, ns)
    return (found.text or default) if found is not None else default


def _read_xml(path: Path) -> SourceBook:
    text, _gzipped = _load_xml(path)
    ns = _namespaces(text)
    body = _TEMPLATE_BLOCK.sub("", text)
    root = ET.fromstring(body)
    book = root.find("gnc:book", ns)
    scope = book if book is not None else root
    book_guid = _xml_text(scope, "book:id", ns) if "book" in ns else ""
    accounts: dict[str, SourceAccount] = {}
    root_guid = ""
    for element in scope.findall("gnc:account", ns):
        guid = _xml_text(element, "act:id", ns)
        space = _xml_text(element, "act:commodity/cmdty:space", ns)
        mnemonic = _xml_text(element, "act:commodity/cmdty:id", ns)
        scu = _xml_text(element, "act:commodity-scu", ns)
        accounts[guid] = SourceAccount(f"{space}:{mnemonic}" if mnemonic else "", int(scu or 100))
        if _xml_text(element, "act:type", ns) == "ROOT" and not root_guid:
            root_guid = guid
    currencies: dict[str, tuple[str, int]] = {}
    for element in scope.findall("gnc:commodity", ns):
        space = _xml_text(element, "cmdty:space", ns)
        mnemonic = _xml_text(element, "cmdty:id", ns)
        if space in ("CURRENCY", "ISO4217"):
            fraction = _xml_text(element, "cmdty:fraction", ns)
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
        value = _xml_text(split, "split:value", ns, "0/1")
        guid = _xml_text(split, "split:id", ns)
        splits[guid] = SourceSplit(
            guid,
            _xml_text(split, "split:account", ns),
            money_from_fraction(value),
            money_from_fraction(_xml_text(split, "split:quantity", ns, value)),
            _xml_text(split, "split:memo", ns),
            _xml_text(split, "split:action", ns),
            _xml_text(split, "split:reconciled-state", ns, "n"),
            _xml_text(split, "split:lot", ns),
        )
    return SourceTxn(
        _xml_text(element, "trn:id", ns),
        _xml_text(element, "trn:currency/cmdty:id", ns),
        _xml_text(element, "trn:num", ns),
        _safe_date(_xml_text(element, "trn:date-posted/ts:date", ns)),
        _xml_text(element, "trn:description", ns),
        splits,
    )


# ----------------------------------------------------------------- fingerprint


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
            if "books" not in _tables(conn):
                return ""
            row = conn.execute("SELECT guid FROM books LIMIT 1").fetchone()
            return str(row[0]) if row else ""
        finally:
            conn.close()
    text, _gzipped = _load_xml(path)
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
        return "gnclock" in _tables(conn) and bool(
            conn.execute("SELECT COUNT(*) FROM gnclock").fetchone()[0]
        )
    finally:
        conn.close()


def _preflight(db: DbSQLite) -> tuple[SourceFingerprint, Path]:
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


# ------------------------------------------------------------------ planning


@dataclass(frozen=True, slots=True)
class TargetSplit:
    guid: str
    account: str
    value: tuple[int, int]
    quantity: tuple[int, int]
    memo: str
    action: str
    state: str
    reconcile_date: date | None


@dataclass(frozen=True, slots=True)
class TargetTxn:
    guid: str
    currency: str
    num: str
    post_date: date
    description: str
    splits: tuple[TargetSplit, ...]


@dataclass(frozen=True, slots=True)
class _Operation:
    #: ``new``, ``edit``, or ``delete``.
    action: str
    guid: str
    target: TargetTxn | None


@dataclass(frozen=True, slots=True)
class WritebackChange:
    """Everything that would be written for one transaction."""

    transaction: str
    post_date: date
    description: str
    #: ``new``, ``edit``, ``reconcile``, and/or ``delete``.
    kinds: tuple[str, ...]
    #: Plain-language lines naming each change.
    details: tuple[str, ...]
    operation: _Operation | None = field(repr=False, default=None)


@dataclass(frozen=True, slots=True)
class WritebackUnsupported:
    transaction: str
    post_date: date
    description: str
    reason: str


@dataclass(frozen=True, slots=True)
class WritebackPlan:
    source: str
    changes: tuple[WritebackChange, ...]
    unsupported: tuple[WritebackUnsupported, ...]
    #: ``sqlite`` or ``xml``.
    format: str = "sqlite"


class _Refused(Exception):
    pass


def _pair(value: Money, fraction: int) -> tuple[int, int]:
    quantized = value.quantize(fraction)
    if quantized != value:
        raise _Refused("an amount has more precision than GnuCash allows for it")
    return quantized.as_gnc(fraction)


def _state(split) -> str:
    raw = split.reconcile.value if isinstance(split.reconcile, ReconcileState) else "n"
    return raw if raw in _WRITABLE_STATES else "n"


def _inventory(db: DbSQLite, root_guid: str) -> set[str]:
    raw = db.get_metadata(_INVENTORY_KEY, {})
    handles: set[str] = set()
    if not isinstance(raw, dict):
        return handles
    entries = [raw.get(f"gnucash:{root_guid}")] if root_guid else list(raw.values())
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("transactions"), list):
            handles.update(str(item) for item in entry["transactions"])
    return handles


def _skipped_identities(db: DbSQLite) -> set[str]:
    raw = db.get_metadata(_SKIPPED_HISTORY_KEY, {})
    found: set[str] = set()
    if isinstance(raw, dict):
        for entry in raw.values():
            if isinstance(entry, dict):
                found.update(
                    key.split(":", 1)[1] for key in entry if key.startswith("transaction:")
                )
    return found


def plan_writeback(db: DbSQLite) -> WritebackPlan:
    """Preview every supported local change; write nothing."""
    _recorded_fp, path = _preflight(db)
    return _plan(db, read_book(path))


def _plan(db: DbSQLite, book: SourceBook) -> WritebackPlan:
    source_guid = {
        account.handle: account.source_guid
        for account in db.iter_accounts()
        if account.source_guid and account.source_guid in book.accounts
    }
    changes: list[WritebackChange] = []
    unsupported: list[WritebackUnsupported] = []
    from_gnucash = _inventory(db, book.root_guid)

    def refuse(guid: str, when: date, description: str, reason: str) -> None:
        unsupported.append(WritebackUnsupported(guid, when, description, reason))

    local_handles: set[str] = set()
    for transaction in sorted(db.iter_transactions(), key=lambda t: (t.post_date, t.handle)):
        local_handles.add(transaction.handle)
        existing = book.transactions.get(transaction.handle)
        if existing is None:
            if not any(split.account in source_guid for split in transaction.splits):
                continue  # entirely BreadSched-only accounts: nothing to do with GnuCash
            if transaction.handle in from_gnucash:
                refuse(
                    transaction.handle,
                    transaction.post_date,
                    transaction.description,
                    "deleted in GnuCash and kept here; not written back",
                )
                continue
        try:
            target = _target(db, transaction, book, source_guid, existing)
            change = _change(db, transaction, target, existing, book)
        except _Refused as exc:
            refuse(transaction.handle, transaction.post_date, transaction.description, str(exc))
            continue
        if change is not None:
            changes.append(change)

    skipped = _skipped_identities(db)
    for guid in sorted(from_gnucash - local_handles - skipped):
        source = book.transactions.get(guid)
        if source is None:
            continue
        when = source.post_date or date.min
        if source.reconciled:
            refuse(guid, when, source.description, "reconciled in GnuCash; not deleted there")
            continue
        if source.in_lot:
            refuse(guid, when, source.description, "part of a GnuCash lot; not deleted there")
            continue
        changes.append(
            WritebackChange(
                guid,
                when,
                source.description,
                ("delete",),
                (f"Delete {source.description!r} ({len(source.splits)} splits) from GnuCash",),
                _Operation("delete", guid, None),
            )
        )
    changes.sort(key=lambda item: (item.post_date, item.transaction))
    return WritebackPlan(str(book.path), tuple(changes), tuple(unsupported), book.format)


def _currency_of(db: DbSQLite, transaction: Transaction) -> tuple[str, int]:
    currency = db.get_commodity(transaction.currency) if transaction.currency else None
    if currency is None:
        from ...gen.engine.currency import reporting_currency_handle

        currency = db.get_commodity(reporting_currency_handle(db))
    if currency is None:
        raise _Refused("its currency is not known")
    return currency.mnemonic, int(currency.fraction or 100)


def _target(
    db: DbSQLite,
    transaction: Transaction,
    book: SourceBook,
    source_guid: dict[str, str],
    existing: SourceTxn | None,
) -> TargetTxn:
    mnemonic, fraction = _currency_of(db, transaction)
    if mnemonic not in book.currencies:
        raise _Refused("its currency is not in the GnuCash book")
    if book.currencies[mnemonic][1]:
        fraction = book.currencies[mnemonic][1]
    if existing is not None and existing.currency and existing.currency != mnemonic:
        raise _Refused("its currency changed; only GnuCash can change that")
    splits: list[TargetSplit] = []
    for split in transaction.splits:
        account_guid = source_guid.get(split.account)
        if account_guid is None:
            raise _Refused("an account is not in the GnuCash book")
        account = book.accounts[account_guid]
        value = _pair(split.value, fraction)
        in_currency = account.commodity in ("", f"CURRENCY:{mnemonic}", f"ISO4217:{mnemonic}")
        if in_currency:
            # GnuCash keeps quantity equal to value in the transaction currency.
            quantity = value
        else:
            if split.quantity is None:
                raise _Refused("a split in a security or foreign-currency account has no quantity")
            quantity = _pair(split.quantity, account.scu)
        state = _state(split)
        prior = existing.splits.get(split.handle) if existing is not None else None
        reconcile_date = split.reconcile_date if state == "y" else None
        if state == "y" and reconcile_date is None:
            reconcile_date = transaction.post_date
        splits.append(
            TargetSplit(
                split.handle,
                account_guid,
                value,
                quantity,
                split.memo or "",
                split.action or "",
                state,
                reconcile_date if (prior is None or prior.state != state) else None,
            )
        )
    return TargetTxn(
        transaction.handle,
        mnemonic,
        transaction.num or "",
        transaction.post_date,
        transaction.description,
        tuple(splits),
    )


def _money(pair: tuple[int, int]) -> Money:
    return Money(pair[0], pair[1])


def _change(
    db: DbSQLite,
    transaction: Transaction,
    target: TargetTxn,
    existing: SourceTxn | None,
    book: SourceBook,
) -> WritebackChange | None:
    names = {
        account.source_guid: db.full_name(account)
        for account in db.iter_accounts()
        if account.source_guid
    }

    def name(account_guid: str) -> str:
        return names.get(account_guid, account_guid[:8])

    if existing is None:
        if len(target.splits) < 2:
            raise _Refused("a GnuCash transaction needs at least two splits")
        lines = [
            f"New transaction {target.guid[:8]}: {target.post_date.isoformat()} "
            f"{target.description!r} in {target.currency}"
        ]
        lines.extend(
            f"  split {split.guid[:8]}: {name(split.account)} "
            f"{_money(split.value).format()} (reconcile {split.state})"
            for split in target.splits
        )
        return WritebackChange(
            target.guid,
            target.post_date,
            target.description,
            ("new",),
            tuple(lines),
            _Operation("new", target.guid, target),
        )

    details: list[str] = []
    ledger_changed = False
    for label, before, after in (
        ("date", existing.post_date, target.post_date),
        ("description", existing.description, target.description),
        ("number", existing.num, target.num),
    ):
        if before != after:
            details.append(f"{label}: {_shown(before)} -> {_shown(after)}")
            ledger_changed = True
    reconcile_lines: list[str] = []
    wanted = {split.guid: split for split in target.splits}
    for guid, removed in existing.splits.items():
        if guid not in wanted:
            details.append(
                f"remove split {name(removed.account)} {removed.value.format(parens_negative=True)}"
            )
            ledger_changed = True
    for split in target.splits:
        prior = existing.splits.get(split.guid)
        label = name(split.account)
        if prior is None:
            details.append(f"add split {label} {_money(split.value).format()}")
            ledger_changed = True
            continue
        if prior.account != split.account:
            details.append(f"split account: {name(prior.account)} -> {label}")
            ledger_changed = True
        if prior.value != _money(split.value):
            details.append(
                f"amount on {label}: {prior.value.format()} -> {_money(split.value).format()}"
            )
            ledger_changed = True
        if prior.quantity != _money(split.quantity):
            ledger_changed = True
        if prior.quantity != _money(split.quantity) and split.quantity != split.value:
            details.append(f"quantity on {label}: {prior.quantity} -> {_money(split.quantity)}")
            ledger_changed = True
        for field_label, before, after in (
            ("memo", prior.memo, split.memo),
            ("action", prior.action, split.action),
        ):
            if before != after:
                details.append(f"{field_label} on {label}: {_shown(before)} -> {_shown(after)}")
                ledger_changed = True
        if prior.state != split.state:
            reconcile_lines.append(f"reconcile on {label}: {prior.state} -> {split.state}")
    if ledger_changed and existing.reconciled:
        raise _Refused("reconciled in GnuCash; only its reconcile state is written back")
    if ledger_changed and existing.in_lot:
        raise _Refused("part of a GnuCash lot; only its reconcile state is written back")
    if not details and not reconcile_lines:
        return None
    kinds = (("edit",) if details else ()) + (("reconcile",) if reconcile_lines else ())
    return WritebackChange(
        target.guid,
        target.post_date,
        target.description,
        kinds,
        tuple(details + reconcile_lines),
        _Operation("edit", target.guid, target),
    )


def _safe_date(raw: object) -> date | None:
    try:
        return parse_gnc_date(str(raw)) if raw else None
    except ValueError:
        return None


def _shown(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if value is None:
        return "(blank)"
    text = str(value)
    return repr(text) if text else "(blank)"


# --------------------------------------------------------------- SQLite backend


def _sql_stamp(when: date, style: str) -> str:
    if style == "compact":
        return when.strftime("%Y%m%d") + _NEUTRAL_TIME.replace(":", "")
    return f"{when.isoformat()} {_NEUTRAL_TIME}"


def _slot_ids(conn: sqlite3.Connection, owners: Iterable[str]) -> list[int]:
    """Every slot row owned by ``owners``, following nested frames."""
    ids: list[int] = []
    frontier = set(owners)
    seen: set[str] = set()
    while frontier:
        seen |= frontier
        marks = ",".join("?" * len(frontier))
        rows = conn.execute(
            f"SELECT id, slot_type, guid_val FROM slots WHERE obj_guid IN ({marks})",
            tuple(frontier),
        ).fetchall()
        ids.extend(row[0] for row in rows)
        frontier = {row[2] for row in rows if row[1] == 9 and row[2]} - seen
    return ids


def _delete_slots(conn: sqlite3.Connection, owners: Iterable[str]) -> None:
    ids = _slot_ids(conn, owners)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        conn.execute(f"DELETE FROM slots WHERE id IN ({','.join('?' * len(chunk))})", chunk)


def _expect(cursor: sqlite3.Cursor, description: str) -> None:
    if cursor.rowcount != 1:
        raise WritebackError(
            "writeback.source.conflict",
            f"GnuCash no longer matches the preview for {description!r}",
        )


def _sql_split_row(target: TargetTxn, split: TargetSplit, style: str) -> tuple[Any, ...]:
    reconciled = _sql_stamp(split.reconcile_date, style) if split.reconcile_date else None
    return (
        split.guid,
        target.guid,
        split.account,
        split.memo,
        split.action,
        split.state,
        reconciled,
        split.value[0],
        split.value[1],
        split.quantity[0],
        split.quantity[1],
        None,
    )


def _sqlite_apply(conn: sqlite3.Connection, book: SourceBook, change: WritebackChange) -> None:
    operation = change.operation
    assert operation is not None
    style = book.date_style
    has_slots = "slots" in _tables(conn)
    if operation.action == "delete":
        splits = [row[0] for row in conn.execute(
            "SELECT guid FROM splits WHERE tx_guid=?", (operation.guid,)
        )]  # fmt: skip
        if has_slots:
            _delete_slots(conn, [operation.guid, *splits])
        conn.execute("DELETE FROM splits WHERE tx_guid=?", (operation.guid,))
        _expect(conn.execute("DELETE FROM transactions WHERE guid=?", (operation.guid,)),
                change.description)  # fmt: skip
        return
    target = operation.target
    assert target is not None
    currency_guid = book.currencies[target.currency][0]
    posted = _sql_stamp(target.post_date, style)
    if operation.action == "new":
        now = datetime.now(timezone.utc)
        entered = (
            now.strftime("%Y%m%d%H%M%S")
            if style == "compact"
            else now.strftime("%Y-%m-%d %H:%M:%S")
        )
        conn.execute(
            "INSERT INTO transactions (guid, currency_guid, num, post_date, enter_date,"
            " description) VALUES (?,?,?,?,?,?)",
            (target.guid, currency_guid, target.num, posted, entered, target.description),
        )
        if has_slots:
            conn.execute(
                "INSERT INTO slots (obj_guid, name, slot_type, gdate_val) VALUES (?,?,?,?)",
                (target.guid, "date-posted", 10, target.post_date.strftime("%Y%m%d")),
            )
        existing: dict[str, SourceSplit] = {}
    else:
        _expect(
            conn.execute(
                "UPDATE transactions SET num=?, post_date=?, description=? WHERE guid=?",
                (target.num, posted, target.description, target.guid),
            ),
            change.description,
        )
        if has_slots:
            conn.execute(
                "UPDATE slots SET gdate_val=? WHERE obj_guid=? AND name='date-posted'",
                (target.post_date.strftime("%Y%m%d"), target.guid),
            )
        existing = book.transactions[target.guid].splits
    wanted = {split.guid for split in target.splits}
    for guid in existing:
        if guid not in wanted:
            if has_slots:
                _delete_slots(conn, [guid])
            _expect(conn.execute("DELETE FROM splits WHERE guid=?", (guid,)), change.description)
    for split in target.splits:
        if split.guid not in existing:
            conn.execute(
                "INSERT INTO splits (guid, tx_guid, account_guid, memo, action,"
                " reconcile_state, reconcile_date, value_num, value_denom, quantity_num,"
                " quantity_denom, lot_guid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                _sql_split_row(target, split, style),
            )
            continue
        assignments = (
            "account_guid=?, memo=?, action=?, reconcile_state=?, value_num=?,"
            " value_denom=?, quantity_num=?, quantity_denom=?"
        )
        params: list[Any] = [
            split.account,
            split.memo,
            split.action,
            split.state,
            split.value[0],
            split.value[1],
            split.quantity[0],
            split.quantity[1],
        ]
        if split.reconcile_date is not None:
            assignments += ", reconcile_date=?"
            params.append(_sql_stamp(split.reconcile_date, style))
        _expect(
            conn.execute(
                f"UPDATE splits SET {assignments} WHERE guid=? AND tx_guid=?",
                (*params, split.guid, target.guid),
            ),
            change.description,
        )


def _write_sqlite(path: Path, book: SourceBook, selected: tuple[WritebackChange, ...]) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if (
            "gnclock" in _tables(conn)
            and conn.execute("SELECT COUNT(*) FROM gnclock").fetchone()[0]
        ):
            raise WritebackError(
                "writeback.source.locked", "GnuCash has the book open; close it in GnuCash first"
            )
        for change in selected:
            _sqlite_apply(conn, book, change)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# ------------------------------------------------------------------ XML backend


def _prefixed(tag: str, prefixes: dict[str, str]) -> str:
    if tag.startswith("{"):
        uri, local = tag[1:].split("}", 1)
        return f"{prefixes[uri]}:{local}"
    return tag


def _serialize(element: ET.Element, prefixes: dict[str, str], depth: int = 0) -> str:
    """Write an element the way GnuCash lays out its XML: two-space indents."""
    pad = "  " * depth
    tag = _prefixed(element.tag, prefixes)
    attrs = "".join(
        f" {_prefixed(key, prefixes)}={quoteattr(value)}" for key, value in element.attrib.items()
    )
    children = list(element)
    if children:
        inner = "".join(_serialize(child, prefixes, depth + 1) for child in children)
        return f"{pad}<{tag}{attrs}>\n{inner}{pad}</{tag}>\n"
    if element.text:
        return f"{pad}<{tag}{attrs}>{escape(element.text)}</{tag}>\n"
    return f"{pad}<{tag}{attrs}/>\n"


def _xml_stamp(when: date) -> str:
    return f"{when.isoformat()} {_NEUTRAL_TIME} +0000"


def _set_child(
    parent: ET.Element, tag: str, text: str | None, order: list[str], *, keep_empty=False
) -> ET.Element | None:
    """Set, add (in GnuCash's element order), or remove a leaf child."""
    found = parent.find(tag)
    if not text and not keep_empty:
        if found is not None:
            parent.remove(found)
        return None
    if found is None:
        found = ET.Element(tag)
        position = order.index(tag)
        index = len(parent)
        for i, child in enumerate(parent):
            if child.tag in order and order.index(child.tag) > position:
                index = i
                break
        parent.insert(index, found)
    found.text = text
    return found


def _ts(parent: ET.Element, ns: dict[str, str], when: date) -> None:
    for child in list(parent):
        parent.remove(child)
    stamp = ET.SubElement(parent, _q(ns, "ts:date"))
    stamp.text = _xml_stamp(when)


def _xml_split(element: ET.Element | None, split: TargetSplit, ns: dict[str, str]) -> ET.Element:
    q = lambda name: _q(ns, name)  # noqa: E731
    order = [
        q("split:id"),
        q("split:memo"),
        q("split:action"),
        q("split:reconciled-state"),
        q("split:reconcile-date"),
        q("split:value"),
        q("split:quantity"),
        q("split:account"),
        q("split:lot"),
        q("split:slots"),
    ]
    if element is None:
        element = ET.Element(q("trn:split"))
        guid = ET.SubElement(element, q("split:id"), {"type": "guid"})
        guid.text = split.guid
    _set_child(element, q("split:memo"), split.memo, order)
    _set_child(element, q("split:action"), split.action, order)
    _set_child(element, q("split:reconciled-state"), split.state, order, keep_empty=True)
    if split.reconcile_date is not None:
        holder = element.find(q("split:reconcile-date"))
        if holder is None:
            holder = _set_child(element, q("split:reconcile-date"), "", order, keep_empty=True)
        assert holder is not None
        holder.text = None
        _ts(holder, ns, split.reconcile_date)
    _set_child(element, q("split:value"), f"{split.value[0]}/{split.value[1]}", order)
    _set_child(element, q("split:quantity"), f"{split.quantity[0]}/{split.quantity[1]}", order)
    account = _set_child(element, q("split:account"), split.account, order)
    assert account is not None
    account.attrib["type"] = "guid"
    return element


def _date_slot(element: ET.Element, ns: dict[str, str], when: date, create: bool) -> None:
    slots = element.find("trn:slots", ns)
    for slot in slots.findall("slot", ns) if slots is not None else []:
        key = slot.find("slot:key", ns)
        if key is not None and key.text == "date-posted":
            gdate = slot.find("slot:value/gdate", ns)
            if gdate is not None:
                gdate.text = when.isoformat()
            return
    if not create:
        return
    if slots is None:
        slots = ET.Element(_q(ns, "trn:slots"))
        splits = element.find("trn:splits", ns)
        index = list(element).index(splits) if splits is not None else len(element)
        element.insert(index, slots)
    slot = ET.SubElement(slots, "slot")
    ET.SubElement(slot, _q(ns, "slot:key")).text = "date-posted"
    value = ET.SubElement(slot, _q(ns, "slot:value"), {"type": "gdate"})
    ET.SubElement(value, "gdate").text = when.isoformat()


def _xml_transaction_element(
    element: ET.Element | None, target: TargetTxn, ns: dict[str, str]
) -> ET.Element:
    q = lambda name: _q(ns, name)  # noqa: E731
    order = [
        q("trn:id"),
        q("trn:currency"),
        q("trn:num"),
        q("trn:date-posted"),
        q("trn:date-entered"),
        q("trn:description"),
        q("trn:slots"),
        q("trn:splits"),
    ]
    creating = element is None
    if element is None:
        element = ET.Element(q("gnc:transaction"), {"version": "2.0.0"})
        ET.SubElement(element, q("trn:id"), {"type": "guid"}).text = target.guid
        currency = ET.SubElement(element, q("trn:currency"))
        ET.SubElement(currency, q("cmdty:space")).text = "CURRENCY"
        ET.SubElement(currency, q("cmdty:id")).text = target.currency
        posted = ET.SubElement(element, q("trn:date-posted"))
        _ts(posted, ns, target.post_date)
        entered = ET.SubElement(element, q("trn:date-entered"))
        stamp = ET.SubElement(entered, q("ts:date"))
        stamp.text = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000")
        ET.SubElement(element, q("trn:description"))
        ET.SubElement(element, q("trn:splits"))
    _set_child(element, q("trn:num"), target.num, order)
    posted_holder = element.find(q("trn:date-posted"))
    assert posted_holder is not None
    _ts(posted_holder, ns, target.post_date)
    _set_child(element, q("trn:description"), target.description, order, keep_empty=True)
    _date_slot(element, ns, target.post_date, creating)
    splits = element.find(q("trn:splits"))
    assert splits is not None
    current = {_xml_text(split, "split:id", ns): split for split in splits.findall(q("trn:split"))}
    wanted = {split.guid for split in target.splits}
    for guid, split_element in current.items():
        if guid not in wanted:
            splits.remove(split_element)
    for split in target.splits:
        existing = current.get(split.guid)
        updated = _xml_split(existing, split, ns)
        if existing is None:
            splits.append(updated)
    return element


def _write_xml(path: Path, book: SourceBook, selected: tuple[WritebackChange, ...]) -> None:
    text, gzipped = _load_xml(path)
    # Written blocks follow the book's own line endings, so a book saved with
    # CRLF does not end up with mixed ones.
    newline = "\r\n" if "\r\n" in text else "\n"

    def serialize(element: ET.Element) -> str:
        return _serialize(element, prefixes).replace("\n", newline)

    ns = _namespaces(text)
    prefixes = {uri: prefix for prefix, uri in ns.items()}
    spans = {}
    for start, end in _book_spans(text):
        found = re.search(r'<trn:id type="guid">([0-9a-f]+)</trn:id>', text[start:end])
        if found:
            spans[found.group(1)] = (start, end)
    edits: list[tuple[int, int, str]] = []
    added: list[str] = []
    delta = 0
    for change in selected:
        operation = change.operation
        assert operation is not None
        if operation.action == "new":
            assert operation.target is not None
            element = _xml_transaction_element(None, operation.target, ns)
            added.append(serialize(element))
            delta += 1
            continue
        if operation.guid not in spans:
            raise WritebackError(
                "writeback.source.conflict",
                f"GnuCash no longer matches the preview for {change.description!r}",
            )
        start, end = spans[operation.guid]
        if operation.action == "delete":
            edits.append((start, end, ""))
            delta -= 1
            continue
        assert operation.target is not None
        element = _parse_block(text[start:end], ns)
        _xml_transaction_element(element, operation.target, ns)
        edits.append((start, end, serialize(element)))
    if added:
        anchor = max((end for _start, end in spans.values()), default=None)
        if anchor is None:
            anchor = _insert_point(text)
        edits.append((anchor, anchor, "".join(added)))
    for start, end, replacement in sorted(edits, key=lambda item: item[0], reverse=True):
        text = text[:start] + replacement + text[end:]
    if delta:
        text = _recount(text, delta, newline)
    payload = text.encode("utf-8")
    if gzipped:
        payload = gzip.compress(payload, mtime=0)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _insert_point(text: str) -> int:
    """Where the first transaction goes when the book has none yet."""
    for marker in (
        "<gnc:template-transactions>",
        '<gnc:schedxaction version="2.0.0">',
        '<gnc:budget version="2.0.0">',
        "</gnc:book>",
    ):
        position = text.find(marker)
        if position != -1:
            return position
    raise WritebackError("writeback.source.unreadable", "the GnuCash XML book has no book element")


def _recount(text: str, delta: int, newline: str = "\n") -> str:
    pattern = re.compile(r'(<gnc:count-data cd:type="transaction">)(\d+)(</gnc:count-data>)')
    found = pattern.search(text)
    if found is None:
        if delta > 0:
            book = re.search(r'<book:id type="guid">[0-9a-f]+</book:id>\r?\n', text)
            if book is not None:
                line = f'<gnc:count-data cd:type="transaction">{delta}</gnc:count-data>{newline}'
                return text[: book.end()] + line + text[book.end() :]
        return text
    count = max(0, int(found.group(2)) + delta)
    return text[: found.start(2)] + str(count) + text[found.end(2) :]


# ------------------------------------------------------------------- applying


def _backup_dir(db: DbSQLite, source: Path) -> Path:
    base = Path(db.path).expanduser().resolve().parent if db.path else source.parent
    stem = Path(db.path).stem if db.path else "breadsched"
    return base / f"{stem}-gnucash-backups"


def _rotate(directory: Path, source: Path, keep: int) -> None:
    backups = sorted(directory.glob(f"{source.name}.*.bak"))
    for stale in backups[: max(0, len(backups) - keep)]:
        stale.unlink(missing_ok=True)


def apply_plan(
    db: DbSQLite,
    plan: WritebackPlan,
    chosen: Iterable[str],
    *,
    keep_backups: int = DEFAULT_KEEP_BACKUPS,
) -> tuple[tuple[WritebackChange, ...], Path]:
    """Write the chosen transactions atomically; return them and the backup.

    The preflight is repeated, the book is re-read, and every written transaction
    is read back and compared. Any mismatch or error restores the backup, so the
    book is either fully written or exactly as it was.
    """
    _recorded_fp, path = _preflight(db)
    if str(path) != plan.source:
        raise WritebackError(
            "writeback.source.changed", "the previewed book is not the imported one"
        )
    wanted = set(chosen)
    selected = tuple(change for change in plan.changes if change.transaction in wanted)
    if not selected or len(selected) != len(wanted):
        raise WritebackError("writeback.selection.invalid", "choose changes from the preview")
    book = read_book(path)

    directory = _backup_dir(db, path)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    backup = directory / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, backup)
    try:
        if book.format == "sqlite":
            _write_sqlite(path, book, selected)
        else:
            _write_xml(path, book, selected)
        _verify(path, selected)
        mismatched = [
            change.description
            for change in selected
            if change.operation is not None
            and change.operation.action != "delete"
            and written_facts(db.get_transaction(change.transaction))
            != source_facts(db, path, change.transaction)
        ]
        if mismatched:
            raise WritebackError(
                "writeback.roundtrip.mismatch",
                f"reading {mismatched[0]!r} back from GnuCash would not reproduce it",
            )
    except Exception:
        shutil.copy2(backup, path)
        raise
    _record_written(db, path, book, selected)
    _rotate(directory, path, max(1, keep_backups))
    return selected, backup


def _target_facts(target: TargetTxn) -> tuple[object, ...]:
    return (
        target.post_date,
        target.description,
        target.num,
        tuple(
            sorted(
                (
                    split.guid,
                    split.account,
                    _money(split.value),
                    _money(split.quantity),
                    split.memo,
                    split.action,
                    split.state,
                )
                for split in target.splits
            )
        ),
    )


def _source_txn_facts(source: SourceTxn) -> tuple[object, ...]:
    return (
        source.post_date,
        source.description,
        source.num,
        tuple(
            sorted(
                (
                    split.guid,
                    split.account,
                    split.value,
                    split.quantity,
                    split.memo,
                    split.action,
                    split.state,
                )
                for split in source.splits.values()
            )
        ),
    )


def _verify(path: Path, selected: tuple[WritebackChange, ...]) -> None:
    """Re-read the book and compare every written transaction with what was meant."""
    book = read_book(path)
    for change in selected:
        operation = change.operation
        assert operation is not None
        written = book.transactions.get(operation.guid)
        if operation.action == "delete":
            ok = written is None
        else:
            assert operation.target is not None
            ok = written is not None and _source_txn_facts(written) == _target_facts(
                operation.target
            )
        if not ok:
            raise WritebackError(
                "writeback.verify.failed",
                f"reading back {change.description!r} did not match what was written",
            )


def written_facts(transaction: Transaction | None) -> tuple[object, ...] | None:
    """The facts a write-back sends, as BreadSched holds them."""
    if transaction is None:
        return None
    return (
        transaction.post_date,
        transaction.description,
        transaction.num or "",
        tuple(
            sorted(
                (split.handle, split.account, split.value, split.memo or "", _state(split))
                for split in transaction.splits
            )
        ),
    )


def source_facts(db: DbSQLite, path: Path, guid: str) -> tuple[object, ...] | None:
    """The same facts read from GnuCash as an import would map them.

    This is the round-trip proof: the written rows, read with the importer's own
    date and amount rules and GnuCash-to-BreadSched account mapping, must equal the
    BreadSched transaction. Nothing is re-imported, so local edits that were not
    chosen for this write stay exactly as they are.
    """
    accounts = {
        account.source_guid: account.handle for account in db.iter_accounts() if account.source_guid
    }
    source = read_book(path).transactions.get(guid)
    if source is None:
        return None
    return (
        source.post_date,
        source.description,
        source.num,
        tuple(
            sorted(
                (
                    split.guid,
                    accounts.get(split.account, split.account),
                    split.value,
                    split.memo,
                    split.state,
                )
                for split in source.splits.values()
            )
        ),
    )


def _record_written(
    db: DbSQLite, path: Path, book: SourceBook, selected: tuple[WritebackChange, ...]
) -> None:
    """Make the written book the imported baseline, touching nothing else.

    The new fingerprint lets the next preview prove GnuCash unchanged again. New
    transactions join the import inventory so GnuCash owns them from now on, and
    deleted ones leave it.
    """
    with db.transaction("Record GnuCash write-back") as txn:
        record_fingerprint(db, path, file_digest(path), txn)
        new = {change.transaction for change in selected if "new" in change.kinds}
        gone = {change.transaction for change in selected if "delete" in change.kinds}
        if (new or gone) and book.root_guid:
            raw = db.get_metadata(_INVENTORY_KEY, {})
            inventory = dict(raw) if isinstance(raw, dict) else {}
            key = f"gnucash:{book.root_guid}"
            entry = inventory.get(key)
            handles = (
                set(entry.get("transactions", []))
                if isinstance(entry, dict) and isinstance(entry.get("transactions"), list)
                else set()
            )
            inventory[key] = {"transactions": sorted((handles | new) - gone)}
            db.set_metadata(_INVENTORY_KEY, inventory, txn)
