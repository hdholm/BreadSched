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

A transfer between two of the user's accounts appears on both statements. A row
whose opposite amount was already imported into another asset or liability account
within a few days, against an uncategorized placeholder, is offered as that
transfer's other side. Only an explicit link replaces the placeholder with the
target account; the linked leg carries the row's identity as its split handle, so
re-importing the row recognizes it. A transaction the user has categorized is never
offered.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from ...gen.db.base import DbTxn
from ...gen.db.sqlite import DbSQLite
from ...gen.engine.categorization import placeholder_handles
from ...gen.engine.currency import reporting_currency_handle
from ...gen.lib.account import AccountClass
from ...gen.lib.money import Money
from ...gen.lib.transaction import Split
from ...gen.utils.amount_input import NumberFormat, detect_number_format, parse_decimal_amount
from ...gen.utils.logs import get_logger
from .gnucash_common import ImportResult, ImportSink

LOG = get_logger(__name__)

__all__ = [
    "CsvDateFormat",
    "CsvInspection",
    "CsvMapping",
    "CsvMappingError",
    "CsvPreview",
    "CsvRow",
    "import_rows",
    "inspect_statement",
    "placeholder_handles",
    "read_statement",
]

CsvDateFormat = Literal["iso", "month-first", "day-first"]
RowStatus = Literal["new", "imported", "possible_duplicate", "possible_transfer", "invalid"]

_DELIMITERS = ",;\t|"
#: How far apart two statements may date the two sides of one transfer.
TRANSFER_WINDOW_DAYS = 3


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
    #: An account for the other side, by full name or unique account name.
    category: str | None = None
    #: A payee already in the book, by name or description key.
    payee: str | None = None
    #: A currency code that must match the target account's currency.
    currency: str | None = None
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
    #: The already-imported, possibly duplicated, or transfer-side transaction.
    existing: str | None = None
    #: The mapped category account; ``None`` posts to Uncategorized CSV.
    category: str | None = None
    #: The mapped payee.
    payee: str | None = None
    #: A note that does not stop the row, such as an unknown payee.
    note: str = ""


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


def _split_handle(identity: str) -> str:
    return _stable_handle("split", identity)


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


def _load_records(source: Path, encoding: str, delimiter: str) -> tuple[list[list[str]], str, str]:
    text, chosen_encoding = _decode(source.read_bytes(), encoding)
    # Windows and classic Mac exports end lines, including those inside quoted
    # fields, with CR LF or CR; normalize so a row reads (and is identified) the
    # same whichever platform wrote the file.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    chosen_delimiter = _delimiter(text, delimiter)
    records = list(csv.reader(text.splitlines(keepends=True), delimiter=chosen_delimiter))
    return records, chosen_encoding, chosen_delimiter


@dataclass(frozen=True, slots=True)
class CsvInspection:
    """Detected layout and the first rows, for choosing a column mapping."""

    encoding: str
    delimiter: str
    columns: tuple[str, ...]
    sample: tuple[tuple[str, ...], ...]


