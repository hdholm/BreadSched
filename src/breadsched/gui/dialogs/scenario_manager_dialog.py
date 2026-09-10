"""Manage saved planning scenarios and their base assumptions."""

from __future__ import annotations

from decimal import Decimal

from ...gen.db.sqlite import DbSQLite
from ...gen.lib import Scenario
from ...gen.lib.base import create_handle
from ..gi_setup import Gtk

__all__ = ["ScenarioManagerDialog"]


_ASSUMPTIONS = (
    ("Income growth", "income_growth"),
    ("Expense inflation", "expense_inflation"),
    ("Investment return", "investment_return"),
    ("Cash interest", "cash_interest"),
    ("Liability interest", "liability_interest"),
)


class ScenarioManagerDialog(Gtk.Window):
    """Rename, duplicate, delete, and edit a saved scenario's base assumptions."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite) -> None:
        super().__init__(title="Manage scenarios", transient_for=parent, modal=True)
        self.db = db
        self._scenarios: list[Scenario] = []
        self._loading = False
        self.set_default_size(620, 520)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        box.append(
            Gtk.Label(
                label=(
                    "Saved scenarios change assumptions and future estimated activity without "
                    "modifying the Baseline plan."
                ),
                xalign=0,
                wrap=True,
            )
        )

        picker_row = Gtk.Box(spacing=8)
        picker_row.append(Gtk.Label(label="Scenario"))
        self.picker = Gtk.DropDown()
        self.picker.set_hexpand(True)
        self.picker.connect("notify::selected", self._on_selected)
        picker_row.append(self.picker)
        self.duplicate_button = Gtk.Button(label="Duplicate…")
        self.duplicate_button.connect("clicked", self._on_duplicate)
        picker_row.append(self.duplicate_button)
        self.delete_button = Gtk.Button(label="Delete…")
        self.delete_button.add_css_class("destructive-action")
        self.delete_button.connect("clicked", self._on_delete)
        picker_row.append(self.delete_button)
        box.append(picker_row)

        self.editor = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(self.editor)

        self.editor.append(Gtk.Label(label="Name", xalign=0))
        self.name_entry = Gtk.Entry()
        self.editor.append(self.name_entry)

        self.editor.append(Gtk.Label(label="Description", xalign=0))
        self.description_entry = Gtk.Entry()
        self.editor.append(self.description_entry)

        self.editor.append(Gtk.Label(label="Base annual assumptions", xalign=0))
        rates = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.rate_controls: dict[str, Gtk.SpinButton] = {}
        for row, (label, attribute) in enumerate(_ASSUMPTIONS):
            rates.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            control = Gtk.SpinButton.new_with_range(-100.0, 100.0, 0.1)
            control.set_digits(2)
            control.set_tooltip_text("Annual percentage; 6.00 means 6% per year")
            rates.attach(control, 1, row, 1, 1)
            rates.attach(Gtk.Label(label="%", xalign=0), 2, row, 1, 1)
            self.rate_controls[attribute] = control
        self.editor.append(rates)

        self.timeline_summary = Gtk.Label(xalign=0, wrap=True)
        self.timeline_summary.add_css_class("dim")
        self.editor.append(self.timeline_summary)

        self.event_summary = Gtk.Label(xalign=0, wrap=True)
        self.event_summary.add_css_class("dim")
        self.editor.append(self.event_summary)

        self.status = Gtk.Label(xalign=0, wrap=True)
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        buttons.append(close)
        self.save_button = Gtk.Button(label="Save changes")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._on_save)
        buttons.append(self.save_button)
        box.append(buttons)

        self._reload()

    def _reload(self, select_handle: str | None = None) -> None:
        self._scenarios = list(self.db.iter_scenarios())
        model = Gtk.StringList()
        selected = 0
        for index, scenario in enumerate(self._scenarios):
            model.append(scenario.name)
            if scenario.handle == select_handle:
                selected = index
        self._loading = True
        try:
            self.picker.set_model(model)
            if self._scenarios:
                self.picker.set_selected(selected)
        finally:
            self._loading = False
        self._load_selected()

    def _selected(self) -> Scenario | None:
        selected = self.picker.get_selected()
        if selected >= len(self._scenarios):
            return None
        return self._scenarios[selected]

    def _on_selected(self, *_args) -> None:
        if not self._loading:
            self._load_selected()

    def _load_selected(self) -> None:
        scenario = self._selected()
        enabled = scenario is not None
        self.editor.set_sensitive(enabled)
        self.save_button.set_sensitive(enabled)
        self.duplicate_button.set_sensitive(enabled)
        self.delete_button.set_sensitive(enabled)
        if scenario is None:
            self.name_entry.set_text("")
            self.description_entry.set_text("")
            self.timeline_summary.set_text("No saved scenarios yet.")
            self.event_summary.set_text("")
            return

        self.name_entry.set_text(scenario.name)
        self.description_entry.set_text(scenario.description)
        for _label, attribute in _ASSUMPTIONS:
            value = getattr(scenario.assumptions, attribute) * Decimal("100")
            self.rate_controls[attribute].set_value(float(value))
        periods = len(scenario.assumption_periods)
        changes = len(scenario.schedule_overrides)
        self.timeline_summary.set_text(
            f"{periods} dated assumption period(s). Timeline editing is handled separately."
        )
        self.event_summary.set_text(
            f"{changes} scenario-specific recurring event change(s) are preserved here."
        )
        self.status.set_text("")
        self.status.remove_css_class("negative")

    def _on_save(self, _button) -> None:
        scenario = self._selected()
        if scenario is None:
            return
        name = self.name_entry.get_text().strip()
        if not name:
            self._error("Give the scenario a name first.")
            return
        duplicate = self.db.get_scenario_by_name(name)
        if duplicate is not None and duplicate.handle != scenario.handle:
            self._error(f'A scenario named "{name}" already exists.')
            return

        scenario.name = name
        scenario.description = self.description_entry.get_text().strip()
        for _label, attribute in _ASSUMPTIONS:
            value = Decimal(str(self.rate_controls[attribute].get_value())) / Decimal("100")
            setattr(scenario.assumptions, attribute, value)
        with self.db.transaction(f"Update scenario {scenario.name}") as txn:
            self.db.commit_scenario(scenario, txn)
        self._reload(scenario.handle)
        self.status.set_text("Scenario saved.")
        self.status.remove_css_class("negative")

    def _on_duplicate(self, _button) -> None:
        scenario = self._selected()
        if scenario is None:
            return
        clone = Scenario.from_dict(scenario.serialize())
        clone.handle = create_handle()
        clone.gid = ""
        clone.change = 0
        clone.name = self._unique_copy_name(scenario.name)
        with self.db.transaction(f"Duplicate scenario {scenario.name}") as txn:
            self.db.add_scenario(clone, txn)
        self._reload(clone.handle)
        self.status.set_text(f'Created "{clone.name}". Rename it or adjust assumptions as needed.')
        self.status.remove_css_class("negative")

    def _unique_copy_name(self, name: str) -> str:
        base = f"{name} copy"
        candidate = base
        number = 2
        while self.db.get_scenario_by_name(candidate) is not None:
            candidate = f"{base} {number}"
            number += 1
        return candidate

    def _on_delete(self, _button) -> None:
        scenario = self._selected()
        if scenario is None:
            return
        ScenarioDeleteDialog(self, self.db, scenario, self._after_delete).present()

    def _after_delete(self) -> None:
        self._reload()
        self.status.set_text("Scenario deleted. Baseline was not changed.")
        self.status.remove_css_class("negative")

    def _error(self, message: str) -> None:
        self.status.set_text(message)
        self.status.add_css_class("negative")


class ScenarioDeleteDialog(Gtk.Window):
    """Confirm destructive removal of one saved scenario."""

    def __init__(
        self,
        parent: Gtk.Window,
        db: DbSQLite,
        scenario: Scenario,
        deleted_callback,
    ) -> None:
        super().__init__(title="Delete scenario", transient_for=parent, modal=True)
        self.db = db
        self.scenario = scenario
        self.deleted_callback = deleted_callback

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.append(
            Gtk.Label(
                label=(
                    f'Delete scenario "{scenario.name}"? This removes its assumptions and '
                    f"{len(scenario.schedule_overrides)} scenario-specific recurring change(s). "
                    "Baseline transactions and schedules are not modified."
                ),
                xalign=0,
                wrap=True,
            )
        )
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        delete = Gtk.Button(label="Delete scenario")
        delete.add_css_class("destructive-action")
        delete.connect("clicked", self._confirm)
        buttons.append(delete)
        box.append(buttons)

    def _confirm(self, _button) -> None:
        with self.db.transaction(f"Delete scenario {self.scenario.name}") as txn:
            self.db.remove_scenario(self.scenario.handle, txn)
        self.close()
        self.deleted_callback()
