# BreadSched

BreadSched is a household-finance application for tracking income and expenses,
planning cash flow from dated financial events, and projecting household finances
under saved assumptions and alternate scenarios.

### Why the name BreadSched?

The application is focused heavily on cash flow, but most obvious names built from
words such as cash, money, funds, projections, or schedules are already used by
other products or services. **Bread** is slang for money, while **Sched** is a
diminutive of schedule. Combining them produces **BreadSched**, with the additional
advantage that the two parts form a rhyming pair.

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
- [`CHANGELOG.md`](CHANGELOG.md) preserves completed milestones and their durable
  acceptance contracts.
- [`DESIGN.md`](DESIGN.md) explains architectural principles, important design
  choices, and the reasoning behind them. It describes the design as it is; it is
  not a backlog.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) defines the development and pull-request
  workflow, including generic regression fixtures, verification requirements, and
  roadmap maintenance expectations.

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

The last applied From, Through, Group by, Show, scenario, and comparison controls
are stored with the book and shared by GTK and web. Every category and planning-flow
row has a total across the selected horizon. Each period also has Income and Expenses
totals calculated from outermost category rollups so a parent and its children are
never added twice.

The Plan leads with a signed **spendable cash bridge**. Income received and retirement
distributions add cash; ordinary expenses, retirement saving, benefit funding, debt
principal, and escrow funding use cash. A residual timing/financing row explains
credit purchases and other events whose expense timing differs from cash settlement.
Those non-overlapping rows reconcile exactly to the projected change in spendable
cash. Opening, ending, and lowest projected spendable-cash balances make the date of
the tightest liquidity point visible. Income and expense detail remains in familiar
positive budget magnitudes, followed by a signed Income less expenses result.

Balance-sheet classifications remain available as informational detail, without a
mixed grand total. Equal-and-opposite account legs of one retirement distribution
are shown as one logical inflow rather than two apparent events. Variance totals and
actual cash cover only the applicable part of the report horizon through its as-of
date; future-only actual and variance summaries are shown as not applicable.

Mortgage servicing also appears in a separate **Cash requirements
(informational)** section. It shows the whole payment needed from spendable cash,
while the interest expense, escrow funding, debt principal, and any fees retain
their own classifications below. The whole-payment row and its components describe
the same dollars and are never added together.

When an actual transaction resolves a scheduled/planned occurrence, BreadSched
retains the original planned occurrence identity and expected amount. Later edits
to the schedule therefore do not rewrite historical plan-versus-actual results.
Click any Plan value to inspect the exact occurrences and actual splits behind it.
The detail states whether each category came from an Income/Expense account type,
whether a balance-sheet planning purpose was explicit or inferred from account type
and direction, and why an actual is unresolved, matched, historical, or explicitly
unexpected. **Resolve actuals…** opens the matching queue. Planning-purpose mistakes
on posted transaction splits can be corrected in the normal transaction editor;
scheduled classifications are corrected in their schedule editor.

### Scheduled transactions

Scheduled transactions represent recurring household events such as pay, bills,
loan payments, transfers, savings, and estimates. They support recurrence bounds,
weekend adjustment, skips, one-time overrides, future-effective amount changes,
multi-split transactions, and formula-driven schedules.

Fixed multi-split schedules can also change individual signed legs from an exact
date onward—for example, a payroll deduction and its net-deposit leg changing
together. GTK and web reject an effective set that no longer balances. Plan and
Projection details identify whether each amount came from the base template, a
formula, a per-leg timeline, or a whole-schedule override.

Imported schedules that BreadSched cannot reproduce safely remain inspectable
rather than being silently simplified. Their original formula or recurrence data
is shown read-only and excluded from planning, projection, and posting until it can
be translated safely. It can still be duplicated exactly into an independent
definition for review. Supported fixed and formula schedule forms can be edited
while preserving their stored semantics. The desktop details editor scrolls within
the available window so its Close/Save actions remain reachable even for large
multi-split definitions. Formula amounts shown in Scheduled are evaluated for the
displayed occurrence, including imported GnuCash `i` period variables; the stored
formula text is not rewritten merely for display.

