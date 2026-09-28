"""Import Quicken Interchange Format (QIF) account transactions.

The parser intentionally uses only the Python standard library.  QIF has no stable
record identifiers, so deterministic UUID5 handles are derived from account names
and transaction content.  Re-importing the same export therefore updates/adopts the
same objects as far as the source format permits.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from datetime import date
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
from .gnucash_common import ImportResult, ImportSink
from .quotes import record_security_quote

LOG = get_logger(__name__)

__all__ = ["import_book", "sniff"]


QifDateFormat = Literal["month-first", "day-first"]


_INVESTMENT_TYPES = {"invst", "port", "401(k)/403(b)"}

_QIF_TYPES = {
    "bank": "BANK",
    "cash": "CASH",
    "ccard": "CREDIT",
    "credit card": "CREDIT",
}


def sniff(path: str | Path) -> bool:
    """Return True when *path* looks like a QIF text file."""
    try:
        head = Path(path).read_text(encoding="utf-8-sig", errors="replace")[:4096]
    except OSError:
        return False
    return any(line.startswith(("!Type:", "!Account")) for line in head.splitlines())


def _stable_handle(kind: str, *parts: object) -> str:
    text = "|".join(str(part) for part in parts)
    return uuid5(NAMESPACE_URL, f"breadsched:qif:{kind}:{text}").hex


def _date_parts(raw: str) -> tuple[str, str, str]:
    text = raw.strip().replace("'", "/").replace("-", "/")
    parts = [part.strip() for part in text.split("/") if part.strip()]
    if len(parts) != 3:
        raise ValueError(f"unrecognised QIF date {raw!r}")
    return parts[0], parts[1], parts[2]


def _detect_date_format(values: list[str]) -> QifDateFormat | None:
    evidence: set[QifDateFormat] = set()
    for raw in values:
        first, second, _third = _date_parts(raw)
        if len(first) == 4:
            continue
        first_value = int(first)
        second_value = int(second)
        if first_value > 12 and second_value <= 12:
            evidence.add("day-first")
        elif second_value > 12 and first_value <= 12:
            evidence.add("month-first")
        elif first_value > 12 and second_value > 12:
            raise ValueError(f"unrecognised QIF date {raw!r}")
    if len(evidence) > 1:
        raise ValueError("QIF source contains conflicting date orders")
    return next(iter(evidence), None)


def _parse_date(raw: str, date_format: QifDateFormat) -> date:
    first, second, third = _date_parts(raw)
    if len(first) == 4:
        year, month, day = int(first), int(second), int(third)
    else:
        if date_format == "month-first":
            month, day = int(first), int(second)
        else:
            day, month = int(first), int(second)
        year = int(third)
        if year < 100:
            year += 2000 if year < 70 else 1900
    return date(year, month, day)


def _parse_amount(raw: str, number_format: NumberFormat) -> Money:
    try:
        return Money(parse_decimal_amount(raw, number_format))
    except ValueError as exc:
        raise ValueError(f"unrecognised QIF amount {raw!r}") from exc


def _date_texts(records: list[list[str]]) -> list[str]:
    values: list[str] = []
    for record in records:
        if not record or record[0].startswith("!"):
            continue
        fields, _split_rows = _transaction_fields(record)
        if fields.get("D"):
            values.append(fields["D"])
    return values


def _amount_texts(records: list[list[str]]) -> list[str]:
    values: list[str] = []
    for record in records:
        if not record or record[0].startswith("!"):
            continue
        fields, split_rows = _transaction_fields(record)
        if fields.get("T"):
            values.append(fields["T"])
        values.extend(item["amount"] for item in split_rows if item.get("amount"))
    return values


def _records(lines: list[str]):
    """Yield QIF directives and caret-terminated records in source order."""
    index = 0
    while index < len(lines):
        line = lines[index].rstrip("\r\n")
        if not line:
            index += 1
            continue
        if line == "!Account":
            record = [line]
            index += 1
            while index < len(lines):
                item = lines[index].rstrip("\r\n")
                index += 1
                if item == "^":
                    break
                if item:
                    record.append(item)
            yield record
            continue
        if line.startswith("!"):
            yield [line]
            index += 1
            continue
        record = []
        while index < len(lines):
            item = lines[index].rstrip("\r\n")
            index += 1
            if item == "^":
                break
            if item:
                record.append(item)
        if record:
            yield record


def _top_level(db: DbSQLite, name: str) -> str | None:
    account = db.get_account_by_name(name)
    return account.handle if account is not None else None


def _ensure_path(
    sink: ImportSink,
    parent: str | None,
    path: str,
    atype: str,
    namespace: str,
) -> str:
    current = parent
    pieces = [part.strip() for part in path.split(":") if part.strip()]
    for index, piece in enumerate(pieces):
        leaf = index == len(pieces) - 1
        item_type = atype if leaf else ("EXPENSE" if atype == "EXPENSE" else "INCOME")
        guid = _stable_handle("account", namespace, ":".join(pieces[: index + 1]), item_type)
        account = sink.account(guid, piece, item_type, current)
        current = account.handle
    if current is None:
        raise ValueError("empty QIF account/category name")
    return current


def _ensure_source_account(
    sink: ImportSink,
    db: DbSQLite,
    name: str,
    qif_type: str,
) -> str:
    atype = _QIF_TYPES.get(qif_type.casefold(), "BANK")
    parent_name = "Liabilities" if atype == "CREDIT" else "Assets"
    parent = _top_level(db, parent_name)
    guid = _stable_handle("account", "source", name, atype)
    return sink.account(guid, name, atype, parent).handle


def _category_handle(
    sink: ImportSink,
    db: DbSQLite,
    label: str,
    amount: Money,
    transfer: Callable[[str], str | None] | None = None,
) -> str:
    """A category (negative ``amount`` is an expense) or a ``[Account]`` transfer.

    ``transfer`` resolves a transfer to an investment account's cash instead of
    creating a bank account of the same name.
    """
    category = label.split("/", 1)[0].strip()
    if category.startswith("[") and category.endswith("]"):
        target = category[1:-1].strip()
        resolved = transfer(target) if transfer is not None else None
        if resolved is not None:
            return resolved
        return _ensure_source_account(sink, db, target, "Bank")
    atype = "EXPENSE" if amount < 0 else "INCOME"
    parent = _top_level(db, "Expenses" if atype == "EXPENSE" else "Income")
    return _ensure_path(sink, parent, category or "Uncategorized", atype, "category")


def _transaction_fields(record: list[str]) -> tuple[dict[str, str], list[dict[str, str]]]:
    fields: dict[str, str] = {}
    split_rows: list[dict[str, str]] = []
    current_split: dict[str, str] | None = None
    for line in record:
        code, value = line[0], line[1:]
        if code == "S":
            current_split = {"category": value}
            split_rows.append(current_split)
        elif code == "E" and current_split is not None:
            current_split["memo"] = value
        elif code == "$" and current_split is not None:
            current_split["amount"] = value
        else:
            fields[code] = value
    return fields, split_rows


def _import_prices(
    sink: ImportSink,
    db: DbSQLite,
    record: list[str],
    number_format: NumberFormat,
    date_format: QifDateFormat,
) -> None:
    """Read ``"SYMBOL",price,"date"`` lines from a ``!Type:Prices`` section.

    QIF names no currency, so a quote is taken to be in the book's reporting
    currency; everything else goes through the shared quote contract.
    """
    from ...gen.engine.currency import reporting_currency_handle

    currency = reporting_currency_handle(db)
    for row in csv.reader(record, skipinitialspace=True):
        if len(row) < 3:
            sink.result.skip("QIF price line is incomplete", ",".join(row), kind="price")
            continue
        symbol, raw_price, raw_date = (item.strip() for item in row[:3])
        try:
            value = _parse_amount(raw_price, number_format)
            quote_date = _parse_date(raw_date, date_format)
        except ValueError:
            sink.result.skip(
                "QIF price has an unreadable amount or date",
                symbol or "(no symbol)",
                identity=f"qif:{symbol}:{raw_date}",
                kind="price",
            )
            continue
        record_security_quote(
            sink,
            db,
            source="qif",
            symbol=symbol,
            quote_date=quote_date,
            value=value,
            currency=currency,
        )


def import_book(
    db: DbSQLite,
    path: str | Path,
    include_scheduled: bool = True,
    message: str | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    number_format: NumberFormat | Literal["auto"] = "auto",
    date_format: QifDateFormat | Literal["auto"] = "auto",
    notify: bool = True,
) -> ImportResult:
    """Import QIF bank, cash, credit-card, and investment accounts and transactions.

    Investment records (``!Type:Invst``) and the security list go through
    :mod:`.qif_investment`; prices through the shared quote contract.
    """
    from .qif_investment import QifInvestments

    del include_scheduled
    source = Path(path)
    result = ImportResult(source=str(source), source_format="qif")
    lines = source.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    total_records = lines.count("^")

    def report(stage: str, done: int) -> None:
        if progress is not None:
            progress(stage, done, total_records)

    current_name = source.stem
    current_type = "Bank"
    section_type = "Bank"
    records = list(_records(lines))
    if number_format == "auto":
        try:
            detected_format = detect_number_format(_amount_texts(records))
        except ValueError as exc:
            result.warn(str(exc))
            return result
        if detected_format is None:
            detected_format = "dot"
            result.warn("QIF number format is ambiguous; assuming period decimal separator")
    else:
        detected_format = number_format
    if date_format == "auto":
        try:
            detected_date_format = _detect_date_format(_date_texts(records))
        except ValueError as exc:
            result.warn(str(exc))
            return result
        if detected_date_format is None:
            detected_date_format = "month-first"
            result.warn("QIF date order is ambiguous; assuming month/day/year")
    else:
        detected_date_format = date_format
    investment_names: set[str] = set()
    securities: list[list[str]] = []
    scan_section = ""
    for record in records:
        if record and record[0] == "!Account":
            fields = {line[0]: line[1:].strip() for line in record[1:] if line}
            if fields.get("T", "").casefold() in _INVESTMENT_TYPES and fields.get("N"):
                investment_names.add(fields["N"].casefold())
        elif record and record[0].startswith("!Type:"):
            scan_section = record[0].partition(":")[2].strip().casefold()
        elif record and not record[0].startswith("!") and scan_section == "security":
            securities.append(record)
    with db.transaction(message or f"Import {source.name}", batch=True, notify=notify) as txn:
        sink = ImportSink(db, txn, result)
        result.scan("transaction")

        def transfer(name: str) -> str | None:
            if name.casefold() in investment_names:
                return investments.brokerage(name).cash
            return None

        def category(label: str, amount: Money) -> str:
            return _category_handle(sink, db, label, amount, transfer)

        investments = QifInvestments(
            sink,
            db,
            handle=_stable_handle,
            category=category,
            parse_amount=lambda raw: _parse_amount(raw, detected_format),
            parse_date=lambda raw: _parse_date(raw, detected_date_format),
        )
        for record in securities:
            investments.learn_security(record)
        done = 0
        identity_counts: dict[tuple[object, ...], int] = {}
        for record in records:
            if not record:
                continue
            first = record[0]
            if first == "!Account":
                fields = {line[0]: line[1:] for line in record[1:] if line}
                current_name = fields.get("N", current_name).strip() or current_name
                current_type = fields.get("T", current_type).strip() or current_type
                if current_type.casefold() in _INVESTMENT_TYPES:
                    investments.brokerage(current_name)
                else:
                    _ensure_source_account(sink, db, current_name, current_type)
                continue
            if first.startswith("!Type:"):
                section_type = first.partition(":")[2].strip()
                if section_type.casefold() not in {"security", "prices"}:
                    current_type = section_type or current_type
                continue
            if first.startswith("!"):
                continue
            done += 1
            report("Reading QIF transactions", done)
            if section_type.casefold() == "prices":
                _import_prices(sink, db, record, detected_format, detected_date_format)
                continue
            if section_type.casefold() == "security":
                continue
            if section_type.casefold() == "invst":
                investment_names.add(current_name.casefold())
                investments.record(current_name, record)
                continue
            fields, split_rows = _transaction_fields(record)
            try:
                post_date = _parse_date(fields.get("D", ""), detected_date_format)
                amount = _parse_amount(fields.get("T", ""), detected_format)
            except ValueError as exc:
                result.skip(
                    str(exc),
                    fields.get("P", fields.get("M", "transaction")),
                    identity=_stable_handle("skipped", *record),
                    kind="transaction",
                )
                continue
            source_account = _ensure_source_account(sink, db, current_name, current_type)
            raw_splits: list[dict] = [
                {
                    "account": source_account,
                    "value": amount,
                    "memo": fields.get("M", ""),
                    "reconcile": fields.get("C", ""),
                }
            ]
            if split_rows:
                for item in split_rows:
                    try:
                        split_amount = _parse_amount(item.get("amount", ""), detected_format)
                    except ValueError as exc:
                        result.skip(
                            str(exc),
                            fields.get("P", "transaction"),
                            identity=_stable_handle("skipped", *record),
                            kind="transaction",
                        )
                        raw_splits = []
                        break
                    target = category(item.get("category", "Uncategorized"), split_amount)
                    raw_splits.append(
                        {
                            "account": target,
                            "value": -split_amount,
                            "memo": item.get("memo", ""),
                        }
                    )
                if not raw_splits:
                    continue
            else:
                target = category(fields.get("L", "Uncategorized"), amount)
                raw_splits.append({"account": target, "value": -amount})
            split_identity = tuple(
                (item["account"], str(item["value"]), item.get("memo", "")) for item in raw_splits
            )
            identity = (
                current_name,
                post_date.isoformat(),
                str(amount),
                fields.get("P", ""),
                fields.get("M", ""),
                fields.get("N", ""),
                split_identity,
            )
            occurrence = identity_counts.get(identity, 0) + 1
            identity_counts[identity] = occurrence
            handle = _stable_handle("transaction", *identity, occurrence)
            kept = sink.keep_local_categories(handle, source_account, raw_splits)
            if kept is None:
                result.observe("transaction", handle)
                result.transactions_unchanged += 1
                continue
            sink.transaction(
                handle,
                post_date,
                fields.get("P", "").strip() or fields.get("M", "").strip() or "QIF transaction",
                None,
                fields.get("N", ""),
                kept,
            )
        report("Finishing", done)
        result.finish(db, txn)
    if notify:
        db.emit("database-changed", (db,))
    LOG.info("QIF import finished: %s", result.describe())
    return result
