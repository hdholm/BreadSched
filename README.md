# CashPerspective

Track income and expenses against a cash-flow budget, and project them forward for
years under assumptions you can name, save and argue with.

CashPerspective reads GnuCash books directly — both the SQLite and the compressed-XML
container — so it can sit alongside GnuCash rather than replacing it. The
architecture follows [Gramps](https://github.com/gramps-project/gramps); the ledger
and register interface follow
[GnuCash](https://github.com/Gnucash/gnucash); the direct-read command line
follows [gnucash-cli](https://github.com/loftx/gnucash-cli), but without needing
GnuCash's Python bindings installed.

```
pip install -e ".[gui,dev]"     # GUI extra needs PyGObject and a GTK 4 runtime
pytest                          # full suite; GUI tests skip without a GTK display
cashperspective --help                 # command line
cashperspective-gtk household.cashperspective # graphical interface
```

For a pre-submit development check, run `make check`. It executes Ruff, mypy, the
randomised and fixed-order test suites, the end-to-end demo, and a source/wheel
build. CI repeats those checks across supported Python versions and also installs
the built wheel from outside the checkout so missing package data or accidental
source-tree imports are caught before release.

## What it does

- **Double-entry ledger.** Transactions own their splits and cannot be stored
  unbalanced. Amounts are exact rationals, never floats.
- **Cash-flow budget.** Per-period amounts rather than one annual figure, because
  a budget that averages the heating bill across twelve months hides the January
  the current account runs dry.
- **Scheduled transactions.** Recurrence rules with month-end clamping and
  business-day adjustment, posted idempotently.
- **Multi-year projection.** Monthly steps over any horizon, driven by the budget,
  by the schedules, or by both.
- **Saved scenarios.** Named assumption sets that can be compared side by side.
- **GnuCash import.** Accounts, transactions, commodities and scheduled
  transactions, from either container format.

## Dashboard

The view a book opens on, in both the desktop and browser interfaces, computed by
one engine so the two cannot disagree.

Accounts are gathered into groups from three sources, in order of precedence, so
that answers given elsewhere are not ignored here:

1. **A loan that names its asset** becomes a property line — value against debt,
   reported as equity and loan-to-value, the pair of numbers that actually answers
   "how is the house doing". *All* the loans secured on one asset share a single
   line: a house with two mortgages has one equity figure, and a line per loan
   would count the house twice.
2. **The dashboard configuration**, which is the explicit answer and beats
   anything inferred.
3. **An account naming a group** — the account editor's *Dashboard group* field —
   for accounts the configuration does not mention. This is a default placement,
   not an override: taking an account out of a group it was deliberately put in is
   how a property line loses its house and reports a value of zero against its
   mortgage. The field also shapes the default configuration, so it still has an
   effect on a book that has never configured its dashboard.

An account claimed by a higher rule is removed from the lower ones rather than
appearing twice: a house counted in both a property line and an asset total
inflates net worth by the whole value of the house. Every recurring outflow is
normalised from its own recurrence to a monthly and annual figure, so a quarterly
HOA fee and a fortnightly daycare bill can be added together and sorted against
each other. Each bill also shows what should already be set aside — its accrual so
far through the current cycle — so an annual premium is visibly a twelfth funded a
month after it was last paid rather than arriving from nowhere in November.

Two horizons are configurable and deliberately kept apart, because a household can
be comfortable on one and short on the other:

- **Liquidity** — can the bills falling due in the next *n* days be paid from the
  bank, after the income expected in that window?
- **Emergency fund** — how many months of outgoings are covered with no income at
  all?

Groups and horizons are stored in the book rather than in user settings: they name
accounts, and carrying them to another book would point at the wrong ones.

```bash
cashperspective dashboard household.cashperspective --emergency-months 12
```

### Relationships an imported book does not state

GnuCash records what happened, not what it means. A mortgage never names the house
it bought; a credit card's due date exists only as a pattern in a year of payments.
Both matter here — equity needs the pair, a forecast needs the date — so both are
inferred, and offered rather than applied:

```bash
cashperspective infer household.cashperspective            # list, with the evidence for each
cashperspective infer household.cashperspective --apply    # accept the confident ones
```

A wrong guess that announces itself costs a moment; one applied quietly becomes a
figure somebody trusts. Import lists what it noticed and changes nothing unless
asked.

Accounts carry the relationships themselves: a dashboard group, the asset a loan is
secured on, and for a credit card whether it is cleared monthly or carries a
balance. That last distinction is not cosmetic — a card cleared each month is a
payment channel, while one carrying a balance is a debt that compounds and whose
usual payment is a real monthly outflow, held as a scheduled estimate.

### Several budgets, each with its own projection

Budgets can be cloned, and a projection belongs to the budget it came from, so two
plans can be compared rather than being copies of one another. Each scheduled flow
carries the budgets it belongs to, and whether that question has been decided at
all: until it has, the flow counts everywhere, so a schedule created before budgets
existed never silently vanishes from them. The flag matters — with "empty means
every budget" instead, removing a flow from the only budget in a book would have
put it back into all of them.

In the interface, the budget view has *Use this budget*, *Flows…* and *Clone…*.

```bash
cashperspective budget-clone household.cashperspective Base "Tighter"
cashperspective budget-member household.cashperspective remove --budget Tighter --schedule Groceries
cashperspective budget-use household.cashperspective Tighter    # the dashboard follows this one
```

## Loans

A loan is not one number, it is a shape: a level payment whose interest and
principal parts move against each other every month. Recording it as a fixed
transfer shows a household repaying a mortgage in a straight line, which is wrong
from the first payment and badly wrong for a decade.

Loans are therefore stored as scheduled transactions with **formula** splits over
`ipmt` and `ppmt`, resolved against each occurrence's own period number. That is
the same technique GnuCash uses, which is why an imported GnuCash mortgage now
evaluates instead of reading as zero — formulas may call `pmt`, `ipmt`, `ppmt`,
`fv`, `pv` and `nper`, in either Python's `f(a, b)` or GnuCash's `f(a : b)` call
syntax. The functions are whitelisted by name, so a formula still cannot reach
anything that was not deliberately offered to it.

A mortgage written by GnuCash's loan assistant looks like this, and is evaluated
as it stands:

```
ppmt( .05375 / 12.00 : i : 180.00 : 399,200.00 : 0 : 0 )
```

Three things there defeat a naive parser, and any one of them left unhandled makes
every mortgage payment in the book evaluate to nothing — a household whose loans
appear to cost it zero a month. Colons separate the arguments; the principal
carries a thousands separator, which has to be removed *before* the colons become
commas or the amount arrives as two arguments; and `i` is the period number,
supplied per occurrence alongside our own `period`.

Sign conventions differ between the two worlds, so each keeps its own: the
`finance` module is a spreadsheet, where a payment you make is negative, while the
formula language returns positive amounts because GnuCash puts `ipmt(...)` straight
into a debit slot.

Setting up a loan clears any interest rate on the liability account: the schedule's
`ipmt` leg *is* the interest, and leaving a rate on the account as well makes a
projection charge it twice — turning a repaying mortgage into a growing one,
slowly enough to look plausible.

## Architecture

The layering is Gramps': a storage-agnostic object model, an abstract database
interface, engines that are pure functions over it, and a UI that only displays.

```
cashperspective/
├── gen/                    core; standard library only, no GTK
│   ├── lib/                domain objects (Money, Account, Transaction, Budget…)
│   ├── db/                 DbBase contract + SQLite backend, batches, undo, signals
│   ├── engine/             ledger, schedule, cashflow, projection
│   ├── plug/               plugin registry
│   └── utils/              signal dispatch
├── plugins/
│   ├── importer/           gnucash_sqlite, gnucash_xml
│   └── export/             CSV
├── cli/                    argparse front end, --json on every command
└── gui/                    GTK4; the only place `gi` is imported
```

`tests/test_architecture.py` enforces this: the build fails if anything under
`gen/`, `cli/` or `plugins/` imports GTK, if the core acquires a third-party
dependency, or if a GUI module stops parsing.

### Decisions worth knowing about

**Money is a rational, not a float or a Decimal.** `Money` holds integer
`numerator`/`denominator`, exactly as GnuCash's `gnc_numeric` does, so a split's
`value_num`/`value_denom` pair survives a round trip untouched. `Money(0.1)`
raises `TypeError` on purpose.

**Rates compound; they do not divide.** A 6% annual return becomes
`(1.06 ** (1/12)) - 1` per month. Dividing by twelve would silently turn it into
6.17% a year.

**All writes go through a transaction.** A half-written double-entry transaction
is a corrupt ledger, so the unit of durability is the batch. An entire GnuCash
import is one batch and therefore one Ctrl+Z.

**Repaints are deferred out of the event that caused them.** Rebuilding a view
from inside a focus or value-changed handler disposes the widget whose gesture is
still running; GTK then continues that gesture with no event and reports
`gdk_event_triggers_context_menu: assertion 'event != NULL' failed`. Editing a
budget cell and clicking into the next one is exactly that sequence, so those
views repaint on the idle instead.

**Signals fire after commit, never during.** A view repainting mid-batch would
render an unbalanced book. Bulk operations suppress per-object signals entirely
and emit one summary instead.

**A forecast reports a broken schedule; it does not refuse to run.** A scheduled
transaction whose calculated legs disagree is a fault in that schedule, not a
reason to abandon a forty-year projection. The residual is named once, against the
schedule responsible. At import, a two-leg template with one unusable formula is
balanced against its good side — with two legs the missing figure is not a guess —
and the repair is reported rather than made silently.

**Due transactions are asked about, never posted for you.** Opening a book raises
a dialog listing what is due, and each occurrence is decided on its own: post it,
be reminded later, or mark it done without posting. The default is to ask again,
because the safe answer to a question the user has not read is not to write to
their ledger. "Never" applies to that one date — skipping March must not also
dismiss February or silence April.

**A damaged record costs one record.** Every defect an importer meets is either
repaired or skipped, never raised: an exception would abort the enclosing batch and
roll back everything imported so far. Each skip is reported with the date,
description and GUID of the transaction responsible, because "a transaction needs
at least two splits" is not actionable against a book with ten thousand of them.
`-v`, `--debug` and `--log-file` escalate the detail; the import dialog has the
same option and writes the log beside the file being imported.

**Nothing is counted twice.** Under the `combined` projection basis, an account
driven by a scheduled transaction is dropped from the budget, because the schedule
is the more specific statement of intent. Rent entered in both places is charged
once.

**Scenarios store assumptions, never results.** A saved forecast is recomputed
against the current ledger every time it is opened, so it incorporates new actual
transactions instead of quietly going stale.

**Scenario assumptions can change over time.** A scenario may contain dated
assumption periods that override only the rates that change, globally or for a
specific account. Investment, cash and liability rates take effect in the month a
period begins; income growth and expense inflation preserve the existing annual
budget-escalation behavior and use the rate in force at each projection anniversary.

**Formulas are parsed, not `eval`'d.** Scheduled-transaction formulas go through
an `ast` walk with a node whitelist. This application's whole job is reading other
people's financial documents; `eval` on their contents is not an option.

## Starting up

The application opens on a start screen rather than creating a book. A book made
unasked leaves a file somebody did not choose in a place they did not pick, and
makes "which book am I looking at" the first question instead of the last. Four
ways in are offered:

- **New book** — an empty chart of accounts.
- **Open book** — one you already have. Books written under the previous name
  (`.cashcast`) still open.
- **Import a GnuCash book** — creates the new book and imports into it in one go,
  which is what a first run usually is.
- **Use the default book** — for people who only ever want one; created in your
  documents folder the first time it is asked for, and not before.

## Three interfaces

The same engines drive all three, so a figure cannot differ between them.

```bash
cashperspective --help                       # command line
cashperspective-gtk household.cashperspective       # GTK4 desktop interface
cashperspective web household.cashperspective       # browser interface on 127.0.0.1:8765
```

The web interface binds to loopback only and refuses any other host: it has no
authentication, and a finance tool listening on a network interface is not a
default anyone should have to discover.

## Command line

Every command takes `--json`, so the CLI doubles as the scripting interface.

```bash
cashperspective init household.cashperspective
cashperspective import household.cashperspective ~/Documents/accounts.gnucash

# When an import does not produce what you expected:
cashperspective import household.cashperspective accounts.gnucash -v            # progress + warnings
cashperspective import household.cashperspective accounts.gnucash --debug       # every record read
cashperspective import household.cashperspective accounts.gnucash --log-file import.log

cashperspective verify household.cashperspective       # non-mutating storage + ledger checks
cashperspective backup household.cashperspective household.backup
cashperspective restore household.backup restored.cashperspective
# Existing destinations require --overwrite; the old book is preserved first as
# restored.cashperspective.pre-restore.bak.

cashperspective accounts household.cashperspective
cashperspective register household.cashperspective "Assets:Checking Account" --limit 20
cashperspective balance household.cashperspective --as-of 2026-06-30

cashperspective add household.cashperspective --date 2026-03-01 --description "Rent" \
    --from "Assets:Checking Account" --to "Expenses:Rent" --amount 1800.00

# `register` prints a short id for each row; edit or delete by that.
cashperspective edit household.cashperspective e871f519 --amount 1950.00 --description "Rent, revised"
cashperspective delete household.cashperspective e871f519

cashperspective scheduled household.cashperspective --days 30
cashperspective scheduled household.cashperspective --post

cashperspective budget-set household.cashperspective --name 2026 \
    --account "Expenses:Groceries" --amount 600.00
cashperspective budget household.cashperspective --name 2026

cashperspective scenario household.cashperspective save --name "Base case" \
    --years 20 --income-growth 0.03 --inflation 0.025 --investment-return 0.06
cashperspective scenario household.cashperspective save --name "Long recession" \
    --years 20 --income-growth 0.00 --inflation 0.05 --investment-return 0.01
cashperspective project household.cashperspective --scenario "Base case" --csv forecast.csv
cashperspective compare household.cashperspective "Base case" "Long recession"
```

`verify` opens the book read-only and is deliberately more tolerant than a normal
application open: malformed object blobs are reported by type and handle instead of
stopping at the first undecodable record. Ordinary writes are stricter. Before a
database transaction commits, CashPerspective verifies the resulting object graph and
derived indexes; an invalid final state rolls the entire transaction back.

`backup` uses SQLite's online backup API rather than copying the main file, so
committed pages still resident in the WAL are included. `restore` verifies both
SQLite integrity and CashPerspective's logical invariants before installing a
backup. Replacing an existing book is never implicit: `--overwrite` first writes
a consistent `.pre-restore.bak`, removes stale SQLite WAL/SHM sidecars, and only
then atomically installs the restored database.


### Reading a GnuCash book without importing it

The `gnucash` subcommands read the file in place, which is the behaviour
gnucash-cli offers through GnuCash's Python bindings. Here it is plain SQLite, so
no GnuCash installation is needed.

```bash
cashperspective gnucash info         ~/Documents/accounts.gnucash
cashperspective gnucash accounts     ~/Documents/accounts.gnucash --json
cashperspective gnucash transactions ~/Documents/accounts.gnucash \
    --start 2026-01-01 --end 2026-03-31
```

The source is opened through a `file:…?mode=ro` URI: it can be read while GnuCash
has it open, and can never be written to.

## Interface

The GUI needs GTK 4 and PyGObject, which are system packages rather than pure
Python wheels:

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 libgtk-4-1  # Debian/Ubuntu
sudo dnf install python3-gobject python3-cairo gtk4                    # Fedora
brew install pygobject3 gtk4 py3cairo                                  # macOS
pip install -e ".[gui]"
```

Three equivalent ways to start it, all accepting an optional book to open:

```bash
cashperspective-gtk household.cashperspective     # installed launcher
cashperspective gui household.cashperspective     # subcommand, if you only remember one binary
python -m cashperspective.gui household.cashperspective
```

With no argument the window opens empty and you create or open a book from the
toolbar. `cashperspective-gtk --help` and `--version` work without GTK installed, and so
does the error you get if it is missing: the launcher never imports `gi` until
arguments have been checked, so a missing GTK stack produces install instructions
for your platform and exit code 3, rather than an import traceback.

All PyGObject imports go through `gui/gi_setup.py`. It pins every namespace
version in one place, and absorbs the deprecation warning PyGObject raises inside
its own import machinery — a warning nothing here can prevent, suppressed at the
import rather than by a blanket test-config filter that would leave it in place for
anyone embedding the code.

Five categories in a Gramps-style sidebar, switching a stack of views:

- **Accounts** — a lazily-built `Gtk.TreeListModel` tree with recursive balances
  and summary cards. Double-clicking an account opens its register, as GnuCash
  does; *Edit account…* changes its settings. Activating a placeholder expands it,
  since it holds no entries of its own.
- **Register** — GnuCash's ledger layout, with debit and credit column headings
  that change per account type (*Deposit*/*Withdrawal* for a bank account,
  *Payment*/*Charge* for a credit card). Naming columns after what the account
  actually does removes the commonest source of entry errors.
- **Scheduled** — the standing arrangements, each expanding to its splits, with a
  Commitment/Estimate column. **Upcoming** is the diary of what is due.
- **Budget** — a period-by-period grid on the same list widget as the other views,
  so its columns drag to resize and hide from the header menu. Rows are grouped by
  account class and collapse to their totals, which is what makes a chart of sixty
  expense categories readable. Figures are editable in place; net cash flow is the
  bottom row, with shortfall periods highlighted and the year's total in the Total
  column.
- **Projection** — assumption sliders beside a Cairo line chart that recomputes as
  you drag. The zero line is drawn heavier than the other gridlines and the region
  below it tinted, so a forecast that dips negative reads as a problem rather than
  as a line that happens to be low.

## Compatibility notes

| Concern | Behaviour |
|---|---|
| Container format | Detected by sniffing the header, not the extension — GnuCash names both `.gnucash` |
| GUIDs | Reused verbatim as handles, so re-importing updates rather than duplicating |
| Template accounts | GnuCash's hidden scheduled-transaction tree is excluded from the chart of accounts and from the ledger |
| Scheduled transactions | Template splits resolved through the `slots` indirection to the real target accounts |
| Out-of-balance transactions | Difference posted to `Imbalance` with a warning naming the transaction |
| Single-split transactions | Balanced to `Imbalance`; a lone zero-value split is skipped instead, since balancing it would invent a record |
| Orphaned splits | That transaction skipped and counted; the rest of the book still imports |
| Any malformed record | Never aborts the batch — one bad transaction must not cost the other ten thousand |
| Scheduled template formulas | Read as amounts in any locale (`1,800.00`, `1.800,00`, `1 800,00`), evaluated if arithmetic, kept as text if they name GnuCash variables |
| An unreadable schedule | Skipped with a warning naming it; the ledger still imports |
| Multi-currency books | Values imported at their stored denominations; cross-currency valuation is not yet modelled |

## Status

Working and tested: the object model, storage with undo, all four engines, both
importers, CSV export, and the full CLI. The core suite requires no display.

Coverage is 90–98% across `gen/db`, `gen/engine` and the importers, and 95% on the
CLI. The GTK4 layer is exercised by `pytest -m gui`, which builds real
widgets against a live GTK 4 runtime under a virtual display: every view is
constructed and bound, registers and charts are populated, dialogs post
transactions. They skip automatically where no display is available, so
`pytest` is expected to pass either way — GUI-marked tests skip cleanly without a display,
and behave identically with or without a D-Bus session bus.

Test order is randomised on every run (pytest-randomly, in the dev extra), and the
seed is printed so a failure can be reproduced. `make test-ordered` runs in a fixed
order for bisecting. This is here because an order-dependent test did ship once: it
captured log records on the root logger, which stops receiving them as soon as
anything configures logging.

Not yet built: reconciliation,
investment lots and cost basis, price quotes, and multi-currency valuation.

## Licence

GNU Affero General Public License, version 3 or later. The full text is in
`LICENSE`.

The Affero clause matters for the browser interface: anyone who runs a modified
copy of this program as a network service has to offer the source of their
modifications to the people using it, which a plain GPL would not require.