def inspect_statement(
    path: str | Path,
    *,
    encoding: str = "auto",
    delimiter: str = "auto",
    header: bool = True,
    limit: int = 5,
) -> CsvInspection:
    """Read only the layout: detected encoding and delimiter, columns, and sample rows."""
    records, chosen_encoding, chosen_delimiter = _load_records(Path(path), encoding, delimiter)
    records = [row for row in records if any(value.strip() for value in row)]
    headers = records[0] if header and records else []
    body = records[1:] if header else records
    width = max((len(row) for row in body), default=len(headers))
    columns = tuple(item.strip() for item in headers) or tuple(
        str(index) for index in range(1, width + 1)
    )
    return CsvInspection(
        chosen_encoding,
        chosen_delimiter,
        columns,
        tuple(tuple(row) for row in body[:limit]),
    )


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
    records, encoding, delimiter = _load_records(source, mapping.encoding, mapping.delimiter)
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
    category_col = _column(mapping.category, labels, mapping.header, "category")
    payee_col = _column(mapping.payee, labels, mapping.header, "payee")
    currency_col = _column(mapping.currency, labels, mapping.header, "currency")
    assert date_col is not None
    resolve = _Resolver(db, account)

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

    rows_by_line = dict(numbered)
    existing_by_key, linked, candidates = _index_book(db, account)
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
        imported = identity if db.get_transaction(identity) is not None else None
        imported = imported or linked.get(_split_handle(identity))
        if imported is not None:
            rows.append(
                CsvRow(line, when, amount, description, memo, "imported", "", identity, imported)
            )
            continue
        source_row = rows_by_line[line]
        problem = resolve.currency(cell(source_row, currency_col))
        category, category_problem = resolve.category(cell(source_row, category_col))
        problem = problem or category_problem
        if problem:
            rows.append(CsvRow(line, when, amount, description, memo, "invalid", problem))
            continue
        payee, note = resolve.payee(cell(source_row, payee_col))
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
                    category=category,
                    payee=payee,
                    note=note,
                )
            )
            continue
        rows.append(
            CsvRow(
                line,
                when,
                amount,
                description,
                memo,
                "new",
                "",
                identity,
                category=category,
                payee=payee,
                note=note,
            )
        )
    rows = _offer_transfers(rows, candidates)
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


class _Resolver:
    """Map optional category, payee, and currency cells to what the book has.

    Nothing is invented: a category must name one existing account (its full name,
    or a name no other account shares), and a currency must be the target
    account's. Those problems stop the row. An unknown payee only leaves the row
    without one.
    """

    def __init__(self, db: DbSQLite, account: str) -> None:
        from ...gen.engine.payees import match_key, payee_index

        self._match_key = match_key
        self._by_full: dict[str, str] = {}
        self._by_name: dict[str, list[str]] = {}
        for item in db.iter_accounts():
            if item.is_root or item.placeholder or item.handle == account:
                continue
            self._by_full[db.full_name(item).casefold()] = item.handle
            self._by_name.setdefault(item.name.casefold(), []).append(item.handle)
        payees = list(db.iter_payees())
        self._payee_names = {payee.name.casefold(): payee.handle for payee in payees}
        self._payee_keys = {key: payee.handle for key, payee in payee_index(payees).items()}
        target = db.get_account(account)
        commodity = db.get_commodity(target.commodity) if target and target.commodity else None
        if commodity is None or not commodity.is_currency:
            commodity = db.get_commodity(reporting_currency_handle(db))
        self._currency = commodity.mnemonic if commodity is not None else ""

    def currency(self, text: str) -> str:
        if text and self._currency and text.strip().upper() != self._currency.upper():
            return f"the row is in {text.strip()}, but the account is in {self._currency}"
        return ""

    def category(self, text: str) -> tuple[str | None, str]:
        if not text:
            return None, ""
        wanted = text.strip().casefold()
        if wanted in self._by_full:
            return self._by_full[wanted], ""
        matches = self._by_name.get(wanted, [])
        if len(matches) == 1:
            return matches[0], ""
        if matches:
            return None, f"category {text!r} matches several accounts; use its full name"
        return None, f"category {text!r} is not an account in the book"

    def payee(self, text: str) -> tuple[str | None, str]:
        if not text:
            return None, ""
        found = self._payee_names.get(text.strip().casefold()) or self._payee_keys.get(
            self._match_key(text)
        )
        return (found, "") if found else (None, f"payee {text!r} is not in the book")


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


@dataclass(frozen=True, slots=True)
class _TransferSide:
    transaction: str
    when: date
    #: The value a row in the target account must have to complete the transfer.
    amount: Money
    account: str


