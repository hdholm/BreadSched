"""Read-only GTK expense visualization over the shared Plan query."""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite
from ...gen.engine.currency import reporting_currency_label
from ...gen.lib.money import Money
from ...gen.services.expense_explorer import (
    ExpenseExplorer,
    category_trend_chart,
    expense_drilldown,
    query_expense_explorer,
    spending_charts,
)
from ...gen.services.plan import PlanQuery
from ...gen.utils import logs
from ..background import BackgroundJob
from ..gi_setup import Gtk
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from ..widgets.help import help_row
from ..widgets.model_chart import ModelChartView

__all__ = ["ExpenseExplorerDialog"]

LOG = logs.get_logger(__name__)

#: Narrowest a choice may be squeezed, so its text stays readable (#311).
CHOICE_WIDTH = 180
#: Account names are long; their choices get more room.
ACCOUNT_CHOICE_WIDTH = 260


class ExpenseExplorerDialog(BoundedWindow):
    """Plan categories by period, with charts, a trend, and merchant detail.

    The Plan behind it is calculated off the main loop (#310): the window opens at
    once with a note and a Cancel button, and fills in when the calculation ends.
    Choosing a period or category reuses that result and explains only the chosen
    category.
    """

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite, request: PlanQuery) -> None:
        super().__init__(title="Expense Explorer", transient_for=parent)
        self.set_default_size(1200, 820)
        self._db = db
        self._request = request
        self._currency = reporting_currency_label(db)
        self._report: ExpenseExplorer | None = None
        self._generation = 0
        #: True while a calculation is running; choices wait for it.
        self.busy_calculating = False
        self.job: BackgroundJob[ExpenseExplorer, None] = BackgroundJob()
        self.connect("close-request", self._on_close_request)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(12)
        self.set_child(outer)
        outer.append(help_row("expenses"))

        # Each choice sits beside its label, two to a row, so none is squeezed to a
        # few characters by the others (#311).
        controls = Gtk.Grid(column_spacing=8, row_spacing=6)
        outer.append(controls)
        self.period = bounded_dropdown([])
        self.sort = bounded_dropdown(["Period actual", "Plan", "Period variance", "Name"])
        self.category = bounded_dropdown([])
        self.income_category = bounded_dropdown([])
        for column, row, text, choice, width in (
            (0, 0, "Period", self.period, CHOICE_WIDTH),
            (2, 0, "Sort categories", self.sort, CHOICE_WIDTH),
            (0, 1, "Category trend", self.category, ACCOUNT_CHOICE_WIDTH),
            (2, 1, "Income detail", self.income_category, ACCOUNT_CHOICE_WIDTH),
        ):
            label = Gtk.Label(label=text, xalign=1)
            label.set_mnemonic_widget(choice)
            choice.set_size_request(width, -1)
            choice.set_hexpand(True)
            controls.attach(label, column, row, 1, 1)
            controls.attach(choice, column + 1, row, 1, 1)
        self.rollover = Gtk.CheckButton(label="Carry prior periods")
        self.rollover.connect("toggled", self._toggle_rollover)
        controls.attach(self.rollover, 1, 2, 3, 1)
        self.controls = controls

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        scroll = Gtk.ScrolledWindow(child=self.content)
        scroll.set_vexpand(True)
        outer.append(scroll)

        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.print_button = Gtk.Button(label="Print…")
        self.print_button.connect("clicked", self._print)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        actions.append(self.print_button)
        actions.append(close)
        outer.append(actions)

        for choice in (self.period, self.category, self.sort, self.income_category):
            choice.connect("notify::selected", self._update)
        self._calculate()

    # ------------------------------------------------------------ calculation

    @property
    def report(self) -> ExpenseExplorer:
        """The calculated explorer; only read once it has loaded."""
        assert self._report is not None
        return self._report

    @property
    def loaded(self) -> bool:
        return self._report is not None and not self.busy_calculating

    def wait_loaded(self, timeout: float = 60.0) -> bool:
        """Wait (pumping GLib) until the calculation is shown; for tests."""
        self.job.wait(timeout)
        return self.loaded

    def _calculate(self) -> None:
        """Calculate the explorer in the background, showing that it is working."""
        self.job.cancel()
        self._generation += 1
        generation = self._generation
        self.busy_calculating = True
        self.controls.set_sensitive(False)
        self.print_button.set_sensitive(False)
        self._clear()
        busy = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        spinner = Gtk.Spinner()
        spinner.start()
        busy.append(spinner)
        busy.append(self._label("Calculating the Plan for the Expense Explorer…"))
        cancel = Gtk.Button(label="Cancel")
        cancel.set_tooltip_text("Stop calculating and close the Expense Explorer")
        cancel.connect("clicked", lambda *_: self.close())
        busy.append(cancel)
        self.busy = busy
        self.content.append(busy)
        db, request, rollover = self._db, self._request, self.rollover.get_active()

        def work(_cancel, _report) -> ExpenseExplorer:
            worker = db
            path = db.path
            owns = path is not None and path != ":memory:"
            if path is not None and owns:
                worker = DbSQLite()
                worker.load(path, "r")
            try:
                result = query_expense_explorer(worker, request, rollover=rollover)
            finally:
                if owns:
                    worker.close()
            if result.value is None:
                raise ValueError(result.errors[0].code)
            return result.value

        self.job = BackgroundJob()
        self.job.start(
            work,
            lambda _progress: None,
            lambda report: self._loaded(generation, report),
            lambda exc: self._failed(generation, exc),
        )

    def _loaded(self, generation: int, report: ExpenseExplorer) -> None:
        if generation != self._generation:
            return
        self.busy_calculating = False
        first = self._report is None
        self._report = report
        if first:
            self._fill(self.period, [item.label for item in report.totals])
            self._fill(self.category, [item.full_name for item in report.categories])
            self._fill(self.income_category, [item.full_name for item in report.income_categories])
        self.income_category.set_sensitive(bool(report.income_categories))
        self.controls.set_sensitive(True)
        self.print_button.set_sensitive(True)
        self._update()

    def _failed(self, generation: int, exc: BaseException) -> None:
        if generation != self._generation:
            return
        self.busy_calculating = False
        from ...gen.utils.cancellation import OperationCancelled

        if isinstance(exc, OperationCancelled):
            return
        LOG.error("expense explorer failed", exc_info=(type(exc), exc, exc.__traceback__))
        self._clear()
        self.content.append(self._label(f"The Expense Explorer could not be calculated: {exc}"))

    def _on_close_request(self, *_args) -> bool:
        # The calculation's result is no longer wanted; it finishes on its own.
        self._generation += 1
        self.job.cancel()
        return False

    def _fill(self, choice: Gtk.DropDown, names: list[str]) -> None:
        choice.set_model(Gtk.StringList.new(names))
        choice.set_selected(0)

    def _clear(self) -> None:
        while child := self.content.get_first_child():
            self.content.remove(child)

    def _drilldown(self, account: str, index: int):
        """One category-period explained from the loaded result, not a new Plan."""
        assert self._report is not None
        result = expense_drilldown(self._db, self._report, account, index)
        return result.value.drilldown if result.value is not None else None

    def _toggle_rollover(self, _button) -> None:
        if self._report is not None:
            self._calculate()

    def _print(self, _button) -> None:
        from ...plugins.export.report_layout import expense_explorer_layout
        from .. import printing

        if self._report is None:
            return
        category_index = self.category.get_selected()
        period_index = self.period.get_selected()
        if category_index >= len(self.report.categories):
            return
        result = expense_drilldown(
            self._db,
            self._report,
            self.report.categories[category_index].account,
            period_index,
        )
        if result.value is None:
            return
        income_detail = None
        choices = self.report.income_categories
        chosen = self.income_category.get_selected()
        if self.report.income and chosen < len(choices):
            income_detail = self._drilldown(choices[chosen].account, period_index)
        printing.print_document(
            self, expense_explorer_layout(result.value, income_detail, self._currency)
        )

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

    def _charts(self, models, index: int) -> list[ModelChartView]:
        """Shared-model charts of the Plan's periods; clicking one selects its period."""
        views = []
        for model in models:
            if model.empty:
                continue
            view = ModelChartView(model, height=220, on_select=self.period.set_selected)
            view.set_selected(index)
            self.content.append(view)
            if model.partial_note:
                note = self._label(model.partial_note)
                note.set_wrap(True)
                note.add_css_class("negative")
                self.content.append(note)
            views.append(view)
        return views

    def _append_spending(self, index: int, *, income: bool = False) -> None:
        """Total plan and actual, actual by category, and shares, by period."""
        points = self.report.income if income else self.report.spending
        self.content.append(
            self._label("Income over time" if income else "Spending over time", heading=True)
        )
        explanation = self._label(
            f"Actual is posted through {self.report.plan.report.as_of.isoformat()}; "
            "the rule marks that date's period. Categories past the seventh are "
            "combined as Other. "
            + (
                "Clicking a period also selects it for the expense comparison."
                if income
                else "Click a period to compare its categories."
            )
        )
        explanation.set_wrap(True)
        self.content.append(explanation)
        charts = self._charts(
            spending_charts(self.report, income=income, currency=self._currency), index
        )
        if income:
            self.income_charts = charts
        else:
            self.spending_charts = charts
        rows = self.report.income_categories if income else self.report.categories
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
                    (point.currency_incomplete, point.completeness.label or "missing quote"),
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
                elif column == len(cells) - 1 and not point.completeness.complete:
                    # The excluded amounts behind a partial period (#236).
                    label.set_tooltip_text("\n".join(point.completeness.detail()))
                grid.attach(label, column, row, 1, 1)
        if income:
            self.income_table = grid
        else:
            self.spending_table = grid
        self.content.append(grid)

    def _append_income_detail(self, index: int) -> None:
        """The dated planned occurrences and receipts behind one income period."""
        choices = self.report.income_categories
        chosen = self.income_category.get_selected()
        if not choices or chosen >= len(choices):
            return
        selected = choices[chosen]
        detail = self._drilldown(selected.account, index)
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
        if self._report is None or self.busy_calculating:
            return
        self._clear()
        if not self.report.categories:
            self.content.append(self._label("No expense categories in this Plan range."))
            return
        index = self.period.get_selected()
        if index >= len(self.report.totals):
            return
        selected_index = self.category.get_selected()
        if selected_index >= len(self.report.categories):
            selected_index = 0
        selected = self.report.categories[selected_index]
        rows = list(self.report.categories)
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
        if self.report.income:
            self._append_spending(index, income=True)
            self._append_income_detail(index)
        self.content.append(
            self._label(f"Category comparison — {self.report.totals[index].label}", heading=True)
        )
        self.content.append(
            self._label(
                "First bar: plan; second bar: period actual (everything dated in the "
                "period). Remaining uses actual through as-of."
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
            summary = self._label(
                f"Plan {value.planned.format()} · Period actual {value.actual.format()} · "
                f"Period variance {variance} · Carry {carry} · Remaining {remaining}"
            )
            # Wrapped, so a narrower window keeps every figure in view (#311).
            summary.set_wrap(True)
            summary.set_max_width_chars(48)
            grid.attach(summary, 2, i, 1, 1)
        self.content.append(self._label(f"{selected.full_name} trend", heading=True))
        trend = self._charts(
            (category_trend_chart(selected, self.report.plan.report.as_of, self._currency),),
            index,
        )
        self.trend_chart = trend[0] if trend else None
        grid = Gtk.Grid(column_spacing=12, row_spacing=3)
        for column, heading in enumerate(
            ("Period", "Plan", "Period actual", "Period variance", "Carry in", "Remaining")
        ):
            grid.attach(self._label(heading, heading=True), column, 0, 1, 1)
        for line, period in enumerate(selected.periods, 1):
            remaining = (
                period.remaining.format()
                if period.remaining is not None
                else period.remaining_reason or "—"
            )
            for column, text in enumerate(
                (
                    f"{'▸ ' if line - 1 == index else ''}{period.label}",
                    period.planned.format(),
                    period.actual.format(),
                    period.variance.format() if period.variance is not None else "—",
                    period.carry_in.format() if period.carry_in is not None else "—",
                    remaining,
                )
            ):
                label = self._label(text)
                if column:
                    label.set_xalign(1)
                    label.add_css_class("numeric")
                grid.attach(label, column, line, 1, 1)
        self.trend_table = grid
        self.content.append(grid)
        drilldown = self._drilldown(selected.account, index)
        if drilldown is None:
            return
        self.content.append(self._label("Merchants — actual only", heading=True))
        self.content.append(self._label("The category plan is unallocated across merchants."))
        for group in drilldown.merchants:
            self.content.append(self._label(f"{group.name} · {group.amount.format()}"))
            for transaction in group.transactions:
                self.content.append(
                    self._label(
                        f"    {transaction.post_date:%Y-%m-%d} · "
                        f"{transaction.description.strip() or 'Unknown merchant'} · "
                        f"{transaction.amount.format()}"
                    )
                )
