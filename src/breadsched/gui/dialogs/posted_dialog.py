"""The transactions a due review just posted, to check and correct in the editor.

After Apply posts scheduled transactions, they are shown here before anything else
happens (#328), as GnuCash shows a since-last-run batch. The list on the left names
each one (date, description, amount); the one chosen is open on the right in the
same transaction editor as everywhere else (``widgets/transaction_editor``):
its Entry page, its splits on the register's grid, and its Tags & documents.
**Save** stores a correction and **Delete…** removes the transaction, each through
the shared transaction service as an ordinary, undoable change. The list rereads
the book after every change, so it shows what is stored, and a transaction
deleted elsewhere simply drops out.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import partial

from ...api import ApiError
from ...api.local import book_server
from ...gen.lib.money import Money
from ...gen.lib.transaction import Transaction
from ..gi_setup import Gtk
from ..remote_book import OpenBook
from ..widgets.bounded import BoundedWindow, action_row
from ..widgets.confirm import delete_button
from ..widgets.transaction_editor import TransactionEditor

__all__ = ["PostedTransactionsDialog"]


def _amount(transaction: Transaction) -> Money:
    """What a transaction moves: the sum of its money-in splits."""
    return sum((split.value for split in transaction.splits if split.value > 0), Money(0))


class PostedTransactionsDialog(BoundedWindow):
    """The transactions just posted, each opened in the transaction editor."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: OpenBook,
        handles: Iterable[str],
        *,
        skipped: int = 0,
    ) -> None:
        super().__init__(title="Posted transactions", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.db = db
        self.handles = list(handles)
        self.set_default_size(1040, 600)

        box = self.frame("due-review")

        done = f" and marked {skipped} as done" if skipped else ""
        self.heading = Gtk.Label(xalign=0, wrap=True)
        self.heading.set_text(
            f"Posted {len(self.handles)} scheduled transaction(s){done}. Check each one; "
            "correct and save, or delete, any that are not right. Undo with Ctrl+Z also works."
        )
        box.append(self.heading)

        body = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True)
        body.set_shrink_start_child(False)
        body.set_resize_start_child(False)
        self.listing = Gtk.ListBox()
        self.listing.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listing.add_css_class("navigation-sidebar")
        self.listing.connect("row-selected", self._on_row_selected)
        listing_scroll = Gtk.ScrolledWindow(child=self.listing)
        listing_scroll.set_size_request(260, -1)
        listing_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body.set_start_child(listing_scroll)
        self.side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.side.set_margin_start(12)
        body.set_end_child(self.side)
        box.append(body)

        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.add_css_class("dim")
        box.append(self.status)

        self.actions(box)

        #: The editor of the transaction chosen in the list, and its window buttons.
        self.editor: TransactionEditor | None = None
        self.save_button: Gtk.Button | None = None
        #: The listed transactions' handles, in list order.
        self.listed: list[str] = []
        self.refresh()

    # ----------------------------------------------------------------- listing

    def transactions(self) -> list[Transaction]:
        """The posted transactions still in the book, in date order."""
        found = []
        for handle in self.handles:
            try:
                found.append(book_server(self.db).find_transaction(handle))
            except ApiError:
                continue  # deleted since it was posted
        return sorted(found, key=lambda txn: (txn.post_date, txn.description))

    def refresh(self, chosen: str | None = None) -> None:
        """List the posted transactions as stored now, and open ``chosen`` (else the
        one open before, else the first)."""
        keep = chosen or (
            self.editor.transaction.handle if self.editor and self.editor.transaction else None
        )
        self.listing.unselect_all()
        while (child := self.listing.get_first_child()) is not None:
            self.listing.remove(child)
        listed = self.transactions()
        self.listed = [transaction.handle for transaction in listed]
        for transaction in listed:
            row = Gtk.ListBoxRow()
            row.handle = transaction.handle
            line = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            for side in ("top", "bottom", "start", "end"):
                getattr(line, f"set_margin_{side}")(6)
            title = Gtk.Label(label=transaction.description or "(no description)", xalign=0)
            title.set_wrap(True)
            line.append(title)
            detail = Gtk.Label(
                label=f"{transaction.post_date.isoformat()}   {_amount(transaction).format()}",
                xalign=0,
            )
            detail.add_css_class("dim")
            detail.add_css_class("numeric")
            line.append(detail)
            row.set_child(line)
            self.listing.append(row)
        if not listed:
            self._show(None)
            return
        target = keep if keep in self.listed else self.listed[0]
        self.select(target)

    def select(self, handle: str) -> TransactionEditor | None:
        """Open the posted transaction ``handle`` in the editor."""
        if handle not in self.listed:
            return None
        row = self.listing.get_row_at_index(self.listed.index(handle))
        self.listing.select_row(row)
        return self.editor

    def _on_row_selected(self, _listing, row) -> None:
        if row is None:
            return
        if self.editor is not None and self.editor.transaction is not None:
            if self.editor.transaction.handle == row.handle:
                return
        try:
            transaction = book_server(self.db).find_transaction(row.handle)
        except ApiError:
            self.refresh()
            return
        self._show(transaction)

    def _show(self, transaction: Transaction | None) -> None:
        while (child := self.side.get_first_child()) is not None:
            self.side.remove(child)
        self.editor, self.save_button = None, None
        if transaction is None:
            self.side.append(Gtk.Label(label="None of the posted transactions remain.", xalign=0))
            return
        editor = TransactionEditor(
            self.db, transaction=transaction, on_saved=self._after_save, on_close=self.close
        )
        editor.set_vexpand(True)
        self.side.append(editor)
        delete = delete_button(
            f"the transaction {transaction.description}",
            partial(self.delete, transaction.handle),
        )
        delete.set_tooltip_text("Remove this transaction; Ctrl+Z restores it")
        save = Gtk.Button(label="Save")
        save.connect("clicked", lambda *_: editor.save())
        # The editor's own row, under it; the window's row only closes.
        self.side.append(action_row(destructive=delete, primary=save))
        editor.on_can_save = save.set_sensitive
        editor.revalidate()
        self.editor, self.save_button = editor, save

    # ----------------------------------------------------------------- changes

    def _after_save(self, handle: str) -> None:
        self.status.remove_css_class("negative")
        try:
            saved = book_server(self.db).find_transaction(handle)
            self.status.set_text(f"Saved {saved.description}. Ctrl+Z undoes it.")
        except ApiError:
            self.status.set_text("Saved.")
        self.editor = None  # reopen it as stored
        self.refresh(chosen=handle)

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
        following = None
        if handle in self.listed:
            index = self.listed.index(handle)
            rest = [h for h in self.listed if h != handle]
            following = rest[min(index, len(rest) - 1)] if rest else None
        self.editor = None
        self.refresh(chosen=following)
        return True
