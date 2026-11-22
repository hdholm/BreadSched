"""Edit recurring estimates that belong to one saved scenario.

Scenario schedules never mutate the baseline book schedule they replace.  They are
forecast inputs only: the resolved scenario event stream is consumed by both Plan
and Projection, while actual ledger posting continues to use the baseline book.

The editor is the schedule editor (``schedule_dialog``), its splits on the same
split grid, without what only a posting carries: its kind, number, notes, tags,
documents, automatic posting, and days in advance.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import cast

from ...api import ApiError
from ...api.local import book_server
from ...gen.lib import Scenario, ScenarioSchedule, ScheduledTransaction
from ...gen.services import (
    FixedScheduleInput,
    FormulaScenarioScheduleInput,
    SaveFixedScenarioSchedule,
)
from ..gi_setup import Gtk
from ..remote_book import OpenBook
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from .schedule_dialog import _GROWTH_POLICIES, ScheduleDialog

__all__ = ["ScenarioScheduleDialog", "ScenarioSchedulePickerDialog"]


class ScenarioScheduleDialog(ScheduleDialog):
    """Create a scenario estimate or replace one baseline schedule."""

    POSTS = False

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: OpenBook,
        scenario: Scenario,
        source: ScheduledTransaction | None = None,
        current: ScenarioSchedule | None = None,
    ) -> None:
        self.scenario = scenario
        #: The baseline schedule this replaces, or None for an estimate of its own.
        self.baseline = source
        self.current = current
        # A scenario estimate has a schedule's every field the editor reads for an
        # estimate (POSTS false); it is loaded as one.
        super().__init__(parent, db, cast(ScheduledTransaction, current or source), creating=True)
        self.set_title("Alternate scheduled transaction" if source else "New scenario estimate")

    def _build_shell(self) -> tuple[Gtk.Box, Gtk.Box]:
        box, content = super()._build_shell()
        explanation = Gtk.Label(xalign=0, wrap=True)
        explanation.add_css_class("dim")
        if self.baseline is None:
            explanation.set_text(
                f"This recurring estimate belongs only to scenario “{self.scenario.name}”. "
                "It affects Plan and Projection but is never posted to the ledger."
            )
        else:
            explanation.set_text(
                f"This replaces “{self.baseline.name}” only while scenario "
                f"“{self.scenario.name}” is selected. The baseline schedule is not changed."
            )
        content.prepend(explanation)
        return box, content

    def _stored_currency(self) -> str | None:
        return self.baseline.currency if self.baseline is not None else None

    # ----------------------------------------------------------------- saving

    def _draft_fixed(self) -> ScenarioSchedule:
        return self.book.draft_fixed_scenario_schedule(self._fixed_request())

    @property
    def book(self):
        return book_server(self.db)

    def _definition(self) -> FixedScheduleInput:
        """The schedule editor's definition, kept an estimate (or the replaced
        schedule's kind) and with the evidence it was estimated from."""
        held = self.current or self.baseline
        return dataclasses.replace(
            super()._definition(),
            enabled=True,
            placeholder=held.placeholder if held is not None else True,
            estimate_evidence=held.estimate_evidence if held is not None else None,
        )

    def _fixed_request(self) -> SaveFixedScenarioSchedule:  # type: ignore[override]
        return SaveFixedScenarioSchedule(
            scenario_handle=self.scenario.handle,
            source_schedule=self.baseline.handle if self.baseline is not None else None,
            definition=self._definition(),
            source=self.baseline,
            current=self.current,
        )

    def _formula_request(self) -> FormulaScenarioScheduleInput:  # type: ignore[override]
        recurrence = self._recurrence()
        variables = self._formula_variables()
        assert self.baseline is not None and recurrence is not None and variables is not None
        return FormulaScenarioScheduleInput(
            scenario_handle=self.scenario.handle,
            source_schedule=self.baseline.handle,
            name=self.name_entry.get_text().strip(),
            recurrence=recurrence,
            formulas={index: entry.get_text() for index, entry in self._formula_entries},
            variables=variables,
            growth_policy=_GROWTH_POLICIES[self.growth_policy.get_selected()][1],
            skipped=tuple(self._skipped(recurrence) or ()),
            source=self.baseline,
            current=self.current,
        )

    def build(self) -> ScenarioSchedule:  # type: ignore[override]
        """Build the scenario-owned recurring estimate described by the form."""
        recurrence = self._recurrence()
        assert recurrence is not None
        if self._formula_mode:
            return self.book.draft_formula_scenario_schedule(self._formula_request())
        return self.book.draft_fixed_scenario_schedule(self._fixed_request())

    def _on_save(self, _button) -> None:
        try:
            if self._formula_mode:
                self.book.save_formula_scenario_schedule(self._formula_request())
            else:
                self.book.save_fixed_scenario_schedule(self._fixed_request())
        except ValueError as error:
            self.status.set_text(str(error))
            return
        except ApiError as error:
            self.status.set_text(str(error))
            return
        self.close()


class ScenarioSchedulePickerDialog(BoundedWindow):
    """Choose a baseline schedule before altering or suppressing it."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: OpenBook,
        title: str,
        action_label: str,
        callback: Callable[[ScheduledTransaction], None],
    ) -> None:
        super().__init__(title=title, transient_for=parent, modal=True)
        self._callback = callback
        self._schedules = [
            editor.schedule for editor in book_server(db).schedules_view(0).definitions
        ]
        self.set_default_size(460, 180)

        box = self.frame("scheduled")
        box.append(Gtk.Label(label="Baseline scheduled transaction", xalign=0))
        self.schedule = bounded_dropdown(
            [item.name for item in self._schedules] or ["(none available)"]
        )
        box.append(self.schedule)

        apply = Gtk.Button(label=action_label)
        apply.set_sensitive(bool(self._schedules))
        apply.connect("clicked", self._on_apply)
        self.actions(box, dismiss="Cancel", primary=apply)

    def _on_apply(self, _button) -> None:
        if not self._schedules:
            return
        selected = self._schedules[self.schedule.get_selected()]
        self.close()
        self._callback(selected)
