"""HTTP input and output for transaction tags and linked documents.

This adapter parses the browser's requests into ``services.attachments`` and
translates the results; validation and every write stay in the service, so a
rejected request leaves the stored transaction unchanged. A document is served
only when the transaction lists it, and the browser can link or relink only a web
address or a file inside the attachment folder (``contained_location``), so the
routes cannot be used to read an arbitrary file.
"""

from __future__ import annotations

import mimetypes
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..gen.db.sqlite import DbSQLite
from ..gen.engine import attachments as attachment_engine
from ..gen.lib.transaction import Transaction
from ..gen.services import (
    attach_file,
    attach_location,
    contained_location,
    detach,
    relink,
    set_tags,
)
from ..gen.services.contracts import ServiceError
from ..presentation import service_error_message

if TYPE_CHECKING:
    from .server import Api

__all__ = [
    "attachment_content",
    "attachment_upload",
    "document_json",
    "transaction_attachment_link",
    "transaction_attachment_relink",
    "transaction_attachment_remove",
    "transaction_tags",
]

#: Largest document the browser may upload.
MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024


def _refuse(status: int, code: str, field: str) -> Exception:
    from .resources import ResourceError  # resources imports this module

    error = ServiceError(code, (field,))
    return ResourceError(status, code, error.fields, service_error_message(error))


def document_json(db: DbSQLite, transaction: Transaction) -> list[dict[str, object]]:
    """Every linked document of a transaction, with whether it can be found."""
    return [
        {
            "location": item.location,
            "kind": item.kind,
            "present": item.present,
            "missing": item.missing,
            "owner": item.owner,
            "path": str(item.path) if item.path is not None else None,
        }
        for item in attachment_engine.statuses(db, transaction)
    ]


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def _changed(api: Api, result: Any) -> dict[str, object]:
    if result.value is None:
        raise api._service_resource_error(result.errors[0])
    transaction = result.value
    return {
        "handle": transaction.handle,
        "tags": list(transaction.tags),
        "documents": document_json(api.db, transaction),
    }


def transaction_tags(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Replace a transaction's tags: ``{"transaction": handle, "tags": [text, ...]}``."""
    tags = payload.get("tags")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("tags must be a list of text")
    return _changed(api, set_tags(api.db, _text(payload, "transaction"), tags))


def _contained(api: Api, location: str) -> str:
    confined = contained_location(api.db, location)
    if isinstance(confined, ServiceError):
        raise api._service_resource_error(confined)
    return confined


def transaction_attachment_link(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Link a web address or a file already in the attachment folder."""
    transaction = _text(payload, "transaction")
    location = _contained(api, _text(payload, "location"))
    return _changed(api, attach_location(api.db, transaction, location))


def transaction_attachment_remove(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Unlink a document; the file itself is kept."""
    return _changed(api, detach(api.db, _text(payload, "transaction"), _text(payload, "location")))


def transaction_attachment_relink(api: Api, payload: Mapping[str, Any]) -> dict[str, object]:
    """Point a moved or missing document at its place in the attachment folder."""
    transaction = _text(payload, "transaction")
    location = _text(payload, "location")
    target = _contained(api, _text(payload, "to"))
    return _changed(api, relink(api.db, transaction, location, target))


def attachment_upload(
    api: Api, db: DbSQLite, *, transaction: str, filename: str, content: bytes
) -> dict[str, object]:
    """Copy a browser-selected file into the attachment folder and link it."""
    if (
        not filename
        or filename in {".", ".."}
        or len(filename.encode("utf-8")) > 255
        or any(c in filename for c in "/\\\0")
    ):
        raise _refuse(400, "attachment.filename.invalid", "filename")
    if not content:
        raise _refuse(400, "attachment.file.empty", "file")
    with tempfile.TemporaryDirectory(prefix="breadsched-attachment-") as staging:
        root = os.path.realpath(staging)
        staged = os.path.normpath(os.path.join(root, filename))
        if not staged.startswith(root + os.sep):
            raise _refuse(400, "attachment.filename.invalid", "filename")
        Path(staged).write_bytes(content)
        return _changed(api, attach_file(db, transaction, Path(staged)))


def attachment_content(db: DbSQLite, transaction: str, location: str) -> tuple[bytes, str]:
    """The bytes and media type of one document the transaction lists."""
    stored = db.get_transaction(transaction)
    if stored is None:
        raise _refuse(404, "transaction.not_found", "transaction")
    item = next(
        (entry for entry in attachment_engine.statuses(db, stored) if entry.location == location),
        None,
    )
    if item is None:
        raise _refuse(404, "attachment.not_found", "location")
    if item.kind == "web" or item.path is None:
        raise _refuse(400, "attachment.web_address", "location")
    if not item.path.is_file():
        raise _refuse(404, "attachment.missing", "location")
    kind = mimetypes.guess_type(item.path.name)[0] or "application/octet-stream"
    return item.path.read_bytes(), kind
