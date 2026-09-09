"""Budget versus actual.

Built on a ``Gtk.ColumnView`` over a tree model, which buys three things at once:
columns that drag to resize, sections that collapse, and a header menu for hiding
columns — the same behaviour as every other list view rather than a special case.

The grid is period-by-period rather than a single annual column, because the whole
argument for a cash-flow budget is that timing matters. Rows are grouped by account
class and collapse to their totals, so a household with sixty expense categories
can still see whether the year works.

The bottom row carries net cash flow per period and is highlighted where it goes
negative. That row is the answer to "will this plan actually work", and it should
be findable without reading any of the rows above it.
"""

from __future__ import annotations

from ...gen.engine import cashflow
from ...gen.lib.account import AccountClass
from ...gen.lib.money import Money
from ..gi_setup import Gio, Gtk, Pango
from ._base import BaseView, Row, column_menu, unwrap

__all__ = ["BudgetView", "SectionRow", "LineRow", "TotalsRow"]

#: Account class -> heading, in the order a budget reads.
_SECTIONS = [
    (AccountClass.INCOME, "Income"),
    (AccountClass.EXPENSE, "Expenses"),
    (AccountClass.ASSET, "Transfers to savings"),
    (AccountClass.LIABILITY, "Debt payments"),
]


class LineRow:
    """One account's budgeted and actual figures, plus any accounts beneath it.

    A budget reads the way the chart of accounts does: ``Utilities`` above
    ``Utilities:Gas`` and ``Utilities:Electricity``, each parent totalling what sits
    under it. A flat list of colon-joined names makes the reader do that grouping in
    their head, every time.
    """

    __slots__ = ("line", "label", "children")

    def __init__(self, line, label: str | None = None, children=None) -> None:
        self.line = line
        self.label = label or line.name
        self.children: list[LineRow] = children or []

    @property
    def title(self) -> str:
        return self.label

    @property
    def account(self) -> str:
        return self.line.account if self.line else ""

    def budgeted(self, period: int) -> Money:
        total = self.line.periods[period].budgeted if self.line else Money(0)
        for child in self.children:
            total = total + child.budgeted(period)
        return total

    def actual(self, period: int) -> Money:
        total = self.line.periods[period].actual if self.line else Money(0)
        for child in self.children:
            total = total + child.actual(period)
        return total

    @property
    def editable(self) -> bool:
        # A parent shows the total of what is under it; typing over that total
        # would have to guess how to spread the change across its children.
        return self.line is not None and not self.children


class SectionRow:
    """An account class, collapsing the lines beneath it to their totals."""

    __slots__ = ("account_class", "title", "lines", "roots")

    def __init__(
        self, account_class: AccountClass, title: str, lines: list, roots=None
    ) -> None:
        self.account_class = account_class
        self.title = title
        self.lines = lines
        #: Top-level LineRows; each may have children of its own.
        self.roots: list = roots or []

    def budgeted(self, period: int) -> Money:
        total = Money(0)
        for line in self.lines:
            total = total + line.periods[period].budgeted
        return total

    def actual(self, period: int) -> Money:
        total = Money(0)
        for line in self.lines:
            total = total + line.periods[period].actual
        return total

    @property
    def editable(self) -> bool:
        return False


class TotalsRow:
    """Net cash flow: income less expenses, period by period."""

    __slots__ = ("report", "title")

    def __init__(self, report) -> None:
        self.report = report
        self.title = "Net cash flow"

    def budgeted(self, period: int) -> Money:
        return self.report.net_cash_flow(period)

    def actual(self, period: int) -> Money:
        return self.report.net_cash_flow(period, actual=True)

    @property
    def editable(self) -> bool:
        return False


