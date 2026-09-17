"""Typed import workflow shared by CLI, GTK, and web adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from ..db.sqlite import DbSQLite
from ..plug import IMPORTER, PluginManager, remember_import_source
from .contracts import ServiceError, ServiceResult

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
    notify: bool = True
    progress: ImportProgress | None = None


@dataclass(frozen=True, slots=True)
class ImportedBook:
    source: str
    format_id: str
    format_name: str
    result: ImportResult


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
    if plugin.id == "qif":
        kwargs["date_format"] = request.date_format

    result = cast("ImportResult", plugin.run(db, str(source), **kwargs))
    remembered = remember_import_source(db, source)
    return ServiceResult.success(ImportedBook(remembered, plugin.id, plugin.name, result))
