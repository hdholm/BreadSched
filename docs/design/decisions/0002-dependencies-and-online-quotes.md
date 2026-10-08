# 0002: Third-party packages outside the core, and where online quotes come from

Part of the [BreadSched design](../../../DESIGN.md); see
[Architecture and ownership](../architecture.md).

- **Status:** accepted, 2026-10-08
- **Decision:** the financial core stays standard-library only; adapters and
  presentations may use third-party packages when a decision record justifies each
  one. Online quotes come from native fetchers for tsp.gov, the European Central
  Bank, and Alpha Vantage, plus an optional bridge to an installed Finance::Quote
  outside the Flatpak. Fedora RPM and Ubuntu DEB packages are added so that most
  Linux users can use that bridge.

## Context

Until now nothing below the GUI could import anything but the standard library.
That kept the engines easy to audit and every package easy to build. But it also
ruled out maintained libraries for jobs where the maintenance is the hard part,
such as quote sources that change their formats.

The maintainer needs Thrift Savings Plan (TSP) fund prices and would rather not
maintain a long tail of quote scrapers. No Python library matches Finance::Quote's
breadth: yfinance and yahooquery cover Yahoo only, pandas-datareader is largely
unmaintained, and OpenBB is far too heavy to bundle. GnuCash users usually already
have Finance::Quote installed.

## Decision

### Dependency policy

- `gen/` (domain, storage, engines, and services) stays standard-library only.
  `tests/test_architecture.py` enforces this for `gen/`, `cli/`, and `plugins/`.
- Adapters (`plugins/`), the web server, the CLI, and the GTK interface may add a
  third-party package. Each addition needs a decision record giving:
  - the reason, and why the standard library is not enough;
  - its licence;
  - how it is packaged in the wheel, the Flatpak, the Windows installer, and the
    RPM and DEB packages;
  - what happens when it is missing.
- A network-facing dependency is an optional extra, so the application still works
  without it.
- When a package is admitted, `test_architecture.py`'s allowlist names it for that
  layer only.

### Online quotes

- A commodity asks for online quotes through `Commodity.quote_source`, which
  GnuCash import carries over when GnuCash fetches quotes for that commodity.
- `gen/services/quotes.update_quotes` decides what to request and stores the
  results as ordinary dated prices whose source is `Online: <origin>`. It updates
  rather than duplicates, never touches the network, and takes the fetcher as an
  argument.
- `plugins/quotes` fetches with the standard library alone:
  - **`tsp`**: tsp.gov's share-price CSV.
  - **`currency`**: the ECB's daily reference rates, crossed through the euro into
    the reporting currency.
  - **`alphavantage`**: `GLOBAL_QUOTE`, using the key from settings or
    `ALPHAVANTAGE_API_KEY`, which Finance::Quote also reads.
  - **Any other name**: passed to Finance::Quote, run as `perl` with a small JSON
    script (core Perl `JSON::PP` only). It is used only where Finance::Quote is
    installed and the process is not in a Flatpak.
- The Flatpak does not get permission to run host commands; Flatpak users use the
  native sources. The RPM and DEB packages run on the host, where an installed
  Finance::Quote is available.

## Consequences

- No new Python dependency is needed for quotes.
- TSP, exchange rates, and keyed securities work everywhere, including the Flatpak
  and Windows. Finance::Quote's other sources work only where it is installed.
- A source that changes format breaks only its own commodities, with a reason
  shown per commodity. Finance::Quote's maintainers keep its long tail working.
- Packaging grows by two native Linux formats; their build and smoke tests join CI.
