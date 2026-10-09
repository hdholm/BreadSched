"""Budget jars by account and period, each jar on request, printable."""

from __future__ import annotations

from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.engine.activity import ReportingPeriod
from ...gen.engine.budget_jars import (
    JarPeriod,
    JarsReport,
    budget_jars,
    currency_labels,
    jar_kind_label,
)
from ...plugins.export.report_layout import budget_jars_layout
from .. import printing
from ..gi_setup import Gtk
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from ..widgets.help import help_row

__all__ = ["BudgetJarsDialog"]

_PERIODS = (ReportingPeriod.MONTH, ReportingPeriod.QUARTER, ReportingPeriod.YEAR)
_HEADINGS = ("Period", "Filled", "Planned", "Actual", "Variance", "Level")


def _cell(text: str, *, numeric: bool = False) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=1 if numeric else 0)
    if numeric:
        label.add_css_class("numeric")
    return label


def _grid(periods: tuple[JarPeriod, ...]) -> Gtk.Grid:
    grid = Gtk.Grid(column_spacing=16, row_spacing=4)
    for column, text in enumerate(_HEADINGS):
        label = _cell(text, numeric=column > 0)
        label.add_css_class("dim")
        grid.attach(label, column, 0, 1, 1)
    for row, item in enumerate(periods, start=1):
        for column, text in enumerate(
            (
                item.label,
                item.filled.format(),
                item.planned.format(),
                item.actual.format(),
                item.variance.format(parens_negative=True),
                item.level.format(parens_negative=True),
            )
        ):
            grid.attach(_cell(text, numeric=column > 0), column, row, 1, 1)
    return grid


class BudgetJarsDialog(BoundedWindow):
    """Every jar's fills and draws in the Plan's range, bundled by account."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: DbSQLite,
        start: date | None = None,
        end: date | None = None,
        period: ReportingPeriod = ReportingPeriod.MONTH,
    ) -> None:
        super().__init__(title="Budget Jars", transient_for=parent)
        self.db = db
        today = date.today()
        self.start = start or date(today.year, today.month, 1)
        self.end = end or date(today.year, 12, 31)
        self.period = period
        self.set_default_size(820, 620)
        self.report: JarsReport

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(12)
        self.set_child(outer)
        outer.append(help_row("jars"))
        note = Gtk.Label(
            label=(
                "Each scheduled payment and estimate, and each savings goal, is a jar. It "
                "fills from each income in its cycle by that income's share, and is drawn "
                "by the actual transaction matched to it. Periods only group dated events; "
                "a level counts the occurrences due from the first day shown."
            ),
            xalign=0,
            wrap=True,
        )
        note.add_css_class("dim")
        outer.append(note)

        toolbar = Gtk.Box(spacing=8)
        toolbar.append(_cell(f"{self.start.isoformat()} to {self.end.isoformat()}"))
        toolbar.append(Gtk.Label(label="Group by"))
        self.period_choice = bounded_dropdown([item.value.title() for item in _PERIODS])
        self.period_choice.update_property([Gtk.AccessibleProperty.LABEL], ["Group by"])
        self.period_choice.set_selected(_PERIODS.index(period))
        self.period_choice.connect("notify::selected", lambda *_: self._choose_period())
        toolbar.append(self.period_choice)
        self.print_button = Gtk.Button(label="Print…")
        self.print_button.connect("clicked", lambda *_: self.print_report())
        toolbar.append(self.print_button)
        outer.append(toolbar)

        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scroller = Gtk.ScrolledWindow(child=self.body, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(240)
        outer.append(scroller)
        close = Gtk.Button(label="Close", halign=Gtk.Align.END)
        close.connect("clicked", lambda *_: self.close())
        outer.append(close)
        self.refresh()

    def _choose_period(self) -> None:
        index = self.period_choice.get_selected()
        if index != Gtk.INVALID_LIST_POSITION:
            self.period = _PERIODS[index]
            self.refresh()

    def refresh(self) -> None:
        """Recompute the jars for the range and grouping and redraw them."""
        self.report = report = budget_jars(self.db, self.start, self.end, period=self.period)
        child = self.body.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.body.remove(child)
            child = following
        labels = currency_labels(self.db, report)
        self.account_rows: list[Gtk.Expander] = []
        for total in report.totals:
            label = labels.get(total.currency, "")
            heading = _cell("All jars" + (f" ({label})" if label else ""))
            heading.add_css_class("heading")
            self.body.append(heading)
            self.body.append(_grid(total.periods))
        for bundle in report.accounts:
            label = labels.get(bundle.currency, "")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.set_margin_start(18)
            box.append(_grid(bundle.periods))
            for jar in bundle.jars:
                title = _cell(f"{jar.name} — {jar_kind_label(jar.kind)}")
                title.add_css_class("dim")
                box.append(title)
                box.append(_grid(jar.periods))
            expander = Gtk.Expander(
                label=f"{bundle.account_name}{f' ({label})' if label else ''}: "
                + ", ".join(jar.name for jar in bundle.jars)
            )
            expander.get_label_widget().set_wrap(True)
            expander.set_child(box)
            self.body.append(expander)
            self.account_rows.append(expander)
        if not report.accounts:
            self.body.append(_cell("No scheduled payments, estimates, or goals in this range."))
        for problem in report.problems:
            warning = _cell(problem)
            warning.set_wrap(True)
            warning.add_css_class("negative")
            self.body.append(warning)

    def layout(self):
        return budget_jars_layout(self.report, currency_labels(self.db, self.report))

    def print_report(self) -> None:
        printing.print_document(self, self.layout())
