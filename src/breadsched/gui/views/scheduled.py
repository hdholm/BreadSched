"""Scheduled transactions, in two views.

Definitions and upcoming occurrences answer different questions and are separated
accordingly. *Scheduled* is the standing arrangement — what recurs, how often, and
whether it is a commitment or an estimate. *Upcoming* is the diary — what is due,
what is overdue, and what needs posting. Mixing them meant neither list could be
sorted usefully, because sorting by date is meaningless for a definition and
sorting by name is meaningless for a due list.

Selecting a definition expands its splits as rows beneath it, the same way the
register expands a transaction, so a loan payment shows its interest and principal
legs where they belong.
"""

from __future__ import annotations

from datetime import date, timedelta

from ...gen.engine import schedule
from ...gen.lib import PeriodType
from ...gen.lib.money import Money
from ..gi_setup import Gio, Gtk, Pango
from ._base import BaseView, Row, column, column_menu, sorted_model, unwrap

__all__ = ["ScheduledView", "UpcomingView", "ScheduleSplitRow"]


class ScheduleSplitRow:
    """One leg of a scheduled transaction, shown beneath its definition."""

    __slots__ = ("split", "schedule", "db")

    def __init__(self, split, schedule_object, db) -> None:
        self.split = split
        self.schedule = schedule_object
        self.db = db

    @property
    def account_name(self) -> str:
        account = self.db.get_account(self.split.account)
        return self.db.full_name(account) if account else "(unknown account)"

    def amount_text(self) -> str:
        if self.split.formula:
            # A loan leg is a formula. Showing the resolved figure alongside the
            # expression explains why next month's number will not match this one.
            resolved = self.split.resolve(self.schedule.variables)
            return f"{resolved.format(parens_negative=True)}  ={self.split.formula}"
        return (self.split.amount or Money(0)).format(parens_negative=True)


def _is_split(item) -> bool:
    return isinstance(item, ScheduleSplitRow)


def _kind_of(item) -> str:
    if _is_split(item):
        return ""
    return "Estimate" if item.placeholder else "Commitment"


def _amount_of(item) -> str:
    if _is_split(item):
        return item.amount_text()
    return item.amount().format()


