"""The register's blank entry row: type a new transaction where it will appear (#158).

The last row of every GTK register is a blank transaction. It is a sentinel, not a
database object: its cells host one persistent set of entry widgets owned by
:class:`BlankEntryRow`, so a value typed there survives the register repainting
around it. Committing builds balancing splits and saves them through the ordinary
``save_transaction`` service; it is not a parallel transaction model. The row holds
a two-split entry directly; "Split" expands it into one editable line per split
beneath it, with an imbalance line that must reach zero before it commits.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
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

__all__ = ["BLANK", "BLANK_PAYLOADS", "BlankEntry", "BlankEntryRow", "ImbalanceLine", "SplitLine"]


class BlankEntry:
    """Marker payload for the register's last, always-editable row."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "BLANK"


BLANK = BlankEntry()


def _amount_entry() -> Gtk.Entry:
    entry = Gtk.Entry(placeholder_text="0.00", xalign=1)
    entry.set_width_chars(9)
    entry.add_css_class("numeric")
    return entry


class SplitLine:
    """One editable split beneath the blank row in split mode.

    Its amounts are in the split's own direction: Increase adds to the line's
    account the way the register's Increase column adds to the register account.
    """

    debit = credit = None
    account_name = ""

    def __init__(self, row: BlankEntryRow) -> None:
        #: The stored split this line edits, or None for a new split.
        self.handle: str | None = None
        self.memo = Gtk.Entry(placeholder_text="Memo")
        self.memo.set_hexpand(True)
        self.account = Gtk.DropDown()
        self.account.set_hexpand(True)
        self.increase = _amount_entry()
        self.decrease = _amount_entry()
        self.fields: list[Gtk.Widget] = [self.memo, self.account, self.increase, self.decrease]
        #: Column title → the widget hosted in that column for this line.
        self.blank_cells: dict[str, Gtk.Widget] = {
            "Description": self.memo,
            "Transfer": self.account,
            "Increase": self.increase,
            "Decrease": self.decrease,
        }
        row.attach_line(self)

    def value_text(self) -> tuple[str, int]:
        """The typed amount text and its sign (+1 increase, -1 decrease)."""
        increase = self.increase.get_text().strip()
        if increase:
            return increase, 1
        return self.decrease.get_text().strip(), -1

    def has_input(self) -> bool:
        return bool(
            self.memo.get_text().strip()
            or self.increase.get_text().strip()
            or self.decrease.get_text().strip()
        )


class ImbalanceLine:
    """The line under the splits that shows how far they are from balancing."""

    debit = credit = None
    account_name = ""

    def __init__(self) -> None:
        self.caption = Gtk.Label(label="Imbalance", xalign=0)
        self.caption.add_css_class("dim")
        self.amount = Gtk.Label(xalign=1)
        self.amount.add_css_class("numeric")
        self.blank_cells: dict[str, Gtk.Widget] = {
            "Description": self.caption,
            "Balance": self.amount,
        }


#: Payloads that belong to the blank row rather than to the ledger.
BLANK_PAYLOADS = (BlankEntry, SplitLine, ImbalanceLine)

#: Answers to the unsaved-input question, in AlertDialog button order.
CANCEL, DISCARD, SAVE = 0, 1, 2


def _label(widget: Gtk.Widget, text: str) -> None:
    widget.update_property([Gtk.AccessibleProperty.LABEL], [text])


