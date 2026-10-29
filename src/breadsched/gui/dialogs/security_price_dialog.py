"""Create a security and record exact dated market prices."""

from __future__ import annotations

from datetime import date

from ...api import ApiError
from ...api.local import book_server
from ...gen.db.sqlite import DbSQLite
from ...gen.lib.money import Money
from ...gen.services.quotes import QuoteRequest
from ...gen.services.security_prices import SaveSecurityPrice
from ...gen.utils.amount_input import parse_user_amount
from ...gen.utils.settings import Settings
from ...plugins.quotes import OnlineQuotes, alphavantage_key
from ..background import BackgroundJob
from ..gi_setup import Gtk
from ..widgets.bounded import BoundedWindow
from ..widgets.choice import bounded_dropdown
from ..widgets.date_entry import attach_date_keys, parse_date_text
from ..widgets.help import help_row

__all__ = ["SecurityPriceDialog"]


class SecurityPriceDialog(BoundedWindow):
    """Small price editor shared conceptually with the web write boundary."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: DbSQLite,
        settings: Settings | None = None,
        fetcher_factory=None,
    ) -> None:
        super().__init__(title="Security price", transient_for=parent, modal=True)
        self.db = db
        self.settings = settings if settings is not None else Settings()
        self.fetcher_factory = fetcher_factory or (
            lambda key: OnlineQuotes(alphavantage_key=key or None)
        )
        self.job: BackgroundJob = BackgroundJob()
        quotes = book_server(db).securities()
        self.securities = [quote.security for quote in quotes]
        self._latest = {quote.security.handle: quote.latest for quote in quotes}
        self.currencies = list(book_server(db).currencies(book_order=True))
        self.set_default_size(520, 430)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.prepend(help_row("prices"))

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

        self.security_picker = bounded_dropdown(
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

        self.currency_picker = bounded_dropdown(
            [f"{item.mnemonic} — {item.fullname}" for item in self.currencies]
            or ["USD — US Dollar (create)"]
        )
        grid.attach(Gtk.Label(label="Quote currency", xalign=0), 0, row, 1, 1)
        grid.attach(self.currency_picker, 1, row, 1, 1)
        row += 1

        self.date_entry = attach_date_keys(Gtk.Entry(text=date.today().isoformat()))
        grid.attach(Gtk.Label(label="As of", xalign=0), 0, row, 1, 1)
        grid.attach(self.date_entry, 1, row, 1, 1)
        row += 1

        self.price_entry = Gtk.Entry(placeholder_text="125.25", hexpand=True)
        grid.attach(Gtk.Label(label="Price per unit", xalign=0), 0, row, 1, 1)
        price_row = Gtk.Box(spacing=6)
        price_row.append(self.price_entry)
        # A security with a quote source can fetch its price here to review
        # before saving it (#317).
        self.quote_button = Gtk.Button(label="Get online quote")
        self.quote_button.connect("clicked", lambda *_: self.get_online_quote())
        price_row.append(self.quote_button)
        grid.attach(price_row, 1, row, 1, 1)

        self.status = Gtk.Label(xalign=0, wrap=True)
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        self.save_button = Gtk.Button(label="Save price")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._on_save)
        self.connect("close-request", self._on_close)
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
        source = "" if creating else self.securities[index - 1].quote_source.strip()
        self.quote_button.set_sensitive(bool(source) and not self.job.active)
        self.quote_button.set_tooltip_text(
            f"Fetch today's price from {source}; it is saved only when you choose Save price"
            if source
            else "Give this security a quote source in Online quotes to fetch its price"
        )
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
        latest = self._latest.get(security.handle)
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
            quote_date = parse_date_text(self.date_entry.get_text().strip())
            value = Money(parse_user_amount(self.price_entry.get_text().strip()))
            fraction = int(self.fraction_entry.get_text().strip())
        except (ValueError, ArithmeticError) as exc:
            self._error(str(exc))
            return
        request = SaveSecurityPrice(
            security=security_handle,
            currency=(
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
        try:
            book_server(self.db).save_security_price(request)
        except ApiError as error:
            self._error(str(error))
            return
        self.close()

    def _on_close(self, *_args) -> bool:
        self.job.cancel()
        return False

    def get_online_quote(self) -> None:
        """Fetch the chosen security's price off the main thread, to review."""
        index = self.security_picker.get_selected()
        if not index or self.job.active:
            return
        security = self.securities[index - 1]
        source = security.quote_source.strip()
        if not source:
            return
        currency = book_server(self.db).reporting_currency() or "USD"
        fetcher = self.fetcher_factory(alphavantage_key(self.settings))
        request = QuoteRequest(security.handle, security.mnemonic, source)
        self.quote_button.set_sensitive(False)
        self.status.remove_css_class("negative")
        self.status.set_text(f"Getting {security.mnemonic} from {source}…")
        self.job = BackgroundJob()
        self.job.start(
            lambda _cancel, _report: fetcher.fetch([request], currency),
            lambda _progress: None,
            lambda fetched: self._quote_arrived(security.handle, fetched),
            self._quote_failed,
        )

    def _quote_arrived(self, handle: str, fetched) -> None:
        self.quote_button.set_sensitive(True)
        index = self.security_picker.get_selected()
        if not index or self.securities[index - 1].handle != handle:
            return  # another security was chosen meanwhile
        quotes, failures = fetched
        if not quotes:
            reason = failures[0].reason if failures else "no quote came back"
            self._error(f"No quote: {reason}.")
            return
        quote = quotes[0]
        self.price_entry.set_text(f"{quote.value}")
        self.date_entry.set_text(quote.when.isoformat())
        match = next(
            (i for i, item in enumerate(self.currencies) if item.mnemonic == quote.currency),
            None,
        )
        if match is not None:
            self.currency_picker.set_selected(match)
        self.status.remove_css_class("negative")
        self.status.set_text(
            f"{quote.value} {quote.currency} on {quote.when.isoformat()} from {quote.origin}. "
            "Choose Save price to keep it."
        )

    def _quote_failed(self, error: BaseException) -> None:
        self.quote_button.set_sensitive(True)
        self._error(f"The quote could not be fetched: {error}")

    def _error(self, message: str) -> None:
        self.status.set_text(message)
        self.status.add_css_class("negative")
