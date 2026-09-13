"""Open private, self-contained HTML reports in the system print preview."""

from __future__ import annotations

import atexit
import tempfile
from pathlib import Path

from .gi_setup import Gio

__all__ = ["open_print_preview", "write_print_preview"]

_PREVIEWS: set[Path] = set()


def write_print_preview(document: str) -> Path:
    """Write one owner-readable temporary report and return its path."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="breadsched-print-",
        suffix=".html",
        delete=False,
    ) as target:
        target.write(document)
        path = Path(target.name)
    _PREVIEWS.add(path)
    return path


def open_print_preview(document: str) -> Path:
    """Open a generated report in the default browser and its print dialog."""
    path = write_print_preview(document)
    Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
    return path


@atexit.register
def _remove_previews() -> None:
    for path in _PREVIEWS:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
