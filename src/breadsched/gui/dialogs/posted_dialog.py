"""The transactions a due review just posted, split by split, to check and correct.

After Apply posts scheduled transactions, they are shown here before anything else
happens (#328), as GnuCash shows a since-last-run batch. Each transaction is listed
with every split on its own line (account, memo, and the amount under that
account's own heading), because a single transfer column cannot describe
transactions from many accounts at once.

**Edit…** opens the transaction in the full editor, and **Delete** removes it
through the shared transaction service; either is an ordinary, undoable change.
The list rereads the book after every change, so it shows what is stored, and a
transaction deleted elsewhere simply drops out.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import partial

from ...api import ApiError
from ...api.local import book_server
from ...gen.db.sqlite import DbSQLite
from ...gen.engine import ledger
from ...gen.lib.transaction import Transaction
from ..gi_setup import Gtk
from ..widgets.bounded import BoundedWindow, scroll_body
from ..widgets.confirm import delete_button
from ..widgets.help import help_row

__all__ = ["PostedTransactionsDialog", "split_lines"]


def split_lines(db: DbSQLite, transaction: Transaction) -> list[tuple[str, str, str, str]]:
    """Each split as (account, memo, heading, amount), money in before money out.

    The heading is the split account's own register heading for its direction
    (Deposit, Expense, Withdrawal, …), so the line reads as that register would.
    """
    lines = []
    ordered = sorted(transaction.splits, key=lambda split: split.value < 0)
    for split in ordered:
        account = db.get_account(split.account)
        increase, decrease = (
            ledger.register_headings(account.atype) if account is not None else ("In", "Out")
        )
        heading = increase if split.value >= 0 else decrease
        lines.append(
            (
                db.full_name(split.account),
                split.memo,
                heading,
                abs(split.value).format(),
            )
        )
    return lines


class PostedTransactionsDialog(BoundedWindow):
    """The transactions just posted, each with its splits, an editor, and Delete."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: DbSQLite,
        handles: Iterable[str],
        *,
        skipped: int = 0,
    ) -> None:
        super().__init__(title="Posted transactions", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.db = db
        self.handles = list(handles)
        self.set_default_size(760, 480)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.append(help_row("due-review"))

        done = f" and marked {skipped} as done" if skipped else ""
        self.heading = Gtk.Label(xalign=0, wrap=True)
        self.heading.set_text(
            f"Posted {len(self.handles)} scheduled transaction(s){done}. Check each one; "
            "edit or delete any that are not right. Undo with Ctrl+Z also works."
        )
        box.append(self.heading)

        self.journal = Gtk.Grid(column_spacing=14, row_spacing=4)
        box.append(self.journal)

        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.add_css_class("dim")
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        close = Gtk.Button(label="Close")
        close.add_css_class("suggested-action")
        close.connect("clicked", lambda *_: self.close())
        buttons.append(close)
        box.append(buttons)
        scroll_body(self)

        #: Each listed transaction's Edit and Delete buttons, by handle (for tests).
        self.edit_buttons: dict[str, Gtk.Button] = {}
        self.delete_buttons: dict[str, Gtk.Button] = {}
        self.refresh()

    # ----------------------------------------------------------------- listing

    def transactions(self) -> list[Transaction]:
        """The posted transactions still in the book, in date order."""
        found = [self.db.get_transaction(handle) for handle in self.handles]
        return sorted(
            (txn for txn in found if txn is not None),
            key=lambda txn: (txn.post_date, txn.description),
        )

    def refresh(self) -> None:
        """Show the posted transactions as they are stored now."""
        while (child := self.journal.get_first_child()) is not None:
            self.journal.remove(child)
        self.edit_buttons, self.delete_buttons = {}, {}
        for position, heading in enumerate(("Date", "Account", "Memo", "", "Amount")):
            label = Gtk.Label(label=heading, xalign=1 if position == 4 else 0)
            label.add_css_class("summary-label")
            self.journal.attach(label, position, 0, 1, 1)
        row = 1
        listed = self.transactions()
        for transaction in listed:
            date_label = Gtk.Label(label=transaction.post_date.isoformat(), xalign=0)
            self.journal.attach(date_label, 0, row, 1, 1)
            title = Gtk.Label(label=transaction.description or "(no description)", xalign=0)
            title.set_wrap(True)
            title.add_css_class("summary-label")
            self.journal.attach(title, 1, row, 2, 1)
            actions = Gtk.Box(spacing=6)
            edit = Gtk.Button(label="Edit…")
            edit.set_tooltip_text("Open this transaction in the full editor")
            edit.connect("clicked", lambda _b, handle=transaction.handle: self.edit(handle))
            actions.append(edit)
            delete = delete_button(
                f"the transaction {transaction.description}",
                partial(self.delete, transaction.handle),
            )
            delete.set_tooltip_text("Remove this transaction; Ctrl+Z restores it")
            actions.append(delete)
            self.journal.attach(actions, 3, row, 2, 1)
            self.edit_buttons[transaction.handle] = edit
            self.delete_buttons[transaction.handle] = delete
            row += 1
            for account, memo, heading, amount in split_lines(self.db, transaction):
                name = Gtk.Label(label=account, xalign=0)
                name.set_margin_start(18)
                self.journal.attach(name, 1, row, 1, 1)
                self.journal.attach(Gtk.Label(label=memo, xalign=0), 2, row, 1, 1)
                self.journal.attach(Gtk.Label(label=heading, xalign=0), 3, row, 1, 1)
                value = Gtk.Label(label=amount, xalign=1)
                value.add_css_class("numeric")
                self.journal.attach(value, 4, row, 1, 1)
                row += 1
        if not listed:
            self.journal.attach(
                Gtk.Label(label="None of the posted transactions remain.", xalign=0),
                0,
                row,
                5,
                1,
            )

    # ----------------------------------------------------------------- changes

    def edit(self, handle: str) -> Gtk.Window | None:
        """Open ``handle`` in the full editor; the list rereads it when that closes."""
        transaction = self.db.get_transaction(handle)
        if transaction is None:
            self.refresh()
            return None
        from .transaction_dialog import TransactionDialog

        editor = TransactionDialog(self, self.db, transaction=transaction)
        editor.connect("close-request", self._after_edit)
        editor.present()
        return editor

    def _after_edit(self, *_args) -> bool:
        self.refresh()
        self.status.set_text("")
        return False  # let the editor close

    def delete(self, handle: str) -> bool:
        """Delete ``handle`` through the book server; False with the reason if refused."""
        try:
            deleted = book_server(self.db).delete_transaction(handle)
        except ApiError as error:
            self.status.set_text(str(error))
            self.status.add_css_class("negative")
            return False
        self.status.remove_css_class("negative")
        self.status.set_text(f"Deleted {deleted.description}. Ctrl+Z restores it.")
        self.refresh()
        return True
