"""Import Quicken Interchange Format (QIF) account transactions.

The parser intentionally uses only the Python standard library.  QIF has no stable
record identifiers, so deterministic UUID5 handles are derived from account names
and transaction content.  Re-importing the same export therefore updates/adopts the
same objects as far as the source format permits.
"""

from __future__ import annotations

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


def _parse_date(raw: str) -> date:
    text = raw.strip().replace("'", "/").replace("-", "/")
    parts = [part.strip() for part in text.split("/") if part.strip()]
    if len(parts) != 3:
        raise ValueError(f"unrecognised QIF date {raw!r}")
    first, second, third = parts
    if len(first) == 4:
        year, month, day = int(first), int(second), int(third)
    else:
        month, day, year = int(first), int(second), int(third)
        if year < 100:
            year += 2000 if year < 70 else 1900
    return date(year, month, day)


def _parse_amount(raw: str) -> Money:
    text = raw.strip().replace(",", "")
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        return Money(Decimal(text))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"unrecognised QIF amount {raw!r}") from exc


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
) -> str:
    category = label.split("/", 1)[0].strip()
    if category.startswith("[") and category.endswith("]"):
        target = category[1:-1].strip()
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


def import_book(
    db: DbSQLite,
    path: str | Path,
    include_scheduled: bool = True,
    message: str | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> ImportResult:
    """Import bank/cash/credit-card QIF accounts and transactions.

    Investment transaction sections are reported and skipped for now rather than
    guessed into ordinary cash transactions.
    """
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
    with db.transaction(message or f"Import {source.name}", batch=True) as txn:
        sink = ImportSink(db, txn, result)
        done = 0
        for record in records:
            if not record:
                continue
            first = record[0]
            if first == "!Account":
                fields = {line[0]: line[1:] for line in record[1:] if line}
                current_name = fields.get("N", current_name).strip() or current_name
                current_type = fields.get("T", current_type).strip() or current_type
                _ensure_source_account(sink, db, current_name, current_type)
                continue
            if first.startswith("!Type:"):
                section_type = first.partition(":")[2].strip()
                current_type = section_type or current_type
                if section_type.casefold() == "invst":
                    result.warn("QIF investment transactions are not imported yet")
                continue
            if first.startswith("!"):
                continue
            done += 1
            report("Reading QIF transactions", done)
            if section_type.casefold() == "invst":
                result.skip("QIF investment transaction support is not implemented", first)
                continue
            fields, split_rows = _transaction_fields(record)
            try:
                post_date = _parse_date(fields.get("D", ""))
                amount = _parse_amount(fields.get("T", ""))
            except ValueError as exc:
                result.skip(str(exc), fields.get("P", fields.get("M", "transaction")))
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
                        split_amount = _parse_amount(item.get("amount", ""))
                    except ValueError as exc:
                        result.skip(str(exc), fields.get("P", "transaction"))
                        raw_splits = []
                        break
                    target = _category_handle(
                        sink, db, item.get("category", "Uncategorized"), split_amount
                    )
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
                target = _category_handle(
                    sink, db, fields.get("L", "Uncategorized"), amount
                )
                raw_splits.append({"account": target, "value": -amount})
            identity = (
                current_name, post_date.isoformat(), str(amount), fields.get("P", ""),
                fields.get("M", ""), fields.get("N", ""), done,
            )
            sink.transaction(
                _stable_handle("transaction", *identity),
                post_date,
                fields.get("P", "").strip() or fields.get("M", "").strip() or "QIF transaction",
                None,
                fields.get("N", ""),
                raw_splits,
            )
        report("Finishing", done)
    db.emit("database-changed", (db,))
    LOG.info("QIF import finished: %s", result.describe())
    return result
