"""Read-only GTK net worth history over the shared service."""

from __future__ import annotations

from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.engine.activity import ReportingPeriod
from ...gen.lib.money import Money
from ...gen.lib.recurrence import add_months
from ...gen.services.net_worth import (
    NetWorthChange,
    NetWorthHistory,
    NetWorthPoint,
    query_net_worth_change,
    query_net_worth_history,
)
from ..gi_setup import GLib, Gtk
from ..widgets.bounded import BoundedWindow
from ..widgets.chart import LineChart, Series
from ..widgets.choice import bounded_dropdown

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


class NetWorthHistoryDialog(BoundedWindow):
    def __init__(self, parent: Gtk.Window | None, db: DbSQLite, today: date | None = None) -> None:
        super().__init__(title="Net Worth History", transient_for=parent)
        self.set_default_size(820, 600)
        self._db = db
        self._today = today or date.today()
        self.history: NetWorthHistory | None = None
        self.change: NetWorthChange | None = None
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(12)
        self.set_child(outer)
        controls = Gtk.Box(spacing=8)
        outer.append(controls)
        controls.append(Gtk.Label(label="Group by"))
        self.grouping = bounded_dropdown([label for label, *_ in GROUPINGS])
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
        self.change_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.append(self.change_box)
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
        self._close_change()
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
                if column == 5:
                    button = Gtk.Button(label="Explain" if point.change is None else text)
                    button.add_css_class("flat")
                    button.set_tooltip_text("Show the postings behind this change")
                    button.connect("clicked", self._explain, point)
                    grid.attach(button, column, row, 1, 1)
                    continue
                label = self._label(text, numeric=numeric)
                if column == 0:
                    label.set_tooltip_text(
                        "\n".join(
                            f"{line.name} ({line.kind}): {self._money(line.value)}"
                            for line in point.lines
                        )
                        or None
                    )
                if column == 6 and not point.completeness.complete:
                    # What the withheld point leaves out and how to include it (#236).
                    label.set_tooltip_text("\n".join(point.completeness.detail()))
                grid.attach(label, column, row, 1, 1)
        self.table = grid
        self.content.append(grid)

    def _close_change(self, *_args) -> None:
        self.change = None
        while child := self.change_box.get_first_child():
            self.change_box.remove(child)

    def _explain(self, _button, point: NetWorthPoint) -> None:
        """Show the postings behind ``point``'s change, reconciled to market movement."""
        self._close_change()
        result = query_net_worth_change(self._db, point.start, point.end, self._today)
        if result.value is None:
            self.change_box.append(self._label(result.errors[0].code))
            return
        self.change = change = result.value
        heading = f"Net worth change {change.start.isoformat()} through {change.closing_on}"
        self.change_box.append(
            self._label(heading + (" (to date)" if change.partial else ""), heading=True)
        )
        notes = [
            "Each posting is the net of its splits in asset and debt accounts, converted "
            "with the quote applicable on its date. Market and exchange-rate changes are "
            "the rest."
        ]
        if change.transfers:
            notes.append(
                f"{change.transfers} transfer(s) between your own accounts left out: "
                "they do not change net worth."
            )
        if change.missing:
            notes.append(f"Missing quote: {', '.join(change.missing)}. Totals are withheld.")
            notes.extend(change.completeness.detail())
        for note in notes:
            label = self._label(note)
            label.set_wrap(True)
            self.change_box.append(label)
        actions = Gtk.Box(spacing=8)
        for caption, handler in (
            ("Print…", self._print_change),
            ("Export CSV…", self._export_change),
            ("Close", self._close_change),
        ):
            button = Gtk.Button(label=caption)
            button.connect("clicked", handler)
            actions.append(button)
        self.change_box.append(actions)
        grid = Gtk.Grid(column_spacing=14, row_spacing=3)
        headings = ["Date", "Description", "Accounts", "Currency", "Net worth effect"]
        for column, heading in enumerate(headings):
            grid.attach(self._label(heading, heading=True), column, 0, 1, 1)
        rows = [
            [
                posting.posted.isoformat(),
                posting.description,
                "; ".join(posting.accounts),
                posting.currency,
                self._money(posting.effect),
            ]
            for posting in change.postings
        ]
        rows += [
            ["", label, "", "", self._money(value)]
            for label, value in (
                (f"Opening net worth ({change.opening_on.isoformat()})", change.opening),
                ("Postings", change.posted),
                ("Market and exchange-rate changes", change.revaluation),
                (f"Closing net worth ({change.closing_on.isoformat()})", change.closing),
                ("Change", change.change),
            )
        ]
        for row, cells in enumerate(rows, 1):
            for column, text in enumerate(cells):
                grid.attach(self._label(text, numeric=column == 4), column, row, 1, 1)
        self.change_table = grid
        scroll = Gtk.ScrolledWindow(child=grid)
        scroll.set_min_content_height(160)
        self.change_box.append(scroll)

    def _print_change(self, _button) -> None:
        from ...plugins.export.report_layout import net_worth_change_layout
        from .. import printing

        if self.change is not None:
            printing.print_document(self, net_worth_change_layout(self.change))

    def export_change(self, path: str) -> None:
        from ...plugins.export.csv_export import export_net_worth_change

        if self.change is not None:
            export_net_worth_change(self.change, path)

    def _export_change(self, _button) -> None:
        if self.change is None:
            return
        dialog = Gtk.FileDialog(
            title="Export net worth change",
            initial_name=f"net-worth-change-{self.change.start.isoformat()}.csv",
        )

        def on_saved(file_dialog, result) -> None:
            try:
                file = file_dialog.save_finish(result)
            except GLib.Error:
                return
            self.export_change(file.get_path())

        dialog.save(self, None, on_saved)

    def _print(self, _button) -> None:
        from ...plugins.export.report_layout import net_worth_history_layout
        from .. import printing

        if self.history is not None:
            printing.print_document(self, net_worth_history_layout(self.history))
