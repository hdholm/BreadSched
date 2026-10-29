"""Date entry fields that read dates as the register does.

Every desktop field that takes a date reads it with
:func:`~breadsched.gen.engine.entry_input.parse_entry_date`, the register's rules,
so a date typed one way in the register is accepted the same way everywhere else:
``2026-03-15``, ``3/15/26``, ``3/15`` (this year), ``15`` (this month), and the
single-key shortcuts (``t`` today, ``+``/``-`` a day, ``]``/``[`` a month,
``m``/``h`` the month's first and last day, ``y``/``r`` the year's).

:func:`attach_date_keys` gives a field the shortcut keys, and :func:`read_date`
and :func:`parse_date_text` read what it holds. When the field is
left it shows the date it holds as ``YYYY-MM-DD``.
"""

from __future__ import annotations

from datetime import date

from ...gen.engine.entry_input import DATE_KEYS, EntryInputError, parse_entry_date
from ..gi_setup import Gdk, Gtk

__all__ = ["attach_date_keys", "parse_date_text", "read_date"]


def parse_date_text(text: str, base: date | None = None) -> date:
    """``text`` read as the register reads a date, relative to ``base`` (today by
    default) for a shortcut or a partial date; ``EntryInputError`` (a ``ValueError``)
    if it is none."""
    return parse_entry_date(text, base or date.today())


def read_date(entry: Gtk.Editable, base: date | None = None) -> date:
    """The date typed in ``entry`` (see :func:`parse_date_text`)."""
    return parse_date_text(entry.get_text(), base)


def attach_date_keys(entry: Gtk.Entry) -> Gtk.Entry:
    """Give ``entry`` the register's date shortcuts, and tidy what it holds on leaving."""
    keys = Gtk.EventControllerKey()

    def on_key(_controller, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        character = chr(Gdk.keyval_to_unicode(keyval) or 0)
        if character not in DATE_KEYS:
            return False
        text = entry.get_text()
        try:
            # A key changes the date the field holds (or today, when empty); while
            # a date is only partly typed, the key is typed like any other.
            held = parse_entry_date(text, date.today()) if text.strip() else date.today()
            changed = parse_entry_date(character, held)
        except EntryInputError:
            return False
        entry.set_text(changed.isoformat())
        entry.set_position(-1)
        return True

    keys.connect("key-pressed", on_key)
    entry.add_controller(keys)

    focus = Gtk.EventControllerFocus()

    def on_leave(*_args) -> None:
        text = entry.get_text()
        if not text.strip():
            return
        try:
            shown = parse_entry_date(text, date.today()).isoformat()
        except EntryInputError:
            return
        if shown != text:
            entry.set_text(shown)

    focus.connect("leave", on_leave)
    entry.add_controller(focus)
    return entry
