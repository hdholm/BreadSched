"""Reconcile one account against a dated statement balance.

The window follows GnuCash's reconcile window (#362): before a statement starts
it shows the account's reconciled balance on the statement date, to compare with
the statement's previous balance, and adds a missing charge such as interest in
one step. A started statement lists Funds In and Funds Out side by side, each with
its checked total, under a running reconciled balance and difference. Several
rows can be selected and checked or unchecked together, and an entry dated after
the statement is listed below a heading, shaded, in case its date is wrong.

The window is not modal, nor kept above the main window, and there is one per
account: while it is open the rest of the application can still be used and brought
in front of it, for example to enter a missed scheduled
transaction, and the lists follow every change to the book. Each check is saved
with the open statement at once, so **Postpone** simply closes the window and the
statement resumes when it is reopened; **Abandon** cancels the statement, which
never changed a ledger split.
"""

from __future__ import annotations

import weakref
from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.engine import reconciliation
from ...gen.lib import Account, Money, ReconciliationStatus
from ...gen.services import (
    AddStatementEntry,
    ReconciliationAction,
    StartReconciliation,
    UpdateReconciliation,
    add_statement_entry,
    cancel_reconciliation,
    complete_reconciliation,
    reopen_reconciliation,
    start_reconciliation,
    update_reconciliation,
)
from ...gen.services.claims import claim_link_proposals
from ...gen.services.receivables import reimbursement_proposals
from ...gen.utils.amount_input import parse_user_amount
from ...presentation import claim_link_notice, reimbursement_notice, service_error_message
from .. import hidden_accounts
from ..gi_setup import Gdk, GLib, Gtk, Pango
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from ..widgets.date_entry import attach_date_keys, parse_date_text
from ..widgets.help import help_row

__all__ = ["ReconciliationDialog"]

#: The open window for each book and account, so Reconcile brings it forward.
_OPEN: weakref.WeakValueDictionary[tuple[int, str], ReconciliationDialog] = (
    weakref.WeakValueDictionary()
)

ABANDON = 1


def _candidate_key(candidate: reconciliation.ReconciliationCandidate) -> tuple:
    """What a row shows, apart from its check: unchanged keys keep the lists."""
    return (
        candidate.split,
        candidate.post_date,
        candidate.num,
        candidate.description,
        candidate.amount,
        candidate.after_statement,
        candidate.funds_in,
    )


#: The sort buttons above each list, and what each orders by (#377).
_SORT_LABELS = {
    "checked": "✓",
    "date": "Date",
    "num": "Num",
    "description": "Description",
    "amount": "Amount",
}


