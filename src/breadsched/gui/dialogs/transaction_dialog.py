"""Enter or edit a transaction, in a window of its own.

The window holds the one transaction editor (``widgets/transaction_editor``) that
every place entering or changing a transaction uses, with the window's buttons:
**Make scheduled…** and **Delete…** for a stored transaction, **Cancel**, and
**Save**, which stays unavailable until the splits balance. Saving closes it.
"""

from __future__ import annotations

from datetime import date

from ...gen.lib import Transaction
from ..gi_setup import Gtk
from ..remote_book import OpenBook
from ..widgets.bounded import BoundedWindow
from ..widgets.confirm import delete_button
from ..widgets.help import help_row
from ..widgets.transaction_editor import MORE, TransactionEditor

__all__ = ["MORE", "TransactionDialog"]


class TransactionDialog(BoundedWindow):
    """Create a transaction, or change one that already exists."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: OpenBook,
        default_account: str | None = None,
        default_date: date | None = None,
        transaction: Transaction | None = None,
    ) -> None:
        super().__init__(
            title="Edit transaction" if transaction is not None else "New transaction",
            transient_for=parent,
            modal=True,
        )
        self.db = db
        self.set_default_size(760, 560)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.append(help_row("transactions"))

        self.editor = TransactionEditor(
            db,
            default_account=default_account,
            default_date=default_date,
            transaction=transaction,
            on_saved=lambda _handle: self.close(),
            on_close=self.close,
        )
        self.editor.set_vexpand(True)
        box.append(self.editor)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        if transaction is not None:
            schedule_button = Gtk.Button(label="Make scheduled…")
            schedule_button.set_tooltip_text(
                "Create a new scheduled transaction draft from every split"
            )
            schedule_button.connect("clicked", lambda *_: self.editor.make_scheduled(self))
            buttons.append(schedule_button)
            buttons.append(
                delete_button(f"the transaction {transaction.description}", self._on_delete)
            )
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        self.save_button = Gtk.Button(label="Save")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", lambda *_: self.editor.save())
        buttons.append(self.save_button)
        box.append(buttons)
        self.editor.on_can_save = self.save_button.set_sensitive
        self.editor.revalidate()

    # The editor's state, as openers and tests read it from the window.

    @property
    def saved(self) -> bool:
        return self.editor.saved

    @property
    def transaction(self) -> Transaction | None:
        return self.editor.transaction

    @property
    def status(self) -> Gtk.Label:
        return self.editor.status

    def prefill(self, **values) -> None:
        self.editor.prefill(**values)

    def _on_delete(self) -> None:
        if self.editor.delete():
            self.close()
