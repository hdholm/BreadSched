"""Import a bank or card statement exported as CSV, through a user's column mapping.

CSV has no standard layout, so nothing is guessed about which column means what:
the caller maps the date, the amount (or separate debit and credit columns), and
optionally the description and memo. Encoding, delimiter, date order, and decimal
convention are detected from the whole file unless chosen, and an ambiguous date
order is refused rather than assumed.

Each row receives a deterministic identity from the target account and its own
facts, with an occurrence counter for identical rows. Re-importing a row that is
already in the book leaves that transaction untouched, so a category the user
chose after the first import is never reverted. A row that matches a different
transaction in the target account on the same date and amount is held back as a
possible duplicate unless the caller explicitly includes it.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money
from ...gen.utils.amount_input import NumberFormat, detect_number_format, parse_decimal_amount
from ...gen.utils.logs import get_logger
from .gnucash_common import ImportResult, ImportSink

LOG = get_logger(__name__)

__all__ = [
    "CsvDateFormat",
    "CsvMapping",
    "CsvMappingError",
    "CsvPreview",
    "CsvRow",
    "import_rows",
    "read_statement",
]

CsvDateFormat = Literal["iso", "month-first", "day-first"]
RowStatus = Literal["new", "imported", "possible_duplicate", "invalid"]

_DELIMITERS = ",;\t|"


class CsvMappingError(ValueError):
    """A mapping or file-level problem; ``code`` names it for the service layer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CsvMapping:
    """Which columns hold which facts, by header name or 1-based position."""

    date: str
    amount: str | None = None
    debit: str | None = None
    credit: str | None = None
    description: str | None = None
    memo: str | None = None
    date_format: CsvDateFormat | Literal["auto"] = "auto"
    number_format: NumberFormat | Literal["auto"] = "auto"
    encoding: str = "auto"
    delimiter: str = "auto"
    header: bool = True
    #: Flip every sign, for exports that show money out as positive.
    invert: bool = False


@dataclass(frozen=True, slots=True)
class CsvRow:
    """One source line as it would be imported, or why it cannot be."""

    line: int
    when: date | None
    amount: Money | None
    description: str
    memo: str
    status: RowStatus
    reason: str = ""
    identity: str = ""
    #: The already-imported or possibly duplicated transaction, when there is one.
    existing: str | None = None


@dataclass(frozen=True, slots=True)
class CsvPreview:
    source: str
    account: str
    encoding: str
    delimiter: str
    date_format: CsvDateFormat
    number_format: NumberFormat
    columns: tuple[str, ...]
    rows: tuple[CsvRow, ...] = field(default=())

    def count(self, status: RowStatus) -> int:
        return sum(1 for row in self.rows if row.status == status)


def _stable_handle(kind: str, *parts: object) -> str:
    text = "|".join(str(part) for part in parts)
    return uuid5(NAMESPACE_URL, f"breadsched:csv:{kind}:{text}").hex


def _decode(raw: bytes, encoding: str) -> tuple[str, str]:
    if encoding != "auto":
        try:
            return raw.decode(encoding), encoding
        except (LookupError, UnicodeDecodeError) as exc:
            raise CsvMappingError(
                "import.csv.encoding.invalid", f"cannot read the file as {encoding}"
            ) from exc
    try:
        return raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        # Windows-1252 decodes every byte; it is the usual spreadsheet export.
        return raw.decode("cp1252"), "cp1252"


def _delimiter(text: str, chosen: str) -> str:
    if chosen != "auto":
        if len(chosen) != 1:
            raise CsvMappingError("import.csv.delimiter.invalid", "use a one-character delimiter")
        return chosen
    sample = "\n".join(text.splitlines()[:20])
    try:
        return csv.Sniffer().sniff(sample, delimiters=_DELIMITERS).delimiter
    except csv.Error:
        return ","


def _column(name: str | None, headers: Sequence[str], header: bool, field_name: str) -> int | None:
    if name is None or not name.strip():
        return None
    wanted = name.strip()
    if header:
        folded = [item.strip().casefold() for item in headers]
        if wanted.casefold() in folded:
            return folded.index(wanted.casefold())
    if wanted.isdigit() and 1 <= int(wanted) <= max(len(headers), 1):
        return int(wanted) - 1
    raise CsvMappingError(
        "import.csv.column.not_found", f"the {field_name} column {name!r} is not in the file"
    )


def _date_parts(raw: str) -> tuple[str, str, str]:
    text = raw.strip().replace(".", "/").replace("-", "/")
    parts = [part.strip() for part in text.split("/")]
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"unrecognised date {raw!r}")
    return parts[0], parts[1], parts[2]


