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

from ...gen.db.sqlite import DbSQLite
from ...gen.engine import schedule
from ...gen.engine.schedule import AccountPaymentDefinition
from ...gen.lib import ScheduledTransaction
from ...gen.lib.money import Money
from ..gi_setup import Gio, Gtk, Pango
from ._base import BaseView, Row, column, column_menu, sorted_model, unwrap

__all__ = ["ScheduledView", "UpcomingView", "ScheduleSplitRow"]


def _representative_date(schedule_object: ScheduledTransaction) -> date:
    """Date whose occurrence context should be shown for a definition."""
    today = date.today()
    return (
        schedule_object.recurrence.next_after(today - timedelta(days=1))
        or schedule_object.recurrence.last_occurrence()
        or schedule_object.recurrence.start
    )


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
            when = _representative_date(self.schedule)
            resolved = self.split.resolve(self.schedule.context(when))
            return f"{resolved.format(parens_negative=True)}  ={self.split.formula}"
        return (self.split.amount or Money(0)).format(parens_negative=True)


def _is_split(item) -> bool:
    return isinstance(item, ScheduleSplitRow)


def _kind_of(item) -> str:
    if _is_split(item):
        return ""
    if isinstance(item, AccountPaymentDefinition):
        return "Account payment"
    return "Estimate" if item.placeholder else "Commitment"


def _amount_of(item) -> str:
    if _is_split(item):
        return item.amount_text()
    return item.amount(when=_representative_date(item)).format()


