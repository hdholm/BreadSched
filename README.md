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

Foreign-currency account balances use available direct dated exchange quotes;
account views identify the quote date and source or disclose a missing quote with
the original ledger currency. Account chart rollups and cash/net-worth summaries
with missing reporting-currency quotes read "Missing reporting-currency quote".
Cross-report currency totals remain under development.

Documented alpha releases are published from a tested main commit with wheel,
source archive, and `SHA256SUMS` assets. GitHub marks alpha versions as
pre-releases; check the versioned release notes and verify downloaded assets
against the published checksums before installation.
Release builds run without repository write access; a separate publisher validates
the tested main commit and artifacts before tagging. Ordinary CI uses read-only
repository tokens. The loopback web server serves only its packaged page, script,
and stylesheet from its static directory.

## Explore expenses

Apply a Plan horizon, grouping, and scenario, then choose **Explore expenses…** in
GTK Plan or scroll to **Expense Explorer** on the web Plan page. Compare category
plan and actual amounts for a selected period, sort categories, inspect a category
trend across periods, and open merchant transaction detail. Merchant totals are
actuals grouped temporarily by transaction description; the category plan is not
allocated into merchant budgets. Both interfaces can print the applied view. See
the [User Guide](src/breadsched/USER_GUIDE.md#explore-expenses) for steps and
interpretation.

Plan value detail in GTK and web explains a selected category, planning flow, or
mortgage cash requirement with dated planned and actual contributions. The web
response uses the same activity engine as the other Plan views; see the
[User Guide](src/breadsched/USER_GUIDE.md#plan) for how to read these values.
The web Plan view uses the same shared Plan service for saved controls and scenario
comparisons as the GTK view; selecting a comparison displays per-section differences
without changing either scenario.
Baseline scheduled transactions created or edited in the web interface use the
shared schedule service; a rejected edit leaves the stored schedule unchanged.
The same write boundary applies to scenario-only estimates and changes to a
scenario's baseline schedules; a rejected request leaves saved overrides unchanged.
Account views disclose a security quote's date and source. When a reporting-currency
quote is missing, they label the ledger-value fallback; see the
[User Guide](src/breadsched/USER_GUIDE.md#security-prices-and-current-value).
The web Dashboard uses the shared Dashboard calculation for its summary, groups,
bills, and income. Query-specific liquidity and emergency-fund horizons apply to
the current view without changing saved Dashboard settings.
The valuation layer uses exact as-of direct currency quotes for ordinary foreign
account balances, with provenance and explicit missing-quote status. Account chart
rollups and cash/net-worth summaries require complete direct quotes; Dashboard,
Plan, Projection, and other reports still need that policy applied. Conversion-path
rules remain under development.
The web Scenarios list displays Base and saved scenarios with effective assumptions,
their inheritance sources, and accounts eligible for account-specific rates. Saving
a scenario still uses the shared scenario service.
Web Projection month details use the shared projection calculation and show opening
and closing cash, account movements, dated events, and effective assumption sources.
Inspecting a draft month does not save edited projection controls.
Web Projection summaries and comparisons also use that shared engine. The comparison
shows aligned monthly and ending-value differences without saving either draft.

## Install and run for development

```bash
pip install -e ".[gui,dev]"          # PyGObject + GTK 4 runtime are also required
pytest                               # GUI tests skip when GTK is unavailable
breadsched --help                    # command line
breadsched-gtk household.breadsched  # GTK4 desktop interface
breadsched web household.breadsched  # loopback browser interface
```

With no book argument, `breadsched-gtk` opens the start workflow for creating,
opening, or importing a book. `breadsched gui` and `python -m breadsched.gui` are
equivalent launcher forms. Do not expose the development web server on an untrusted
network.

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
