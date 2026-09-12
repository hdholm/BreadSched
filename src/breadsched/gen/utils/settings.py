"""Persistent settings, in INI files under the user's config directory.

Uses the platform's normal per-user configuration area: XDG config on Linux and
other Unix systems, ``%APPDATA%`` on Windows, and ``~/Library/Application Support``
on macOS. Two files have different lifetimes and different levels of interest to a
human:

* ``settings.ini`` — deliberate preferences, such as the book to reopen.
* ``views.ini`` — remembered interface state: column widths, sort order, which
  columns are hidden. Losing this is an inconvenience, not a loss.

Writes are atomic: the file is written alongside and renamed over the original, so
an interrupted save cannot leave a half-written config that fails to parse on the
next start.
"""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any

from .logs import get_logger
from .user_paths import config_directory

__all__ = ["Settings", "config_directory"]

LOG = get_logger(__name__)

class Settings:
    """A small typed wrapper over an INI file.

    Reading never raises: a missing, unreadable or corrupt file yields defaults,
    because a bad config file should cost the user their preferences, not their
    ability to start the application.
    """

    def __init__(self, name: str = "settings", directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory else config_directory()
        self.path = self.directory / f"{name}.ini"
        self._parser = configparser.ConfigParser()
        self.load()

    # ------------------------------------------------------------------- disk

    def load(self) -> None:
        self._parser = configparser.ConfigParser()
        if not self.path.exists():
            return
        try:
            self._parser.read(self.path, encoding="utf-8")
        except (configparser.Error, OSError) as exc:
            LOG.warning("ignoring unreadable settings at %s: %s", self.path, exc)
            self._parser = configparser.ConfigParser()

    def save(self) -> bool:
        """Write the file, atomically. Returns False if it could not be written."""
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".ini.tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                self._parser.write(handle)
            temporary.replace(self.path)
            return True
        except OSError as exc:
            LOG.warning("could not save settings to %s: %s", self.path, exc)
            return False

    # ------------------------------------------------------------------ access

    def get(self, section: str, key: str, default: str | None = None) -> str | None:
        try:
            return self._parser.get(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default

    def get_int(self, section: str, key: str, default: int = 0) -> int:
        raw = self.get(section, key)
        try:
            return int(raw) if raw is not None else default
        except ValueError:
            return default

    def get_bool(self, section: str, key: str, default: bool = False) -> bool:
        raw = self.get(section, key)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")

    def get_list(self, section: str, key: str) -> list[str]:
        raw = self.get(section, key)
        return [item for item in (raw or "").split(",") if item]

    def set(self, section: str, key: str, value: Any) -> None:
        if not self._parser.has_section(section):
            self._parser.add_section(section)
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            value = ",".join(str(item) for item in value)
        self._parser.set(section, key, str(value))

    def remove(self, section: str, key: str) -> None:
        try:
            self._parser.remove_option(section, key)
        except configparser.NoSectionError:
            pass

    def sections(self) -> list[str]:
        return self._parser.sections()

    def __contains__(self, section: str) -> bool:
        return self._parser.has_section(section)

    def __repr__(self) -> str:
        return f"<Settings {self.path}>"
