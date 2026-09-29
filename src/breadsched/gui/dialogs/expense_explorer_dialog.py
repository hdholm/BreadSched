"""Read-only GTK expense visualization over the shared Plan query."""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money
from ...gen.services.expense_explorer import ExpenseExplorer, query_expense_explorer
from ...gen.services.plan import PlanQuery
from ..gi_setup import Gtk
from ..widgets.chart import LineChart, Series


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
        controls.append(Gtk.Label(label="Income detail"))
        self.income_category = Gtk.DropDown.new_from_strings(
            [item.full_name for item in self._report.income_categories]
        )
        self.income_category.set_sensitive(bool(self._report.income_categories))
        controls.append(self.income_category)
        self.rollover = Gtk.CheckButton(label="Carry prior periods")
        self.rollover.connect("toggled", self._toggle_rollover)
        controls.append(self.rollover)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        controls.append(close)
        printing = Gtk.Button(label="Print…")
        printing.connect("clicked", self._print)
        controls.append(printing)
        self.period.connect("notify::selected", self._update)
        self.category.connect("notify::selected", self._update)
        self.sort.connect("notify::selected", self._update)
        self.income_category.connect("notify::selected", self._update)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        scroll = Gtk.ScrolledWindow(child=self.content)
        scroll.set_vexpand(True)
        outer.append(scroll)
        self._update()

    def _toggle_rollover(self, _button) -> None:
        result = query_expense_explorer(
            self._db, self._request, rollover=self.rollover.get_active()
        )
        if result.value is not None:
            self._report = result.value
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
            rollover=self.rollover.get_active(),
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

    def _append_spending(self, index: int, *, income: bool = False) -> None:
        """Total plan and actual by period; clicking a period selects it."""
        points = self._report.income if income else self._report.spending
        self.content.append(
            self._label("Income over time" if income else "Spending over time", heading=True)
        )
        self.content.append(
            self._label(
                f"Total plan and actual. Actual is posted through "
                f"{self._report.plan.report.as_of.isoformat()}; the dashed line marks the "
                "first future period. "
                + (
                    "Income is split by top-level income category; clicking a period "
                    "also selects it for the expense comparison."
                    if income
                    else "Click a period to compare its categories."
                )
            )
        )
        chart = LineChart()
        chart.set_content_height(180)
        chart.set_vexpand(False)
        chart.empty_message = "A trend needs at least two periods"
        chart.set_data(
            [
                Series("Plan", [float(point.planned.to_decimal()) for point in points]),
                Series("Actual", [float(point.actual.to_decimal()) for point in points]),
            ],
            [point.label for point in points],
        )
        chart.marker_index = next((i for i, point in enumerate(points) if point.future), None)
        chart.selected_index = index
        click = Gtk.GestureClick()

        def pressed(_gesture, _count, x, _y) -> None:
            chosen = chart.index_at(x)
            if chosen is not None:
                self.period.set_selected(chosen)

        click.connect("pressed", pressed)
        chart.add_controller(click)
        if income:
            self.income_chart = chart
        else:
            self.spending_chart = chart
        self.content.append(chart)
        rows = self._report.income_categories if income else self._report.categories
        names = {row.account: row.full_name for row in rows}
        grid = Gtk.Grid(column_spacing=12, row_spacing=3)
        headings = ["Period", "Plan", "Actual"]
        if points:
            headings += [names.get(handle, handle) for handle, _amount in points[0].categories]
        headings.append("Note")
        for column, heading in enumerate(headings):
            grid.attach(self._label(heading, heading=True), column, 0, 1, 1)
        for row, point in enumerate(points, 1):
            notes = [
                text
                for flag, text in (
                    (point.partial, "to date"),
                    (point.future, "future"),
                    (point.currency_incomplete, "missing quote"),
                )
                if flag
            ]
            cells = [
                f"{'▸ ' if row - 1 == index else ''}{point.label}",
                point.planned.format(),
                point.actual.format(),
                *(amount.format() for _handle, amount in point.categories),
                ", ".join(notes) or "—",
            ]
            for column, text in enumerate(cells):
                label = self._label(text)
                if 0 < column < len(cells) - 1:
                    label.set_xalign(1)
                    label.add_css_class("numeric")
                grid.attach(label, column, row, 1, 1)
        if income:
            self.income_table = grid
        else:
            self.spending_table = grid
        self.content.append(grid)

    def _append_income_detail(self, index: int) -> None:
        """The dated planned occurrences and receipts behind one income period."""
        choices = self._report.income_categories
        chosen = self.income_category.get_selected()
        if not choices or chosen >= len(choices):
            return
        selected = choices[chosen]
        result = query_expense_explorer(
            self._db,
            self._request,
            account=selected.account,
            period_index=index,
            rollover=self.rollover.get_active(),
        )
        detail = result.value.drilldown if result.value is not None else None
        if detail is None:
            return
        self.content.append(
            self._label(f"{selected.full_name} — {detail.period.label}", heading=True)
        )
        grid = Gtk.Grid(column_spacing=12, row_spacing=3)
        rows = [("Planned date", "Scheduled", "Expected")]
        rows += [
            (item.planned_date.isoformat(), item.description, item.expected.format())
            for item in detail.planned_events
        ]
        rows.append(("Received", "Payer", "Actual"))
        rows += [
            (item.post_date.isoformat(), group.name, item.amount.format())
            for group in detail.merchants
            for item in group.transactions
        ]
        for row, cells in enumerate(rows):
            heading = cells[0] in {"Planned date", "Received"}
            for column, text in enumerate(cells):
                label = self._label(text, heading=heading)
                if column == 2 and not heading:
                    label.set_xalign(1)
                    label.add_css_class("numeric")
                grid.attach(label, column, row, 1, 1)
        self.income_detail = grid
        self.content.append(grid)
        self.content.append(
            self._label(
                f"Planned {detail.period.planned.format()}; actual {detail.period.actual.format()}."
            )
        )

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
        self._append_spending(index)
        if self._report.income:
            self._append_spending(index, income=True)
            self._append_income_detail(index)
        self.content.append(
            self._label(f"Category comparison — {self._report.totals[index].label}", heading=True)
        )
        self.content.append(
            self._label(
                "First bar: plan; second bar: full-period actual. "
                "Remaining uses actual through as-of."
            )
        )
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
            remaining = (
                value.remaining.format()
                if value.remaining is not None
                else value.remaining_reason or "—"
            )
            carry = value.carry_in.format() if value.carry_in is not None else "—"
            grid.attach(
                self._label(
                    f"Plan {value.planned.format()} · Actual {value.actual.format()} · "
                    f"Variance {variance} · Carry {carry} · Remaining {remaining}"
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
            remaining = (
                period.remaining.format()
                if period.remaining is not None
                else period.remaining_reason or "—"
            )
            carry = period.carry_in.format() if period.carry_in is not None else "—"
            line.append(self._label(period.label))
            line.append(self._bar(period.planned, trend_scale))
            line.append(self._bar(period.actual, trend_scale))
            line.append(
                self._label(
                    f"{period.planned.format()} / {period.actual.format()} / "
                    f"{period.variance.format() if period.variance is not None else '—'} · "
                    f"Carry {carry} · Remaining {remaining}"
                )
            )
            self.content.append(line)
        detail = query_expense_explorer(
            self._db,
            self._request,
            account=selected.account,
            period_index=index,
            rollover=self.rollover.get_active(),
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
