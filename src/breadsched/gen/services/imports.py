"""Typed import workflow shared by CLI, GTK, and web adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, cast

from ..db.sqlite import DbSQLite
from ..plug import (
    IMPORTER,
    ImportLimit,
    PluginManager,
    last_import_date,
    limited_import,
    remember_import_source,
)
from .contracts import ServiceError, ServiceResult
from .receivables import sync_all_receivables

if TYPE_CHECKING:
    from ...plugins.importer.gnucash_common import ImportResult

ImportProgress = Callable[[str, int, int], None]


@dataclass(frozen=True, slots=True)
class ImportBook:
    source: str
    format: str | None = None
    include_scheduled: bool = True
    number_format: str = "auto"
    date_format: str = "auto"
    #: Also import QIF and OFX rows matching a transaction already in the account on the
    #: same date for the same amount, instead of holding them back.
    include_duplicates: bool = False
    notify: bool = True
    progress: ImportProgress | None = None
    #: Take only transactions dated from ``since`` through ``until``.
    since: date | None = None
    until: date | None = None
    #: With no ``since``, start at the day this source was last imported (every date
    #: for a source never imported before, or with ``include_duplicates``).
    since_last_import: bool = False
    #: Take only transactions with a split in one of these accounts (handles) or an
    #: account below one; empty for every account.
    accounts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportedBook:
    source: str
    format_id: str
    format_name: str
    result: ImportResult


def import_start_date(db: DbSQLite, source: str) -> date | None:
    """The day ``source`` was last imported: where a new import starts by default."""
    return last_import_date(db, source.strip()) if source.strip() else None


def _limit(db: DbSQLite, request: ImportBook, source: Path) -> ImportLimit | ServiceError:
    since = request.since
    # Including possible duplicates asks again for rows an earlier import held back,
    # whatever their dates, so it never starts at the last import.
    if since is None and request.since_last_import and not request.include_duplicates:
        since = last_import_date(db, source)
    if since is not None and request.until is not None and request.until < since:
        return ServiceError("import.dates.reversed", ("until",))
    chosen: set[str] = set()
    for handle in request.accounts:
        if db.get_account(handle) is None:
            return ServiceError("import.account.not_found", ("accounts",))
        chosen.add(handle)
    if chosen:
        # A chosen account stands for the accounts below it too.
        parents = {account.handle: account.parent for account in db.iter_accounts()}
        for handle, parent in parents.items():
            while parent is not None:
                if parent in chosen:
                    chosen.add(handle)
                    break
                parent = parents.get(parent)
    return ImportLimit(since, request.until, frozenset(chosen))


def import_book(db: DbSQLite, request: ImportBook) -> ServiceResult[ImportedBook]:
    """Validate, select, and run one importer, then remember its source."""
    raw_source = request.source.strip()
    if not raw_source:
        return ServiceResult.failure(ServiceError("import.source.required", ("source",)))
    source = Path(raw_source).expanduser()
    if not source.exists():
        return ServiceResult.failure(ServiceError("import.source.not_found", ("source",)))
    if not source.is_file():
        return ServiceResult.failure(ServiceError("import.source.not_file", ("source",)))

    manager = PluginManager.instance()
    plugin = (
        manager.get(IMPORTER, request.format)
        if request.format is not None
        else manager.for_file(str(source), IMPORTER)
    )
    if plugin is None:
        code = "import.format.not_found" if request.format else "import.format.unrecognized"
        field = "format" if request.format else "source"
        return ServiceResult.failure(ServiceError(code, (field,)))
    if request.number_format not in {"auto", "dot", "comma"}:
        return ServiceResult.failure(
            ServiceError("import.number_format.invalid", ("number_format",))
        )
    if request.date_format not in {"auto", "month-first", "day-first"}:
        return ServiceResult.failure(ServiceError("import.date_format.invalid", ("date_format",)))

    kwargs: dict[str, object] = {
        "include_scheduled": request.include_scheduled,
        "notify": request.notify,
    }
    if request.progress is not None:
        kwargs["progress"] = request.progress
    if plugin.id in {"qif", "ofx"}:
        kwargs["number_format"] = request.number_format
    if plugin.id in {"qif", "ofx"}:
        kwargs["include_duplicates"] = request.include_duplicates
    if plugin.id == "qif":
        kwargs["date_format"] = request.date_format

    limit = _limit(db, request, source)
    if isinstance(limit, ServiceError):
        return ServiceResult.failure(limit)
    with limited_import(limit):
        result = cast("ImportResult", plugin.run(db, str(source), **kwargs))
    # An import can change a linked expense directly; keep receivable accounts true.
    sync_all_receivables(db)
    remembered = remember_import_source(db, source)
    return ServiceResult.success(ImportedBook(remembered, plugin.id, plugin.name, result))
