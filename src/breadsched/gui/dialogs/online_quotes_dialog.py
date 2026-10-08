"""Choose each commodity's online quote source and fetch quotes.

The network fetch runs in a :class:`~breadsched.gui.background.BackgroundJob`
worker that never touches the book; the answers are stored on the main thread by
``update_quotes`` through :class:`~breadsched.gen.services.quotes.Prefetched`.
"""

from __future__ import annotations

from ...gen.db.sqlite import DbSQLite
from ...gen.engine.currency import reporting_currency_handle
from ...gen.services.quotes import (
    Prefetched,
    QuoteUpdate,
    SetQuoteSource,
    quote_requests,
    set_quote_source,
    update_quotes,
)
from ...gen.utils.settings import Settings
from ...plugins.quotes import (
    OnlineQuotes,
    alphavantage_key,
    finance_quote_status,
    save_alphavantage_key,
)
from ...presentation import price_text, service_error_message
from ..background import BackgroundJob
from ..gi_setup import Gtk, Pango
from ..widgets.bounded import BoundedWindow, scroll_body
from ..widgets.help import help_row

__all__ = ["OnlineQuotesDialog"]

_SOURCES_HINT = (
    "tsp (Thrift Savings Plan funds), alphavantage (stocks and funds, with your key), "
    "currency (exchange rates, for currencies), or a Finance::Quote method such as vanguard"
)


