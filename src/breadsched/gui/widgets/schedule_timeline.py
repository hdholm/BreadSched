"""Structured editors for a schedule's exceptions: skipped occurrences and planning splits."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date

from ..gi_setup import Gtk
from .choice import bounded_dropdown
from .date_entry import attach_date_keys, date_hint, parse_date_text, shown_date

__all__ = [
    "DateListEditor",
    "PlanningSplitListEditor",
]


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


class DateListEditor(_ListEditor):
    """Edit occurrence dates, optionally selected from generated valid dates."""

    def __init__(
        self,
        on_changed: Callable[..., None],
        add_label: str,
        date_choices: Callable[[], Iterable[date]] | None = None,
    ) -> None:
        super().__init__(on_changed)
        self._date_choices = date_choices
        add = Gtk.Button(label=add_label, halign=Gtk.Align.START)
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self.add_row())
        self.append(add)

    def add_row(self, when: date | None = None) -> None:
        row = Gtk.Box(spacing=6)
        choices = [shown_date(item) for item in self._date_choices()] if self._date_choices else []
        current = shown_date(when) if when is not None else None
        if self._date_choices is None:
            date_entry = attach_date_keys(Gtk.Entry(placeholder_text=date_hint(), hexpand=True))
            if current is not None:
                date_entry.set_text(current)
            date_entry.connect("changed", self._on_changed)
        else:
            if current is not None and current not in choices:
                choices.insert(0, current)
            date_entry = bounded_dropdown(choices or ["(no occurrences)"])
            date_entry.set_hexpand(True)
            if current in choices:
                date_entry.set_selected(choices.index(current))
            date_entry.connect("notify::selected", self._on_changed)
        remove = Gtk.Button(icon_name="list-remove-symbolic")
        remove.set_tooltip_text("Remove")
        remove.add_css_class("flat")
        remove.connect("clicked", lambda *_: self._remove(row))
        row.append(date_entry)
        row.append(remove)
        self._rows.append(row)
        self._row_data.append((row, date_entry))
        self._on_changed()

    def refresh_date_choices(self) -> None:
        if self._date_choices is None:
            return
        values = []
        for raw in self.values():
            try:
                values.append(parse_date_text(raw))
            except ValueError:
                continue
        self.set_values(values)

    def set_values(self, values: Iterable[date]) -> None:
        while child := self._rows.get_first_child():
            self._rows.remove(child)
        self._row_data.clear()
        for when in values:
            self.add_row(when)

    def values(self) -> list[str]:
        values = []
        for _row, date_control in self._row_data:
            if self._date_choices is None:
                values.append(date_control.get_text().strip())
                continue
            item = date_control.get_selected_item()
            values.append(item.get_string() if item is not None else "")
        return values


class PlanningSplitListEditor(_ListEditor):
    """Edit additional fixed split legs for a simple scheduled transaction."""

    def __init__(
        self,
        on_changed: Callable[..., None],
        account_names: list[str],
        purpose_labels: list[str],
        activity_labels: list[str] | None = None,
    ) -> None:
        super().__init__(on_changed)
        self._account_names = account_names
        self._purpose_labels = purpose_labels
        self._activity_labels = activity_labels or ["Ordinary investment activity"]
        add = Gtk.Button(label="Add split", halign=Gtk.Align.START)
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self.add_row())
        self.append(add)

    def add_row(
        self,
        account_index: int = 0,
        amount: str = "",
        purpose_index: int = 0,
        activity_index: int = 0,
        memo: str = "",
        direction_index: int = 0,
    ) -> None:
        row = Gtk.Box(spacing=6)
        account = bounded_dropdown(self._account_names)
        account.set_hexpand(True)
        account.set_selected(account_index)
        value = Gtk.Entry(placeholder_text="Amount")
        value.set_text(amount)
        purpose = bounded_dropdown(self._purpose_labels)
        purpose.set_selected(purpose_index)
        activity = bounded_dropdown(self._activity_labels)
        activity.set_selected(activity_index)
        direction = bounded_dropdown(["Normal direction", "Opposite direction"])
        direction.set_selected(direction_index)
        direction.set_tooltip_text(
            "Use opposite direction only when the stored ledger leg intentionally runs "
            "against this account's normal balance direction."
        )
        memo_entry = Gtk.Entry(placeholder_text="Memo", hexpand=True)
        memo_entry.set_text(memo)
        account.connect("notify::selected", self._on_changed)
        value.connect("changed", self._on_changed)
        purpose.connect("notify::selected", self._on_changed)
        activity.connect("notify::selected", self._on_changed)
        direction.connect("notify::selected", self._on_changed)
        memo_entry.connect("changed", self._on_changed)
        remove = Gtk.Button(icon_name="list-remove-symbolic")
        remove.set_tooltip_text("Remove")
        remove.add_css_class("flat")
        remove.connect("clicked", lambda *_: self._remove(row))
        row.append(account)
        row.append(value)
        row.append(purpose)
        row.append(activity)
        row.append(direction)
        row.append(memo_entry)
        row.append(remove)
        self._rows.append(row)
        self._row_data.append((row, account, value, purpose, activity, direction, memo_entry))
        self._on_changed()

    def set_values(self, values: Iterable[tuple[int, str, int, int, str, int]]) -> None:
        while child := self._rows.get_first_child():
            self._rows.remove(child)
        self._row_data.clear()
        for account_index, amount, purpose_index, activity_index, memo, direction_index in values:
            self.add_row(
                account_index,
                amount,
                purpose_index,
                activity_index,
                memo,
                direction_index,
            )

    def values(self) -> list[tuple[int, str, int, int, str, int]]:
        return [
            (
                account.get_selected(),
                value.get_text().strip(),
                purpose.get_selected(),
                activity.get_selected(),
                memo.get_text().strip(),
                direction.get_selected(),
            )
            for _row, account, value, purpose, activity, direction, memo in self._row_data
        ]
