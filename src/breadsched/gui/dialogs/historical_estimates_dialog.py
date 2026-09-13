"""Review estimates inferred from historical category activity."""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite
from ...gen.engine import estimates
from ..gi_setup import Gtk

__all__ = ["HistoricalEstimatesDialog"]


class HistoricalEstimatesDialog(Gtk.Window):
    """Offer historical category estimates without applying them silently."""

    def __init__(self, parent: Gtk.Window, db: DbSQLite) -> None:
        super().__init__(title="Suggest estimates from history", transient_for=parent)
        self.db = db
        self.set_default_size(760, 520)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        self.set_child(outer)

        controls = Gtk.Box(spacing=8)
        controls.append(Gtk.Label(label="Completed months", xalign=0))
        adjustment = Gtk.Adjustment(
            value=12, lower=3, upper=120, step_increment=1, page_increment=12
        )
        self.months = Gtk.SpinButton(adjustment=adjustment, numeric=True)
        controls.append(self.months)

        self.scenarios = list(db.iter_scenarios())
        self.target = Gtk.DropDown.new_from_strings(
            ["Base", *[scenario.name for scenario in self.scenarios]]
        )
        controls.append(Gtk.Label(label="Add to", xalign=0))
        controls.append(self.target)
        self.target.connect("notify::selected", self._on_target_changed)

        analyze = Gtk.Button(label="Analyze")
        analyze.connect("clicked", self._on_analyze)
        controls.append(analyze)
        outer.append(controls)

        note = Gtk.Label(
            label=(
                "Suggestions use completed historical activity after known schedules. "
                "Review opens the normal editor; nothing is added until you choose Save."
            ),
            xalign=0,
            wrap=True,
        )
        note.add_css_class("dim")
        outer.append(note)

        self.status = Gtk.Label(xalign=0, wrap=True)
        outer.append(self.status)
        self.rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scroller = Gtk.ScrolledWindow(child=self.rows)
        scroller.set_vexpand(True)
        outer.append(scroller)

        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        outer.append(close)
        self._reload()

    def _scenario_handle(self) -> str | None:
        selected = self.target.get_selected()
        return None if selected == 0 else self.scenarios[selected - 1].handle

    def _clear_rows(self) -> None:
        child = self.rows.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.rows.remove(child)
            child = following

    def _reload(self) -> None:
        self._clear_rows()
        proposals = estimates.propose_historical_estimates(
            self.db,
            months=self.months.get_value_as_int(),
            scenario_handle=self._scenario_handle(),
        )
        if not proposals:
            self.status.set_text("No categories have enough completed history yet.")
            return
        self.status.set_text(f"{len(proposals)} proposal(s)")
        for proposal in proposals:
            row = Gtk.Box(spacing=10)
            add = Gtk.Button(label="Review…")
            add.connect("clicked", self._on_add, proposal)
            row.append(add)
            label = Gtk.Label(
                label=(
                    f"{proposal.display_amount.format()} "
                    f"{proposal.recurrence.describe()}: "
                    f"{proposal.source_name} → {proposal.destination_name}\n"
                    f"{proposal.reason}; "
                    f"confidence {proposal.confidence:.0%}"
                ),
                xalign=0,
                wrap=True,
            )
            label.set_hexpand(True)
            row.append(label)
            self.rows.append(row)

    def _on_analyze(self, _button) -> None:
        self._reload()

    def _on_target_changed(self, *_args) -> None:
        self._reload()

    def _on_add(self, button, proposal) -> None:
        try:
            scenario_handle = self._scenario_handle()
            if scenario_handle is None:
                from .schedule_dialog import ScheduleDialog

                draft = estimates.draft_historical_estimate(self.db, proposal)
                dialog = ScheduleDialog(self, self.db, source=draft, creating=True)
            else:
                from .scenario_schedule_dialog import ScenarioScheduleDialog

                scenario = self.db.get_scenario(scenario_handle)
                if scenario is None:
                    raise ValueError("saved scenario no longer exists")
                draft = estimates.draft_scenario_estimate(self.db, proposal)
                dialog = ScenarioScheduleDialog(self, self.db, scenario, current=draft)
        except ValueError as exc:
            self.status.set_text(str(exc))
            return
        button.set_sensitive(False)

        def finished(*_args) -> bool:
            button.set_sensitive(True)
            self._reload()
            return False

        dialog.connect("close-request", finished)
        dialog.present()
        self.status.set_text(f"Review the estimate for {proposal.category_name}, then Save.")
