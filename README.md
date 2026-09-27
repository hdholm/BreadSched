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

- [`src/breadsched/USER_GUIDE.md`](src/breadsched/USER_GUIDE.md) is the standalone,
  task-oriented user guide. The installed GTK application presents the same document
  from **Help → User Guide** (`F1`).
- [`ROADMAP.md`](ROADMAP.md) is the single source of future work.
- [`CHANGELOG.md`](CHANGELOG.md) records completed milestones and their durable
  acceptance contracts.
- [`DESIGN.md`](DESIGN.md) explains the current architecture and design rationale.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) defines the development and pull-request
  workflow.

Alpha releases provide a wheel, source archive, and checksums. Native Linux and
Windows installers are planned. Read the release notes and verify the checksums
before installing. The [User Guide](src/breadsched/USER_GUIDE.md) covers current
workflows and their limits, including exchange rates, imports, and recovery.

## Explore expenses

Apply a Plan horizon, grouping, and scenario, then choose **Explore expenses…** in
GTK Plan or scroll to **Expense Explorer** on the web Plan page. Compare category
plan and actual amounts for a selected period, sort categories, inspect a category
trend across periods, and open merchant transaction detail. Merchant totals are
actuals grouped temporarily by transaction description; the category plan is not
allocated into merchant budgets. Both interfaces can print the applied view. See
the [User Guide](src/breadsched/USER_GUIDE.md#explore-expenses) for steps and
interpretation.

Plan value detail explains dated planned and actual contributions. The current
Expense Explorer compares plan and actual, but does not yet calculate per-category
remaining balances or rollover. An ungrouped Dashboard shows complete ledger net
worth; group totals need setup and emergency coverage needs committed outgoings. The
Dashboard discloses when selected groups leave asset, debt, or cash accounts outside
their totals. The [User Guide](src/breadsched/USER_GUIDE.md#dashboard-and-near-term-cash) explains
current behavior, and the [roadmap](ROADMAP.md) tracks these improvements.

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
The current hardening phase prioritizes financial correctness, storage integrity,
importer preservation, security, and realistic-book performance before another broad
feature-expansion cycle.

See [`ROADMAP.md`](ROADMAP.md) for the authoritative pending-work list.

## Licence

See [`LICENSE`](LICENSE) for the project's licensing terms.
