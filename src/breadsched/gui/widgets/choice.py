"""Drop-down choices that never widen a window past the screen.

GTK's default drop-down shows the chosen string at full length, and the button's
minimum width is that string's width: one account named with a long path made a
register tab, and so the main window, thousands of pixels wide. A bounded
drop-down shortens the shown choice in the middle (``Expenses:…:Groceries``, so a
path keeps its root and leaf) and lets it shrink, while its list still shows every
choice in full, wrapped, with a check beside the chosen one.
"""

from __future__ import annotations

import weakref
from collections.abc import Sequence

from ..gi_setup import Gtk, Pango

__all__ = ["bound_dropdown", "bounded_dropdown"]

#: Characters a shown choice may take before it is shortened.
CHOICE_CHARS = 36
#: Characters a line of the open list may take before it wraps.
LIST_CHARS = 72


def _text(item) -> str:
    getter = getattr(item, "get_string", None)
    return getter() if getter is not None else str(item or "")


def bound_dropdown(dropdown: Gtk.DropDown, chars: int = CHOICE_CHARS) -> Gtk.DropDown:
    """Shorten ``dropdown``'s shown choice and show its list in full."""
    button = Gtk.SignalListItemFactory()

    def setup_button(_factory, list_item) -> None:
        label = Gtk.Label(xalign=0)
        label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        label.set_max_width_chars(chars)
        label.set_width_chars(min(chars, 4))
        list_item.set_child(label)

    def bind_button(_factory, list_item) -> None:
        list_item.get_child().set_label(_text(list_item.get_item()))

    button.connect("setup", setup_button)
    button.connect("bind", bind_button)

    # Rows of the open list currently showing an item, to move the check.
    shown: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
    owner = weakref.ref(dropdown)

    def refresh_checks(*_args) -> None:
        current = owner()
        chosen = current.get_selected_item() if current is not None else None
        for list_item, check in list(shown.items()):
            check.set_opacity(1.0 if list_item.get_item() is chosen else 0.0)

    listing = Gtk.SignalListItemFactory()

    def setup_row(_factory, list_item) -> None:
        row = Gtk.Box(spacing=6)
        label = Gtk.Label(xalign=0)
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_max_width_chars(LIST_CHARS)
        label.set_hexpand(True)
        check = Gtk.Image.new_from_icon_name("object-select-symbolic")
        row.append(label)
        row.append(check)
        list_item.set_child(row)

    def bind_row(_factory, list_item) -> None:
        row = list_item.get_child()
        label = row.get_first_child()
        check = row.get_last_child()
        label.set_label(_text(list_item.get_item()))
        shown[list_item] = check
        current = owner()
        chosen = current.get_selected_item() if current is not None else None
        check.set_opacity(1.0 if list_item.get_item() is chosen else 0.0)

    def unbind_row(_factory, list_item) -> None:
        shown.pop(list_item, None)

    listing.connect("setup", setup_row)
    listing.connect("bind", bind_row)
    listing.connect("unbind", unbind_row)
    dropdown.set_factory(button)
    dropdown.set_list_factory(listing)
    dropdown.connect("notify::selected-item", refresh_checks)
    return dropdown


def bounded_dropdown(
    strings: Sequence[str] | None = None, *, chars: int = CHOICE_CHARS
) -> Gtk.DropDown:
    """A drop-down of ``strings`` (or an empty one) that cannot widen its window."""
    dropdown = (
        Gtk.DropDown.new_from_strings(list(strings)) if strings is not None else Gtk.DropDown()
    )
    return bound_dropdown(dropdown, chars)