def _detect_date_format(values: Sequence[str]) -> CsvDateFormat | None:
    evidence: set[CsvDateFormat] = set()
    for raw in values:
        try:
            first, second, _third = _date_parts(raw)
        except ValueError:
            continue
        if len(first) == 4:
            evidence.add("iso")
            continue
        first_value, second_value = int(first), int(second)
        if first_value > 12 >= second_value:
            evidence.add("day-first")
        elif second_value > 12 >= first_value:
            evidence.add("month-first")
    if "iso" in evidence and len(evidence) == 1:
        return "iso"
    evidence.discard("iso")
    if len(evidence) > 1:
        raise CsvMappingError(
            "import.csv.date_format.conflict", "the file mixes day-first and month-first dates"
        )
    return next(iter(evidence), None)


def _parse_date(raw: str, date_format: CsvDateFormat) -> date:
    first, second, third = _date_parts(raw)
    if len(first) == 4:
        year, month, day = int(first), int(second), int(third)
    else:
        if date_format == "day-first":
            day, month = int(first), int(second)
        else:
            month, day = int(first), int(second)
        year = int(third)
        if year < 100:
            year += 2000 if year < 70 else 1900
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError(f"unrecognised date {raw!r}") from exc


def read_statement(db: DbSQLite, path: str | Path, account: str, mapping: CsvMapping) -> CsvPreview:
    """Parse and classify every row against the book, without writing anything."""
    if mapping.amount and (mapping.debit or mapping.credit):
        raise CsvMappingError(
            "import.csv.amount.mapping", "map either one amount column or debit/credit columns"
        )
    if not mapping.amount and not (mapping.debit or mapping.credit):
        raise CsvMappingError(
            "import.csv.amount.mapping", "map an amount column or debit/credit columns"
        )
    source = Path(path)
    text, encoding = _decode(source.read_bytes(), mapping.encoding)
    # Windows and classic Mac exports end lines, including those inside quoted
    # fields, with CR LF or CR; normalize so a row reads (and is identified) the
    # same whichever platform wrote the file.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    delimiter = _delimiter(text, mapping.delimiter)
    records = [row for row in csv.reader(text.splitlines(keepends=True), delimiter=delimiter)]
    first_line = 1
    headers: list[str] = []
    if mapping.header:
        headers = records[0] if records else []
        records = records[1:]
        first_line = 2
    width = max((len(row) for row in records), default=len(headers))
    labels = headers or [str(index) for index in range(1, width + 1)]
    date_col = _column(mapping.date, labels, mapping.header, "date")
    amount_col = _column(mapping.amount, labels, mapping.header, "amount")
    debit_col = _column(mapping.debit, labels, mapping.header, "debit")
    credit_col = _column(mapping.credit, labels, mapping.header, "credit")
    description_col = _column(mapping.description, labels, mapping.header, "description")
    memo_col = _column(mapping.memo, labels, mapping.header, "memo")
    assert date_col is not None

    def cell(row: Sequence[str], index: int | None) -> str:
        return row[index].strip() if index is not None and index < len(row) else ""

    numbered = [
        (first_line + offset, row)
        for offset, row in enumerate(records)
        if any(value.strip() for value in row)
    ]
    date_format: CsvDateFormat | None
    if mapping.date_format == "auto":
        date_format = _detect_date_format([cell(row, date_col) for _line, row in numbered])
        if date_format is None and numbered:
            raise CsvMappingError(
                "import.csv.date_format.ambiguous",
                "every date could be day-first or month-first; choose the date order",
            )
    else:
        date_format = mapping.date_format
    amount_cells = [
        cell(row, index)
        for _line, row in numbered
        for index in (amount_col, debit_col, credit_col)
        if index is not None
    ]
    number_format: NumberFormat | None
    if mapping.number_format == "auto":
        try:
            number_format = detect_number_format(amount_cells)
        except ValueError as exc:
            raise CsvMappingError("import.csv.number_format.conflict", str(exc)) from exc
    else:
        number_format = mapping.number_format
    date_format = date_format or "iso"
    number_format = number_format or "dot"

    parsed: list[tuple[int, date | None, Money | None, str, str, str]] = []
    for line, row in numbered:
        description = cell(row, description_col)
        memo = cell(row, memo_col)
        try:
            when: date | None = _parse_date(cell(row, date_col), date_format)
        except ValueError as exc:
            parsed.append((line, None, None, description, memo, f"bad date: {exc}"))
            continue
        try:
            amount = _row_amount(row, cell, amount_col, debit_col, credit_col, number_format)
        except ValueError as exc:
            parsed.append((line, when, None, description, memo, f"bad amount: {exc}"))
            continue
        parsed.append((line, when, -amount if mapping.invert else amount, description, memo, ""))

    # Identical rows are distinct occurrences; all occurrences of one row form a
    # family that must not be mistaken for a duplicate of itself.
    totals: dict[tuple[object, ...], int] = {}
    for _line, parsed_when, parsed_amount, description, memo, reason in parsed:
        if not reason and parsed_when is not None:
            facts = (account, parsed_when.isoformat(), str(parsed_amount), description, memo)
            totals[facts] = totals.get(facts, 0) + 1
    family = {
        _stable_handle("transaction", *facts, occurrence)
        for facts, count in totals.items()
        for occurrence in range(1, count + 1)
    }

    existing_by_key = _existing_in_account(db, account)
    occurrences: dict[tuple[object, ...], int] = {}
    rows: list[CsvRow] = []
    for line, row_when, row_amount, description, memo, reason in parsed:
        if reason or row_when is None or row_amount is None:
            rows.append(CsvRow(line, row_when, None, description, memo, "invalid", reason))
            continue
        when, amount = row_when, row_amount
        facts = (account, when.isoformat(), str(amount), description, memo)
        occurrence = occurrences.get(facts, 0) + 1
        occurrences[facts] = occurrence
        identity = _stable_handle("transaction", *facts, occurrence)
        if db.get_transaction(identity) is not None:
            rows.append(
                CsvRow(line, when, amount, description, memo, "imported", "", identity, identity)
            )
            continue
        others = [
            handle for handle in existing_by_key.get((when, amount), []) if handle not in family
        ]
        if others:
            rows.append(
                CsvRow(
                    line,
                    when,
                    amount,
                    description,
                    memo,
                    "possible_duplicate",
                    "same date and amount as a transaction already in this account",
                    identity,
                    others[0],
                )
            )
            continue
        rows.append(CsvRow(line, when, amount, description, memo, "new", "", identity))
    return CsvPreview(
        str(source),
        account,
        encoding,
        delimiter,
        date_format,
        number_format,
        tuple(labels),
        tuple(rows),
    )