class ScheduledView(BaseView):
    """The definitions: what recurs, and how."""

    WATCHES = ("database-changed", "scheduled-add", "scheduled-update", "scheduled-delete")

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self._suggest_dialog: Gtk.Window | None = None
        self._active_selection = None
        self._build()

    def set_db(self, db: DbSQLite | None) -> None:
        if self._suggest_dialog is not None and db is not self.db:
            self._suggest_dialog.close()
            self._suggest_dialog = None
        super().set_db(db)

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

        self.duplicate_button = Gtk.Button(label="Duplicate…")
        self.duplicate_button.set_sensitive(False)
        self.duplicate_button.connect("clicked", self._on_duplicate_clicked)
        bar.append(self.duplicate_button)

        self.delete_button = Gtk.Button(label="Delete…")
        self.delete_button.add_css_class("destructive-action")
        self.delete_button.set_sensitive(False)
        self.delete_button.connect("clicked", self._on_delete_clicked)
        bar.append(self.delete_button)

        loan_button = Gtk.Button(label="New loan…")
        loan_button.set_tooltip_text("Set up a loan with calculated interest")
        loan_button.connect("clicked", self._on_loan_clicked)
        bar.append(loan_button)

        self.definitions_view = self._definitions_view()
        self.estimates_view = self._definitions_view()
        bar.append(column_menu("scheduled-commitments", self.definitions_view, self._settings()))
        bar.append(column_menu("scheduled-estimates", self.estimates_view, self._settings()))
        self.append(bar)
        commitments_heading = Gtk.Label(label="Commitments and account payments", xalign=0)
        commitments_heading.add_css_class("total-row")
        commitments_heading.set_margin_start(12)
        self.append(commitments_heading)
        commitments_scroller = Gtk.ScrolledWindow(child=self.definitions_view)
        commitments_scroller.set_vexpand(True)
        self.append(commitments_scroller)

        estimates_heading = Gtk.Label(label="Estimates", xalign=0)
        estimates_heading.add_css_class("total-row")
        estimates_heading.set_margin_start(12)
        self.append(estimates_heading)
        estimates_scroller = Gtk.ScrolledWindow(child=self.estimates_view)
        estimates_scroller.set_vexpand(True)
        self.append(estimates_scroller)

        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("dim")
        for side in ("start", "end"):
            getattr(self.status, f"set_margin_{side}")(12)
        self.append(self.status)

    def _definitions_view(self):
        view = Gtk.ColumnView()
        view.set_show_row_separators(True)
        view.append_column(self._name_column())
        view.append_column(column("Kind", _kind_of, sort_key=_kind_of))
        view.append_column(
            column(
                "Frequency",
                lambda s: "" if _is_split(s) else s.recurrence.describe(),
                expand=True,
            )
        )
        view.append_column(column("Next", lambda s: "" if _is_split(s) else self._next_text(s)))
        view.append_column(column("Amount", _amount_of, numeric=True))
        view.append_column(
            column(
                "Planning purpose",
                lambda s: (
                    s.split.planning_flow.label
                    if _is_split(s) and s.split.planning_flow is not None
                    else ""
                ),
            )
        )
        view.append_column(
            column(
                "Automatic",
                lambda s: (
                    ""
                    if _is_split(s)
                    else (
                        "account"
                        if isinstance(s, AccountPaymentDefinition)
                        else ("yes" if s.auto_create else "no")
                    )
                ),
            )
        )
        view.append_column(
            column(
                "Enabled",
                lambda s: "" if _is_split(s) else ("yes" if s.enabled else "no"),
            )
        )
        return view

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
        schedules = list(self.db.iter_scheduled())
        account_payments = schedule.account_payment_definitions(self.db, schedules=schedules)
        commitments = [item for item in schedules if not item.placeholder]
        self._set_definitions_model(self.definitions_view, [*commitments, *account_payments], True)
        self._set_definitions_model(
            self.estimates_view,
            [item for item in schedules if item.placeholder],
            False,
        )
        # Gtk.SingleSelection auto-selects the first row before our notify handler
        # is connected.  Synchronize action sensitivity and expansion explicitly so
        # the visibly selected row is also the application's selected row.
        selection = self.definitions_view.get_model()
        self._on_selected(selection, None)

        estimates = sum(1 for s in schedules if s.placeholder)
        self.status.set_text(
            f"{len(schedules)} scheduled: {len(schedules) - estimates} commitment(s), "
            f"{estimates} estimate(s); {len(account_payments)} account-linked payment(s)"
        )

    def _set_definitions_model(self, view, items, autoselect: bool) -> None:
        definitions = Gio.ListStore.new(Row)
        for item in items:
            definitions.append(Row(item))
        tree = Gtk.TreeListModel.new(definitions, False, False, self._split_children)
        selection = Gtk.SingleSelection(model=sorted_model(view, tree))
        selection.set_autoselect(autoselect)
        selection.connect("notify::selected", self._on_selected)
        view.set_model(selection)

    def select_schedule(self, handle: str) -> None:
        """Select and expand one schedule, arriving from another view."""
        for view in (self.definitions_view, self.estimates_view):
            selection = view.get_model()
            if selection is None:
                continue
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
        if (
            not isinstance(payload, (ScheduledTransaction, AccountPaymentDefinition))
            or self.db is None
            or not payload.splits
        ):
            return None
        store = Gio.ListStore.new(Row)
        for split in payload.splits:
            store.append(Row(ScheduleSplitRow(split, payload, self.db)))
        return store

    def _next_text(self, sched) -> str:
        if isinstance(sched, AccountPaymentDefinition):
            return sched.next_due.isoformat()
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
        if selection is None:
            return
        self._active_selection = selection
        position = selection.get_selected()
        model = selection.get_model()
        for index in range(model.get_n_items()):
            tree_row = model.get_item(index)
            if tree_row is None or tree_row.get_depth() != 0:
                continue
            tree_row.set_expanded(index == position)
        selected = selection.get_selected_item()
        payload = unwrap(selected) if selected is not None else None
        selected_schedule = payload is not None and not _is_split(payload)
        self.edit_button.set_sensitive(selected_schedule)
        saved_schedule = isinstance(payload, ScheduledTransaction)
        self.delete_button.set_sensitive(saved_schedule)
        self.duplicate_button.set_sensitive(saved_schedule)

    def _selected_item(self) -> ScheduledTransaction | AccountPaymentDefinition | None:
        selection = self._active_selection
        selected = selection.get_selected_item() if selection is not None else None
        payload = unwrap(selected) if selected is not None else None
        if isinstance(payload, (ScheduledTransaction, AccountPaymentDefinition)):
            return payload
        return None

    def _selected_schedule(self) -> ScheduledTransaction | None:
        payload = self._selected_item()
        return payload if isinstance(payload, ScheduledTransaction) else None

    def _editability_reason(self, sched) -> str:
        if sched is None or _is_split(sched):
            return "No scheduled transaction is selected."
        if self.db is None:
            return "No book is open."
        return schedule.schedule_editability(self.db, sched).reason

    def _on_edit_clicked(self, _button) -> None:
        if self.db is None:
            return
        selected = self._selected_item()
        if selected is None:
            return
        if isinstance(selected, AccountPaymentDefinition):
            account = self.db.get_account(selected.account)
            if account is None:
                return
            from ..dialogs.account_dialog import AccountDialog

            dialog = AccountDialog(self.get_root(), self.db, account=account)
            dialog.connect("close-request", self.refresh_on_close)
            dialog.present()
            return
        from ..dialogs.schedule_dialog import ScheduleDialog

        reason = self._editability_reason(selected)
        dialog = ScheduleDialog(
            self.get_root(),
            self.db,
            source=selected,
            read_only_reason=reason or None,
        )
        dialog.connect("close-request", self.refresh_on_close)
        dialog.present()

    def _on_duplicate_clicked(self, _button) -> None:
        if self.db is None:
            return
        source = self._selected_schedule()
        if source is None:
            return
        from ..dialogs.schedule_dialog import ScheduleDialog

        reason = self._editability_reason(source)
        if reason:
            dialog = ScheduleDuplicateDialog(self.get_root(), self.db, source, reason, self.refresh)
            dialog.present()
            return
        draft = schedule.duplicate_definition(source)
        dialog = ScheduleDialog(self.get_root(), self.db, source=draft, creating=True)
        dialog.connect("close-request", self.refresh_on_close)
        dialog.present()

    def _on_delete_clicked(self, _button) -> None:
        if self.db is None:
            return
        selected = self._selected_schedule()
        if selected is None:
            return
        dialog = ScheduleDeleteDialog(self.get_root(), self.db, selected, self.refresh)
        dialog.present()

    def _on_new_clicked(self, _button) -> None:
        if self.db is None:
            return
        from ..dialogs.schedule_dialog import ScheduleDialog

        dialog = ScheduleDialog(self.get_root(), self.db)
        dialog.connect("close-request", self.refresh_on_close)
        dialog.present()

    def _on_suggest_clicked(self, _button) -> None:
        if self.db is None:
            return
        if self._suggest_dialog is not None:
            self._suggest_dialog.present()
            return
        from ..dialogs.historical_estimates_dialog import HistoricalEstimatesDialog

        dialog = HistoricalEstimatesDialog(self.get_root(), self.db)
        self._suggest_dialog = dialog

        def closed(*_args) -> bool:
            self._suggest_dialog = None
            self.refresh()
            return False

        dialog.connect("close-request", closed)
        dialog.present()

    def _on_loan_clicked(self, _button) -> None:
        if self.db is None:
            return
        from ..dialogs.loan_dialog import LoanDialog

        dialog = LoanDialog(self.get_root(), self.db)
        dialog.connect("close-request", self.refresh_on_close)
        dialog.present()


