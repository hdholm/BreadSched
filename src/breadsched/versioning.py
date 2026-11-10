"""Application and native-book compatibility version reporting."""

from __future__ import annotations

import platform
from typing import TypedDict
from urllib.parse import urlencode

from . import RELEASE_STAGE, __version__
from .gen.db.migrations import MIN_SUPPORTED_SCHEMA_VERSION
from .gen.db.sqlite import SCHEMA_VERSION

__all__ = [
    "ISSUES_URL",
    "VersionDetails",
    "issue_details",
    "issue_url",
    "version_details",
    "version_summary",
]


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


#: Where BreadSched's issues are reported (its GitHub issue forms).
ISSUES_URL = "https://github.com/hdholm/BreadSched/issues"


def issue_details(interface: str = "Desktop (GTK)") -> str:
    """What a bug report needs about this installation, as the form's text."""
    import os
    import sys

    installed = (
        "Flatpak"
        if os.environ.get("FLATPAK_ID")
        else "Windows installer or pip"
        if sys.platform == "win32"
        else "package, pip, or source"
    )
    return "\n".join(
        (
            f"{version_summary('BreadSched')} [{RELEASE_STAGE}]",
            f"Interface: {interface}",
            f"Platform: {platform.system()} {platform.release()}".rstrip(),
            f"Python: {platform.python_version()}",
            f"Installed from: {installed}",
        )
    )


def issue_url(kind: str = "bug", *, interface: str = "Desktop (GTK)") -> str:
    """The GitHub form for a new bug report (``bug``) or feature request
    (``feature``); a bug report has this version and system already filled in."""
    if kind == "feature":
        return f"{ISSUES_URL}/new?" + urlencode({"template": "feature_request.yml"})
    fields = {"template": "bug_report.yml", "details": issue_details(interface)}
    return f"{ISSUES_URL}/new?" + urlencode(fields)
