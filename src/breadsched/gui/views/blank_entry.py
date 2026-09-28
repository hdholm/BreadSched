"""The register's blank entry row: type a new transaction where it will appear (#158).

The last row of every GTK register is a blank transaction. It is a sentinel, not a
database object: its cells host one persistent set of entry widgets owned by
:class:`BlankEntryRow`, so a value typed there survives the register repainting
around it. Committing builds exactly two balancing splits and saves them through
the ordinary ``save_transaction`` service; it is not a parallel transaction model.
Anything more than two splits goes through "Split…" to the full editor.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from ...gen.lib.amount import Amount
from ...gen.lib.money import Money
from ...gen.services import (
    SaveTransaction,
    TransactionInput,
    TransactionSplitInput,
    save_transaction,
    transaction_currency,
)
from ...gen.services.autocomplete import EntrySuggestion, SuggestEntry, suggest_entry
from ...gen.utils.amount_input import parse_user_amount
from ...presentation import service_error_message
from ..gi_setup import Gdk, GLib, Gtk

__all__ = ["BLANK", "BlankEntry", "BlankEntryRow"]


class BlankEntry:
    """Marker payload for the register's last, always-editable row."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "BLANK"


BLANK = BlankEntry()

#: Answers to the unsaved-input question, in AlertDialog button order.
CANCEL, DISCARD, SAVE = 0, 1, 2


def _label(widget: Gtk.Widget, text: str) -> None:
    widget.update_property([Gtk.AccessibleProperty.LABEL], [text])


