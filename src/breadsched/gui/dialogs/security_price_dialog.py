"""Create a security and record exact dated market prices."""

from __future__ import annotations

from datetime import date

from ...gen.db.sqlite import DbSQLite
from ...gen.engine import valuation
from ...gen.lib.money import Money
from ...gen.utils.amount_input import parse_user_amount
from ..gi_setup import Gtk

__all__ = ["SecurityPriceDialog"]


class SecurityPriceDialog(Gtk.Window):
    """Small price editor shared conceptually with the web write boundary."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite) -> None:
        super().__init__(title="Security price", transient_for=parent, modal=True)
        self.db = db
        self.securities = [item for item in db.iter_commodities() if not item.is_currency]
        self.currencies = [item for item in db.iter_commodities() if item.is_currency]
        self.set_default_size(520, 430)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        explanation = Gtk.Label(
            label=(
                "A dated price values the exact security quantities already in the ledger. "
                "It does not rewrite historical transaction values."
            ),
            xalign=0,
            wrap=True,
        )
        explanation.add_css_class("dim")
        box.append(explanation)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        box.append(grid)
        row = 0

        self.security_picker = Gtk.DropDown.new_from_strings(
            ["New security…"] + [f"{item.mnemonic} — {item.fullname}" for item in self.securities]
        )
        grid.attach(Gtk.Label(label="Security", xalign=0), 0, row, 1, 1)
        grid.attach(self.security_picker, 1, row, 1, 1)
        row += 1

        self.mnemonic_entry = Gtk.Entry(placeholder_text="INDEX")
        grid.attach(Gtk.Label(label="Symbol", xalign=0), 0, row, 1, 1)
        grid.attach(self.mnemonic_entry, 1, row, 1, 1)
        row += 1

        self.fullname_entry = Gtk.Entry(placeholder_text="Index fund")
        grid.attach(Gtk.Label(label="Full name", xalign=0), 0, row, 1, 1)
        grid.attach(self.fullname_entry, 1, row, 1, 1)
        row += 1

        self.namespace_entry = Gtk.Entry(text="FUND")
        grid.attach(Gtk.Label(label="Namespace", xalign=0), 0, row, 1, 1)
        grid.attach(self.namespace_entry, 1, row, 1, 1)
        row += 1

        self.fraction_entry = Gtk.Entry(text="10000", input_purpose=Gtk.InputPurpose.DIGITS)
        self.fraction_entry.set_tooltip_text(
            "Denominator of the smallest unit, for example 10000 for four decimal places"
        )
        grid.attach(Gtk.Label(label="Smallest-unit denominator", xalign=0), 0, row, 1, 1)
        grid.attach(self.fraction_entry, 1, row, 1, 1)
        row += 1

        self.currency_picker = Gtk.DropDown.new_from_strings(
            [f"{item.mnemonic} — {item.fullname}" for item in self.currencies]
            or ["USD — US Dollar (create)"]
        )
        grid.attach(Gtk.Label(label="Quote currency", xalign=0), 0, row, 1, 1)
        grid.attach(self.currency_picker, 1, row, 1, 1)
        row += 1

        self.date_entry = Gtk.Entry(text=date.today().isoformat())
        grid.attach(Gtk.Label(label="As of", xalign=0), 0, row, 1, 1)
        grid.attach(self.date_entry, 1, row, 1, 1)
        row += 1

        self.price_entry = Gtk.Entry(placeholder_text="125.25")
        grid.attach(Gtk.Label(label="Price per unit", xalign=0), 0, row, 1, 1)
        grid.attach(self.price_entry, 1, row, 1, 1)

        self.status = Gtk.Label(xalign=0, wrap=True)
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        self.save_button = Gtk.Button(label="Save price")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._on_save)
        buttons.append(self.save_button)
        box.append(buttons)
        self.security_picker.connect("notify::selected", self._on_security_changed)
        self._on_security_changed()

    def _on_security_changed(self, *_args) -> None:
        index = self.security_picker.get_selected()
        creating = index == 0
        for widget in (
            self.mnemonic_entry,
            self.fullname_entry,
            self.namespace_entry,
            self.fraction_entry,
        ):
            widget.set_sensitive(creating)
        if creating:
            self.mnemonic_entry.set_text("")
            self.fullname_entry.set_text("")
            self.namespace_entry.set_text("FUND")
            self.fraction_entry.set_text("10000")
            self.price_entry.set_text("")
            self.date_entry.set_text(date.today().isoformat())
            return
        security = self.securities[index - 1]
        self.mnemonic_entry.set_text(security.mnemonic)
        self.fullname_entry.set_text(security.fullname)
        self.namespace_entry.set_text(security.namespace)
        self.fraction_entry.set_text(str(security.fraction))
        latest = valuation.latest_price(self.db, security)
        if latest is None:
            self.price_entry.set_text("")
            self.date_entry.set_text(date.today().isoformat())
            return
        self.price_entry.set_text(latest.value.format())
        self.date_entry.set_text(latest.quote_date.isoformat())
        currency_index = next(
            (i for i, item in enumerate(self.currencies) if item.handle == latest.currency),
            None,
        )
        if currency_index is not None:
            self.currency_picker.set_selected(currency_index)

    def _on_save(self, _button) -> None:
        index = self.security_picker.get_selected()
        security_handle = self.securities[index - 1].handle if index else None
        try:
            quote_date = date.fromisoformat(self.date_entry.get_text().strip())
            value = Money(parse_user_amount(self.price_entry.get_text().strip()))
            fraction = int(self.fraction_entry.get_text().strip())
            valuation.save_security_price(
                self.db,
                security_handle=security_handle,
                currency_handle=(
                    self.currencies[self.currency_picker.get_selected()].handle
                    if self.currencies
                    else None
                ),
                quote_date=quote_date,
                value=value,
                mnemonic=self.mnemonic_entry.get_text(),
                fullname=self.fullname_entry.get_text(),
                namespace=self.namespace_entry.get_text(),
                fraction=fraction,
            )
        except (ValueError, ArithmeticError) as exc:
            self._error(str(exc))
            return
        self.close()

    def _error(self, message: str) -> None:
        self.status.set_text(message)
        self.status.add_css_class("negative")
