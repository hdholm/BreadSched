"""Read-only GTK expense visualization over the shared Plan query."""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money
from ...gen.services.expense_explorer import ExpenseExplorer, query_expense_explorer
from ...gen.services.plan import PlanQuery
from ..gi_setup import Gtk


class ExpenseExplorerDialog(Gtk.Window):
    def __init__(self, parent: Gtk.Window | None, db: DbSQLite, request: PlanQuery) -> None:
        super().__init__(title="Expense Explorer", transient_for=parent)
        self.set_default_size(1000, 720)
        self._db = db
        self._request = request
        result = query_expense_explorer(db, request)
        if result.value is None:
            raise ValueError(result.errors[0].code)
        self._report: ExpenseExplorer = result.value
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(12)
        self.set_child(outer)
        controls = Gtk.Box(spacing=8)
        outer.append(controls)
        controls.append(Gtk.Label(label="Period"))
        self.period = Gtk.DropDown.new_from_strings([item.label for item in self._report.totals])
        controls.append(self.period)
        controls.append(Gtk.Label(label="Category trend"))
        self.category = Gtk.DropDown.new_from_strings(
            [item.full_name for item in self._report.categories]
        )
        controls.append(self.category)
        controls.append(Gtk.Label(label="Sort categories"))
        self.sort = Gtk.DropDown.new_from_strings(["Actual", "Plan", "Variance", "Name"])
        controls.append(self.sort)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        controls.append(close)
        printing = Gtk.Button(label="Print…")
        printing.connect("clicked", self._print)
        controls.append(printing)
        self.period.connect("notify::selected", self._update)
        self.category.connect("notify::selected", self._update)
        self.sort.connect("notify::selected", self._update)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        scroll = Gtk.ScrolledWindow(child=self.content)
        scroll.set_vexpand(True)
        outer.append(scroll)
        self._update()

    def _print(self, _button) -> None:
        from ...plugins.export.html_report import expense_explorer_report
        from ..printing import open_print_preview

        category_index = self.category.get_selected()
        period_index = self.period.get_selected()
        if category_index >= len(self._report.categories):
            return
        result = query_expense_explorer(
            self._db,
            self._request,
            account=self._report.categories[category_index].account,
            period_index=period_index,
        )
        if result.value is not None:
            open_print_preview(expense_explorer_report(result.value))

    @staticmethod
    def _label(text: str, *, heading: bool = False) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0)
        label.set_selectable(True)
        if heading:
            label.add_css_class("heading")
        return label

    @staticmethod
    def _bar(amount: Money, scale: float) -> Gtk.ProgressBar:
        bar = Gtk.ProgressBar()
        bar.set_fraction(min(1.0, abs(amount.numerator / amount.denominator) / scale))
        bar.set_hexpand(True)
        return bar

    def _update(self, *_args) -> None:
        while child := self.content.get_first_child():
            self.content.remove(child)
        if not self._report.categories:
            self.content.append(self._label("No expense categories in this Plan range."))
            return
        index = self.period.get_selected()
        if index >= len(self._report.totals):
            return
        selected_index = self.category.get_selected()
        if selected_index >= len(self._report.categories):
            selected_index = 0
        selected = self._report.categories[selected_index]
        rows = list(self._report.categories)
        sort = self.sort.get_selected()
        if sort == 3:
            rows.sort(key=lambda row: row.full_name.casefold())
        else:
            field = ("actual", "planned", "variance")[sort]
            rows.sort(key=lambda row: getattr(row.periods[index], field) or Money(0), reverse=True)
        scale = max(
            1.0,
            *(
                abs(value.numerator / value.denominator)
                for row in rows
                for value in (row.periods[index].planned, row.periods[index].actual)
            ),
        )
        self.content.append(
            self._label(f"Category comparison — {self._report.totals[index].label}", heading=True)
        )
        self.content.append(self._label("First bar: plan; second bar: actual."))
        grid = Gtk.Grid(column_spacing=12, row_spacing=5)
        self.content.append(grid)
        for i, row in enumerate(rows):
            value = row.periods[index]
            grid.attach(self._label(row.full_name), 0, i, 1, 1)
            bars = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            bars.append(self._bar(value.planned, scale))
            bars.append(self._bar(value.actual, scale))
            grid.attach(bars, 1, i, 1, 1)
            variance = value.variance.format() if value.variance is not None else "—"
            grid.attach(
                self._label(
                    f"Plan {value.planned.format()} · Actual {value.actual.format()} · "
                    f"Variance {variance}"
                ),
                2,
                i,
                1,
                1,
            )
        self.content.append(self._label(f"{selected.full_name} trend", heading=True))
        trend_scale = max(
            1.0,
            *(
                abs(value.numerator / value.denominator)
                for period in selected.periods
                for value in (period.planned, period.actual)
            ),
        )
        for period in selected.periods:
            line = Gtk.Box(spacing=8)
            line.append(self._label(period.label))
            line.append(self._bar(period.planned, trend_scale))
            line.append(self._bar(period.actual, trend_scale))
            line.append(
                self._label(
                    f"{period.planned.format()} / {period.actual.format()} / "
                    f"{period.variance.format() if period.variance is not None else '—'}"
                )
            )
            self.content.append(line)
        detail = query_expense_explorer(
            self._db, self._request, account=selected.account, period_index=index
        )
        if detail.value is None or detail.value.drilldown is None:
            return
        self.content.append(self._label("Merchants — actual only", heading=True))
        self.content.append(self._label("The category plan is unallocated across merchants."))
        for group in detail.value.drilldown.merchants:
            self.content.append(self._label(f"{group.name} · {group.amount.format()}"))
            for transaction in group.transactions:
                self.content.append(
                    self._label(
                        f"    {transaction.post_date:%Y-%m-%d} · "
                        f"{transaction.description.strip() or 'Unknown merchant'} · "
                        f"{transaction.amount.format()}"
                    )
                )