class ScheduledView(BaseView):
    """The definitions: what recurs, and how."""

    WATCHES = ("database-changed", "scheduled-add", "scheduled-update",
               "scheduled-delete")

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self._build()

    def _build(self) -> None:
        bar = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{side}")(8)

        title = Gtk.Label(label="Scheduled transactions", xalign=0)
        title.add_css_class("category-title")
        title.set_hexpand(True)
        bar.append(title)

        new_button = Gtk.Button(label="New scheduled…")
        new_button.set_tooltip_text("Create a scheduled transaction or an estimate")
        new_button.connect("clicked", self._on_new_clicked)
        bar.append(new_button)

        suggest_button = Gtk.Button(label="Suggest from history…")
        suggest_button.set_tooltip_text(
            "Propose planning estimates from completed category history"
        )
        suggest_button.connect("clicked", self._on_suggest_clicked)
        bar.append(suggest_button)

        self.edit_button = Gtk.Button(label="View / Edit…")
        self.edit_button.set_tooltip_text(
            "View the selected schedule; edit it when BreadSched can preserve it safely"
        )
        self.edit_button.set_sensitive(False)
        self.edit_button.connect("clicked", self._on_edit_clicked)
        bar.append(self.edit_button)

        loan_button = Gtk.Button(label="New loan…")
        loan_button.set_tooltip_text("Set up a loan with calculated interest")
        loan_button.connect("clicked", self._on_loan_clicked)
        bar.append(loan_button)

        self.definitions_view = Gtk.ColumnView()
        self.definitions_view.set_show_row_separators(True)
        self.definitions_view.append_column(self._name_column())
        self.definitions_view.append_column(
            column("Kind", _kind_of, sort_key=_kind_of)
        )
        self.definitions_view.append_column(
            column(
                "Frequency",
                lambda s: "" if _is_split(s) else s.recurrence.describe(),
                expand=True,
            )
        )
        self.definitions_view.append_column(
            column("Next", lambda s: "" if _is_split(s) else self._next_text(s))
        )
        self.definitions_view.append_column(
            column("Amount", _amount_of, numeric=True)
        )
        self.definitions_view.append_column(
            column(
                "Planning purpose",
                lambda s: (
                    s.split.planning_flow.label
                    if _is_split(s) and s.split.planning_flow is not None
                    else ""
                ),
            )
        )
        self.definitions_view.append_column(
            column(
                "Automatic",
                lambda s: "" if _is_split(s) else ("yes" if s.auto_create else "no"),
            )
        )
        self.definitions_view.append_column(
            column(
                "Enabled",
                lambda s: "" if _is_split(s) else ("yes" if s.enabled else "no"),
            )
        )
        bar.append(
            column_menu("scheduled", self.definitions_view, self._settings())
        )
        self.append(bar)

        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("dim")
        for side in ("start", "end"):
            getattr(self.status, f"set_margin_{side}")(12)
        self.append(self.status)

        scroller = Gtk.ScrolledWindow(child=self.definitions_view)
        scroller.set_vexpand(True)
        self.append(scroller)

    def _settings(self):
        return getattr(self.manager.get_application(), "view_settings", None)

    # ----------------------------------------------------------------- columns

    def _name_column(self) -> Gtk.ColumnViewColumn:
        """The name cell carries the expander, so splits sit beneath their schedule."""
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
            if _is_split(payload):
                label.set_text(payload.account_name)
                label.add_css_class("dim")
            else:
                label.set_text(payload.name)
                label.remove_css_class("dim")

        factory.connect("setup", on_setup)
        factory.connect("bind", on_bind)
        col = Gtk.ColumnViewColumn(title="Name", factory=factory)
        col.set_expand(True)
        col.set_resizable(True)
        return col

    # ------------------------------------------------------------------- model

    def refresh(self) -> None:
        if self.db is None:
            return
        definitions = Gio.ListStore.new(Row)
        schedules = list(self.db.iter_scheduled())
        for sched in schedules:
            definitions.append(Row(sched))

        tree = Gtk.TreeListModel.new(definitions, False, False, self._split_children)
        selection = Gtk.SingleSelection(
            model=sorted_model(self.definitions_view, tree)
        )
        selection.connect("notify::selected", self._on_selected)
        self.definitions_view.set_model(selection)
        # Gtk.SingleSelection auto-selects the first row before our notify handler
        # is connected.  Synchronize action sensitivity and expansion explicitly so
        # the visibly selected row is also the application's selected row.
        self._on_selected(selection, None)

        estimates = sum(1 for s in schedules if s.placeholder)
        self.status.set_text(
            f"{len(schedules)} scheduled: {len(schedules) - estimates} commitment(s), "
            f"{estimates} estimate(s)"
        )

    def select_schedule(self, handle: str) -> None:
        """Select and expand one schedule, arriving from another view."""
        selection = self.definitions_view.get_model()
        if selection is None:
            return
        for index in range(selection.get_n_items()):
            row = selection.get_item(index)
            payload = unwrap(row)
            if getattr(payload, "handle", None) == handle:
                selection.set_selected(index)
                if hasattr(row, "set_expanded"):
                    row.set_expanded(True)
                return

    def _split_children(self, item):
        payload = item.payload if isinstance(item, Row) else item
        if _is_split(payload) or self.db is None or not payload.splits:
            return None
        store = Gio.ListStore.new(Row)
        for split in payload.splits:
            store.append(Row(ScheduleSplitRow(split, payload, self.db)))
        return store

    def _next_text(self, sched) -> str:
        following = sched.recurrence.next_after(date.today() - timedelta(days=1))
        return following.isoformat() if following else "finished"

    def _splits_summary(self, sched) -> str:
        """Every leg as text. Kept for the web and CLI views, and for tests."""
        if self.db is None or not sched.splits:
            return "(no splits)"
        parts = []
        for split in sched.splits:
            row = ScheduleSplitRow(split, sched, self.db)
            parts.append(f"{row.account_name} {row.amount_text()}")
        return "  |  ".join(parts)

    # ----------------------------------------------------------------- actions

    def _on_selected(self, selection, _param) -> None:
        position = selection.get_selected()
        model = selection.get_model()
        for index in range(model.get_n_items()):
            tree_row = model.get_item(index)
            if tree_row is None or tree_row.get_depth() != 0:
                continue
            tree_row.set_expanded(index == position)
        selected = selection.get_selected_item()
        payload = unwrap(selected) if selected is not None else None
        self.edit_button.set_sensitive(
            payload is not None and not _is_split(payload)
        )

    def _editability_reason(self, sched) -> str:
        if sched is None or _is_split(sched):
            return "No scheduled transaction is selected."
        has_formula = any(split.formula for split in sched.splits)
        supported_periods = {
            PeriodType.DAY,
            PeriodType.WEEK,
            PeriodType.SEMI_MONTH,
            PeriodType.MONTH,
            PeriodType.YEAR,
            PeriodType.ONCE,
        }
        if sched.recurrence.period not in supported_periods:
            return (
                "This schedule uses a recurrence that the fixed schedule editor "
                "cannot yet reproduce without changing its meaning."
            )
        if has_formula:
            # Formula expressions and variables stay protected, but the schedule's
            # metadata/recurrence can be edited without reconstructing those splits.
            return ""
        if len(getattr(sched, "splits", [])) < 2:
            return (
                "This imported schedule does not have enough split information for "
                "the fixed schedule editor to reproduce it safely."
            )
        classes = []
        funding_candidates = 0
        planning_flow_splits = 0
        for split in sched.splits:
            account = (
                self.db.get_account(split.account) if self.db is not None else None
            )
            account_class = account.account_class.value if account is not None else ""
            classes.append(account_class)
            if split.planning_flow is not None:
                planning_flow_splits += 1
            if (
                account_class not in {"income", "expense"}
                and split.planning_flow is None
            ):
                funding_candidates += 1
        has_income_expense = any(value in {"income", "expense"} for value in classes)
        ordinary_balance_transfer = False
        if not has_income_expense and planning_flow_splits == 0:
            ordinary_balance_transfer = all(
                value in {"asset", "liability"} for value in classes
            )
            if ordinary_balance_transfer:
                resolved = [split.resolve(sched.variables) for split in sched.splits]
                positives = [value for value in resolved if value > 0]
                negatives = [value for value in resolved if value < 0]
                ordinary_balance_transfer = (
                    len(positives) == 1
                    and bool(negatives)
                    and sum(resolved, Money(0)) == Money(0)
                )
        if (
            not has_income_expense
            and planning_flow_splits < 1
            and not ordinary_balance_transfer
        ):
            return (
                "This schedule has neither an Income/Expense leg, an explicit "
                "planning-purpose leg, nor an unambiguous fixed balance-sheet "
                "transfer that the schedule editor can use as its primary amount."
            )
        if funding_candidates < 1:
            return (
                "This schedule has no ordinary funding split that the fixed schedule "
                "editor can preserve."
            )
        return ""

    def _on_edit_clicked(self, _button) -> None:
        if self.db is None:
            return
        selection = self.definitions_view.get_model()
        selected = selection.get_selected_item() if selection is not None else None
        sched = unwrap(selected) if selected is not None else None
        if sched is None or _is_split(sched):
            return
        from ..dialogs.schedule_dialog import ScheduleDialog

        reason = self._editability_reason(sched)
        dialog = ScheduleDialog(
            self.get_root(),
            self.db,
            source=sched,
            read_only_reason=reason or None,
        )
        dialog.connect("close-request", lambda *_: (self.refresh(), False)[1])
        dialog.present()

    def _on_new_clicked(self, _button) -> None:
        if self.db is None:
            return
        from ..dialogs.schedule_dialog import ScheduleDialog

        dialog = ScheduleDialog(self.get_root(), self.db)
        dialog.connect("close-request", lambda *_: (self.refresh(), False)[1])
        dialog.present()

    def _on_suggest_clicked(self, _button) -> None:
        if self.db is None:
            return
        from ..dialogs.historical_estimates_dialog import HistoricalEstimatesDialog

        dialog = HistoricalEstimatesDialog(self.get_root(), self.db)
        dialog.connect("close-request", lambda *_: (self.refresh(), False)[1])
        dialog.present()

    def _on_loan_clicked(self, _button) -> None:
        if self.db is None:
            return
        from ..dialogs.loan_dialog import LoanDialog

        dialog = LoanDialog(self.get_root(), self.db)
        dialog.connect("close-request", lambda *_: (self.refresh(), False)[1])
        dialog.present()


