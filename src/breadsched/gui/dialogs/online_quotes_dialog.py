"""Choose each commodity's online quote source and fetch quotes.

The network fetch runs in a :class:`~breadsched.gui.background.BackgroundJob`
worker that never touches the book; the answers are stored on the main thread by
the book server's ``store_quotes`` (``gen.services.quotes.StoreQuotes``).
"""

from __future__ import annotations

from ...api import ApiError
from ...api.local import book_server
from ...gen.services.quotes import QuoteUpdate, SetQuoteSource, StoreQuotes
from ...gen.utils.settings import Settings
from ...plugins.quotes import (
    FinanceQuoteInfo,
    OnlineQuotes,
    alphavantage_key,
    finance_quote_info,
    quote_source_choices,
    quote_source_warning,
    save_alphavantage_key,
)
from ...presentation import price_text
from ..background import BackgroundJob
from ..gi_setup import Gtk, Pango
from ..remote_book import OpenBook
from ..widgets.bounded import BoundedWindow, scroll_body
from ..widgets.help import help_row

__all__ = ["OnlineQuotesDialog"]

_SOURCES_HINT = (
    "tsp (Thrift Savings Plan funds), alphavantage (stocks and funds, with your key), "
    "currency (exchange rates, for currencies), or a Finance::Quote method such as usa "
    "or nyse; choose from the list beside each source"
)


class SourceEntry(Gtk.Entry):
    """A quote source typed freely or chosen from the sources known here (#317)."""

    def __init__(self, text: str, choices: list[str]) -> None:
        super().__init__(text=text, hexpand=True)
        self.choices = choices
        self.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, "pan-down-symbolic")
        self.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Choose a quote source")
        self.popover = Gtk.Popover()
        self.popover.set_parent(self)
        self.popover.set_autohide(True)
        self.listing = Gtk.ListBox()
        self.listing.connect("row-activated", self._on_chosen)
        scroller = Gtk.ScrolledWindow(child=self.listing)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(220)
        scroller.set_propagate_natural_width(True)
        self.popover.set_child(scroller)
        self.connect("icon-press", lambda *_: self.show_choices())
        self.connect("destroy", lambda *_: self.popover.unparent())

    def shown_choices(self) -> list[str]:
        """The choices the list offers: those containing what is typed, else all."""
        typed = self.get_text().strip().casefold()
        matching = [name for name in self.choices if typed and typed in name.casefold()]
        return matching or list(self.choices)

    def show_choices(self) -> None:
        while row := self.listing.get_first_child():
            self.listing.remove(row)
        for name in self.shown_choices():
            label = Gtk.Label(label=name, xalign=0)
            for side in ("top", "bottom", "start", "end"):
                getattr(label, f"set_margin_{side}")(4)
            self.listing.append(label)
        self.popover.popup()

    def _on_chosen(self, _listing, row: Gtk.ListBoxRow) -> None:
        label = row.get_child()
        if isinstance(label, Gtk.Label):
            self.set_text(label.get_text())
            self.set_position(-1)
        self.popover.popdown()


class OnlineQuotesDialog(BoundedWindow):
    """Quote sources per commodity, the Alpha Vantage key, and **Get quotes**."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        db: OpenBook,
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
        self.probe: BackgroundJob[FinanceQuoteInfo, None] = BackgroundJob()
        self.last_update: QuoteUpdate | None = None
        #: What Finance::Quote offers here, once asked (in the background).
        self.finance_quote: FinanceQuoteInfo | None = None
        #: Every source a commodity may name; Finance::Quote's join once known.
        self.source_choices: list[str] = list(quote_source_choices(FinanceQuoteInfo(False, "")))
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
        self.source_entries: dict[str, SourceEntry] = {}
        commodities = [
            item for item in book_server(db).quote_plan().commodities if not item.reporting
        ]
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
            entry = SourceEntry(commodity.quote_source, self.source_choices)
            entry.set_placeholder_text("currency" if commodity.currency else "none")
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

        # Asking Finance::Quote what it offers starts Perl and loads every
        # module, so it runs off the main thread (#317).
        self.finance_quote_label = Gtk.Label(label="Checking Finance::Quote…", xalign=0, wrap=True)
        self.finance_quote_label.add_css_class("dim")
        body.append(self.finance_quote_label)

        # What happened stays in view below the scrolling form, beside the
        # buttons, however long the list of commodities (#317).
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.results = Gtk.Label(xalign=0, wrap=True, selectable=True)
        footer.append(self.results)

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
        footer.append(buttons)
        outer.append(footer)
        scroll_body(self)
        self.connect("close-request", self._on_close)
        self.probe.start(
            lambda _cancel, _report: finance_quote_info(),
            lambda _progress: None,
            self._finance_quote_known,
            lambda error: self.finance_quote_label.set_text(
                f"Finance::Quote could not be checked: {error}"
            ),
        )

    def _finance_quote_known(self, info: FinanceQuoteInfo) -> None:
        self.finance_quote = info
        self.source_choices[:] = quote_source_choices(info)
        if info.available:
            text = f"{info.reason}, offering {len(info.methods)} quote methods to choose from."
        else:
            text = f"{info.reason}. Its other sources cannot be used."
        self.finance_quote_label.set_text(text)

    def _on_close(self, *_args) -> bool:
        self.job.cancel()
        self.probe.cancel()
        return False

    def save_sources(self) -> bool:
        """Store every changed quote source and the key; False if any was refused."""
        problems: list[str] = []
        warnings: list[str] = []
        book = book_server(self.db)
        stored = {item.handle: item for item in book.quote_plan().commodities}
        for handle, entry in self.source_entries.items():
            commodity = stored.get(handle)
            if commodity is None or commodity.quote_source == entry.get_text().strip():
                continue
            try:
                source = book.set_quote_source(SetQuoteSource(handle, entry.get_text()))
            except ApiError as error:
                problems.append(f"{commodity.mnemonic}: {error}")
                continue
            if self.finance_quote is not None:
                warning = quote_source_warning(source, self.finance_quote)
                if warning is not None:
                    warnings.append(f"{commodity.mnemonic}: {warning}")
        if alphavantage_key(self.settings) != self.key_entry.get_text().strip():
            if not save_alphavantage_key(self.settings, self.key_entry.get_text()):
                problems.append("The Alpha Vantage key could not be saved")
        self.results.set_text("\n".join(problems + warnings) or "Sources saved.")
        return not problems

    def get_quotes(self) -> None:
        """Save the sources, fetch off the main thread, then store what came back."""
        if self.job.active or not self.save_sources():
            return
        plan = book_server(self.db).quote_plan()
        requests = plan.requests
        if not requests:
            self.results.set_text("No commodity has a quote source.")
            return
        currency = plan.currency or "USD"
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
        try:
            update = book_server(self.db).store_quotes(StoreQuotes(tuple(quotes), tuple(failures)))
        except ApiError as error:
            self.results.set_text(str(error))
            return
        self.last_update = update
        lines = [
            f"{item.symbol}: {price_text(item.value)} {item.currency} on {item.when.isoformat()} "
            f"({item.source}){'' if item.changed else ', unchanged'}"
            for item in update.stored
        ]
        lines += [f"{item.symbol}: not updated — {item.reason}" for item in update.failures]
        self.results.set_text("\n".join(lines) or "No quotes came back.")

    def _failed(self, error: BaseException) -> None:
        self.fetch_button.set_sensitive(True)
        self.results.set_text(f"Quotes could not be fetched: {error}")
