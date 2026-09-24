# BreadSched User Guide

BreadSched is a household-finance application for keeping an exact double-entry
ledger, planning cash flow from dated financial events, and comparing alternate
multi-year projections. This guide describes the GTK desktop application, the web
interface, and the command-line tools shipped in the same release. The installed
copy is release-specific; consult the guide packaged with the BreadSched version
you are running.

BreadSched is under active development. Keep independent backups of any financial
book and review imported or inferred data before relying on it.

## Getting started

### Install for development

From a BreadSched source checkout, install the application and its development
dependencies with:

```bash
pip install -e ".[gui,dev]"
```

The GTK application also requires a GTK 4 runtime and PyGObject supplied by your
operating system. Run `breadsched-gtk --help` for platform-specific package hints
when the GTK stack is unavailable.

### Start an interface

Open a native BreadSched book in the GTK desktop application:

```bash
breadsched-gtk household.breadsched
```

With no book argument, the GTK application offers to create a book, open a book,
or import a GnuCash book into a new BreadSched book. The equivalent commands are
`breadsched gui` and `python -m breadsched.gui`.

Start the loopback web interface with:

```bash
breadsched web household.breadsched
```

Do not expose the development web server on an untrusted network.

The command line can create and inspect books without GTK:

```bash
breadsched init household.breadsched
breadsched --help
```

### Create or import your first book

For a new household book:

1. Choose **File → New Book** in GTK, or run `breadsched init BOOK`.
2. Create the accounts that hold money, obligations, income, and expenses.
3. Record opening balances as balanced transactions. Equity is normally the other
   side of an opening-balance transaction.
4. Add known recurring commitments in Scheduled.
5. Add planning estimates for recurring activity that is not a commitment.
6. Review the Plan and Dashboard before building longer Projection scenarios.

To begin from GnuCash, choose **File → Import GnuCash Book into New Book**. Keep the
GnuCash source file in place while the import runs. BreadSched reads it without
modifying it and stores imported source identifiers so later imports can update the
same records.

## Understand the model

### Ledger and exact amounts

Every transaction is balanced and owns two or more splits. BreadSched stores exact
rational values instead of binary floating-point approximations, preserving source
numerators and denominators where possible. Account balances are derived from
transactions; editing a displayed total never bypasses the ledger.

### Dated planning instead of monthly cells

The Plan is built from events with dates:

- actual ledger transactions;
- scheduled commitments;
- scheduled estimates;
- scenario-specific additions, replacements, and suppressions.

Changing Plan from Month to Quarter or Year only changes how those events are
grouped. It does not move an event or manufacture a monthly allocation. For example,
a weekly grocery estimate produces occurrences on its weekly cadence, a
semi-monthly paycheck produces two dated occurrences, and an annual insurance bill
remains on its due date even in a monthly report.

### Commitments and estimates

A commitment is an event you expect to occur and may post, such as rent, a loan
payment, or a paycheck. An estimate fills a planning gap, such as groceries or an
irregular utility amount. Dashboard and Upcoming show commitments as obligations;
estimates belong in Plan and Projection and are not presented as bills already due.

**Suggest from History** creates reviewable estimate drafts. It never writes a
schedule directly. Open **Review**, check the inferred account, purpose, amount,
cadence, dates, seasonal profile, and evidence, then save or cancel. Accepted
estimates retain their evidence as provenance. The evidence names the versioned
inference rules used for anomaly handling, cadence, trend, seasonality, confidence,
and funding selection, so a later rule revision does not disguise how an older
suggestion was produced.

### Actuals and planned occurrences

When a real transaction resolves a planned occurrence, BreadSched retains the
original expected date and amount. Editing a schedule later therefore does not
rewrite historical variance. In Plan, select a value to inspect its occurrences
and actual splits. Use **Resolve actuals…** when an actual needs to be matched,
marked unexpected, or reviewed. Correct a posted split's planning purpose in the
transaction editor; correct a scheduled purpose in the schedule editor.

## Navigate the application

The GTK sidebar and the web navigation expose the same main work areas:

- **Dashboard** summarizes household position, expected income, pending bills,
  liquidity, emergency-fund information, and linked assets and loans.
- **FSA Dashboard** shows benefit-year availability and open healthcare claims.
- **Accounts** is the hierarchical chart of accounts with balances and metadata.
- **Register** shows the transaction history for one account.
- **Scheduled** separates commitments and account payments from estimates.
- **Plan** compares dated planned and actual activity over a selected horizon.
- **Review** resolves actual activity and supports related review queues.
- **Projection** calculates future state under Base or saved scenarios.