class BudgetView(BaseView):
    """A spreadsheet-shaped budget report."""

    WATCHES = ("database-changed", "budget-add", "budget-update", "budget-delete",
               "transaction-add", "transaction-update", "transaction-delete")

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self.budget_handle: str | None = None
        self._show_actuals = True
        self._report = None
        #: True while the grid is being rebuilt, so filling a cell does not look
        #: like a user edit and write back a figure that never changed.
        self._rendering = False
        #: True while this view is writing its own edit, so the signal that write
        #: emits does not rebuild the grid underneath the cell being left.
        self._committing = False
        self._periods = 0
        self._build()

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        self.bar = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(self.bar, f"set_margin_{side}")(8)

        self.budget_picker = Gtk.DropDown()
        self.budget_picker.connect("notify::selected", self._on_budget_changed)
        self.bar.append(self.budget_picker)

        new_button = Gtk.Button(label="New budget…")
        new_button.set_action_name("app.new-budget")
        self.bar.append(new_button)

        self.use_button = Gtk.Button(label="Use this budget")
        self.use_button.set_tooltip_text(
            "The dashboard and new projections follow the budget in use"
        )
        self.use_button.connect("clicked", self._on_use_clicked)
        self.bar.append(self.use_button)

        members_button = Gtk.Button(label="Flows…")
        members_button.set_tooltip_text(
            "Choose which recurring flows this budget counts"
        )
        members_button.connect("clicked", self._on_members_clicked)
        self.bar.append(members_button)

        clone_button = Gtk.Button(label="Clone…")
        clone_button.set_tooltip_text("Copy this budget to try an alternative plan")
        clone_button.connect("clicked", self._on_clone_clicked)
        self.bar.append(clone_button)

        actuals = Gtk.ToggleButton(label="Show actuals", active=True)
        actuals.connect("toggled", self._on_actuals_toggled)
        self.bar.append(actuals)

        expand_button = Gtk.Button(label="Expand all")
        expand_button.connect("clicked", lambda *_: self._set_all_expanded(True))
        self.bar.append(expand_button)

        collapse_button = Gtk.Button(label="Collapse all")
        collapse_button.connect("clicked", lambda *_: self._set_all_expanded(False))
        self.bar.append(collapse_button)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        self.bar.append(spacer)

        self.headline = Gtk.Label(xalign=1)
        self.headline.add_css_class("dim")
        self.bar.append(self.headline)
        self.append(self.bar)

        self.column_view = Gtk.ColumnView()
        self.column_view.set_show_row_separators(True)
        self._menu_button: Gtk.Widget | None = None

        scroller = Gtk.ScrolledWindow(child=self.column_view)
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.append(scroller)

    def _settings(self):
        return getattr(self.manager.get_application(), "view_settings", None)

    # ----------------------------------------------------------------- columns

    def _rebuild_columns(self, report) -> None:
        """Columns depend on the budget, so they are rebuilt when it changes."""
        existing = self.column_view.get_columns()
        for index in reversed(range(existing.get_n_items())):
            self.column_view.remove_column(existing.get_item(index))

        self.column_view.append_column(self._account_column())
        for period, label in enumerate(report.labels):
            self.column_view.append_column(self._period_column(label, period))
            if self._show_actuals:
                self.column_view.append_column(
                    self._readonly_column(f"{label} actual", period, actual=True)
                )
        self.column_view.append_column(self._total_column("Total", actual=False))
        if self._show_actuals:
            self.column_view.append_column(
                self._total_column("Total actual", actual=True)
            )

        if self._menu_button is not None:
            self.bar.remove(self._menu_button)
        self._menu_button = column_menu(
            "budget", self.column_view, self._settings()
        )
        self.bar.append(self._menu_button)

    def _account_column(self) -> Gtk.ColumnViewColumn:
        """The account cell carries the expander, so sections collapse."""
        factory = Gtk.SignalListItemFactory()

        def on_setup(_factory, item) -> None:
            expander = Gtk.TreeExpander()
            label = Gtk.Label(xalign=0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            expander.set_child(label)
            item.set_child(expander)

        def on_bind(_factory, item) -> None:
            tree_row = item.get_item()
            expander = item.get_child()
            expander.set_list_row(tree_row)
            payload = unwrap(tree_row)
            label = expander.get_child()
            label.set_text(payload.title)
            bold = isinstance(payload, (SectionRow, TotalsRow))
            if bold:
                label.add_css_class("total-row")
            else:
                label.remove_css_class("total-row")

        factory.connect("setup", on_setup)
        factory.connect("bind", on_bind)
        col = Gtk.ColumnViewColumn(title="Account", factory=factory)
        col.set_expand(True)
        col.set_resizable(True)
        col.set_fixed_width(260)
        return col

    def _period_column(self, title: str, period: int) -> Gtk.ColumnViewColumn:
        """An editable budgeted figure. Sections and totals show, but do not edit."""
        factory = Gtk.SignalListItemFactory()

        def on_setup(_factory, item) -> None:
            entry = Gtk.Entry(xalign=1, width_chars=9, has_frame=False)
            entry.add_css_class("numeric")
            item.set_child(entry)

        def on_bind(_factory, item) -> None:
            payload = unwrap(item.get_item())
            entry = item.get_child()
            amount = payload.budgeted(period)
            self._rendering = True
            try:
                entry.set_text(f"{amount.to_decimal():,.2f}" if amount else "")
            finally:
                self._rendering = False

            editable = payload.editable
            entry.set_editable(editable)
            entry.set_can_focus(editable)
            if editable:
                entry.remove_css_class("total-row")
                self._attach_commit(entry, payload.account, period)
            else:
                entry.add_css_class("total-row")
            if amount < 0:
                entry.add_css_class("negative")
            else:
                entry.remove_css_class("negative")
            if isinstance(payload, TotalsRow) and amount < 0:
                entry.add_css_class("shortfall")
            else:
                entry.remove_css_class("shortfall")

        factory.connect("setup", on_setup)
        factory.connect("bind", on_bind)
        col = Gtk.ColumnViewColumn(title=title, factory=factory)
        col.set_resizable(True)
        col.set_fixed_width(110)
        return col

    def _readonly_column(
        self, title: str, period: int, actual: bool
    ) -> Gtk.ColumnViewColumn:
        return self._label_column(
            title, lambda payload: payload.actual(period) if actual
            else payload.budgeted(period)
        )

    def _total_column(self, title: str, actual: bool) -> Gtk.ColumnViewColumn:
        def value(payload) -> Money:
            total = Money(0)
            for period in range(self._periods):
                total = total + (
                    payload.actual(period) if actual else payload.budgeted(period)
                )
            return total

        return self._label_column(title, value, bold=True)

    def _label_column(self, title: str, value, bold: bool = False):
        factory = Gtk.SignalListItemFactory()

        def on_setup(_factory, item) -> None:
            label = Gtk.Label(xalign=1)
            label.add_css_class("numeric")
            if bold:
                label.add_css_class("total-row")
            item.set_child(label)

        def on_bind(_factory, item) -> None:
            payload = unwrap(item.get_item())
            label = item.get_child()
            amount = value(payload)
            label.set_text(amount.format(parens_negative=True) if amount else "")
            if amount < 0:
                label.add_css_class("negative")
            else:
                label.remove_css_class("negative")

        factory.connect("setup", on_setup)
        factory.connect("bind", on_bind)
        col = Gtk.ColumnViewColumn(title=title, factory=factory)
        col.set_resizable(True)
        col.set_fixed_width(110)
        return col

    # ------------------------------------------------------------------- model

    def refresh(self) -> None:
        if self.db is None:
            return
        budgets = list(self.db.iter_budgets())
        self._budgets = budgets
        model = Gtk.StringList()
        for budget in budgets:
            model.append(budget.name)
        self.budget_picker.set_model(model)

        if not budgets:
            self.column_view.set_model(None)
            self.headline.set_text("This book has no budgets yet")
            return
        if self.budget_handle is None:
            self.budget_handle = budgets[0].handle
        for index, budget in enumerate(budgets):
            if budget.handle == self.budget_handle:
                self.budget_picker.set_selected(index)
                break

        budget = self.db.get_budget(self.budget_handle)
        if budget is None:
            return
        report = cashflow.build_report(self.db, budget)
        self._report = report
        self._refresh_use_button()
        self._periods = budget.periods
        self._rebuild_columns(report)
        self._populate(report)

    def _populate(self, report) -> None:
        roots = Gio.ListStore.new(Row)
        self._sections: list[SectionRow] = []
        for account_class, title in _SECTIONS:
            lines = report.by_class(account_class)
            if not lines:
                continue
            section = SectionRow(
                account_class, title, lines, self._hierarchy(lines)
            )
            self._sections.append(section)
            roots.append(Row(section))
        roots.append(Row(TotalsRow(report)))

        tree = Gtk.TreeListModel.new(roots, False, True, self._children_of)
        self.column_view.set_model(Gtk.NoSelection(model=tree))
        self._refresh_headline(report)

    def _hierarchy(self, lines: list) -> list[LineRow]:
        """Nest the report's lines by their account path.

        Ancestors with no budget line of their own are synthesised, so a child is
        never orphaned under a heading that does not exist. Those placeholders show
        the total of what is beneath them and cannot be edited.
        """
        by_path: dict[str, LineRow] = {
            line.name: LineRow(line, line.name.rsplit(":", 1)[-1]) for line in lines
        }

        def ensure(path: str) -> LineRow:
            """Return the row for ``path``, creating its ancestors as needed."""
            if path not in by_path:
                by_path[path] = LineRow(None, path.rsplit(":", 1)[-1])
                _attach(path)
            return by_path[path]

        def _attach(path: str) -> None:
            if ":" not in path:
                roots.append(by_path[path])
                return
            parent = ensure(path.rsplit(":", 1)[0])
            parent.children.append(by_path[path])

        roots: list[LineRow] = []
        # Shallowest first, so a parent exists before anything is hung off it.
        for path in sorted(by_path, key=lambda name: name.count(":")):
            _attach(path)
        return sorted(roots, key=lambda row: row.title.lower())

    def _children_of(self, item):
        payload = item.payload if isinstance(item, Row) else item
        if isinstance(payload, SectionRow):
            store = Gio.ListStore.new(Row)
            for line in payload.roots:
                store.append(Row(line))
            return store
        if isinstance(payload, LineRow) and payload.children:
            store = Gio.ListStore.new(Row)
            for child in sorted(payload.children, key=lambda r: r.title.lower()):
                store.append(Row(child))
            return store
        return None

    def _set_all_expanded(self, expanded: bool) -> None:
        selection = self.column_view.get_model()
        if selection is None:
            return
        model = selection.get_model()
        for index in range(model.get_n_items()):
            tree_row = model.get_item(index)
            if tree_row is not None and tree_row.get_depth() == 0:
                tree_row.set_expanded(expanded)

    def _refresh_headline(self, report) -> None:
        summary = report.annual_summary()
        shortfalls = report.shortfall_periods()
        headline = (
            f"Income {summary['income'].format()}   "
            f"Expenses {summary['expense'].format()}   "
            f"Net for the period {summary['net'].format(parens_negative=True)}"
        )
        if shortfalls:
            # A year that ends ahead can still run dry mid-way, so say both.
            first = report.labels[min(shortfalls)]
            headline += (
                f"   -   shortfall in {len(shortfalls)} period(s), first in {first}"
            )
        self.headline.set_text(headline)
        self.headline.remove_css_class("dim")
        if summary["net"] < 0 or shortfalls:
            self.headline.add_css_class("negative")
        else:
            self.headline.remove_css_class("negative")

    # ------------------------------------------------------------------ editing

    def _attach_commit(self, entry: Gtk.Entry, account: str, period: int) -> None:
        """Wire one cell to write back, replacing any previous wiring.

        Cell widgets are recycled by the list view, so a handler left from the row
        this widget showed a moment ago would write the new value to the old
        account.
        """
        previous = getattr(entry, "_commit_handler", None)
        if previous is not None:
            entry.disconnect(previous)
        controller = getattr(entry, "_focus_controller", None)
        if controller is not None:
            entry.remove_controller(controller)

        entry._commit_handler = entry.connect(
            "activate", lambda widget: self._commit(widget, account, period)
        )
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda _c: self._commit(entry, account, period))
        entry.add_controller(focus)
        entry._focus_controller = focus

    def _editable(self, account: str, period: int, amount: Money) -> Gtk.Entry:
        """A single budgeted figure the user can type over."""
        entry = Gtk.Entry(xalign=1, width_chars=9, has_frame=False)
        entry.set_text(f"{amount.to_decimal():,.2f}" if amount else "")
        entry.add_css_class("numeric")
        entry.set_placeholder_text("0.00")
        self._attach_commit(entry, account, period)
        return entry

    def _on_change(self, *_args) -> None:
        """The grid is rebuilt out of band; see BaseView.schedule_refresh."""
        self.schedule_refresh()

    def _commit(self, entry: Gtk.Entry, account: str, period: int) -> None:
        if self.db is None or self.budget_handle is None or self._rendering:
            return
        if self._committing:
            return
        budget = self.db.get_budget(self.budget_handle)
        if budget is None:
            return

        text = entry.get_text().strip()
        try:
            value = Money(text) if text else Money(0)
        except (ValueError, ArithmeticError):
            # Say why once rather than blocking the keystroke, and leave the stored
            # figure alone.
            entry.add_css_class("negative")
            self.headline.set_text(f"{text!r} is not an amount")
            return
        entry.remove_css_class("negative")

        if value == budget.amount(account, period):
            return
        budget.set_amount(account, period, value)
        # The write emits budget-update, which this view listens for. Guarding
        # against re-entering our own edit, and letting the repaint happen on the
        # idle, keeps the cell that is being left out of a rebuild that is running
        # inside its own focus handler.
        self._committing = True
        try:
            with self.db.transaction("Edit budget") as txn:
                self.db.commit_budget(budget, txn)
        finally:
            self._committing = False
        self.schedule_refresh()

    # ----------------------------------------------------------------- actions

    def _current_budget(self):
        if self.db is None or self.budget_handle is None:
            return None
        return self.db.get_budget(self.budget_handle)

    def _refresh_use_button(self) -> None:
        from ...gen.engine import budgeting

        if self.db is None:
            return
        current = budgeting.current_budget(self.db)
        in_use = current is not None and current.handle == self.budget_handle
        self.use_button.set_sensitive(not in_use)
        self.use_button.set_label("In use" if in_use else "Use this budget")

    def _on_use_clicked(self, _button) -> None:
        from ...gen.engine import budgeting

        budget = self._current_budget()
        if budget is None:
            return
        budgeting.set_current_budget(self.db, budget)
        self._refresh_use_button()
        # The dashboard reads the current budget, so it has to be told.
        self.manager.refresh_views()

    def _on_members_clicked(self, _button) -> None:
        budget = self._current_budget()
        if budget is None:
            return
        from ..dialogs.membership_dialog import MembershipDialog

        dialog = MembershipDialog(self.get_root(), self.db, budget)
        dialog.connect(
            "close-request", lambda *_: (self.manager.refresh_views(), False)[1]
        )
        dialog.present()

    def _on_clone_clicked(self, _button) -> None:
        budget = self._current_budget()
        if budget is None:
            return
        from ...gen.engine import budgeting

        window = Gtk.Window(title="Clone budget", transient_for=self.get_root(),
                            modal=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(16)
        box.append(Gtk.Label(label=f"Copy {budget.name!r} to a new budget", xalign=0))
        entry = Gtk.Entry(text=f"{budget.name} (copy)")
        box.append(entry)
        status = Gtk.Label(xalign=0)
        box.append(status)

        def on_clone(_button) -> None:
            name = entry.get_text().strip()
            if not name:
                status.set_text("Give the copy a name")
                return
            if any(b.name == name for b in self.db.iter_budgets()):
                status.set_text(f"A budget named {name!r} already exists")
                return
            copy = budgeting.clone_budget(self.db, budget, name)
            self.budget_handle = copy.handle
            window.close()
            self.refresh()

        button = Gtk.Button(label="Clone")
        button.add_css_class("suggested-action")
        button.connect("clicked", on_clone)
        box.append(button)
        window.set_child(box)
        window.present()

    def _on_budget_changed(self, picker, _param) -> None:
        index = picker.get_selected()
        if 0 <= index < len(getattr(self, "_budgets", [])):
            handle = self._budgets[index].handle
            if handle != self.budget_handle:
                self.budget_handle = handle
                self.refresh()

    def _on_actuals_toggled(self, button: Gtk.ToggleButton) -> None:
        self._show_actuals = button.get_active()
        self.refresh()
