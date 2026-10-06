"""Import bank and credit-card transactions from OFX/QFX files.

OFX 1.x is SGML-like rather than well-formed XML, while OFX 2.x is XML.  Banking
statement transaction aggregates use the same tag names in both generations, so
this importer deliberately extracts those aggregates and scalar tags directly.
That keeps the parser deterministic, standard-library-only, and tolerant of the
legacy files still downloaded from many financial institutions.

Re-importing a statement recognizes its rows by FITID (or, without one, by the
row's own facts) and changes nothing. A new row that matches a transaction from
elsewhere in the same account (entered by hand, imported from a CSV, or imported
from an earlier statement under a different FITID) on the same date for the same
amount is held back as a possible duplicate, one row per existing transaction,
unless the caller includes possible duplicates. The statement's own transactions
never count as such a match.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money
from ...gen.utils.amount_input import (
    NumberFormat,
    detect_number_format,
    parse_decimal_amount,
)
from ...gen.utils.logs import get_logger
from .duplicates import DuplicateGuard
from .gnucash_common import ImportResult, ImportSink
from .quotes import record_security_quote

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


def _parse_amount(raw: str, number_format: NumberFormat) -> Money:
    try:
        return Money(parse_decimal_amount(raw, number_format))
    except ValueError as exc:
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
        guid,
        name,
        atype,
        parent,
        commodity=commodity,
        description=f"Imported OFX account ending {tail}",
    ).handle


def _row_handle(
    block: str,
    account_id: str,
    fallback_counts: dict[tuple[object, ...], int],
    number_format: NumberFormat,
) -> str | None:
    """The stable transaction handle of one STMTTRN, or ``None`` when it is unreadable.

    A FITID identifies the row; without one, its own facts and their occurrence
    count in this statement do, so identical rows stay distinct.
    """
    try:
        post_date = _parse_date(_tag(block, "DTPOSTED"))
        amount = _parse_amount(_tag(block, "TRNAMT"), number_format)
    except ValueError:
        return None
    fitid = _tag(block, "FITID")
    if fitid:
        identity = fitid
    else:
        memo = _tag(block, "MEMO")
        description = _tag(block, "NAME") or memo or _tag(block, "TRNTYPE") or "OFX transaction"
        fallback = (
            account_id,
            post_date.isoformat(),
            str(amount),
            description,
            memo,
            _tag(block, "CHECKNUM"),
        )
        occurrence = fallback_counts.get(fallback, 0) + 1
        fallback_counts[fallback] = occurrence
        identity = _stable_handle("fallback", *fallback, occurrence)
    return _stable_handle("transaction", account_id, identity)


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


def _transaction_rate(block: str) -> tuple[str, str, bool] | None:
    """(currency code, rate, amounts-are-foreign) from a transaction's rate aggregate.

    ``CURRENCY`` means the transaction's amounts are in ``CURSYM``; ``ORIGCURRENCY``
    means they were already converted to the statement currency (``CURDEF``) and
    ``CURSYM`` names the original. Either way ``CURRATE`` is the number of
    statement-currency units per unit of ``CURSYM``.
    """
    for name, foreign_amounts in (("CURRENCY", True), ("ORIGCURRENCY", False)):
        found = _blocks(block, name)
        if found:
            return _tag(found[0], "CURSYM"), _tag(found[0], "CURRATE"), foreign_amounts
    return None


def _parse_rate(raw: str, number_format: NumberFormat) -> Decimal | None:
    try:
        rate = parse_decimal_amount(raw, number_format)
    except ValueError:
        return None
    return rate if rate > 0 else None


def _security_quotes(text: str) -> list[tuple[str, str, str, str]]:
    """(ticker, price, date, currency) from SECLIST ``SECINFO`` and ``INVPOS``.

    A position's price is dated by ``DTPRICEASOF``; a security list entry by
    ``DTASOF``. Positions name their security only by ``UNIQUEID``, which the
    security list maps to a ticker.
    """
    tickers: dict[str, str] = {}
    quotes: list[tuple[str, str, str, str]] = []
    for block in _blocks(text, "SECINFO"):
        unique = _tag(block, "UNIQUEID")
        ticker = _tag(block, "TICKER") or unique
        if unique:
            tickers[unique] = ticker
        price = _tag(block, "UNITPRICE")
        if price:
            quotes.append((ticker, price, _tag(block, "DTASOF"), _tag(block, "CURSYM")))
    for block in _blocks(text, "INVPOS"):
        unique = _tag(block, "UNIQUEID")
        price = _tag(block, "UNITPRICE")
        if price:
            quotes.append(
                (
                    tickers.get(unique, unique),
                    price,
                    _tag(block, "DTPRICEASOF"),
                    _tag(block, "CURSYM"),
                )
            )
    return quotes


def _record_quotes(
    sink: ImportSink,
    db: DbSQLite,
    quotes: list[tuple[str, str, str, str]],
    default_currency: str,
    number_format: NumberFormat | Literal["auto"],
) -> None:
    """Store each quote through the shared contract (``importer.quotes``)."""
    if number_format == "auto":
        try:
            detected = detect_number_format(price for _ticker, price, _when, _cur in quotes)
        except ValueError:
            detected = None
        chosen: NumberFormat = detected or "dot"
    else:
        chosen = number_format
    for ticker, raw_price, raw_date, currency_code in quotes:
        try:
            value = _parse_amount(raw_price, chosen)
            quote_date = _parse_date(raw_date)
        except ValueError:
            sink.result.skip(
                "OFX price has an unreadable amount or date",
                ticker or "(no symbol)",
                identity=f"ofx:{ticker}:{raw_date}",
                kind="price",
            )
            continue
        record_security_quote(
            sink,
            db,
            source="ofx",
            symbol=ticker,
            quote_date=quote_date,
            value=value,
            currency_code=currency_code or default_currency,
        )


def _import_investment(
    db: DbSQLite,
    source: Path,
    text: str,
    result: ImportResult,
    currency_code: str,
    institution: str,
    message: str | None,
    number_format: NumberFormat | Literal["auto"],
    notify: bool,
) -> ImportResult:
    """An investment statement: brokerage transactions, then security prices."""
    from .ofx_investment import import_investment_statement

    if number_format == "auto":
        amounts = re.findall(
            r"<(?:TOTAL|UNITS|UNITPRICE|TRNAMT|COMMISSION|FEES)>\s*([^<\r\n]+)",
            text,
            flags=re.IGNORECASE,
        )
        try:
            detected = detect_number_format(amounts)
        except ValueError as exc:
            result.warn(str(exc))
            return result
        chosen: NumberFormat = detected or "dot"
    else:
        chosen = number_format
    with db.transaction(message or f"Import {source.name}", batch=True, notify=notify) as txn:
        sink = ImportSink(db, txn, result)
        commodity = sink.commodity("CURRENCY", currency_code, currency_code)
        import_investment_statement(
            sink,
            db,
            text,
            tag=_tag,
            blocks=_blocks,
            parse_date=_parse_date,
            number_format=chosen,
            currency=commodity,
            broker=institution,
        )
        _record_quotes(sink, db, _security_quotes(text), currency_code, chosen)
        result.finish(db, txn)
    if notify:
        db.emit("database-changed", (db,))
    LOG.info("OFX investment import finished: %s", result.describe())
    return result


def import_book(
    db: DbSQLite,
    path: str | Path,
    include_scheduled: bool = True,
    message: str | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    number_format: NumberFormat | Literal["auto"] = "auto",
    notify: bool = True,
    include_duplicates: bool = False,
) -> ImportResult:
    """Import OFX/QFX bank and credit-card statement transactions."""
    del include_scheduled
    source = Path(path)
    text = _read_text(source)
    result = ImportResult(source=str(source), source_format="ofx")

    currency_code = _tag(text, "CURDEF", "USD") or "USD"
    institution = _tag(text, "ORG") or _tag(text, "FI")
    if _blocks(text, "INVACCTFROM"):
        return _import_investment(
            db, source, text, result, currency_code, institution, message, number_format, notify
        )
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
        quotes = _security_quotes(text)
        if quotes:
            # An investment statement with no bank account can still carry
            # security prices; they are imported on their own.
            with db.transaction(
                message or f"Import prices from {source.name}", batch=True, notify=notify
            ) as txn:
                sink = ImportSink(db, txn, result)
                _record_quotes(sink, db, quotes, currency_code, number_format)
                result.finish(db, txn)
            if notify:
                db.emit("database-changed", (db,))
            return result
        result.warn("No bank or credit-card account information was found")
        return result
    account_id = _tag(account_block, "ACCTID")
    transaction_blocks = _blocks(text, "STMTTRN")
    if number_format == "auto":
        try:
            detected_format = detect_number_format(
                _tag(block, "TRNAMT") for block in transaction_blocks
            )
        except ValueError as exc:
            result.warn(str(exc))
            return result
        if detected_format is None:
            detected_format = "dot"
            result.warn("OFX number format is ambiguous; assuming period decimal separator")
    else:
        detected_format = number_format

    def report(done: int) -> None:
        if progress is not None:
            progress("Reading OFX transactions", done, len(transaction_blocks))

    # This statement's own rows, so a re-imported row is never its own duplicate.
    family_counts: dict[tuple[object, ...], int] = {}
    family = {
        handle
        for block in transaction_blocks
        if (handle := _row_handle(block, account_id, family_counts, detected_format))
    }

    with db.transaction(message or f"Import {source.name}", batch=True, notify=notify) as txn:
        sink = ImportSink(db, txn, result)
        result.scan("transaction")
        commodity = sink.commodity("CURRENCY", currency_code, currency_code)
        statement_currency = db.get_commodity(commodity)
        fraction = statement_currency.fraction if statement_currency is not None else 100
        source_account = _source_account(sink, db, account_id, account_type, institution, commodity)
        guard = None if include_duplicates else DuplicateGuard(db, source_account, family)
        fallback_counts: dict[tuple[object, ...], int] = {}
        for index, block in enumerate(transaction_blocks, 1):
            report(index)
            try:
                post_date = _parse_date(_tag(block, "DTPOSTED"))
                amount = _parse_amount(_tag(block, "TRNAMT"), detected_format)
            except ValueError as exc:
                result.skip(
                    str(exc),
                    _tag(block, "NAME", "transaction"),
                    identity=_stable_handle("skipped", account_id, block),
                    kind="transaction",
                )
                continue
            name = _tag(block, "NAME")
            memo = _tag(block, "MEMO")
            description = name or memo or _tag(block, "TRNTYPE") or "OFX transaction"
            handle = _row_handle(block, account_id, fallback_counts, detected_format)
            assert handle is not None
            counter = _counter_account(sink, db, amount)
            rate_info = _transaction_rate(block)
            if rate_info is not None:
                code, raw_rate, foreign_amounts = rate_info
                rate = _parse_rate(raw_rate, detected_format)
                code = code.strip().upper()
                if foreign_amounts and code != currency_code.upper() and (rate is None or not code):
                    result.skip(
                        "OFX foreign-currency transaction has no usable exchange rate",
                        description,
                        identity=handle,
                        kind="transaction",
                    )
                    continue
                if rate is not None and code and code != currency_code.upper():
                    # The statement's own rate becomes dated price evidence,
                    # stored like a manual exchange rate (CURSYM priced in CURDEF).
                    sink.price(
                        _stable_handle("rate", code, currency_code, post_date.isoformat()),
                        sink.commodity("CURRENCY", code, code),
                        commodity,
                        post_date,
                        Money(rate),
                        source="ofx",
                        quote_type="transaction",
                    )
                    if foreign_amounts:
                        amount = (amount * rate).quantize(fraction)
            if guard is not None and db.get_transaction(handle) is None:
                if guard.claim(post_date, amount) is not None:
                    result.possible_duplicates += 1
                    result.skip(
                        "possible duplicate of a transaction already in this account on the "
                        "same date for the same amount",
                        description,
                        identity=handle,
                        kind="transaction",
                    )
                    continue
            splits = sink.keep_local_categories(
                handle,
                source_account,
                [
                    {
                        "account": source_account,
                        "value": amount,
                        "memo": memo,
                    },
                    {"account": counter, "value": -amount},
                ],
            )
            if splits is None:
                result.observe("transaction", handle)
                result.transactions_unchanged += 1
                continue
            sink.transaction(
                handle,
                post_date,
                description,
                currency_code,
                _tag(block, "CHECKNUM"),
                splits,
            )
        _record_quotes(sink, db, _security_quotes(text), currency_code, number_format)
        if progress is not None:
            progress("Finishing", len(transaction_blocks), len(transaction_blocks))
        result.finish(db, txn)
    if notify:
        db.emit("database-changed", (db,))
    LOG.info("OFX import finished: %s", result.describe())
    return result