class BlankEntryRow:
    """The widgets and behavior of one register's blank entry row."""

    def __init__(self, view, editing=None, split: str | None = None) -> None:
        self.view = view
        #: The stored transaction being edited in place (#158 slice 3), or None for
        #: the blank row that enters new ones; ``edit_split`` is its split shown in
        #: this register.
        self.editing = editing
        self.edit_split = split
        #: Split handles behind the (register, transfer) sides of a two-split edit.
        self._simple_handles: tuple[str | None, str | None] = (split, None)
        self._snapshot: tuple | None = None
        self._noun = "Edited transaction" if editing is not None else "New transaction"
        #: The date to offer next: the last one entered in this register, else today.
        self.last_date: date | None = None
        self.transfers: list = []
        #: Every postable, visible account, for split lines (the register's too).
        self.accounts: list = []
        self.payees: list = []
        self.split_mode = False
        self.lines: list[SplitLine] = []
        self.imbalance = ImbalanceLine()
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
        self.increase = _amount_entry()
        self.decrease = _amount_entry()
        self.split_toggle = Gtk.ToggleButton(label="Split")
        self.split_toggle.set_has_frame(False)
        self.split_toggle.set_tooltip_text("Enter this transaction as several splits, in place")
        self.split_toggle.connect("toggled", self._on_split_toggled)
        self.editor_button = Gtk.Button(icon_name="document-edit-symbolic")
        self.editor_button.set_has_frame(False)
        self.editor_button.set_tooltip_text(
            "Open this entry in the full editor for notes, a claim, or other details"
        )
        self.editor_button.connect("clicked", lambda *_: self.open_split_editor())
        self.actions = Gtk.Box(spacing=2)
        self.actions.append(self.split_toggle)
        self.actions.append(self.editor_button)

        for widget, name in (
            (self.date, "date"),
            (self.num, "number"),
            (self.description, "description"),
            (self.payee, "payee"),
            (self.transfer, "transfer account"),
            (self.split_toggle, "split in place"),
            (self.editor_button, "full editor"),
        ):
            _label(widget, f"{self._noun} {name}")
        for widget in (
            self.date,
            self.num,
            self.description,
            self.payee,
            self.transfer,
            self.increase,
            self.decrease,
            self.split_toggle,
            self.editor_button,
        ):
            self._keys(widget)

        # Typing in one amount clears the other, so the direction is never ambiguous.
        self._pair(self.increase, self.decrease)
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
            "Balance": self.actions,
        }

    @contextmanager
    def _quiet(self) -> Iterator[None]:
        """Change widgets without treating the change as the user's own input."""
        previous = self._syncing
        self._syncing = True
        try:
            yield
        finally:
            self._syncing = previous

    def _keys(self, widget: Gtk.Widget) -> None:
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key, widget)
        widget.add_controller(keys)

    def _pair(self, increase: Gtk.Entry, decrease: Gtk.Entry) -> None:
        increase.connect("changed", self._on_amount_changed, decrease)
        decrease.connect("changed", self._on_amount_changed, increase)

    @property
    def fields(self) -> list[Gtk.Widget]:
        """Tab order, which is also the column order, line by line in split mode."""
        head: list[Gtk.Widget] = [self.date, self.num, self.description, self.payee]
        if self.split_mode:
            body = [widget for line in self.lines for widget in line.fields]
        else:
            body = [self.transfer, self.increase, self.decrease]
        return [*head, *body, self.split_toggle, self.editor_button]

    def rows(self) -> list:
        """The payloads the register shows for the blank row, top to bottom."""
        if not self.split_mode:
            return [BLANK]
        return [BLANK, *self.lines, self.imbalance]

    # ------------------------------------------------------------ split lines

    def attach_line(self, line: SplitLine) -> None:
        number = len(self.lines) + 1
        for widget, name in (
            (line.memo, "memo"),
            (line.account, "account"),
            (line.increase, "increase"),
            (line.decrease, "decrease"),
        ):
            _label(widget, f"New split {number} {name}")
            self._keys(widget)
        self._pair(line.increase, line.decrease)
        for entry in (line.memo, line.increase, line.decrease):
            entry.connect("changed", self._on_line_changed)
        line.account.connect("notify::selected", self._on_line_changed)
        self._fill_accounts(line.account)

    def _fill_accounts(self, picker: Gtk.DropDown, handle: str | None = None) -> None:
        names = Gtk.StringList()
        names.append("(choose account)")
        db = self.view.db
        for account in self.accounts:
            names.append(db.full_name(account) if db is not None else account.name)
        with self._quiet():
            picker.set_model(names)
            handles = [account.handle for account in self.accounts]
            picker.set_selected(handles.index(handle) + 1 if handle in handles else 0)

    @staticmethod
    def _line_handle(row: BlankEntryRow, line: SplitLine) -> str | None:
        index = line.account.get_selected() - 1
        if 0 <= index < len(row.accounts):
            return row.accounts[index].handle
        return None

    def line_account(self, line: SplitLine) -> str | None:
        return self._line_handle(self, line)

    def _new_line(
        self,
        account: str | None = None,
        value: Money | None = None,
        memo: str = "",
        handle: str | None = None,
    ):
        line = SplitLine(self)
        line.handle = handle
        self.lines.append(line)
        with self._quiet():
            if account is not None:
                self._fill_accounts(line.account, account)
            line.memo.set_text(memo)
            if value is not None and value != 0:
                target = line.increase if value > 0 else line.decrease
                target.set_text(abs(value).format())
        return line

    def _on_line_changed(self, *_args) -> None:
        if self._syncing:
            return
        # A trailing empty line is always ready for the next split.
        if self.lines and (
            self.lines[-1].has_input() or self.line_account(self.lines[-1]) is not None
        ):
            self._new_line()
            self.view.update_blank_rows(self)
        self.update_imbalance()

    def line_value(self, line: SplitLine) -> Money | None:
        """A line's signed value; None when blank. Raises ``ValueError`` if unreadable."""
        text, sign = line.value_text()
        if not text:
            return None
        try:
            value = Money(parse_user_amount(text))
        except (ValueError, ArithmeticError) as error:
            raise ValueError(text) from error
        return value if sign > 0 else -value

    def residual(self) -> Money:
        total = Money(0)
        for line in self.lines:
            try:
                value = self.line_value(line)
            except ValueError:
                continue
            if value is not None:
                total = total + value
        return total

    def update_imbalance(self) -> None:
        residual = self.residual()
        self.imbalance.amount.set_text(
            residual.format(parens_negative=True) if residual else "Balanced"
        )
        if residual:
            self.imbalance.amount.add_css_class("negative")
        else:
            self.imbalance.amount.remove_css_class("negative")

    def set_split_mode(self, split: bool, *, lines=None) -> bool:
        """Expand into split lines, or collapse back; False when that would lose splits.

        Expanding carries the row's amount and transfer into the first two lines
        unless ``lines`` gives (account, value, memo) triples to start from.
        Collapsing is only possible while at most two lines hold a split.
        """
        if split == self.split_mode:
            return True
        view = self.view
        if split:
            self.lines = []
            if lines is None:
                try:
                    amount = self.signed_amount()
                except ValueError:
                    amount = None
                mine_handle, other_handle = (
                    self._simple_handles if self.editing is not None else (None, None)
                )
                lines = [
                    (view.account_handle, amount, self._memo_of(mine_handle), mine_handle),
                    (
                        self.transfer_handle(),
                        -amount if amount is not None else None,
                        self._memo_of(other_handle),
                        other_handle,
                    ),
                ]
            for account, value, memo, *handle in lines:
                self._new_line(account, value, memo, handle[0] if handle else None)
            self._new_line()
        else:
            filled = [
                line
                for line in self.lines
                if line.has_input() or self.line_account(line) is not None
            ]
            if len(filled) > 2:
                view.set_entry_status(
                    "Remove splits until two remain, or use the full editor.", error=True
                )
                self._set_toggle(True)
                return False
            mine = next(
                (line for line in filled if self.line_account(line) == view.account_handle),
                None,
            )
            if filled and mine is None:
                view.set_entry_status(
                    "Put one split in this register's account before leaving split mode.",
                    error=True,
                )
                self._set_toggle(True)
                return False
            other = next((line for line in filled if line is not mine), None)
            try:
                amount = self.line_value(mine) if mine is not None else None
                if amount is None and other is not None:
                    other_value = self.line_value(other)
                    amount = -other_value if other_value is not None else None
            except ValueError:
                amount = None
            with self._quiet():
                for entry in (self.increase, self.decrease):
                    entry.set_text("")
                if amount:
                    target = self.increase if amount > 0 else self.decrease
                    target.set_text(abs(amount).format())
            self._simple_handles = (
                mine.handle if mine is not None else None,
                other.handle if other is not None else None,
            )
            other_account = self.line_account(other) if other is not None else None
            if other_account is not None:
                self.select_transfer(other_account)
                self._transfer_touched = True
            self.lines = []
        self.split_mode = split
        self._set_toggle(split)
        for widget in (self.transfer, self.increase, self.decrease):
            widget.set_visible(not split)
        self.update_imbalance()
        view.update_blank_rows(self)
        return True

    def _set_toggle(self, active: bool) -> None:
        with self._quiet():
            self.split_toggle.set_active(active)

    def _on_split_toggled(self, toggle: Gtk.ToggleButton) -> None:
        if self._syncing:
            return
        if self.set_split_mode(toggle.get_active()) and toggle.get_active() and self.lines:
            self.lines[0].memo.grab_focus()

    # ----------------------------------------------------------------- state

    def default_date(self) -> date:
        return self.last_date or date.today()

    def has_input(self) -> bool:
        """Whether anything was typed that leaving would lose."""
        if self.editing is not None:
            return self._state() != self._snapshot
        typed = (self.num, self.description, self.increase, self.decrease)
        return (
            any(entry.get_text().strip() for entry in typed)
            or self.payee.get_selected() > 0
            or any(line.has_input() for line in self.lines)
        )

    def clear(self) -> None:
        """Reset every field to its default, keeping the date last entered.

        An in-place edit has no defaults to return to: clearing it ends the edit.
        """
        if self.editing is not None:
            self.view.stop_editing()
            return
        if self.split_mode:
            self.lines = []
            self.set_split_mode(False)
        with self._quiet():
            self.date.set_text(self.default_date().isoformat())
            for entry in (self.num, self.description, self.increase, self.decrease):
                entry.set_text("")
            self.payee.set_selected(0)
            if self.transfers:
                self.transfer.set_selected(0)
        self._transfer_touched = False

    def set_headings(self, increase: str, decrease: str) -> None:
        _label(self.increase, f"{self._noun} {increase.lower()}")
        _label(self.decrease, f"{self._noun} {decrease.lower()}")

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
        with self._quiet():
            self.transfers = sorted(
                (
                    account
                    for account in db.iter_accounts()
                    if account.handle != account_handle
                    and not account.is_root
                    and not account.placeholder
                    and (not account.hidden or account.handle in self.referenced)
                ),
                key=db.full_name,
            )
            names = Gtk.StringList()
            for account in self.transfers:
                names.append(db.full_name(account))
            self.transfer.set_model(names)
            chosen_lines = [self.line_account(line) for line in self.lines]
            self.accounts = sorted(
                (
                    account
                    for account in db.iter_accounts()
                    if not account.is_root
                    and not account.placeholder
                    and (not account.hidden or account.handle in self.referenced)
                ),
                key=db.full_name,
            )
            for line, handle in zip(self.lines, chosen_lines, strict=True):
                self._fill_accounts(line.account, handle)
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
        if chosen_transfer not in [account.handle for account in self.transfers]:
            self._transfer_touched = False

    @property
    def referenced(self) -> set[str]:
        """Accounts the edited transaction already uses, offered even if hidden."""
        if self.editing is None:
            return set()
        return {split.account for split in self.editing.splits}

    def _source_split(self, handle: str | None):
        if self.editing is None or handle is None:
            return None
        return next((split for split in self.editing.splits if split.handle == handle), None)

    def _memo_of(self, handle: str | None) -> str:
        split = self._source_split(handle)
        return split.memo if split is not None else ""

    def _state(self) -> tuple:
        return (
            self.date.get_text().strip(),
            self.num.get_text().strip(),
            self.description.get_text().strip(),
            self.payee_handle(),
            self.split_mode,
            None if self.split_mode else self.transfer_handle(),
            None if self.split_mode else self.increase.get_text().strip(),
            None if self.split_mode else self.decrease.get_text().strip(),
            tuple(
                (
                    line.handle,
                    self.line_account(line),
                    line.memo.get_text().strip(),
                    line.increase.get_text().strip(),
                    line.decrease.get_text().strip(),
                )
                for line in self.lines
                if line.has_input() or self.line_account(line) is not None
            ),
        )

    def load(self) -> None:
        """Fill the fields from the transaction being edited in place."""
        transaction = self.editing
        if transaction is None:
            return
        mine = self._source_split(self.edit_split)
        others = [split for split in transaction.splits if split is not mine]
        with self._quiet():
            self.date.set_text(transaction.post_date.isoformat())
            self.num.set_text(transaction.num)
            self.description.set_text(transaction.description)
            handles = [payee.handle for payee in self.payees]
            self.payee.set_selected(
                handles.index(transaction.payee) + 1 if transaction.payee in handles else 0
            )
            simple = (
                mine is not None
                and len(others) == 1
                and others[0].account != mine.account
                and self.select_transfer(others[0].account)
            )
            if simple:
                assert mine is not None
                self._simple_handles = (mine.handle, others[0].handle)
                target = self.increase if mine.value > 0 else self.decrease
                target.set_text(abs(mine.value).format())
                self._transfer_touched = True
            else:
                ordered = ([mine] if mine is not None else []) + others
                self.set_split_mode(
                    True,
                    lines=[
                        (split.account, split.value, split.memo, split.handle) for split in ordered
                    ],
                )
        self.update_imbalance()
        self._snapshot = self._state()

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
        with self._quiet():
            self.transfer.set_selected(handles.index(handle))
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
            if widget in (self.split_toggle, self.editor_button):
                return False  # Enter on a button presses it
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
        with self._quiet():
            other.set_text("")

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
        if self.editing is not None:
            return None  # a stored transaction is never proposed over
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
        filled: list[str] = []
        if self.split_mode or len(suggestion.splits) > 2:
            # Split lines take the whole proposal, but only while none is typed in.
            lines_empty = not any(line.has_input() for line in self.lines)
            main_empty = not (self.increase.get_text().strip() or self.decrease.get_text().strip())
            if lines_empty and main_empty and not self._transfer_touched:
                if self.split_mode:
                    self.lines = []
                    self.split_mode = False
                self.set_split_mode(
                    True,
                    lines=[(split.account, split.value, split.memo) for split in suggestion.splits],
                )
                filled.append(f"{len(suggestion.splits)} splits")
            self._propose_payee(suggestion, payee, filled)
            if filled:
                view.set_entry_status(
                    f"Proposed from {source}: {', '.join(filled)}. Edit anything, then press Enter."
                )
            return suggestion
        amounts_empty = not (self.increase.get_text().strip() or self.decrease.get_text().strip())
        if (
            not self._transfer_touched
            and suggestion.transfer_account is not None
            and self.select_transfer(suggestion.transfer_account)
        ):
            filled.append(view.db.full_name(suggestion.transfer_account))
        if amounts_empty and suggestion.amount is not None:
            target = self.increase if suggestion.amount > 0 else self.decrease
            with self._quiet():
                target.set_text(abs(suggestion.amount).format())
            filled.append(abs(suggestion.amount).format())
        self._propose_payee(suggestion, payee, filled)
        if filled:
            view.set_entry_status(
                f"Proposed from {source}: {', '.join(filled)}. Edit anything, then press Enter."
            )
        return suggestion

    def _propose_payee(self, suggestion, chosen: str | None, filled: list[str]) -> None:
        if chosen is not None or suggestion.payee is None:
            return
        handles = [item.handle for item in self.payees]
        if suggestion.payee in handles:
            with self._quiet():
                self.payee.set_selected(handles.index(suggestion.payee) + 1)
            filled.append(self.payees[handles.index(suggestion.payee)].name)

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
        """Save the row as one balanced transaction; True on success."""
        view = self.view
        if view.db is None or view.account_handle is None or not self._enabled:
            return False
        current = view.db.get_account(view.account_handle)
        if current is None or (current.hidden and self.editing is None):
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
        currency = transaction_currency(
            view.db, self.editing.currency if self.editing is not None else None
        )
        if self.split_mode:
            splits = self._line_splits(currency)
            if splits is None:
                return False
            amount = sum(
                (split.value.value for split in splits if split.account == current.handle),
                Money(0),
            )
        else:
            two = self._two_splits(current.handle, currency)
            if two is None:
                return False
            splits, amount = two
        return self._save(when, description, currency, splits, amount)

    def _line_splits(self, currency: str) -> tuple[TransactionSplitInput, ...] | None:
        """Split inputs from the lines, or None after reporting what is wrong."""
        view = self.view
        splits: list[TransactionSplitInput] = []
        for line in self.lines:
            account = self.line_account(line)
            try:
                value = self.line_value(line)
            except ValueError:
                self._fail("Enter a valid amount.", line.increase)
                return None
            if value is None and account is None:
                if line.memo.get_text().strip():
                    self._fail("Choose an account for this split.", line.account)
                    return None
                continue
            if account is None:
                self._fail("Choose an account for this split.", line.account)
                return None
            if value is None or value == 0:
                self._fail("Enter an amount for this split.", line.increase)
                return None
            splits.append(
                self._split_input(account, value, currency, line.handle, line.memo.get_text())
            )
        if len(splits) < 2:
            self._fail("A transaction needs at least two splits.", self.lines[0].account)
            return None
        residual = self.residual()
        if residual:
            self._fail(
                f"The splits are out of balance by {residual.format(parens_negative=True)}.",
                self.lines[-1].memo,
            )
            return None
        if not any(split.account == view.account_handle for split in splits):
            self._fail("One split must be in this register's account.", self.lines[0].account)
            return None
        return tuple(splits)

    def _two_splits(self, handle: str, currency: str):
        """The two splits of an ordinary entry and its signed amount, or None."""
        view = self.view
        transfer = self.transfer_handle()
        if transfer is None:
            self._fail("Choose a visible transfer account.", self.transfer)
            return None
        typed = self._amount_entry()
        text = typed.get_text().strip()
        if not text:
            heading = view.debit_column.get_title()
            other = view.credit_column.get_title()
            self._fail(f"Enter an amount under {heading} or {other}.", typed)
            return None
        try:
            value = Money(parse_user_amount(text))
        except (ValueError, ArithmeticError):
            self._fail("Enter a valid amount.", typed)
            return None
        if value <= 0:
            self._fail("Amount must be greater than zero.", typed)
            return None
        amount = value if typed is self.increase else -value
        mine, other = self._simple_handles if self.editing is not None else (None, None)
        return (
            self._split_input(handle, amount, currency, mine, self._memo_of(mine)),
            self._split_input(transfer, -amount, currency, other, self._memo_of(other)),
        ), amount

    def _split_input(
        self, account: str, value: Money, currency: str, handle: str | None, memo: str
    ) -> TransactionSplitInput:
        """One split input, keeping what the row does not show from its stored split."""
        source = self._source_split(handle)
        return TransactionSplitInput(
            account,
            Amount(value, currency),
            handle=handle if source is not None else None,
            memo=memo.strip(),
            planning_flow=source.planning_flow if source is not None else None,
            investment_activity=source.investment_activity if source is not None else None,
        )

    def _save(self, when: date, description: str, currency: str, splits, amount: Money) -> bool:
        view = self.view
        payee = self.payee_handle()
        editing = self.editing
        if editing is None:
            # The new entry's repaint scrolls to the end, where the blank row is (#157).
            view.scroll_to_end_on_refresh()
        result = save_transaction(
            view.db,
            SaveTransaction(
                TransactionInput(
                    post_date=when,
                    description=description,
                    num=self.num.get_text().strip(),
                    # Notes are not shown in the row, so an edit keeps them.
                    notes=editing.notes if editing is not None else "",
                    currency=currency,
                    payee=payee,
                    # An edit shows the payee, so choosing none clears it.
                    set_payee=payee is not None or editing is not None,
                    splits=tuple(splits),
                ),
                existing_handle=editing.handle if editing is not None else None,
                source=editing,
            ),
        )
        if not result.ok:
            view.cancel_scroll_to_end()
            view.set_entry_status(service_error_message(result.errors[0]), error=True)
            return False
        if editing is not None:
            view.set_entry_status(f"Saved changes to {description}.")
            view.stop_editing()
            return True
        self.last_date = when
        heading = view.debit_column.get_title() if amount > 0 else view.credit_column.get_title()
        self.clear()
        view.set_entry_status(
            f"Posted {description}: {abs(amount).format()} {heading}."
            if amount
            else f"Posted {description}."
        )
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
        if self.editing is not None:
            # An in-place edit hands over to the full editor for the same transaction.
            transaction = self.editing
            self.confirm_leave(lambda: view.edit_transaction(transaction))
            return None
        from ..dialogs.transaction_dialog import TransactionDialog

        try:
            when = date.fromisoformat(self.date.get_text().strip())
        except ValueError:
            when = self.default_date()
        try:
            amount = None if self.split_mode else self.signed_amount()
        except ValueError:
            amount = None
        dialog = TransactionDialog(
            view.get_root(),
            view.db,
            default_account=view.account_handle,
            default_date=when,
        )
        lines = None
        if self.split_mode:
            lines = []
            for line in self.lines:
                account = self.line_account(line)
                try:
                    value = self.line_value(line)
                except ValueError:
                    value = None
                if account is not None or value is not None:
                    lines.append((account, value, line.memo.get_text().strip()))
        dialog.prefill(
            description=self.description.get_text().strip(),
            num=self.num.get_text().strip(),
            payee=self.payee_handle(),
            transfer=self.transfer_handle(),
            amount=amount,
            splits=lines,
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
        editing = self.editing is not None
        alert = Gtk.AlertDialog(
            message=(
                "Save your changes to this transaction?"
                if editing
                else "Save the transaction you were entering?"
            ),
            detail=(
                "The transaction being edited in the register has unsaved changes."
                if editing
                else "The blank register row has input that has not been saved."
            ),
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
