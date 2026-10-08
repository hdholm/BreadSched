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

Reading lives in ``gnucash_source``, planning in ``gnucash_writeback_plan``, and the
two writers in ``gnucash_book_writers``; this module backs up, applies, verifies,
and records the result, and re-exports the public API.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.transaction import Transaction
from .gnucash_book_writers import (
    write_sqlite,
    write_xml,
)
from .gnucash_source import (
    FINGERPRINT_KEY,
    SourceBook,
    SourceFingerprint,
    SourceTxn,
    WritebackError,
    book_format,
    file_digest,
    fingerprint,
    preflight,
    read_book,
    record_fingerprint,
)
from .gnucash_writeback_plan import (
    INVENTORY_KEY,
    TargetTxn,
    WritebackChange,
    WritebackPlan,
    WritebackUnsupported,
    money_of,
    plan_writeback,
    split_state,
)

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

#: Book metadata: how many write-back backups to keep (the oldest are removed).
KEEP_BACKUPS_KEY = "gnucash.writeback.keep_backups"
DEFAULT_KEEP_BACKUPS = 10


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
    _recorded_fp, path = preflight(db)
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
            write_sqlite(path, book, selected)
        else:
            write_xml(path, book, selected)
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
                    money_of(split.value),
                    money_of(split.quantity),
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
                (split.handle, split.account, split.value, split.memo or "", split_state(split))
                for split in transaction.splits
                if not split.importer_added  # never sent, so never compared
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
            raw = db.get_metadata(INVENTORY_KEY, {})
            inventory = dict(raw) if isinstance(raw, dict) else {}
            key = f"gnucash:{book.root_guid}"
            entry = inventory.get(key)
            handles = (
                set(entry.get("transactions", []))
                if isinstance(entry, dict) and isinstance(entry.get("transactions"), list)
                else set()
            )
            inventory[key] = {"transactions": sorted((handles | new) - gone)}
            db.set_metadata(INVENTORY_KEY, inventory, txn)
