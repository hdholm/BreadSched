"""Read-only GTK net worth history over the shared service."""

from __future__ import annotations

from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.engine.activity import ReportingPeriod
from ...gen.lib.money import Money
from ...gen.lib.recurrence import add_months
from ...gen.services.net_worth import NetWorthHistory, query_net_worth_history
from ..gi_setup import Gtk
from ..widgets.chart import LineChart, Series

#: Grouping choices and how far back each looks from the current period.
GROUPINGS = (
    ("Month", ReportingPeriod.MONTH, 11),
    ("Quarter", ReportingPeriod.QUARTER, 21),
    ("Year", ReportingPeriod.YEAR, 48),
)


def history_request(months_back: int, today: date) -> tuple[date, date]:
    """The range ending with the current year: whole periods back from today."""
    end = date(today.year, 12, 31)
    return add_months(today.replace(day=1), -months_back, day=1), end


class NetWorthHistoryDialog(Gtk.Window):
    def __init__(self, parent: Gtk.Window | None, db: DbSQLite, today: date | None = None) -> None:
        super().__init__(title="Net Worth History", transient_for=parent)
        self.set_default_size(820, 600)
        self._db = db
        self._today = today or date.today()
        self.history: NetWorthHistory | None = None
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(12)
        self.set_child(outer)
        controls = Gtk.Box(spacing=8)
        outer.append(controls)
        controls.append(Gtk.Label(label="Group by"))
        self.grouping = Gtk.DropDown.new_from_strings([label for label, *_ in GROUPINGS])
        self.grouping.connect("notify::selected", self._update)
        controls.append(self.grouping)
        printing = Gtk.Button(label="Print…")
        printing.connect("clicked", self._print)
        controls.append(printing)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        controls.append(close)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        scroll = Gtk.ScrolledWindow(child=self.content)
        scroll.set_vexpand(True)
        outer.append(scroll)
        self._update()

    @staticmethod
    def _label(text: str, *, heading: bool = False, numeric: bool = False) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=1 if numeric else 0)
        label.set_selectable(True)
        if heading:
            label.add_css_class("heading")
        if numeric:
            label.add_css_class("numeric")
        return label

    @staticmethod
    def _money(value: Money | None) -> str:
        return "Missing quote" if value is None else value.format()

    def _update(self, *_args) -> None:
        while child := self.content.get_first_child():
            self.content.remove(child)
        _label, period, back = GROUPINGS[self.grouping.get_selected()]
        start, end = history_request(back, self._today)
        result = query_net_worth_history(self._db, start, end, period, self._today)
        if result.value is None:
            self.content.append(self._label(result.errors[0].code))
            return
        self.history = history = result.value
        self.content.append(
            self._label(
                "Assets less debts, market-valued at each period end (the last on "
                f"{history.as_of.isoformat()}). A point with a missing quote is left out "
                "of the chart and names the account instead of guessing a conversion."
            )
        )
        complete = [point for point in history.points if point.net_worth is not None]
        chart = LineChart()
        chart.set_content_height(200)
        chart.set_vexpand(False)
        chart.empty_message = "A trend needs at least two valued periods"
        chart.set_data(
            [
                Series(
                    "Net worth",
                    [
                        float(point.net_worth.to_decimal())
                        for point in complete
                        if point.net_worth is not None
                    ],
                )
            ],
            [point.label for point in complete],
        )
        self.chart = chart
        self.content.append(chart)
        grid = Gtk.Grid(column_spacing=14, row_spacing=3)
        headings = ["Period", "Valued on", "Assets", "Debts", "Net worth", "Change", "Note"]
        for column, heading in enumerate(headings):
            grid.attach(self._label(heading, heading=True), column, 0, 1, 1)
        for row, point in enumerate(history.points, 1):
            notes = [
                text
                for text in (
                    "to date" if point.partial else "",
                    f"missing quote: {', '.join(point.missing)}" if point.missing else "",
                )
                if text
            ]
            cells = [
                (point.label, False),
                (point.valued_on.isoformat(), False),
                (self._money(point.assets), True),
                (self._money(point.debts), True),
                (self._money(point.net_worth), True),
                ("—" if point.change is None else point.change.format(), True),
                ("; ".join(notes) or "—", False),
            ]
            for column, (text, numeric) in enumerate(cells):
                label = self._label(text, numeric=numeric)
                if column == 0:
                    label.set_tooltip_text(
                        "\n".join(
                            f"{line.name} ({line.kind}): {self._money(line.value)}"
                            for line in point.lines
                        )
                        or None
                    )
                grid.attach(label, column, row, 1, 1)
        self.table = grid
        self.content.append(grid)

    def _print(self, _button) -> None:
        from ...plugins.export.html_report import net_worth_history_report
        from ..printing import open_print_preview

        if self.history is not None:
            open_print_preview(net_worth_history_report(self.history))
