"""The projection view.

Assumption sliders sit next to the chart and recompute on release, so the effect of
a change in the return assumption is visible immediately rather than after a
dialog round trip.  A forecast is an argument about the future, and the fastest way
to understand one is to push on it.

Baseline assumption changes are a session draft until saved as a scenario. Changes to
a selected saved scenario are persisted with "Save scenario changes". Plan and
Projection share the same scenario selection so both views describe the same future.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from time import monotonic

from ...gen.db.sqlite import DbSQLite
from ...gen.engine import projection
from ...gen.lib import Assumptions, ProjectionBasis, Scenario  # noqa: E402
from ...gen.utils.logs import get_logger  # noqa: E402
from ..gi_setup import GLib, Gtk
from ..planning_context import (
    baseline_scenario,
    select_scenario,
    selected_scenario_handle,
)
from ..widgets.chart import LineChart, Series  # noqa: E402
from ._base import BaseView  # noqa: E402

__all__ = ["ProjectionView"]

LOG = get_logger(__name__)

_PROGRESS_POPUP_DELAY_SECONDS = 0.5

_BASIS_ORDER = [
    ProjectionBasis.SCHEDULED,
    ProjectionBasis.BUDGET,
    ProjectionBasis.COMBINED,
]

_ASSUMPTIONS = [
    ("income_growth", "Income growth", -0.05, 0.15, 0.03),
    ("expense_inflation", "Expense inflation", -0.02, 0.15, 0.025),
    ("investment_return", "Investment return", -0.05, 0.15, 0.06),
    ("cash_interest", "Cash interest", 0.0, 0.10, 0.01),
    ("liability_interest", "Liability interest", 0.0, 0.30, 0.0),
]


class ProjectionView(BaseView):
    """Multi-year forecast with live assumptions and saved scenarios."""

    WATCHES = ("database-changed", "scenario-add", "scenario-update", "scenario-delete",
               "budget-update", "transaction-add")

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self._baseline = baseline_scenario(manager)
        self.scenario = self._baseline
        self._scenario_handle: str | None = selected_scenario_handle(manager)
        self._scales: dict[str, Gtk.Scale] = {}
        self._comparison: projection.Projection | None = None
        self._updating = False
        self._projection_dirty = True
        self._build()

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        self.append(self._build_toolbar())

        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        split.set_position(880)
        split.set_vexpand(True)
        self.append(split)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(right, f"set_margin_{side}")(12)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.summary = Gtk.Box(spacing=18)
        for side in ("start", "end", "top"):
            getattr(self.summary, f"set_margin_{side}")(12)
        left.append(self.summary)

        self.chart = LineChart()
        for side in ("start", "end", "bottom"):
            getattr(self.chart, f"set_margin_{side}")(12)
        left.append(self.chart)

        self.warning_label = Gtk.Label(xalign=0)
        self.warning_label.add_css_class("dim")
        self.warning_label.set_wrap(True)
        for side in ("start", "end", "bottom"):
            getattr(self.warning_label, f"set_margin_{side}")(12)
        left.append(self.warning_label)

        split.set_start_child(left)
        split.set_end_child(self._build_controls(right))

    def _build_toolbar(self) -> Gtk.Box:
        bar = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{side}")(8)

        self.scenario_picker = Gtk.DropDown()
        self.scenario_picker.connect("notify::selected", self._on_scenario_chosen)
        bar.append(Gtk.Label(label="Scenario"))
        bar.append(self.scenario_picker)

        compare_button = Gtk.Button(label="Compare with…")
        compare_button.connect("clicked", self._on_compare_clicked)
        bar.append(compare_button)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)

        self.save_button = Gtk.Button(label="Save as scenario")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._on_save_clicked)
        bar.append(self.save_button)

        export_button = Gtk.Button(icon_name="document-save-symbolic")
        export_button.set_tooltip_text("Export the monthly rows as CSV")
        export_button.connect("clicked", self._on_export_clicked)
        bar.append(export_button)
        return bar

    def _build_controls(self, box: Gtk.Box) -> Gtk.Widget:
        title = Gtk.Label(label="Assumptions", xalign=0)
        title.add_css_class("category-title")
        box.append(title)

        for key, label, low, high, default in _ASSUMPTIONS:
            box.append(self._scale_row(key, label, low, high, default))

        box.append(Gtk.Separator())

        years_box = Gtk.Box(spacing=8)
        years_box.append(Gtk.Label(label="Years", xalign=0))
        # Retirement and mortgage questions are asked in decades, so the range
        # runs to a hundred years rather than forty.
        self.years_spin = Gtk.SpinButton.new_with_range(1, 100, 1)
        self.years_spin.set_value(self.scenario.years)
        self.years_spin.connect("value-changed", self._on_input_changed)
        years_box.append(self.years_spin)
        box.append(years_box)

        basis_box = Gtk.Box(spacing=8)
        basis_box.append(Gtk.Label(label="Driven by", xalign=0))
        self.basis_picker = Gtk.DropDown.new_from_strings(
            ["Scheduled events", "Legacy budget", "Legacy budget + schedules"]
        )
        self.basis_picker.set_selected(_BASIS_ORDER.index(self.scenario.basis))
        self.basis_picker.connect("notify::selected", self._on_input_changed)
        basis_box.append(self.basis_picker)
        box.append(basis_box)

        budget_box = Gtk.Box(spacing=8)
        budget_box.append(Gtk.Label(label="Budget", xalign=0))
        self.budget_picker = Gtk.DropDown()
        self.budget_picker.connect("notify::selected", self._on_input_changed)
        budget_box.append(self.budget_picker)
        box.append(budget_box)

        scroller = Gtk.ScrolledWindow(child=box)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        return scroller

    def _scale_row(self, key, label, low, high, default) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        caption = Gtk.Label(label=label, xalign=0)
        caption.add_css_class("summary-label")
        row.append(caption)

        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, low, high, 0.0025)
        scale.set_value(default)
        scale.set_draw_value(True)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.set_format_value_func(lambda _s, value: f"{value:.2%}")
        scale.set_hexpand(True)
        scale.connect("value-changed", self._on_input_changed)
        self._scales[key] = scale
        row.append(scale)
        return row

    # ------------------------------------------------------------------- model

    def set_db(self, db: DbSQLite | None) -> None:
        """Attach a book and invalidate, but do not project while hidden."""
        self._projection_dirty = True
        self._comparison = None
        super().set_db(db)

    def _is_visible(self) -> bool:
        """Return whether this is the category currently shown by the manager."""
        return self.manager.stack.get_visible_child() is self

    def _on_change(self, *_args) -> None:
        """Invalidate projections without eagerly rebuilding a hidden view."""
        self._projection_dirty = True
        if self._is_visible():
            self.schedule_refresh()

    def refresh(self) -> None:
        if self.db is None:
            return
        if not self._is_visible():
            self._projection_dirty = True
            return
        if not self._projection_dirty:
            return
        self._updating = True
        try:
            self._populate_scenarios()
            self._populate_budgets()
        finally:
            self._updating = False
        self.recompute()

    def _populate_scenarios(self) -> None:
        self._scenarios = list(self.db.iter_scenarios())
        model = Gtk.StringList()
        model.append("Baseline")
        selected = 0
        chosen = None
        for index, scenario in enumerate(self._scenarios, 1):
            model.append(scenario.name)
            if scenario.handle == self._scenario_handle:
                selected = index
                chosen = scenario
        if self._scenario_handle is not None and chosen is None:
            self._scenario_handle = None
            select_scenario(self.manager, None, source=self)
        self.scenario = chosen or self._baseline
        self.scenario_picker.set_model(model)
        self.scenario_picker.set_selected(selected)
        self._load_scenario_controls()
        self.save_button.set_label(
            "Save scenario changes" if chosen is not None else "Save as scenario"
        )

    def _load_scenario_controls(self) -> None:
        self.years_spin.set_value(self.scenario.years)
        self.basis_picker.set_selected(_BASIS_ORDER.index(self.scenario.basis))
        for key, scale in self._scales.items():
            scale.set_value(float(getattr(self.scenario.assumptions, key)))

    def planning_scenario_changed(self, handle: str | None) -> None:
        """Follow the scenario selected in Plan without eagerly projecting hidden data."""
        self._scenario_handle = handle
        self._projection_dirty = True
        if self._is_visible():
            self.schedule_refresh()

    def _populate_budgets(self) -> None:
        self._budgets = list(self.db.iter_budgets())
        model = Gtk.StringList()
        model.append("None")
        for budget in self._budgets:
            model.append(budget.name)
        self.budget_picker.set_model(model)
        if (
            self.scenario.budget is None
            and self._budgets
            and self.scenario.basis is not ProjectionBasis.SCHEDULED
        ):
            self.scenario.budget = self._budgets[0].handle
        for index, budget in enumerate(self._budgets):
            if budget.handle == self.scenario.budget:
                self.budget_picker.set_selected(index + 1)
                break

    def _collect(self) -> Scenario:
        """Read the controls back into the working scenario."""
        self.scenario.years = int(self.years_spin.get_value())
        self.scenario.basis = _BASIS_ORDER[self.basis_picker.get_selected()]
        selected = self.budget_picker.get_selected()
        self.scenario.budget = (
            self._budgets[selected - 1].handle
            if 0 < selected <= len(getattr(self, "_budgets", []))
            else None
        )
        self.scenario.assumptions = Assumptions(
            **{
                key: Decimal(str(round(scale.get_value(), 4)))
                for key, scale in self._scales.items()
            }
        )
        if self.scenario.start is None:
            self.scenario.start = date.today().replace(day=1)
        return self.scenario

    def recompute(self) -> None:
        if self.db is None:
            return
        self._projection_dirty = False
        try:
            result = self._project_with_progress(self._collect())
        except Exception as exc:  # noqa: BLE001 - shown to the user, and logged
            # A failure here used to escape into the signal handler that triggered
            # it, where GTK prints it and carries on. The chart kept whatever it
            # had -- nothing -- and reported "not enough data to plot", which
            # describes the symptom and hides the cause.
            LOG.exception("projection failed")
            self.chart.set_data([], [])
            self.warning_label.set_text(
                f"The projection could not be calculated: {exc}"
            )
            self.warning_label.add_css_class("negative")
            return
        self.warning_label.remove_css_class("negative")
        self._render(result)

    def _project_with_progress(self, scenario: Scenario) -> projection.Projection:
        """Calculate one scenario while showing position through its horizon."""
        if self.db is None:
            raise RuntimeError("no book is open")

        progress_window: Gtk.Window | None = None
        progress_bar: Gtk.ProgressBar | None = None
        progress_label: Gtk.Label | None = None
        started = monotonic()
        last_fraction = -1.0
        last_phase = ""

        def on_progress(update: projection.ProjectionProgress) -> None:
            nonlocal progress_window, progress_bar, progress_label
            nonlocal last_fraction, last_phase

            # Fast projections should feel instantaneous rather than flashing a
            # modal window.  Create the popup lazily only after calculation has
            # taken long enough that visible feedback is useful.  If the first
            # callback after the delay is completion, there is nothing useful to
            # display and we avoid a one-frame 100% popup.
            if progress_window is None:
                elapsed = monotonic() - started
                if elapsed < _PROGRESS_POPUP_DELAY_SECONDS or update.fraction >= 1.0:
                    return
                progress_window, progress_bar, progress_label = self._open_progress()

            if (
                update.phase == last_phase
                and update.fraction < 1.0
                and update.fraction - last_fraction < 0.002
            ):
                return
            last_fraction = update.fraction
            last_phase = update.phase
            assert progress_bar is not None
            assert progress_label is not None
            progress_bar.set_fraction(update.fraction)
            progress_bar.set_text(f"{update.fraction:.0%}")
            progress_label.set_text(
                f"{update.phase}: {update.current:%b %d, %Y} of {update.end:%b %d, %Y}"
            )
            # Projection remains synchronous so a control change has a
            # deterministic result before its signal handler returns. Pumping the
            # main context keeps the modal progress window painted between engine
            # checkpoints without coupling the financial model to GTK or threads.
            context = GLib.MainContext.default()
            while context.pending():
                context.iteration(False)

        try:
            return projection.project(self.db, scenario, progress=on_progress)
        finally:
            if progress_window is not None:
                progress_window.close()

    def _open_progress(self) -> tuple[Gtk.Window, Gtk.ProgressBar, Gtk.Label]:
        """Create and paint the projection calculation popup."""
        root = self.get_root()
        window = Gtk.Window(title="Calculating projection…", modal=True)
        if isinstance(root, Gtk.Window):
            window.set_transient_for(root)
            window.set_destroy_with_parent(True)
        window.set_default_size(440, -1)
        window.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        label = Gtk.Label(label="Preparing projection…", xalign=0)
        label.set_wrap(True)
        box.append(label)
        bar = Gtk.ProgressBar(show_text=True)
        bar.set_text("0%")
        box.append(bar)
        window.set_child(box)
        window.present()

        # Make sure the popup is visible before event expansion starts.
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)
        return window, bar, label

    def _render(self, result: projection.Projection) -> None:
        labels = [row.label for row in result.rows]
        series = [
            Series("Cash", [float(r.cash_close.to_decimal()) for r in result.rows],
                   fill=True),
            Series("Investments", [float(r.holdings.to_decimal()) for r in result.rows]),
            Series("Net worth", [float(r.net_worth.to_decimal()) for r in result.rows]),
        ]
        if self._comparison is not None:
            series.append(
                Series(
                    f"{self._comparison.scenario.name} net worth",
                    [float(r.net_worth.to_decimal()) for r in self._comparison.rows],
                )
            )
        self.chart.set_data(series, labels)

        shortfall = result.first_shortfall()
        cards = [
            ("Ending net worth", result.ending_net_worth.format(parens_negative=True)),
            ("Ending cash", result.ending_cash.format(parens_negative=True)),
            ("Lowest cash", result.minimum_cash.format(parens_negative=True)),
            ("Total growth", result.total("investment_growth").format()),
            ("Cash runs out", shortfall.label if shortfall else "Never"),
        ]
        self._render_summary(cards, alarm=shortfall is not None)
        self.warning_label.set_text("  ".join(result.warnings))

    def _render_summary(self, cards, alarm: bool) -> None:
        child = self.summary.get_first_child()
        while child is not None:
            self.summary.remove(child)
            child = self.summary.get_first_child()
        for index, (label, value) in enumerate(cards):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.add_css_class("card")
            caption = Gtk.Label(label=label, xalign=0)
            caption.add_css_class("summary-label")
            amount = Gtk.Label(label=value, xalign=0)
            amount.add_css_class("summary-value")
            amount.add_css_class("numeric")
            if value.startswith(("(", "-")) or (alarm and index == len(cards) - 1):
                amount.add_css_class("negative")
            box.append(caption)
            box.append(amount)
            self.summary.append(box)

    # ----------------------------------------------------------------- actions

    def _on_input_changed(self, *_args) -> None:
        if not self._updating:
            self.recompute()

    def _on_scenario_chosen(self, picker, _param) -> None:
        if self._updating or self.db is None:
            return
        index = picker.get_selected()
        chosen = self._scenarios[index - 1] if index > 0 else None
        self._scenario_handle = chosen.handle if chosen is not None else None
        select_scenario(self.manager, self._scenario_handle, source=self)
        self.scenario = chosen or self._baseline
        self._updating = True
        try:
            self._load_scenario_controls()
            self._populate_budgets()
            self.save_button.set_label(
                "Save scenario changes" if chosen is not None else "Save as scenario"
            )
        finally:
            self._updating = False
        self.recompute()

    def _on_compare_clicked(self, _button) -> None:
        if self.db is None or not getattr(self, "_scenarios", []):
            return
        names = [s.name for s in self._scenarios]
        picker = Gtk.DropDown.new_from_strings(["None", *names])
        dialog = Gtk.Window(
            title="Compare with", transient_for=self.get_root(), modal=True
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(16)
        box.append(Gtk.Label(label="Overlay another scenario's net worth"))
        box.append(picker)
        apply_button = Gtk.Button(label="Apply")
        apply_button.add_css_class("suggested-action")

        def on_apply(_b) -> None:
            index = picker.get_selected()
            dialog.close()
            self._comparison = (
                self._project_with_progress(self._scenarios[index - 1])
                if index > 0 else None
            )
            self.recompute()

        apply_button.connect("clicked", on_apply)
        box.append(apply_button)
        dialog.set_child(box)
        dialog.present()

    def _on_save_clicked(self, _button) -> None:
        if self.db is None:
            return
        scenario = self._collect()
        if self._scenario_handle is not None:
            with self.db.transaction(f"Update scenario {scenario.name}") as txn:
                self.db.commit_scenario(scenario, txn)
            return

        from ..dialogs.scenario_dialog import SaveScenarioDialog

        draft = Scenario.from_dict(scenario.serialize())
        draft.name = ""
        SaveScenarioDialog(self.get_root(), self.db, draft).present()

    def _on_export_clicked(self, _button) -> None:
        if self.db is None:
            return
        dialog = Gtk.FileDialog(title="Export projection", initial_name="projection.csv")

        def on_saved(file_dialog, result) -> None:
            try:
                file = file_dialog.save_finish(result)
            except GLib.Error:
                return
            from ...plugins.export.csv_export import export_projection

            export_projection(
                self._project_with_progress(self._collect()), file.get_path()
            )

        dialog.save(self.get_root(), None, on_saved)
