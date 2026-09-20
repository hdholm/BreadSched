"""Set up a loan, in the manner of GnuCash's mortgage assistant.

The dialog asks for what a lender tells you — amount, rate, term — and shows the
resulting payment and the first year's split between interest and principal before
anything is saved. Seeing that a 200,000 mortgage puts 1,000 of its first 1,199
payment straight into interest is the point of the preview: it is the number that
makes a projection believable, and the one people are most surprised by.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from ...gen.db.sqlite import DbSQLite
from ...gen.engine.currency import reporting_fraction
from ...gen.engine.loans import LoanTerms, schedule_preview
from ...gen.lib import Money
from ...gen.lib.account import AccountClass
from ...gen.services import SaveLoan, save_loan
from ...gen.utils.amount_input import parse_user_amount
from ...presentation import service_error_message
from ..gi_setup import Gtk

__all__ = ["LoanDialog"]


class LoanDialog(Gtk.Window):
    """Enter a loan's terms and create its scheduled payment."""

    def __init__(self, parent: Gtk.Window | None, db: DbSQLite) -> None:
        super().__init__(title="Set up a loan", transient_for=parent, modal=True)
        self.db = db
        self.set_default_size(620, 620)

        self._liabilities = self._accounts_of(AccountClass.LIABILITY)
        self._expenses = self._accounts_of(AccountClass.EXPENSE)
        self._funding = [
            a
            for a in db.iter_accounts()
            if a.atype.is_cash_like and not a.placeholder and not a.hidden
        ]

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        box.append(grid)
        row = 0

        self.name_entry = Gtk.Entry(placeholder_text="Mortgage")
        self.name_entry.set_hexpand(True)
        self.name_entry.connect("changed", self._recalculate)
        grid.attach(Gtk.Label(label="Name", xalign=0), 0, row, 1, 1)
        grid.attach(self.name_entry, 1, row, 1, 1)
        row += 1

        self.amount_entry = Gtk.Entry(placeholder_text="200000.00")
        self.amount_entry.connect("changed", self._recalculate)
        grid.attach(Gtk.Label(label="Amount borrowed", xalign=0), 0, row, 1, 1)
        grid.attach(self.amount_entry, 1, row, 1, 1)
        row += 1

        self.rate_entry = Gtk.Entry(placeholder_text="6.0")
        self.rate_entry.set_tooltip_text("Annual interest rate, as a percentage")
        self.rate_entry.connect("changed", self._recalculate)
        grid.attach(Gtk.Label(label="Annual rate (%)", xalign=0), 0, row, 1, 1)
        grid.attach(self.rate_entry, 1, row, 1, 1)
        row += 1

        self.years_spin = Gtk.SpinButton.new_with_range(1, 50, 1)
        self.years_spin.set_value(25)
        self.years_spin.connect("value-changed", self._recalculate)
        grid.attach(Gtk.Label(label="Term (years)", xalign=0), 0, row, 1, 1)
        grid.attach(self.years_spin, 1, row, 1, 1)
        row += 1

        self.start_entry = Gtk.Entry(text=date.today().replace(day=1).isoformat())
        self.start_entry.connect("changed", self._recalculate)
        grid.attach(Gtk.Label(label="First payment", xalign=0), 0, row, 1, 1)
        grid.attach(self.start_entry, 1, row, 1, 1)
        row += 1

        self.liability_picker = self._picker(self._liabilities)
        grid.attach(Gtk.Label(label="Loan account", xalign=0), 0, row, 1, 1)
        grid.attach(self.liability_picker, 1, row, 1, 1)
        row += 1

        self.interest_picker = self._picker(self._expenses)
        grid.attach(Gtk.Label(label="Interest expense", xalign=0), 0, row, 1, 1)
        grid.attach(self.interest_picker, 1, row, 1, 1)
        row += 1

        self.funding_picker = self._picker(self._funding)
        grid.attach(Gtk.Label(label="Paid from", xalign=0), 0, row, 1, 1)
        grid.attach(self.funding_picker, 1, row, 1, 1)
        row += 1

        self.opening_check = Gtk.CheckButton(
            label="Record what is currently owed as an opening balance", active=True
        )
        self.opening_check.set_tooltip_text(
            "Without this the loan account reads zero, and a forecast shows "
            "payments leaving against a debt that does not exist."
        )
        grid.attach(self.opening_check, 1, row, 1, 1)

        self.summary = Gtk.Label(xalign=0)
        self.summary.add_css_class("summary-value")
        box.append(self.summary)

        self.preview = Gtk.Grid(column_spacing=18, row_spacing=2)
        scroller = Gtk.ScrolledWindow(child=self.preview)
        scroller.set_vexpand(True)
        box.append(scroller)

        self.status = Gtk.Label(xalign=0)
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        self.save_button = Gtk.Button(label="Create loan")
        self.save_button.add_css_class("suggested-action")
        self.save_button.set_sensitive(False)
        self.save_button.connect("clicked", self._on_save)
        buttons.append(self.save_button)
        box.append(buttons)

        self._recalculate()

    # ------------------------------------------------------------------ inputs

    def _accounts_of(self, account_class: AccountClass) -> list:
        return [
            a
            for a in self.db.iter_accounts()
            if a.account_class is account_class
            and not a.placeholder
            and not a.is_root
            and not a.hidden
        ]

    def _picker(self, accounts: list) -> Gtk.DropDown:
        picker = Gtk.DropDown.new_from_strings(
            [self.db.full_name(a) for a in accounts] or ["(none available)"]
        )
        picker.connect("notify::selected", self._recalculate)
        return picker

    def terms(self) -> LoanTerms | None:
        """The loan the form currently describes, or None if it is incomplete."""
        name = self.name_entry.get_text().strip()
        if not name or not (self._liabilities and self._expenses and self._funding):
            return None
        try:
            principal = Money(parse_user_amount(self.amount_entry.get_text().strip()))
            rate = Decimal(self.rate_entry.get_text().strip() or "0") / Decimal(100)
            start = date.fromisoformat(self.start_entry.get_text().strip())
        except (ValueError, ArithmeticError, InvalidOperation):
            return None
        if principal <= 0 or rate < 0:
            return None

        return LoanTerms(
            name=name,
            principal=principal,
            annual_rate=rate,
            years=int(self.years_spin.get_value()),
            start=start,
            liability=self._liabilities[self.liability_picker.get_selected()].handle,
            interest_account=self._expenses[self.interest_picker.get_selected()].handle,
            payment_account=self._funding[self.funding_picker.get_selected()].handle,
            fraction=reporting_fraction(self.db),
        )

    # ----------------------------------------------------------------- preview

    def _recalculate(self, *_args) -> None:
        child = self.preview.get_first_child()
        while child is not None:
            self.preview.remove(child)
            child = self.preview.get_first_child()

        terms = self.terms()
        if terms is None:
            self.save_button.set_sensitive(False)
            self.summary.set_text("")
            missing = []
            if not (self._liabilities and self._expenses and self._funding):
                missing.append(
                    "this book needs a liability account, an expense account for "
                    "interest, and a bank account"
                )
            self.status.set_text("; ".join(missing))
            return

        self.status.set_text("")
        self.save_button.set_sensitive(True)
        self.summary.set_text(
            f"Payment {terms.payment().format()} per month   -   "
            f"total interest {terms.total_interest().format()}"
        )

        for position, heading in enumerate(
            ("Payment", "Amount", "Interest", "Principal", "Balance")
        ):
            label = Gtk.Label(label=heading, xalign=1 if position else 0)
            label.add_css_class("summary-label")
            self.preview.attach(label, position, 0, 1, 1)

        for index, entry in enumerate(schedule_preview(terms, rows=12), start=1):
            self.preview.attach(Gtk.Label(label=str(entry["period"]), xalign=0), 0, index, 1, 1)
            for position, key in enumerate(
                ("payment", "interest", "principal", "balance"), start=1
            ):
                amount = entry[key]
                assert isinstance(amount, Money)
                cell = Gtk.Label(label=amount.format(), xalign=1)
                cell.add_css_class("numeric")
                self.preview.attach(cell, position, index, 1, 1)

    def _on_save(self, _button) -> None:
        terms = self.terms()
        if terms is None:
            return
        result = save_loan(
            self.db,
            SaveLoan(terms, opening_balance=self.opening_check.get_active()),
        )
        if not result.ok:
            self.status.set_text(service_error_message(result.errors[0]))
            return
        self.close()
