"""Derived event-driven planning view.

Scheduled occurrences and actual ledger transactions are authoritative.  Month,
quarter and year are presentation buckets only; changing the grouping never writes
planning data back to the book.
"""

from __future__ import annotations

from datetime import date

from ...gen.engine.activity import ReportingPeriod, build_activity_report
from ..gi_setup import Gtk
from ._base import BaseView

__all__ = ["PlanView"]


class PlanView(BaseView):
    """Read-only plan versus actual summary derived from exact-dated events."""

    WATCHES = (
        "database-changed",
        "transaction-add",
        "transaction-update",
        "transaction-delete",
        "scheduled-add",
        "scheduled-update",
        "scheduled-delete",
    )

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self._report = None
        self._year = date.today().year
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

        schedules = Gtk.Button(label="Edit schedules…")
        schedules.connect("clicked", lambda *_: self.manager.show_category("scheduled"))
        bar.append(schedules)
        self.append(bar)

        self.summary = Gtk.Label(xalign=0, wrap=True)
        self.summary.set_margin_start(12)
        self.summary.set_margin_end(12)
        self.summary.set_margin_bottom(8)
        self.append(self.summary)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(570)
        paned.set_vexpand(True)
        self.append(paned)

        self.periods = Gtk.ListBox()
        self.periods.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.periods.connect("row-selected", self._on_period_selected)
        period_scroll = Gtk.ScrolledWindow(child=self.periods)
        period_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        paned.set_start_child(period_scroll)

        detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        detail_box.set_margin_top(10)
        detail_box.set_margin_bottom(10)
        detail_box.set_margin_start(12)
        detail_box.set_margin_end(12)
        paned.set_end_child(detail_box)

        self.detail_title = Gtk.Label(label="Select a period", xalign=0)
        self.detail_title.add_css_class("heading")
        detail_box.append(self.detail_title)

        self.detail_summary = Gtk.Label(xalign=0, wrap=True)
        self.detail_summary.add_css_class("dim")
        detail_box.append(self.detail_summary)

        self.events = Gtk.ListBox()
        self.events.set_selection_mode(Gtk.SelectionMode.NONE)
        event_scroll = Gtk.ScrolledWindow(child=self.events)
        event_scroll.set_vexpand(True)
        detail_box.append(event_scroll)

    def _clear(self, box: Gtk.ListBox) -> None:
        while (row := box.get_row_at_index(0)) is not None:
            box.remove(row)

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
        start = date(self._year, 1, 1)
        end = date(self._year, 12, 31)
        report = build_activity_report(self.db, start, end, period=self._grouping())
        self._report = report
        self.summary.set_text(
            f"Planned cash {report.planned_cash_change.format(parens_negative=True)}   ·   "
            f"Actual cash {report.actual_cash_change.format(parens_negative=True)}   ·   "
            f"Variance {report.cash_variance.format(parens_negative=True)}   ·   "
            f"{report.unresolved_count} expected unresolved   ·   "
            f"{report.unresolved_actual_count} actuals to review"
        )

        self._clear(self.periods)
        for period in report.periods:
            row = Gtk.ListBoxRow()
            row.activity_period = period
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(8)
            box.set_margin_end(8)
            box.append(Gtk.Label(label=period.label, xalign=0))
            detail = Gtk.Label(
                label=(
                    f"Planned {period.planned_cash_change.format(parens_negative=True)}   "
                    f"Actual {period.actual_cash_change.format(parens_negative=True)}   "
                    f"Variance {period.cash_variance.format(parens_negative=True)}"
                ),
                xalign=0,
            )
            detail.add_css_class("dim")
            box.append(detail)
            row.set_child(box)
            self.periods.append(row)

        first = self.periods.get_row_at_index(0)
        if first is not None:
            self.periods.select_row(first)

    def _on_period_selected(self, _box, row) -> None:
        self._clear(self.events)
        period = getattr(row, "activity_period", None) if row is not None else None
        if period is None:
            self.detail_title.set_text("Select a period")
            self.detail_summary.set_text("")
            return

        self.detail_title.set_text(period.label)
        self.detail_summary.set_text(
            f"Expected {len(period.planned_events)} event(s), "
            f"{len(period.unresolved)} unresolved · "
            f"Actual {len(period.actual_transactions)} transaction(s), "
            f"{len(period.unresolved_actuals)} awaiting review, "
            f"{len(period.unexpected)} unexpected"
        )

        items: list[tuple[date, str]] = []
        for event in period.planned_events:
            status = "matched" if event.status.value == "actualized" else "expected"
            items.append(
                (
                    event.planned_date,
                    f"{event.planned_date.isoformat()}  {event.description}\n"
                    f"Expected {event.expected_amount.format()} · {status}",
                )
            )
        for actual in period.actual_transactions:
            resolution = actual.planning_resolution.value
            variance = ""
            if actual.planned_amount is not None:
                variance = f" · variance {actual.variance.format(parens_negative=True)}"
                if actual.date_variance_days is not None:
                    variance += f" · {actual.date_variance_days:+d} day(s)"
            items.append(
                (
                    actual.post_date,
                    f"{actual.post_date.isoformat()}  {actual.description}\n"
                    f"Actual {actual.amount.format()} · {resolution}{variance}",
                )
            )

        for _when, text in sorted(items, key=lambda item: (item[0], item[1])):
            label = Gtk.Label(label=text, xalign=0, wrap=True)
            label.set_margin_top(5)
            label.set_margin_bottom(5)
            label.set_margin_start(8)
            label.set_margin_end(8)
            self.events.append(label)
