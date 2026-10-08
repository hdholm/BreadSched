"""Online quotes: tsp.gov, the ECB, Alpha Vantage, Finance::Quote, and their storage."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from datetime import date
from decimal import Decimal

import pytest
from gnucash_fixtures import create_book

from breadsched.cli.main import main as cli
from breadsched.gen.lib import Commodity, Money
from breadsched.gen.services.quotes import (
    FetchedQuote,
    QuoteFailure,
    QuoteRequest,
    SetQuoteSource,
    quote_requests,
    set_quote_source,
    update_quotes,
)
from breadsched.plugins.importer import gnucash_sqlite
from breadsched.plugins.quotes import OnlineQuotes, finance_quote_status
from breadsched.plugins.quotes.sources import (
    FINANCE_QUOTE_SCRIPT,
    QuoteSourceError,
    parse_alphavantage,
    parse_ecb,
    parse_finance_quote,
    parse_tsp,
    tsp_fund_key,
)

TSP_CSV = (
    "Date,L Income,L 2050,G Fund,F Fund,C Fund,S Fund,I Fund\n"
    "2026-10-07,26.1234,35.5555,18.4521,19.0102,98.7654,85.4321,45.6789\n"
    "2026-10-06,26.1000,35.5000,18.4500,19.0000,98.0000,85.0000,45.0000\n"
)

ECB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
  xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <gesmes:subject>Reference rates</gesmes:subject>
  <Cube><Cube time="2026-10-07">
    <Cube currency="USD" rate="1.1000"/><Cube currency="GBP" rate="0.8800"/>
    <Cube currency="JPY" rate="160.00"/>
  </Cube></Cube>
</gesmes:Envelope>"""

AV_QUOTE = json.dumps(
    {
        "Global Quote": {
            "01. symbol": "VTSAX",
            "05. price": "131.4500",
            "07. latest trading day": "2026-10-07",
        }
    }
)


class TestParsers:
    def test_tsp_reads_the_latest_day_for_every_fund(self):
        when, prices = parse_tsp(TSP_CSV)
        assert when == date(2026, 10, 7)
        assert prices["g"] == Decimal("18.4521")
        assert prices["l2050"] == Decimal("35.5555")
        assert prices["lincome"] == Decimal("26.1234")
        # Older rows first make no difference.
        lines = TSP_CSV.strip().splitlines()
        assert parse_tsp("\n".join([lines[0], lines[2], lines[1]])) == (when, prices)

    def test_tsp_fund_names(self):
        assert {tsp_fund_key(name) for name in ("G Fund", "g", "GFUND", "G fund")} == {"g"}
        assert tsp_fund_key("L 2050") == "l2050"

    @pytest.mark.parametrize("text", ["", "Date,G Fund\n", "Fund,G Fund\nx,1\n", "<html>"])
    def test_tsp_refuses_what_is_not_a_price_file(self, text):
        with pytest.raises(QuoteSourceError):
            parse_tsp(text)

    def test_ecb_rates_per_euro(self):
        when, rates = parse_ecb(ECB_XML)
        assert when == date(2026, 10, 7)
        assert rates == {
            "EUR": Decimal(1),
            "USD": Decimal("1.1000"),
            "GBP": Decimal("0.8800"),
            "JPY": Decimal("160.00"),
        }
        with pytest.raises(QuoteSourceError):
            parse_ecb("<html>not rates</html>")

    def test_alpha_vantage_quote_and_its_refusals(self):
        assert parse_alphavantage(AV_QUOTE) == (date(2026, 10, 7), Decimal("131.4500"))
        for answer in (
            {"Note": "Thank you for using Alpha Vantage! Our standard API rate limit is 25"},
            {"Error Message": "Invalid API call."},
            {"Information": "The demo API key is for demo purposes only."},
            {"Global Quote": {}},
        ):
            with pytest.raises(QuoteSourceError) as caught:
                parse_alphavantage(json.dumps(answer))
            if len(answer) == 1 and "Global Quote" not in answer:
                assert next(iter(answer.values())) in str(caught.value)

    def test_finance_quote_answers(self):
        answers = parse_finance_quote(
            json.dumps(
                {
                    "vanguard": {
                        "VFIAX": {
                            "success": True,
                            "nav": "512.30",
                            "isodate": "2026-10-07",
                            "currency": "usd",
                        },
                        "NOPE": {"success": False, "errormsg": "Invalid symbol"},
                    }
                }
            )
        )
        assert answers[("vanguard", "VFIAX")] == (date(2026, 10, 7), Decimal("512.30"), "USD")
        assert answers[("vanguard", "NOPE")] == "Invalid symbol"