Suggest from History produces reviewable drafts rather than writing directly to the
book. Choosing **Review…** opens the normal Base or scenario schedule editor with
the inferred accounts, amount, cadence, dates, and seasonal values populated. The
proposal is added only after **Save**; cancelling leaves the book unchanged, and a
later analysis measures coverage from the values that were actually saved.
Suggestions keep ordinary Income/Expense categories separate from retirement
saving and distributions, investment contributions and withdrawals, debt
principal, and benefit/FSA funding. The Purpose column names that distinction, and
accepted drafts preserve it so Plan and Projection do not reinterpret a
balance-sheet movement as ordinary income or expense. Existing future coverage is
matched by account and purpose, so accepting one purpose does not conceal another
purpose on the same account.
Known future coverage is matched against an exact rolling twelve-month window.
When a continuing replacement begins partway through that window, BreadSched can
offer a bounded bridge estimate for only the uncovered months. Isolated future
events do not truncate recurring needs, biennial/triennial history keeps its longer
cadence and next due date, and a lone completed event is not projected as recurring.
Stable two-to-eleven-month cadences such as quarterly activity retain their calendar
interval even when the posting day varies. With at least six active months, isolated
amount spikes are excluded conservatively and disclosed in the proposal; shorter
histories are never silently trimmed. Confidence reflects history coverage, sample
depth, retained observations, and robust amount variability rather than coverage
alone. Analysis interprets the complete transaction before extracting ordinary
categories: reinvested dividends and interest, investment fees, and rollovers do
not become recurring household income or expense suggestions. In multi-split loan,
retirement, and benefit transactions, repeated funding evidence is retained and a
spendable-cash counterpart wins an otherwise equal choice instead of a principal,
investment, or restricted-asset leg.

Credit-card payment terms belong to the card account: whether it is paid in full,
its usual carried-balance payment, payment day, and optional Bank or Cash **Paid
from** account. The resulting monthly obligation appears consistently in Dashboard,
Scheduled, and Upcoming. A paid-in-full card uses its current balance; a carried
card uses the lesser of its balance and usual payment. An actual cash-to-card
payment advances the cycle, while an unpaid overdue obligation remains due. An
enabled explicit/imported schedule with a pending or future payment for the same
card takes precedence. The account-linked row is informational and is never posted
automatically.

**Verify Book** checks SQLite integrity plus the financial relationships that a
database-only check cannot see. It reports unbalanced or orphaned transactions,
commodity/SCU precision conflicts, duplicate realization of a scheduled occurrence,
and reconciliation drift without silently rounding or repairing the ledger.
Projection separately refuses to return a reporting period whose opening state,
dated changes, accruals, and closing state do not reconcile.

Use **New loan…** in Scheduled to enter an amount borrowed, annual rate, term,
first payment, loan account, interest expense, and payment account. BreadSched shows
the calculated level payment and the first year of principal/interest allocation
before saving. Creating the loan stores one formula-driven monthly schedule and can
record the opening liability, so Projection starts with both the debt and its
economically correct amortization. This workflow is available in GTK and web.

### Scenarios and Projection

Base is the household's current expected plan: the current ledger, baseline
scheduled and estimated activity, and Base assumptions. Saved scenarios are
alternatives over Base, not separate ledgers. They contain assumptions and alternate
planned events, not cached forecast results. Projection recomputes against the
current ledger so new actuals and baseline schedules that a scenario has not
replaced are automatically incorporated.

Projection is event-driven. Cash, investment, and liability state advances between
actual, scheduled, estimated, and one-off dated events. Reporting months are views
of those state transitions rather than the engine's clock.

Base is the expected plan. A newly derived saved scenario inherits each Base annual
and account-specific assumption until that value is explicitly overridden, so later
Base edits continue to reach untouched alternatives. Scenario management labels the
source of effective values. A saved scenario can instead inherit assumptions from
another saved scenario, forming a deterministic, cycle-free chain; reparenting keeps
the child's deliberate overrides, and a parent with children cannot be deleted.
Dated assumption periods and scenario events remain local to their owning scenario.
Scenarios saved by the preceding alpha retain all of their former values as deliberate
overrides when first read.

In GTK, Projection and file imports run in the background. Longer projections show
progress and can be cancelled; imports always remain one atomic undo step, so a
cancelled import writes nothing. The open book and import source should remain in
place until the operation reports completion or cancellation. After a successful
import, the visible book is refreshed once from the GTK main loop.

Both GTK4 and web schedule/scenario editors expose a projection growth policy:

- `auto` — infer the appropriate ordinary household behavior;
- `none` — keep the event nominally fixed;
- `income` — apply the scenario income-growth assumption;
- `inflation` — apply the scenario expense-inflation assumption.