class ScheduleDuplicateDialog(Gtk.Window):
    """Name and confirm an exact copy of a protected schedule definition."""

    def __init__(self, parent, db, scheduled, reason: str, saved_callback) -> None:
        super().__init__(title="Duplicate scheduled transaction", transient_for=parent, modal=True)
        self.db = db
        self.scheduled = scheduled
        self.saved_callback = saved_callback

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.append(
            Gtk.Label(
                label=(
                    f"{reason}\n\nThe recurrence, formulas, splits, source details, "
                    "and other protected fields will be copied exactly. Completed "
                    "and skipped occurrence state will be cleared."
                ),
                xalign=0,
                wrap=True,
            )
        )
        self.name_entry = Gtk.Entry(text=f"{scheduled.name} copy")
        self.name_entry.connect("changed", self._validate)
        box.append(Gtk.Label(label="Copy name", xalign=0))
        box.append(self.name_entry)
        self.status = Gtk.Label(xalign=0)
        box.append(self.status)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        self.save_button = Gtk.Button(label="Create exact copy")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._confirm)
        buttons.append(self.save_button)
        box.append(buttons)
        self._validate()

    def _validate(self, *_args) -> None:
        valid = bool(self.name_entry.get_text().strip())
        self.save_button.set_sensitive(valid)
        self.status.set_text("" if valid else "Give the copied schedule a name.")

    def _confirm(self, _button) -> None:
        try:
            schedule.duplicate_saved_definition(
                self.db, self.scheduled.handle, name=self.name_entry.get_text()
            )
        except (KeyError, ValueError) as exc:
            self.status.set_text(str(exc))
            self.status.add_css_class("negative")
            return
        self.close()
        self.saved_callback()


