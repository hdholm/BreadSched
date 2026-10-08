"""Read what each online quote source returns, without touching the network.

Every parser takes the text a source sent and returns prices or raises
:class:`QuoteSourceError` with a reason a person can act on. The formats follow
the sources themselves and the Finance::Quote modules that read them:

* ``tsp``: the Thrift Savings Plan's share-price history CSV from tsp.gov, one
  row per business day and one column per fund (``G Fund``, ``L 2050``...).
* ``currency``: the European Central Bank's daily reference rates XML, each rate
  in units of the currency per euro.
* ``alphavantage``: Alpha Vantage's ``GLOBAL_QUOTE`` JSON.
* Finance::Quote: the JSON that :data:`FINANCE_QUOTE_SCRIPT` prints.
"""

from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal, InvalidOperation

__all__ = [
    "FINANCE_QUOTE_SCRIPT",
    "QuoteSourceError",
    "parse_alphavantage",
    "parse_ecb",
    "parse_finance_quote",
    "parse_tsp",
    "tsp_fund_key",
]


class QuoteSourceError(ValueError):
    """A source answered with something that is not a usable price."""


def _decimal(text: str, what: str) -> Decimal:
    cleaned = re.sub(r"[^0-9.\-]", "", text or "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise QuoteSourceError(f"{what} is not a number: {text!r}") from exc
    if not value.is_finite() or value <= 0:
        raise QuoteSourceError(f"{what} is not a price: {text!r}")
    return value


def _date(text: str, what: str) -> date:
    try:
        return date.fromisoformat((text or "").strip()[:10])
    except ValueError as exc:
        raise QuoteSourceError(f"{what} has no date: {text!r}") from exc


def tsp_fund_key(name: str) -> str:
    """``G Fund``, ``g``, and ``GFUND`` all name the G fund; ``L 2050`` is ``l2050``."""
    key = re.sub(r"\s+", "", name).lower()
    match = re.fullmatch(r"(.)fund", key)
    return match.group(1) if match else key


def parse_tsp(text: str) -> tuple[date, dict[str, Decimal]]:
    """The latest day's price for every fund in the CSV, keyed by :func:`tsp_fund_key`."""
    rows = [row for row in csv.reader(io.StringIO(text.lstrip("﻿"))) if any(row)]
    if len(rows) < 2:
        raise QuoteSourceError("tsp.gov sent no prices")
    header = [cell.strip() for cell in rows[0]]
    keys = [tsp_fund_key(cell) for cell in header]
    if "date" not in keys:
        raise QuoteSourceError("tsp.gov sent a file without a Date column")
    day_column = keys.index("date")
    dated = []
    for row in rows[1:]:
        try:
            dated.append((_date(row[day_column], "a tsp.gov row"), row))
        except (QuoteSourceError, IndexError):
            continue
    if not dated:
        raise QuoteSourceError("tsp.gov sent no dated prices")
    when, latest = max(dated, key=lambda item: item[0])
    prices: dict[str, Decimal] = {}
    for index, key in enumerate(keys):
        if index == day_column or index >= len(latest) or not latest[index].strip():
            continue
        try:
            prices[key] = _decimal(latest[index], header[index])
        except QuoteSourceError:
            continue
    return when, prices


def parse_ecb(text: str) -> tuple[date, dict[str, Decimal]]:
    """Units of each currency per euro on the reference date (the euro itself is 1)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise QuoteSourceError("the ECB sent something that is not its rates file") from exc
    when = None
    rates: dict[str, Decimal] = {"EUR": Decimal(1)}
    for element in root.iter():
        if element.tag.endswith("Cube"):
            if "time" in element.attrib:
                when = _date(element.attrib["time"], "the ECB rates")
            if "currency" in element.attrib and "rate" in element.attrib:
                currency = element.attrib["currency"].strip().upper()
                rates[currency] = _decimal(element.attrib["rate"], f"the ECB {currency} rate")
    if when is None or len(rates) == 1:
        raise QuoteSourceError("the ECB sent no rates")
    return when, rates


def parse_alphavantage(text: str) -> tuple[date, Decimal]:
    """The latest trading day's price from a ``GLOBAL_QUOTE`` answer."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QuoteSourceError("Alpha Vantage sent something that is not JSON") from exc
    if not isinstance(data, dict):
        raise QuoteSourceError("Alpha Vantage sent an unexpected answer")
    for key in ("Error Message", "Note", "Information"):
        if data.get(key):
            # A bad symbol, a rate limit, or a key problem, in Alpha Vantage's words.
            raise QuoteSourceError(f"Alpha Vantage: {data[key]}")
    quote = data.get("Global Quote")
    if not isinstance(quote, dict) or not quote:
        raise QuoteSourceError("Alpha Vantage has no quote for this symbol")
    return (
        _date(str(quote.get("07. latest trading day", "")), "the Alpha Vantage quote"),
        _decimal(str(quote.get("05. price", "")), "the Alpha Vantage price"),
    )


#: Read ``{"requests": [{"method": ..., "symbols": [...]}]}`` on standard input and
#: print each symbol's result as JSON, using Finance::Quote's own field names.
FINANCE_QUOTE_SCRIPT = r"""
use strict;
use warnings;
use Finance::Quote;
use JSON::PP;
my $request = JSON::PP::decode_json(do { local $/; <STDIN> });
my $quoter = Finance::Quote->new();
$quoter->timeout(60);
my %known = map { $_ => 1 } $quoter->sources;
my %result;
for my $group (@{ $request->{requests} }) {
    my $method = $group->{method};
    my @symbols = @{ $group->{symbols} };
    unless ($known{$method}) {
        $result{$method}{$_} = {success => JSON::PP::false,
            errormsg => "Finance::Quote has no '$method' source"} for @symbols;
        next;
    }
    my %info = eval { $quoter->fetch($method, @symbols) };
    my $error = $@;
    for my $symbol (@symbols) {
        my %one = (success => ($info{$symbol, 'success'} ? JSON::PP::true : JSON::PP::false));
        for my $field (qw(last nav price isodate currency errormsg)) {
            $one{$field} = $info{$symbol, $field} if defined $info{$symbol, $field};
        }
        $one{errormsg} = $error if $error && !$info{$symbol, 'success'};
        $result{$method}{$symbol} = \%one;
    }
}
print JSON::PP::encode_json(\%result);
"""


def parse_finance_quote(
    text: str,
) -> dict[tuple[str, str], tuple[date, Decimal, str] | str]:
    """``(method, symbol)`` → ``(date, price, currency)``, or the reason it failed."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QuoteSourceError("Finance::Quote sent something that is not JSON") from exc
    if not isinstance(data, dict):
        raise QuoteSourceError("Finance::Quote sent an unexpected answer")
    result: dict[tuple[str, str], tuple[date, Decimal, str] | str] = {}
    for method, symbols in data.items():
        if not isinstance(symbols, dict):
            continue
        for symbol, info in symbols.items():
            if not isinstance(info, dict) or not info.get("success"):
                reason = info.get("errormsg") if isinstance(info, dict) else None
                result[(method, symbol)] = str(reason or "Finance::Quote found no price")
                continue
            try:
                raw = info.get("last") or info.get("nav") or info.get("price") or ""
                price = _decimal(str(raw), f"the {method} price")
                when = _date(str(info.get("isodate", "")), f"the {method} quote")
            except QuoteSourceError as exc:
                result[(method, symbol)] = str(exc)
                continue
            currency = str(info.get("currency") or "").strip().upper()
            if not currency:
                result[(method, symbol)] = "Finance::Quote did not say which currency"
                continue
            result[(method, symbol)] = (when, price, currency)
    return result