Formula-driven schedules are fixed by default in `auto` mode so, for example, a
formula mortgage is not inflated merely because one leg posts to interest expense.
Mixed gross-to-net payroll schedules grow as one balanced event under income growth.

### Account types

Every account has one visible BreadSched type. The type determines both its ordinary
accounting class and its household-planning behavior; there is no separate account
kind to configure. Imported accounts retain their exact GnuCash source type as
read-only interoperability information, but that source label does not overwrite a
BreadSched type selected by the user.

| Type | Meaning |
| --- | --- |
| **Cash** | Immediately spendable physical or on-hand funds. |
| **Bank** | Immediately spendable institutional funds, with bank-statement, reconciliation, import, and payment-source workflows. |
| **Asset** | General property or non-liquid value that contributes to net worth but is not assumed to be available cash. |
| **Investment** | Market-valued security holdings with dated prices, returns, contributions, withdrawals, and future lot support. |
| **Retirement** | Restricted or tax-advantaged saving with retirement-contribution and distribution semantics. Investment descendants inherit the retirement context. |
| **FSA / benefit** | Benefit availability determined by plan-year elections and claims rather than the custodial ledger balance. |
| **Escrow** | Restricted funds whose contributions are planning expense and whose later disbursements must not count the same expense twice. |
| **Credit card** | A revolving purchase and payment channel with a statement cycle, payment day, paid-in-full state, and optional interest. |
| **Loan** | Amortizing debt with principal, interest, a payment schedule, and an optional linked asset. |
| **Liability** | An obligation for which BreadSched should not assume credit-card or amortizing-loan behavior. |
| **Income** | A household inflow category rather than a balance-sheet account. |
| **Expense** | A household consumption or outflow category. |
| **Equity** | Opening balances and ledger adjustments; normally an advanced account. |

Root and Technical types are structural/import-only and are not ordinary user
accounts. Explicit split planning purposes remain available when one transaction's
meaning needs to override normal account-type inference.

Expense, Loan, general Liability, Escrow, and carried-balance Credit card accounts
can be included in or excluded from emergency-fund sizing. They are included by
default; clear **Carry in emergency fund** for costs that stop when household income
stops. Cash, Bank, Asset, Investment, Retirement, FSA, Income, Equity, Root,
Technical, and paid-in-full Credit card accounts are always excluded. A card paid
in full still affects near-term liquidity; its underlying expense categories, not
the card payment itself, determine emergency need.

### Escrow planning treatment

Escrow is kept on the balanced ledger as a restricted asset, but Plan recognizes
the household cost when cash or income funds the escrow account. A later tax,
insurance, or similar payment from escrow is shown as a draw and does not count the
covered expense a second time. If cash pays part of the same charge directly, only
that uncovered part is new expense.

Refunds and corrections are distinguished rather than treating every escrow deposit
as new spending. A vendor credit returned to escrow restores restricted funds and is
planning-neutral; money returned from escrow to spendable cash reverses prior
expense. Transfers between escrow accounts and Equity/manual balance corrections
change balances without creating household expense. A combined mortgage payment
shows its whole cash requirement once; escrow funding and interest remain expense
classifications while principal only reduces the loan.
Projection keeps a negative escrow balance visible and warns when an event creates
or worsens the shortfall.

### Security quantities, prices, and current value

Investment and Retirement accounts may name a non-currency commodity such as a
fund or stock. Transaction splits retain both the value in the transaction currency
and the quantity in the account commodity. A dated price then values those exact
units in the book's reporting currency as of a requested date. The latest applicable
quote is used in Accounts, Dashboard groups, net worth, and the opening state of a
Projection; if no usable quote exists, BreadSched explicitly retains the ledger
value instead of inventing a market price.

Use **Security price…** in the GTK Accounts view or web Accounts page to create a
security and record an exact dated price. An account uses that price after its
commodity is set to the security in the account editor. Re-entering a BreadSched
price for the same security, currency, and date updates that quote. If a new native
book has no currency commodity yet, its first price entry creates USD explicitly.
GnuCash SQLite and XML imports also preserve supported dated prices and their stable
source GUIDs.
Direct security-to-reporting-currency quotes are supported now; foreign-exchange
conversion, automatic quote downloads, lots, and cost basis remain future work.

### Investment activity

