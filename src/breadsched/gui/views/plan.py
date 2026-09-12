"""Derived event-driven planning view.

Scheduled occurrences and actual ledger transactions are authoritative.  Month,
quarter and year are presentation buckets only; changing the grouping never writes
planning data back to the book.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date

from ...gen.engine.activity import (
    CategoryReport,
    ReportingPeriod,
    build_category_report,
    explain_category_period,
    explain_planning_flow_period,
)
from ...gen.lib import Scenario
from ..gi_setup import Gtk
from ..planning_context import (
    baseline_scenario,
    select_scenario,
    selected_scenario_handle,
)
from ._base import BaseView

__all__ = ["PlanView"]

_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


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
        self._report: CategoryReport | None = None
        today = date.today()
        self._start_date = date(today.year, 1, 1)
        self._end_date = date(today.year + 1, 12, 31)
        self._period_index = 0
        self._measure_index = 0
        self._bounds_initialized = False
        self._updating_controls = False
        self._scenarios: list[Scenario] = []
        self._scenario_handle: str | None = selected_scenario_handle(manager)
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
        new_scenario = Gtk.Button(label="New scenario…")
        new_scenario.connect("clicked", self._on_new_scenario)
        bar.append(new_scenario)
        manage_scenarios = Gtk.Button(label="Manage scenarios…")
        manage_scenarios.connect("clicked", self._on_manage_scenarios)
        bar.append(manage_scenarios)
        bar.append(Gtk.Label(label="Scenario"))
        self.scenario = Gtk.DropDown()
        self.scenario.connect("notify::selected", self._on_scenario_changed)
        bar.append(self.scenario)
        bar.append(Gtk.Label(label="From"))
        self.start_month = Gtk.DropDown.new_from_strings(list(_MONTHS))
        self.start_month.set_selected(self._start_date.month - 1)
        self.start_month.connect("notify::selected", self._on_controls_changed)
        bar.append(self.start_month)
        self.start_year = Gtk.SpinButton.new_with_range(1, date.today().year + 150, 1)
        self.start_year.set_value(self._start_date.year)
        self.start_year.connect("value-changed", self._on_controls_changed)
        bar.append(self.start_year)
        bar.append(Gtk.Label(label="Through"))
        self.end_month = Gtk.DropDown.new_from_strings(list(_MONTHS))
        self.end_month.set_selected(self._end_date.month - 1)
        self.end_month.connect("notify::selected", self._on_controls_changed)
        bar.append(self.end_month)
        self.end_year = Gtk.SpinButton.new_with_range(1, date.today().year + 150, 1)
        self.end_year.set_value(self._end_date.year)
        self.end_year.connect("value-changed", self._on_controls_changed)
        bar.append(self.end_year)
        bar.append(Gtk.Label(label="Group by"))
        self.period = Gtk.DropDown.new_from_strings(["Month", "Quarter", "Year"])
        self.period.set_selected(self._period_index)
        self.period.connect("notify::selected", self._on_controls_changed)
        bar.append(self.period)
        bar.append(Gtk.Label(label="Show"))
        self.measure = Gtk.DropDown.new_from_strings(["Plan", "Actual", "Variance"])
        self.measure.set_selected(self._measure_index)
        self.measure.connect("notify::selected", self._on_controls_changed)
        bar.append(self.measure)
        self.apply_button = Gtk.Button(label="Apply")
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", self._on_apply)
        bar.append(self.apply_button)
        schedules = Gtk.Button(label="Edit baseline schedules…")
        schedules.connect("clicked", lambda *_: self.manager.show_category("scheduled"))
        bar.append(schedules)
        self.append(bar)

        self.control_status = Gtk.Label(xalign=0, wrap=True)
        self.control_status.set_margin_start(12)
        self.control_status.set_margin_end(12)
        self.control_status.set_margin_bottom(4)
        self.append(self.control_status)

        scenario_bar = Gtk.Box(spacing=8)
        self.scenario_events_box = scenario_bar
        self.scenario_events_box.set_sensitive(False)
        for side in ("bottom", "start", "end"):
            getattr(scenario_bar, f"set_margin_{side}")(8)
        scenario_bar.append(Gtk.Label(label="Scenario events", xalign=0))
        self.add_estimate_button = Gtk.Button(label="Add estimate…")
        self.add_estimate_button.set_sensitive(False)
        self.add_estimate_button.connect("clicked", self._on_add_estimate)
        scenario_bar.append(self.add_estimate_button)
        self.alter_schedule_button = Gtk.Button(label="Alter baseline…")
        self.alter_schedule_button.set_sensitive(False)
        self.alter_schedule_button.connect("clicked", self._on_alter_schedule)
        scenario_bar.append(self.alter_schedule_button)
        self.suppress_schedule_button = Gtk.Button(label="Suppress baseline…")
        self.suppress_schedule_button.set_sensitive(False)
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

    def set_db(self, db) -> None:
        """Attach a book and restore the default display horizon for that book."""
        today = date.today()
        self._start_date = date(today.year, 1, 1)
        self._end_date = date(today.year + 1, 12, 31)
        self._period_index = 0
        self._measure_index = 0
        self._bounds_initialized = False
        self._updating_controls = True
        try:
            self.start_year.set_value(self._start_date.year)
            self.start_month.set_selected(self._start_date.month - 1)
            self.end_year.set_value(self._end_date.year)
            self.end_month.set_selected(self._end_date.month - 1)
            self.period.set_selected(self._period_index)
            self.measure.set_selected(self._measure_index)
        finally:
            self._updating_controls = False
        super().set_db(db)

    def _populate_scenarios(self) -> None:
        if self.db is None:
            return
        self._scenarios = list(self.db.iter_scenarios())
        model = Gtk.StringList()
        model.append("Base scenario")
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
        if selected == 0 and self._scenario_handle is not None:
            self._scenario_handle = None
            select_scenario(self.manager, None, source=self)

    def _scenario_at_selection(self):
        selected = self.scenario.get_selected()
        if selected == 0 or selected > len(self._scenarios):
            return None
        return self._scenarios[selected - 1]

    def _selected_scenario(self):
        if self._scenario_handle is None:
            return None
        return next(
            (scenario for scenario in self._scenarios if scenario.handle == self._scenario_handle),
            None,
        )

    def _on_scenario_changed(self, *_args) -> None:
        if self._updating_scenarios:
            return
        selected = self._scenario_at_selection()
        self._scenario_handle = selected.handle if selected is not None else None
        select_scenario(self.manager, self._scenario_handle, source=self)
        self._update_scenario_actions()
        self._validate_controls(show_message=True)

    def planning_scenario_changed(self, handle: str | None) -> None:
        """Follow scenario selections made in another planning view."""
        self._scenario_handle = handle
        self._populate_scenarios()
        self._update_scenario_actions()
        self.schedule_refresh()

    def _update_scenario_actions(self) -> None:
        selected = self._selected_scenario()
        enabled = self.db is not None and selected is not None
        self.scenario_events_box.set_sensitive(enabled)
        self.add_estimate_button.set_sensitive(enabled)
        self.alter_schedule_button.set_sensitive(enabled)
        self.suppress_schedule_button.set_sensitive(enabled)
        if selected is None:
            self.scenario_hint.set_text(
                "Base scenario is unchanged by scenario events. Choose or create a saved scenario "
                "to add, alter, or suppress an event."
            )
        else:
            count = len(selected.schedule_overrides)
            self.scenario_hint.set_text(
                f"{count} scenario-specific recurring change(s); Base scenario remains unchanged."
            )

    def _on_new_scenario(self, _button) -> None:
        if self.db is None:
            return
        from ...gen.lib import Assumptions, Scenario
        from ..dialogs.scenario_dialog import SaveScenarioDialog

        base = baseline_scenario(self.manager, self.db)
        scenario = Scenario(
            start=self._start_date,
            years=max(1, self._end_date.year - self._start_date.year + 1),
            basis=base.basis,
            assumptions=Assumptions.from_dict(base.assumptions.serialize()),
        )
        SaveScenarioDialog(self.get_root(), self.db, scenario).present()

    def _on_manage_scenarios(self, _button) -> None:
        if self.db is None:
            return
        from ..dialogs.scenario_manager_dialog import ScenarioManagerDialog

        ScenarioManagerDialog(self.get_root(), self.db, self.manager).present()

    def _require_scenario(self):
        selected = self._selected_scenario()
        if selected is None:
            self.scenario_hint.set_text(
                "Select a saved scenario first; Base scenario itself is never modified by "
                "scenario events."
            )
        return selected

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
        scenario.schedule_overrides.append(ScenarioSchedule.from_scheduled(schedule, enabled=False))
        with self.db.transaction(f"Update scenario {scenario.name}") as txn:
            self.db.commit_scenario(scenario, txn)

    def _grouping(self) -> ReportingPeriod:
        return (
            ReportingPeriod.MONTH,
            ReportingPeriod.QUARTER,
            ReportingPeriod.YEAR,
        )[self._period_index]

    @staticmethod
    def _month_end(year: int, month: int) -> date:
        return date(year, month, monthrange(year, month)[1])

    @staticmethod
    def _previous_month(when: date) -> date:
        if when.month == 1:
            return date(when.year - 1, 12, 1)
        return date(when.year, when.month - 1, 1)

    def _earliest_data_date(self) -> date:
        today = date.today()
        if self.db is None:
            return date(today.year, 1, 1)
        dates: list[date] = []
        dates.extend(transaction.post_date for transaction in self.db.iter_transactions())
        dates.extend(schedule.recurrence.start for schedule in self.db.iter_scheduled())
        for scenario in self.db.iter_scenarios():
            dates.extend(item.recurrence.start for item in scenario.schedule_overrides)
            dates.extend(item.when for item in scenario.one_offs)
        return min(dates, default=date(today.year, 1, 1))

    def _maximum_through_month(self) -> date:
        today = date.today()
        anniversary_year = today.year + 150
        anniversary = date(
            anniversary_year,
            today.month,
            min(today.day, monthrange(anniversary_year, today.month)[1]),
        )
        candidate = date(anniversary.year, anniversary.month, 1)
        if self._month_end(candidate.year, candidate.month) > anniversary:
            candidate = self._previous_month(candidate)
        return candidate

    def _initialize_range_bounds(self) -> None:
        if self.db is None or self._bounds_initialized:
            return
        minimum = self._earliest_data_date().replace(day=1)
        maximum = self._maximum_through_month()
        self.start_year.set_range(minimum.year, maximum.year)
        self.end_year.set_range(minimum.year, maximum.year)
        if self._start_date < minimum:
            self._start_date = minimum
        if self._end_date < self._start_date:
            self._end_date = self._month_end(self._start_date.year, self._start_date.month)
        maximum_end = self._month_end(maximum.year, maximum.month)
        if self._end_date > maximum_end:
            self._end_date = maximum_end
        self._updating_controls = True
        try:
            self.start_year.set_value(self._start_date.year)
            self.start_month.set_selected(self._start_date.month - 1)
            self.end_year.set_value(self._end_date.year)
            self.end_month.set_selected(self._end_date.month - 1)
        finally:
            self._updating_controls = False
        self._bounds_initialized = True

    def _pending_range(self) -> tuple[date, date]:
        start_year = self.start_year.get_value_as_int()
        start_month = self.start_month.get_selected() + 1
        end_year = self.end_year.get_value_as_int()
        end_month = self.end_month.get_selected() + 1
        start = date(start_year, start_month, 1)
        end = self._month_end(end_year, end_month)
        return start, end

    def _validate_controls(self, *, show_message: bool) -> bool:
        if self.db is None:
            return False
        start, end = self._pending_range()
        minimum = self._earliest_data_date().replace(day=1)
        maximum_month = self._maximum_through_month()
        maximum = self._month_end(maximum_month.year, maximum_month.month)
        message = ""
        if start < minimum:
            message = f"From cannot be earlier than the first book data ({minimum:%b %Y})."
        elif end < start:
            message = "Through must be the same month as From or later."
        elif end > maximum:
            message = f"Through cannot be later than {maximum_month:%b %Y}."
        self.apply_button.set_sensitive(not message)
        if show_message:
            self.control_status.set_text(message)
            if message:
                self.control_status.add_css_class("negative")
            else:
                self.control_status.remove_css_class("negative")
        return not message

    def _on_controls_changed(self, *_args) -> None:
        if not self._updating_controls:
            self._validate_controls(show_message=True)

    def _on_apply(self, _button) -> None:
        if not self._validate_controls(show_message=True):
            return
        self._start_date, self._end_date = self._pending_range()
        self._period_index = self.period.get_selected()
        self._measure_index = self.measure.get_selected()
        self.control_status.set_text(
            f"Showing {self._start_date:%b %Y} through {self._end_date:%b %Y}."
        )
        self.control_status.remove_css_class("negative")
        self.refresh()

    def refresh(self) -> None:
        if self.db is None:
            return
        self._initialize_range_bounds()
        self._populate_scenarios()
        selected_scenario = self._selected_scenario() or baseline_scenario(self.manager, self.db)
        self._report = build_category_report(
            self.db,
            self._start_date,
            self._end_date,
            period=self._grouping(),
            scenario=selected_scenario,
        )
        activity = self._report.activity
        self.summary.set_text(
            f"Planned cash {activity.planned_cash_change.format(parens_negative=True)}   ·   "
            f"Actual cash {activity.actual_cash_change.format(parens_negative=True)}   ·   "
            f"Variance {self._report.cash_variance.format(parens_negative=True)}   ·   "
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
                values = (category.planned, category.actual, category.variance)[self._measure_index]
                for col, value in enumerate(values, 1):
                    period = periods[col - 1]
                    label = Gtk.Label(
                        label=(value.format(parens_negative=True) if value is not None else "—"),
                        xalign=1,
                    )
                    button = Gtk.Button()
                    button.set_child(label)
                    button.set_tooltip_text(f"Explain {category.full_name} — {period.label}")
                    button.connect("clicked", self._on_plan_cell_clicked, category, period)
                    self.grid.attach(button, col, row_index, 1, 1)
                row_index += 1

        if self._report.planning_flows:
            section = Gtk.Label(label="Planning flows", xalign=0)
            section.add_css_class("heading")
            self.grid.attach(section, 0, row_index, 1, 1)
            row_index += 1
            for flow in self._report.planning_flows:
                name = Gtk.Label(label=flow.name, xalign=0)
                name.set_tooltip_text(flow.full_name)
                self.grid.attach(name, 0, row_index, 1, 1)
                values = (flow.planned, flow.actual, flow.variance)[self._measure_index]
                for col, value in enumerate(values, 1):
                    period = periods[col - 1]
                    label = Gtk.Label(
                        label=(value.format(parens_negative=True) if value is not None else "—"),
                        xalign=1,
                    )
                    button = Gtk.Button()
                    button.set_child(label)
                    button.set_tooltip_text(f"Explain {flow.name} — {period.label}")
                    button.connect("clicked", self._on_flow_cell_clicked, flow, period)
                    self.grid.attach(button, col, row_index, 1, 1)
                row_index += 1

    def _on_flow_cell_clicked(self, _button, flow, period) -> None:
        if self.db is None:
            return
        scenario = self._selected_scenario() or baseline_scenario(self.manager, self.db)
        detail = explain_planning_flow_period(
            self.db, flow.kind, flow.account, period.start, period.end, scenario=scenario
        )
        from ..dialogs.plan_detail_dialog import PlanDetailDialog

        PlanDetailDialog(
            self.get_root(),
            self.db,
            detail,
            period_label=period.label,
            scenario_name=("Base scenario" if self._scenario_handle is None else scenario.name),
        ).present()

    def _on_plan_cell_clicked(self, _button, category, period) -> None:
        if self.db is None:
            return
        scenario = self._selected_scenario() or baseline_scenario(self.manager, self.db)
        detail = explain_category_period(
            self.db, category.account, period.start, period.end, scenario=scenario
        )
        from ..dialogs.plan_detail_dialog import PlanDetailDialog

        PlanDetailDialog(
            self.get_root(),
            self.db,
            detail,
            period_label=period.label,
            scenario_name=("Base scenario" if self._scenario_handle is None else scenario.name),
        ).present()
