"""HTTP input and output for browser CSV statement mapping, preview, and import.

This adapter only parses JSON into the shared service's request and translates its
result. File and account validation, row classification, and the single-batch
write all stay in ``gen/services/csv_import``, so a rejected request never writes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from ..gen.services.csv_import import (
    CsvImportRequest,
    CsvInspection,
    CsvMapping,
    InspectCsv,
    import_csv,
    inspect_csv,
    preview_csv_import,
)

if TYPE_CHECKING:
    from .server import Api

_TEXT_FIELDS = ("date", "amount", "debit", "credit", "description", "memo")
_CHOICES = {
    "date_format": ("auto", "iso", "month-first", "day-first"),
    "number_format": ("auto", "dot", "comma"),
}


def _text(payload: Mapping[str, Any], key: str, default: str = "") -> str:
    value = payload.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def _flag(payload: Mapping[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false")
    return value


def _choice(raw: Mapping[str, Any], key: str) -> str:
    value = _text(raw, key, "auto") or "auto"
    if value not in _CHOICES[key]:
        raise ValueError(f"{key} must be one of {', '.join(_CHOICES[key])}")
    return value


def _mapping(payload: Mapping[str, Any]) -> CsvMapping:
    raw = payload.get("mapping")
    if not isinstance(raw, Mapping):
        raise ValueError("mapping must be an object")
    columns = {key: (_text(raw, key) or None) for key in _TEXT_FIELDS}
    return CsvMapping(
        date=columns["date"] or "",
        amount=columns["amount"],
        debit=columns["debit"],
        credit=columns["credit"],
        description=columns["description"],
        memo=columns["memo"],
        date_format=cast(Any, _choice(raw, "date_format")),
        number_format=cast(Any, _choice(raw, "number_format")),
        encoding=_text(raw, "encoding", "auto") or "auto",
        delimiter=_text(raw, "delimiter", "auto") or "auto",
        header=_flag(raw, "header", True),
        invert=_flag(raw, "invert", False),
    )


def _request(payload: Mapping[str, Any]) -> CsvImportRequest:
    return CsvImportRequest(
        source=_text(payload, "path"),
        account=_text(payload, "account"),
        mapping=_mapping(payload),
        include_duplicates=_flag(payload, "include_duplicates", False),
    )


def csv_inspect(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    nested = payload.get("mapping")
    raw: Mapping[str, Any] = nested if isinstance(nested, Mapping) else payload
    result = inspect_csv(
        InspectCsv(
            source=_text(payload, "path"),
            encoding=_text(raw, "encoding", "auto") or "auto",
            delimiter=_text(raw, "delimiter", "auto") or "auto",
            header=_flag(raw, "header", True),
        )
    )
    if result.value is None:
        raise api._service_resource_error(result.errors[0])
    inspection: CsvInspection = result.value
    return {
        "encoding": inspection.encoding,
        "delimiter": inspection.delimiter,
        "columns": list(inspection.columns),
        "sample": [list(row) for row in inspection.sample],
    }


def csv_preview(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    result = preview_csv_import(api.db, _request(payload))
    if result.value is None:
        raise api._service_resource_error(result.errors[0])
    preview = result.value
    return {
        "encoding": preview.encoding,
        "delimiter": preview.delimiter,
        "date_format": preview.date_format,
        "number_format": preview.number_format,
        "columns": list(preview.columns),
        "counts": {
            "new": preview.count("new"),
            "imported": preview.count("imported"),
            "possible_duplicate": preview.count("possible_duplicate"),
            "invalid": preview.count("invalid"),
        },
        "rows": [
            {
                "line": row.line,
                "date": row.when.isoformat() if row.when else None,
                "amount": str(row.amount.to_decimal()) if row.amount is not None else None,
                "description": row.description,
                "memo": row.memo,
                "status": row.status,
                "reason": row.reason,
            }
            for row in preview.rows
        ],
    }


def csv_import(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    request = _request(payload)
    result = import_csv(api.db, request)
    if result.value is None:
        raise api._service_resource_error(result.errors[0])
    imported = result.value
    return {
        "new": imported.result.transactions_new,
        "already_imported": imported.result.transactions_unchanged,
        "possible_duplicates": imported.preview.count("possible_duplicate"),
        "duplicates_included": request.include_duplicates,
        "skipped": imported.result.skipped,
        "detail": imported.result.detail(limit=50),
    }