def _row_amount(row, cell, amount_col, debit_col, credit_col, number_format) -> Money:
    if amount_col is not None:
        text = cell(row, amount_col)
        if not text:
            raise ValueError("the amount is empty")
        return Money(parse_decimal_amount(text, number_format))
    debit_text, credit_text = cell(row, debit_col), cell(row, credit_col)
    if debit_text and credit_text:
        raise ValueError("both debit and credit are filled")
    if not debit_text and not credit_text:
        raise ValueError("neither debit nor credit is filled")
    if debit_text:
        return -abs(Money(parse_decimal_amount(debit_text, number_format)))
    return abs(Money(parse_decimal_amount(credit_text, number_format)))


def _existing_in_account(db: DbSQLite, account: str) -> dict[tuple[date, Money], list[str]]:
    found: dict[tuple[date, Money], list[str]] = {}
    for transaction in db.iter_transactions():
        for split in transaction.splits:
            if split.account == account:
                found.setdefault((transaction.post_date, split.value), []).append(
                    transaction.handle
                )
    return found


def _counter_account(sink: ImportSink, db: DbSQLite, amount: Money) -> str:
    root_name, atype = ("Expenses", "EXPENSE") if amount < 0 else ("Income", "INCOME")
    top = db.get_account_by_name(root_name)
    parent = top.handle if top is not None else None
    guid = _stable_handle("category", atype, "Uncategorized CSV")
    return sink.account(guid, "Uncategorized CSV", atype, parent).handle


def import_rows(
    db: DbSQLite,
    preview: CsvPreview,
    *,
    include_duplicates: bool = False,
    message: str | None = None,
) -> ImportResult:
    """Write the preview's new rows (and, if chosen, possible duplicates) in one batch."""
    source = Path(preview.source)
    result = ImportResult(source=str(source), source_format="csv")
    accepted = {"new", "possible_duplicate"} if include_duplicates else {"new"}
    with db.transaction(message or f"Import {source.name}", batch=True) as txn:
        sink = ImportSink(db, txn, result)
        result.scan("transaction")
        for row in preview.rows:
            subject = f"line {row.line}"
            if row.status == "invalid":
                result.skip(row.reason, subject, identity=f"line:{row.line}", kind="transaction")
                continue
            if row.status == "imported":
                # Never rewrite an accepted row: the user may have recategorized it.
                result.observe("transaction", row.identity)
                result.transactions_unchanged += 1
                continue
            if row.status not in accepted:
                result.skip(
                    "possible duplicate not included",
                    subject,
                    identity=row.identity,
                    kind="transaction",
                )
                continue
            assert row.when is not None and row.amount is not None
            counter = _counter_account(sink, db, row.amount)
            sink.transaction(
                row.identity,
                row.when,
                row.description or row.memo or "CSV transaction",
                None,
                "",
                [
                    {"account": preview.account, "value": row.amount, "memo": row.memo},
                    {"account": counter, "value": -row.amount},
                ],
            )
        result.finish(db, txn)
    db.emit("database-changed", (db,))
    LOG.info("CSV import finished: %s", result.describe())
    return result
