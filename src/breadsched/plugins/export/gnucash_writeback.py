"""Write simple BreadSched edits back to the GnuCash SQLite book they came from (#174).

The contract is deliberately narrow. A write is always previewed first and names
the exact source rows it inserts or updates; it covers only

- **new** two-split transactions between accounts that exist in GnuCash, in one
  currency, with quantities equal to values;
- **edits** of the date, description, number, or split memos of an imported
  transaction that no GnuCash split has reconciled; and
- **reconcile state** (``n``/``c``/``y`` and its date) set in BreadSched.

Anything else BreadSched differs on (amounts, accounts, added or removed splits,
foreign-currency or commodity splits, deletions) is listed as unsupported and never
written. The book is refused when GnuCash holds its lock, when it is not the book
last imported, or when its bytes changed since that import, so every difference
BreadSched sees is a local edit rather than a GnuCash one.

Applying copies the book to a timestamped backup first, writes every chosen row in
one ``BEGIN IMMEDIATE`` transaction after re-checking each row against the preview,
re-reads what it wrote, and restores the backup on any mismatch or error.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money
from ...gen.lib.transaction import ReconcileState, Transaction
from ..importer.gnucash_common import money_from_pair, parse_gnc_date

__all__ = [
    "FINGERPRINT_KEY",
    "KEEP_BACKUPS_KEY",
    "DEFAULT_KEEP_BACKUPS",
    "SourceFingerprint",
    "WritebackChange",
    "WritebackError",
    "WritebackPlan",
    "WritebackUnsupported",
    "apply_plan",
    "file_digest",
    "source_facts",
    "written_facts",
    "fingerprint",
    "plan_writeback",
    "record_fingerprint",
]

#: Book metadata: the GnuCash SQLite book last imported and its exact bytes.
FINGERPRINT_KEY = "gnucash.writeback.source"
#: Book metadata: how many write-back backups to keep (the oldest are removed).
KEEP_BACKUPS_KEY = "gnucash.writeback.keep_backups"
DEFAULT_KEEP_BACKUPS = 10
_INVENTORY_KEY = "import.source_inventory"
_WRITABLE_STATES = {"n", "c", "y"}


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


@dataclass(frozen=True, slots=True)
class WritebackChange:
    """Everything that would be written for one transaction."""

    transaction: str
    post_date: date
    description: str
    #: ``new``, ``edit``, and/or ``reconcile``.
    kinds: tuple[str, ...]
    #: Plain-language lines naming each source row and value written.
    details: tuple[str, ...]
    #: The exact statements, as (sql, parameters, expected current row).
    statements: tuple[tuple[str, tuple[Any, ...], tuple[Any, ...] | None], ...] = field(
        repr=False, default=()
    )


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


# ----------------------------------------------------------------- fingerprint


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _book_guid(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("SELECT guid FROM books LIMIT 1").fetchone()
    except sqlite3.OperationalError:  # a minimal book with no books table
        return ""
    return str(row[0]) if row else ""


def fingerprint(path: str | Path) -> SourceFingerprint:
    """The book's resolved path, content digest, and book GUID."""
    resolved = Path(path).expanduser().resolve()
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    try:
        guid = _book_guid(conn)
    finally:
        conn.close()
    return SourceFingerprint(str(resolved), file_digest(resolved), guid)


def record_fingerprint(db: DbSQLite, path: str | Path, digest: str, txn=None) -> None:
    """Remember the book as imported, before BreadSched changed anything locally."""
    resolved = Path(path).expanduser().resolve()
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    try:
        guid = _book_guid(conn)
    finally:
        conn.close()
    db.set_metadata(FINGERPRINT_KEY, SourceFingerprint(str(resolved), digest, guid).as_dict(), txn)


def _recorded(db: DbSQLite) -> SourceFingerprint:
    raw = db.get_metadata(FINGERPRINT_KEY)
    if not isinstance(raw, dict) or not all(
        isinstance(raw.get(key), str) for key in ("path", "sha256", "book_guid")
    ):
        raise WritebackError(
            "writeback.source.unknown",
            "import the GnuCash SQLite book into BreadSched before writing back to it",
        )
    return SourceFingerprint(raw["path"], raw["sha256"], raw["book_guid"])


