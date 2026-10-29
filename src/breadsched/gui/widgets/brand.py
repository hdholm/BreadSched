"""BreadSched's icon and logo, from the package's own resources.

The application icon is installed with the desktop entry on Linux and Windows,
but a checkout or a ``pip`` install has none; the package carries the same icon in
a hicolor tree that GTK is told to search, so every window shows it. The logo, with
the BreadSched name, appears on the start-up page and in the About dialog.
``scripts/brand_assets.py`` makes both from the artwork in ``data/brand``.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from ..gi_setup import Gdk, Gtk

__all__ = [
    "LOGO_WIDTH",
    "LogoPicture",
    "install_icons",
    "logo_path",
    "logo_picture",
    "logo_texture",
]

_RESOURCES = resources.files("breadsched.gui").joinpath("resources")
#: How wide the start-up page draws the logo.
LOGO_WIDTH = 320


def install_icons(display: Gdk.Display | None) -> None:
    """Let GTK find the packaged application icon on ``display``."""
    if display is None:
        return
    icons = Path(str(_RESOURCES.joinpath("icons")))
    if icons.is_dir():
        Gtk.IconTheme.get_for_display(display).add_search_path(str(icons))


def logo_path() -> str:
    return str(_RESOURCES.joinpath("breadsched-logo.png"))


def logo_texture() -> Gdk.Texture:
    return Gdk.Texture.new_from_filename(logo_path())


class LogoPicture(Gtk.Widget):
    """The logo at a fixed width, drawn from the full-size image.

    A ``Gtk.Picture`` asks for the image's own size; this asks for the width given,
    and still draws sharply on a high-density screen.
    """

    def __init__(self, width: int) -> None:
        super().__init__()
        self._texture = logo_texture()
        self._size = (width, round(width * self._texture.get_height() / self._texture.get_width()))
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["BreadSched"])
        self.add_css_class("brand-logo")

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    def do_measure(self, orientation, _for_size):
        size = self._size[0] if orientation == Gtk.Orientation.HORIZONTAL else self._size[1]
        return size, size, -1, -1

    def do_snapshot(self, snapshot) -> None:
        self._texture.snapshot(snapshot, self.get_width(), self.get_height())


def logo_picture(width: int = LOGO_WIDTH) -> LogoPicture:
    """The logo, ``width`` pixels wide."""
    return LogoPicture(width)
