# BreadSched

BreadSched is a household-finance application for tracking income and expenses,
planning cash flow from dated financial events, and projecting household finances
under saved assumptions and alternate scenarios.

GTK4 is the primary/reference interface and Linux is the primary native desktop
target. The web interface is required to maintain functional parity with GTK4 so
that the same book, planning rules, and financial engines can also support browser
or web-based delivery where native GTK packaging is impractical.

BreadSched is intended over time to cover the household-finance workflows for
which a user might otherwise rely on GnuCash, without attempting to reproduce
GnuCash's business-accounting features. Until that household feature set is
sufficiently complete, compatibility with GnuCash is a first-class requirement:
users should be able to keep an existing GnuCash ledger while using BreadSched's
Plan, scenarios, projections, and household-specific analysis.

## Where to read next

This README is the entry point for users and new contributors. The repository keeps
other concerns deliberately separate:

- [`ROADMAP.md`](ROADMAP.md) is the **single source of future work**. Pending,
  proposed, reprioritized, and deferred work belongs there rather than in the
  README or design documentation.
- [`DESIGN.md`](DESIGN.md) explains architectural principles, important design
  choices, and the reasoning behind them. It describes the design as it is; it is
  not a backlog.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) defines the development and patch workflow,
  including generic regression fixtures, verification requirements, and roadmap
  maintenance expectations.

Full task-oriented user documentation and in-application help are planned in the
roadmap. Until then, this README provides the basic operational overview.

## Install and run for development

```bash
pip install -e ".[gui,dev]"          # PyGObject + GTK 4 runtime are also required
pytest                                # GUI tests skip when GTK is unavailable
breadsched --help                     # command line
breadsched-gtk household.breadsched   # GTK4 desktop interface
breadsched web household.breadsched   # loopback browser interface
```

For a pre-submit development check, run:

```bash
make check
```

The Makefile checks the local `src/` tree with Ruff, mypy, randomized core tests
using pytest-xdist (`-n auto` by default), serial GTK runtime tests, the end-to-end
demo, and source/wheel builds. Set `PYTEST_XDIST_WORKERS` to override the local core
worker count. `make test-ordered` remains the serial deterministic diagnostic path.
CI repeats parallel core checks across the supported Python versions and operating
systems, while GTK runtime behavior is validated serially on Linux.

## Core concepts

### Ledger

BreadSched uses exact double-entry transactions. A transaction owns its splits and
cannot be stored unbalanced. Monetary values use exact rational arithmetic rather
than binary floating point so imported GnuCash numerators/denominators can be
preserved faithfully.

### Plan

The Plan is derived from **dated financial activity**, not from independent monthly
budget cells. Its inputs are:

- actual ledger transactions;
- scheduled commitments;
- scheduled estimates;
- scenario-specific additions, replacements, or suppressions.

Month, quarter, and year views aggregate those same dated events. Changing the
reporting period does not move the underlying financial event.

When an actual transaction resolves a scheduled/planned occurrence, BreadSched
retains the original planned occurrence identity and expected amount. Later edits
to the schedule therefore do not rewrite historical budget-versus-actual results.

### Scheduled transactions

Scheduled transactions represent recurring household events such as pay, bills,
loan payments, transfers, savings, and estimates. They support recurrence bounds,
weekend adjustment, skips, one-time overrides, future-effective amount changes,
multi-split transactions, and formula-driven schedules.

Imported schedules that BreadSched cannot reproduce safely remain inspectable
rather than being silently simplified. Supported fixed and formula schedule forms
can be edited while preserving their stored semantics.

### Scenarios and Projection

Saved scenarios contain assumptions and alternate planned events, not cached
forecast results. Projection recomputes against the current ledger so new actuals
are automatically incorporated.

Projection is event-driven. Cash, investment, and liability state advances between
actual, scheduled, estimated, and one-off dated events. Reporting months are views
of those state transitions rather than the engine's clock.

Both GTK4 and web schedule/scenario editors expose a projection growth policy:

- `auto` — infer the appropriate ordinary household behavior;
- `none` — keep the event nominally fixed;
- `income` — apply the scenario income-growth assumption;
- `inflation` — apply the scenario expense-inflation assumption.

Formula-driven schedules are fixed by default in `auto` mode so, for example, a
formula mortgage is not inflated merely because one leg posts to interest expense.
Mixed gross-to-net payroll schedules grow as one balanced event under income growth.