def _locked(conn: sqlite3.Connection) -> bool:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gnclock'"
    ).fetchone()
    return bool(exists) and conn.execute("SELECT COUNT(*) FROM gnclock").fetchone()[0] > 0


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
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        if _locked(conn):
            raise WritebackError(
                "writeback.source.locked", "GnuCash has the book open; close it in GnuCash first"
            )
    finally:
        conn.close()
    return recorded, path


# ------------------------------------------------------------------ planning


def _date_style(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT post_date FROM transactions WHERE post_date IS NOT NULL LIMIT 1")
    sample = row.fetchone()
    text = str(sample[0]).strip() if sample and sample[0] else ""
    return "compact" if len(text) == 14 and text.isdigit() else "iso"


def _stamp(when: date, style: str) -> str:
    # GnuCash stores date-only postings at 10:59:00 UTC, its neutral time.
    if style == "compact":
        return when.strftime("%Y%m%d") + "105900"
    return when.strftime("%Y-%m-%d") + " 10:59:00"


def _pair(value: Money, fraction: int) -> tuple[int, int]:
    quantized = value.quantize(fraction)
    if quantized != value:
        raise ValueError("amount has more precision than the GnuCash commodity allows")
    return quantized.as_gnc(fraction)


def _state(split) -> str:
    raw = split.reconcile.value if isinstance(split.reconcile, ReconcileState) else "n"
    return raw if raw in _WRITABLE_STATES else "n"


def _inventory(db: DbSQLite) -> set[str]:
    raw = db.get_metadata(_INVENTORY_KEY, {})
    handles: set[str] = set()
    if isinstance(raw, dict):
        for entry in raw.values():
            if isinstance(entry, dict) and isinstance(entry.get("transactions"), list):
                handles.update(str(item) for item in entry["transactions"])
    return handles


def plan_writeback(db: DbSQLite) -> WritebackPlan:
    """Preview every supported local change; write nothing."""
    _recorded_fp, path = _preflight(db)
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return _plan(db, conn, str(path))
    finally:
        conn.close()


def _plan(db: DbSQLite, conn: sqlite3.Connection, source: str) -> WritebackPlan:
    style = _date_style(conn)
    gnc_accounts = {row["guid"] for row in conn.execute("SELECT guid FROM accounts")}
    currencies = {
        row["mnemonic"]: (row["guid"], int(row["fraction"] or 100))
        for row in conn.execute(
            "SELECT guid, mnemonic, fraction FROM commodities "
            "WHERE namespace IN ('CURRENCY','ISO4217')"
        )
    }
    gnc_tx = {row["guid"]: dict(row) for row in conn.execute("SELECT * FROM transactions")}
    gnc_splits: dict[str, dict[str, dict[str, Any]]] = {}
    for row in conn.execute("SELECT * FROM splits"):
        gnc_splits.setdefault(row["tx_guid"], {})[row["guid"]] = dict(row)
    from_gnucash = _inventory(db)

    source_guid: dict[str, str] = {}
    for account in db.iter_accounts():
        if account.source_guid and account.source_guid in gnc_accounts:
            source_guid[account.handle] = account.source_guid

    changes: list[WritebackChange] = []
    unsupported: list[WritebackUnsupported] = []

    def refuse(transaction: Transaction, reason: str) -> None:
        unsupported.append(
            WritebackUnsupported(
                transaction.handle, transaction.post_date, transaction.description, reason
            )
        )

    for transaction in sorted(db.iter_transactions(), key=lambda t: (t.post_date, t.handle)):
        if transaction.handle in gnc_tx:
            _plan_existing(
                db,
                transaction,
                gnc_tx[transaction.handle],
                gnc_splits.get(transaction.handle, {}),
                source_guid,
                style,
                changes,
                refuse,
            )
            continue
        if not any(split.account in source_guid for split in transaction.splits):
            continue  # entirely BreadSched-only accounts: nothing to do with GnuCash
        if transaction.handle in from_gnucash:
            refuse(transaction, "deleted in GnuCash and kept here; not written back")
            continue
        _plan_new(db, transaction, source_guid, currencies, style, changes, refuse)
    return WritebackPlan(source, tuple(changes), tuple(unsupported))


def _plan_new(db, transaction, source_guid, currencies, style, changes, refuse) -> None:
    if len(transaction.splits) != 2:
        refuse(transaction, "only two-split transactions can be written to GnuCash")
        return
    if any(split.account not in source_guid for split in transaction.splits):
        refuse(transaction, "an account is not in the GnuCash book")
        return
    currency = db.get_commodity(transaction.currency) if transaction.currency else None
    if currency is None:
        from ...gen.engine.currency import reporting_currency_handle

        currency = db.get_commodity(reporting_currency_handle(db))
    if currency is None or currency.mnemonic not in currencies:
        refuse(transaction, "its currency is not in the GnuCash book")
        return
    currency_guid, fraction = currencies[currency.mnemonic]
    for split in transaction.splits:
        account = db.get_account(split.account)
        if account is not None and account.commodity and account.commodity != currency.handle:
            refuse(transaction, "a split is in another currency or a security")
            return
        if split.quantity is not None and split.quantity != split.value:
            refuse(transaction, "a split's quantity differs from its amount")
            return
    try:
        pairs = [_pair(split.value, fraction) for split in transaction.splits]
    except ValueError as exc:
        refuse(transaction, str(exc))
        return
    stamp = _stamp(transaction.post_date, style)
    now = _stamp(date.today(), style)
    statements: list[tuple[str, tuple[Any, ...], tuple[Any, ...] | None]] = [
        (
            "INSERT INTO transactions (guid, currency_guid, num, post_date, enter_date,"
            " description) VALUES (?,?,?,?,?,?)",
            (
                transaction.handle,
                currency_guid,
                transaction.num,
                stamp,
                now,
                transaction.description,
            ),
            None,
        )
    ]
    details = [
        f"New transaction {transaction.handle[:8]}: {transaction.post_date.isoformat()} "
        f"{transaction.description!r} in {currency.mnemonic}"
    ]
    for split, (num, den) in zip(transaction.splits, pairs, strict=True):
        state = _state(split)
        reconciled = (
            _stamp(split.reconcile_date, style) if state == "y" and split.reconcile_date else None
        )
        statements.append(
            (
                "INSERT INTO splits (guid, tx_guid, account_guid, memo, action,"
                " reconcile_state, reconcile_date, value_num, value_denom, quantity_num,"
                " quantity_denom, lot_guid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    split.handle,
                    transaction.handle,
                    source_guid[split.account],
                    split.memo,
                    split.action,
                    state,
                    reconciled,
                    num,
                    den,
                    num,
                    den,
                    None,
                ),
                None,
            )
        )
        details.append(
            f"  split {split.handle[:8]}: {db.full_name(split.account)} "
            f"{split.value.format()} (reconcile {state})"
        )
    changes.append(
        WritebackChange(
            transaction.handle,
            transaction.post_date,
            transaction.description,
            ("new",),
            tuple(details),
            tuple(statements),
        )
    )


