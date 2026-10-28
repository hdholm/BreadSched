"""Browser-selected import files, retained under a stable per-book source path."""

from __future__ import annotations

from ..api.contract import ApiError
from ..gen.db.sqlite import DbSQLite
from ..gen.services.uploads import store_import_upload
from .context import Api
from .import_resource import import_local


def import_upload(
    api: Api,
    db: DbSQLite,
    *,
    filename: str,
    content: bytes,
    number_format: str = "auto",
    date_format: str = "auto",
    include_duplicates: bool = False,
) -> dict:
    """Import a selected file; reuse its path for later uploads of the same name."""
    stored = store_import_upload(db, filename, content)
    if stored.value is None:
        raise ApiError.from_service(stored.errors[0])
    upload = stored.value
    if upload.path.suffix == ".csv":
        # A CSV statement needs a column mapping before anything is imported;
        # keep it and return its path for inspect, preview, and import.
        return {"format": "csv", "path": str(upload.path)}
    try:
        return import_local(
            api,
            {
                "path": str(upload.path),
                "include_scheduled": True,
                "number_format": number_format,
                "date_format": date_format,
                "include_duplicates": include_duplicates,
            },
        )
    except Exception:
        upload.undo()
        raise
