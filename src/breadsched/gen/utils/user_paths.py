"""Platform-correct user directories and synced-path detection.

The project stays dependency-free, so this module implements the small subset of
``platformdirs`` behavior BreadSched needs using only the standard library.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

__all__ = [
    "companion_path",
    "config_directory",
    "data_directory",
    "documents_directory",
    "portal_document_id",
    "sync_service_for_path",
]

APP_DIRECTORY = "breadsched"


def _environment(environ: Mapping[str, str] | None = None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def config_directory(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
) -> Path:
    """Return BreadSched's platform-appropriate per-user config directory."""
    env = _environment(environ)
    home = Path.home() if home is None else Path(home)
    platform = sys.platform if platform is None else platform

    if platform == "win32":
        root = Path(env.get("APPDATA", home / "AppData" / "Roaming"))
    elif platform == "darwin":
        root = home / "Library" / "Application Support"
    else:
        root = Path(env.get("XDG_CONFIG_HOME", home / ".config"))
    return root / APP_DIRECTORY


def data_directory(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
) -> Path:
    """Return BreadSched's per-user data directory (inside the sandbox under Flatpak)."""
    env = _environment(environ)
    home = Path.home() if home is None else Path(home)
    platform = sys.platform if platform is None else platform

    if platform == "win32":
        root = Path(env.get("LOCALAPPDATA", home / "AppData" / "Local"))
    elif platform == "darwin":
        root = home / "Library" / "Application Support"
    else:
        root = Path(env.get("XDG_DATA_HOME", home / ".local" / "share"))
    return root / APP_DIRECTORY


def _portal_roots(env: Mapping[str, str]) -> list[Path]:
    runtime = env.get("XDG_RUNTIME_DIR")
    if not runtime and hasattr(os, "getuid"):
        runtime = f"/run/user/{os.getuid()}"
    roots = [Path("/run/flatpak/doc")]
    if runtime:
        roots.insert(0, Path(runtime) / "doc")
    return roots


def portal_document_id(path: str | Path, *, environ: Mapping[str, str] | None = None) -> str | None:
    """The document-portal id of *path*, or ``None`` for an ordinary file.

    A sandboxed application reaches a file the user chose outside the folders it
    may read directly (for BreadSched's Flatpak, anything outside Documents)
    through the document portal, as ``$XDG_RUNTIME_DIR/doc/<id>/<name>``. That
    directory holds only the chosen file: a file created beside it never appears
    under its own name on the host, so companions such as backups must go
    elsewhere (:func:`companion_path`).
    """
    env = _environment(environ)
    target = Path(path).expanduser()
    for root in _portal_roots(env):
        try:
            relative = target.relative_to(root)
        except ValueError:
            continue
        parts = relative.parts
        if parts[:1] == ("by-app",):
            # The host's view of one application's documents.
            parts = parts[2:]
        if len(parts) >= 2:
            return parts[0]
    return None


def companion_path(
    path: str | Path,
    suffix: str,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Where a file that belongs beside *path* (a backup, a log) is written.

    Normally that is ``<path><suffix>`` in the same folder. For a document-portal
    file it is ``<data directory>/beside-documents/<id>/<name><suffix>``, which the
    user can find on the host (under ``~/.var/app/<app id>/data`` for Flatpak); that
    folder is created when needed.
    """
    target = Path(path)
    document = portal_document_id(target, environ=environ)
    if document is None:
        return target.with_name(f"{target.name}{suffix}")
    folder = data_directory(environ=environ, home=home) / "beside-documents" / document
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{target.name}{suffix}"


def _xdg_documents(home: Path, env: Mapping[str, str]) -> Path | None:
    configured = env.get("XDG_DOCUMENTS_DIR")
    if configured:
        return Path(configured.replace("$HOME", str(home))).expanduser()

    config_home = Path(env.get("XDG_CONFIG_HOME", home / ".config"))
    user_dirs = config_home / "user-dirs.dirs"
    try:
        text = user_dirs.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'^XDG_DOCUMENTS_DIR=["\'](.+?)["\']\s*$', text, re.MULTILINE)
    if not match:
        return None
    value = match.group(1).replace("$HOME", str(home))
    return Path(value).expanduser()


def documents_directory(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
) -> Path:
    """Return the best standard-library approximation of the user's Documents folder."""
    env = _environment(environ)
    home = Path.home() if home is None else Path(home)
    platform = sys.platform if platform is None else platform

    if platform == "win32":
        # Windows commonly redirects Documents under OneDrive. Prefer an existing
        # redirected folder when the shell's usual environment variables expose it.
        for key in ("OneDriveConsumer", "OneDrive", "OneDriveCommercial"):
            root = env.get(key)
            if root:
                candidate = Path(root) / "Documents"
                if candidate.is_dir():
                    return candidate
        candidate = home / "Documents"
        return candidate if candidate.is_dir() else home

    if platform == "darwin":
        candidate = home / "Documents"
        return candidate if candidate.is_dir() else home

    xdg = _xdg_documents(home, env)
    if xdg is not None and xdg.is_dir():
        return xdg
    candidate = home / "Documents"
    return candidate if candidate.is_dir() else home


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.expanduser().resolve())
    except (OSError, ValueError):
        return False
    return True


def sync_service_for_path(
    path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> str | None:
    """Return a likely sync provider when *path* lives under a known synced root."""
    env = _environment(environ)
    home = Path.home() if home is None else Path(home)
    target = Path(path)
    roots: list[tuple[str, Path]] = []
    for key, label in (
        ("OneDriveConsumer", "OneDrive"),
        ("OneDriveCommercial", "OneDrive"),
        ("OneDrive", "OneDrive"),
        ("DROPBOX", "Dropbox"),
        ("GOOGLE_DRIVE", "Google Drive"),
    ):
        value = env.get(key)
        if value:
            roots.append((label, Path(value)))
    roots.extend(
        [
            ("iCloud Drive", home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"),
            ("Dropbox", home / "Dropbox"),
            ("Google Drive", home / "Google Drive"),
        ]
    )
    for label, root in roots:
        if _is_within(target, root):
            return label
    return None
