"""HTTP input and output for GnuCash write-back (#174).

This adapter only parses JSON into the shared write-back service's requests, sent
through the book server (``writeback_preview``, ``apply_writeback``,
``set_writeback_keep_backups``), and translates its results. The preview, every
refusal, the backup, the atomic write, the read-back, and the round-trip proof all
stay in ``gen/services/gnucash_writeback``, so a rejected request never changes
either book.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..api import ApiError
from ..gen.services.gnucash_writeback import ApplyWriteback

if TYPE_CHECKING:
    from .context import Api
    from .resources import QueryParams


def gnucash_writeback(api: Api, query: QueryParams) -> dict[str, object]:
    """The preview: what each transaction's write changes, and what cannot be written."""
    query.finish()
    keep = api.book.writeback_keep_backups()
    try:
        plan = api.book.writeback_preview()
    except ApiError as error:
        return {
            "available": False,
            "code": error.code,
            "message": str(error),
            "keep_backups": keep,
        }
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
    applied = api.book.apply_writeback(ApplyWriteback(tuple(raw)))
    return {"written": [change.transaction for change in applied.written], "backup": applied.backup}


def gnucash_writeback_settings(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    keep = payload.get("keep_backups")
    if isinstance(keep, bool) or not isinstance(keep, int):
        raise ValueError("keep_backups must be a whole number")
    return {"keep_backups": api.book.set_writeback_keep_backups(keep)}
