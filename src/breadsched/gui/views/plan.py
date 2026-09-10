"""Derived event-driven planning view.

Scheduled occurrences and actual ledger transactions are authoritative.  Month,
quarter and year are presentation buckets only; changing the grouping never writes
planning data back to the book.
"""

from __future__ import annotations

from datetime import date

from ...gen.engine.activity import ReportingPeriod, build_category_report
from ..gi_setup import Gtk
from ._base import BaseView

__all__ = ["PlanView"]


class PlanView(BaseView):
    """Category-oriented plan versus actual view derived from transaction events."""

    WATCHES = (
        "database-changed",
        "transaction-add",
        "transaction-update",
        "transaction-delete",
        "scheduled-add",
        "scheduled-update",
        "scheduled-delete",
        "scenario-add",
        "scenario-update",
        "scenario-delete",
    )

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self._report = None
        self._year = date.today().year
        self._scenarios = []
        self._scenario_handle: str | None = None
        self._updating_scenarios = False
        self._build()

    def _build(self) -> None:
        bar = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{side}")(8)
        title = Gtk.Label(label="Plan", xalign=0)
        title.add_css_class("category-title")
        bar.append(title)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)
        bar.append(Gtk.Label(label="Scenario"))
        self.scenario = Gtk.DropDown()
        self.scenario.connect("notify::selected", self._on_scenario_changed)
        bar.append(self.scenario)
        bar.append(Gtk.Label(label="Year"))
        self.year = Gtk.SpinButton.new_with_range(1900, 2300, 1)
        self.year.set_value(self._year)
        self.year.connect("value-changed", self._on_controls_changed)
        bar.append(self.year)
        bar.append(Gtk.Label(label="Group by"))
        self.period = Gtk.DropDown.new_from_strings(["Month", "Quarter", "Year"])
        self.period.set_selected(0)
        self.period.connect("notify::selected", self._on_controls_changed)
        bar.append(self.period)
        bar.append(Gtk.Label(label="Show"))
        self.measure = Gtk.DropDown.new_from_strings(["Plan", "Actual", "Variance"])
        self.measure.set_selected(0)
        self.measure.connect("notify::selected", self._render)
        bar.append(self.measure)
        schedules = Gtk.Button(label="Edit baseline schedules…")
        schedules.connect("clicked", lambda *_: self.manager.show_category("scheduled"))
        bar.append(schedules)
        self.append(bar)

        scenario_bar = Gtk.Box(spacing=8)
        for side in ("bottom", "start", "end"):
            getattr(scenario_bar, f"set_margin_{side}")(8)
        scenario_bar.append(Gtk.Label(label="Scenario events", xalign=0))
        self.add_estimate_button = Gtk.Button(label="Add estimate…")
        self.add_estimate_button.connect("clicked", self._on_add_estimate)
        scenario_bar.append(self.add_estimate_button)
        self.alter_schedule_button = Gtk.Button(label="Alter baseline…")
        self.alter_schedule_button.connect("clicked", self._on_alter_schedule)
        scenario_bar.append(self.alter_schedule_button)
        self.suppress_schedule_button = Gtk.Button(label="Suppress baseline…")
        self.suppress_schedule_button.connect("clicked", self._on_suppress_schedule)
        scenario_bar.append(self.suppress_schedule_button)
        self.scenario_hint = Gtk.Label(xalign=0, wrap=True)
        self.scenario_hint.add_css_class("dim")
        self.scenario_hint.set_hexpand(True)
        scenario_bar.append(self.scenario_hint)
        self.append(scenario_bar)

        self.summary = Gtk.Label(xalign=0, wrap=True)
        self.summary.set_margin_start(12)
        self.summary.set_margin_end(12)
        self.summary.set_margin_bottom(8)
        self.append(self.summary)

        note = Gtk.Label(
            label=(
                "Income and expense values are derived from exact-dated scheduled/estimated "
                "and actual transaction splits; reporting periods do not store budget values."
            ),
            xalign=0,
            wrap=True,
        )
        note.add_css_class("dim")
        note.set_margin_start(12)
        note.set_margin_end(12)
        note.set_margin_bottom(8)
        self.append(note)

        self.grid = Gtk.Grid(column_spacing=12, row_spacing=3)
        self.grid.set_margin_top(8)
        self.grid.set_margin_bottom(12)
        self.grid.set_margin_start(12)
        self.grid.set_margin_end(12)
        scroll = Gtk.ScrolledWindow(child=self.grid)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        self.append(scroll)

    def _populate_scenarios(self) -> None:
        if self.db is None:
            return
        self._scenarios = list(self.db.iter_scenarios())
        model = Gtk.StringList()
        model.append("Baseline")
        selected = 0
        for index, scenario in enumerate(self._scenarios, 1):
            model.append(scenario.name)
            if scenario.handle == self._scenario_handle:
                selected = index
        self._updating_scenarios = True
        try:
            self.scenario.set_model(model)
            self.scenario.set_selected(selected)
        finally:
            self._updating_scenarios = False

    def _selected_scenario(self):
        selected = self.scenario.get_selected()
        if selected == 0 or selected > len(self._scenarios):
            return None
        return self._scenarios[selected - 1]

    def _on_scenario_changed(self, *_args) -> None:
        if self._updating_scenarios:
            return
        selected = self._selected_scenario()
        self._scenario_handle = selected.handle if selected is not None else None
        self.refresh()

    def _update_scenario_actions(self) -> None:
        selected = self._selected_scenario()
        enabled = selected is not None
        self.add_estimate_button.set_sensitive(enabled)
        self.alter_schedule_button.set_sensitive(enabled)
        self.suppress_schedule_button.set_sensitive(enabled)
        if selected is None:
            self.scenario_hint.set_text(
                "Choose a saved scenario to add, alter, or suppress recurring estimates."
            )
        else:
            count = len(selected.schedule_overrides)
            self.scenario_hint.set_text(
                f"{count} scenario-specific recurring change(s); baseline remains unchanged."
            )

    def _require_scenario(self):
        return self._selected_scenario()

    def _on_add_estimate(self, _button) -> None:
        scenario = self._require_scenario()
        if scenario is None or self.db is None:
            return
        from ..dialogs.scenario_schedule_dialog import ScenarioScheduleDialog

        ScenarioScheduleDialog(self.get_root(), self.db, scenario).present()

    def _on_alter_schedule(self, _button) -> None:
        scenario = self._require_scenario()
        if scenario is None or self.db is None:
            return
        from ..dialogs.scenario_schedule_dialog import ScenarioSchedulePickerDialog

        ScenarioSchedulePickerDialog(
            self.get_root(),
            self.db,
            "Alter baseline in scenario",
            "Alter…",
            self._edit_baseline_schedule,
        ).present()

    def _edit_baseline_schedule(self, schedule) -> None:
        scenario = self._require_scenario()
        if scenario is None or self.db is None:
            return
        from ..dialogs.scenario_schedule_dialog import ScenarioScheduleDialog

        current = next(
            (
                item
                for item in scenario.schedule_overrides
                if item.source_schedule == schedule.handle
            ),
            None,
        )
        ScenarioScheduleDialog(
            self.get_root(),
            self.db,
            scenario,
            source=schedule,
            current=current,
        ).present()

    def _on_suppress_schedule(self, _button) -> None:
        scenario = self._require_scenario()
        if scenario is None or self.db is None:
            return
        from ..dialogs.scenario_schedule_dialog import ScenarioSchedulePickerDialog

        ScenarioSchedulePickerDialog(
            self.get_root(),
            self.db,
            "Suppress baseline in scenario",
            "Suppress",
            self._suppress_baseline_schedule,
        ).present()

    def _suppress_baseline_schedule(self, schedule) -> None:
        scenario = self._require_scenario()
        if scenario is None or self.db is None:
            return
        from ...gen.lib import ScenarioSchedule

        scenario.schedule_overrides = [
            existing
            for existing in scenario.schedule_overrides
            if existing.source_schedule != schedule.handle
        ]
        scenario.schedule_overrides.append(
            ScenarioSchedule.from_scheduled(schedule, enabled=False)
        )
        with self.db.transaction(f"Update scenario {scenario.name}") as txn:
            self.db.commit_scenario(scenario, txn)

    def _grouping(self) -> ReportingPeriod:
        return (
            ReportingPeriod.MONTH,
            ReportingPeriod.QUARTER,
            ReportingPeriod.YEAR,
        )[self.period.get_selected()]

    def _on_controls_changed(self, *_args) -> None:
        self._year = self.year.get_value_as_int()
        self.refresh()

    def refresh(self) -> None:
        if self.db is None:
            return
        self._populate_scenarios()
        start = date(self._year, 1, 1)
        end = date(self._year, 12, 31)
        selected_scenario = self._selected_scenario()
        self._report = build_category_report(
            self.db,
            start,
            end,
            period=self._grouping(),
            scenario=selected_scenario,
        )
        activity = self._report.activity
        self.summary.set_text(
            f"Planned cash {activity.planned_cash_change.format(parens_negative=True)}   ·   "
            f"Actual cash {activity.actual_cash_change.format(parens_negative=True)}   ·   "
            f"Variance {activity.cash_variance.format(parens_negative=True)}   ·   "
            f"{activity.unresolved_count} expected unresolved   ·   "
            f"{activity.unresolved_actual_count} actuals to review"
        )
        self._update_scenario_actions()
        self._render()

    def _clear_grid(self) -> None:
        child = self.grid.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.grid.remove(child)
            child = nxt

    def _render(self, *_args) -> None:
        self._clear_grid()
        if self._report is None:
            return
        periods = self._report.activity.periods
        heading = Gtk.Label(label="Category", xalign=0)
        heading.add_css_class("heading")
        self.grid.attach(heading, 0, 0, 1, 1)
        for col, period in enumerate(periods, 1):
            label = Gtk.Label(label=period.label, xalign=1)
            label.add_css_class("heading")
            self.grid.attach(label, col, 0, 1, 1)

        row_index = 1
        sections = (("Income", self._report.income), ("Expenses", self._report.expenses))
        for section_name, rows in sections:
            section = Gtk.Label(label=section_name, xalign=0)
            section.add_css_class("heading")
            self.grid.attach(section, 0, row_index, 1, 1)
            row_index += 1
            for category in rows:
                name = Gtk.Label(label=("   " * category.depth) + category.name, xalign=0)
                name.set_tooltip_text(category.full_name)
                self.grid.attach(name, 0, row_index, 1, 1)
                values = (category.planned, category.actual, category.variance)[
                    self.measure.get_selected()
                ]
                for col, value in enumerate(values, 1):
                    label = Gtk.Label(label=value.format(parens_negative=True), xalign=1)
                    self.grid.attach(label, col, row_index, 1, 1)
                row_index += 1
