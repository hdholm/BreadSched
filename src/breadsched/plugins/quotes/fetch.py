"""Fetch online quotes: tsp.gov, the ECB, Alpha Vantage, and Finance::Quote.

:class:`OnlineQuotes` is the :class:`~breadsched.gen.services.quotes.QuoteFetcher`
every interface passes to ``update_quotes``. It asks each source once per update
(the TSP and ECB files cover every fund and currency at once), never retries in a
loop, and turns every network or format problem into a per-symbol failure, so one
broken source never stops the others.

Finance::Quote is used only when it is installed outside a Flatpak sandbox: it runs
as ``perl`` with :data:`~.sources.FINANCE_QUOTE_SCRIPT`, which reads the request as
JSON and prints JSON. Any quote source BreadSched does not fetch itself goes there.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from decimal import Decimal, localcontext

from ...gen.services.quotes import NATIVE_SOURCES, FetchedQuote, QuoteFailure, QuoteRequest
from .sources import (
    FINANCE_QUOTE_SCRIPT,
    QuoteSourceError,
    parse_alphavantage,
    parse_ecb,
    parse_finance_quote,
    parse_tsp,
    tsp_fund_key,
)

__all__ = [
    "ALPHAVANTAGE_KEY_SETTING",
    "OnlineQuotes",
    "alphavantage_key",
    "finance_quote_status",
    "save_alphavantage_key",
]

#: ``settings.ini`` section and key holding the user's Alpha Vantage API key; the
#: ``ALPHAVANTAGE_API_KEY`` environment variable (what Finance::Quote reads) also works.
ALPHAVANTAGE_KEY_SETTING = ("quotes", "alphavantage_api_key")

TSP_URL = "https://www.tsp.gov/data/fund-price-history.csv"
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ALPHAVANTAGE_URL = "https://www.alphavantage.co/query"
_TIMEOUT = 30
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) BreadSched"

Download = Callable[[str], str]


def _download(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310 - fixed https URLs
        return response.read().decode("utf-8-sig", errors="replace")


def alphavantage_key(settings) -> str:
    """The Alpha Vantage key from ``settings`` (a ``Settings``), else the environment."""
    section, key = ALPHAVANTAGE_KEY_SETTING
    return (settings.get(section, key) or os.environ.get("ALPHAVANTAGE_API_KEY", "")).strip()


def save_alphavantage_key(settings, value: str) -> bool:
    """Store (or, when empty, remove) the key in ``settings``; False if it cannot be saved."""
    section, key = ALPHAVANTAGE_KEY_SETTING
    if value.strip():
        settings.set(section, key, value.strip())
    else:
        settings.remove(section, key)
    if not settings.save():
        return False
    try:
        # The key is a credential: only its owner may read the settings file.
        os.chmod(settings.path, 0o600)
    except OSError:
        pass
    return True


def finance_quote_status() -> tuple[bool, str]:
    """Whether Finance::Quote can be used here, and if not, why.

    When it can, the text names the installed version: quote websites change
    often, and an old Finance::Quote is the usual reason its sources fail.
    """
    if os.environ.get("FLATPAK_ID"):
        return False, "Finance::Quote is not available in the Flatpak"
    perl = shutil.which("perl")
    if perl is None:
        return False, "Finance::Quote needs Perl, which is not installed"
    try:
        found = subprocess.run(  # noqa: S603 - fixed arguments
            [perl, "-MFinance::Quote", "-MJSON::PP", "-e", "print $Finance::Quote::VERSION"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, "Finance::Quote could not be started"
    if found.returncode != 0:
        return False, "Finance::Quote is not installed"
    version = found.stdout.strip()
    if version and all(part.isdigit() for part in version.split(".")):
        return True, f"Finance::Quote {version} is installed"
    return True, "Finance::Quote is installed"


class OnlineQuotes:
    """The real quote fetcher; ``download`` and ``run_perl`` are replaceable for tests."""

    def __init__(
        self,
        *,
        alphavantage_key: str | None = None,
        download: Download = _download,
        run_perl: Callable[[str], str] | None = None,
        today: date | None = None,
    ) -> None:
        self.alphavantage_key = (
            alphavantage_key or os.environ.get("ALPHAVANTAGE_API_KEY", "")
        ).strip()
        self.download = download
        self.run_perl = run_perl
        self.today = today or date.today()

    def fetch(
        self, requests: Sequence[QuoteRequest], reporting_currency: str
    ) -> tuple[list[FetchedQuote], list[QuoteFailure]]:
        quotes: list[FetchedQuote] = []
        failures: list[QuoteFailure] = []
        groups: dict[str, list[QuoteRequest]] = {}
        for request in requests:
            groups.setdefault(request.source, []).append(request)
        for source, members in sorted(groups.items()):
            handler = {
                "tsp": self._tsp,
                "currency": lambda items: self._ecb(items, reporting_currency),
                "alphavantage": self._alphavantage,
            }.get(source)
            if handler is None:
                continue
            got, failed = handler(members)
            quotes.extend(got)
            failures.extend(failed)
        others = [item for item in requests if item.source not in NATIVE_SOURCES]
        if others:
            got, failed = self._finance_quote(others)
            quotes.extend(got)
            failures.extend(failed)
        return quotes, failures

    # ------------------------------------------------------------------ sources

    @staticmethod
    def _fail(items: Sequence[QuoteRequest], reason: str) -> list[QuoteFailure]:
        return [QuoteFailure(item.symbol, item.source, reason) for item in items]

    def _get(self, url: str, what: str) -> str:
        try:
            return self.download(url)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise QuoteSourceError(f"{what} could not be reached ({exc})") from exc

    def _tsp(self, items: Sequence[QuoteRequest]):
        query = urllib.parse.urlencode(
            {
                "startdate": (self.today - timedelta(days=14)).isoformat(),
                "enddate": self.today.isoformat(),
                "Lfunds": 1,
                "InvFunds": 1,
                "download": 1,
            }
        )
        try:
            when, prices = parse_tsp(self._get(f"{TSP_URL}?{query}", "tsp.gov"))
        except QuoteSourceError as exc:
            return [], self._fail(items, str(exc))
        quotes, failures = [], []
        for item in items:
            price = prices.get(tsp_fund_key(item.symbol))
            if price is None:
                failures.append(QuoteFailure(item.symbol, "tsp", "tsp.gov lists no such fund"))
            else:
                quotes.append(FetchedQuote(item.symbol, "tsp", price, "USD", when, "tsp.gov"))
        return quotes, failures

    def _ecb(self, items: Sequence[QuoteRequest], reporting: str):
        try:
            when, rates = parse_ecb(self._get(ECB_URL, "The ECB"))
        except QuoteSourceError as exc:
            return [], self._fail(items, str(exc))
        target = rates.get(reporting.upper())
        if target is None:
            return [], self._fail(items, f"the ECB publishes no {reporting} rate")
        quotes, failures = [], []
        for item in items:
            per_euro = rates.get(item.symbol.upper())
            if per_euro is None:
                failures.append(
                    QuoteFailure(
                        item.symbol, "currency", f"the ECB publishes no {item.symbol} rate"
                    )
                )
                continue
            # One unit of the currency in the reporting currency, via the euro.
            with localcontext() as context:
                context.prec = 28
                value = (target / per_euro).quantize(Decimal("0.0000000001"))
            quotes.append(FetchedQuote(item.symbol, "currency", value, reporting, when, "ECB"))
        return quotes, failures

    def _alphavantage(self, items: Sequence[QuoteRequest]):
        if not self.alphavantage_key:
            return [], self._fail(
                items, "an Alpha Vantage API key is needed (Settings, or ALPHAVANTAGE_API_KEY)"
            )
        quotes, failures = [], []
        for item in items:
            query = urllib.parse.urlencode(
                {"function": "GLOBAL_QUOTE", "symbol": item.symbol, "apikey": self.alphavantage_key}
            )
            try:
                when, price = parse_alphavantage(
                    self._get(f"{ALPHAVANTAGE_URL}?{query}", "Alpha Vantage")
                )
            except QuoteSourceError as exc:
                failures.append(QuoteFailure(item.symbol, "alphavantage", str(exc)))
                continue
            # Alpha Vantage does not say which currency; like Finance::Quote, take US
            # dollars, the currency of the exchanges it covers by default.
            quotes.append(
                FetchedQuote(item.symbol, "alphavantage", price, "USD", when, "Alpha Vantage")
            )
        return quotes, failures

    def _finance_quote(self, items: Sequence[QuoteRequest]):
        run = self.run_perl
        if run is None:
            available, reason = finance_quote_status()
            if not available:
                return [], self._fail(
                    items, f"{reason}, so the {items[0].source!r} source is not available"
                )
            run = _run_finance_quote
        groups: dict[str, list[str]] = {}
        for item in items:
            groups.setdefault(item.source, []).append(item.symbol)
        request = {"requests": [{"method": m, "symbols": s} for m, s in sorted(groups.items())]}
        try:
            answers = parse_finance_quote(run(json.dumps(request)))
        except (QuoteSourceError, OSError, subprocess.SubprocessError) as exc:
            return [], self._fail(items, f"Finance::Quote failed ({exc})")
        quotes, failures = [], []
        for item in items:
            answer = answers.get((item.source, item.symbol), "Finance::Quote returned nothing")
            if isinstance(answer, str):
                failures.append(QuoteFailure(item.symbol, item.source, answer))
                continue
            when, price, currency = answer
            quotes.append(
                FetchedQuote(
                    item.symbol, item.source, price, currency, when, f"Finance::Quote {item.source}"
                )
            )
        return quotes, failures


def _run_finance_quote(request: str) -> str:
    perl = shutil.which("perl") or "perl"
    finished = subprocess.run(  # noqa: S603 - fixed arguments; the request goes on stdin
        [perl, "-e", FINANCE_QUOTE_SCRIPT],
        input=request,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if finished.returncode != 0:
        lines = finished.stderr.strip().splitlines()
        raise subprocess.SubprocessError(lines[-1] if lines else "perl failed")
    return finished.stdout
