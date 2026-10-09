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
re-sorts and re-totals correctly.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from ...gen.engine import ledger
from ...gen.lib.account import AccountClass, AccountType
from ..gi_setup import GLib, Gtk
from ..register_sheet import RegisterSheet
from ..widgets.choice import bounded_dropdown
from ..widgets.register_grid import RegisterGrid
from ._base import BaseView

__all__ = ["RegisterView", "column_headings"]

#: Answers to the unsaved-input question, in AlertDialog button order.
CANCEL, DISCARD, SAVE = 0, 1, 2


def column_headings(atype: AccountType) -> tuple[str, str]:
    """Debit and credit column headings for an account type."""
    return ledger.register_headings(atype)


class RegisterView(BaseView):
    """A ledger grid for one account at a time."""

    WATCHES = (
        "database-changed",
        "transaction-add",
        "transaction-update",
        "transaction-delete",
        "account-update",
    )

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self.account_handle: str | None = None
        self.sheet: RegisterSheet | None = None
        self._pickable: list = []
        # Repopulating the picker resets its selection, which fires
        # notify::selected and would otherwise overwrite the account the caller
        # just asked for.
        self._updating = False
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
            "Filter by description, number, notes, tag, memo, or account"
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
        bar.append(self.split_button)

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
        self.sheet.set_filter(self.filter_entry.get_text())
        self.grid.set_sheet(self.sheet)
        self._show_balance()

    def _show_balance(self) -> None:
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

        def focus() -> bool:
            self.grid.sync()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(focus)

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

    def cursor_transaction(self):
        """The stored transaction under the cursor, or None on the blank one."""
        return self.sheet.draft.source if self.sheet is not None else None

    def track_selected_reimbursable(self, *_args):
        """Open reimbursable expenses to track the cursor transaction's cost."""
        transaction = self.cursor_transaction()
        if self.db is None or transaction is None:
            self.set_entry_status(
                "Put the cursor on a transaction to track as reimbursable.", error=True
            )
            return None
        transaction = self.db.get_transaction(transaction.handle)
        accounts = {account.handle: account for account in self.db.iter_accounts()}
        cost = next(
            (
                split
                for split in (transaction.splits if transaction is not None else ())
                if split.value > 0
                and accounts.get(split.account) is not None
                and accounts[split.account].account_class is AccountClass.EXPENSE
            ),
            None,
        )
        if transaction is None or cost is None:
            self.set_entry_status(
                "Only a transaction with an expense can be tracked as reimbursable.", error=True
            )
            return None
        application = self.manager.get_application() if self.manager is not None else None
        opener = getattr(application, "on_receivables", None)
        if opener is None:
            return None
        return opener(expense=(transaction.handle, cost.handle))

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
            when = date.fromisoformat(draft.date.strip())
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

        dialog = ReconciliationDialog(self.get_root(), self.db, account)
        dialog.connect("close-request", self.refresh_on_close)
        dialog.present()
