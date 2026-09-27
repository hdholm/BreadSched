"""Manual dated exchange rate, entered exactly as the web and CLI accept it.

The rate is directional: target-currency units per one source-currency unit. The
shared valuation contract validates the pair and value and writes a BreadSched-
owned quote; imported quotes and ledger amounts are never changed. Accounts then
show the selected quote's date and source, or a missing-quote warning.
"""

from __future__ import annotations

from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.engine import valuation
from ...gen.engine.currency import reporting_currency_handle
from ...gen.lib.commodity import CommodityPrice
from ...gen.lib.money import Money
from ...gen.utils.amount_input import parse_user_amount
from ..gi_setup import Gtk

__all__ = ["ExchangeRateDialog"]


class ExchangeRateDialog(Gtk.Window):
    """Choose two currencies, a date, and an exact rate."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite) -> None:
        super().__init__(title="Exchange rate", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.db = db
        self.currencies = sorted(
            (item for item in db.iter_commodities() if item.is_currency),
            key=lambda item: item.mnemonic,
        )
        self.saved: CommodityPrice | None = None
        self.set_default_size(520, 320)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        explanation = Gtk.Label(
            label=(
                "Enter target-currency units per one source-currency unit. The dated "
                "manual quote leaves imported quotes and ledger amounts intact. Accounts "
                "show the selected quote date and source, or a missing-quote warning."
            ),
            xalign=0,
            wrap=True,
        )
        explanation.add_css_class("dim")
        box.append(explanation)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        box.append(grid)
        labels = [f"{item.mnemonic} — {item.fullname}" for item in self.currencies]

        self.source_picker = Gtk.DropDown.new_from_strings(labels)
        grid.attach(Gtk.Label(label="From currency", xalign=0), 0, 0, 1, 1)
        grid.attach(self.source_picker, 1, 0, 1, 1)

        self.target_picker = Gtk.DropDown.new_from_strings(labels)
        grid.attach(Gtk.Label(label="To currency", xalign=0), 0, 1, 1, 1)
        grid.attach(self.target_picker, 1, 1, 1, 1)
        reporting = reporting_currency_handle(db)
        target_index = next(
            (i for i, item in enumerate(self.currencies) if item.handle == reporting), 0
        )
        self.target_picker.set_selected(target_index)
        self.source_picker.set_selected(
            next((i for i in range(len(self.currencies)) if i != target_index), 0)
        )

        self.date_entry = Gtk.Entry(text=date.today().isoformat())
        grid.attach(Gtk.Label(label="As of", xalign=0), 0, 2, 1, 1)
        grid.attach(self.date_entry, 1, 2, 1, 1)

        self.rate_entry = Gtk.Entry(placeholder_text="1.25")
        self.rate_entry.set_tooltip_text("Target-currency units per one source-currency unit")
        grid.attach(Gtk.Label(label="Target units per source unit", xalign=0), 0, 3, 1, 1)
        grid.attach(self.rate_entry, 1, 3, 1, 1)

        self.current = Gtk.Label(xalign=0, wrap=True)
        self.current.add_css_class("dim")
        box.append(self.current)

        self.status = Gtk.Label(xalign=0, wrap=True)
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        self.save_button = Gtk.Button(label="Save rate")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._on_save)
        buttons.append(self.save_button)
        box.append(buttons)

        if len(self.currencies) < 2:
            self.save_button.set_sensitive(False)
            self._error("Add a second currency before entering an exchange rate.")
        for picker in (self.source_picker, self.target_picker):
            picker.connect("notify::selected", self._show_current)
        self._show_current()

    def _pair(self):
        if len(self.currencies) < 2:
            return None, None
        return (
            self.currencies[self.source_picker.get_selected()],
            self.currencies[self.target_picker.get_selected()],
        )

    def _show_current(self, *_args) -> None:
        """Show the latest direct quote for the chosen pair, if there is one."""
        source, target = self._pair()
        if source is None or target is None or source.handle == target.handle:
            self.current.set_text("")
            return
        latest = valuation.latest_price(self.db, source, currency=target)
        if latest is None:
            self.current.set_text(f"No {source.mnemonic}→{target.mnemonic} quote is recorded yet.")
            return
        self.current.set_text(
            f"Latest {source.mnemonic}→{target.mnemonic}: {latest.value.to_decimal()} "
            f"on {latest.quote_date.isoformat()} ({latest.source or 'unknown source'})."
        )

    def save(self) -> CommodityPrice | None:
        """Validate through the shared contract and save; None when refused."""
        source, target = self._pair()
        if source is None or target is None:
            self._error("Add a second currency before entering an exchange rate.")
            return None
        try:
            quote_date = date.fromisoformat(self.date_entry.get_text().strip())
        except ValueError:
            self._error("Enter the quote date as YYYY-MM-DD.")
            return None
        try:
            value = Money(parse_user_amount(self.rate_entry.get_text().strip()))
        except (ValueError, ArithmeticError):
            self._error("Enter the rate as a number, for example 1.25.")
            return None
        try:
            self.saved = valuation.save_currency_quote(
                self.db,
                source_handle=source.handle,
                target_handle=target.handle,
                quote_date=quote_date,
                value=value,
            )
        except ValueError as exc:
            self._error(str(exc).capitalize() + ".")
            return None
        return self.saved

    def _on_save(self, _button) -> None:
        if self.save() is not None:
            self.close()

    def _error(self, message: str) -> None:
        self.status.set_text(message)
        self.status.add_css_class("negative")
