"""Drop-down choices that never widen a window past the screen.

GTK's default drop-down shows the chosen string at full length, and the button's
minimum width is that string's width: one account named with a long path made a
register tab, and so the main window, thousands of pixels wide. A bounded
drop-down shortens the shown choice in the middle (``Expenses:…:Groceries``, so a
path keeps its root and leaf), while its list still shows every choice in full,
wrapped, with a check beside the chosen one.

Only a choice longer than :data:`CHOICE_CHARS` is shortened: the button and the
open list are at least as wide as the longest choice up to that limit, so short
choices such as "Keep" or "Scale with gross" always show whole. Before #327 the
button could shrink to four characters and the list's rows wrapped a word a few
letters at a time.
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


def _longest(model) -> int:
    """Characters in the longest choice ``model`` holds."""
    if model is None:
        return 0
    return max((len(_text(model.get_item(i))) for i in range(model.get_n_items())), default=0)


def bound_dropdown(dropdown: Gtk.DropDown, chars: int = CHOICE_CHARS) -> Gtk.DropDown:
    """Shorten ``dropdown``'s shown choice only past ``chars``, and show its list in full."""
    button = Gtk.SignalListItemFactory()
    # The longest choice, kept per model so a long account list is read once.
    widest: dict[str, object] = {"model": None, "count": -1, "chars": 0}

    def longest() -> int:
        model = dropdown.get_model()
        count = model.get_n_items() if model is not None else 0
        if widest["model"] is not model or widest["count"] != count:
            widest.update(model=model, count=count, chars=_longest(model))
        return int(widest["chars"])  # type: ignore[call-overload]

    def setup_button(_factory, list_item) -> None:
        label = Gtk.Label(xalign=0)
        label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        label.set_max_width_chars(chars)
        list_item.set_child(label)

    def bind_button(_factory, list_item) -> None:
        label = list_item.get_child()
        label.set_label(_text(list_item.get_item()))
        # When every choice fits the limit none is shortened, so the button is
        # as wide as its longest choice; past it, the shown choice is shortened.
        widest_choice = longest()
        label.set_ellipsize(
            Pango.EllipsizeMode.NONE if widest_choice <= chars else Pango.EllipsizeMode.MIDDLE
        )
        label.set_width_chars(min(chars, max(4, widest_choice)))

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
        # A margin over the average character width, so a short choice never wraps.
        label.set_width_chars(min(LIST_CHARS, longest() + 2))
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