class BlankEntryRow:
    """The widgets and behavior of one register's blank entry row."""

    def __init__(self, view) -> None:
        self.view = view
        #: The date to offer next: the last one entered in this register, else today.
        self.last_date: date | None = None
        self.transfers: list = []
        self.payees: list = []
        self._syncing = False
        self._transfer_touched = False
        self._enabled = True

        self.date = Gtk.Entry(text=date.today().isoformat(), placeholder_text="YYYY-MM-DD")
        self.date.set_width_chars(10)
        self.num = Gtk.Entry(placeholder_text="Num")
        self.num.set_width_chars(4)
        self.description = Gtk.Entry(placeholder_text="Description")
        self.description.set_hexpand(True)
        self.payee = Gtk.DropDown.new_from_strings(["(no payee)"])
        self.transfer = Gtk.DropDown()
        self.transfer.set_hexpand(True)
        self.transfer.set_tooltip_text("Other side of this two-split transaction")
        self.increase = Gtk.Entry(placeholder_text="0.00", xalign=1)
        self.increase.set_width_chars(9)
        self.increase.add_css_class("numeric")
        self.decrease = Gtk.Entry(placeholder_text="0.00", xalign=1)
        self.decrease.set_width_chars(9)
        self.decrease.add_css_class("numeric")
        self.split_button = Gtk.Button(label="Split…")
        self.split_button.set_has_frame(False)
        self.split_button.set_tooltip_text(
            "Open this entry in the full editor for more splits, notes, or a claim"
        )
        self.split_button.connect("clicked", lambda *_: self.open_split_editor())

        #: Tab order, which is also the column order.
        self.fields: list[Gtk.Widget] = [
            self.date,
            self.num,
            self.description,
            self.payee,
            self.transfer,
            self.increase,
            self.decrease,
            self.split_button,
        ]
        for widget, name in (
            (self.date, "date"),
            (self.num, "number"),
            (self.description, "description"),
            (self.payee, "payee"),
            (self.transfer, "transfer account"),
            (self.split_button, "split editor"),
        ):
            _label(widget, f"New transaction {name}")
        for widget in self.fields:
            keys = Gtk.EventControllerKey()
            keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            keys.connect("key-pressed", self._on_key, widget)
            widget.add_controller(keys)

        # Typing in one amount clears the other, so the direction is never ambiguous.
        self.increase.connect("changed", self._on_amount_changed, self.decrease)
        self.decrease.connect("changed", self._on_amount_changed, self.increase)
        self.transfer.connect("notify::selected", self._on_transfer_changed)
        self.payee.connect("notify::selected", self._on_payee_changed)
        leave = Gtk.EventControllerFocus()
        leave.connect("leave", lambda *_: self.propose())
        self.description.add_controller(leave)

        #: Column title → the widget hosted in that column's blank cell.
        self.cells: dict[str, Gtk.Widget] = {
            "Date": self.date,
            "Num": self.num,
            "Description": self.description,
            "Payee": self.payee,
            "Transfer": self.transfer,
            "Increase": self.increase,
            "Decrease": self.decrease,
            "Balance": self.split_button,
        }

    # ----------------------------------------------------------------- state

    def default_date(self) -> date:
        return self.last_date or date.today()

    def has_input(self) -> bool:
        """Whether anything was typed that leaving would lose."""
        typed = (self.num, self.description, self.increase, self.decrease)
        return any(entry.get_text().strip() for entry in typed) or self.payee.get_selected() > 0

    def clear(self) -> None:
        """Reset every field to its default, keeping the date last entered."""
        self._syncing = True
        try:
            self.date.set_text(self.default_date().isoformat())
            for entry in (self.num, self.description, self.increase, self.decrease):
                entry.set_text("")
            self.payee.set_selected(0)
            if self.transfers:
                self.transfer.set_selected(0)
        finally:
            self._syncing = False
        self._transfer_touched = False

    def set_headings(self, increase: str, decrease: str) -> None:
        _label(self.increase, f"New transaction {increase.lower()}")
        _label(self.decrease, f"New transaction {decrease.lower()}")

    def set_enabled(self, enabled: bool, reason: str = "") -> None:
        """Make the row insensitive, saying why in the description cell."""
        self._enabled = enabled
        for widget in self.fields:
            widget.set_sensitive(enabled)
        self.description.set_placeholder_text("Description" if enabled else reason)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def populate(self, db, account_handle: str) -> None:
        """Rebuild the transfer and payee pickers, keeping their selections."""
        chosen_transfer = self.transfer_handle()
        chosen_payee = self.payee_handle()
        self._syncing = True
        try:
            self.transfers = sorted(
                (
                    account
                    for account in db.iter_accounts()
                    if account.handle != account_handle
                    and not account.is_root
                    and not account.placeholder
                    and not account.hidden
                ),
                key=db.full_name,
            )
            names = Gtk.StringList()
            for account in self.transfers:
                names.append(db.full_name(account))
            self.transfer.set_model(names)
            handles = [account.handle for account in self.transfers]
            if handles:
                self.transfer.set_selected(
                    handles.index(chosen_transfer) if chosen_transfer in handles else 0
                )
            self.payees = sorted(db.iter_payees(), key=lambda payee: payee.name.casefold())
            payee_names = Gtk.StringList()
            payee_names.append("(no payee)")
            for payee in self.payees:
                payee_names.append(payee.name)
            self.payee.set_model(payee_names)
            payee_handles = [payee.handle for payee in self.payees]
            self.payee.set_selected(
                payee_handles.index(chosen_payee) + 1 if chosen_payee in payee_handles else 0
            )
        finally:
            self._syncing = False
        if chosen_transfer not in [account.handle for account in self.transfers]:
            self._transfer_touched = False

    def transfer_handle(self) -> str | None:
        index = self.transfer.get_selected()
        if 0 <= index < len(self.transfers):
            return self.transfers[index].handle
        return None

    def payee_handle(self) -> str | None:
        index = self.payee.get_selected() - 1
        if 0 <= index < len(self.payees):
            return self.payees[index].handle
        return None

    def select_transfer(self, handle: str) -> bool:
        handles = [account.handle for account in self.transfers]
        if handle not in handles:
            return False
        self._syncing = True
        try:
            self.transfer.set_selected(handles.index(handle))
        finally:
            self._syncing = False
        return True

    # -------------------------------------------------------------- keyboard

    def _on_key(self, _controller, keyval: int, _keycode: int, state, widget) -> bool:
        return self.handle_key(widget, keyval, state)

    def handle_key(self, widget: Gtk.Widget, keyval: int, state=0) -> bool:
        """Tab/Shift+Tab move between fields, Enter commits, Escape clears."""
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab, Gdk.KEY_KP_Tab):
            backwards = keyval == Gdk.KEY_ISO_Left_Tab or bool(state & Gdk.ModifierType.SHIFT_MASK)
            # A column the user has hidden takes no part in the order; before the
            # register is shown, every field counts.
            shown = widget.get_mapped()
            order = [f for f in self.fields if f is widget or not shown or f.get_mapped()]
            index = order.index(widget)
            step = -1 if backwards else 1
            target = order[(index + step) % len(order)]
            if widget is self.description:
                self.propose()
            target.grab_focus()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            if widget is self.split_button:
                return False  # Enter on the button presses it
            if widget is self.description:
                self.propose()
            self.commit()
            return True
        if keyval == Gdk.KEY_Escape:
            self.clear()
            self.view.set_entry_status("")
            self.date.grab_focus()
            return True
        return False

    # ------------------------------------------------------------- behavior

    def _on_amount_changed(self, entry: Gtk.Entry, other: Gtk.Entry) -> None:
        if self._syncing or not entry.get_text():
            return
        self._syncing = True
        try:
            other.set_text("")
        finally:
            self._syncing = False

    def _on_transfer_changed(self, *_args) -> None:
        if not self._syncing:
            self._transfer_touched = True

    def _on_payee_changed(self, *_args) -> None:
        if not self._syncing:
            self.propose()

    def propose(self) -> EntrySuggestion | None:
        """Fill fields still at their defaults from the latest matching entry.

        Typed input is never overwritten: amounts are filled only while both are
        empty, the transfer only while the user has not chosen one, and the payee
        only while none is chosen.
        """
        view = self.view
        if view.db is None or view.account_handle is None or not self._enabled:
            return None
        description = self.description.get_text()
        payee = self.payee_handle()
        if not description.strip() and payee is None:
            return None
        result = suggest_entry(
            view.db,
            SuggestEntry(description=description, payee=payee, account=view.account_handle),
        )
        suggestion = result.value.suggestion if result.value is not None else None
        if suggestion is None:
            return None
        source = f"{suggestion.when.isoformat()} “{suggestion.description}”"
        if len(suggestion.splits) > 2:
            view.set_entry_status(
                f"{source} has {len(suggestion.splits)} splits; choose Split… to start from it."
            )
            return suggestion
        filled: list[str] = []
        amounts_empty = not (self.increase.get_text().strip() or self.decrease.get_text().strip())
        if (
            not self._transfer_touched
            and suggestion.transfer_account is not None
            and self.select_transfer(suggestion.transfer_account)
        ):
            filled.append(view.db.full_name(suggestion.transfer_account))
        if amounts_empty and suggestion.amount is not None:
            target = self.increase if suggestion.amount > 0 else self.decrease
            self._syncing = True
            try:
                target.set_text(abs(suggestion.amount).format())
            finally:
                self._syncing = False
            filled.append(abs(suggestion.amount).format())
        if payee is None and suggestion.payee is not None:
            handles = [item.handle for item in self.payees]
            if suggestion.payee in handles:
                self._syncing = True
                try:
                    self.payee.set_selected(handles.index(suggestion.payee) + 1)
                finally:
                    self._syncing = False
                filled.append(self.payees[handles.index(suggestion.payee)].name)
        if filled:
            view.set_entry_status(
                f"Proposed from {source}: {', '.join(filled)}. Edit anything, then press Enter."
            )
        return suggestion

    def _fail(self, message: str, widget: Gtk.Widget) -> None:
        self.view.set_entry_status(message, error=True)
        widget.grab_focus()

    def signed_amount(self) -> Money | None:
        """The typed amount as it moves the register account, or None if absent.

        Raises ``ValueError`` for text that is not an amount.
        """
        for entry, sign in ((self.increase, 1), (self.decrease, -1)):
            text = entry.get_text().strip()
            if text:
                try:
                    value = Money(parse_user_amount(text))
                except (ValueError, ArithmeticError) as error:
                    raise ValueError(text) from error
                return value if sign > 0 else -value
        return None

    def commit(self) -> bool:
        """Save the row as one balanced two-split transaction; True on success."""
        view = self.view
        if view.db is None or view.account_handle is None or not self._enabled:
            return False
        current = view.db.get_account(view.account_handle)
        transfer = self.transfer_handle()
        if current is None or current.hidden or transfer is None:
            self._fail("Choose a visible transfer account.", self.transfer)
            return False
        try:
            when = date.fromisoformat(self.date.get_text().strip())
        except ValueError:
            self._fail("Enter the date as YYYY-MM-DD.", self.date)
            return False
        description = self.description.get_text().strip()
        if not description:
            self._fail("Enter a description.", self.description)
            return False
        typed = self._amount_entry()
        text = typed.get_text().strip()
        if not text:
            heading = view.debit_column.get_title()
            other = view.credit_column.get_title()
            self._fail(f"Enter an amount under {heading} or {other}.", typed)
            return False
        try:
            value = Money(parse_user_amount(text))
        except (ValueError, ArithmeticError):
            self._fail("Enter a valid amount.", typed)
            return False
        if value <= 0:
            self._fail("Amount must be greater than zero.", typed)
            return False
        amount = value if typed is self.increase else -value

        currency = transaction_currency(view.db)
        payee = self.payee_handle()
        # The new entry's repaint scrolls to the end, where the blank row is (#157).
        view.scroll_to_end_on_refresh()
        result = save_transaction(
            view.db,
            SaveTransaction(
                TransactionInput(
                    post_date=when,
                    description=description,
                    num=self.num.get_text().strip(),
                    currency=currency,
                    payee=payee,
                    set_payee=payee is not None,
                    splits=(
                        TransactionSplitInput(current.handle, Amount(amount, currency)),
                        TransactionSplitInput(transfer, Amount(-amount, currency)),
                    ),
                )
            ),
        )
        if not result.ok:
            view.cancel_scroll_to_end()
            view.set_entry_status(service_error_message(result.errors[0]), error=True)
            return False
        self.last_date = when
        heading = view.debit_column.get_title() if amount > 0 else view.credit_column.get_title()
        self.clear()
        view.set_entry_status(f"Posted {description}: {abs(amount).format()} {heading}.")
        GLib.idle_add(lambda: self.date.grab_focus() and GLib.SOURCE_REMOVE)
        return True

    def _amount_entry(self) -> Gtk.Entry:
        return self.decrease if self.decrease.get_text().strip() else self.increase

    # ----------------------------------------------------------- full editor

    def open_split_editor(self):
        """Open the full editor prefilled with whatever the row holds."""
        view = self.view
        if view.db is None or view.account_handle is None:
            return None
        from ..dialogs.transaction_dialog import TransactionDialog

        try:
            when = date.fromisoformat(self.date.get_text().strip())
        except ValueError:
            when = self.default_date()
        try:
            amount = self.signed_amount()
        except ValueError:
            amount = None
        dialog = TransactionDialog(
            view.get_root(),
            view.db,
            default_account=view.account_handle,
            default_date=when,
        )
        dialog.prefill(
            description=self.description.get_text().strip(),
            num=self.num.get_text().strip(),
            payee=self.payee_handle(),
            transfer=self.transfer_handle(),
            amount=amount,
        )

        def on_close(*_args) -> bool:
            # Saving in the editor used the row's values, so the row resets;
            # cancelling leaves it exactly as it was.
            if dialog.saved:
                self.clear()
                view.scroll_to_end_on_refresh()
            view.refresh_on_close()
            return False

        dialog.connect("close-request", on_close)
        dialog.present()
        return dialog

    # ------------------------------------------------------ unsaved input

    def confirm_leave(self, proceed: Callable[[], None]) -> None:
        """Run ``proceed`` now, or after Save/Discard when input would be lost."""
        if not self.has_input():
            proceed()
            return

        def answered(choice: int) -> None:
            if choice == SAVE:
                if self.commit():
                    proceed()
            elif choice == DISCARD:
                self.clear()
                self.view.set_entry_status("")
                proceed()

        self.ask_unsaved(answered)

    def ask_unsaved(self, answered: Callable[[int], None]) -> None:
        alert = Gtk.AlertDialog(
            message="Save the transaction you were entering?",
            detail="The blank register row has input that has not been saved.",
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

        alert.choose(self.view.get_root(), None, finished)