def _plan_existing(db, transaction, row, gnc_splits, source_guid, style, changes, refuse) -> None:
    local = {split.handle: split for split in transaction.splits}
    if set(local) != set(gnc_splits):
        refuse(transaction, "splits were added or removed; only GnuCash can change that")
        return
    for handle, split in local.items():
        source = gnc_splits[handle]
        value = money_from_pair(source["value_num"], source["value_denom"])
        quantity = money_from_pair(source["quantity_num"], source["quantity_denom"])
        if source_guid.get(split.account) != source["account_guid"] or split.value != value:
            refuse(transaction, "an amount or account changed; only GnuCash can change that")
            return
        if split.quantity is not None and split.quantity != quantity:
            refuse(transaction, "a quantity changed; only GnuCash can change that")
            return

    statements: list[tuple[str, tuple[Any, ...], tuple[Any, ...] | None]] = []
    details: list[str] = []
    kinds: list[str] = []
    text_changes: list[tuple[str, str, object, object]] = []
    gnc_date = _safe_date(row["post_date"])
    if gnc_date != transaction.post_date:
        text_changes.append(("post_date", "date", gnc_date, transaction.post_date))
    for column, label, attribute in (
        ("description", "description", "description"),
        ("num", "number", "num"),
    ):
        before = row[column] or ""
        after = getattr(transaction, attribute) or ""
        if before != after:
            text_changes.append((column, label, before, after))
    memo_changes = [
        (handle, gnc_splits[handle]["memo"] or "", split.memo or "")
        for handle, split in local.items()
        if (gnc_splits[handle]["memo"] or "") != (split.memo or "")
    ]
    gnc_reconciled = any(item["reconcile_state"] == "y" for item in gnc_splits.values())
    if (text_changes or memo_changes) and gnc_reconciled:
        refuse(transaction, "reconciled in GnuCash; its text and date are not written back")
        return
    if text_changes:
        kinds.append("edit")
        for column, label, before, after in text_changes:
            written = (
                _stamp(after, style) if column == "post_date" and isinstance(after, date) else after
            )
            current = row[column]
            statements.append(
                (
                    f"UPDATE transactions SET {column}=? WHERE guid=? AND {column} IS ?",
                    (written, transaction.handle, current),
                    (current,),
                )
            )
            details.append(f"{label}: {_shown(before)} -> {_shown(after)}")
    if memo_changes:
        if "edit" not in kinds:
            kinds.append("edit")
        for handle, before, after in memo_changes:
            statements.append(
                (
                    "UPDATE splits SET memo=? WHERE guid=? AND memo IS ?",
                    (after, handle, gnc_splits[handle]["memo"]),
                    (gnc_splits[handle]["memo"],),
                )
            )
            account = db.full_name(local[handle].account)
            details.append(f"memo on {account}: {_shown(before)} -> {_shown(after)}")
    for handle, split in local.items():
        source = gnc_splits[handle]
        state = _state(split)
        if split.reconcile.value not in _WRITABLE_STATES:
            continue
        if source["reconcile_state"] == state:
            continue
        reconciled = (
            _stamp(split.reconcile_date or transaction.post_date, style) if state == "y" else None
        )
        statements.append(
            (
                "UPDATE splits SET reconcile_state=?, reconcile_date=? WHERE guid=? AND "
                "reconcile_state IS ?",
                (state, reconciled, handle, source["reconcile_state"]),
                (source["reconcile_state"],),
            )
        )
        if "reconcile" not in kinds:
            kinds.append("reconcile")
        details.append(
            f"reconcile on {db.full_name(split.account)}: {source['reconcile_state']} -> {state}"
        )
    if statements:
        changes.append(
            WritebackChange(
                transaction.handle,
                transaction.post_date,
                transaction.description,
                tuple(kinds),
                tuple(details),
                tuple(statements),
            )
        )


