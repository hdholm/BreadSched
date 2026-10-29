"""HTTP input and output for browser CSV statement mapping, preview, and import.

This adapter only parses JSON into the shared service's request and translates its
result. File and account validation, row classification, and the single-batch
write all stay in ``gen/services/csv_import``, so a rejected request never writes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from ..gen.services.csv_import import CsvImportRequest, CsvMapping, InspectCsv
from ..presentation import claim_link_notice, reimbursement_notice

if TYPE_CHECKING:
    from .context import Api

_TEXT_FIELDS = (
    "date",
    "amount",
    "debit",
    "credit",
    "description",
    "memo",
    "category",
    "currency",
)
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


def _splits(raw: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """Split column pairs; a pair left entirely blank in the form is ignored."""
    items = raw.get("splits") or []
    if not isinstance(items, list):
        raise ValueError("splits must be a list")
    pairs = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("each split must be an object")
        category, amount = _text(item, "category").strip(), _text(item, "amount").strip()
        if category or amount:
            pairs.append((category, amount))
    return tuple(pairs)


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
        category=columns["category"],
        currency=columns["currency"],
        date_format=cast(Any, _choice(raw, "date_format")),
        number_format=cast(Any, _choice(raw, "number_format")),
        encoding=_text(raw, "encoding", "auto") or "auto",
        delimiter=_text(raw, "delimiter", "auto") or "auto",
        header=_flag(raw, "header", True),
        invert=_flag(raw, "invert", False),
        splits=_splits(raw),
    )


def _request(payload: Mapping[str, Any]) -> CsvImportRequest:
    return CsvImportRequest(
        source=_text(payload, "path"),
        account=_text(payload, "account"),
        mapping=_mapping(payload),
        include_duplicates=_flag(payload, "include_duplicates", False),
        link_transfers=_flag(payload, "link_transfers", False),
    )


def csv_inspect(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    nested = payload.get("mapping")
    raw: Mapping[str, Any] = nested if isinstance(nested, Mapping) else payload
    inspection = api.book.inspect_csv(
        InspectCsv(
            source=_text(payload, "path"),
            encoding=_text(raw, "encoding", "auto") or "auto",
            delimiter=_text(raw, "delimiter", "auto") or "auto",
            header=_flag(raw, "header", True),
        )
    )
    return {
        "encoding": inspection.encoding,
        "delimiter": inspection.delimiter,
        "columns": list(inspection.columns),
        "sample": [list(row) for row in inspection.sample],
    }


def csv_preview(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    preview = api.book.preview_csv(_request(payload))
    names = {record.account.handle: record.full_name for record in api.book.accounts()}
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
            "possible_transfer": preview.count("possible_transfer"),
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
                "category": names.get(row.category, row.category) if row.category else None,
                "splits": [
                    {"category": names.get(account, account), "amount": str(value.to_decimal())}
                    for account, value in row.splits
                ],
            }
            for row in preview.rows
        ],
    }


def csv_import(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    request = _request(payload)
    imported = api.book.import_csv(request)
    return {
        "new": imported.result.transactions_new,
        "already_imported": imported.result.transactions_unchanged,
        "possible_duplicates": imported.preview.count("possible_duplicate"),
        "duplicates_included": request.include_duplicates,
        "possible_transfers": imported.preview.count("possible_transfer"),
        "transfers_linked": imported.result.transactions_linked,
        "skipped": imported.result.skipped,
        "detail": imported.result.detail(limit=50),
        "problems": [problem.as_dict() for problem in imported.result.problems()],
        "reimbursement_notice": reimbursement_notice(len(api.book.reimbursement_proposals())),
        "claim_link_notice": claim_link_notice(len(api.book.claim_link_proposals())),
    }
