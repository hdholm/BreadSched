"""Structured editors for effective-dated schedule changes and exceptions."""

from __future__ import annotations

import calendar
from collections.abc import Callable, Iterable
from datetime import date

from ..gi_setup import Gtk

__all__ = [
    "DateListEditor",
    "DatedAmountListEditor",
    "MonthAmountListEditor",
    "PlanningSplitListEditor",
    "SplitAmountTimelineEditor",
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


class DatedAmountListEditor(_ListEditor):
    """Edit date/amount pairs, optionally selecting from valid occurrence dates."""

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

    def add_row(self, when: date | None = None, amount: str = "") -> None:
        row = Gtk.Box(spacing=6)
        date_entry = self._make_date_control(when)
        amount_entry = Gtk.Entry(placeholder_text="Amount", hexpand=True)
        if amount:
            amount_entry.set_text(amount)
        if self._date_choices is None:
            date_entry.connect("changed", self._on_changed)
        else:
            date_entry.connect("notify::selected", self._on_changed)
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

    def _make_date_control(self, when: date | None) -> Gtk.Widget:
        if self._date_choices is None:
            entry = Gtk.Entry(placeholder_text="YYYY-MM-DD", hexpand=True)
            if when is not None:
                entry.set_text(when.isoformat())
            return entry
        choices = [item.isoformat() for item in self._date_choices()]
        current = when.isoformat() if when is not None else None
        if current is not None and current not in choices:
            choices.insert(0, current)
        dropdown = Gtk.DropDown.new_from_strings(choices or ["(no occurrences)"])
        dropdown.set_hexpand(True)
        if current in choices:
            dropdown.set_selected(choices.index(current))
        return dropdown

    def _date_value(self, control: Gtk.Widget) -> str:
        if self._date_choices is None:
            return control.get_text().strip()
        item = control.get_selected_item()
        return item.get_string() if item is not None else ""

    def refresh_date_choices(self) -> None:
        if self._date_choices is None:
            return
        values = []
        for when_text, amount in self.values():
            try:
                when = date.fromisoformat(when_text)
            except ValueError:
                continue
            values.append((when, amount))
        self.set_values(values)

    def set_values(self, values: Iterable[tuple[date, str]]) -> None:
        while child := self._rows.get_first_child():
            self._rows.remove(child)
        self._row_data.clear()
        for when, amount in values:
            self.add_row(when, amount)

    def values(self) -> list[tuple[str, str]]:
        return [
            (self._date_value(date_entry), amount_entry.get_text().strip())
            for _row, date_entry, amount_entry in self._row_data
        ]


class MonthAmountListEditor(_ListEditor):
    """Edit unique calendar-month amount overrides."""

    def __init__(self, on_changed: Callable[..., None]) -> None:
        super().__init__(on_changed)
        add = Gtk.Button(label="Add seasonal month", halign=Gtk.Align.START)
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self.add_row())
        self.append(add)

    def add_row(self, month: int = 1, amount: str = "") -> None:
        row = Gtk.Box(spacing=6)
        month_control = Gtk.DropDown.new_from_strings(list(calendar.month_name)[1:])
        month_control.set_selected(max(0, min(11, month - 1)))
        amount_entry = Gtk.Entry(placeholder_text="Amount", hexpand=True)
        amount_entry.set_text(amount)
        month_control.connect("notify::selected", self._on_changed)
        amount_entry.connect("changed", self._on_changed)
        remove = Gtk.Button(icon_name="list-remove-symbolic")
        remove.set_tooltip_text("Remove")
        remove.add_css_class("flat")
        remove.connect("clicked", lambda *_: self._remove(row))
        row.append(month_control)
        row.append(amount_entry)
        row.append(remove)
        self._rows.append(row)
        self._row_data.append((row, month_control, amount_entry))
        self._on_changed()

    def set_values(self, values: Iterable[tuple[int, str]]) -> None:
        while child := self._rows.get_first_child():
            self._rows.remove(child)
        self._row_data.clear()
        for month, amount in values:
            self.add_row(month, amount)

    def values(self) -> list[tuple[int, str]]:
        return [
            (month.get_selected() + 1, amount.get_text().strip())
            for _row, month, amount in self._row_data
        ]


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
        choices = [item.isoformat() for item in self._date_choices()] if self._date_choices else []
        current = when.isoformat() if when is not None else None
        if self._date_choices is None:
            date_entry = Gtk.Entry(placeholder_text="YYYY-MM-DD", hexpand=True)
            if current is not None:
                date_entry.set_text(current)
            date_entry.connect("changed", self._on_changed)
        else:
            if current is not None and current not in choices:
                choices.insert(0, current)
            date_entry = Gtk.DropDown.new_from_strings(choices or ["(no occurrences)"])
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
                values.append(date.fromisoformat(raw))
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
        account = Gtk.DropDown.new_from_strings(self._account_names)
        account.set_hexpand(True)
        account.set_selected(account_index)
        value = Gtk.Entry(placeholder_text="Amount")
        value.set_text(amount)
        purpose = Gtk.DropDown.new_from_strings(self._purpose_labels)
        purpose.set_selected(purpose_index)
        activity = Gtk.DropDown.new_from_strings(self._activity_labels)
        activity.set_selected(activity_index)
        direction = Gtk.DropDown.new_from_strings(["Normal direction", "Opposite direction"])
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


class SplitAmountTimelineEditor(_ListEditor):
    """Edit exact signed amount changes for individual fixed split legs."""

    def __init__(self, on_changed: Callable[..., None], account_names: list[str]) -> None:
        super().__init__(on_changed)
        self._account_names = account_names
        add = Gtk.Button(label="Add leg amount change", halign=Gtk.Align.START)
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self.add_row())
        self.append(add)

    def add_row(self, account_index: int = 0, when: date | None = None, amount: str = "") -> None:
        row = Gtk.Box(spacing=6)
        account = Gtk.DropDown.new_from_strings(self._account_names)
        account.set_hexpand(True)
        account.set_selected(account_index)
        when_entry = Gtk.Entry(placeholder_text="YYYY-MM-DD")
        when_entry.set_text(when.isoformat() if when is not None else "")
        amount_entry = Gtk.Entry(placeholder_text="Signed ledger amount")
        amount_entry.set_text(amount)
        account.connect("notify::selected", self._on_changed)
        when_entry.connect("changed", self._on_changed)
        amount_entry.connect("changed", self._on_changed)
        remove = Gtk.Button(icon_name="list-remove-symbolic")
        remove.set_tooltip_text("Remove")
        remove.add_css_class("flat")
        remove.connect("clicked", lambda *_: self._remove(row))
        row.append(account)
        row.append(when_entry)
        row.append(amount_entry)
        row.append(remove)
        self._rows.append(row)
        self._row_data.append((row, account, when_entry, amount_entry))
        self._on_changed()

    def set_values(self, values: Iterable[tuple[int, date, str]]) -> None:
        while child := self._rows.get_first_child():
            self._rows.remove(child)
        self._row_data.clear()
        for account_index, when, amount in values:
            self.add_row(account_index, when, amount)

    def values(self) -> list[tuple[int, str, str]]:
        return [
            (
                account.get_selected(),
                when.get_text().strip(),
                amount.get_text().strip(),
            )
            for _row, account, when, amount in self._row_data
        ]
