"""Application and native-book compatibility version reporting."""

from __future__ import annotations

from typing import TypedDict

from . import __version__
from .gen.db.migrations import MIN_SUPPORTED_SCHEMA_VERSION
from .gen.db.sqlite import SCHEMA_VERSION

__all__ = ["VersionDetails", "version_details", "version_summary"]


class SchemaWindow(TypedDict):
    minimum: int
    maximum: int


class VersionDetails(TypedDict):
    application_version: str
    native_schema_version: int | None
    supported_schema_versions: SchemaWindow


def version_details(*, native_schema_version: int | None = None) -> VersionDetails:
    """Return machine-readable build and native-book compatibility versions."""
    return {
        "application_version": __version__,
        "native_schema_version": native_schema_version,
        "supported_schema_versions": {
            "minimum": MIN_SUPPORTED_SCHEMA_VERSION,
            "maximum": SCHEMA_VERSION,
        },
    }


def version_summary(program: str = "breadsched") -> str:
    """Return the version line used by CLI launchers and release diagnostics."""
    return (
        f"{program} {__version__} "
        f"(native schema {SCHEMA_VERSION}; supports {MIN_SUPPORTED_SCHEMA_VERSION}–"
        f"{SCHEMA_VERSION})"
    )
