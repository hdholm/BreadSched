"""HTTP input and output for GnuCash write-back (#174).

This adapter only parses JSON into the shared write-back service's requests and
translates its results. The preview, every refusal, the backup, the atomic write,
the read-back, and the round-trip proof all stay in
``gen/services/gnucash_writeback``, so a rejected request never changes either book.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..gen.services.gnucash_writeback import (
    ApplyWriteback,
    apply_writeback,
    preview_writeback,
    set_writeback_keep_backups,
    writeback_keep_backups,
)

if TYPE_CHECKING:
    from .resources import QueryParams
    from .server import Api


def gnucash_writeback(api: Api, query: QueryParams) -> dict[str, object]:
    """The preview: what each transaction's write changes, and what cannot be written."""
    query.finish()
    keep = writeback_keep_backups(api.db)
    previewed = preview_writeback(api.db)
    if previewed.value is None:
        error = previewed.errors[0]
        from ..presentation import service_error_message

        return {
            "available": False,
            "code": error.code,
            "message": service_error_message(error),
            "keep_backups": keep,
        }
    plan = previewed.value
    return {
        "available": True,
        "source": plan.source,
        "keep_backups": keep,
        "changes": [
            {
                "transaction": change.transaction,
                "date": change.post_date,
                "description": change.description,
                "kinds": list(change.kinds),
                "details": list(change.details),
            }
            for change in plan.changes
        ],
        "unsupported": [
            {
                "transaction": item.transaction,
                "date": item.post_date,
                "description": item.description,
                "reason": item.reason,
            }
            for item in plan.unsupported
        ],
    }


def gnucash_writeback_apply(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    raw = payload.get("transactions")
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError("transactions must be a list of transaction handles")
    result = apply_writeback(api.db, ApplyWriteback(tuple(raw)))
    if result.value is None:
        raise api._service_resource_error(result.errors[0])
    return {
        "written": [change.transaction for change in result.value.written],
        "backup": result.value.backup,
    }


def gnucash_writeback_settings(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    keep = payload.get("keep_backups")
    if isinstance(keep, bool) or not isinstance(keep, int):
        raise ValueError("keep_backups must be a whole number")
    result = set_writeback_keep_backups(api.db, keep)
    if result.value is None:
        raise api._service_resource_error(result.errors[0])
    return {"keep_backups": result.value}