Use **View** to switch work areas. Registers can also open in independent windows;
their account, filter, selection, and expanded row do not replace the main window's
register state.

Press `F1` or choose **Help → User Guide** in GTK to reopen this guide.

## Accounts

### Choose an account type

Each account has one visible BreadSched type. It determines its accounting class
and default household-planning behavior:

- **Cash** is immediately spendable physical or on-hand money.
- **Bank** is immediately spendable institutional money and supports statement,
  reconciliation, import, and payment-source workflows.
- **Asset** is property or non-liquid value included in net worth.
- **Investment** holds market-valued securities and investment activity.
- **Retirement** holds restricted or tax-advantaged savings. Investment children
  inherit the retirement context.
- **FSA / benefit** uses plan-year elections and claims to determine availability.
- **Escrow** is restricted funding whose later disbursement must not count the same
  expense twice.
- **Credit card** is a revolving purchase and payment channel.
- **Loan** is amortizing debt with principal and interest behavior.
- **Liability** is an obligation without credit-card or loan assumptions.
- **Income** and **Expense** are household flow categories.
- **Equity** is used for opening balances and ledger adjustments.

Root and Technical types are structural or import-only. An explicit planning
purpose on a split can override normal type inference when a particular transaction
has a different household meaning.

### Organize and edit accounts

Use parent accounts to create a readable hierarchy. Placeholder accounts organize
children but should not receive transaction splits. Hidden accounts remain visible
where old records refer to them but are omitted from new-entry choices.

Imported GnuCash chart fields are read-only because GnuCash owns those values.
Change the source name, parent, code, description, commodity, placeholder, or hidden
state in GnuCash and import again. BreadSched-local type, notes, Dashboard group,
projection settings, and household relationships remain editable.

### Security prices and current value

For an Investment or Retirement account, use **Security price…** to define a
security and record an exact dated price. Assign the account's commodity to that
security. Accounts, Dashboard, and Projection use the latest applicable direct
quote in the reporting currency; if no usable quote exists, BreadSched explicitly
retains the ledger value instead of inventing a market value.

## Transactions and registers

Select an account to open its register. Headings use account-appropriate household
language such as Deposit/Withdrawal or Payment/Charge. The register filter searches
descriptions, numbers, notes, split memos, and account names without changing the
full-ledger running balance.

Quick entry creates an ordinary two-split transaction:

1. Choose the other visible account.
2. Enter a positive amount and the transaction details.
3. Choose the button whose label describes the effect on the displayed account.

Use the full transaction editor for additional splits, notes, reconciliation
metadata, FSA links, or investment classifications. A transaction cannot be saved
unless its exact splits balance.

### Reconcile a statement

Bank, Cash, Asset, Investment, Retirement, FSA, Escrow, Credit card, Loan,
Liability, and Technical registers can be reconciled:

1. Choose **Reconcile…** from the register.
2. Enter the statement date and ending balance.
3. Check eligible entries until the exact difference is zero.
4. Finish the session to mark the selected splits reconciled.

Cancel leaves ledger splits unchanged and retains an audit record. The most recent
completed statement can be reopened for correction; its entries return to Cleared
until it balances and is finished again. Reopen later completed statements first.

## Scheduled activity

Scheduled transactions support recurrence bounds, weekend adjustment, skipped
occurrences, one-time overrides, future-effective amount changes, multiple splits,
and bounded formulas.

Use **New transaction** for a fixed or formula-driven commitment or estimate. Review
all split signs and purposes. Fixed multi-split schedules can change individual
signed legs from an effective date, but every effective set must still balance.

Imported schedules that BreadSched cannot reproduce safely remain visible but
read-only and are excluded from planning, projection, and posting. Duplicate one
into an independent definition if you want to translate and review it without
altering the imported evidence.

### Credit-card payments

Configure payment behavior on the Credit card account: paid in full or carried
balance, payment day, usual carried-balance payment, and optional **Paid from** Bank
or Cash account. BreadSched then shows the known obligation consistently in
Dashboard, Scheduled, and Upcoming. An explicit or imported payment schedule takes
precedence. The generated account-linked row is informational and is not posted
automatically.

### Create a loan

Choose **New loan…** in Scheduled and enter the amount borrowed, annual rate, term,
first payment, loan account, interest expense account, and payment account. Review
the level payment and the first year of principal/interest allocation before
saving. BreadSched creates one formula schedule and can record the opening
liability.

