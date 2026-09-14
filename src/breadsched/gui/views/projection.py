"""The projection view.

Assumption sliders sit next to the chart and recompute on release, so the effect of
a change in the return assumption is visible immediately rather than after a
dialog round trip.  A forecast is an argument about the future, and the fastest way
to understand one is to push on it.

Base scenario assumption changes are persisted in the open book. Changes to a selected
saved scenario are persisted with "Save scenario changes". Plan and Projection share
the same scenario selection so both views describe the same future.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from time import monotonic

from ...gen.db.sqlite import DbSQLite
from ...gen.engine import projection
from ...gen.lib import Assumptions, Scenario  # noqa: E402
from ...gen.utils.cancellation import OperationCancelled
from ...gen.utils.logs import get_logger  # noqa: E402
from ..background import BackgroundJob
from ..gi_setup import GLib, Gtk
from ..planning_context import (
    baseline_scenario,
    notify_planning_scenario_changed,
    persist_baseline_assumptions,
    select_scenario,
    selected_scenario_handle,
)
from ..widgets.chart import LineChart, Series  # noqa: E402
from ._base import BaseView  # noqa: E402

__all__ = ["ProjectionView"]

LOG = get_logger(__name__)

_PROGRESS_POPUP_DELAY_SECONDS = 0.5

_ASSUMPTIONS = [
    ("income_growth", "Income growth", -0.05, 0.15, 0.03),
    ("expense_inflation", "Expense inflation", -0.02, 0.15, 0.025),
    ("investment_return", "Investment return", -0.05, 0.15, 0.06),
    ("cash_interest", "Cash interest", 0.0, 0.10, 0.01),
    ("liability_interest", "Liability interest", 0.0, 0.30, 0.0),
]


class ProjectionView(BaseView):
    """Multi-year forecast with live assumptions and saved scenarios."""

    PRINTABLE = True

    WATCHES = (
        "database-changed",
        "scenario-add",
        "scenario-update",
        "scenario-delete",
        "transaction-add",
    )

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self._baseline = baseline_scenario(manager)
        self.scenario = self._baseline
        self._scenario_handle: str | None = selected_scenario_handle(manager)
        self._scales: dict[str, Gtk.Scale] = {}
        self._comparison: projection.Projection | None = None
        self._result: projection.Projection | None = None
        self._updating = False
        self._projection_dirty = True
        self._job: BackgroundJob[projection.Projection, projection.ProjectionProgress] | None = None
        self._job_generation = 0
        self._progress_started = 0.0
        self._progress_window: Gtk.Window | None = None
        self._progress_bar: Gtk.ProgressBar | None = None
        self._progress_label: Gtk.Label | None = None
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
        self.warning_label.set_selectable(True)
        self.warning_label.set_valign(Gtk.Align.START)
        for side in ("start", "end", "bottom"):
            getattr(self.warning_label, f"set_margin_{side}")(12)
        self.warning_scroller = Gtk.ScrolledWindow(child=self.warning_label)
        self.warning_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.warning_scroller.set_max_content_height(180)
        self.warning_scroller.set_propagate_natural_height(True)
        self.warning_scroller.set_vexpand(False)
        self.warning_scroller.set_visible(False)
        left.append(self.warning_scroller)

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

        self.explain_button = Gtk.Button(label="Explain month…")
        self.explain_button.set_sensitive(False)
        self.explain_button.set_tooltip_text(
            "Inspect the events, balances, and assumptions behind a projected month"
        )
        self.explain_button.connect("clicked", self._on_explain_clicked)
        bar.append(self.explain_button)

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
        self._cancel_projection(invalidate=True, wait=True)
        self._projection_dirty = True
        self._comparison = None
        self._result = None
        if hasattr(self, "explain_button"):
            self.explain_button.set_sensitive(False)
        super().set_db(db)
        if db is not None:
            self._baseline = baseline_scenario(self.manager, db)
            if self._scenario_handle is None:
                self.scenario = self._baseline

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
        finally:
            self._updating = False
        self.recompute()

    def _populate_scenarios(self) -> None:
        if self.db is None:
            return
        self._scenarios = list(self.db.iter_scenarios())
        model = Gtk.StringList()
        model.append("Base scenario")
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
        self._baseline = baseline_scenario(self.manager, self.db)
        self.scenario = chosen.clone() if chosen is not None else self._baseline
        self.scenario_picker.set_model(model)
        self.scenario_picker.set_selected(selected)
        self._load_scenario_controls()
        self.save_button.set_label(
            "Save scenario changes" if chosen is not None else "Save base as scenario"
        )

    def _load_scenario_controls(self) -> None:
        self.years_spin.set_value(self.scenario.years)
        assumptions = self.scenario.effective_assumptions()
        for key, scale in self._scales.items():
            scale.set_value(float(getattr(assumptions, key)))
            source = self.scenario.assumption_sources(self.scenario.start)[key]
            scale.set_tooltip_text(f"Effective value from {source}.")

    def planning_scenario_changed(self, handle: str | None) -> None:
        """Follow the scenario selected in Plan without eagerly projecting hidden data."""
        self._scenario_handle = handle
        self._projection_dirty = True
        if self._is_visible():
            self.schedule_refresh()

    def _collect(self) -> Scenario:
        """Read controls into the selected draft without sharing saved DB objects."""
        self.scenario.years = int(self.years_spin.get_value())
        rates = {
            key: Decimal(str(round(scale.get_value(), 4))) for key, scale in self._scales.items()
        }
        updated = Assumptions(
            income_growth=rates["income_growth"],
            expense_inflation=rates["expense_inflation"],
            investment_return=rates["investment_return"],
            cash_interest=rates["cash_interest"],
            liability_interest=rates["liability_interest"],
            per_account=self.scenario.assumptions.per_account,
        )
        if self.scenario.inherits_base_assumptions:
            previous = self.scenario.effective_assumptions()
            for key in rates:
                value = getattr(updated, key)
                if value != getattr(previous, key):
                    self.scenario.set_assumption_override(key, value)
        else:
            self.scenario.assumptions = updated
        if self.scenario.start is None:
            self.scenario.start = date.today().replace(day=1)
        return self.scenario

    def recompute(self) -> None:
        if self.db is None:
            return
        self._projection_dirty = False
        self._calculate(self._collect(), self._render)

    def _calculate(
        self,
        scenario: Scenario,
        on_success: Callable[[projection.Projection], None],
    ) -> None:
        """Calculate from a read-only worker snapshot and deliver on GTK's loop."""
        if self.db is None:
            return
        self._cancel_projection()
        self._job_generation += 1
        generation = self._job_generation
        path = self.db.path
        source_db = self.db
        snapshot = scenario.clone()
        self._progress_started = monotonic()
        self.warning_label.remove_css_class("negative")
        self._show_projection_notes(["Calculating projection…"])

        def work(cancel, report) -> projection.Projection:
            worker_db = source_db
            owns_worker = False
            if path is not None and path != ":memory:":
                worker_db = DbSQLite()
                worker_db.load(path, "r")
                owns_worker = True

            def progress_callback(update: projection.ProjectionProgress) -> None:
                if cancel.is_set():
                    raise OperationCancelled()
                report(update)

            try:
                return projection.project(worker_db, snapshot, progress=progress_callback)
            finally:
                if owns_worker:
                    worker_db.close()

        self._job = BackgroundJob()
        self._job.start(
            work,
            lambda update: self._projection_progress(generation, update),
            lambda result: self._projection_succeeded(generation, result, on_success),
            lambda exc: self._projection_failed(generation, exc),
        )

    def _projection_progress(self, generation: int, update: projection.ProjectionProgress) -> None:
        if generation != self._job_generation:
            return
        if self._progress_window is None:
            elapsed = monotonic() - self._progress_started
            if elapsed < _PROGRESS_POPUP_DELAY_SECONDS or update.fraction >= 1.0:
                return
            self._open_progress()
        assert self._progress_bar is not None
        assert self._progress_label is not None
        self._progress_bar.set_fraction(update.fraction)
        self._progress_bar.set_text(f"{update.fraction:.0%}")
        self._progress_label.set_text(
            f"{update.phase}: {update.current:%b %d, %Y} of {update.end:%b %d, %Y}"
        )

    def _projection_succeeded(
        self,
        generation: int,
        result: projection.Projection,
        on_success: Callable[[projection.Projection], None],
    ) -> None:
        if generation != self._job_generation:
            return
        self._close_progress()
        self.warning_label.remove_css_class("negative")
        on_success(result)

    def _projection_failed(self, generation: int, exc: BaseException) -> None:
        if generation != self._job_generation:
            return
        self._close_progress()
        if isinstance(exc, OperationCancelled):
            self._projection_dirty = True
            self._show_projection_notes(["Projection cancelled."])
            return
        LOG.error("projection failed", exc_info=(type(exc), exc, exc.__traceback__))
        self._result = None
        self.explain_button.set_sensitive(False)
        self.chart.set_data([], [])
        self._show_projection_notes([f"The projection could not be calculated: {exc}"])
        self.warning_label.add_css_class("negative")

    def _cancel_projection(self, invalidate: bool = False, wait: bool = False) -> None:
        job = self._job
        if job is not None and job.active:
            job.cancel()
        if invalidate:
            self._job_generation += 1
        self._close_progress()
        if wait and job is not None:
            job.wait()

    def _open_progress(self) -> None:
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
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self._cancel_projection())
        box.append(cancel)
        window.set_child(box)
        window.present()
        self._progress_window = window
        self._progress_bar = bar
        self._progress_label = label

    def _close_progress(self) -> None:
        if self._progress_window is not None:
            self._progress_window.close()
        self._progress_window = None
        self._progress_bar = None
        self._progress_label = None

    def wait_for_background(self, timeout: float = 10.0) -> bool:
        """Wait for the current calculation; deterministic support for GUI tests."""
        return self._job is None or self._job.wait(timeout)

    def _render(self, result: projection.Projection) -> None:
        self._result = result
        self.explain_button.set_sensitive(bool(result.rows))
        labels = [row.label for row in result.rows]
        series = [
            Series("Cash", [float(r.cash_close.to_decimal()) for r in result.rows], fill=True),
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
        self._show_projection_notes(result.warnings, bullets=True)

    def _show_projection_notes(self, notes: list[str], *, bullets: bool = False) -> None:
        """Render readable notes without letting them dictate the window height."""
        visible = [note.strip() for note in notes if note.strip()]
        text = "\n\n".join(f"• {note}" if bullets else note for note in visible)
        self.warning_label.set_text(text)
        self.warning_scroller.set_visible(bool(text))

    def printable_html(self) -> str | None:
        """Return the current projection calculation and visible comparison."""
        if self._result is None:
            return None
        from pathlib import Path

        from ...plugins.export.html_report import projection_report

        application = self.manager.get_application()
        book_path = getattr(application, "book_path", None)
        return projection_report(
            self._result,
            comparison=self._comparison,
            book_name=Path(book_path).name if book_path else "",
        )

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
        if self._updating:
            return
        if self._scenario_handle is None:
            self._collect()
            if self.db is not None:
                persist_baseline_assumptions(self.manager, self.db)
                for saved in self._scenarios:
                    saved.attach_base_assumptions(self.scenario.assumptions)
            notify_planning_scenario_changed(self.manager, source=self)
        self.recompute()

    def _on_scenario_chosen(self, picker, _param) -> None:
        if self._updating or self.db is None:
            return
        index = picker.get_selected()
        chosen = self._scenarios[index - 1] if index > 0 else None
        if chosen is not None:
            chosen = self.db.get_scenario(chosen.handle)
            if chosen is None:
                self.schedule_refresh()
                return
        self._scenario_handle = chosen.handle if chosen is not None else None
        select_scenario(self.manager, self._scenario_handle, source=self)
        self.scenario = chosen.clone() if chosen is not None else self._baseline
        self._updating = True
        try:
            self._load_scenario_controls()
            self.save_button.set_label(
                "Save scenario changes" if chosen is not None else "Save base as scenario"
            )
        finally:
            self._updating = False
        self.recompute()

    def _on_explain_clicked(self, _button) -> None:
        if self.db is None or self._result is None:
            return
        from ..dialogs.projection_detail_dialog import ProjectionDetailDialog

        ProjectionDetailDialog(self.get_root(), self.db, self._result).present()

    def _on_compare_clicked(self, _button) -> None:
        if self.db is None or not getattr(self, "_scenarios", []):
            return
        names = [s.name for s in self._scenarios]
        picker = Gtk.DropDown.new_from_strings(["None", *names])
        dialog = Gtk.Window(title="Compare with", transient_for=self.get_root(), modal=True)
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
            if index == 0:
                self._comparison = None
                self.recompute()
                return

            def comparison_ready(result: projection.Projection) -> None:
                self._comparison = result
                self.recompute()

            self._calculate(self._scenarios[index - 1], comparison_ready)

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

        draft = Scenario.derived_from_base(
            scenario.effective_assumptions(),
            start=scenario.start,
            years=scenario.years,
        )
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

            def export(calculated: projection.Projection) -> None:
                export_projection(calculated, file.get_path())

            self._calculate(self._collect(), export)

        dialog.save(self.get_root(), None, on_saved)
