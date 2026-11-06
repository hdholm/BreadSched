"""Share prices for scheduled transactions that buy or sell shares (#417).

A schedule stores what each leg is worth, not a number of shares, since a fund's
price moves between occurrences. Before entering, :func:`price_shares` asks the
book server which legs buy or sell shares (``book.due_quotes``). It fetches an
online quote in the background for each commodity that has a quote source and
no price as recent as the occurrence, and stores what comes back
(``book.store_quotes``). Then :class:`SharePricesDialog` shows each leg with its
amount, the latest price and the shares that price buys. A leg with no price
needs a share price or a number of shares before **Enter**. A price typed, or
the price that typed shares imply, is saved as a dated price in the same
undoable change that enters the transaction.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from decimal import InvalidOperation

from ...api import ApiError
from ...api.local import book_server
from ...gen.lib.money import Money
from ...gen.services import DueQuoteNeeds, DueQuotes, LegPrice, LegQuote
from ...gen.services.quotes import QuoteFailure, QuoteFetcher, StoreQuotes
from ...gen.utils.amount_input import parse_user_amount
from ...gen.utils.settings import Settings
from ...plugins.quotes import OnlineQuotes, alphavantage_key
from ...presentation import price_text
from ..background import BackgroundJob
from ..gi_setup import Gtk
from ..remote_book import OpenBook
from ..widgets.bounded import BoundedWindow
from ..widgets.help import help_row

__all__ = ["SharePricesDialog", "SharePricing", "price_shares"]


def default_fetcher() -> QuoteFetcher:
    """The online quote fetcher; replaced in tests so they never touch the network."""
    return OnlineQuotes(alphavantage_key=alphavantage_key(Settings()) or None)


def _amount(text: str) -> Money | None:
    try:
        value = Money(parse_user_amount(text)) if text.strip() else None
    except (InvalidOperation, ValueError):
        return None
    return value if value is not None and value > 0 else None


def _plain(value: Money) -> str:
    return format(value.to_decimal(6).normalize(), "f")


class SharePricesDialog(BoundedWindow):
    """Each leg that buys or sells shares, with its price and shares to confirm."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        legs: Sequence[LegQuote],
        failures: Sequence[QuoteFailure],
        on_done: Callable[[tuple[LegPrice, ...] | None], None],
    ) -> None:
        super().__init__(title="Share prices", transient_for=parent, modal=True)
        self.set_destroy_with_parent(True)
        self.set_default_size(720, 320)
        self.legs = list(legs)
        self.on_done = on_done
        self._answered = False
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        self.set_child(box)
        box.append(help_row("due-review"))
        box.append(
            Gtk.Label(
                label=(
                    "Shares are the amount over the share price. Type a share price or "
                    "the number of shares where one is missing or different; a price "
                    "typed is saved for that date."
                ),
                xalign=0,
                wrap=True,
            )
        )
        for failure in failures:
            note = Gtk.Label(
                label=f"{failure.symbol}: no online quote ({failure.reason}).", xalign=0, wrap=True
            )
            note.add_css_class("dim-label")
            box.append(note)
        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        for column, heading in enumerate(
            ("Occurrence", "Amount", "Commodity", "Share price", "Shares")
        ):
            label = Gtk.Label(label=heading, xalign=0 if column in (0, 2) else 1)
            label.add_css_class("heading")
            grid.attach(label, column, 0, 1, 1)
        #: Each leg's (price entry, shares entry), in the order of :attr:`legs`.
        self.entries: list[tuple[Gtk.Entry, Gtk.Entry]] = []
        #: Legs whose shares were typed (not worked out from the price).
        self.typed_shares: set[int] = set()
        self._filling = False
        for row, leg in enumerate(self.legs, start=1):
            occurrence = Gtk.Label(
                label=f"{leg.name} · {leg.when.isoformat()}\n{leg.account_name}", xalign=0
            )
            grid.attach(occurrence, 0, row, 1, 1)
            grid.attach(Gtk.Label(label=leg.value.format(), xalign=1), 1, row, 1, 1)
            quote = (
                f"{price_text(leg.price)} {leg.currency} on "
                f"{leg.price_date.isoformat() if leg.price_date else '?'} ({leg.price_source})"
                if leg.price is not None
                else "No price stored"
            )
            grid.attach(Gtk.Label(label=f"{leg.mnemonic}\n{quote}", xalign=0), 2, row, 1, 1)
            price = Gtk.Entry(width_chars=10, xalign=1)
            price.set_text(price_text(leg.price) if leg.price is not None else "")
            price.set_tooltip_text(f"Price of one {leg.mnemonic} share in {leg.currency}")
            shares = Gtk.Entry(width_chars=10, xalign=1)
            shares.set_text(_plain(leg.shares) if leg.shares is not None else "")
            shares.set_tooltip_text(f"Shares of {leg.mnemonic} bought or sold")
            price.connect("changed", lambda _e, index=row - 1: self._fill(index, from_price=True))
            shares.connect("changed", lambda _e, index=row - 1: self._fill(index, from_price=False))
            grid.attach(price, 3, row, 1, 1)
            grid.attach(shares, 4, row, 1, 1)
            self.entries.append((price, shares))
        scroller = Gtk.ScrolledWindow(child=grid, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        box.append(scroller)
        self.message = Gtk.Label(xalign=0, wrap=True)
        self.message.add_css_class("error")
        box.append(self.message)
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        enter = Gtk.Button(label="Enter")
        enter.add_css_class("suggested-action")
        enter.connect("clicked", lambda _b: self.enter())
        actions.append(cancel)
        actions.append(enter)
        box.append(actions)
        self.connect("close-request", self._on_close)
        if self.entries:
            self.entries[0][0].grab_focus()

    def _fill(self, index: int, *, from_price: bool) -> None:
        """Either box fills the other: shares are the amount over the price."""
        if self._filling:
            return
        price, shares = self.entries[index]
        source, target = (price, shares) if from_price else (shares, price)
        if from_price:
            self.typed_shares.discard(index)
        else:
            self.typed_shares.add(index)
        typed = _amount(source.get_text())
        value = abs(self.legs[index].value)
        self._filling = True
        try:
            if typed is None:
                target.set_text("")
            else:
                other = Money(
                    value.numerator * typed.denominator, value.denominator * typed.numerator
                )
                target.set_text(price_text(other) if not from_price else _plain(other))
        finally:
            self._filling = False

    def prices(self) -> tuple[LegPrice, ...] | None:
        """What to send for each leg, or ``None`` (and a message) when one lacks a price."""
        found: list[LegPrice] = []
        for index, (leg, (price, shares)) in enumerate(zip(self.legs, self.entries, strict=True)):
            typed_shares = _amount(shares.get_text()) if index in self.typed_shares else None
            typed_price = _amount(price.get_text())
            if typed_shares is not None:
                found.append(LegPrice(leg.schedule, leg.when, leg.account, shares=typed_shares))
            elif typed_price is not None and typed_price != leg.price:
                found.append(LegPrice(leg.schedule, leg.when, leg.account, price=typed_price))
            elif typed_price is None:
                self.message.set_text(
                    f"Give a share price or the shares of {leg.mnemonic} for "
                    f"{leg.name} {leg.when.isoformat()}."
                )
                return None
        return tuple(found)

    def enter(self) -> bool:
        prices = self.prices()
        if prices is None:
            return False
        self._answered = True
        self.close()
        self.on_done(prices)
        return True

    def _on_close(self, _window) -> bool:
        if not self._answered:
            self._answered = True
            self.on_done(None)
        return False


class SharePricing:
    """One run of fetch-then-ask before entering; keeps its job and dialog alive."""

    def __init__(self) -> None:
        self.job: BackgroundJob = BackgroundJob()
        self.dialog: SharePricesDialog | None = None


def price_shares(
    parent: Gtk.Window | None,
    db: OpenBook,
    occurrences: Sequence[tuple[str, date]],
    proceed: Callable[[tuple[LegPrice, ...]], None],
    *,
    as_of: date | None = None,
    fetcher_factory: Callable[[], QuoteFetcher] | None = None,
    on_cancel: Callable[[], None] | None = None,
) -> SharePricing:
    """Price the shares these occurrences buy or sell, then call ``proceed``.

    With no such legs ``proceed`` runs at once with nothing to add. Cancelling
    the dialog calls ``on_cancel`` and enters nothing.
    """
    pricing = SharePricing()
    book = book_server(db)
    request = DueQuotes(tuple(occurrences), as_of)

    def ask(needs: DueQuoteNeeds, failures: tuple[QuoteFailure, ...]) -> None:
        if not needs.legs:
            proceed(())
            return

        def done(prices: tuple[LegPrice, ...] | None) -> None:
            if prices is None:
                if on_cancel is not None:
                    on_cancel()
                return
            proceed(prices)

        pricing.dialog = SharePricesDialog(parent, needs.legs, failures, done)
        pricing.dialog.present()

    if not occurrences:
        proceed(())
        return pricing
    needs = book.due_quotes(request)
    if not needs.requests or needs.currency is None:
        ask(needs, ())
        return pricing
    fetcher = (fetcher_factory or default_fetcher)()
    requests, currency = needs.requests, needs.currency

    def stored(fetched) -> None:
        quotes, failures = fetched
        try:
            update = book.store_quotes(StoreQuotes(tuple(quotes), tuple(failures)))
        except ApiError as error:
            failed(error)
            return
        ask(book.due_quotes(request), update.failures)

    def failed(error: BaseException) -> None:
        ask(
            book.due_quotes(request),
            tuple(QuoteFailure(item.symbol, item.source, str(error)) for item in requests),
        )

    # The network is reached off the main thread; the book is written on it.
    pricing.job.start(
        lambda _cancel, _report: fetcher.fetch(requests, currency),
        lambda _progress: None,
        stored,
        failed,
    )
    return pricing
