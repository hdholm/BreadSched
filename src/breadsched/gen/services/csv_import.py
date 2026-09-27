"""Preview and import a user-mapped CSV statement for CLI, GTK, and web adapters.

The preview and the import read the file through the same mapping, so the rows a
user accepted are the rows written. Validation failures return a service error and
leave the book unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...plugins.importer.csv_import import (
    CsvMapping,
    CsvMappingError,
    CsvPreview,
    import_rows,
    read_statement,
)
from ...plugins.importer.gnucash_common import ImportResult
from ..db.sqlite import DbSQLite
from ..lib.account import AccountClass
from .contracts import ServiceError, ServiceResult

__all__ = [
    "CsvImportRequest",
    "CsvImported",
    "CsvMapping",
    "import_csv",
    "preview_csv_import",
]

_FIELDS = {
    "import.csv.column.not_found": ("mapping",),
    "import.csv.amount.mapping": ("amount",),
    "import.csv.date_format.ambiguous": ("date_format",),
    "import.csv.date_format.conflict": ("date_format",),
    "import.csv.number_format.conflict": ("number_format",),
    "import.csv.encoding.invalid": ("encoding",),
    "import.csv.delimiter.invalid": ("delimiter",),
}


@dataclass(frozen=True, slots=True)
class CsvImportRequest:
    source: str
    account: str
    mapping: CsvMapping
    #: Also import rows matching an existing transaction on date and amount.
    include_duplicates: bool = False


@dataclass(frozen=True, slots=True)
class CsvImported:
    preview: CsvPreview
    result: ImportResult


def _validate(db: DbSQLite, request: CsvImportRequest) -> ServiceError | Path:
    raw = request.source.strip()
    if not raw:
        return ServiceError("import.source.required", ("source",))
    source = Path(raw).expanduser()
    if not source.exists():
        return ServiceError("import.source.not_found", ("source",))
    if not source.is_file():
        return ServiceError("import.source.not_file", ("source",))
    account = db.get_account(request.account)
    if (
        account is None
        or account.placeholder
        or account.account_class not in (AccountClass.ASSET, AccountClass.LIABILITY)
    ):
        return ServiceError("import.csv.account.invalid", ("account",))
    return source


def preview_csv_import(db: DbSQLite, request: CsvImportRequest) -> ServiceResult[CsvPreview]:
    """Classify every row as new, already imported, a possible duplicate, or invalid."""
    checked = _validate(db, request)
    if isinstance(checked, ServiceError):
        return ServiceResult.failure(checked)
    try:
        preview = read_statement(db, checked, request.account, request.mapping)
    except CsvMappingError as exc:
        return ServiceResult.failure(ServiceError(exc.code, _FIELDS.get(exc.code, ())))
    return ServiceResult.success(preview)


def import_csv(db: DbSQLite, request: CsvImportRequest) -> ServiceResult[CsvImported]:
    """Import the previewed rows as one undoable batch."""
    previewed = preview_csv_import(db, request)
    if previewed.value is None:
        return ServiceResult.failure(*previewed.errors)
    result = import_rows(db, previewed.value, include_duplicates=request.include_duplicates)
    return ServiceResult.success(CsvImported(previewed.value, result))
