# BreadSched Household Financial Manager

BreadSched is a household finance application for tracking income and expenses,
planning cash flow from dated financial events, and projecting household finances
under saved assumptions and alternate scenarios.

GTK4 is the primary/reference interface and Linux is the primary native desktop
target. There is a web interface that uses the same data, planning rules, and
financial engines.

BreadSched is intended over time to cover the household-finance workflows for
which a user might otherwise rely on GnuCash, KMyMoney, HomeBank, ActualBudget,
beancount, Firefly III, or Quicken Classic, without reproducing business
accounting features. GnuCash compatibility remains a strict requirement while
BreadSched's standalone household feature set matures.

This is **Alpha** software, barely above proof-of-concept and includes a lot
of not yet fully reviewed AI coding.  You are welcome and encouraged to use it,
play with it, suggest improvements, and provide feedback.  But you rely on it
at some peril. Backward data compatibility is only maintained for a **very**
limited time.  While there are no known serious bugs as of this writing I am
certain there are many unknown bugs. If you are using it at all you are strongly
encouraged to let me know, or at least follow releases closely or you may not
be able to read your data with a new version.

## Why the name BreadSched?

The application is focused heavily on cash flow, but most of the good,
descriptive names that invoke cash, money, funds, coins, plan, insight, map,
projection, stream, flow, road, oracle, advisor, vision or schedule are already
used by some one for some thing.  **Bread** is a slang term for money and
**Sched** is an obvious diminutive of schedule; together they provide memorable
rhyme for a name.

## Documentation

- [`src/breadsched/USER_GUIDE.md`](src/breadsched/USER_GUIDE.md) is the user
  guide's overview, with a part for each interface:
  [desktop](src/breadsched/guide/desktop.md),
  [browser](src/breadsched/guide/web.md), and
  [command line](src/breadsched/guide/cli.md).
  Each front-end provides all of them: **Help → User Guide** (`F1`) on the
  desktop, **Guide** in the browser, and `breadsched guide` on the command line.
- [`ROADMAP.md`](ROADMAP.md) is the current future work plan for broader
  features.  GitHub issues are used to track bugs and more limited feature
  requests; never attach a real financial book or statement to one. Report
  security problems privately as [`SECURITY.md`](SECURITY.md) describes.
- [`CHANGELOG.md`](CHANGELOG.md) records completed changes by version; older
  versions are condensed and link their release notes.
- [`DESIGN.md`](DESIGN.md) explains the current architecture and design
  rationale for anyone who wants to dig into the code.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) defines the development and pull-request
  workflow.

Alpha releases provide a wheel, source archive, a per-user Windows installer
with its own Python and GTK runtime (`BreadSched-<version>-setup.exe`, built
and tested on Windows from the released commit; not yet code-signed), a Linux
Flatpak bundle (`BreadSched-<version>.flatpak`, built and installed from the
released commit), and checksums covering all of them. Read the release notes
and verify the checksums before installing. The
[User Guide](src/breadsched/USER_GUIDE.md) covers current features and their
limits, including exchange rates, imports, and recovery. CI tests the Flatpak
in its sandbox, including the file chooser and printing through the desktop
portal and books kept outside Documents.

## Install and run for development

BreadSched requires Python 3.11 or later.

```bash
pip install -e ".[gui,dev]"          # PyGObject + GTK 4 runtime are also required
pytest                               # GUI tests skipped when GTK is unavailable
breadsched --help                    # command line
breadsched sample sample.breadsched  # separate synthetic learning book
breadsched-gtk household.breadsched  # GTK4 desktop interface
breadsched web household.breadsched  # loopback browser interface
```

With no book argument, `breadsched-gtk` opens the start workflow for creating,
opening, or importing a book. `breadsched gui` and `python -m breadsched.gui`
are equivalent launcher forms. **Do not** expose the development web server on
an untrusted network.

The sample command creates a new book with generic household balances,
transactions, Dashboard groups, recurring commitments, a Plan estimate, and a
Base scenario. It refuses to replace an existing file. Use 
`--as-of YYYY-MM-DD` to reproduce its relative dates; see the
[User Guide](src/breadsched/USER_GUIDE.md#synthetic-sample-book).

For a pre-submit development check, run:

```bash
make check
```

The Makefile checks the local `src/` tree with Ruff and mypy, runs randomized
core tests with pytest-xdist, runs serial GTK tests, executes the end-to-end
demo, and builds source and wheel distributions. Set `PYTEST_XDIST_WORKERS` to
override the core worker count. `make test-ordered` remains the serial
deterministic diagnostic path.

## Book safety

BreadSched native books use the `.breadsched` suffix and SQLite storage. Keep
independent backups and do not place the only copy of a book in a
synchronization location that is unsafe for SQLite. The GTK File menu and
CLI provide Verify, Backup, and Restore operations; the
[User Guide](src/breadsched/USER_GUIDE.md) explains them in detail. Documents
linked to transactions stay in an attachment folder beside the book, as in
GnuCash; a book backup does not include that folder.

## Project status

BreadSched is under active development and is not yet a complete replacement
for a stable financial management program. GnuCash import is essentially
one-way; changes reach a GnuCash book (SQLite or XML) only through an explicit,
previewed write-back. If you keep using GnuCash, follow the User Guide's
[side-by-side guidance](src/breadsched/USER_GUIDE.md#keep-gnucash-and-breadsched-side-by-side)
to avoid duplicate or overwritten transactions. Re-import keeps statements you
reconcile in BreadSched and holds GnuCash changes to those transactions for a
batched review. When a release changes the native book format, opening a book
for writing migrates it after writing a verified backup beside it; earlier
builds cannot open the migrated book, so restore that backup to roll back.
The current hardening phase prioritizes financial correctness, storage
integrity, importer preservation, security, and realistic-book performance
before another broad feature-expansion cycle.

See [`ROADMAP.md`](ROADMAP.md) for the authoritative work plan.

## Licence

See [`LICENSE`](LICENSE) for the project's GNU AGPL licensing terms.
