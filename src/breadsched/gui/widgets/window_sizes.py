"""Windows open at the size they were last given (#349).

Sizes are the user's, not a book's, so they live in the view settings (section
``window-sizes``), keyed by the kind of window: ``main`` for the book window,
``register-window`` for an independent register, and a dialog's class name.

The main window and register windows remember their size, and whether they were
maximized, every time they close. A dialog remembers a size only once you have
resized it: until then it opens at the size its content asks for, which changes
with what it shows. A remembered size is still kept within the screen.
"""

from __future__ import annotations

from ..gi_setup import Gtk

__all__ = ["SECTION", "forget_sizes", "remember_size", "restore_size", "stored_size"]

SECTION = "window-sizes"


def _settings(window: Gtk.Window):
    """The application's view settings, through the window or what it belongs to."""
    candidate: Gtk.Window | None = window
    while candidate is not None:
        settings = getattr(candidate.get_application(), "view_settings", None)
        if settings is not None:
            return settings
        candidate = candidate.get_transient_for()
    return None


def stored_size(window: Gtk.Window, key: str) -> tuple[int, int, bool] | None:
    """The remembered (width, height, maximized) for ``key``, if any."""
    settings = _settings(window)
    if settings is None:
        return None
    raw = settings.get(SECTION, key)
    if not raw:
        return None
    parts = raw.split(",")
    try:
        width, height = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height, len(parts) > 2 and parts[2] == "maximized"


def restore_size(window: Gtk.Window, key: str) -> bool:
    """Give ``window`` its remembered size (and maximize it if it was); False if none."""
    stored = stored_size(window, key)
    if stored is None:
        return False
    width, height, maximized = stored
    window.set_default_size(width, height)
    if maximized:
        window.maximize()
    return True


def remember_size(window: Gtk.Window, key: str) -> bool:
    """Store ``window``'s size now (its unmaximized size) and whether it is maximized."""
    settings = _settings(window)
    if settings is None:
        return False
    # GTK keeps the default size in step with the window's unmaximized size, so a
    # maximized window still reports the size it returns to.
    width, height = window.get_default_size()
    if width <= 0 or height <= 0:
        width, height = window.get_width(), window.get_height()
    if width <= 0 or height <= 0:
        return False
    value = f"{width},{height}" + (",maximized" if window.is_maximized() else "")
    if settings.get(SECTION, key) != value:
        settings.set(SECTION, key, value)
        settings.save()
    return True


def forget_sizes(settings) -> None:
    """Drop every remembered size, so windows open at their own sizes again."""
    settings.remove_section(SECTION)
