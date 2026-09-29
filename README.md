# BreadSched

BreadSched is a household-finance application for tracking income and expenses,
planning cash flow from dated financial events, and projecting household finances
under saved assumptions and alternate scenarios.

GTK4 is the primary/reference interface and Linux is the primary native desktop
target. The web interface uses the same book, planning rules, and financial engines
for browser delivery where native GTK packaging is impractical.

BreadSched is intended over time to cover the household-finance workflows for which
a user might otherwise rely on GnuCash, without reproducing GnuCash's business-
accounting features. GnuCash compatibility remains a first-class requirement while
BreadSched's standalone household feature set matures.

## Why the name BreadSched?

The application is focused heavily on cash flow, but most obvious names built from
cash, money, funds, projections, or schedules are already used. **Bread** is slang
for money and **Sched** is a diminutive of schedule; together they also rhyme.

## Documentation

- [`src/breadsched/USER_GUIDE.md`](src/breadsched/USER_GUIDE.md) is the user guide's
  overview, with a part for each interface: [desktop](src/breadsched/guide/desktop.md),
  [browser](src/breadsched/guide/web.md), and [command line](src/breadsched/guide/cli.md).
  Every interface shows all of them: **Help → User Guide** (`F1`) on the desktop,
  **Guide** in the browser, and `breadsched guide` on the command line.
- [`ROADMAP.md`](ROADMAP.md) is the single source of future work.
- [`CHANGELOG.md`](CHANGELOG.md) records completed milestones and their durable
  acceptance contracts.
- [`DESIGN.md`](DESIGN.md) explains the current architecture and design rationale.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) defines the development and pull-request
  workflow.

Alpha releases provide a wheel, source archive, and checksums. CI builds and tests
a per-user Windows installer with its own Python and GTK runtime
(`packaging/windows/`); it is not yet published with releases. A native Linux
installer is planned. Read the release notes and verify the checksums
before installing. The [User Guide](src/breadsched/USER_GUIDE.md) covers current
workflows and their limits, including exchange rates, imports, and recovery.
The source tree includes a Flatpak manifest, desktop entry, AppStream metadata, and
icon, with CI checks for installed, offline CLI book, file, and writer-lock
workflows and a sandboxed GTK smoke (views, help, icon, settings) under Documents
access. GTK file portals and printing remain to be validated before an installer
is published.

## Install and run for development

```bash
pip install -e ".[gui,dev]"          # PyGObject + GTK 4 runtime are also required
pytest                               # GUI tests skip when GTK is unavailable
breadsched --help                    # command line
breadsched sample sample.breadsched  # separate synthetic learning book
breadsched-gtk household.breadsched  # GTK4 desktop interface
breadsched web household.breadsched  # loopback browser interface
```

With no book argument, `breadsched-gtk` opens the start workflow for creating,
opening, or importing a book. `breadsched gui` and `python -m breadsched.gui` are
equivalent launcher forms. Do not expose the development web server on an untrusted
network.

The sample command creates a new book with generic household balances, transactions,
Dashboard groups, recurring commitments, a Plan estimate, and a Base scenario. It
refuses to replace an existing file. Use `--as-of YYYY-MM-DD` to reproduce its
relative dates; see the [User Guide](src/breadsched/USER_GUIDE.md#synthetic-sample-book).

For a pre-submit development check, run:

```bash
make check
```

The Makefile checks the local `src/` tree with Ruff and mypy, runs randomized core
tests with pytest-xdist, runs serial GTK tests, executes the end-to-end demo, and
builds source and wheel distributions. Set `PYTEST_XDIST_WORKERS` to override the
core worker count. `make test-ordered` remains the serial deterministic diagnostic
path.

## Book safety

BreadSched native books use the `.breadsched` suffix and SQLite storage. Keep
independent backups and do not place the only copy of a book in a synchronization
location that is unsafe for SQLite. The GTK File menu and CLI provide Verify,
Backup, and Restore operations; the [User Guide](src/breadsched/USER_GUIDE.md)
explains them in detail.

## Project status

BreadSched is under active development and is not yet a complete GnuCash replacement.
GnuCash import is one-way. If you keep using GnuCash, follow the User Guide's
[side-by-side guidance](src/breadsched/USER_GUIDE.md#keep-gnucash-and-breadsched-side-by-side)
to avoid duplicate or overwritten transactions. Re-import keeps statements you
reconcile in BreadSched and holds GnuCash changes to those transactions for a
batched review.
When a release changes the native book format, opening a book for writing migrates
it after writing a verified backup beside it; earlier builds cannot open the
migrated book, so restore that backup to roll back.
The current hardening phase prioritizes financial correctness, storage integrity,
importer preservation, security, and realistic-book performance before another broad
feature-expansion cycle.

See [`ROADMAP.md`](ROADMAP.md) for the authoritative pending-work list.

## Licence

See [`LICENSE`](LICENSE) for the project's licensing terms.