class _Web:
    def __init__(self, **pages):
        self.pages = pages
        self.asked: list[str] = []

    def __call__(self, url: str) -> str:
        self.asked.append(url)
        for marker, page in self.pages.items():
            if marker in url:
                if isinstance(page, Exception):
                    raise page
                return page
        raise OSError("no route")


class TestOnlineQuotes:
    def test_each_source_is_asked_once_and_tsp_matches_funds(self):
        web = _Web(tsp=TSP_CSV, ecb=ECB_XML)
        fetcher = OnlineQuotes(download=web, today=date(2026, 10, 8))
        quotes, failures = fetcher.fetch(
            [
                QuoteRequest("g", "G", "tsp"),
                QuoteRequest("l", "L2050", "tsp"),
                QuoteRequest("x", "Q", "tsp"),
                QuoteRequest("gbp", "GBP", "currency"),
            ],
            "USD",
        )
        assert len(web.asked) == 2
        assert "startdate=2026-09-24" in web.asked[1] or "startdate=2026-09-24" in web.asked[0]
        by_symbol = {quote.symbol: quote for quote in quotes}
        assert by_symbol["G"].value == Decimal("18.4521")
        assert by_symbol["G"].origin == "tsp.gov" and by_symbol["G"].currency == "USD"
        assert by_symbol["L2050"].value == Decimal("35.5555")
        # A pound in dollars, through the euro: 1.10 / 0.88.
        assert by_symbol["GBP"].value == Decimal("1.2500000000")
        assert by_symbol["GBP"].currency == "USD" and by_symbol["GBP"].origin == "ECB"
        assert failures == [QuoteFailure("Q", "tsp", "tsp.gov lists no such fund")]

    def test_an_unreachable_source_fails_its_symbols_only(self):
        web = _Web(tsp=OSError("connection refused"), ecb=ECB_XML)
        quotes, failures = OnlineQuotes(download=web).fetch(
            [QuoteRequest("g", "G", "tsp"), QuoteRequest("gbp", "GBP", "currency")], "EUR"
        )
        assert [quote.symbol for quote in quotes] == ["GBP"]
        assert quotes[0].value == Decimal("1.1363636364")  # 1 / 0.88 euros per pound
        assert failures[0].symbol == "G" and "could not be reached" in failures[0].reason

    def test_alpha_vantage_needs_a_key(self, monkeypatch):
        monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
        request = [QuoteRequest("v", "VTSAX", "alphavantage")]
        quotes, failures = OnlineQuotes(download=_Web()).fetch(request, "USD")
        assert quotes == [] and "API key" in failures[0].reason
        web = _Web(alphavantage=AV_QUOTE)
        quotes, failures = OnlineQuotes(alphavantage_key="k", download=web).fetch(request, "USD")
        assert failures == [] and quotes[0].value == Decimal("131.4500")
        assert "apikey=k" in web.asked[0] and "symbol=VTSAX" in web.asked[0]

    def test_other_sources_go_to_finance_quote(self):
        seen = []

        def perl(request: str) -> str:
            seen.append(json.loads(request))
            return json.dumps(
                {
                    "vanguard": {
                        "VFIAX": {
                            "success": True,
                            "last": "512.30",
                            "isodate": "2026-10-07",
                            "currency": "USD",
                        }
                    }
                }
            )

        quotes, failures = OnlineQuotes(download=_Web(), run_perl=perl).fetch(
            [QuoteRequest("v", "VFIAX", "vanguard")], "USD"
        )
        assert seen == [{"requests": [{"method": "vanguard", "symbols": ["VFIAX"]}]}]
        assert failures == [] and quotes[0].origin == "Finance::Quote vanguard"

    def test_finance_quote_is_off_in_the_flatpak(self, monkeypatch):
        monkeypatch.setenv("FLATPAK_ID", "org.breadsched.BreadSched")
        assert finance_quote_status() == (False, "Finance::Quote is not available in the Flatpak")
        quotes, failures = OnlineQuotes(download=_Web()).fetch(
            [QuoteRequest("v", "VFIAX", "vanguard")], "USD"
        )
        assert quotes == [] and "Flatpak" in failures[0].reason

    @pytest.mark.parametrize(
        ("returncode", "stdout", "expected"),
        [
            (0, "1.71", (True, "Finance::Quote 1.71 is installed")),
            (0, "1.59\n", (True, "Finance::Quote 1.59 is installed")),
            (0, "", (True, "Finance::Quote is installed")),
            (0, "1.71; rm -rf /", (True, "Finance::Quote is installed")),
            (2, "", (False, "Finance::Quote is not installed")),
        ],
    )
    def test_the_status_names_the_installed_version(
        self, monkeypatch, returncode, stdout, expected
    ):
        """An old Finance::Quote is the usual reason its sources fail, so say which."""
        from breadsched.plugins.quotes import fetch

        monkeypatch.delenv("FLATPAK_ID", raising=False)
        monkeypatch.setattr(fetch.shutil, "which", lambda name: "/usr/bin/perl")
        seen = []

        def run(args, **kwargs):
            seen.append(args)
            return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

        monkeypatch.setattr(fetch.subprocess, "run", run)
        assert finance_quote_status() == expected
        assert seen[0][:3] == ["/usr/bin/perl", "-MFinance::Quote", "-MJSON::PP"]

    @pytest.mark.skipif(
        shutil.which("perl") is None
        or subprocess.run(["perl", "-MFinance::Quote", "-e", "1"], check=False).returncode,
        reason="Finance::Quote is not installed",
    )
    def test_the_real_status_reports_a_version(self, monkeypatch):
        monkeypatch.delenv("FLATPAK_ID", raising=False)
        available, text = finance_quote_status()
        assert available and text.startswith("Finance::Quote ") and text[15].isdigit()

    @pytest.mark.skipif(
        shutil.which("perl") is None
        or subprocess.run(["perl", "-MFinance::Quote", "-e", "1"], check=False).returncode,
        reason="Finance::Quote is not installed",
    )
    def test_the_finance_quote_script_runs_in_real_perl(self):
        finished = subprocess.run(
            ["perl", "-e", FINANCE_QUOTE_SCRIPT],
            input=json.dumps({"requests": [{"method": "no_such_method", "symbols": ["X"]}]}),
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        answers = parse_finance_quote(finished.stdout)
        assert answers[("no_such_method", "X")] == "Finance::Quote has no 'no_such_method' source"


class _Fixed:
    def __init__(self, quotes=(), failures=()):
        self.quotes = list(quotes)
        self.failures = list(failures)
        self.requests: list[QuoteRequest] = []

    def fetch(self, requests, reporting_currency):
        self.requests = list(requests)
        self.reporting = reporting_currency
        return self.quotes, self.failures


@pytest.fixture
def commodities(db):
    items = {
        "usd": Commodity(mnemonic="USD"),
        "gbp": Commodity(mnemonic="GBP", quote_source="currency"),
        "g": Commodity(namespace="TSP", mnemonic="G", fraction=10000, quote_source="tsp"),
        "fund": Commodity(namespace="FUND", mnemonic="VTSAX", fraction=10000),
    }
    with db.transaction("Commodities") as txn:
        for item in items.values():
            db.add_commodity(item, txn)
    return items


DAY = date(2026, 10, 7)


class TestUpdateQuotes:
    def test_requests_cover_quoted_commodities_only(self, db, commodities):
        assert quote_requests(db) == [
            QuoteRequest(commodities["gbp"].handle, "GBP", "currency"),
            QuoteRequest(commodities["g"].handle, "G", "tsp"),
        ]

    def test_quotes_are_stored_with_their_origin_and_not_duplicated(self, db, commodities):
        fetcher = _Fixed(
            [
                FetchedQuote("G", "tsp", Decimal("18.4521"), "USD", DAY, "tsp.gov"),
                FetchedQuote("GBP", "currency", Decimal("1.25"), "USD", DAY, "ECB"),
            ],
            [QuoteFailure("X", "tsp", "tsp.gov lists no such fund")],
        )
        update = update_quotes(db, fetcher).value
        assert fetcher.reporting == "USD"
        assert [(item.symbol, item.changed) for item in update.stored] == [
            ("G", True),
            ("GBP", True),
        ]
        assert update.failures == (QuoteFailure("X", "tsp", "tsp.gov lists no such fund"),)
        [price] = db.iter_prices(commodities["g"].handle)
        assert (price.value, price.quote_date, price.source, price.quote_type) == (
            Money("18.4521"),
            DAY,
            "Online: tsp.gov",
            "last",
        )
        assert price.currency == commodities["usd"].handle
        # The same quote again changes nothing; a corrected one updates in place.
        again = update_quotes(db, fetcher).value
        assert [item.changed for item in again.stored] == [False, False]
        fetcher.quotes[0] = FetchedQuote("G", "tsp", Decimal("18.5"), "USD", DAY, "tsp.gov")
        update_quotes(db, fetcher)
        assert [p.value for p in db.iter_prices(commodities["g"].handle)] == [Money("18.5")]

    def test_a_dry_run_and_unusable_quotes_store_nothing(self, db, commodities):
        fetcher = _Fixed(
            [
                FetchedQuote("G", "tsp", Decimal("18"), "USD", DAY, "tsp.gov"),
                FetchedQuote("GBP", "currency", Decimal("1.2"), "CHF", DAY, "ECB"),
            ]
        )
        update = update_quotes(db, fetcher, dry_run=True).value
        assert update.dry_run and [item.symbol for item in update.stored] == ["G"]
        assert update.failures[0].reason == "currency CHF is not in the book"
        assert list(db.iter_prices()) == []

    def test_quote_sources_are_chosen_and_checked(self, db, commodities):
        fund = commodities["fund"].handle
        assert set_quote_source(db, SetQuoteSource(fund, "alphavantage")).value == "alphavantage"
        assert db.get_commodity(fund).quote_source == "alphavantage"
        for source, code in (
            ("two words", "quotes.source.invalid"),
            ("currency", "quotes.source.currency"),
        ):
            result = set_quote_source(db, SetQuoteSource(fund, source))
            assert result.errors[0].code == code
        gbp = commodities["gbp"].handle
        assert set_quote_source(db, SetQuoteSource(gbp, "tsp")).errors[0].code == (
            "quotes.source.currency"
        )
        assert set_quote_source(db, SetQuoteSource(fund, "")).value == ""
        assert "quote_source" not in db.get_commodity(fund).serialize()
        missing = set_quote_source(db, SetQuoteSource("missing", "tsp"))
        assert missing.errors[0].code == "quotes.commodity.not_found"


def test_gnucash_import_carries_quote_sources(db, tmp_path):
    book = create_book(
        tmp_path / "quoted.gnucash",
        [("root", "Root Account", "ROOT", None, 0), ("bank", "Checking", "BANK", "root", 0)],
    )
    conn = sqlite3.connect(book.path)
    with conn:
        conn.execute(
            "INSERT INTO commodities VALUES (?,?,?,?,?,?,?,?,?)",
            ("a" * 32, "TSP", "G", "G Fund", None, 10000, 1, "tsp", None),
        )
        conn.execute(
            "INSERT INTO commodities VALUES (?,?,?,?,?,?,?,?,?)",
            ("b" * 32, "FUND", "OLD", "Old fund", None, 10000, 0, "vanguard", None),
        )
    conn.close()
    gnucash_sqlite.import_book(db, book.path)
    sources = {item.mnemonic: item.quote_source for item in db.iter_commodities()}
    assert sources["G"] == "tsp"
    assert sources["OLD"] == ""  # GnuCash does not fetch quotes for it
    conn = sqlite3.connect(book.path)
    with conn:
        conn.execute("UPDATE commodities SET quote_flag=0 WHERE mnemonic='G'")
    conn.close()
    gnucash_sqlite.import_book(db, book.path)
    assert {item.mnemonic: item.quote_source for item in db.iter_commodities()}["G"] == ""


def test_cli_lists_sets_and_fetches(tmp_path, capsys, monkeypatch):
    from breadsched.cli import quote_commands

    path = str(tmp_path / "book.breadsched")
    cli(["init", path])
    db_path = path
    from breadsched.gen.db.sqlite import DbSQLite

    db = DbSQLite()
    db.load(db_path)
    with db.transaction("Fund") as txn:
        db.add_commodity(Commodity(namespace="TSP", mnemonic="G", fraction=10000), txn)
    db.close()
    capsys.readouterr()

    assert cli(["quote-source", path, "G", "tsp"]) == 0
    assert "online quotes from tsp" in capsys.readouterr().out
    assert cli(["quotes", path, "--list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["requests"] == [{"symbol": "G", "source": "tsp"}]

    fetcher = _Fixed([FetchedQuote("G", "tsp", Decimal("18.4521"), "USD", DAY, "tsp.gov")])
    monkeypatch.setattr(quote_commands, "_fetcher", lambda: fetcher)
    assert cli(["quotes", path, "--dry-run", "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True and preview["stored"][0]["price"] == "18.4521"
    assert cli(["quotes", path]) == 0
    assert "stored" in capsys.readouterr().out
    db = DbSQLite()
    db.load(db_path, mode="r")
    try:
        assert [price.source for price in db.iter_prices()] == ["Online: tsp.gov"]
    finally:
        db.close()

    monkeypatch.setattr(
        quote_commands, "_fetcher", lambda: _Fixed(failures=[QuoteFailure("G", "tsp", "down")])
    )
    assert cli(["quotes", path]) == 1
    assert "G (tsp): down" in capsys.readouterr().out