def _index_book(
    db: DbSQLite, account: str
) -> tuple[dict[tuple[date, Money], list[str]], dict[str, str], list[_TransferSide]]:
    """One pass: target-account activity, linked legs, and open transfer sides."""
    found: dict[tuple[date, Money], list[str]] = {}
    linked: dict[str, str] = {}
    candidates: list[_TransferSide] = []
    placeholders = {handle for _kind, handle in placeholder_handles()}
    reporting = reporting_currency_handle(db)
    for transaction in db.iter_transactions():
        in_target = False
        for split in transaction.splits:
            if split.account == account:
                in_target = True
                linked[split.handle] = transaction.handle
                found.setdefault((transaction.post_date, split.value), []).append(
                    transaction.handle
                )
        if in_target or len(transaction.splits) != 2:
            continue
        if (transaction.currency or reporting) != reporting:
            continue
        guess = [split for split in transaction.splits if split.account in placeholders]
        other = [split for split in transaction.splits if split.account not in placeholders]
        if len(guess) != 1 or len(other) != 1:
            continue
        source = db.get_account(other[0].account)
        if source is None or source.account_class not in (
            AccountClass.ASSET,
            AccountClass.LIABILITY,
        ):
            continue
        candidates.append(
            _TransferSide(transaction.handle, transaction.post_date, guess[0].value, source.name)
        )
    return found, linked, candidates


def _offer_transfers(rows: list[CsvRow], candidates: list[_TransferSide]) -> list[CsvRow]:
    """Pair new rows with open transfer sides, nearest date first, one to one."""
    pairs = sorted(
        (abs((row.when - side.when).days), index, side.when, side.transaction, side)
        for index, row in enumerate(rows)
        # A row with a mapped category already says where its money went.
        if row.status == "new" and row.when is not None and row.category is None
        for side in candidates
        if side.amount == row.amount and abs((row.when - side.when).days) <= TRANSFER_WINDOW_DAYS
    )
    taken_rows: set[int] = set()
    taken_sides: set[str] = set()
    offered = list(rows)
    for _distance, index, _when, handle, side in pairs:
        if index in taken_rows or handle in taken_sides:
            continue
        taken_rows.add(index)
        taken_sides.add(handle)
        row = rows[index]
        offered[index] = replace(
            row,
            status="possible_transfer",
            reason=(
                f"other side of an uncategorized {(-side.amount).format()} in "
                f"{side.account} on {side.when.isoformat()}"
            ),
            existing=handle,
        )
    return offered


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
    link_transfers: bool = False,
    message: str | None = None,
) -> ImportResult:
    """Write the preview's new rows in one batch.

    Possible duplicates are written only when included. A possible transfer is
    linked to its other side when ``link_transfers`` is set, and otherwise imported
    as an ordinary new row, leaving the existing transaction untouched.
    """
    source = Path(preview.source)
    result = ImportResult(source=str(source), source_format="csv")
    accepted = {"new", "possible_transfer"}
    if include_duplicates:
        accepted.add("possible_duplicate")
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
            if row.status == "possible_transfer" and link_transfers and row.existing:
                if _link_transfer(db, txn, preview.account, row):
                    result.observe("transaction", row.identity)
                    result.transactions_linked += 1
                    continue
            counter = row.category or _counter_account(sink, db, row.amount)
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
                payee=row.payee,
            )
        result.finish(db, txn)
    db.emit("database-changed", (db,))
    LOG.info("CSV import finished: %s", result.describe())
    return result


def _link_transfer(db: DbSQLite, txn: DbTxn, account: str, row: CsvRow) -> bool:
    """Replace the other side's placeholder split with this row's account."""
    assert row.existing is not None and row.amount is not None
    transaction = db.get_transaction(row.existing)
    if transaction is None:
        return False
    placeholders = {handle for _kind, handle in placeholder_handles()}
    guess = [split for split in transaction.splits if split.account in placeholders]
    if len(guess) != 1 or guess[0].value != row.amount:
        return False
    leg = Split(account, row.amount, memo=row.memo, handle=_split_handle(row.identity))
    transaction.splits = [leg if split is guess[0] else split for split in transaction.splits]
    db.commit_transaction(transaction, txn)
    return True
