"""Import bank and credit-card transactions from OFX/QFX files.

OFX 1.x is SGML-like rather than well-formed XML, while OFX 2.x is XML.  Banking
statement transaction aggregates use the same tag names in both generations, so
this importer deliberately extracts those aggregates and scalar tags directly.
That keeps the parser deterministic, standard-library-only, and tolerant of the
legacy files still downloaded from many financial institutions.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money
from ...gen.utils.logs import get_logger
from .gnucash_common import ImportResult, ImportSink

LOG = get_logger(__name__)

__all__ = ["import_book", "sniff"]

_TAG_CACHE: dict[str, re.Pattern[str]] = {}


def sniff(path: str | Path) -> bool:
    """Return True when the file looks like OFX/QFX data."""
    try:
        head = Path(path).read_bytes()[:8192].decode("ascii", errors="ignore").upper()
    except OSError:
        return False
    return "<OFX>" in head or "OFXHEADER:" in head


def _stable_handle(kind: str, *parts: object) -> str:
    text = "|".join(str(part) for part in parts)
    return uuid5(NAMESPACE_URL, f"breadsched:ofx:{kind}:{text}").hex


def _pattern(tag: str) -> re.Pattern[str]:
    key = tag.upper()
    if key not in _TAG_CACHE:
        _TAG_CACHE[key] = re.compile(
            rf"<{re.escape(key)}(?:\s[^>]*)?>\s*([^<\r\n]+)", re.IGNORECASE
        )
    return _TAG_CACHE[key]


def _tag(text: str, name: str, default: str = "") -> str:
    match = _pattern(name).search(text)
    return match.group(1).strip() if match else default


def _blocks(text: str, name: str) -> list[str]:
    return re.findall(
        rf"<{re.escape(name)}(?:\s[^>]*)?>(.*?)</{re.escape(name)}\s*>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _parse_date(raw: str) -> date:
    digits = "".join(char for char in raw if char.isdigit())
    if len(digits) < 8:
        raise ValueError(f"unrecognised OFX date {raw!r}")
    return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))


def _parse_amount(raw: str) -> Money:
    try:
        return Money(Decimal(raw.strip().replace(",", "")))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"unrecognised OFX amount {raw!r}") from exc


def _top_level(db: DbSQLite, name: str) -> str | None:
    account = db.get_account_by_name(name)
    return account.handle if account is not None else None


def _source_account(
    sink: ImportSink,
    db: DbSQLite,
    account_id: str,
    account_type: str,
    institution: str,
    commodity: str | None,
) -> str:
    kind = account_type.upper()
    credit = kind in {"CREDITCARD", "CREDITLINE"}
    atype = "CREDIT" if credit else "BANK"
    parent = _top_level(db, "Liabilities" if credit else "Assets")
    tail = account_id[-4:] if account_id else "unknown"
    label = {
        "CHECKING": "Checking",
        "SAVINGS": "Savings",
        "MONEYMRKT": "Money Market",
        "CREDITCARD": "Credit Card",
        "CREDITLINE": "Credit Line",
    }.get(kind, "Bank Account")
    name = f"{institution} {label} {tail}".strip() if institution else f"{label} {tail}"
    guid = _stable_handle("account", account_id, kind, institution)
    return sink.account(
        guid, name, atype, parent, commodity=commodity,
        description=f"Imported OFX account ending {tail}",
    ).handle


def _counter_account(sink: ImportSink, db: DbSQLite, amount: Money) -> str:
    if amount < 0:
        parent = _top_level(db, "Expenses")
        atype = "EXPENSE"
        name = "Uncategorized OFX"
    else:
        parent = _top_level(db, "Income")
        atype = "INCOME"
        name = "Uncategorized OFX"
    guid = _stable_handle("category", atype, name)
    return sink.account(guid, name, atype, parent).handle


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    head = raw[:4096].decode("ascii", errors="ignore")
    charset = _tag(head, "CHARSET").upper()
    encodings = ["utf-8-sig"]
    if charset in {"1252", "WINDOWS-1252"}:
        encodings.insert(0, "cp1252")
    encodings.extend(["cp1252", "latin-1"])
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def import_book(
    db: DbSQLite,
    path: str | Path,
    include_scheduled: bool = True,
    message: str | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> ImportResult:
    """Import OFX/QFX bank and credit-card statement transactions."""
    del include_scheduled
    source = Path(path)
    text = _read_text(source)
    result = ImportResult(source=str(source), source_format="ofx")
    if "<INVSTMTRS" in text.upper() or "<INVSTMTTRNRS" in text.upper():
        result.warn("OFX investment transactions are not imported yet")

    currency_code = _tag(text, "CURDEF", "USD") or "USD"
    institution = _tag(text, "ORG") or _tag(text, "FI")
    account_block = ""
    account_type = ""
    bank_blocks = _blocks(text, "BANKACCTFROM")
    card_blocks = _blocks(text, "CCACCTFROM")
    if bank_blocks:
        account_block = bank_blocks[0]
        account_type = _tag(account_block, "ACCTTYPE", "CHECKING")
    elif card_blocks:
        account_block = card_blocks[0]
        account_type = "CREDITCARD"
    else:
        result.warn("No bank or credit-card account information was found")
        return result
    account_id = _tag(account_block, "ACCTID")
    transaction_blocks = _blocks(text, "STMTTRN")

    def report(done: int) -> None:
        if progress is not None:
            progress("Reading OFX transactions", done, len(transaction_blocks))

    with db.transaction(message or f"Import {source.name}", batch=True) as txn:
        sink = ImportSink(db, txn, result)
        commodity = sink.commodity("CURRENCY", currency_code, currency_code)
        source_account = _source_account(
            sink, db, account_id, account_type, institution, commodity
        )
        fallback_counts: dict[tuple[object, ...], int] = {}
        for index, block in enumerate(transaction_blocks, 1):
            report(index)
            try:
                post_date = _parse_date(_tag(block, "DTPOSTED"))
                amount = _parse_amount(_tag(block, "TRNAMT"))
            except ValueError as exc:
                result.skip(str(exc), _tag(block, "NAME", "transaction"))
                continue
            fitid = _tag(block, "FITID")
            name = _tag(block, "NAME")
            memo = _tag(block, "MEMO")
            description = name or memo or _tag(block, "TRNTYPE") or "OFX transaction"
            counter = _counter_account(sink, db, amount)
            if fitid:
                identity = fitid
            else:
                fallback = (
                    account_id, post_date.isoformat(), str(amount), description, memo,
                    _tag(block, "CHECKNUM"),
                )
                occurrence = fallback_counts.get(fallback, 0) + 1
                fallback_counts[fallback] = occurrence
                identity = _stable_handle("fallback", *fallback, occurrence)
            sink.transaction(
                _stable_handle("transaction", account_id, identity),
                post_date,
                description,
                currency_code,
                _tag(block, "CHECKNUM"),
                [
                    {
                        "account": source_account,
                        "value": amount,
                        "memo": memo,
                    },
                    {"account": counter, "value": -amount},
                ],
            )
        if progress is not None:
            progress("Finishing", len(transaction_blocks), len(transaction_blocks))
    db.emit("database-changed", (db,))
    LOG.info("OFX import finished: %s", result.describe())
    return result