## Dashboard and near-term cash

Dashboard keeps expected income and committed bills separate. **Hold now** is the
portion of current cash reserved for a bill. It accrues on actual income dates in
proportion to the income received during that bill cycle. If there is no identified
income before the due date, the full bill is protected.

An overdue bill remains in liquidity until resolved. A paid-in-full card contributes
its balance on the payment date; a carried card contributes the lesser of its
balance and usual payment. Scheduled card purchases remain planning expenses but do
not become immediate cash bills, preventing a purchase and its later payment from
being counted twice.

Dashboard groups accept account-style paths such as `Investments:Plan A`. Generated
headings total their children without double-counting account subtrees. Link an
asset and loan to show equity, loan-to-value, and a bounded repayment date together.

## Plan

Choose From, Through, Group by, Show, scenario, and comparison values, then apply
them. These controls are saved with the book and shared between GTK and web.

The first section is a signed spendable-cash bridge. Income and retirement
distributions add cash. Ordinary expenses, retirement saving, benefit funding,
debt principal, and escrow funding use cash. A residual timing/financing row
explains events such as credit purchases whose expense date differs from cash
settlement. The bridge reconciles to the projected change in spendable cash and
shows the lowest balance and its date.

Income and expense detail uses familiar positive magnitudes followed by Income less
expenses. Balance-sheet classifications are informational and deliberately have no
mixed grand total. Mortgage cash requirements show the whole payment while the
interest, escrow, principal, and fee components retain their classifications; never
add the whole-payment row to its components.

Actual and variance totals stop at the report's as-of date. Future-only actual and
variance summaries are not applicable rather than zero.

Expense exploration follows the same Plan horizon, period grouping, and scenario.
Its category plan, actual, and variance come from Plan; merchant groups use the
actual transactions in a selected category and period. Descriptions are trimmed
and compared without case, and a blank description appears as **Unknown merchant**.
These groups are temporary and have actual amounts only: a category budget is not
divided into merchant budgets. In GTK Plan, select **Explore expenses…** to compare
category bars, inspect a category trend, and review merchant transactions. The web
Plan page includes the same explorer below the cash outlook. Choose a period and
category to update its tables, chart, and merchant detail. The web print view
includes the applied explorer; use **Print…** in the GTK Explorer to print the
selected category and period with its merchant detail.

## Projection and scenarios

Base is the household's current expected plan: the ledger, baseline schedules and
estimates, and Base assumptions. A saved scenario is an alternative over Base, not
a separate ledger or cached forecast. New actuals and unchanged Base schedules
therefore continue to affect it.

To compare an alternative:

1. Open Projection and create a scenario derived from Base or another scenario.
2. Override only assumptions that differ, such as income growth, inflation, return,
   or an account-specific rate.
3. Add, replace, or suppress scenario events where the alternative changes dated
   activity.
4. Calculate the scenario and compare it with Base or another saved scenario.
5. Inspect warnings and detail rather than relying only on the chart.

Untouched assumptions inherit through a deterministic, cycle-free scenario chain.
Later parent edits reach inheriting children; deliberate child overrides remain.
Dated assumption periods and scenario events belong to their owning scenario.

Projection advances account state between dated actual, scheduled, estimated, and
one-off events. Months and years are reports of those transitions, not the engine's
clock. GTK runs longer projections in the background and allows cancellation.

Schedule and scenario events have a growth policy:

- `auto` infers ordinary household behavior;
- `none` keeps the nominal amount fixed;
- `income` applies the income-growth assumption;
- `inflation` applies the expense-inflation assumption.

Formula schedules are fixed by default in `auto`, while a mixed gross-to-net payroll
schedule grows as one balanced event under income growth.

## Special household workflows

### Investments and retirement

Classify the split that changes an Investment or Retirement holding as Contribution,
Taxable withdrawal, Retirement distribution, Reinvested dividend, Reinvested
interest, Investment fee, or Retirement rollover. Projection separates these
activities from market performance. A rollover requires two balanced
retirement-context holding legs and does not change total holdings.

Unclassified legacy movements use a compatible directional fallback, but explicit
classification is needed to distinguish income, fees, and rollovers. BreadSched does
not currently calculate tax, lots, or cost basis.

### Escrow

Funding an Escrow account from cash or income is the household expense. A later tax
or insurance payment from escrow draws restricted funds without counting the same
expense again. Vendor credits, refunds to spendable cash, transfers between escrow
accounts, and balance corrections have distinct treatment. Projection leaves a
negative escrow balance visible and warns when an event creates or worsens it.

