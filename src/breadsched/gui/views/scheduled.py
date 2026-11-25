"""Scheduled transactions: their occurrences by date, and the definitions.

Two lists answer two questions, and each sorts usefully on its own. **Upcoming**
is the diary: every occurrence due within the look-ahead chosen, oldest first,
flagging the overdue. The definitions below it are the standing arrangement: what
recurs, how often, and whether it is a commitment or an estimate (the estimates in
a table of their own). The browser's Scheduled page shows the same three lists.

Selecting a definition expands its splits as rows beneath it, the same way the
register expands a transaction, so a loan payment shows its interest and principal
legs where they belong.

The view's commands (Edit, Delete, Duplicate, Enter Next, Skip Next, Match, and the
rest) are toolbar icons, Actions menu items, and, for a row, its right-click menu
(``view_catalog``): one row is selected across the three tables, and a command acts
on its schedule; Match acts on the selected occurrence, or on a selected schedule's
current one.
"""

from __future__ import annotations

from datetime import date, timedelta

from ...api import ApiError
from ...api.local import book_server
from ...gen.engine import schedule
from ...gen.engine.schedule import AccountPaymentDefinition
from ...gen.lib import ScheduledTransaction
from ...gen.lib.money import Money
from ...gen.services import (
    DueDecision,
    DuplicateSchedule,
    ResolveNext,
    ServiceError,
)
from ...gen.services.schedule_editing import ScheduledPage
from ...presentation import service_error_message
from ..dialogs.share_prices import price_shares
from ..gi_setup import Gio, Gtk, Pango
from ..remote_book import OpenBook
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from ..widgets.date_entry import shown_date
from ._base import (
    BaseView,
    Row,
    _compare_by,
    attach_row_menu,
    column,
    mark_row,
    scroll_to_row,
    sorted_model,
    table_section,
    unwrap,
)

__all__ = ["ScheduledView", "ScheduleSplitRow"]


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

    __slots__ = ("split", "schedule", "names")

    def __init__(self, split, schedule_object, names: dict[str, str]) -> None:
        self.split = split
        self.schedule = schedule_object
        #: Every account's full name, as the book server listed them.
        self.names = names

    @property
    def account_name(self) -> str:
        return self.names.get(self.split.account, "(unknown account)")

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


def _name_key(item) -> str:
    """What the Name column sorts by: a split's account, else the schedule's name."""
    return (item.account_name if _is_split(item) else item.name).casefold()


