"""Online quotes against the real sources: tsp.gov, the ECB, Alpha Vantage, Finance::Quote.

These are the complete tests of the quote path: real downloads, real formats, real
Perl, and real storage through the service, CLI, and web adapter. They need open
internet access, so they run only with ``BREADSCHED_NETWORK_TESTS=1`` (the weekly
"Live quote sources" workflow sets it); elsewhere each is skipped with that reason.
Without ``ALPHAVANTAGE_API_KEY``, Alpha Vantage uses its public ``demo`` key, which
answers for IBM only. Finance::Quote tests also need ``perl`` with Finance::Quote.

Prices change daily, so the assertions check what must hold for any day: the
answer is a positive price in a plausible range, dated within the last few
business days, from the expected source, and stored exactly once.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import date, timedelta
from decimal import Decimal

import pytest

from breadsched.cli.main import main as cli
from breadsched.gen.db.sqlite import DbSQLite
from breadsched.gen.lib import Commodity, Money
from breadsched.gen.services.quotes import QuoteRequest, update_quotes
from breadsched.plugins.quotes import OnlineQuotes, finance_quote_status
from breadsched.plugins.quotes.fetch import _run_finance_quote
from breadsched.plugins.quotes.sources import parse_finance_quote

pytestmark = pytest.mark.network

TODAY = date.today()
#: Sources publish on business days; allow for weekends, holidays, and time zones.
RECENT = TODAY - timedelta(days=10)
TSP_FUNDS = ("G", "F", "C", "S", "I", "LINCOME", "L2050")
AV_KEY = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
AV_SYMBOL = os.environ.get("BREADSCHED_ALPHAVANTAGE_SYMBOL", "IBM" if not AV_KEY else "VTI")


def _finance_quote_installed() -> bool:
    return finance_quote_status()[0]


needs_finance_quote = pytest.mark.skipif(
    not _finance_quote_installed(), reason="Finance::Quote is not installed outside a Flatpak"
)


def _recent(when: date) -> None:
    assert RECENT <= when <= TODAY + timedelta(days=1), when


def _alpha_vantage_or_skip(failures) -> None:
    """A rate limit is the service's, not a defect; anything else fails the test."""
    for failure in failures:
        if (
            "rate limit" in failure.reason.lower()
            or "thank you for using" in failure.reason.lower()
        ):
            pytest.skip(f"Alpha Vantage rate limit: {failure.reason}")


class TestSources:
    def test_tsp_gov_prices_every_fund(self):
        quotes, failures = OnlineQuotes().fetch(
            [QuoteRequest(symbol, symbol, "tsp") for symbol in TSP_FUNDS], "USD"
        )
        assert failures == []
        assert sorted(quote.symbol for quote in quotes) == sorted(TSP_FUNDS)
        days = {quote.when for quote in quotes}
        assert len(days) == 1  # one row of the file: every fund priced the same day
        _recent(days.pop())
        for quote in quotes:
            assert quote.origin == "tsp.gov" and quote.currency == "USD"
            assert Decimal(1) < quote.value < Decimal(500), quote

    def test_tsp_gov_refuses_a_fund_it_does_not_list(self):
        quotes, failures = OnlineQuotes().fetch([QuoteRequest("x", "NOPE", "tsp")], "USD")
        assert quotes == []
        assert [failure.reason for failure in failures] == ["tsp.gov lists no such fund"]

    @pytest.mark.parametrize("reporting", ["USD", "EUR", "GBP"])
    def test_ecb_rates_cross_into_each_reporting_currency(self, reporting):
        symbols = [code for code in ("USD", "EUR", "GBP", "JPY", "CAD") if code != reporting]
        quotes, failures = OnlineQuotes().fetch(
            [QuoteRequest(code, code, "currency") for code in symbols], reporting
        )
        assert failures == []
        by_code = {quote.symbol: quote for quote in quotes}
        assert set(by_code) == set(symbols)
        for quote in quotes:
            _recent(quote.when)
            assert quote.origin == "ECB" and quote.currency == reporting
            assert quote.value > 0
        # Rates round-trip through the euro consistently: JPY is always far smaller.
        if "JPY" in by_code and "CAD" in by_code:
            assert by_code["JPY"].value < by_code["CAD"].value

    def test_ecb_refuses_a_currency_it_does_not_publish(self):
        quotes, failures = OnlineQuotes().fetch([QuoteRequest("x", "XXX", "currency")], "USD")
        assert quotes == [] and "publishes no XXX rate" in failures[0].reason

    def test_alpha_vantage_quotes_a_listed_symbol(self):
        fetcher = OnlineQuotes(alphavantage_key=AV_KEY or "demo")
        quotes, failures = fetcher.fetch([QuoteRequest("s", AV_SYMBOL, "alphavantage")], "USD")
        _alpha_vantage_or_skip(failures)
        assert failures == []
        [quote] = quotes
        _recent(quote.when)
        assert quote.origin == "Alpha Vantage" and quote.value > 0

    def test_alpha_vantage_reports_an_unknown_symbol(self):
        if not AV_KEY:
            pytest.skip("the demo key answers only for IBM; set ALPHAVANTAGE_API_KEY")
        fetcher = OnlineQuotes(alphavantage_key=AV_KEY)
        quotes, failures = fetcher.fetch(
            [QuoteRequest("s", "NOSUCHSYMBOLXYZ", "alphavantage")], "USD"
        )
        _alpha_vantage_or_skip(failures)
        assert quotes == [] and failures[0].reason