Investment meaning belongs on the split that changes the Investment or Retirement
holding. Transaction and fixed-schedule editors in GTK and web can classify that
leg explicitly:

- **Contribution** increases an investment or retirement holding from household funds.
- **Taxable withdrawal** decreases a non-retirement investment holding.
- **Retirement distribution** decreases a Retirement account or an Investment
  account nested beneath a Retirement parent.
- **Reinvested dividend** and **Reinvested interest** increase the holding while
  identifying the increase as investment income rather than a contribution.
- **Investment fee** decreases the holding and is reported separately from a withdrawal.
- **Retirement rollover** moves value between two retirement-context holdings. Both
  holding legs are classified, must balance each other, and do not change total holdings.

Projection separates these activities from market performance in its monthly
state transition, detail view, CSV, web response, and printable summary. Older or
unclassified investment movements retain the compatible fallback—an increase is a
contribution and a decrease is a withdrawal/distribution according to retirement
context—but explicit classification is required to distinguish income, fees, and
rollovers. These classifications do not calculate tax, lots, or cost basis.
BreadSched-owned classifications survive GnuCash re-import when a schedule leg can
be matched unambiguously by account.

## Primary GTK4 workflow

The GTK4 application is the reference user experience. Its major views include:

- **Dashboard** — household position, separate expected-income and pending-bill
  lists, liquidity,
  emergency-fund information, and important linked-account relationships. Dashboard
  groups accept explicit account-style paths such as `Investments:Plan A`; generated
  headings total their children, account subtrees are counted once, hidden accounts
  are not direct group members, FSA groups report benefit availability rather than
  custodial balance, and fully repaid loans are omitted. Assigning either side of a
  linked property/loan pair includes its unassigned companion so the group reports
  equity, LTV, and the latest bounded repayment date. Generated parent headings roll
  up only equity/total; property value, amount owed, LTV, and loan end remain on the
  specific loan/property line. A link by itself still creates no group, and
  unassigned accounts do not appear in dashboard groups.
- **FSA Dashboard** — open and recently closed benefit years, election availability,
  and open healthcare claims, kept separate from general household liquidity.
- **Accounts** — hierarchical chart of accounts with balances, account type, and
  account metadata.
- **Register** — account transaction history using account-appropriate debit/credit
  terminology.
- **Scheduled** — separate lists for recurring commitments/account payments and
  planning estimates, including imported schedule details and safe editing where
  round-trip fidelity is possible. Editable schedules
  can be duplicated as reviewed drafts; existing ledger transactions can seed new
  drafts; and definitions can be deleted without deleting transactions already posted.
  Account-linked credit-card payments are shown here without creating duplicate
  saved schedules, and **New loan…** previews and creates amortizing loans.
- **Plan** — category and planning-flow views derived from dated planned and actual
  activity, with per-book controls and non-duplicating row, period, and net-cash
  totals.
- **Review** — resolution of actual transactions against planned occurrences and
  related household workflows.
- **Projection** — saved assumptions and scenarios projected forward from the
  current book.

Dashboard, Plan, and Projection can be printed from the GTK toolbar or **File →
Print Current View** (`Ctrl+P`). BreadSched opens a self-contained print preview in
the default browser using the values already applied in the view; the browser's
print dialog can send it to a printer or save it as PDF. Plan output retains its
applied date range, grouping, measure, scenario, and totals. Projection output
includes its annual assumptions, chart, year-end table, warnings, and any displayed
comparison. If a Projection calculation is still running, printing waits briefly
for that calculation so it cannot silently reopen the preceding report instead.

Plan printouts lead with the cash outlook and omit the private book path. Category,
mortgage, and balance-sheet detail remains visible in the preview but is an optional
print appendix, disabled by default. Select **Include category detail when printing**
in the preview when that supporting detail is needed; its table starts on a new page
and repeats column headings.

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