def _due_text(occurrence) -> str:
    """An occurrence's date, marked overdue or today."""
    today = date.today()
    if occurrence.when < today:
        return f"{shown_date(occurrence.when)}  (overdue)"
    if occurrence.when == today:
        return f"{shown_date(occurrence.when)}  (today)"
    return shown_date(occurrence.when)


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
    """Occurrences by date, and the definitions: what recurs, and how."""

    KEY = "scheduled"
    WATCHES = (
        "database-changed",
        "scheduled-add",
        "scheduled-update",
        "scheduled-delete",
        "transaction-add",
    )

    #: The look-ahead choices for the occurrences list, in days.
    HORIZONS = (("Due now", 0), ("7 days", 7), ("30 days", 30), ("90 days", 90), ("1 year", 365))

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self._suggest_dialog: Gtk.Window | None = None
        self._active_selection = None
        self._horizon = 30
        #: The definitions as last read through the book server (#358).
        self._page: ScheduledPage | None = None
        self._build()

    def set_db(self, db: OpenBook | None) -> None:
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
        # The commands are toolbar icons and Actions menu items (view_catalog);
        # this names what Enter Next and Skip Next would act on.
        self.next_note = Gtk.Label(xalign=1, wrap=True)
        self.next_note.add_css_class("dim")
        bar.append(self.next_note)
        self.append_toolbar(bar)

        # Every occurrence by date (what the Upcoming view showed): the look-ahead
        # changes what the list shows, so it sits beside the list's heading.
        self.upcoming_view = Gtk.ColumnView()
        self.upcoming_view.add_css_class("data-table")
        self.upcoming_view.set_show_row_separators(True)
        self.upcoming_view.append_column(column("Due", _due_text, sort_key=lambda o: o.when))
        self.upcoming_view.append_column(column("Schedule", lambda o: o.name, expand=True))
        self.upcoming_view.append_column(column("Kind", lambda o: _kind_of(o.schedule)))
        self.upcoming_view.append_column(
            column("Frequency", lambda o: o.schedule.recurrence.describe(), expand=True)
        )
        self.upcoming_view.append_column(
            column("Amount", lambda o: o.amount.format(), numeric=True)
        )
        upcoming = table_section(self.upcoming_view, "upcoming", self._settings(), title="Upcoming")
        header = upcoming.get_first_child()
        look_ahead = Gtk.Label(label="Look ahead")
        header.insert_child_after(look_ahead, upcoming.heading)
        self.horizon_picker = bounded_dropdown([label for label, _days in self.HORIZONS])
        self.horizon_picker.set_selected(2)
        self.horizon_picker.connect("notify::selected", self._on_horizon_changed)
        header.insert_child_after(self.horizon_picker, look_ahead)
        self.upcoming_note = Gtk.Label(xalign=0, wrap=True)
        self.upcoming_note.add_css_class("dim")
        upcoming.insert_child_after(self.upcoming_note, header)

        self.definitions_view = self._definitions_view()
        self.estimates_view = self._definitions_view()
        # Each table carries its own column chooser in its heading (#153).
        for view, view_id, title_text, section in (
            (self.upcoming_view, "upcoming", "Upcoming", upcoming),
            (
                self.definitions_view,
                "scheduled-commitments",
                "Commitments and account payments",
                None,
            ),
            (self.estimates_view, "scheduled-estimates", "Estimates", None),
        ):
            if section is None:
                section = table_section(view, view_id, self._settings(), title=title_text)
            for side in ("start", "end"):
                getattr(section, f"set_margin_{side}")(12)
            section.scroller.set_vexpand(True)
            section.set_vexpand(True)
            self.append(section)
            # Activating a row edits its schedule; right-clicking lists its commands.
            view.connect("activate", lambda *_: self._on_edit_clicked(None))
            attach_row_menu(self, view)

        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("dim")
        for side in ("start", "end"):
            getattr(self.status, f"set_margin_{side}")(12)
        self.append(self.status)

    def _definitions_view(self):
        view = Gtk.ColumnView()
        view.add_css_class("data-table")
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
        view.append_column(
            column(
                "Next",
                lambda s: "" if _is_split(s) else self._next_text(s),
                # By date, whatever order Preferences shows it in; finished ones last.
                sort_key=lambda s: "" if _is_split(s) else self._next_key(s),
            )
        )
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
            mark_row(item)
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
        # Like every other column, its heading sorts (#427): schedules by name, and
        # a schedule's splits, beneath it, by account.
        col.set_sorter(Gtk.CustomSorter.new(_compare_by(_name_key, None, False)))
        return col

    # ------------------------------------------------------------------- model

    def refresh(self) -> None:
        if self.db is None:
            return
        # Drawing the tables again would leave the first row selected; the row that
        # was selected (a schedule just edited, entered, or skipped, say) stays so.
        kept = self._kept()
        today = date.today()
        self._page = book_server(self.db).scheduled_page(self._horizon, today)
        schedules = list(self._page.schedules)
        account_payments = list(self._page.payments)
        commitments = [item for item in schedules if not item.placeholder]
        self._active_selection = None
        occurrences = Gio.ListStore.new(Row)
        for occurrence in self._page.upcoming:
            occurrences.append(Row(occurrence))
        selection = Gtk.SingleSelection(model=sorted_model(self.upcoming_view, occurrences))
        # Like the definitions' tables, it starts with no row selected.
        selection.set_autoselect(False)
        selection.set_can_unselect(True)
        selection.set_selected(Gtk.INVALID_LIST_POSITION)
        selection.connect("notify::selected", self._on_selected)
        self.upcoming_view.set_model(selection)
        self._set_definitions_model(self.definitions_view, [*commitments, *account_payments], True)
        self._set_definitions_model(
            self.estimates_view,
            [item for item in schedules if item.placeholder],
            False,
        )
        # The first commitment starts selected; synchronize the commands and the
        # expansion explicitly so the visibly selected row is also the one the
        # commands act on. One row is selected across the tables.
        self._on_selected(self.definitions_view.get_model(), None)
        if kept is not None:
            self._restore(kept)
        self._show_upcoming_note(today)

        estimates = sum(1 for s in schedules if s.placeholder)
        self.status.set_text(
            f"{len(schedules)} scheduled: {len(schedules) - estimates} commitment(s), "
            f"{estimates} estimate(s); {len(account_payments)} account-linked payment(s)"
        )

    def _show_upcoming_note(self, today: date) -> None:
        """Count the overdue, and what is to be entered or reminded in advance."""
        if self._page is None or self.db is None:
            return
        occurrences = list(self._page.upcoming)
        overdue = [o for o in occurrences if o.when <= today]
        if overdue:
            text = f"{len(overdue)} occurrence(s) overdue, oldest {shown_date(overdue[0].when)}"
            self.upcoming_note.add_css_class("negative")
        else:
            text = (
                f"Nothing overdue; {len(occurrences)} occurrence(s) in the next "
                f"{self._horizon} days"
            )
            self.upcoming_note.remove_css_class("negative")
        # Enter- and remind-in-advance occurrences are reviewed too (#352).
        early = [
            item
            for review in book_server(self.db).due_review(today)
            for item in review.items
            if item.when > today
        ]
        if early:
            text = f"{text}; {len(early)} to enter or remind in advance"
        self.upcoming_note.set_text(f"{text}. Review Due Transactions… posts what is due.")

    def _on_horizon_changed(self, picker, _param) -> None:
        self._horizon = self.HORIZONS[picker.get_selected()][1]
        self.refresh()

    def _set_definitions_model(self, view, items, first: bool) -> None:
        definitions = Gio.ListStore.new(Row)
        for item in items:
            definitions.append(Row(item))
        tree = Gtk.TreeListModel.new(definitions, False, False, self._split_children)
        selection = Gtk.SingleSelection(model=sorted_model(view, tree))
        # A selection made with a model selects its first row; only the first table
        # keeps it (``first``), and neither selects a row by itself again, so a row
        # chosen in one table can clear the other's. Newer GTK (4.20, say) ignores
        # clearing a selection that cannot be unselected, so both tables can be.
        selection.set_autoselect(False)
        selection.set_can_unselect(True)
        if not first:
            selection.set_selected(Gtk.INVALID_LIST_POSITION)
        selection.connect("notify::selected", self._on_selected)
        view.set_model(selection)

    def _kept(self) -> tuple[str, str, date | None] | None:
        """What is selected now, to select again after drawing the tables anew: an
        occurrence (its schedule and date), or a schedule or card payment (a
        selected split counting as its schedule)."""
        selection = self._active_selection
        selected = selection.get_selected_item() if selection is not None else None
        payload = unwrap(selected) if selected is not None else None
        if selection is self.upcoming_view.get_model() and payload is not None:
            return ("occurrence", getattr(payload.schedule, "handle", ""), payload.when)
        if isinstance(payload, ScheduleSplitRow):
            payload = payload.schedule
        handle = getattr(payload, "handle", None)
        return ("schedule", handle, None) if handle else None

    def _restore(self, kept: tuple[str, str, date | None]) -> None:
        kind, handle, when = kept
        if kind == "schedule":
            self.select_schedule(handle)
            return
        selection = self.upcoming_view.get_model()
        for index in range(selection.get_n_items()):
            occurrence = unwrap(selection.get_item(index))
            if getattr(occurrence.schedule, "handle", None) == handle and occurrence.when == when:
                selection.set_selected(index)
                self._on_selected(selection, None)
                scroll_to_row(self.upcoming_view, index)
                return

    def select_schedule(self, handle: str) -> None:
        """Select, expand, and scroll to one schedule: arriving from another view, or
        keeping the one selected when the tables are drawn again."""
        for view in (self.definitions_view, self.estimates_view):
            selection = view.get_model()
            if selection is None:
                continue
            for index in range(selection.get_n_items()):
                row = selection.get_item(index)
                payload = unwrap(row)
                if getattr(payload, "handle", None) == handle and not _is_split(payload):
                    selection.set_selected(index)
                    self._on_selected(selection, None)
                    if hasattr(row, "set_expanded"):
                        row.set_expanded(True)
                    # In view, rather than left wherever the redrawn list starts.
                    scroll_to_row(view, index)
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
            store.append(Row(ScheduleSplitRow(split, payload, self._names())))
        return store

    def _names(self) -> dict[str, str]:
        if self._page is None and self.db is not None:
            self._page = book_server(self.db).scheduled_page()
        return dict(self._page.names) if self._page is not None else {}

    def _next_date(self, sched) -> date | None:
        if isinstance(sched, AccountPaymentDefinition):
            return sched.next_due
        return sched.recurrence.next_after(date.today() - timedelta(days=1))

    def _next_text(self, sched) -> str:
        following = self._next_date(sched)
        return shown_date(following) if following else "finished"

    def _next_key(self, sched) -> str:
        following = self._next_date(sched)
        return following.isoformat() if following else "~finished"

    def _splits_summary(self, sched) -> str:
        """Every leg as text. Kept for the web and CLI views, and for tests."""
        if self.db is None or not sched.splits:
            return "(no splits)"
        parts = []
        for split in sched.splits:
            row = ScheduleSplitRow(split, sched, self._names())
            parts.append(f"{row.account_name} {row.amount_text()}")
        return "  |  ".join(parts)

    # ----------------------------------------------------------------- actions

    def _tables(self) -> tuple[Gtk.ColumnView, ...]:
        return (self.upcoming_view, self.definitions_view, self.estimates_view)

    def _on_selected(self, selection, _param) -> None:
        if selection is None:
            return
        position = selection.get_selected()
        if position == Gtk.INVALID_LIST_POSITION and selection is not self._active_selection:
            return  # another table letting go of its row
        self._active_selection = selection
        if position != Gtk.INVALID_LIST_POSITION:
            for view in self._tables():
                other = view.get_model()
                if other is not None and other is not selection:
                    other.set_selected(Gtk.INVALID_LIST_POSITION)
        if selection is not self.upcoming_view.get_model():
            model = selection.get_model()
            for index in range(model.get_n_items()):
                tree_row = model.get_item(index)
                if tree_row is None or tree_row.get_depth() != 0:
                    continue
                tree_row.set_expanded(index == position)
        self._update_commands()

    def _selected_occurrence(self):
        """The occurrence selected in the Upcoming list, if one is."""
        selection = self._active_selection
        if selection is None or selection is not self.upcoming_view.get_model():
            return None
        selected = selection.get_selected_item()
        return unwrap(selected) if selected is not None else None

    def match_selected(self, today=None):
        """Open the Match window on the selected occurrence, or on the selected
        schedule's current one."""
        from ..dialogs.match_dialog import OccurrenceMatchWindow

        sched = self._selected_schedule()
        if sched is None or self.db is None:
            return None
        occurrence = self._selected_occurrence()
        when = (
            occurrence.when
            if occurrence is not None
            else sched.current_occurrence(today or date.today())
        )
        if when is None:
            return None
        root = self.get_root()
        window = OccurrenceMatchWindow(
            root if isinstance(root, Gtk.Window) else None, self.db, sched.occurrence_key(when)
        )
        window.present()
        return window

    def _update_commands(self) -> None:
        """Grey out the commands the selected row cannot use, and name the
        occurrence Enter Next and Skip Next would act on."""
        selection = self._active_selection
        selected = selection.get_selected_item() if selection is not None else None
        payload = unwrap(selected) if selected is not None else None
        item = self._selected_item()
        schedule = item if isinstance(item, ScheduledTransaction) else None
        upcoming = (
            book_server(self.db).next_occurrence(schedule.handle)
            if schedule is not None and self.db is not None
            else None
        )
        self.enable_commands(
            edit=item is not None and not _is_split(payload),
            delete=schedule is not None,
            duplicate=schedule is not None,
            match=schedule is not None,
            enter_next=upcoming is not None,
            skip_next=upcoming is not None,
        )
        if schedule is not None and upcoming is not None:
            self.next_note.set_text(
                f"Next of {schedule.name}: {shown_date(upcoming.when)} ({upcoming.amount.format()})"
            )
        elif schedule is not None:
            self.next_note.set_text(f"{schedule.name} has no occurrence to enter or skip")
        else:
            self.next_note.set_text("")

    def resolve_next(self, decision: DueDecision) -> bool:
        """Enter or skip the selected schedule's next occurrence; False if refused.

        Entering first prices any shares the occurrence buys or sells (#417); while
        their prices are asked for, it returns True and enters once they are given.
        """
        sched = self._selected_schedule()
        if sched is None or self.db is None:
            return False
        # Through the book server, as the command line and the browser do (#359).
        book = book_server(self.db)
        upcoming = book.next_occurrence(sched.handle)
        if upcoming is None:
            self.status.set_text(service_error_message(ServiceError("schedule.next.none")))
            return False
        if decision is DueDecision.SKIP:
            return self._resolve_next(sched, upcoming, decision, ())
        outcome: list[bool] = []
        self.share_pricing = price_shares(
            self.get_root(),
            self.db,
            [(sched.handle, upcoming.when)],
            lambda prices: outcome.append(self._resolve_next(sched, upcoming, decision, prices)),
        )
        return outcome[0] if outcome else True

    def _resolve_next(self, sched, upcoming, decision: DueDecision, prices) -> bool:
        if self.db is None:
            return False
        book = book_server(self.db)
        try:
            outcome = book.resolve_next(
                ResolveNext(sched.handle, decision, when=upcoming.when, prices=prices)
            )
        except ApiError as error:
            self.status.set_text(str(error))
            return False
        self.refresh()
        when = shown_date(upcoming.when)
        if decision is DueDecision.SKIP:
            self.status.set_text(f"Skipped {sched.name} of {when}. Ctrl+Z restores it.")
        else:
            self.status.set_text(f"Entered {sched.name} of {when}. Ctrl+Z removes it.")
            from ..dialogs.posted_dialog import PostedTransactionsDialog

            dialog = PostedTransactionsDialog(self.get_root(), self.db, outcome.transactions)
            dialog.connect("close-request", self.refresh_on_close)
            dialog.present()
        return True

    def _selected_item(self) -> ScheduledTransaction | AccountPaymentDefinition | None:
        """The selected schedule or card payment, or the selected occurrence's."""
        selection = self._active_selection
        selected = selection.get_selected_item() if selection is not None else None
        payload = unwrap(selected) if selected is not None else None
        if selection is not None and selection is self.upcoming_view.get_model():
            payload = getattr(payload, "schedule", None)
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
        if self._page is None or sched.handle not in self._page.editability:
            self._page = book_server(self.db).scheduled_page()
        found = self._page.editability.get(sched.handle)
        return found.reason if found is not None else ""

    def _on_edit_clicked(self, _button) -> None:
        if self.db is None:
            return
        selected = self._selected_item()
        if selected is None:
            return
        if isinstance(selected, AccountPaymentDefinition):
            try:
                account = book_server(self.db).account(selected.account).account
            except ApiError:
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

    def _on_enter_next_clicked(self, _button=None) -> bool:
        return self.resolve_next(DueDecision.POST)

    def _on_skip_next_clicked(self, _button=None) -> bool:
        return self.resolve_next(DueDecision.SKIP)

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

    def _on_payroll_clicked(self, _button=None):
        """Open Payroll on the selected schedule when it is a paycheck."""
        if self.db is None:
            return None
        from ..dialogs.payroll_dialog import PayrollDialog

        selected = self._selected_schedule()
        dialog = PayrollDialog(
            self.get_root(), self.db, selected.handle if selected is not None else None
        )
        dialog.connect("close-request", self.refresh_on_close)
        dialog.present()
        return dialog


