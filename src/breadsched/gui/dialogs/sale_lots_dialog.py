"""Choose which lots one sale of shares sells (specific identification)."""

from __future__ import annotations

from collections.abc import Callable

from ...gen.db.sqlite import DbSQLite
from ...gen.engine.cost_basis import shares_text
from ...gen.services import SaleLots, SetSaleLots, sale_lots, set_sale_lots
from ...presentation import lot_text, sale_text, service_error_message
from ..gi_setup import Gtk
from ..widgets.bounded import BoundedWindow, scroll_body
from ..widgets.help import help_row

__all__ = ["SaleLotsDialog"]

_METHODS = {"fifo": "oldest shares first", "average": "average cost"}


class SaleLotsDialog(BoundedWindow):
    """The lots held before a sale, with the shares to take from each."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: DbSQLite,
        transaction: str,
        split: str,
        on_saved: Callable[[SaleLots], None] | None = None,
    ) -> None:
        super().__init__(title="Choose Lots Sold", transient_for=parent, modal=True)
        self.db = db
        self.transaction = transaction
        self.split = split
        self.on_saved = on_saved
        self.set_default_size(620, 420)
        current = sale_lots(db, transaction, split).value
        if current is None:
            raise ValueError("not a sale of shares")
        self.current = current

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(14)
        self.set_child(outer)
        outer.append(help_row("specific-lots"))
        heading = Gtk.Label(
            label=f"{db.full_name(current.account)}: {sale_text(current.sale)}",
            xalign=0,
            wrap=True,
        )
        heading.add_css_class("heading")
        outer.append(heading)
        method = _METHODS.get(current.method, current.method)
        note = Gtk.Label(
            label=(
                f"Enter the shares to sell from each lot. Shares you do not assign are "
                f"sold by the account's method ({method})."
            ),
            xalign=0,
            wrap=True,
        )
        note.add_css_class("dim")
        outer.append(note)

        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        outer.append(grid)
        for column, title in enumerate(("Lot", "Shares to sell")):
            label = Gtk.Label(label=title, xalign=0)
            label.add_css_class("dim")
            grid.attach(label, column, 0, 1, 1)
        chosen = {pick.lot: shares_text(pick.quantity) for pick in current.picks}
        self.entries: dict[str, Gtk.Entry] = {}
        for row, lot in enumerate(current.offered, start=1):
            grid.attach(Gtk.Label(label=lot_text(lot), xalign=0), 0, row, 1, 1)
            entry = Gtk.Entry(text=chosen.get(lot.transaction, ""), width_chars=10)
            entry.set_placeholder_text("0")
            entry.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [f"Shares to sell from the lot bought {lot.acquired.isoformat()}"],
            )
            grid.attach(entry, 1, row, 1, 1)
            self.entries[lot.transaction] = entry
        if not current.offered:
            grid.attach(
                Gtk.Label(label="No lots were held before this sale.", xalign=0), 0, 1, 2, 1
            )

        self.message = Gtk.Label(xalign=0, wrap=True)
        self.message.add_css_class("negative")
        outer.append(self.message)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        self.method_button = Gtk.Button(label="Use the Account's Method")
        self.method_button.connect("clicked", lambda *_: self.use_method())
        self.save_button = Gtk.Button(label="Save")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", lambda *_: self.save())
        for button in (cancel, self.method_button, self.save_button):
            buttons.append(button)
        outer.append(buttons)
        scroll_body(self)

    def _store(self, picks: tuple[tuple[str, str], ...]) -> bool:
        result = set_sale_lots(self.db, SetSaleLots(self.transaction, self.split, picks))
        if result.value is None:
            self.message.set_text(
                "\n".join(service_error_message(error) for error in result.errors)
            )
            return False
        self.current = result.value
        if self.on_saved is not None:
            self.on_saved(result.value)
        self.close()
        return True

    def save(self) -> bool:
        """Store the shares entered per lot; empty or zero entries name no shares."""
        picks = tuple(
            (lot, text)
            for lot, entry in self.entries.items()
            if (text := entry.get_text().strip()) and text not in {"0", "0.0", "0.00"}
        )
        return self._store(picks)

    def use_method(self) -> bool:
        """Sell by the account's method again."""
        return self._store(())
