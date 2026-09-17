"""Save the current assumptions as a named scenario."""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite  # noqa: E402
from ...gen.lib import Scenario  # noqa: E402
from ...gen.services import SaveScenario, save_scenario  # noqa: E402
from ...presentation import service_error_message  # noqa: E402
from ..gi_setup import Gtk

__all__ = ["SaveScenarioDialog"]


class SaveScenarioDialog(Gtk.Window):
    """Name a set of assumptions so it can be compared against later.

    Saving under an existing name updates that scenario rather than creating a
    second one with the same label, since two scenarios called "Base" is the state
    from which nobody recovers.
    """

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite, scenario: Scenario) -> None:
        super().__init__(title="Save scenario", transient_for=parent, modal=True)
        self.db = db
        self.scenario = scenario
        self.set_default_size(420, 260)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        self.name_entry = Gtk.Entry(placeholder_text="Base case")
        if scenario.name and scenario.name != "Working scenario":
            self.name_entry.set_text(scenario.name)
        box.append(Gtk.Label(label="Name", xalign=0))
        box.append(self.name_entry)

        self.description_entry = Gtk.Entry(placeholder_text="What this scenario assumes, and why")
        self.description_entry.set_text(scenario.description)
        box.append(Gtk.Label(label="Description", xalign=0))
        box.append(self.description_entry)

        assumptions = scenario.effective_assumptions()
        summary = Gtk.Label(xalign=0, wrap=True)
        summary.add_css_class("dim")
        summary.set_text(
            f"{scenario.years} years from {scenario.start:%b %Y}.\n"
            f"Income growth {assumptions.income_growth:.2%}, "
            f"expense inflation {assumptions.expense_inflation:.2%}, "
            f"investment return {assumptions.investment_return:.2%}, "
            f"cash interest {assumptions.cash_interest:.2%}."
        )
        box.append(summary)

        self.status = Gtk.Label(xalign=0)
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save)
        buttons.append(save)
        box.append(buttons)

    def _on_save(self, _button) -> None:
        name = self.name_entry.get_text().strip()
        if not name:
            self.status.set_text("Give the scenario a name first.")
            self.status.add_css_class("negative")
            return

        existing = self.db.get_scenario_by_name(name)
        target = existing or self.scenario.clone()
        target.name = name
        target.description = self.description_entry.get_text().strip()
        target.years = self.scenario.years
        target.start = self.scenario.start
        target.assumptions = self.scenario.assumptions
        target.inherits_base_assumptions = self.scenario.inherits_base_assumptions
        target.parent_handle = self.scenario.parent_handle
        target.assumption_overrides = set(self.scenario.assumption_overrides)
        target.account_assumption_overrides = set(self.scenario.account_assumption_overrides)
        target.account_assumption_suppressions = set(self.scenario.account_assumption_suppressions)
        target.opening_overrides = dict(self.scenario.opening_overrides)
        target.one_offs = list(self.scenario.one_offs)
        target.assumption_periods = list(self.scenario.assumption_periods)
        target.schedule_overrides = list(self.scenario.schedule_overrides)

        result = save_scenario(
            self.db,
            SaveScenario(target, existing_handle=existing.handle if existing else None),
        )
        if not result.ok:
            self.status.set_text(service_error_message(result.errors[0]))
            self.status.add_css_class("negative")
            return
        self.close()