class UpcomingView(BaseView):
    """The diary: what is due, what is overdue, and what needs posting."""

    WATCHES = ("database-changed", "scheduled-add", "scheduled-update",
               "scheduled-delete", "transaction-add")

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self._horizon = 30
        self._build()

    def _build(self) -> None:
        bar = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{side}")(8)

        title = Gtk.Label(label="Upcoming", xalign=0)
        title.add_css_class("category-title")
        title.set_hexpand(True)
        bar.append(title)

        bar.append(Gtk.Label(label="Look ahead"))
        self.horizon_picker = Gtk.DropDown.new_from_strings(
            ["Due now", "7 days", "30 days", "90 days", "1 year"]
        )
        self.horizon_picker.set_selected(2)
        self.horizon_picker.connect("notify::selected", self._on_horizon_changed)
        bar.append(self.horizon_picker)

        self.post_button = Gtk.Button(label="Review due…")
        self.post_button.add_css_class("suggested-action")
        self.post_button.connect("clicked", self._on_post_clicked)
        bar.append(self.post_button)

        self.upcoming_view = Gtk.ColumnView()
        self.upcoming_view.set_show_row_separators(True)
        self.upcoming_view.append_column(
            column("Due", self._due_text, sort_key=lambda o: o.when)
        )
        self.upcoming_view.append_column(
            column("Schedule", lambda o: o.name, expand=True)
        )
        self.upcoming_view.append_column(column("Kind", lambda o: _kind_of(o.schedule)))
        self.upcoming_view.append_column(
            column("Frequency", lambda o: o.schedule.recurrence.describe(), expand=True)
        )
        self.upcoming_view.append_column(
            column("Amount", lambda o: o.amount.format(), numeric=True)
        )
        bar.append(column_menu("upcoming", self.upcoming_view, self._settings()))
        self.append(bar)

        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("dim")
        for side in ("start", "end"):
            getattr(self.status, f"set_margin_{side}")(12)
        self.append(self.status)

        scroller = Gtk.ScrolledWindow(child=self.upcoming_view)
        scroller.set_vexpand(True)
        self.append(scroller)

    def _settings(self):
        return getattr(self.manager.get_application(), "view_settings", None)

    def refresh(self) -> None:
        if self.db is None:
            return
        today = date.today()
        occurrences = schedule.due_occurrences(
            self.db, as_of=today, horizon_days=self._horizon
        )
        store = Gio.ListStore.new(Row)
        for occurrence in occurrences:
            store.append(Row(occurrence))
        self.upcoming_view.set_model(
            Gtk.SingleSelection(model=sorted_model(self.upcoming_view, store))
        )

        overdue = [o for o in occurrences if o.when <= today]
        if overdue:
            self.status.set_text(
                f"{len(overdue)} occurrence(s) overdue, "
                f"oldest {overdue[0].when.isoformat()}"
            )
            self.status.add_css_class("negative")
        else:
            self.status.set_text(
                f"Nothing overdue; {len(occurrences)} occurrence(s) in the next "
                f"{self._horizon} days"
            )
            self.status.remove_css_class("negative")
        self.post_button.set_sensitive(bool(overdue))

    def _due_text(self, occurrence) -> str:
        today = date.today()
        if occurrence.when < today:
            return f"{occurrence.when.isoformat()}  (overdue)"
        if occurrence.when == today:
            return f"{occurrence.when.isoformat()}  (today)"
        return occurrence.when.isoformat()

    def _on_horizon_changed(self, picker, _param) -> None:
        self._horizon = [0, 7, 30, 90, 365][picker.get_selected()]
        self.refresh()

    def _on_post_clicked(self, _button) -> None:
        """Open the same review dialog the book shows on opening.

        Posting everything due in one click is the behaviour this replaced: it is
        one keystroke away from writing a month of transactions nobody read.
        """
        if self.db is None:
            return
        from ..dialogs.due_dialog import DueDialog

        due = schedule.due_occurrences(self.db, horizon_days=0)
        if not due:
            self.status.set_text("Nothing is due")
            return
        dialog = DueDialog(self.get_root(), self.db, due)
        dialog.connect("close-request", lambda *_: (self.refresh(), False)[1])
        dialog.present()
