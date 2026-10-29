"""The register: one account's transactions in a GnuCash-style ledger grid.

Columns follow GnuCash's basic ledger — date, number, description, transfer,
reconcile state, then separate debit and credit columns whose headings change
with the account type (``Deposit``/``Withdrawal`` for a bank account,
``Charge``/``Payment`` for a credit card). Those labels are not decoration:
"debit" and "credit" are the single most common source of data-entry errors in a
household ledger, and naming the columns after what the account actually does
removes the translation step.

The grid (``widgets/register_grid``) draws ruled plain-text rows with one editor
at the cursor, and the sheet behind it (``register_sheet``) holds the cursor and
the transaction being typed. A register opens on the blank transaction at the
end, ready for the next entry, as a check register does. The running balance is
computed by the engine, not accumulated in the widget, so a back-dated entry
re-sorts and re-totals correctly. An account that holds a security shows shares,
price, and shares held instead of the amount balance, and its footer the shares
and their quoted value (#323).
"""

from __future__ import annotations

import weakref
from collections.abc import Callable
from datetime import date

from ...gen.engine import ledger
from ...gen.lib.account import Account, AccountClass, AccountType
from ...presentation.investments import register_share_summary
from ..gi_setup import GLib, Gtk
from ..register_sheet import RegisterSheet
from ..widgets.choice import bounded_dropdown
from ..widgets.date_entry import parse_date_text
from ..widgets.register_grid import BANDING, RegisterGrid
from ._base import BaseView

__all__ = ["RegisterView", "column_headings"]

#: Every register showing, in any window, so a display setting reaches them all.
_LIVE_REGISTERS: weakref.WeakSet = weakref.WeakSet()


def apply_register_display() -> None:
    """Redraw every open register with the current Double Line and shading settings."""
    for register in list(_LIVE_REGISTERS):
        register.apply_display()


#: The row shading choices, in the order Preferences lists them.
BANDING_CHOICES = (
    ("transactions", "Shade alternate transactions"),
    ("lines", "Shade alternate lines"),
    ("none", "No shading"),
)

#: Answers to the unsaved-input question, in AlertDialog button order.
CANCEL, DISCARD, SAVE = 0, 1, 2


def column_headings(atype: AccountType) -> tuple[str, str]:
    """Debit and credit column headings for an account type."""
    return ledger.register_headings(atype)