Use **Print** in the header to print whichever web view is currently displayed or
save it as PDF. Print styling removes navigation and action buttons, expands Plan
tables beyond their on-screen scroll area, and includes the selected controls and
calculated report content.
For Plan, the same optional category-detail control keeps the default printout
focused on spendable cash and moves supporting detail to a paginated appendix.

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
breadsched restore household.backup restored-household.breadsched
breadsched accounts household.breadsched
breadsched register household.breadsched "Assets:Checking Account" --limit 20
breadsched balance household.breadsched --as-of 2026-06-30
```

Use `breadsched --help` and each subcommand's `--help` for the currently supported
command surface.

GTK exposes **Back Up Current Book**, **Restore Backup as New Book**, and **Verify
Current Book** in the File menu. Verification runs read-only in the background and
checks both the SQLite file and BreadSched's financial relationships. The web
interface provides the same read-only verification report in its Verify view.
Restoring over a file held by a live writer is refused; GTK restores to a different
path and opens that verified result.

## Registers and quick entry

Each register shows one account with household-language debit/credit headings such
as Deposit/Withdrawal or Payment/Charge. The filter searches descriptions, numbers,
notes, split memos, and account names without changing the full-ledger running
balance. **Open in new window** creates an independently navigable register; its
account, filter, selection, and expanded row do not replace the main register's
state. The web equivalent opens a token-preserving browser window with the selected
register.

Quick entry records an ordinary two-sided transaction directly above the register.
Choose the other visible account, enter a positive amount, then use the button whose
label describes the effect on the displayed account. BreadSched constructs one
balanced two-split `Transaction` and commits it atomically through the same database
path as the full editor. Use the full transaction editor for additional splits,
notes, reconciliation metadata, FSA links, or investment classifications. Hidden
accounts remain readable where already referenced but are never offered for a new
quick-entry transfer.

## Statement reconciliation

Bank, Cash, Asset, Investment, Retirement, FSA, Escrow, Credit card, Loan,
Liability, and Technical registers can be reconciled against a dated statement.
Choose **Reconcile…**, enter the statement date and ending balance, then check the
eligible entries until the exact running difference reaches zero. Already-cleared
entries begin checked. Finishing changes the selected splits to reconciled in the
same atomic book operation that completes the persisted statement session.

Cancel leaves ledger splits unchanged and retains an audit record. The most recent
completed statement can be reopened for correction; its entries return to Cleared
until it is balanced and finished again. Later completed statements must be reopened
first. The web register exposes the same workflow through the shared engine.

## GnuCash compatibility

BreadSched can import GnuCash SQLite and compressed-XML books and preserves source
GUIDs where possible so subsequent imports can identify the same records. Import
uses the same validated internal sink regardless of source format.

Re-importing the same GnuCash book updates source-owned data and removes transactions
that have disappeared from the source. A source-deleted transaction still used by a
BreadSched reconciliation or FSA claim is retained and reported for review rather
than leaving a broken local reference.

Compatibility is intentionally important during BreadSched's transition toward a
standalone household ledger. The goal is not business-feature parity with GnuCash;
it is to preserve enough household ledger semantics that users can use BreadSched's
planning and projection capabilities without abandoning a mature existing book.

Current compatibility includes accounts, transactions, commodities, dated prices, scheduled
transactions, formula schedules, reconciliation state, and an expanding set of
account metadata. QIF and OFX imports detect period-vs-comma decimal conventions
from the source file rather than silently stripping punctuation. GTK4 and web import
workflows can override that detection when a file is ambiguous. QIF likewise detects
month-first versus day-first date order from the complete file when the source gives
unambiguous evidence. Ambiguous number/date formats can be selected explicitly by
importer callers instead of being guessed per transaction.
GnuCash SQLite and XML scheduled formulas that the bounded BreadSched formula
engine can evaluate—including safe financial functions and occurrence variables—
remain dynamic after import. Arbitrary Python execution is never enabled.
The last successfully imported source is remembered separately for each BreadSched
book and preselected the next time that book's GTK or web import workflow opens. A
remembered path is only presentation state: it never triggers an import automatically,
and imported source files remain read-only.

Imported account provenance is read-only and separate from BreadSched's visible
account type. BreadSched retains the exact GnuCash source GUID even when an imported
root or top-level account is adopted into the existing chart, so later source
renames do not create duplicate accounts. Historical GnuCash account types are
mapped conservatively, while an unknown type remains visible and initially uses the
non-planning Technical type for review. The GTK account editor's **GnuCash source →
Details** action and the web Accounts **Details** action show the source type, GUID,
and typed fields that BreadSched preserves without interpreting.

Imported account notes are shown with that read-only source provenance. They are
separate from the editable local Notes field, so a GnuCash re-import may refresh
the source note without overwriting BreadSched planning context. Older alpha data
is separated automatically only when its retained typed source field proves where
the shared note originated.

For an imported account, GnuCash-owned chart fields—name, parent, code,
description, commodity/SCU, placeholder, and hidden state—are inspectable but not
editable in BreadSched. Change those fields in GnuCash and re-import. BreadSched's
account type, local notes, dashboard group, projection settings, and household
relationships remain editable because they are local planning decisions.

GnuCash transaction-level notes are retained separately from split memos and from
BreadSched-authored notes. Imported notes are visible but read-only and refresh from
the source on re-import; local notes remain editable and are not overwritten.

Import problems are reported per record rather than aborting the
entire book whenever safe recovery is possible.

The web import view accepts a local file path visible to the BreadSched process; browser-native
file upload is planned as a convenience improvement. Native QIF and OFX/QFX importers
are also available for bank/cash/credit-card style
history. Their supported and pending formats are tracked in `ROADMAP.md`.

## Dashboard liquidity and bill reserves

The general Dashboard presents committed scheduled bills and expected income in
separate lists; planning estimates remain in Plan and Projection rather than appearing
as obligations in Dashboard or Upcoming. Dashboard cash-flow figures distinguish
committed activity from the outlook including budgeted estimates. A bill's **Hold
now** reserve accrues on actual income dates and
in proportion to each income event's share of all income in that bill cycle; income
rows never have a hold. If no income is identified before a bill is due, the full
bill is protected. An overdue bill continues to count against liquidity while
income received in its next cycle starts a separate reserve for the next occurrence.
Paid-monthly cards contribute their full current balance on the configured payment
date; cards carrying a balance contribute the configured usual payment, capped at
the balance. If a payment becomes overdue, its amount is frozen at the card balance
on that due date. Later card activity is held separately for the next due date, so
the statement balance is never held twice. Any later positive payment from a cash-
like account—including a partial payment—resolves the overdue occurrence; a refund
or payment reversal does not. Account-payment rows omit monthly and annual
normalizations because the balance is already the dated obligation. An explicit
payment schedule takes precedence over the account-derived card rows.

A scheduled purchase charged to a credit card is still a planning expense, but it
is not an immediate spendable-cash bill. The generated card-payment row is the
current known cash obligation and does not pre-add future scheduled purchases to
the card balance. Those purchases affect liquidity after they post to the card,
preventing the purchase and its later payment from being counted as two cash needs.

## FSA / benefit planning

FSA accounts can carry funding years with election and run-out information.
BreadSched separates benefit availability from the custodial ledger balance and can
associate service/claim episodes with healthcare payments, reimbursements,
allocations, refunds, and rejected reimbursement attempts. The dedicated **FSA
Dashboard** shows benefit-year availability and open claims.

This is a household-specific feature area rather than a general accounting model;
its continuing work is tracked in the roadmap.

## Book files and safety

BreadSched native books use the `.breadsched` suffix and SQLite storage. During the
alpha period, each release supports a rolling one-version migration window; older
and newer schema numbers are rejected explicitly rather than guessed. A writable
migration first creates a verified `.pre-migration-vN.bak` beside the book, then
applies all changes in one transaction. If migration fails, the book rolls back and
the backup remains available for recovery. Read-only opens never migrate. This
boundary does not affect GnuCash, QIF, OFX, or QFX import. Verify and backup
operations are available from the CLI. A writable book is protected by a small sidecar lock file;
a second process may still open the book read-only, but cannot become a competing
writer. Clean shutdown removes the lock, and a stale same-host lock is reclaimed when
its recorded process no longer exists.

Do not place the only copy of a financial book in a location whose synchronization
or filesystem behavior is not safe for SQLite. BreadSched warns when a book path is
inside a recognized OneDrive, iCloud Drive, Dropbox, or Google Drive root. Keep
independent backups.

Settings use the platform's normal per-user configuration directory, and the default
book chooser follows the platform Documents location (including XDG `user-dirs.dirs`
on Linux and common OneDrive Documents redirection on Windows).

## Project status

BreadSched is under active development and is not yet a complete GnuCash replacement.
The current hardening phase is deliberately prioritizing financial correctness,
storage integrity, importer preservation, security, and realistic-book performance
before another broad feature-expansion cycle.

For the authoritative pending-work list, see [`ROADMAP.md`](ROADMAP.md).
For architectural rationale, see [`DESIGN.md`](DESIGN.md).

## Licence

See the repository licence file for the project's licensing terms.
