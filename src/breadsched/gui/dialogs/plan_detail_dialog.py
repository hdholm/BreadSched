"""Explain one derived Plan category/period value."""

from __future__ import annotations

from collections.abc import Sequence

from ...gen.db.sqlite import DbSQLite
from ...gen.engine.activity import CategoryPeriodDetail, PlanningFlowPeriodDetail
from ..gi_setup import Gtk

__all__ = ["PlanDetailDialog"]


_SOURCE_NAMES = {
    "scheduled": "Scheduled",
    "one_off": "One-time estimate",
    "scenario_schedule": "Scenario estimate",
}


class PlanDetailDialog(Gtk.Window):
    """Show the exact planned and actual activity behind a Plan cell."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: DbSQLite,
        detail: CategoryPeriodDetail | PlanningFlowPeriodDetail,
        *,
        period_label: str,
        scenario_name: str,
    ) -> None:
        super().__init__(
            title=f"{detail.full_name} — {period_label}",
            transient_for=parent,
            modal=True,
        )
        self.set_default_size(900, 620)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(16)
        self.set_child(outer)

        outer.append(
            Gtk.Label(
                label=(f"{scenario_name} · {detail.start:%Y-%m-%d} through {detail.end:%Y-%m-%d}"),
                xalign=0,
            )
        )
        variance = (
            detail.variance.format(parens_negative=True) if detail.variance is not None else "—"
        )
        summary = Gtk.Label(
            label=(
                f"Plan {detail.planned.format(parens_negative=True)}   ·   "
                f"Actual {detail.actual.format(parens_negative=True)}   ·   "
                f"Variance {variance}"
            ),
            xalign=0,
        )
        summary.add_css_class("heading")
        outer.append(summary)

        notebook = Gtk.Notebook()
        notebook.set_vexpand(True)
        outer.append(notebook)
        notebook.append_page(self._planned_page(detail), Gtk.Label(label="Planned occurrences"))
        notebook.append_page(self._actual_page(detail), Gtk.Label(label="Actual transactions"))

        close = Gtk.Button(label="Close")
        close.set_halign(Gtk.Align.END)
        close.connect("clicked", lambda *_: self.close())
        outer.append(close)

    @staticmethod
    def _table(
        headers: tuple[str, ...],
        rows: Sequence[tuple[str, ...]],
        *,
        numeric_start: int,
    ) -> Gtk.Widget:
        grid = Gtk.Grid(column_spacing=12, row_spacing=5)
        grid.set_margin_top(8)
        grid.set_margin_bottom(8)
        grid.set_margin_start(8)
        grid.set_margin_end(8)
        for col, heading in enumerate(headers):
            label = Gtk.Label(label=heading, xalign=0 if col < numeric_start else 1)
            label.add_css_class("heading")
            grid.attach(label, col, 0, 1, 1)
        for row_index, row in enumerate(rows, 1):
            for col, text in enumerate(row):
                label = Gtk.Label(label=text, xalign=0 if col < numeric_start else 1)
                label.set_wrap(col == numeric_start - 1)
                label.set_selectable(True)
                grid.attach(label, col, row_index, 1, 1)
        scroll = Gtk.ScrolledWindow(child=grid)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        return scroll

    def _planned_page(self, detail: CategoryPeriodDetail | PlanningFlowPeriodDetail) -> Gtk.Widget:
        rows = [
            (
                item.planned_date.isoformat(),
                item.description,
                _SOURCE_NAMES.get(item.source, item.source),
                item.status,
                "\n".join(item.explanation) or "—",
                item.expected.format(parens_negative=True),
                "—" if item.actual is None else item.actual.format(parens_negative=True),
                "—" if item.variance is None else item.variance.format(parens_negative=True),
            )
            for item in detail.planned_events
        ]
        if not rows:
            return Gtk.Label(label="No planned occurrences contribute to this cell.")
        return self._table(
            (
                "Planned",
                "Description",
                "Source",
                "Status",
                "Escrow treatment",
                "Expected",
                "Actual",
                "Variance",
            ),
            rows,
            numeric_start=5,
        )

    def _actual_page(self, detail: CategoryPeriodDetail | PlanningFlowPeriodDetail) -> Gtk.Widget:
        rows = [
            (
                item.post_date.isoformat(),
                item.description,
                item.resolution.value,
                "\n".join(item.explanation) or "—",
                item.amount.format(parens_negative=True),
                "—" if item.expected is None else item.expected.format(parens_negative=True),
                "—" if item.variance is None else item.variance.format(parens_negative=True),
                "—" if item.date_variance_days is None else str(item.date_variance_days),
            )
            for item in detail.actual_transactions
        ]
        if not rows:
            return Gtk.Label(label="No actual transactions contribute to this cell.")
        return self._table(
            (
                "Posted",
                "Description",
                "Resolution",
                "Escrow treatment",
                "Actual",
                "Expected",
                "Variance",
                "Date Δ days",
            ),
            rows,
            numeric_start=4,
        )