class RegisterView(BaseView):
    """A ledger grid for one account at a time."""

    # Transaction signals are connected separately (``set_db``): they name the
    # transactions that changed, so the register reloads only those (#319).
    WATCHES = ("database-changed", "account-update")
    TRANSACTION_SIGNALS = ("transaction-add", "transaction-update", "transaction-delete")
    #: Where the Double Line choice is remembered.
    SETTINGS_SECTION = "register"

    def __init__(self, manager) -> None:
        #: A focus request still waiting for the grid to be shown (``focus_entry``).
        self._focus_source = 0
        super().__init__(manager)
        self.account_handle: str | None = None
        self.sheet: RegisterSheet | None = None
        self._pickable: list = []
        # Repopulating the picker resets its selection, which fires
        # notify::selected and would otherwise overwrite the account the caller
        # just asked for.
        self._updating = False
        #: Transactions changed while the register was hidden, reloaded when shown.
        self._pending_changes: set[str] = set()
        #: Set when this commit's transaction signals were handled, so the
        #: database-changed signal that follows them does not reload again.
        self._handled_commit = False
        self._build()

    def _build(self) -> None:
        bar = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{side}")(8)

        self.account_picker = bounded_dropdown()
        self.account_picker.set_hexpand(True)
        self.account_picker.connect("notify::selected", self._on_account_changed)
        bar.append(self.account_picker)

        self.filter_entry = Gtk.SearchEntry(placeholder_text="Filter this register")
        self.filter_entry.set_tooltip_text(
            "Filter by anything a transaction holds: date, number, description, notes, "
            "tags, memos, accounts, or an amount such as 67 or 67.00"
        )
        self.filter_entry.connect("search-changed", lambda *_: self._apply_filter())
        bar.append(self.filter_entry)

        self.balance_label = Gtk.Label(xalign=1)
        self.balance_label.add_css_class("summary-value")
        self.balance_label.add_css_class("numeric")
        bar.append(self.balance_label)

        self.split_button = Gtk.Button(label="Split")
        self.split_button.set_tooltip_text(
            "Show the transaction under the cursor split by split, or join it again"
        )
        self.split_button.connect("clicked", lambda *_: self.toggle_split())
        # Toolbar choices leave the keyboard in the register (#325).
        self.split_button.set_focus_on_click(False)
        bar.append(self.split_button)

        # GnuCash's Enter and Cancel: the keys' actions as buttons (#344), so an
        # entry can be saved or abandoned without knowing Enter and Escape.
        self.enter_button = Gtk.Button(label="Enter")
        self.enter_button.set_tooltip_text("Save the transaction being entered or changed (Enter)")
        self.enter_button.connect("clicked", lambda *_: self.enter_transaction())
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.set_tooltip_text(
            "Put the transaction being entered or changed back as it was (Escape)"
        )
        self.cancel_button.connect("clicked", lambda *_: self.cancel_transaction())
        for button in (self.enter_button, self.cancel_button):
            button.set_focus_on_click(False)
            bar.append(button)

        # Double Line (View menu) and row shading (Preferences) are settings shared
        # by every register, not toolbar controls (#333).
        self.double_line = self._settings_double_line()
        _LIVE_REGISTERS.add(self)

        self.editor_button = Gtk.Button(icon_name="document-edit-symbolic")
        self.editor_button.set_tooltip_text(
            "Open the transaction under the cursor in the full editor "
            "(notes, a claim, and other details)"
        )
        self.editor_button.connect("clicked", lambda *_: self.open_full_editor())
        bar.append(self.editor_button)

        add_button = Gtk.Button(icon_name="list-add-symbolic")
        add_button.set_tooltip_text("New transaction in the full editor")
        add_button.connect("clicked", self._on_add_clicked)
        bar.append(add_button)

        new_window = Gtk.Button(icon_name="window-new-symbolic")
        new_window.set_tooltip_text("Open this account in an independent register window")
        new_window.connect("clicked", self._on_open_window_clicked)
        bar.append(new_window)

        self.reconcile_button = Gtk.Button(label="Reconcile…")
        self.reconcile_button.set_tooltip_text("Compare this account with a statement")
        self.reconcile_button.connect("clicked", self._on_reconcile_clicked)
        bar.append(self.reconcile_button)

        self.append_toolbar(bar)

        self.grid = RegisterGrid(on_change=self._show_message)
        self.grid.set_banding(self._settings_banding())
        for side in ("start", "end"):
            getattr(self.grid, f"set_margin_{side}")(8)
        self.grid.set_vexpand(True)
        self.append(self.grid)

        # One line under the register reports what was saved, or why not.
        self.entry_status = Gtk.Label(xalign=0, wrap=True)
        self.entry_status.add_css_class("dim")
        for side in ("start", "end"):
            getattr(self.entry_status, f"set_margin_{side}")(8)
        self.entry_status.set_margin_bottom(4)
        self.append(self.entry_status)

    # ------------------------------------------------------------------ model

    def refresh(self) -> None:
        if self.db is None:
            return
        self._updating = True
        try:
            self._populate_picker()
        finally:
            self._updating = False
        if self.account_handle is None:
            return
        account = self.db.get_account(self.account_handle)
        if account is None:
            return
        self.reconcile_button.set_sensitive(
            not account.placeholder
            and account.account_class in {AccountClass.ASSET, AccountClass.LIABILITY}
        )
        if (
            self.sheet is None
            or self.sheet.db is not self.db
            or self.sheet.account != account.handle
        ):
            self.sheet = RegisterSheet(self.db, account.handle)
        else:
            # A change made elsewhere: re-read, keeping the cursor and any typing.
            self.sheet.reload()
        self._pending_changes.clear()
        self.sheet.set_double_line(self.double_line)
        self.sheet.set_filter(self.filter_entry.get_text())
        self.grid.set_sheet(self.sheet)
        self._show_balance()

    # ------------------------------------------------------------- updates

    def set_db(self, db) -> None:
        super().set_db(db)
        if db is None:
            # Nothing is left to focus once the book has closed.
            self.cancel_focus_entry()
            return
        for signal in self.TRANSACTION_SIGNALS:
            self._handlers.append(db.connect(signal, self._on_transactions))

    def _on_transactions(self, handles) -> None:
        """Reload just the transactions a commit changed (#319)."""
        self._handled_commit = True
        sheet = self.sheet
        if sheet is None or sheet.db is not self.db:
            return
        if sheet.saving:
            # The sheet's own save reloads what it saved once it has finished.
            return
        if not self.get_mapped():
            self._pending_changes.update(handles)
            self._stale = True
            return
        sheet.reload(changed=handles)
        self.grid.set_sheet(sheet)
        self._show_balance()

    def _on_change(self, *args) -> None:
        if self._handled_commit:
            # The transaction signals of this commit already updated the register.
            self._handled_commit = False
            return
        super()._on_change(*args)

    def _on_map_refresh(self) -> None:
        sheet = self.sheet
        if self._pending_changes and sheet is not None and not self._full_refresh_pending:
            changed = set(self._pending_changes)
            self._pending_changes.clear()
            sheet.reload(changed=changed)
            self.grid.set_sheet(sheet)
            self._show_balance()
            return
        self.refresh()

    def _settings(self):
        application = self.manager.get_application() if self.manager is not None else None
        return getattr(application, "view_settings", None)

    def _settings_double_line(self) -> bool:
        settings = self._settings()
        return settings.get_bool(self.SETTINGS_SECTION, "double_line") if settings else False

    def _settings_banding(self) -> str:
        settings = self._settings()
        chosen = settings.get(self.SETTINGS_SECTION, "banding") if settings else None
        return chosen if chosen in BANDING else "transactions"

    def apply_display(self) -> None:
        """Show Double Line and row shading as the settings now say."""
        self.double_line = self._settings_double_line()
        self.grid.set_banding(self._settings_banding())
        # A register whose book has closed waits only for garbage collection; it
        # has nothing to redraw, and its sheet's book is gone.
        if self.sheet is not None and self.db is not None and self.sheet.db is self.db:
            self.sheet.set_double_line(self.double_line)
            # The cursor's cell keeps the keyboard, so typing goes on (#325).
            self.grid.sync(focus=self.get_mapped())

    def _show_balance(self) -> None:
        if self.db is None:
            # The book has closed; this register is waiting to be dropped.
            return
        summary = (
            register_share_summary(self.db, self.sheet.account)
            if self.sheet is not None and self.sheet.security is not None
            else None
        )
        if summary is not None:
            # A security's footer is its shares and what they are worth (#323).
            self.balance_label.set_text(summary)
            held = self.sheet.closing_shares() if self.sheet is not None else None
            if held is not None and held < 0:
                self.balance_label.add_css_class("negative")
            else:
                self.balance_label.remove_css_class("negative")
            return
        closing = self.sheet.closing_balance() if self.sheet is not None else None
        self.balance_label.set_text(closing.format(parens_negative=True) if closing else "0.00")
        if closing is not None and closing < 0:
            self.balance_label.add_css_class("negative")
        else:
            self.balance_label.remove_css_class("negative")

    def _apply_filter(self) -> None:
        if self.sheet is not None:
            self.sheet.set_filter(self.filter_entry.get_text())
            self.grid.sync(focus=False)

    def _show_message(self) -> None:
        sheet = self.sheet
        if sheet is None:
            return
        self.set_entry_status(sheet.message, error=sheet.error)
        self._show_balance()

    def _populate_picker(self) -> None:
        db = self.db
        if db is None:
            return
        accounts = sorted(
            (a for a in db.iter_accounts() if not a.is_root and not a.placeholder),
            key=db.full_name,
        )
        self._pickable = accounts
        model = Gtk.StringList()
        for account in accounts:
            model.append(db.full_name(account))
        self.account_picker.set_model(model)
        if self.account_handle is None and accounts:
            self.account_handle = accounts[0].handle
        self._select_in_picker(self.account_handle)

    def _select_in_picker(self, handle: str | None) -> None:
        previous = self._updating
        self._updating = True
        try:
            for index, account in enumerate(self._pickable):
                if account.handle == handle:
                    self.account_picker.set_selected(index)
                    break
        finally:
            self._updating = previous

    # ----------------------------------------------------------- the cursor

    def show_account(self, handle: str) -> None:
        """Show ``handle``'s register, on its blank transaction ready for input."""

        def switch() -> None:
            self.account_handle = handle
            self.sheet = None
            self.set_entry_status("")
            self.refresh()
            self.focus_entry()

        if handle == self.account_handle and self.sheet is not None:
            if self.sheet.to_blank():
                self.grid.sync()
            return
        self.confirm_leave(switch)

    def focus_entry(self) -> None:
        """Put keyboard focus in the cell under the cursor, once the grid is shown."""
        # Only the latest request waits: an older one, from a tab shown before,
        # must not take the keyboard back from wherever it went since.
        self.cancel_focus_entry()
        tries = [0]

        def focus() -> bool:
            # A register just shown may not be sized yet; the editor has no cell
            # to sit on until it is, so wait for that (briefly) before focusing.
            grid = self.grid
            ready = grid.get_mapped() and grid._widths and grid._height > 0
            if not ready and tries[0] < 50:
                tries[0] += 1
                return GLib.SOURCE_CONTINUE
            self._focus_source = 0
            # A register whose book closed, or never shown, has nothing to focus.
            if ready and self.db is not None and self.sheet is not None:
                grid.sync()
            return GLib.SOURCE_REMOVE

        self._focus_source = GLib.timeout_add(20, focus)

    def cancel_focus_entry(self) -> None:
        """Forget a focus request still waiting for the grid (Ctrl+F moved it)."""
        if self._focus_source:
            GLib.source_remove(self._focus_source)
        self._focus_source = 0

    def edit_selected_in_place(self, *_args) -> bool:
        """Typing goes into the transaction under the cursor; this puts focus there."""
        if self.sheet is None:
            return False
        self.grid.sync()
        return True

    def toggle_split(self) -> bool:
        if self.sheet is None:
            return False
        done = self.sheet.toggle_split()
        self.grid.sync()
        return done

    def enter_transaction(self) -> bool:
        """Save the transaction under the cursor; False, with the reason shown, if not."""
        return self.sheet is not None and self.grid.enter()

    def cancel_transaction(self) -> None:
        """Abandon the typing in the transaction under the cursor (the blank one empties)."""
        if self.sheet is not None:
            self.grid.cancel()

    def cursor_transaction(self):
        """The stored transaction under the cursor, or None on the blank one."""
        return self.sheet.draft.source if self.sheet is not None else None

    def track_selected_reimbursable(self, *_args):
        """Open reimbursable expenses to track the cursor transaction's cost.

        Typing not yet entered is saved first, so a transaction just typed (or
        just changed to an expense) can be tracked at once. Every refusal is an
        alert as well as the status line: the command is chosen from a menu, and
        a line under the register was too easy to miss (#371). The dialog opens
        over this register's own window, never over another one the user left
        open, such as the guide.
        """
        if self.db is None or self.sheet is None:
            return self._refuse_tracking("Open a register before tracking a reimbursable expense.")
        sheet = self.sheet
        handle = sheet.draft.source.handle if sheet.draft.source is not None else None
        if sheet.draft.dirty():
            if not sheet.leave():
                self.grid.sync()
                return self._refuse_tracking(
                    f"Save the transaction first: {sheet.message or 'it could not be saved.'}"
                )
            handle = sheet.last_saved
            if handle is not None:
                sheet.select_transaction(handle)
            self.grid.sync()
        transaction = self.db.get_transaction(handle) if handle is not None else None
        if transaction is None:
            return self._refuse_tracking(
                "Put the cursor on a transaction to track as reimbursable."
            )
        accounts = {account.handle: account for account in self.db.iter_accounts()}

        def costs(kind: Callable[[Account], bool]):
            return (
                split
                for split in transaction.splits
                if split.value > 0
                and accounts.get(split.account) is not None
                and kind(accounts[split.account])
            )

        # An expense, else a charge recorded straight to a receivable account (#378).
        cost = next(costs(lambda item: item.account_class is AccountClass.EXPENSE), None) or next(
            costs(lambda item: item.atype is AccountType.RECEIVABLE), None
        )
        if cost is None:
            return self._refuse_tracking(
                f"“{transaction.description}” has nothing to track: choose a transaction "
                "that spends on an expense account, or charges a Receivable account "
                "for what someone will pay back."
            )
        root = self.get_root()
        application = (root.get_application() if isinstance(root, Gtk.Window) else None) or (
            self.manager.get_application() if self.manager is not None else None
        )
        opener = getattr(application, "on_receivables", None)
        if opener is None:
            return self._refuse_tracking("Reimbursable expenses cannot be opened from here.")
        parent = root if isinstance(root, Gtk.Window) else None
        return opener(expense=(transaction.handle, cost.handle), parent=parent)

    def _refuse_tracking(self, text: str) -> None:
        self.set_entry_status(text, error=True)
        root = self.get_root()
        Gtk.AlertDialog(message="Not tracked as reimbursable", detail=text).show(
            root if isinstance(root, Gtk.Window) else None
        )
        return None

    def open_in_new_tab(self) -> None:
        """Open another register tab beside this one (#228)."""
        opener = getattr(self.manager, "open_register_tab", None)
        if opener is not None and self.db is not None:
            opener()

    # ------------------------------------------------------- unsaved typing

    def has_unsaved(self) -> bool:
        return self.sheet is not None and self.sheet.draft.dirty()

    def confirm_leave(self, proceed: Callable[[], None]) -> None:
        """Run ``proceed`` now, or after Save/Discard when typing would be lost."""
        sheet = self.sheet
        if sheet is None or not sheet.draft.dirty():
            proceed()
            return

        def answered(choice: int) -> None:
            if choice == SAVE:
                if sheet.save():
                    proceed()
                else:
                    self.grid.sync()
            elif choice == DISCARD:
                sheet.escape()
                self.set_entry_status("")
                proceed()

        self.ask_unsaved(answered)

    def ask_unsaved(self, answered: Callable[[int], None]) -> None:
        editing = self.cursor_transaction() is not None
        alert = Gtk.AlertDialog(
            message=(
                "Save your changes to this transaction?"
                if editing
                else "Save the transaction you were entering?"
            ),
            detail="The register has typing that has not been saved.",
            buttons=["Cancel", "Discard", "Save"],
            cancel_button=CANCEL,
            default_button=SAVE,
        )

        def finished(dialog, result) -> None:
            try:
                choice = dialog.choose_finish(result)
            except GLib.Error:
                choice = CANCEL
            answered(choice)

        alert.choose(self.get_root(), None, finished)

    def set_entry_status(self, text: str, *, error: bool = False) -> None:
        self.entry_status.set_text(text)
        if error:
            self.entry_status.add_css_class("negative")
        else:
            self.entry_status.remove_css_class("negative")

    # ---------------------------------------------------------------- actions

    def _on_account_changed(self, picker, _param) -> None:
        if self._updating:
            return
        index = picker.get_selected()
        if 0 <= index < len(self._pickable):
            handle = self._pickable[index].handle
            if handle != self.account_handle:
                # Until the user answers, the picker keeps showing this account.
                self._select_in_picker(self.account_handle)
                self.show_account(handle)

    def edit_transaction(self, transaction) -> None:
        if self.db is None:
            return
        self.confirm_leave(lambda: self._open_editor(transaction))

    def open_full_editor(self):
        """The full editor for the transaction under the cursor, or for a new one
        prefilled with whatever the blank transaction holds."""
        transaction = self.cursor_transaction()
        if transaction is not None:
            self.edit_transaction(transaction)
            return None
        return self._new_in_editor()

    def _open_editor(self, transaction):
        if self.db is None:
            return None
        from ..dialogs.transaction_dialog import TransactionDialog

        dialog = TransactionDialog(
            self.get_root(),
            self.db,
            default_account=self.account_handle,
            transaction=transaction,
        )
        dialog.connect("close-request", self.refresh_on_close)
        dialog.present()
        return dialog

    def _new_in_editor(self):
        sheet = self.sheet
        if self.db is None or self.account_handle is None or sheet is None:
            return None
        from ..dialogs.transaction_dialog import TransactionDialog

        draft = sheet.draft
        try:
            when = parse_date_text(draft.date.strip())
        except ValueError:
            when = date.today()
        dialog = TransactionDialog(
            self.get_root(), self.db, default_account=self.account_handle, default_date=when
        )
        amount = None
        if not draft.expanded:
            try:
                amount = sheet._simple_amount()
            except ValueError:
                amount = None
        transfer = sheet.transfer_names.get(draft.transfer.strip())
        lines = None
        if draft.expanded:
            lines = []
            for line in draft.lines:
                if not line.has_input():
                    continue
                text, sign = line.amount_text()
                try:
                    value = sheet.read_amount(text) if text else None
                except ValueError:
                    value = None
                lines.append(
                    (
                        sheet.account_names.get(line.account.strip()),
                        value if value is None or sign > 0 else -value,
                        line.memo.strip(),
                    )
                )
        dialog.prefill(
            description=draft.description.strip(),
            num=draft.num.strip(),
            transfer=transfer,
            amount=amount,
            splits=lines,
        )

        def on_close(*_args) -> bool:
            # Saving in the editor used the blank row's typing, so it empties;
            # cancelling leaves it exactly as it was.
            if dialog.saved:
                sheet.escape()
            self.refresh_on_close()
            return False

        dialog.connect("close-request", on_close)
        dialog.present()
        return dialog

    def _on_add_clicked(self, *_args):
        if self.db is None or self.account_handle is None:
            return None
        from ..dialogs.transaction_dialog import TransactionDialog

        dialog = TransactionDialog(
            self.get_root(),
            self.db,
            default_account=self.account_handle,
            default_date=date.today(),
        )
        dialog.connect("close-request", self.refresh_on_close)
        dialog.present()
        return dialog

    def _on_open_window_clicked(self, *_args) -> None:
        if self.account_handle is None:
            return
        opener = getattr(self.manager, "open_register_window", None)
        if callable(opener):
            opener(self.account_handle)

    def _on_reconcile_clicked(self, *_args) -> None:
        if self.db is None or self.account_handle is None:
            return
        account = self.db.get_account(self.account_handle)
        if account is None:
            return
        from ..dialogs.reconciliation_dialog import ReconciliationDialog

        # Not modal, and one per account: the register stays usable while
        # reconciling, and Reconcile brings an open statement forward (#362).
        ReconciliationDialog.present_for(self.get_root(), self.db, account)
