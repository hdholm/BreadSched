"""Structured editors for effective-dated schedule changes and exceptions."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date

from ..gi_setup import Gtk

__all__ = ["DateListEditor", "DatedAmountListEditor"]


class _ListEditor(Gtk.Box):
    def __init__(self, on_changed: Callable[..., None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._on_changed = on_changed
        self._rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._row_data: list[tuple[Gtk.Widget, ...]] = []
        self.append(self._rows)

    def _remove(self, row: Gtk.Widget) -> None:
        self._rows.remove(row)
        self._row_data = [item for item in self._row_data if item[0] is not row]
        self._on_changed()


class DatedAmountListEditor(_ListEditor):
    """Edit a list of date/amount pairs without exposing serialization syntax."""

    def __init__(self, on_changed: Callable[..., None], add_label: str) -> None:
        super().__init__(on_changed)
        add = Gtk.Button(label=add_label, halign=Gtk.Align.START)
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self.add_row())
        self.append(add)

    def add_row(self, when: date | None = None, amount: str = "") -> None:
        row = Gtk.Box(spacing=6)
        date_entry = Gtk.Entry(placeholder_text="YYYY-MM-DD", hexpand=True)
        amount_entry = Gtk.Entry(placeholder_text="Amount", hexpand=True)
        if when is not None:
            date_entry.set_text(when.isoformat())
        if amount:
            amount_entry.set_text(amount)
        date_entry.connect("changed", self._on_changed)
        amount_entry.connect("changed", self._on_changed)
        remove = Gtk.Button(icon_name="list-remove-symbolic")
        remove.set_tooltip_text("Remove")
        remove.add_css_class("flat")
        remove.connect("clicked", lambda *_: self._remove(row))
        row.append(date_entry)
        row.append(amount_entry)
        row.append(remove)
        self._rows.append(row)
        self._row_data.append((row, date_entry, amount_entry))
        self._on_changed()

    def set_values(self, values: Iterable[tuple[date, str]]) -> None:
        while child := self._rows.get_first_child():
            self._rows.remove(child)
        self._row_data.clear()
        for when, amount in values:
            self.add_row(when, amount)

    def values(self) -> list[tuple[str, str]]:
        return [
            (date_entry.get_text().strip(), amount_entry.get_text().strip())
            for _row, date_entry, amount_entry in self._row_data
        ]


class DateListEditor(_ListEditor):
    """Edit a list of occurrence dates."""

    def __init__(self, on_changed: Callable[..., None], add_label: str) -> None:
        super().__init__(on_changed)
        add = Gtk.Button(label=add_label, halign=Gtk.Align.START)
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self.add_row())
        self.append(add)

    def add_row(self, when: date | None = None) -> None:
        row = Gtk.Box(spacing=6)
        date_entry = Gtk.Entry(placeholder_text="YYYY-MM-DD", hexpand=True)
        if when is not None:
            date_entry.set_text(when.isoformat())
        date_entry.connect("changed", self._on_changed)
        remove = Gtk.Button(icon_name="list-remove-symbolic")
        remove.set_tooltip_text("Remove")
        remove.add_css_class("flat")
        remove.connect("clicked", lambda *_: self._remove(row))
        row.append(date_entry)
        row.append(remove)
        self._rows.append(row)
        self._row_data.append((row, date_entry))
        self._on_changed()

    def set_values(self, values: Iterable[date]) -> None:
        while child := self._rows.get_first_child():
            self._rows.remove(child)
        self._row_data.clear()
        for when in values:
            self.add_row(when)

    def values(self) -> list[str]:
        return [date_entry.get_text().strip() for _row, date_entry in self._row_data]