### FSA and benefit accounts

FSA accounts can hold funding years with election and run-out dates. Benefit
availability is separate from the custodial ledger balance. Claims can associate
healthcare payments, reimbursements, allocations, refunds, and rejected attempts.
Use FSA Dashboard to review open and recently closed benefit years and unresolved
claims.

## Import and GnuCash interoperability

BreadSched imports GnuCash SQLite and compressed XML books and preserves source
identifiers where possible. It supports accounts, transactions, commodities, dated
prices, schedules, bounded formulas, reconciliation state, and selected metadata.
Unsupported source details remain visible instead of being silently simplified.

Re-import updates source-owned data and removes source transactions that have
disappeared. A deleted source transaction still referenced by a BreadSched
reconciliation or FSA claim is retained and reported for review. Local planning
decisions are not overwritten by source refreshes.

QIF and OFX/QFX imports infer decimal and date conventions from whole-file evidence.
When a format is ambiguous, select an explicit override in GTK, web, or an importer
caller. Import problems are reported per record when safe recovery is possible.

GTK imports run in the background as one atomic undo step. Leave the open book and
source file in place until completion or cancellation. A cancelled import writes
nothing. The web importer currently accepts a path visible to the BreadSched server
process rather than uploading a browser-local file.

## Print, export, and inspect

Dashboard, Plan, and Projection can be printed from the GTK toolbar or **File →
Print Current View** (`Ctrl+P`). BreadSched opens a self-contained preview in the
default browser; use the browser print dialog for a printer or PDF. The preview uses
the controls and calculation already applied in the view.

Plan printing leads with the cash outlook and omits the private book path. Optional
category detail begins on a new page. Web **Print** prints the current view with
navigation and actions removed.

Use **File → Export Transactions** for transaction data. Projection also exposes
its dated state and explanations in its supported outputs.

## Protect and recover a book

Native books use the `.breadsched` suffix and SQLite storage. The application version
and the separate native schema version appear in `breadsched --version` and Verify
diagnostics.

Use these GTK File menu commands regularly:

- **Back Up Current Book…** creates an independent verified backup.
- **Verify Current Book** checks SQLite integrity and financial relationships such
  as balanced transactions, valid references, precision, scheduled realization,
  and reconciliation consistency.
- **Restore Backup as New Book…** verifies a backup, writes it to a different path,
  and opens the result. It will not overwrite the currently open live book.

Equivalent command-line operations are:

```bash
breadsched verify household.breadsched
breadsched backup household.breadsched household.backup
breadsched restore household.backup restored-household.breadsched
```

A writable book has a sidecar lock. A second process may open it read-only but
cannot become a competing writer. A stale same-host lock is reclaimed when its
recorded process no longer exists.

Do not keep the only copy of a book in a synchronization location that is unsafe for
SQLite. BreadSched warns about recognized OneDrive, iCloud Drive, Dropbox, and Google
Drive roots. Keep independent backups on separate storage.

## Command-line reference

The CLI exposes book operations and scripting-oriented access to the same core
model. Many commands support `--json` for structured output. Common examples are:

```bash
breadsched init household.breadsched
breadsched import household.breadsched accounts.gnucash
breadsched verify household.breadsched
breadsched accounts household.breadsched
breadsched register household.breadsched "Assets:Checking Account" --limit 20
breadsched balance household.breadsched --as-of 2026-06-30
breadsched estimate household.breadsched suggest --json
```

Use `breadsched --help` and `breadsched COMMAND --help` for the exact command surface
in your installed release.

## Troubleshooting

- If GTK is unavailable, run `breadsched-gtk --help` and install the named GTK 4 and
  PyGObject packages. The CLI continues to work without GTK.
- If a book opens read-only, check whether another BreadSched process has it open and
  inspect the lock information before assuming the lock is stale.
- If imported values or dates are ambiguous, rerun the import with an explicit
  decimal or date-order choice instead of editing many misread transactions.
- If Plan and actuals disagree, open value detail and the actual-resolution queue.
  Check dates, account types, explicit split purposes, and occurrence matches.
- If Projection warns that a period does not reconcile, inspect its opening state,
  dated events, accruals, and closing state. BreadSched refuses to hide that error.
- Before reporting a problem, run Verify and record `breadsched --version`. Never
  attach an unsanitized financial book to a public issue.