@needs_finance_quote
class TestFinanceQuote:
    def test_finance_quote_tsp_agrees_with_the_native_source(self):
        """The same tsp.gov file read two ways must give the same price and day."""
        native, failures = OnlineQuotes().fetch([QuoteRequest("g", "G", "tsp")], "USD")
        assert failures == []
        answers = parse_finance_quote(
            _run_finance_quote(json.dumps({"requests": [{"method": "tsp", "symbols": ["G"]}]}))
        )
        when, price, currency = answers[("tsp", "G")]
        assert (when, price, currency) == (native[0].when, native[0].value, "USD")

    def test_other_sources_are_fetched_through_finance_quote(self):
        """A source BreadSched does not fetch itself goes to Finance::Quote and back.

        Yahoo's JSON source by default; set ``BREADSCHED_FQ_METHOD`` and
        ``BREADSCHED_FQ_SYMBOL`` to check another (a source you rely on, for example).
        """
        method = os.environ.get("BREADSCHED_FQ_METHOD", "yahoo_json")
        symbol = os.environ.get("BREADSCHED_FQ_SYMBOL", "IBM")
        quotes, failures = OnlineQuotes().fetch([QuoteRequest("x", symbol, method)], "USD")
        if failures and "has no" in failures[0].reason:
            pytest.skip(failures[0].reason)
        assert failures == [], failures
        [quote] = quotes
        _recent(quote.when)
        assert quote.origin == f"Finance::Quote {method}" and quote.value > 0

    def test_an_unknown_method_is_reported_not_raised(self):
        quotes, failures = OnlineQuotes().fetch([QuoteRequest("x", "X", "no_such_method")], "USD")
        assert quotes == []
        assert failures[0].reason == "Finance::Quote has no 'no_such_method' source"


@pytest.fixture
def quoted_book(tmp_path):
    db = DbSQLite()
    db.load(str(tmp_path / "live.breadsched"))
    items = [
        Commodity(mnemonic="USD"),
        Commodity(mnemonic="GBP", quote_source="currency"),
        Commodity(namespace="TSP", mnemonic="G", fullname="G Fund", quote_source="tsp"),
        Commodity(namespace="TSP", mnemonic="C", fullname="C Fund", quote_source="tsp"),
    ]
    with db.transaction("Quoted commodities") as txn:
        for item in items:
            db.add_commodity(item, txn)
    yield db
    db.close()


def test_the_service_stores_live_quotes_once(quoted_book):
    db = quoted_book
    first = update_quotes(db, OnlineQuotes()).value
    assert first.failures == ()
    assert sorted(item.symbol for item in first.stored) == ["C", "G", "GBP"]
    assert all(item.changed for item in first.stored)
    for item in first.stored:
        _recent(item.when)
    sources = sorted(price.source for price in db.iter_prices())
    assert sources == ["Online: ECB", "Online: tsp.gov", "Online: tsp.gov"]
    # Fetching again the same day stores nothing new.
    again = update_quotes(db, OnlineQuotes()).value
    assert not any(item.changed for item in again.stored)
    assert len(list(db.iter_prices())) == 3
    # The fund is now valued from the stored quote.
    fund = db.get_commodity_by_mnemonic("G")
    [price] = [p for p in db.iter_prices(fund.handle)]
    assert price.value > Money(1)


def test_the_cli_fetches_and_stores_live_quotes(tmp_path, capsys):
    path = str(tmp_path / "cli.breadsched")
    cli(["init", path])
    db = DbSQLite()
    db.load(path)
    with db.transaction("Fund") as txn:
        db.add_commodity(Commodity(namespace="TSP", mnemonic="G", fullname="G Fund"), txn)
    db.close()
    assert cli(["quote-source", path, "G", "tsp"]) == 0
    capsys.readouterr()
    assert cli(["quotes", path, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["failures"] == []
    [stored] = result["stored"]
    assert stored["symbol"] == "G" and stored["source"] == "Online: tsp.gov"
    _recent(date.fromisoformat(stored["date"]))


def test_the_web_route_fetches_and_stores_live_quotes(tmp_path, monkeypatch):
    import threading
    import urllib.request

    from breadsched.gen.utils.settings import Settings
    from breadsched.web import quote_resource
    from breadsched.web.server import serve

    monkeypatch.setattr(quote_resource, "settings", lambda: Settings(directory=tmp_path / "cfg"))
    path = tmp_path / "web.breadsched"
    cli(["init", str(path)])
    db = DbSQLite()
    db.load(str(path))
    with db.transaction("Fund") as txn:
        db.add_commodity(
            Commodity(namespace="TSP", mnemonic="G", fullname="G Fund", quote_source="tsp"), txn
        )
    httpd = serve(db, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/api/quotes/update",
            data=b"{}",
            headers={"Content-Type": "application/json", "X-BreadSched-Token": httpd.token},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read())
    finally:
        httpd.shutdown()
        httpd.server_close()
        db.close()
    assert result["failures"] == []
    assert [item["source"] for item in result["stored"]] == ["Online: tsp.gov"]


@pytest.mark.skipif(shutil.which("breadsched") is None, reason="no installed breadsched")
def test_an_installed_breadsched_fetches_live_quotes(tmp_path):
    """The installed command (wheel, .deb, or .rpm) fetches as the source tree does."""
    path = tmp_path / "installed.breadsched"
    subprocess.run(["breadsched", "init", str(path)], check=True, capture_output=True)
    db = DbSQLite()
    db.load(str(path))
    with db.transaction("Fund") as txn:
        db.add_commodity(
            Commodity(namespace="TSP", mnemonic="G", fullname="G Fund", quote_source="tsp"), txn
        )
    db.close()
    finished = subprocess.run(
        ["breadsched", "quotes", str(path), "--json"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr
    assert json.loads(finished.stdout)["stored"][0]["source"] == "Online: tsp.gov"