class ScheduleDuplicateDialog(BoundedWindow):
    """Name and confirm an exact copy of a protected schedule definition."""

    def __init__(self, parent, db, scheduled, reason: str, saved_callback) -> None:
        super().__init__(title="Duplicate scheduled transaction", transient_for=parent, modal=True)
        self.db = db
        box = self.frame("scheduled")
        self.scheduled = scheduled
        self.saved_callback = saved_callback

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
        self.save_button = Gtk.Button(label="Create exact copy")
        self.save_button.connect("clicked", self._confirm)
        self.actions(box, dismiss="Cancel", primary=self.save_button)
        self._validate()

    def _validate(self, *_args) -> None:
        valid = bool(self.name_entry.get_text().strip())
        self.save_button.set_sensitive(valid)
        self.status.set_text("" if valid else "Give the copied schedule a name.")

    def _confirm(self, _button) -> None:
        request = DuplicateSchedule(self.scheduled.handle, name=self.name_entry.get_text())
        try:
            book_server(self.db).duplicate_schedule(request)
        except ApiError as error:
            self.status.set_text(str(error))
            self.status.add_css_class("negative")
            return
        self.close()
        self.saved_callback()


class ScheduleDeleteDialog(BoundedWindow):
    """Confirm removal while explaining what remains and what may return."""

    def __init__(self, parent, db, scheduled, deleted_callback) -> None:
        super().__init__(title="Delete scheduled transaction", transient_for=parent, modal=True)
        self.db = db
        box = self.frame("scheduled")
        self.scheduled = scheduled
        self.deleted_callback = deleted_callback

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
        delete = Gtk.Button(label="Delete scheduled transaction")
        delete.add_css_class("destructive-action")
        delete.connect("clicked", self._confirm)
        self.actions(box, dismiss="Cancel", primary=delete)

    def _confirm(self, _button) -> None:
        try:
            book_server(self.db).delete_schedule(self.scheduled.handle)
        except ApiError as error:
            self.status.set_text(str(error))
            self.status.add_css_class("negative")
            return
        self.close()
        self.deleted_callback()