class _Column:
    """One side of the statement: its heading, sort buttons, rows, and checked total."""

    def __init__(self, title: str, on_sort) -> None:
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        heading = Gtk.Label(label=title, xalign=0)
        heading.add_css_class("heading")
        self.box.append(heading)
        #: The list's order; choosing the same button again reverses it.
        self.sort = "date"
        self.descending = False
        self._on_sort = on_sort
        self.sort_buttons: dict[str, Gtk.Button] = {}
        sorts = Gtk.Box(spacing=2)
        sorts.append(Gtk.Label(label="Sort:", xalign=0))
        for key in reconciliation.CANDIDATE_SORTS:
            button = Gtk.Button(label=_SORT_LABELS[key])
            button.set_has_frame(False)
            button.set_tooltip_text(
                "Checked first"
                if key == "checked"
                else "Order by number"
                if key == "num"
                else f"Order by {_SORT_LABELS[key].lower()}"
            )
            button.connect("clicked", lambda _b, k=key: self.sort_by(k))
            self.sort_buttons[key] = button
            sorts.append(button)
        self.box.append(sorts)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.MULTIPLE)
        self.list.set_activate_on_single_click(False)
        self.list.add_css_class("boxed-list")
        scroller = Gtk.ScrolledWindow(child=self.list, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(160)
        self.box.append(scroller)
        self.total = Gtk.Label(xalign=1)
        self.total.add_css_class("numeric")
        self.box.append(self.total)
        #: Each row's split handle; the heading of later entries has none.
        self.splits: dict[Gtk.ListBoxRow, str] = {}

    def selected_splits(self) -> list[str]:
        return [self.splits[row] for row in self.list.get_selected_rows() if row in self.splits]

    def sort_by(self, key: str) -> None:
        """Order the list by ``key``; the current key again reverses the order."""
        self.descending = not self.descending if key == self.sort else False
        self.sort = key
        self._show_sort()
        self._on_sort()

    def _show_sort(self) -> None:
        for key, button in self.sort_buttons.items():
            arrow = (" ▼" if self.descending else " ▲") if key == self.sort else ""
            button.set_label(_SORT_LABELS[key] + arrow)


class ReconciliationDialog(BoundedWindow):
    """Thin GTK presentation over the shared reconciliation service."""

    @classmethod
    def present_for(
        cls, parent: Gtk.Window | None, db: DbSQLite, account: Account
    ) -> ReconciliationDialog:
        """Show the account's reconcile window, opening one only if none is open."""
        existing = _OPEN.get((id(db), account.handle))
        if existing is not None and existing.db is db:
            existing.present()
            return existing
        dialog = cls(parent, db, account)
        dialog.present()
        return dialog

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite, account: Account) -> None:
        # Not transient for the main window: a transient window is kept above its
        # parent, and the main window must be able to come in front of this one
        # while a statement is worked through. It still belongs to the application
        # and closes with the window that opened it.
        super().__init__(title=f"Reconcile {db.full_name(account)}", modal=False)
        self.opener = parent
        if parent is not None:
            application = parent.get_application()
            if application is not None:
                self.set_application(application)
            self._opener_handler: int | None = parent.connect(
                "close-request", lambda *_: self.close() or False
            )
        else:
            self._opener_handler = None
        self.db = db
        self.account = account
        self.set_default_size(900, 640)
        _OPEN[(id(db), account.handle)] = self

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(16)
        outer.append(help_row("reconcile"))
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, vexpand=True)
        outer.append(self.body)
        self.footer = Gtk.Box(spacing=8)
        outer.append(self.footer)
        self.set_child(outer)

        #: The open statement shown, or None on the start page.
        self.session: str | None = None
        self._keys: tuple = ()
        self._checks: dict[str, Gtk.CheckButton] = {}
        self._updating = False
        #: True while a click or a rebuild sets the selection in both lists.
        self._extending = False
        #: The list and row a Shift-click extends from, and the row clicked last.
        self._anchor: tuple[_Column, Gtk.ListBoxRow] | None = None
        self._last_split: str | None = None
        self._refresh_source = 0
        self._handler: int | None = db.connect("database-changed", self._on_book_changed)
        self.connect("destroy", self._on_destroy)
        self.connect("close-request", self._on_close_request)
        self._render()

    # ----------------------------------------------------------------- life

    def _on_close_request(self, *_args) -> bool:
        self._disconnect()
        return False

    def _on_destroy(self, *_args) -> None:
        self._disconnect()

    def _disconnect(self) -> None:
        if self._refresh_source:
            GLib.source_remove(self._refresh_source)
            self._refresh_source = 0
        if self._handler is not None:
            self.db.disconnect(self._handler)
            self._handler = None
        if self._opener_handler is not None and self.opener is not None:
            self.opener.disconnect(self._opener_handler)
            self._opener_handler = None
        if _OPEN.get((id(self.db), self.account.handle)) is self:
            del _OPEN[(id(self.db), self.account.handle)]

    def _on_book_changed(self, *_args) -> None:
        # Coalesce a burst of changes, and never redraw inside a signal handler.
        if not self._refresh_source:
            self._refresh_source = GLib.idle_add(self._idle_refresh)

    def _idle_refresh(self) -> bool:
        self._refresh_source = 0
        self.refresh()
        return False

    def refresh(self) -> None:
        """Follow the book: the statement's lists and totals, or its start page."""
        if not self.db.is_open:
            return
        if self.db.get_account(self.account.handle) is None:
            self.close()
            return
        current = reconciliation.open_for_account(self.db, self.account.handle)
        handle = current.handle if current is not None else None
        if handle != self.session or handle is None:
            if handle is None and self.session is None:
                self._show_starting_balance()
                return
            self._render()
            return
        state = reconciliation.summary(self.db, handle)
        if tuple(_candidate_key(item) for item in state.candidates) != self._keys:
            self._fill(state)
        else:
            self._set_checks(state)
        self._show_totals(state)

    # --------------------------------------------------------------- layout

    def _clear(self, box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            box.remove(child)
            child = box.get_first_child()

    def _render(self) -> None:
        self._clear(self.body)
        self._clear(self.footer)
        self.set_primary(None)
        proposals = reimbursement_proposals(self.db, account=self.account.handle).value or ()
        notice = reimbursement_notice(len(proposals))
        #: Points at waiting reimbursement proposals: a deposit on this statement
        #: may be money back on a reimbursable expense.
        self.reimbursement_note: Gtk.Label | None = None
        if notice:
            self.reimbursement_note = Gtk.Label(label=notice, xalign=0, wrap=True)
            self.reimbursement_note.add_css_class("dim")
            self.body.append(self.reimbursement_note)
        # An FSA statement line may belong on a claim.
        claims = claim_link_proposals(self.db, account=self.account.handle).value or ()
        self.claim_link_note: Gtk.Label | None = None
        if claim_notice := claim_link_notice(len(claims)):
            self.claim_link_note = Gtk.Label(label=claim_notice, xalign=0, wrap=True)
            self.claim_link_note.add_css_class("dim")
            self.body.append(self.claim_link_note)
        current = reconciliation.open_for_account(self.db, self.account.handle)
        self.session = current.handle if current is not None else None
        if current is None:
            self._render_start()
        else:
            self._render_session(current.handle)

    def _render_start(self) -> None:
        explanation = Gtk.Label(
            label=(
                "Enter the statement date and ending balance. Compare the starting "
                "balance with the statement's previous balance before you start: a "
                "difference there was made before this statement. Already-cleared "
                "entries start checked; no ledger state changes until the difference "
                "is zero and the reconciliation is finished."
            ),
            xalign=0,
            wrap=True,
        )
        explanation.add_css_class("dim")
        self.body.append(explanation)
        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        self.statement_date = attach_date_keys(Gtk.Entry(text=date.today().isoformat()))
        self.statement_date.connect("changed", lambda *_: self._show_starting_balance())
        self.starting_balance = Gtk.Label(xalign=1)
        self.starting_balance.add_css_class("numeric")
        self.ending_balance = Gtk.Entry(placeholder_text="0.00", xalign=1)
        grid.attach(Gtk.Label(label="Statement date", xalign=0), 0, 0, 1, 1)
        grid.attach(self.statement_date, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Starting balance", xalign=0), 0, 1, 1, 1)
        grid.attach(self.starting_balance, 1, 1, 1, 1)
        grid.attach(Gtk.Label(label="Ending balance", xalign=0), 0, 2, 1, 1)
        grid.attach(self.ending_balance, 1, 2, 1, 1)
        self.body.append(grid)
        self._show_starting_balance()

        self.body.append(self._statement_entry_form())
        self.status = Gtk.Label(xalign=0, wrap=True)
        self.body.append(self.status)

        completed = [
            item
            for item in self.db.iter_reconciliations(self.account.handle)
            if item.status is ReconciliationStatus.COMPLETED
        ]
        if completed:
            latest = max(completed, key=lambda item: item.statement_date)
            reopen = Gtk.Button(label=f"Reopen statement through {latest.statement_date}")
            reopen.connect("clicked", lambda *_: self._on_reopen(latest.handle))
            self.footer.append(reopen)
        self.footer.append(Gtk.Box(hexpand=True))
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        self.footer.append(close)
        self.start_button = Gtk.Button(label="Start reconciliation")
        self.start_button.add_css_class("suggested-action")
        self.start_button.connect("clicked", self._on_start)
        self.footer.append(self.start_button)

    def _statement_entry_form(self) -> Gtk.Widget:
        """A quick add for a statement line not yet in the book, such as interest."""
        frame = Gtk.Frame(label="Add a charge or credit from the statement")
        box = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(8)
        self.entry_description = Gtk.Entry(text="Interest", hexpand=True)
        self.entry_description.set_width_chars(12)
        self.entry_accounts = sorted(
            (
                account
                for account in self.db.iter_accounts()
                if not account.is_root
                and not account.placeholder
                and (not account.hidden or hidden_accounts.shown())
                and account.handle != self.account.handle
            ),
            key=self.db.full_name,
        )
        names = [self.db.full_name(account) for account in self.entry_accounts]
        self.entry_account = bounded_dropdown(names or ["(none)"])
        self.entry_account.set_hexpand(True)
        interest = next(
            (index for index, name in enumerate(names) if "interest" in name.lower()), None
        )
        if interest is not None:
            self.entry_account.set_selected(interest)
        self.entry_amount = Gtk.Entry(placeholder_text="0.00", xalign=1)
        self.entry_amount.set_width_chars(10)
        self.entry_amount.set_tooltip_text(
            "Positive raises the balance, as interest charged on a card or earned on savings"
        )
        add = Gtk.Button(label="Add")
        add.connect("clicked", lambda *_: self.add_statement_entry())
        for widget in (self.entry_description, self.entry_account, self.entry_amount, add):
            box.append(widget)
        frame.set_child(box)
        return frame

    def _render_session(self, handle: str) -> None:
        state = reconciliation.summary(self.db, handle)
        session = state.reconciliation
        heading = Gtk.Label(label=f"Statement through {session.statement_date}", xalign=0)
        heading.add_css_class("title-3")
        self.body.append(heading)

        target_row = Gtk.Box(spacing=8)
        target_row.append(Gtk.Label(label="Ending balance", xalign=0))
        self.target_entry = Gtk.Entry(text=f"{session.ending_balance.to_decimal():.2f}", xalign=1)
        target_row.append(self.target_entry)
        update = Gtk.Button(label="Update")
        update.connect("clicked", lambda *_: self._on_update_target(handle))
        target_row.append(update)
        self.body.append(target_row)

        columns = Gtk.Box(spacing=12, homogeneous=True, vexpand=True)
        self.funds_in = _Column("Funds In", lambda: self._resort(handle))
        self.funds_out = _Column("Funds Out", lambda: self._resort(handle))
        for column in (self.funds_in, self.funds_out):
            column._show_sort()
            column.list.connect("row-activated", self._on_row_activated)
            column.list.connect("selected-rows-changed", self._on_selection_changed)
            # Clicks are the dialog's own (#374, #375): one click checks a row and
            # selects only it, Ctrl or Shift selects without checking, and a
            # double-click edits. A click on the check itself is the check's.
            clicks = Gtk.GestureClick(button=1)
            clicks.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            clicks.connect("pressed", self._on_list_pressed, column)
            column.list.add_controller(clicks)
            columns.append(column.box)
        self.body.append(columns)

        commands = Gtk.Box(spacing=8)
        check = Gtk.Button(label="Check Selected")
        check.set_tooltip_text("Ctrl- or Shift-click rows to select several")
        check.connect("clicked", lambda *_: self.check_selected(True))
        uncheck = Gtk.Button(label="Uncheck Selected")
        uncheck.connect("clicked", lambda *_: self.check_selected(False))
        new = Gtk.Button(label="New Transaction…")
        new.connect("clicked", lambda *_: self.new_transaction())
        edit = Gtk.Button(label="Edit Transaction…")
        edit.connect("clicked", lambda *_: self.edit_transaction())
        for button in (check, uncheck, new, edit):
            commands.append(button)
        self.body.append(commands)

        self.totals = Gtk.Label(xalign=0, wrap=True)
        self.body.append(self.totals)
        self.status = Gtk.Label(xalign=0, wrap=True)
        self.body.append(self.status)

        abandon = Gtk.Button(label="Abandon…")
        abandon.set_tooltip_text("Cancel this statement; no transaction has been changed")
        abandon.connect("clicked", lambda *_: self.confirm_abandon())
        self.footer.append(abandon)
        self.footer.append(Gtk.Box(hexpand=True))
        postpone = Gtk.Button(label="Postpone")
        postpone.set_tooltip_text("Close; the statement and its checks are kept to resume later")
        postpone.connect("clicked", lambda *_: self.close())
        self.footer.append(postpone)
        self.finish = Gtk.Button(label="Finish")
        self.finish.add_css_class("suggested-action")
        self.finish.connect("clicked", lambda *_: self._on_finish(handle))
        self.footer.append(self.finish)
        self._fill(state)
        self._show_totals(state)

    def _fill(self, state: reconciliation.ReconciliationSummary) -> None:
        """Rebuild both lists, keeping the rows that were selected."""
        selected = set(self.selected_splits()) if self._checks else set()
        self._checks = {}
        self._anchor = None
        self._extending = True
        try:
            self._fill_lists(state, selected)
        finally:
            self._extending = False

    def _resort(self, handle: str) -> None:
        """Rebuild the lists in their chosen order; checks and selection are kept."""
        self._fill(reconciliation.summary(self.db, handle))

    def _fill_lists(self, state: reconciliation.ReconciliationSummary, selected: set) -> None:
        statement = state.reconciliation.statement_date
        for column, funds_in in ((self.funds_in, True), (self.funds_out, False)):
            column.list.remove_all()
            column.splits = {}
            later_heading = False
            ordered = reconciliation.sort_candidates(
                state.candidates, column.sort, column.descending
            )
            for candidate in ordered:
                if candidate.funds_in is not funds_in:
                    continue
                if candidate.after_statement and not later_heading:
                    later_heading = True
                    header = Gtk.ListBoxRow(selectable=False, activatable=False)
                    label = Gtk.Label(label=f"After {statement}", xalign=0)
                    label.add_css_class("dim")
                    header.set_child(label)
                    column.list.append(header)
                row = self._row(candidate)
                column.splits[row] = candidate.split
                column.list.append(row)
                if candidate.split in selected:
                    column.list.select_row(row)
        self._keys = tuple(_candidate_key(item) for item in state.candidates)

    def _row(self, candidate: reconciliation.ReconciliationCandidate) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(spacing=8)
        for side in ("start", "end"):
            getattr(box, f"set_margin_{side}")(4)
        check = Gtk.CheckButton(active=candidate.selected)
        check.connect("toggled", self._on_check_toggled)
        check.update_property([Gtk.AccessibleProperty.LABEL], [f"Check {candidate.description}"])
        self._checks[candidate.split] = check
        when = Gtk.Label(label=candidate.post_date.isoformat(), xalign=0)
        # The check number: sometimes all a statement lists for an entry.
        num = Gtk.Label(label=candidate.num, xalign=0)
        num.set_width_chars(5)
        num.set_ellipsize(Pango.EllipsizeMode.END)
        num.add_css_class("numeric")
        description = Gtk.Label(
            label=candidate.description or "(no description)", xalign=0, hexpand=True
        )
        description.set_ellipsize(Pango.EllipsizeMode.END)
        description.set_width_chars(8)
        amount = Gtk.Label(label=abs(candidate.amount).format(), xalign=1)
        amount.add_css_class("numeric")
        for widget in (check, when, num, description, amount):
            box.append(widget)
        row.set_child(box)
        if candidate.after_statement:
            row.add_css_class("after-statement")
            row.set_tooltip_text("Dated after the statement: check it only if its date is wrong")
        return row

    def _set_checks(self, state: reconciliation.ReconciliationSummary) -> None:
        self._updating = True
        try:
            for candidate in state.candidates:
                check = self._checks.get(candidate.split)
                if check is not None and check.get_active() != candidate.selected:
                    check.set_active(candidate.selected)
        finally:
            self._updating = False

    def _show_starting_balance(self) -> None:
        if self.session is not None or not hasattr(self, "starting_balance"):
            return
        try:
            when = parse_date_text(self.statement_date.get_text().strip())
        except ValueError:
            self.starting_balance.set_text("—")
            return
        balance = reconciliation.reconciled_balance(self.db, self.account.handle, when)
        self.starting_balance.set_text(balance.format(parens_negative=True))

    def _show_totals(self, state: reconciliation.ReconciliationSummary) -> None:
        self.funds_in.total.set_text(f"Checked {state.selected_in.format()}")
        self.funds_out.total.set_text(f"Checked {state.selected_out.format()}")
        self.totals.set_text(
            f"Starting {state.opening_balance.format(parens_negative=True)}  ·  "
            f"Reconciled {state.selected_balance.format(parens_negative=True)}  ·  "
            f"Ending {state.reconciliation.ending_balance.format(parens_negative=True)}  ·  "
            f"Difference {state.difference.format(parens_negative=True)}"
        )
        self.finish.set_sensitive(state.balanced)

    # -------------------------------------------------------------- commands

    def selected_splits(self) -> list[str]:
        """The rows selected in either list, as split handles."""
        return [*self.funds_in.selected_splits(), *self.funds_out.selected_splits()]

    def _save_checks(self, checked: set[str]) -> None:
        if self.session is None:
            return
        ordered = [split for split in self._checks if split in checked]
        result = update_reconciliation(
            self.db, UpdateReconciliation(self.session, selected_splits=tuple(ordered))
        )
        if result.value is None:
            self.status.set_text(service_error_message(result.errors[0]))
            return
        self.status.set_text("")
        self._set_checks(result.value)
        self._show_totals(result.value)

    def _on_check_toggled(self, *_args) -> None:
        if self._updating:
            return
        self._save_checks({split for split, check in self._checks.items() if check.get_active()})

    def _on_row_activated(self, _list, row: Gtk.ListBoxRow) -> None:
        # Enter (or Space) on a row toggles its check; the mouse is _on_list_pressed's.
        split = self.funds_in.splits.get(row) or self.funds_out.splits.get(row)
        if split is not None:
            self._checks[split].set_active(not self._checks[split].get_active())

    def _on_list_pressed(self, gesture, n_press: int, x: float, y: float, column) -> None:
        target = column.list.pick(x, y, Gtk.PickFlags.DEFAULT)
        while target is not None and target is not column.list:
            if isinstance(target, Gtk.CheckButton):
                return
            target = target.get_parent()
        row = column.list.get_row_at_y(int(y))
        if row is None or row not in column.splits:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        state = gesture.get_current_event_state()
        self.click_row(
            row,
            n_press,
            ctrl=bool(state & Gdk.ModifierType.CONTROL_MASK),
            shift=bool(state & Gdk.ModifierType.SHIFT_MASK),
        )

    def click_row(
        self, row: Gtk.ListBoxRow, n_press: int = 1, *, ctrl: bool = False, shift: bool = False
    ):
        """A mouse click on a row, as either list receives it (#374, #375).

        A plain click selects only that row, in both lists, and toggles its check.
        Ctrl adds or removes the row from the selection and Shift extends it from
        the last row clicked in the same list; neither checks anything. The second
        click of a double-click puts the check back and opens the transaction.
        """
        column = self.funds_in if row in self.funds_in.splits else self.funds_out
        split = column.splits.get(row)
        if split is None:
            return None
        if n_press >= 2:
            if ctrl or shift:
                return None
            self._toggle(split)
            return self.edit_transaction(split)
        self._extending = ctrl or shift
        try:
            if shift and self._anchor is not None and self._anchor[0] is column:
                column.list.unselect_all()
                first, last = sorted((self._anchor[1].get_index(), row.get_index()))
                for index in range(first, last + 1):
                    item = column.list.get_row_at_index(index)
                    if item is not None and item in column.splits:
                        column.list.select_row(item)
            elif shift:
                column.list.select_row(row)
                self._anchor = (column, row)
            elif ctrl:
                if row.is_selected():
                    column.list.unselect_row(row)
                else:
                    column.list.select_row(row)
                self._anchor = (column, row)
            else:
                for other in (self.funds_in, self.funds_out):
                    other.list.unselect_all()
                column.list.select_row(row)
                self._anchor = (column, row)
                self._toggle(split)
        finally:
            self._extending = False
        self._last_split = split
        row.grab_focus()
        return None

    def _toggle(self, split: str) -> None:
        check = self._checks.get(split)
        if check is not None:
            check.set_active(not check.get_active())

    def _on_selection_changed(self, changed: Gtk.ListBox) -> None:
        # Moving the selection in one list by keyboard leaves the other empty, so
        # the selected row is always the one the commands act on (#375).
        if self._extending or not changed.get_selected_rows():
            return
        for column in (self.funds_in, self.funds_out):
            if column.list is not changed:
                column.list.unselect_all()

    def check_selected(self, checked: bool) -> None:
        """Check, or uncheck, every selected row in one change."""
        chosen = set(self.selected_splits())
        if not chosen:
            self.status.set_text("Select rows first; Ctrl- or Shift-click selects several.")
            return
        current = {split for split, check in self._checks.items() if check.get_active()}
        self._save_checks(current | chosen if checked else current - chosen)

    def _statement(self) -> reconciliation.ReconciliationSummary | None:
        return reconciliation.summary(self.db, self.session) if self.session else None

    def new_transaction(self) -> Gtk.Window:
        """Enter a transaction in this account, dated on the statement."""
        from .transaction_dialog import TransactionDialog

        state = self._statement()
        when = state.reconciliation.statement_date if state is not None else None
        dialog = TransactionDialog(
            self, self.db, default_account=self.account.handle, default_date=when
        )
        dialog.present()
        return dialog

    def edit_transaction(self, split: str | None = None) -> Gtk.Window | None:
        """Edit the transaction of ``split``, else of the row clicked last among the
        selected ones; the lists follow the change."""
        from .transaction_dialog import TransactionDialog

        chosen = self.selected_splits()
        if split is None:
            split = self._last_split if self._last_split in chosen else next(iter(chosen), None)
        state = self._statement()
        if split is None or state is None:
            self.status.set_text("Select a row to edit its transaction.")
            return None
        candidate = next((item for item in state.candidates if item.split == split), None)
        if candidate is None:
            return None
        transaction = self.db.get_transaction(candidate.transaction)
        if transaction is None:
            return None
        dialog = TransactionDialog(self, self.db, transaction=transaction)
        dialog.present()
        return dialog

    def add_statement_entry(self) -> bool:
        """Add the quick-add line, cleared, dated on the statement date."""
        try:
            when = parse_date_text(self.statement_date.get_text().strip())
            amount = Money(parse_user_amount(self.entry_amount.get_text().strip()))
        except (ValueError, ArithmeticError):
            self.status.set_text("Enter a statement date and an amount to add.")
            return False
        index = self.entry_account.get_selected()
        if not self.entry_accounts or index >= len(self.entry_accounts):
            self.status.set_text("Choose the account the charge or credit belongs to.")
            return False
        other = self.entry_accounts[index]
        description = self.entry_description.get_text().strip()
        result = add_statement_entry(
            self.db,
            AddStatementEntry(self.account.handle, other.handle, when, description, amount),
        )
        if result.value is None:
            self.status.set_text(service_error_message(result.errors[0]))
            return False
        self.entry_amount.set_text("")
        self.status.set_text(
            f"Added {result.value.description} on {when}, cleared; it starts checked."
        )
        self._show_starting_balance()
        return True

    def confirm_abandon(self) -> Gtk.AlertDialog:
        alert = Gtk.AlertDialog(
            message="Abandon this reconciliation?",
            detail=(
                "The statement and its checks are discarded. No transaction has been "
                "changed by reconciling it."
            ),
            modal=True,
            buttons=["Keep Reconciling", "Abandon"],
            cancel_button=0,
            default_button=0,
        )

        def answered(dialog, result) -> None:
            try:
                choice = dialog.choose_finish(result)
            except GLib.Error:
                return
            if choice == ABANDON:
                self.abandon()

        alert.choose(self, None, answered)
        return alert

    def abandon(self) -> bool:
        if self.session is None:
            return False
        result = cancel_reconciliation(self.db, ReconciliationAction(self.session))
        if result.value is None:
            self.status.set_text(service_error_message(result.errors[0]))
            return False
        self._render()
        return True

    def _on_start(self, _button) -> None:
        try:
            statement_date = parse_date_text(self.statement_date.get_text().strip())
            ending = Money(parse_user_amount(self.ending_balance.get_text().strip()))
            result = start_reconciliation(
                self.db,
                StartReconciliation(self.account.handle, statement_date, ending),
            )
        except (ValueError, ArithmeticError) as exc:
            self.status.set_text(str(exc))
            return
        if result.value is None:
            self.status.set_text(service_error_message(result.errors[0]))
            return
        self._render()

    def _on_update_target(self, handle: str) -> None:
        try:
            ending = Money(parse_user_amount(self.target_entry.get_text().strip()))
            result = update_reconciliation(
                self.db,
                UpdateReconciliation(handle, ending_balance=ending),
            )
        except (ValueError, ArithmeticError):
            self.status.set_text("Enter a valid ending balance")
            self.finish.set_sensitive(False)
            return
        if result.value is None:
            self.status.set_text(service_error_message(result.errors[0]))
            self.finish.set_sensitive(False)
            return
        self.status.set_text("")
        self._show_totals(result.value)

    def _on_finish(self, handle: str) -> None:
        result = complete_reconciliation(self.db, ReconciliationAction(handle))
        if result.value is None:
            self.status.set_text(service_error_message(result.errors[0]))
            return
        self.close()

    def _on_reopen(self, handle: str) -> None:
        result = reopen_reconciliation(self.db, ReconciliationAction(handle))
        if result.value is None:
            self.status.set_text(service_error_message(result.errors[0]))
            return
        self._render()
