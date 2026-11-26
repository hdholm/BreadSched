"""Date entry fields that read dates as the register does.

Every desktop field that takes a date reads it with
:func:`~breadsched.gen.engine.entry_input.parse_entry_date`, the register's rules,
so a date typed one way in the register is accepted the same way everywhere else:
``2026-03-15``, ``3/15/26``, ``3/15`` (this year), ``15`` (this month), and the
single-key shortcuts (``t`` today, ``+``/``-`` a day, ``]``/``[`` a month,
``m``/``h`` the month's first and last day, ``y``/``r`` the year's).

:func:`attach_date_keys` gives a field the shortcut keys, and :func:`read_date`
and :func:`parse_date_text` read what it holds. When the field is left it shows the
date it holds as every desktop date is shown.

The desktop writes every date in one style, chosen in Edit → Preferences and
following the computer's locale by default (``gen.utils.date_format``):
:func:`shown_date` writes a date in it, :func:`shown_text` rewrites the ISO dates in
text a shared report or note wrote, and :func:`date_hint` is a field's hint. The
style also sets the order a typed ``3/4`` is read in, so a date can be typed back
as it is shown; ``YYYY-MM-DD`` is always read too.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from datetime import date
from typing import Any, TypeVar

from ...gen.engine.entry_input import DATE_KEYS, EntryInputError, parse_entry_date
from ...gen.utils import date_format
from ..gi_setup import Gdk, Gtk

__all__ = [
    "attach_date_keys",
    "date_examples",
    "date_hint",
    "date_style",
    "parse_date_text",
    "read_date",
    "set_date_style",
    "shown_date",
    "shown_document",
    "shown_text",
]

T = TypeVar("T")

#: Fields of a report document that are not words for people: where a row goes,
#: what it sorts by, and names that identify rather than describe.
_NOT_SHOWN = frozenset({"link", "sort", "kind", "key", "name", "id"})

#: The settings key holding the chosen style (``general`` section).
SETTING = ("general", "date_format")


def set_date_style(style: str | None) -> None:
    """Show every desktop date in ``style`` (one of ``date_format.STYLES``) from now on."""
    date_format.set_current(style)


def date_style() -> str:
    """The style chosen in Preferences, as stored (``locale`` by default)."""
    return date_format.current()


def shown_date(value: date | None, empty: str = "") -> str:
    """``value`` as the desktop shows a date; ``empty`` for none."""
    return date_format.format_date(value, date_format.current()) if value is not None else empty


def shown_text(text: str) -> str:
    """``text`` with each ISO date in it shown as the desktop shows dates."""
    return date_format.rewrite_iso_dates(text, date_format.current())


def shown_document(document: T) -> T:
    """``document`` (a report or Dashboard document, or any part of one) with every
    ISO date in its words shown as the desktop shows dates.

    Shared reports write ISO dates; the desktop draws and prints them in the chosen
    style. A cell whose text changes keeps sorting by its ISO text, so a date column
    still sorts by date whatever order its day and month are shown in. Links, sort
    keys, and identifying names are left as they are.
    """
    if date_format.resolve(date_format.current()) == "iso":
        return document
    return _restyled(document)


def _restyled(value: Any) -> Any:
    if isinstance(value, str):
        return shown_text(value)
    if isinstance(value, tuple):
        items = tuple(_restyled(item) for item in value)
        return value if items == value else items
    if isinstance(value, list):
        return [_restyled(item) for item in value]
    if not is_dataclass(value) or isinstance(value, type):
        return value
    changes: dict[str, Any] = {}
    for field in fields(value):
        if not field.init or field.name in _NOT_SHOWN:
            continue
        before = getattr(value, field.name)
        after = _restyled(before)
        if after != before:
            changes[field.name] = after
    if not changes:
        return value
    cell: Any = value  # a table cell has both
    if "text" in changes and hasattr(cell, "sort") and cell.sort is None:
        changes["sort"] = cell.text
    return replace(value, **changes)


def date_examples() -> str:
    """Dates a field reads, for an error message: ``03/15/2026, 3/15, 15, or t``."""
    return date_format.entry_examples(date_format.current())


def date_hint(prefix: str = "") -> str:
    """A date field's placeholder: how a date is typed, after ``prefix`` if given."""
    hint = date_format.pattern(date_format.current())
    return f"{prefix} {hint}" if prefix else hint


def parse_date_text(text: str, base: date | None = None) -> date:
    """``text`` read as the register reads a date, relative to ``base`` (today by
    default) for a shortcut or a partial date; ``EntryInputError`` (a ``ValueError``)
    if it is none."""
    return parse_entry_date(
        text, base or date.today(), day_first=date_format.day_first(date_format.current())
    )


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
            held = parse_date_text(text) if text.strip() else date.today()
            changed = parse_date_text(character, held)
        except EntryInputError:
            return False
        entry.set_text(shown_date(changed))
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
            shown = shown_date(parse_date_text(text))
        except EntryInputError:
            return
        if shown != text:
            entry.set_text(shown)

    focus.connect("leave", on_leave)
    entry.add_controller(focus)
    return entry
