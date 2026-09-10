"""Explain the events and accruals behind a projected month."""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite
from ...gen.engine import projection
from ..gi_setup import Gtk

__all__ = ["ProjectionDetailDialog"]


class ProjectionDetailDialog(Gtk.Window):
    """Browse reconciled monthly details from an already-calculated projection."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: DbSQLite,
        result: projection.Projection,
    ) -> None:
        super().__init__(title="Projection explanation", transient_for=parent, modal=True)
        self.db = db
        self.result = result
        self.set_default_size(980, 680)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(16)
        self.set_child(outer)

        chooser = Gtk.Box(spacing=8)
        chooser.append(Gtk.Label(label="Month", xalign=0))
        self.month_picker = Gtk.DropDown.new_from_strings(
            [row.label for row in result.rows]
        )
        default = self._default_index()
        self.month_picker.set_selected(default)
        self.month_picker.connect("notify::selected", self._on_month_changed)
        chooser.append(self.month_picker)
        chooser.append(
            Gtk.Label(
                label=f"Scenario: {result.scenario.name}",
                xalign=0,
            )
        )
        outer.append(chooser)

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scroll = Gtk.ScrolledWindow(child=self.content)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        outer.append(scroll)

        close = Gtk.Button(label="Close")
        close.set_halign(Gtk.Align.END)
        close.connect("clicked", lambda *_: self.close())
        outer.append(close)
        self._render(default)

    def _default_index(self) -> int:
        shortfall = self.result.first_shortfall()
        if shortfall is not None:
            return shortfall.index
        return max(0, len(self.result.rows) - 1)

    def _on_month_changed(self, picker, _param) -> None:
        self._render(picker.get_selected())

    def _clear(self) -> None:
        child = self.content.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.content.remove(child)
            child = nxt

    @staticmethod
    def _money(value) -> str:
        return value.format(parens_negative=True)

    @staticmethod
    def _table(
        headers: tuple[str, ...],
        rows: list[tuple[str, ...]],
        *,
        left_columns: int = 1,
    ) -> Gtk.Widget:
        grid = Gtk.Grid(column_spacing=12, row_spacing=5)
        for col, heading in enumerate(headers):
            label = Gtk.Label(label=heading, xalign=0 if col < left_columns else 1)
            label.add_css_class("heading")
            grid.attach(label, col, 0, 1, 1)
        for row_index, row in enumerate(rows, 1):
            for col, text in enumerate(row):
                label = Gtk.Label(label=text, xalign=0 if col < left_columns else 1)
                label.set_selectable(True)
                grid.attach(label, col, row_index, 1, 1)
        return grid

    def _render(self, index: int) -> None:
        self._clear()
        if not self.result.rows:
            self.content.append(Gtk.Label(label="Projection has no reporting months."))
            return
        detail = projection.explain_month(self.db, self.result, index)
        summary = Gtk.Label(
            label=(
                f"{detail.label}   ·   "
                f"Opening cash {self._money(detail.cash_open)}   ·   "
                f"Cash flow {self._money(detail.cash_flow)}   ·   "
                f"Interest {self._money(detail.cash_interest)}   ·   "
                f"Closing cash {self._money(detail.cash_close)}   ·   "
                f"Net worth {self._money(detail.net_worth)}"
            ),
            xalign=0,
        )
        summary.add_css_class("heading")
        summary.set_wrap(True)
        self.content.append(summary)

        assumptions = detail.assumptions
        self.content.append(
            Gtk.Label(
                label=(
                    "Active annual assumptions: "
                    f"income {assumptions.income_growth:.2%}, "
                    f"expenses {assumptions.expense_inflation:.2%}, "
                    f"investments {assumptions.investment_return:.2%}, "
                    f"cash {assumptions.cash_interest:.2%}, "
                    f"liabilities {assumptions.liability_interest:.2%}"
                ),
                xalign=0,
            )
        )

        self.content.append(Gtk.Label(label="Exact planned events", xalign=0))
        event_rows = [
            (
                event.when.isoformat(),
                event.description,
                event.source.value,
                event.status.value,
                self._money(event.expected_amount),
                "—" if event.actual_amount is None else self._money(event.actual_amount),
                "—" if event.variance is None else self._money(event.variance),
            )
            for event in detail.events
        ]
        if event_rows:
            self.content.append(
                self._table(
                    (
                        "Date",
                        "Description",
                        "Source",
                        "Status",
                        "Expected",
                        "Actual",
                        "Variance",
                    ),
                    event_rows,
                    left_columns=4,
                )
            )
        else:
            self.content.append(Gtk.Label(label="No planned events occur in this month."))

        self.content.append(Gtk.Label(label="Investment accounts", xalign=0))
        holding_rows = [
            (
                item.name,
                self._money(item.opening),
                self._money(item.movement),
                self._money(item.accrual),
                self._money(item.closing),
                f"{item.annual_rate:.2%}",
            )
            for item in detail.holdings
        ]
        if holding_rows:
            self.content.append(
                self._table(
                    ("Account", "Opening", "Contributions", "Growth", "Closing", "Rate"),
                    holding_rows,
                )
            )
        else:
            self.content.append(Gtk.Label(label="No projected investment accounts."))

        self.content.append(Gtk.Label(label="Liability accounts", xalign=0))
        liability_rows = [
            (
                item.name,
                self._money(item.opening),
                self._money(item.movement),
                self._money(item.accrual),
                self._money(item.closing),
                f"{item.annual_rate:.2%}",
            )
            for item in detail.liabilities
        ]
        if liability_rows:
            self.content.append(
                self._table(
                    ("Account", "Opening", "Principal Δ", "Interest", "Closing", "Rate"),
                    liability_rows,
                )
            )
        else:
            self.content.append(Gtk.Label(label="No projected liabilities."))