def _safe_date(raw: object) -> date | None:
    try:
        return parse_gnc_date(str(raw)) if raw else None
    except ValueError:
        return None


def _shown(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    return repr(text) if text else "(blank)"


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
    """Write the chosen transactions' rows atomically; return them and the backup.

    The preflight is repeated, every UPDATE is guarded by the row value the
    preview saw, and each written row is read back. Any mismatch or error restores
    the backup, so the book is either fully written or exactly as it was.
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

    directory = _backup_dir(db, path)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    backup = directory / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, backup)

    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _locked(conn):
            raise WritebackError(
                "writeback.source.locked", "GnuCash has the book open; close it in GnuCash first"
            )
        for change in selected:
            for sql, params, _expected in change.statements:
                cursor = conn.execute(sql, params)
                if cursor.rowcount != 1:
                    raise WritebackError(
                        "writeback.source.conflict",
                        f"GnuCash no longer matches the preview for {change.description!r}",
                    )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
        shutil.copy2(backup, path)
        raise
    conn.close()
    try:
        _verify(path, selected)
        mismatched = [
            change.description
            for change in selected
            if written_facts(db.get_transaction(change.transaction))
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
    _record_written(db, path, selected)
    _rotate(directory, path, max(1, keep_backups))
    return selected, backup


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
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM transactions WHERE guid=?", (guid,)).fetchone()
        if row is None:
            return None
        splits = conn.execute("SELECT * FROM splits WHERE tx_guid=?", (guid,)).fetchall()
    finally:
        conn.close()
    return (
        _safe_date(row["post_date"]),
        row["description"] or "",
        row["num"] or "",
        tuple(
            sorted(
                (
                    split["guid"],
                    accounts.get(split["account_guid"], split["account_guid"]),
                    money_from_pair(split["value_num"], split["value_denom"]),
                    split["memo"] or "",
                    split["reconcile_state"],
                )
                for split in splits
            )
        ),
    )


def _record_written(db: DbSQLite, path: Path, selected: tuple[WritebackChange, ...]) -> None:
    """Make the written rows part of the imported baseline, touching nothing else.

    The new fingerprint lets the next preview prove GnuCash unchanged again, and
    new transactions join the import inventory so GnuCash owns them from now on
    (a later deletion in GnuCash then syncs like any imported transaction).
    """
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT root_account_guid FROM books LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        conn.close()
    with db.transaction("Record GnuCash write-back") as txn:
        record_fingerprint(db, path, file_digest(path), txn)
        new = [change.transaction for change in selected if "new" in change.kinds]
        if new and row is not None:
            raw = db.get_metadata(_INVENTORY_KEY, {})
            inventory = dict(raw) if isinstance(raw, dict) else {}
            key = f"gnucash:{row[0]}"
            entry = inventory.get(key)
            handles = (
                set(entry.get("transactions", []))
                if isinstance(entry, dict) and isinstance(entry.get("transactions"), list)
                else set()
            )
            inventory[key] = {"transactions": sorted(handles | set(new))}
            db.set_metadata(_INVENTORY_KEY, inventory, txn)


def _verify(path: Path, selected: tuple[WritebackChange, ...]) -> None:
    """Re-read every row just written and compare it with what was meant."""
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        for change in selected:
            for sql, params, _expected in change.statements:
                if sql.startswith("INSERT INTO transactions"):
                    row = conn.execute(
                        "SELECT guid, currency_guid, num, post_date, enter_date, description "
                        "FROM transactions WHERE guid=?",
                        (params[0],),
                    ).fetchone()
                    ok = row is not None and tuple(row) == params
                elif sql.startswith("INSERT INTO splits"):
                    row = conn.execute(
                        "SELECT guid, tx_guid, account_guid, memo, action, reconcile_state,"
                        " reconcile_date, value_num, value_denom, quantity_num, quantity_denom,"
                        " lot_guid FROM splits WHERE guid=?",
                        (params[0],),
                    ).fetchone()
                    ok = row is not None and tuple(row) == params
                elif sql.startswith("UPDATE transactions"):
                    column = sql.split(" SET ")[1].split("=")[0]
                    row = conn.execute(
                        f"SELECT {column} FROM transactions WHERE guid=?", (params[1],)
                    ).fetchone()
                    ok = row is not None and row[0] == params[0]
                else:
                    if "memo" in sql.split(" WHERE ")[0]:
                        row = conn.execute(
                            "SELECT memo FROM splits WHERE guid=?", (params[1],)
                        ).fetchone()
                        ok = row is not None and row[0] == params[0]
                    else:
                        row = conn.execute(
                            "SELECT reconcile_state, reconcile_date FROM splits WHERE guid=?",
                            (params[2],),
                        ).fetchone()
                        ok = row is not None and tuple(row) == (params[0], params[1])
                if not ok:
                    raise WritebackError(
                        "writeback.verify.failed",
                        f"reading back {change.description!r} did not match what was written",
                    )
    finally:
        conn.close()