### Planning roles

Account planning roles provide household meaning for balance-sheet accounts where
ordinary Income/Expense classification is insufficient. Current roles include
ordinary, retirement, FSA/benefit, loan/debt, and investment accounts. Explicit
split planning purposes can override inference when needed.

These classifications let Plan and Projection distinguish activities such as
retirement saving, retirement distribution, FSA funding, and debt principal from
ordinary transfers.

## Primary GTK4 workflow

The GTK4 application is the reference user experience. Its major views include:

- **Dashboard** — household position, scheduled bills, liquidity, emergency-fund
  information, FSA state, and important linked-account relationships.
- **Accounts** — hierarchical chart of accounts with balances, planning role, and
  account metadata.
- **Register** — account transaction history using account-appropriate debit/credit
  terminology.
- **Scheduled** — recurring commitments and estimates, including imported schedule
  details and safe editing where round-trip fidelity is possible.
- **Plan** — category and planning-flow views derived from dated planned and actual
  activity.
- **Review** — resolution of actual transactions against planned occurrences and
  related household workflows.
- **Projection** — saved assumptions and scenarios projected forward from the
  current book.

Start the GTK application with:

```bash
breadsched-gtk household.breadsched
```

With no book argument, BreadSched opens its start workflow for creating, opening,
or importing a book.

## Web interface

The web interface is a second presentation of the same engines and must maintain
functional parity with GTK for supported workflows. It is not the architectural
reference UI, but it is important both as a normal browser interface and as a
possible delivery mechanism on platforms where distributing GTK4 is difficult.

Start it with:

```bash
breadsched web household.breadsched
```

The web security and deployment model is part of the current hardening roadmap;
do not expose the development server on an untrusted network.

## Command line

The CLI exposes book operations and scripting-oriented access to the same core
model. Commands generally support `--json` where structured output is useful.

Common examples:

```bash
breadsched init household.breadsched
breadsched import household.breadsched accounts.gnucash
breadsched verify household.breadsched
breadsched backup household.breadsched household.backup
breadsched accounts household.breadsched
breadsched register household.breadsched "Assets:Checking Account" --limit 20
breadsched balance household.breadsched --as-of 2026-06-30
```

Use `breadsched --help` and each subcommand's `--help` for the currently supported
command surface.

## GnuCash compatibility

BreadSched can import GnuCash SQLite and compressed-XML books and preserves source
GUIDs where possible so subsequent imports can identify the same records. Import
uses the same validated internal sink regardless of source format.

Compatibility is intentionally important during BreadSched's transition toward a
standalone household ledger. The goal is not business-feature parity with GnuCash;
it is to preserve enough household ledger semantics that users can use BreadSched's
planning and projection capabilities without abandoning a mature existing book.

Current compatibility includes accounts, transactions, commodities, scheduled
transactions, formula schedules, reconciliation state, and an expanding set of
account metadata. Import problems are reported per record rather than aborting the
entire book whenever safe recovery is possible.

Native QIF and OFX/QFX importers are also available for bank/cash/credit-card style
history. Their supported and pending formats are tracked in `ROADMAP.md`.

## FSA / benefit planning

FSA-role accounts can carry funding years with election and run-out information.
BreadSched separates benefit availability from the custodial ledger balance and can
associate service/claim episodes with healthcare payments, reimbursements,
allocations, refunds, and rejected reimbursement attempts.

This is a household-specific feature area rather than a general accounting model;
its continuing work is tracked in the roadmap.

## Book files and safety

BreadSched native books use the `.breadsched` suffix and SQLite storage. Verify and
backup operations are available from the CLI, and schema migrations are expected to
preserve recoverability. Storage hardening, locking, transaction atomicity, and
large-book performance are active roadmap priorities.

Do not place the only copy of a financial book in a location whose synchronization
or filesystem behavior is not safe for SQLite. Keep independent backups.

## Project status

BreadSched is under active development and is not yet a complete GnuCash replacement.
The current hardening phase is deliberately prioritizing financial correctness,
storage integrity, importer preservation, security, and realistic-book performance
before another broad feature-expansion cycle.

For the authoritative pending-work list, see [`ROADMAP.md`](ROADMAP.md).
For architectural rationale, see [`DESIGN.md`](DESIGN.md).

## Licence

See the repository licence file for the project's licensing terms.
