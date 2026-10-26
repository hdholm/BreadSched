"""Editors split into pages: the common fields first, the rest a tab away (#383).

A long editor shows everything at once and buries the few fields most entries
need. ``FormPages`` is a notebook whose first page holds those fields; later pages
hold what is set once (defaults, planning detail) or rarely (future changes).
A page's tab can carry a count, so a page with something on it says so without
being opened. ``FormGrid`` is the label-and-field grid each page lays out.
"""

from __future__ import annotations

from ..gi_setup import Gtk

__all__ = ["FormGrid", "FormPages"]


class FormGrid(Gtk.Grid):
    """Rows of a label and its field, added in order."""

    def __init__(self) -> None:
        super().__init__(column_spacing=10, row_spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(self, f"set_margin_{side}")(12)
        self._next = 0

    def add(
        self, label: str | None, widget: Gtk.Widget, *, tooltip: str | None = None
    ) -> Gtk.Label | None:
        """Add one row; returns its label so a caller can hide the row with it."""
        caption = None
        if label is not None:
            caption = Gtk.Label(label=label, xalign=0, valign=Gtk.Align.START)
            if tooltip:
                caption.set_tooltip_text(tooltip)
            self.attach(caption, 0, self._next, 1, 1)
        widget.set_hexpand(True)
        self.attach(widget, 1, self._next, 1, 1)
        self._next += 1
        return caption

    def note(self, text: str) -> Gtk.Label:
        """A line of explanation across the page."""
        label = Gtk.Label(label=text, xalign=0, wrap=True)
        label.add_css_class("dim")
        self.attach(label, 0, self._next, 2, 1)
        self._next += 1
        return label


class FormPages(Gtk.Notebook):
    """Named pages of an editor; the first is the one shown."""

    def __init__(self) -> None:
        super().__init__()
        self._titles: dict[str, Gtk.Label] = {}
        self._pages: dict[str, FormGrid] = {}

    def page(self, title: str) -> FormGrid:
        grid = FormGrid()
        tab = Gtk.Label(label=title)
        self._titles[title] = tab
        self._pages[title] = grid
        self.append_page(grid, tab)
        return grid

    def set_count(self, title: str, count: int) -> None:
        """Show ``count`` beside a page's title, or just the title when zero."""
        self._titles[title].set_label(f"{title} ({count})" if count else title)

    def tab_label(self, title: str) -> str:
        return self._titles[title].get_label()

    def show_page(self, title: str) -> None:
        self.set_current_page(self.page_num(self._pages[title]))
