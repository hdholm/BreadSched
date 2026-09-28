"""Preview and apply GnuCash write-back for CLI, GTK, and web adapters (#174).

The preview lists every supported local change and every difference that cannot
be written, and writes nothing. Applying recomputes the preview and writes only
the chosen transactions through ``plugins/export/gnucash_writeback`` (backup, one
atomic transaction, read-back, round-trip comparison, restore on failure).
"""

from __future__ import annotations

from dataclasses import dataclass

from ...plugins.export.gnucash_writeback import (
    DEFAULT_KEEP_BACKUPS,
    KEEP_BACKUPS_KEY,
    WritebackChange,
    WritebackError,
    WritebackPlan,
    WritebackUnsupported,
    apply_plan,
    plan_writeback,
)
from ..db.sqlite import DbSQLite
from .contracts import ServiceError, ServiceResult

__all__ = [
    "ApplyWriteback",
    "WritebackApplied",
    "WritebackChange",
    "WritebackPlan",
    "WritebackUnsupported",
    "apply_writeback",
    "preview_writeback",
    "set_writeback_keep_backups",
    "writeback_keep_backups",
]

_FIELDS = {
    "writeback.selection.invalid": ("transactions",),
    "writeback.keep_backups.invalid": ("keep_backups",),
}


@dataclass(frozen=True, slots=True)
class ApplyWriteback:
    #: Handles of the previewed transactions to write.
    transactions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WritebackApplied:
    written: tuple[WritebackChange, ...]
    backup: str


def _failure(exc: WritebackError) -> ServiceResult:
    return ServiceResult.failure(ServiceError(exc.code, _FIELDS.get(exc.code, ())))


def preview_writeback(db: DbSQLite) -> ServiceResult[WritebackPlan]:
    """Every change that could be written, and why others cannot; writes nothing."""
    try:
        return ServiceResult.success(plan_writeback(db))
    except WritebackError as exc:
        return _failure(exc)


def writeback_keep_backups(db: DbSQLite) -> int:
    raw = db.get_metadata(KEEP_BACKUPS_KEY, DEFAULT_KEEP_BACKUPS)
    return (
        raw
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1
        else (DEFAULT_KEEP_BACKUPS)
    )


def set_writeback_keep_backups(db: DbSQLite, keep: int) -> ServiceResult[int]:
    """How many write-back backups of the GnuCash book to keep (1 to 1000)."""
    if isinstance(keep, bool) or not isinstance(keep, int) or not 1 <= keep <= 1000:
        return ServiceResult.failure(
            ServiceError("writeback.keep_backups.invalid", ("keep_backups",))
        )
    db.set_metadata(KEEP_BACKUPS_KEY, keep)
    return ServiceResult.success(keep)


def apply_writeback(
    db: DbSQLite,
    request: ApplyWriteback,
) -> ServiceResult[WritebackApplied]:
    """Write the chosen changes and prove they read back as BreadSched has them.

    Nothing is re-imported: an import would restore GnuCash's values over local
    edits that were not chosen. The round trip is proven by reading the written
    transactions back with the importer's rules instead.
    """
    if not request.transactions:
        return ServiceResult.failure(ServiceError("writeback.selection.invalid", ("transactions",)))
    try:
        plan = plan_writeback(db)
        written, backup = apply_plan(
            db, plan, request.transactions, keep_backups=writeback_keep_backups(db)
        )
    except WritebackError as exc:
        return _failure(exc)
    db.emit("database-changed", (db,))
    return ServiceResult.success(WritebackApplied(written, str(backup)))