class ScheduleDeleteDialog(Gtk.Window):
    """Confirm removal while explaining what remains and what may return."""

    def __init__(self, parent, db, scheduled, deleted_callback) -> None:
        super().__init__(title="Delete scheduled transaction", transient_for=parent, modal=True)
        self.db = db
        self.scheduled = scheduled
        self.deleted_callback = deleted_callback

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.append(
            Gtk.Label(
                label=(
                    f'Delete scheduled transaction "{scheduled.name}"? Already posted '
                    "transactions remain in the ledger. If this definition came from an "
                    "external book, a later re-import may restore it."
                ),
                xalign=0,
                wrap=True,
            )
        )
        self.status = Gtk.Label(xalign=0, wrap=True)
        box.append(self.status)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        delete = Gtk.Button(label="Delete scheduled transaction")
        delete.add_css_class("destructive-action")
        delete.connect("clicked", self._confirm)
        buttons.append(delete)
        box.append(buttons)

    def _confirm(self, _button) -> None:
        try:
            schedule.delete_definition(self.db, self.scheduled.handle)
        except (KeyError, ValueError) as exc:
            self.status.set_text(str(exc))
            self.status.add_css_class("negative")
            return
        self.close()
        self.deleted_callback()


class UpcomingView(BaseView):
    """The diary: what is due, what is overdue, and what needs posting."""

    WATCHES = (
        "database-changed",
        "scheduled-add",
        "scheduled-update",
        "scheduled-delete",
        "transaction-add",
    )

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
        self.upcoming_view.connect("activate", self._on_activated)
        self.upcoming_view.append_column(column("Due", self._due_text, sort_key=lambda o: o.when))
        self.upcoming_view.append_column(column("Schedule", lambda o: o.name, expand=True))
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
        occurrences = schedule.upcoming_occurrences(
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
                f"{len(overdue)} occurrence(s) overdue, oldest {overdue[0].when.isoformat()}"
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

    def _on_activated(self, _view, position: int) -> None:
        selection = self.upcoming_view.get_model()
        selected = selection.get_item(position) if selection is not None else None
        occurrence = unwrap(selected) if selected is not None else None
        if occurrence is not None:
            if isinstance(occurrence.schedule, AccountPaymentDefinition):
                db = self.db
                account = db.get_account(occurrence.schedule.account) if db else None
                if account is None:
                    return
                from ..dialogs.account_dialog import AccountDialog

                assert db is not None
                dialog = AccountDialog(self.get_root(), db, account=account)
                dialog.connect("close-request", self.refresh_on_close)
                dialog.present()
            else:
                self.manager.open_schedule(occurrence.schedule.handle)

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
        dialog.connect("close-request", self.refresh_on_close)
        dialog.present()