class OnlineQuotesDialog(BoundedWindow):
    """Quote sources per commodity, the Alpha Vantage key, and **Get quotes**."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: DbSQLite,
        settings: Settings | None = None,
        fetcher_factory=None,
    ) -> None:
        super().__init__(title="Online quotes", transient_for=parent, modal=True)
        self.db = db
        self.settings = settings if settings is not None else Settings()
        self.fetcher_factory = fetcher_factory or (
            lambda key: OnlineQuotes(alphavantage_key=key or None)
        )
        self.job: BackgroundJob = BackgroundJob()
        self.last_update: QuoteUpdate | None = None
        self.set_default_size(640, 520)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(16)
        self.set_child(outer)
        outer.append(help_row("online-quotes"))
        body = outer
        intro = Gtk.Label(
            label=(
                "Give a security or currency a quote source, then choose Get quotes. "
                "Fetched prices are stored with their source and date; nothing is "
                f"fetched unless you ask. Sources: {_SOURCES_HINT}."
            ),
            xalign=0,
            wrap=True,
        )
        intro.add_css_class("dim")
        body.append(intro)

        self.grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        body.append(self.grid)
        self.source_entries: dict[str, Gtk.Entry] = {}
        reporting = reporting_currency_handle(db)
        commodities = sorted(
            (item for item in db.iter_commodities() if item.handle != reporting),
            key=lambda item: (item.is_currency, item.mnemonic),
        )
        for column, title in enumerate(("Commodity", "Name", "Quote source")):
            heading = Gtk.Label(label=title, xalign=0)
            heading.add_css_class("heading")
            self.grid.attach(heading, column, 0, 1, 1)
        for row, commodity in enumerate(commodities, start=1):
            self.grid.attach(Gtk.Label(label=commodity.mnemonic, xalign=0), 0, row, 1, 1)
            name = Gtk.Label(label=commodity.fullname, xalign=0)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            name.set_max_width_chars(28)
            self.grid.attach(name, 1, row, 1, 1)
            entry = Gtk.Entry(text=commodity.quote_source, hexpand=True)
            entry.set_placeholder_text("currency" if commodity.is_currency else "none")
            entry.set_tooltip_text(_SOURCES_HINT)
            entry.update_property(
                [Gtk.AccessibleProperty.LABEL], [f"Quote source for {commodity.mnemonic}"]
            )
            self.grid.attach(entry, 2, row, 1, 1)
            self.source_entries[commodity.handle] = entry
        if not commodities:
            self.grid.attach(
                Gtk.Label(label="The book has no securities or foreign currencies.", xalign=0),
                0,
                1,
                3,
                1,
            )

        key_row = Gtk.Box(spacing=8)
        key_row.append(Gtk.Label(label="Alpha Vantage API key", xalign=0))
        self.key_entry = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True)
        self.key_entry.set_text(alphavantage_key(self.settings))
        self.key_entry.update_property([Gtk.AccessibleProperty.LABEL], ["Alpha Vantage API key"])
        key_row.append(self.key_entry)
        body.append(key_row)

        available, reason = finance_quote_status()
        self.finance_quote_label = Gtk.Label(
            label=f"{reason}." + ("" if available else " Its other sources cannot be used."),
            xalign=0,
            wrap=True,
        )
        self.finance_quote_label.add_css_class("dim")
        body.append(self.finance_quote_label)

        self.results = Gtk.Label(xalign=0, wrap=True, selectable=True)
        body.append(self.results)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        self.save_button = Gtk.Button(label="Save sources")
        self.save_button.connect("clicked", lambda *_: self.save_sources())
        self.fetch_button = Gtk.Button(label="Get quotes")
        self.fetch_button.add_css_class("suggested-action")
        self.fetch_button.connect("clicked", lambda *_: self.get_quotes())
        for button in (close, self.save_button, self.fetch_button):
            buttons.append(button)
        outer.append(buttons)
        scroll_body(self)
        self.connect("close-request", self._on_close)

    def _on_close(self, *_args) -> bool:
        self.job.cancel()
        return False

    def save_sources(self) -> bool:
        """Store every changed quote source and the key; False if any was refused."""
        problems = []
        for handle, entry in self.source_entries.items():
            commodity = self.db.get_commodity(handle)
            if commodity is None or commodity.quote_source == entry.get_text().strip():
                continue
            result = set_quote_source(self.db, SetQuoteSource(handle, entry.get_text()))
            if result.value is None:
                problems.append(f"{commodity.mnemonic}: {service_error_message(result.errors[0])}")
        if alphavantage_key(self.settings) != self.key_entry.get_text().strip():
            if not save_alphavantage_key(self.settings, self.key_entry.get_text()):
                problems.append("The Alpha Vantage key could not be saved")
        self.results.set_text("\n".join(problems) if problems else "Sources saved.")
        return not problems

    def get_quotes(self) -> None:
        """Save the sources, fetch off the main thread, then store what came back."""
        if self.job.active or not self.save_sources():
            return
        requests = quote_requests(self.db)
        if not requests:
            self.results.set_text("No commodity has a quote source.")
            return
        reporting = self.db.get_commodity(reporting_currency_handle(self.db))
        currency = reporting.mnemonic if reporting is not None else "USD"
        fetcher = self.fetcher_factory(self.key_entry.get_text().strip())
        self.fetch_button.set_sensitive(False)
        self.results.set_text(f"Getting {len(requests)} quote(s)…")
        self.job.start(
            lambda _cancel, _report: fetcher.fetch(requests, currency),
            lambda _progress: None,
            self._store,
            self._failed,
        )

    def _store(self, fetched) -> None:
        self.fetch_button.set_sensitive(True)
        quotes, failures = fetched
        result = update_quotes(self.db, Prefetched(quotes, failures))
        if result.value is None:
            self.results.set_text(service_error_message(result.errors[0]))
            return
        self.last_update = result.value
        lines = [
            f"{item.symbol}: {price_text(item.value)} {item.currency} on {item.when.isoformat()} "
            f"({item.source}){'' if item.changed else ', unchanged'}"
            for item in result.value.stored
        ]
        lines += [f"{item.symbol}: not updated — {item.reason}" for item in result.value.failures]
        self.results.set_text("\n".join(lines) or "No quotes came back.")

    def _failed(self, error: BaseException) -> None:
        self.fetch_button.set_sensitive(True)
        self.results.set_text(f"Quotes could not be fetched: {error}")
