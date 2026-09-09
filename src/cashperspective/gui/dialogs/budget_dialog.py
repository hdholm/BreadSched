"""Creating a budget.

Three starting points, in the order they are useful. Generating from scheduled
transactions comes first because the amounts are already known and already carry
their own periodicity: a fortnightly salary lands three times in the months where
it lands three times, rather than being averaged into a figure that is wrong every
month. Seeding from history covers the categories with no schedule behind them.
Starting empty is there for people who would rather type.
"""

from __future__ import annotations

from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.engine import budgeting
from ...gen.lib import Budget, PeriodKind
from ..gi_setup import Gtk

__all__ = ["NewBudgetDialog"]

_KINDS = [
    ("Monthly", PeriodKind.MONTH, 12),
    ("Quarterly", PeriodKind.QUARTER, 4),
    ("Yearly", PeriodKind.YEAR, 3),
]

_SOURCES = [
    (
        "From scheduled transactions",
        "Every scheduled and placeholder occurrence, dropped into the period it "
        "falls in. Quarterly and fortnightly items keep their real timing.",
    ),
    (
        "From the last 12 months",
        "Seeded from what actually happened. Irregular categories are placed in "
        "the months they occurred, not averaged across the year.",
    ),
    (
        "Empty",
        "A blank grid to fill in by hand.",
    ),
]


class NewBudgetDialog(Gtk.Window):
    """Ask how the budget should be built, then build it."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite) -> None:
        super().__init__(title="New budget", transient_for=parent, modal=True)
        self.db = db
        self.created: Budget | None = None
        self.set_default_size(480, 400)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        self.name_entry = Gtk.Entry(text=str(date.today().year))
        box.append(Gtk.Label(label="Name", xalign=0))
        box.append(self.name_entry)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        grid.attach(Gtk.Label(label="Starting", xalign=0), 0, 0, 1, 1)
        self.start_entry = Gtk.Entry(text=date.today().replace(month=1, day=1).isoformat())
        self.start_entry.set_hexpand(True)
        grid.attach(self.start_entry, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Periods", xalign=0), 0, 1, 1, 1)
        self.kind_picker = Gtk.DropDown.new_from_strings([k[0] for k in _KINDS])
        self.kind_picker.connect("notify::selected", self._on_kind_changed)
        grid.attach(self.kind_picker, 1, 1, 1, 1)

        grid.attach(Gtk.Label(label="How many", xalign=0), 0, 2, 1, 1)
        self.count_spin = Gtk.SpinButton.new_with_range(1, 60, 1)
        self.count_spin.set_value(12)
        grid.attach(self.count_spin, 1, 2, 1, 1)
        box.append(grid)

        box.append(Gtk.Separator())
        box.append(Gtk.Label(label="Start from", xalign=0))

        self._source_buttons: list[Gtk.CheckButton] = []
        first: Gtk.CheckButton | None = None
        for label, explanation in _SOURCES:
            button = Gtk.CheckButton(label=label)
            if first is None:
                first = button
                button.set_active(True)
            else:
                button.set_group(first)
            note = Gtk.Label(label=explanation, xalign=0, wrap=True)
            note.add_css_class("dim")
            note.set_margin_start(28)
            note.set_margin_bottom(6)
            box.append(button)
            box.append(note)
            self._source_buttons.append(button)

        self.status = Gtk.Label(xalign=0, wrap=True)
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        self.create_button = Gtk.Button(label="Create")
        self.create_button.add_css_class("suggested-action")
        self.create_button.connect("clicked", self._on_create)
        buttons.append(self.create_button)
        box.append(buttons)

    def _on_kind_changed(self, picker, _param) -> None:
        self.count_spin.set_value(_KINDS[picker.get_selected()][2])

    @property
    def selected_source(self) -> int:
        for index, button in enumerate(self._source_buttons):
            if button.get_active():
                return index
        return 0

    def build(self) -> Budget:
        """Construct the budget from the current settings, without storing it."""
        name = self.name_entry.get_text().strip() or "Budget"
        start = date.fromisoformat(self.start_entry.get_text().strip())
        kind = _KINDS[self.kind_picker.get_selected()][1]
        periods = int(self.count_spin.get_value())
        source = self.selected_source

        if source == 0:
            return budgeting.from_schedules(self.db, name, start, periods, kind)
        if source == 1:
            return budgeting.suggest_from_history(self.db, name, start, periods, kind)
        return Budget(name=name, start=start, periods=periods, kind=kind)

    def _on_create(self, _button) -> None:
        try:
            budget = self.build()
        except ValueError:
            self.status.set_text("The start date should look like 2026-01-01")
            self.status.add_css_class("negative")
            return

        with self.db.transaction(f"Create budget {budget.name}") as txn:
            self.db.add_budget(budget, txn)
        self.created = budget
        self.close()
