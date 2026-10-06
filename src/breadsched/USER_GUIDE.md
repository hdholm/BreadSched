# BreadSched User Guide

BreadSched is a household-finance application for keeping an exact double-entry
ledger, planning cash flow from dated financial events, and comparing alternate
multi-year projections. The installed copy of this guide is release-specific;
consult the guide packaged with the BreadSched version you are running.

BreadSched is under active development. Keep independent backups of any financial
book and review imported or inferred data before relying on it.

## How this guide is organized

This overview explains what BreadSched does and the rules it follows, whichever
interface you use. Three companion parts give the steps for each interface:

- [Desktop guide](guide/desktop.md): the GTK desktop application, its menus,
  toolbar, dialogs, and keyboard entry.
- [Browser guide](guide/web.md): the loopback web interface and its pages.
- [Command-line guide](guide/cli.md): the `breadsched` commands, their options, and
  their JSON output.

Every interface can show all four parts: **Help → User Guide** (`F1`) in the
desktop application, the **Guide** page in the browser, and `breadsched guide`
(`overview`, `desktop`, `web`, or `cli`) on the command line. Windows and pages for
longer workflows, such as imports, reconciliation, schedules, paychecks, payees,
rules, reimbursables, and goals, also have a **Help** button that opens their
section of the desktop or browser part. The interfaces share one book and one set
of rules, so you can move between them freely.

## Getting started

### Install and download

From a BreadSched source checkout, install the application and its development
dependencies with:

```bash
pip install -e ".[gui,dev]"
```

The desktop application also requires a GTK 4 runtime and PyGObject supplied by
your operating system; see [Start the desktop application](guide/desktop.md#start-the-desktop-application).
The command line and the browser interface work without GTK.

When downloading a packaged alpha, choose the GitHub release marked
**Pre-release**, read its versioned notes, and compare the downloaded wheel or
source archive against the release's `SHA256SUMS` before installing it.
Release artifacts are built from the tested main commit with read-only repository
access, then published after their names and checksums are verified. The loopback
web interface serves only its packaged page, script, and stylesheet.
On Linux, each release also attaches a Flatpak bundle,
`BreadSched-<version>.flatpak`. See
[Install on Linux](guide/desktop.md#install-on-linux).

For Windows, each release attaches an installer that carries its own Python and GTK
runtime (not yet code-signed, so Windows may warn before running it; check it against
`SHA256SUMS` first). See
[Install on Windows](guide/desktop.md#install-on-windows).

### Create or import your first book

For a new household book:

1. Create the book ([desktop](guide/desktop.md#start-the-desktop-application),
   [command line](guide/cli.md#create-and-inspect-books)).
2. Create the accounts that hold money, obligations, income, and expenses.
3. Record opening balances as balanced transactions. Equity is normally the other
   side of an opening-balance transaction.
4. Add known recurring commitments in Scheduled.
5. Add planning estimates for recurring activity that is not a commitment.
6. Review the Plan and Dashboard before building longer Projection scenarios.

To begin from GnuCash, import the GnuCash book into a new BreadSched book. Keep the
GnuCash source file in place while the import runs. BreadSched reads it without
modifying it and stores imported source identifiers so later imports can update the
same records. See [Import and GnuCash interoperability](#import-and-gnucash-interoperability).

### Synthetic sample book

BreadSched can create a separate learning book
([command line](guide/cli.md#synthetic-sample-book)). Every account, transaction,
amount, and name in it is invented, and it is never added to an open household
book. The prior month has opening balances and dated wages, rent, groceries, and
utilities. The following month has recurring wages, rent, and utilities plus a
groceries Plan estimate. Dashboard Cash and Card debt groups and a two-year Base
scenario are included. Open it in the desktop application or the browser, inspect
Dashboard, then select the following month in Plan. The sample is for exploration,
not financial advice or a template whose amounts should be copied into a real book.

### Walkthrough: set up a household

This walkthrough builds a working household book from nothing. The
[command line](guide/cli.md#walkthrough-set-up-a-household) has the same steps as
commands you can run as written; in the desktop application or the browser, use the
screens linked from each step. Use your own names and amounts; the walkthrough's are
invented.

1. **Create the book.** A new book starts with Assets, Liabilities, Equity,
   Income, and Expenses.
2. **Add accounts with opening balances**
   ([Organize and edit accounts](#organize-and-edit-accounts)): a checking and a
   savings account, a retirement account, and a credit card, each with its balance
   on one opening date. Equity takes the other side, so net worth is right from the
   first day. Give the card its usual payment, payment day, and paying account so
   the Dashboard counts the payment as a bill
   ([Credit-card payments](#credit-card-payments)).
3. **Add income and expense categories**, such as Salary, Housing, Groceries, and
   Utilities.
4. **Describe the money that comes and goes**
   ([Scheduled activity](#scheduled-activity),
   [Commitments and estimates](#commitments-and-estimates)). Pay and rent are
   usually scheduled transactions; spending that varies, such as groceries, is a
   Plan estimate. Add a loan with [Create a loan](#create-a-loan) so each payment
   splits principal and interest, and schedule regular transfers into savings or
   retirement.
5. **Add a savings goal** ([Savings goals](#savings-goals)) for something you are
   saving toward.
6. **Check the near term.** The Dashboard
   ([Dashboard and near-term cash](#dashboard-and-near-term-cash)) shows what is
   liquid, what is needed in the next 30 days, and what goals set aside; the
   [Plan](#plan) shows each month's planned income and spending.
7. **Save a Base scenario and project it** ([Projection and scenarios](#projection-and-scenarios))
   to see cash and net worth year by year, and whether each goal is covered.

From then on, record what actually happens (enter, import, or review transactions)
and the Plan compares it with what was expected.

### Walkthrough: compare scenarios

Scenarios answer "what if" without changing the book
([command line](guide/cli.md#walkthrough-compare-scenarios)):

1. Save a new scenario under Base, changing only what differs: here lower
   investment returns and higher inflation
   ([desktop](guide/desktop.md#plan-and-projection),
   [browser](guide/web.md#plan-and-projection)).
2. Change a savings goal in that scenario only, such as a later target date
   ([Savings goals](#savings-goals)); Base keeps the original.
3. Project the new scenario, then compare it with Base year by year. The
   comparison lists each year's net worth in both and the difference.

A child scenario inherits everything from its parent, so later changes to Base
carry into it unless it overrides them.

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

**Suggest** (Suggest Estimates from History) creates reviewable estimate drafts. It
never writes a schedule directly. Review the inferred account, purpose, amount,
cadence, dates, seasonal profile, and evidence, then save or cancel. Accepted
estimates retain their evidence as provenance. The evidence names the versioned
inference rules used for anomaly handling, cadence, trend, seasonality, confidence,
and funding selection, so a later rule revision does not disguise how an older
suggestion was produced.

### Actuals and planned occurrences

When a real transaction resolves a planned occurrence, BreadSched retains the
original expected date and amount. Editing a schedule later therefore does not
rewrite historical variance. A scheduled occurrence is matched by amount only to
an actual in the same transaction currency. If an older link connects different
currencies, its occurrence variance is unavailable. Plan totals convert other
currencies as described under [Plan](#plan). In Plan, open a value to inspect its
occurrences and actual splits, and use **Resolve actuals** when an actual needs to be
matched, marked unexpected, or reviewed. Correct a posted split's planning purpose
in the transaction editor; correct a scheduled purpose in the schedule editor.

Review explains its suggestions. Each planned item it offers for an actual is a
**Close match** (within two days and 5% of the expected amount) or a **Possible
match**, with the reasons: the accounts it shares, how far the amount and date are
from the plan, and any words the descriptions share. When nothing is offered, Review
says why: the nearest planned item using those accounts is outside the seven-day
window, is planned in another currency, or was rejected, or no schedule uses the
accounts at all. Each action says what it will do: **Match** marks the planned item
done, **Reject** stops offering it for this transaction, **Skip** removes the planned
item from the plan, and **Unexpected** records the transaction as unplanned. A
transaction that moves FSA money also says which FSA flow it is (payroll funding, a
payment from the FSA card, a reimbursement, or a provider refund) and whether and how
to attach it to a claim.

## Work areas

The desktop application and the browser offer the same work areas:

- **Dashboard** summarizes household position, expected income, pending bills,
  liquidity, emergency-fund information, and linked assets and loans.
- **FSA Dashboard** shows benefit-year availability and open healthcare claims.
- **Accounts** is the hierarchical chart of accounts with balances and metadata.
- **Register** shows the transaction history for one account.
- **Scheduled** separates commitments and account payments from estimates.
- **Plan** compares dated planned and actual activity over a selected horizon.
- **Review** resolves actual activity and supports related review queues.
- **Projection** calculates future state under Base or saved scenarios.

Payees, categorization rules, reimbursable expenses, imports, and this guide have
their own screens. How to reach each one is in the
[Desktop guide](guide/desktop.md#find-your-way-around) and the
[Browser guide](guide/web.md#find-your-way-around); the command line has a
command for most of them ([Command-line guide](guide/cli.md)).

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
- **Receivable** holds money others owe you back, such as a reimbursable expense.
  It counts toward net worth but never toward liquidity, because it cannot be spent
  until it arrives. A GnuCash A/Receivable account imports as this type.
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

### Currencies and exchange rates

In Accounts, a foreign-currency ledger balance can be shown in the reporting
currency when a dated direct quote exists, or by inverting a reverse pair if no
direct quote applies. The quote evidence shows its date, source, and any
inversion (**inverse rate**); a missing quote keeps the original ledger amount and
names its currency. Do not interpret a missing-quote fallback as a converted
balance. Account chart rollups and cash/net-worth summaries require a direct or
inverse pair quote for each nonzero foreign balance. If one is absent, the total
reads **Missing reporting-currency quote**, and the browser and command-line data
list the affected accounts. Once quotes exist, exact converted amounts are summed
before display. Other reports follow the policies in
[Complete, partial, and unavailable values](#complete-partial-and-unavailable-values).

An eligible direct dated quote is preferred; if none exists, the latest eligible
reverse pair is inverted. The direct quote wins even when the reverse quote is newer.
An older quote remains eligible and visibly dated; BreadSched does not apply an
automatic age cutoff. BreadSched converts only with a rate between the two
currencies themselves (or its inverse); it never goes through a third currency. If
you hold Swiss francs and have only franc-to-euro and euro-to-dollar rates, enter a
franc-to-dollar rate to value them in dollars. Do not add values in unlike
currencies when estimating net worth.

A manual exchange rate is entered as target units per one source unit, for currencies
already in the book ([desktop](guide/desktop.md#exchange-rates-and-security-prices),
[browser](guide/web.md#exchange-rates),
[command line](guide/cli.md#exchange-rates)). A repeated manual entry for the same
pair and date updates that manual quote without replacing imported evidence.
Entering a rate never creates a currency or alters ledger transactions. Imported
exchange quotes remain available offline. When an OFX card or bank statement shows a
foreign-currency purchase with its exchange rate, that rate is saved as the day's
exchange quote and the purchase is posted in the account's currency at that rate.

Plan totals and Expense Explorer convert foreign amounts with these rates (see
[Plan](#plan)). Other views still require a compatible reporting-currency value.
Review the quote date and source before treating a market-valued total as current.

### Complete, partial, and unavailable values

Every value in the reporting currency is in one of four states:

- **Complete:** every amount it covers was converted. An old quote still counts;
  its date and source stay visible in the currency note or quote evidence.
- **Genuine zero:** nothing to convert, such as a foreign account with a zero
  balance, which needs no quote. A zero is always complete.
- **Partial:** a subtotal of the amounts that converted, labelled
  **Partial: excludes N unconverted amounts** beside the number.
- **Unavailable:** withheld rather than shown partial, labelled
  **Unavailable: N amounts lack a quote**, or with the older wording
  **Missing reporting-currency quote**.

Each report applies one policy:

| Report | Policy when an amount has no quote |
|---|---|
| Plan totals, period columns, category cells, and the cash bridge | Partial subtotal; a period or cell that leaves something out is marked **partial** |
| Plan summary through the as-of date | Partial, counting only exclusions dated on or before the as-of date |
| Expense Explorer charts and tables | Partial for each period; **Remaining** is unavailable ("Currency conversion unavailable"), and rollover stops at that period |
| Projection and its chart | Partial from the first month that leaves out a balance or event; the chart shades those months |
| Scenario comparisons (Plan and Projection) | As complete as both scenarios together: a difference between partial values is partial |
| Accounts rollups, Dashboard net worth and liquid cash, Dashboard groups | Unavailable |
| Net worth history and net worth change | Unavailable for that point or change; the chart marks it **n/a** rather than drawing zero |

A partial or unavailable value comes with a list of what it leaves out. The browser
shows the list when you expand the label, the desktop application shows it in the
tooltip or note, and printed reports, `--json` output, and the command-line notes
include it. Each entry gives the account or event, its unconverted amount and
currency, the date, and whether an exchange rate or a security price is missing,
followed by the fix: for example, "Add a EUR exchange rate in Accounts to include
it." A security with no price at all lacks a *price*; one priced only in another
currency lacks an *exchange rate*.

For example, a Plan for October with a 1,000.00 USD rent and a 500.00 EUR rent,
and no EUR quote, shows rent of 1,000.00 marked partial and a note listing
"Flat in Lyon 500.00 EUR (planned 2026-10-03): no exchange rate". The EUR amount is
never counted as dollars. After you enter a EUR→USD rate of 1.10 dated before the
as-of date, the same cell reads 1,550.00 and the label disappears. Being
unavailable because of a missing quote is different from being **not applicable**
(**—**) because a period has not started yet (see
[Reporting terms](#reporting-terms)).

### Security prices and current value

Record a security and its exact dated price for an Investment or Retirement account
([desktop](guide/desktop.md#exchange-rates-and-security-prices)), and assign the
account's commodity to that security. Accounts, Dashboard, and Projection use the
latest applicable direct quote in the reporting currency; if no usable quote exists,
BreadSched explicitly retains the ledger value instead of inventing a market value.
The Accounts views show the date and source of a selected security quote.

A security priced only in another currency (for example a fund quoted in euros) is
valued in that currency and converted with the exchange rate in effect on the same
date; the evidence then shows both, such as `2026-03-01 · ofx; EUR→USD 2026-03-02 ·
bank`. Without that exchange rate the account says so, shows its value in the quote
currency, and is left out of reporting-currency totals until you enter the rate.

Security prices come in with imports too: GnuCash prices, a QIF file's price list,
and the security prices in an OFX investment statement are recorded for securities
already in your book (matched by symbol). A price for a security the book doesn't
have is listed as skipped rather than creating a new security. Imported prices never
replace one you entered yourself.

## Transactions and registers

A register lists one account's entries oldest first, like a check register, with a
full-ledger running balance. Headings use account-appropriate household language
such as Deposit/Withdrawal or Payment/Charge. Filtering the register searches
descriptions, numbers, notes, tags, split memos, payees, and account names without
changing the running balance. A transaction cannot be saved unless its exact splits
balance.

Both the desktop and browser registers end in a blank row for typing a new entry,
with split lines for entries of more than two splits, and let you edit an existing
entry in place ([desktop](guide/desktop.md#enter-transactions-in-a-register),
[browser](guide/web.md#enter-transactions-in-the-register)).

When you type a description for a new entry, BreadSched looks for the latest
earlier transaction in this account whose description matches (ignoring case,
punctuation, and words containing digits, such as store numbers). It fills only what
you have not yet typed or chosen: the transfer account (or every split line of a
multi-split entry), the amount under the same heading, and the payee, and says where
they came from. Nothing is saved until you confirm. Notes, reconciliation, planning
links, and FSA claims are never copied, and editing an existing transaction never
proposes anything.

Editing in place keeps notes, planning purposes, investment classifications,
reconciliation, and FSA links, which the row does not show. Changing an amount in an
existing transaction updates both of the numbers BreadSched keeps for each split. A
split in an account held in another currency or commodity is the exception:
BreadSched refuses to change its amount without its converted quantity, rather than
keep a quantity that no longer matches. Use the full transaction editor for
additional splits, notes, reconciliation metadata, FSA links, or investment
classifications.

### Tags and linked documents

A **tag** is a short label of your own, such as *Tax* or *Home repair*, and a
transaction can carry several. Tags are matched regardless of capitals, and a new
tag takes the spelling the book already uses. A tag cannot contain a comma or be
longer than 64 characters.

A transaction can also link **documents**: a receipt, statement, or invoice file,
or a web address. As in GnuCash, the files stay outside the book. Attaching a file
copies it into an **attachment folder** beside the book (for `household.breadsched`,
the folder `household attachments`) and links it by its name there, so the book
and folder can move together. A Flatpak book outside Documents has no default
attachment folder, because the sandbox cannot use the folder beside it; choose one
first. An existing file of the same name is never
overwritten; the copy gets a numbered name. A file can instead be linked where it
is, and a web address is only linked, never fetched. Removing a document only
unlinks it: the file is kept. The browser can link only web addresses and files
inside the attachment folder, so that it cannot be used to read other files on the
computer; the desktop and command line can link a file anywhere.

A file can go missing: moved, renamed, or on a drive that is not connected. It is
then marked **missing** wherever documents are shown, and the link is kept so that
you can restore the file or **relink** it to where it is now. A missing document
never stops the book from opening or verifying.

A document linked in GnuCash (its **Linked Document**) comes across with the
transaction and is shown beside yours. It is kept as a link, never copied, and it
is refreshed from GnuCash on every re-import, while your own tags and documents
are kept. A relative GnuCash link is looked up under GnuCash's *Path head for
linked files*, which is your home folder unless you tell BreadSched otherwise
([command line](guide/cli.md#tags-and-linked-documents)).

A transaction CSV export includes a **tags** column and a **documents** column. A
book backup does not include the attachment folder: back it up with your other
files. See [desktop](guide/desktop.md#tags-and-linked-documents),
[browser](guide/web.md#tags-and-linked-documents), and
[command line](guide/cli.md#tags-and-linked-documents).

### Reconcile a statement

Bank, Cash, Asset, Investment, Retirement, FSA, Escrow, Receivable, Credit card,
Loan, Liability, and Technical registers can be reconciled: enter the statement date
and ending balance, check eligible entries until the exact difference is zero, and
finish the session to mark the selected splits reconciled
([desktop](guide/desktop.md#reconcile-a-statement),
[browser](guide/web.md#reconcile-a-statement)).

Cancel leaves ledger splits unchanged and retains an audit record. The most recent
completed statement can be reopened for correction; its entries return to Cleared
until it balances and is finished again. Reopen later completed statements first.

Reconciling imported GnuCash transactions is safe: re-import keeps their Reconciled
state and holds any GnuCash change to them for review. See
[Review GnuCash changes to reconciled transactions](#review-gnucash-changes-to-reconciled-transactions).

## Scheduled activity

Scheduled transactions support recurrence bounds, weekend adjustment, skipped
occurrences, one-time overrides, future-effective amount changes, multiple splits,
and bounded formulas. Use a new schedule for a fixed or formula-driven commitment or
estimate, and review all split signs and purposes. Fixed multi-split schedules can
change individual signed legs from an effective date, but every effective set must
still balance. If an edit is rejected, the existing schedule is retained; complex
imported definitions remain protected when a simple editor cannot preserve their
structure.

For alternate assumptions, add a scenario-only estimate or alter an eligible
baseline schedule within a saved scenario. The original baseline definition stays
intact. An invalid exception, such as skipping and overriding the same occurrence,
is rejected without changing the saved scenario.

Imported schedules that BreadSched cannot reproduce safely remain visible but
read-only and are excluded from planning, projection, and posting. Duplicate one
into an independent definition if you want to translate and review it without
altering the imported evidence.

### Review due and missed transactions

Nothing scheduled is posted until you decide
([desktop](guide/desktop.md#review-due-transactions),
[browser](guide/web.md#review-due-transactions),
[command line](guide/cli.md#review-due-transactions)). Due and missed dates are
grouped by schedule with a count and total, and each date has its own choice:

- **Post now** writes that date's transaction;
- **Remind me later**, the default, leaves it due;
- **Never (mark as done)** skips that one date without posting anything.

A schedule with several dates also offers a choice that sets all of them at once.
Applying re-checks every chosen date first: if one was already posted or skipped
elsewhere (another window, the browser, or the command line), nothing is written
and the review says so instead of posting it twice. The whole batch is one undo
step. Plan-only estimates are never offered for posting.

### Credit-card payments

Configure payment behavior on the Credit card account: paid in full or carried
balance, payment day, usual carried-balance payment, and optional **Paid from** Bank
or Cash account. BreadSched then shows the known obligation consistently in
Dashboard, Scheduled, and Upcoming. An explicit or imported payment schedule takes
precedence. The generated account-linked row is informational and is not posted
automatically.

### Paychecks and pay changes

A paycheck is an ordinary schedule whose gross pay goes into an income account and
whose net reaches a bank or cash account; every other line takes money out of the
gross. **Payroll** ([desktop](guide/desktop.md#paychecks-and-pay-changes),
[browser](guide/web.md#paychecks-and-pay-changes),
[command line](guide/cli.md#paychecks-and-pay-changes)) reads each such schedule as
of a date and groups its lines:

- **Taxes**: an expense account with “tax” anywhere in its full name, such as
  *Expenses:Taxes:Federal*;
- **Deductions**: any other expense, such as a health insurance premium;
- **Saved**: a retirement, FSA, investment, or other non-cash asset account;
- **Repayments**: a liability, such as a 401(k) loan;
- **Net deposit**: what reaches the bank, so gross minus all of the above.

A **pay change** sets a new gross from a date. Taxes scale with gross unless you
choose otherwise; you may also scale any other line, set it to a new amount, or keep
it. The net deposit takes the difference. The change is saved as per-line future
amounts, so earlier paychecks, and the Plan and Projection before that date, keep
their amounts. A paycheck already changing after that date, one with formula
amounts, or one with whole-schedule future, seasonal, or later one-time amounts is
refused with a reason, and nothing is written.

A **payroll template** describes one employer's paycheck: the income and deposit
accounts, a usual gross, and each line as a fixed amount or a percentage of gross
(for example 6.2% for Social Security). **Fill from paycheck** describes an existing
paycheck; tax lines become percentages when a percentage reproduces the amount to
the cent. A new paycheck schedule from a template takes a name, first payday, pay
period (weekly, every two weeks, twice a month on the 15th and last day, or monthly),
and optionally a different gross. Templates are kept in the book; each save or
delete is one undo step.

### Create a loan

A new loan takes the amount borrowed, annual rate, term, first payment, loan
account, interest expense account, and payment account. Review the level payment
and the first year of principal/interest allocation before saving. BreadSched
creates one formula schedule and can record the opening liability
([desktop](guide/desktop.md#scheduled-activity)).

## Dashboard and near-term cash

Net worth, Assets, and Debts always cover every asset and liability account in the
book, and Liquid always covers every cash-like account, each valued in the reporting
currency as of the Dashboard date; a missing quote makes the affected figure
unavailable. Dashboard groups only arrange accounts into rows for reading: adding,
removing, or rearranging groups never changes those totals, Needed within N days,
Months covered, or the pending bills and income. Selecting a chart parent for a
group includes its descendants in that group's row; the row shows only the group
and the selected account's own valuation note, and hovering over the row lists every
included account, one per line, with its quote date and source where it has one.

When there are no committed outgoings, Emergency fund and Months covered say
“No committed outgoings” instead of presenting zero as a measured need or duration.
Add schedules for known commitments and review Dashboard groups before relying on
those figures. The [synthetic sample book](#synthetic-sample-book) provides a
separate place to explore these controls.

After importing a GnuCash book, Dashboard starts without invented groups. Its
Net worth uses imported posted balances, and supported imported schedules
can appear as pending bills. Review the account groups and reporting-currency
quotes before relying on liquid or emergency figures; an unvalued foreign cash
balance makes dependent figures unavailable while the bill list remains visible.
Imported securities use their imported price quotes. When no income is scheduled,
next income says “No scheduled income”. A credit card that owes a balance but has
no payment day or payment schedule is not part of Needed within 30 days; the
Dashboard says so in a “Card payments not set up” note until you configure the
card's payment behavior (see [Credit-card payments](#credit-card-payments)).

Dashboard keeps expected income and committed bills separate. **Hold now** is the
portion of current cash reserved for a bill. It accrues on actual income dates in
proportion to the income received during that bill cycle. If there is no identified
income before the due date, the full bill is protected.

When a schedule has missed two or more dates, Dashboard shows it once: the missed
date range and count, the total, and how often it recurs (for example “every
month”). Its monthly and annual figures describe the schedule once rather than
repeating per missed date. Each interface can show every missed date and amount.

An overdue bill remains in liquidity until resolved. A paid-in-full card contributes
its balance on the payment date; a carried card contributes the lesser of its
balance and usual payment. Scheduled card purchases remain planning expenses but do
not become immediate cash bills, preventing a purchase and its later payment from
being counted twice.

**Set aside for goals** shows what your [savings goals](#savings-goals) have set
aside so far, and a **Savings goals** table lists each goal's target date, target,
amount set aside, what remains, and status. Available leaves it out, exactly like a bill's reserve. If a goal's
money is kept in a non-cash account (a brokerage account, say), only the part not
yet moved there is held from Available, and the card says how much that is.

**Reimbursements due** shows what payers still owe you on
[reimbursable expenses](#reimbursable-expenses), with the part that is disputed or
past its expected date. It is part of net worth and never of liquidity.

Dashboard groups accept account-style paths such as `Investments:Plan A`. Generated
headings total their children without double-counting account subtrees. Link an
asset and loan to show equity, loan-to-value, and a bounded repayment date together.
When an account needs a reporting-currency quote, Dashboard marks Net worth (and
that account's group row) unavailable. If a cash-like account needs a quote,
Liquid, Available, emergency shortfall, and Months covered are unavailable too.
Account rows identify the missing quote, and each withheld total lists the
balances it leaves out and the quote to add (see [Complete, partial, and unavailable values](#complete-partial-and-unavailable-values)); unaffected bills,
reserves, and income still appear. Every interface and the printed Dashboard share this disclosure.
Adding a direct or inverse pair quote can restore the totals without changing the
ledger.

### Net worth history

Net worth history shows how net worth has moved: assets, debts, and net worth
valued at the end of each month, quarter, or year, with the change from the
previous period. It uses the same whole-book valuation as the Dashboard's Net
worth, including market prices for securities and exchange rates as they were on
each date. The period containing today is valued on today and marked **to date**;
later periods are not shown, because the ledger has no future balances (use
Projection for those). If an account needs a quote that did not exist on a date,
that point shows no totals and names the account instead of guessing a
conversion, the chart marks it **n/a** instead of drawing zero, and the change on
either side of it is left blank (see
[Complete, partial, and unavailable values](#complete-partial-and-unavailable-values)). Each point also
lists the value of each top-level account tree, such as **Assets** and
**Liabilities**. Open it from the Dashboard
([desktop](guide/desktop.md#net-worth-history), [browser](guide/web.md#net-worth-history),
[command line](guide/cli.md#net-worth-history)).

To see why net worth moved, choose a period's change. The drill-down values net
worth at the end of the day before the period and at its end (or today), and lists
every transaction in between that changed it. It shows each transaction's asset and
debt accounts and its effect, converted with the exchange rate applicable on the
transaction's date. **Market and exchange-rate changes** is the rest of the change:
price moves on securities and currencies you already held. Postings plus that line
always equal the change exactly. Transfers between your own accounts, such as
paying a credit card from checking, do not change net worth and are only counted. If
a transaction needs a quote that did not exist on its date, it shows **Missing
quote** and the totals are withheld rather than guessed. The printout and CSV export
carry the same totals as the screen.

## Plan

Choose From, Through, Group by, Show, scenario, and comparison values, then apply
them. These controls are saved with the book and shared between the desktop
application and the browser. To compare assumptions, select a different saved
scenario or Base as the comparison and apply the controls. The comparison shows
amounts for both cases and differences for matching categories, cash-bridge entries,
mortgage requirements, and planning flows; the original scenarios and recorded
actuals remain unchanged.

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

### Reporting terms

Plan, Expense Explorer, `breadsched activity`, and printed or exported reports use
these terms with one meaning each. The **as-of date** defaults to today.

| Measure | Time window | Operands and sign | Dated after the as-of date | Shown as **—** or not applicable |
|---|---|---|---|---|
| **Plan** | Every occurrence dated in the period or horizon | Expected amounts of scheduled occurrences and estimates, as positive income or expense | Included: it is the plan | Never; zero when nothing is planned |
| **Period actual** | Every posting dated in the period | Recorded splits; refunds reduce it; income and expense are positive magnitudes | Included | Never; zero when nothing is posted |
| **Period variance** | Periods that have started by the as-of date | Period actual less Plan: positive means more spent (expense) or more received (income) | Included, because Period actual includes it | Periods that start after the as-of date |
| **Planned through as-of** | Occurrences dated on or before the as-of date | Planned change in spendable cash (income adds, spending subtracts); occurrences count whole on their dates, never prorated | Excluded | Horizons that start after the as-of date |
| **Actual through as-of** | Postings dated on or before the as-of date | Actual change in spendable cash, same signs | Excluded | Horizons that start after the as-of date |
| **Variance** (Plan summary) | Through the as-of date | Actual through as-of less planned through as-of: positive means more cash than planned | Excluded on both sides | Horizons that start after the as-of date |
| **Remaining** (Expense Explorer) | The full period, with optional carry-in | Carry-in plus Plan less actuals posted through the as-of date; negative means overspent | Excluded until that date passes | Future periods, and categories with an unconverted currency |

None of these is a bank balance. A matched occurrence
counts once on each side: its expected amount stays in Plan on its planned date and
the matching transaction counts as actual on its posting date, so the two never add
up to twice the bill. An unresolved occurrence and an estimate both remain in Plan
until their dates, whether or not an actual arrives. Amounts in another currency with
no applicable exchange rate are left out of every measure and named in the currency
note rather than counted as zero (see below).

For example, with nothing planned in September, 40 spent on September 5 and 100
entered ahead for September 25, and an as-of date of September 15, the September
column shows a period actual of 140 and a period variance of 140. The summary shows
planned 0.00, actual (40.00), and variance (40.00) through September 15, and the
detail for the September 25 transaction says it is counted in the period figures
but not through as-of. Because occurrences count whole on their dates, a bill paid
before its planned date shows as spending ahead of plan until that date passes.
Through-as-of summaries are not applicable, rather than zero, when the whole
horizon starts after the as-of date.

Schedules and transactions in another currency are converted to the reporting
currency before any Plan value is added up. Each currency uses one exchange rate:
the latest one recorded on or before the as-of date. A direct rate (for example
EUR→USD) is preferred; otherwise the reverse rate (USD→EUR) is inverted. A note with
the Plan states the rate, its date, source, and age on the as-of date, and whether
it was inverted. Judge an old rate yourself; BreadSched does not reject it.
Converted amounts are not rounded to cents before they are added up. When no rate
applies, those amounts are **not included in totals**: the note lists each one with
its currency and date rather than counting euros as dollars, and the totals, periods,
and cells they affect are marked partial (see
[Complete, partial, and unavailable values](#complete-partial-and-unavailable-values)).
Add an exchange rate in Accounts to include them.

Open a Plan value to inspect its dated planned occurrences and actual transactions,
including matching status and explanations. Category values show their account
class; planning-flow values identify the flow kind, and mortgage cash requirements
show the whole payment. These details follow the chosen scenario; Base uses the
book's saved Base assumptions. This is a read-only explanation and does not change
the selected Plan or ledger.

### Explore expenses

Expense Explorer uses the applied Plan horizon, **Group by** period (month,
quarter, or year), and scenario; changing them changes the periods and planned
amounts it shows. Actuals are recorded transactions, with the same treatment of
refunds and other special flows as Plan. Remaining uses only actuals posted through
the as-of date. Open it from Plan
([desktop](guide/desktop.md#explore-expenses), [browser](guide/web.md#explore-expenses)).

- **Spending over time** shows total plan (blue) and actual (orange) for every period
  in the applied Plan range, with a table that splits each period's actual across
  your top-level expense categories. A period marked **to date** contains the as-of
  date; **future** periods show only what is already posted, and a dashed line marks
  where they begin; **missing quote** means a foreign-currency amount could not be
  converted and is left out. Choosing a period makes it the comparison period below.
  If all your categories sit under one **Expenses** account, the split uses its
  subcategories, and anything posted to **Expenses** itself gets its own column.
- **Income over time** follows it with the same periods, markers, and notes for
  income: total planned and actual income per period, split across your top-level
  income categories (or the subcategories of a single **Income** account). Choosing
  a period there also makes it the comparison period; the comparison, category
  trend, and merchants below cover expenses only.
- **Income detail** lists what makes up one income category's total for the chosen
  period: each scheduled occurrence with its planned date and expected amount, and
  each receipt with its date and amount, grouped by payer. A receipt with no
  description is grouped as **Unknown payer**.
- The **Period** comparison shows a plan bar and an actual bar for each category,
  plus exact Plan, Period actual, Period variance, and Remaining values, sorted by
  Period actual, Plan, Period variance, or name.
- **Category trend** compares one category's plan and actual across all periods in
  the applied Plan range, and stays on that category when you change the comparison
  period or sort order.
- **Merchants — actual only** groups that category's transactions in the comparison
  period, each group with an actual total and its dated transactions.

Category values and section totals come from Plan; category hierarchy rows can
include child accounts, so do not add parent and child rows together. Period
variance may show **—** where actuals are not yet applicable, including future-only periods.
Merchant groups are temporary views of transaction descriptions, trimmed and matched
without regard to case; blank descriptions appear as **Unknown merchant**. They
include refunds in the same actual total. A category plan is never split into
merchant budgets: for example, a plan of 100 and purchases totaling 120 show a
category variance of 20, even if the purchases appear under several merchants.
The explorer does not create or save merchant rules or change the ledger.

Remaining answers how much of the selected full-period expense plan is left after
actuals posted through the as-of date. A refund increases it; overspending shows a
negative amount. Period actual and Period variance still describe the full selected
period, so a future-dated transaction may appear in Period actual before it affects
Remaining (see [Reporting terms](#reporting-terms)). A
future-only period says “Future period.” Foreign-currency amounts use Plan's
converted values; a category with an amount that no exchange rate converts says
“Currency conversion unavailable,” and rollover stops there. Remaining has no
merchant allocation and is a planning comparison, not a bank balance.

Rollover is off by default. **Carry prior periods** adds a completed period's surplus
or deficit to the next selected period. The first period starts with zero carry.
Read the displayed Carry in, Plan, actual through as-of, and Remaining as a period
bridge. A prior unavailable currency period blocks later carry with “Prior period
unavailable”; future periods never provide a carry. Turning it off returns each
period to its own remaining amount. This view choice does not change transactions,
schedules, or saved Plan settings.

## Projection and scenarios

Base is the household's current expected plan: the ledger, baseline schedules and
estimates, and Base assumptions. A saved scenario is an alternative over Base, not
a separate ledger or cached forecast. New actuals and unchanged Base schedules
therefore continue to affect it.

To compare an alternative:

1. Create a scenario derived from Base or another scenario.
2. Override only assumptions that differ, such as income growth, inflation, return,
   or an account-specific rate.
3. Add, replace, or suppress scenario events where the alternative changes dated
   activity.
4. Calculate the scenario and compare it with Base or another saved scenario.
5. Inspect warnings and detail rather than relying only on the chart.

To keep several scenarios in view, open each one's Projection in a tab of its own:
**New tab** on the desktop, where the book remembers its tabs for next time, or
**Open in new tab** in the browser. See the
[Desktop guide](guide/desktop.md#find-your-way-around) and the
[Browser guide](guide/web.md#find-your-way-around).

Untouched assumptions inherit through a deterministic, cycle-free scenario chain.
Later parent edits reach inheriting children; deliberate child overrides remain.
Dated assumption periods and scenario events belong to their owning scenario.
Review each effective assumption's source after changing a parent to see which
values a child still inherits.

Projection advances account state between dated actual, scheduled, estimated, and
one-off events. Months and years are reports of those transitions, not the engine's
clock. A calculation from edited draft controls remains temporary until you
explicitly save those controls. The Projection summary and scenario comparison use
the same dated calculation.

### How a projection reconciles

Every projected balance is explained, not estimated from a total. For each month,
and across the whole projection, BreadSched shows four bridges
([desktop](guide/desktop.md#plan-and-projection),
[browser](guide/web.md#plan-and-projection),
[command line](guide/cli.md#projection-and-scenarios)):

- **Cash**: opening cash, plus the planned events into and out of cash, plus cash
  interest, equals closing cash.
- **Investments**: opening investments, plus planned movements (contributions,
  withdrawals and distributions, reinvested income, fees, rollovers), plus
  investment performance, equals closing investments.
- **Debts**: opening debts, plus principal borrowed less repaid, plus debt
  interest, equals closing debts.
- **Net worth**: opening net worth, plus the planned events net of transfers, plus
  cash interest and investment performance, less debt interest, equals closing net
  worth. A contribution leaves cash and enters investments, and a principal payment
  leaves cash and reduces a debt, so those moves cancel here.

Interest and performance are the effects of the scenario's assumptions; the month's
detail lists each account's rate and where it came from. Every bridge ends with
**Unexplained**, which is always zero: the projection refuses to produce a month
whose balances its terms do not explain, so a non-zero value would be a defect to
report.

Amounts in another currency are converted once with the exchange rate known on the
day before the projection starts, the same date used for opening balances. This
applies to schedules, estimates, scenario events, and foreign-currency account
balances. Projection warnings state the rate, its date, source, and age, and whether
it was inverted. When no rate applies, the amount or opening balance is left out of
the projection and listed in the warnings; it is never counted as reporting
currency. The summary figures are then marked partial, and the chart shades the
months from the first one affected (see [Complete, partial, and unavailable values](#complete-partial-and-unavailable-values)). Add an exchange rate in Accounts,
then recalculate, to include it. Scenario comparisons use each scenario's converted
projection, and a difference is marked partial when either scenario is. Compare scenarios
over the same horizon to read month-by-month cash and net-worth differences.

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
availability is separate from the custodial ledger balance. A funding year can also
record the plan's rules for unused money: a **carryover limit** carries up to that
much of the unused election into the next funding year once the run-out ends (only
the rest is forfeited), and a **grace period** lets services up to its end date be
claimed against the earlier year, choosing the funding year on the claim. A plan may
have either or both; with both, grace-period claims use the earlier year first and
the carryover is what is still unused when the run-out ends. The FSA Dashboard shows
what each year carried in and carried over. Claims can associate
healthcare payments, reimbursements, allocations, refunds, and rejected attempts.
Use FSA Dashboard to review open and recently closed benefit years and unresolved
claims. Money waiting in an FSA is an FSA asset: net worth, never liquidity.

Mark an FSA as a **dependent care FSA** when it pays for child or dependent care
rather than health care. It follows that plan's rules: only what has been
contributed so far is available (never more than the election), so a large daycare
bill early in the year is paid as payroll contributions arrive and the claim stays
open meanwhile; a claim needs no EOB, so what was paid is what the FSA owes; and
nothing carries over, so a carryover limit is refused. A grace period and a run-out
still apply. The FSA Dashboard marks the account **(dependent care)**.

A claim needs attention when something is left for you to do:

- its figures disagree and it shows **Needs review**;
- the FSA reimbursed more than the claim allows and it shows **Over-reimbursed**;
- it was reopened, or its EOB was raised after the FSA reimbursed part of it, and
  money is still to be reimbursed;
- no EOB has been entered 30 days after the service;
- money is still to be reimbursed and a funding year it draws on must be claimed
  within 30 days (by its run-out date, or the plan-year end when there is none);
- a reimbursement was rejected and money is still to be reimbursed.

The Dashboard counts these claims, and the FSA Dashboard says why each one needs
attention. Its **Claims report** totals every claim by status, FSA account, funding
year, or provider: what was paid, reimbursed, rejected, and is still to come.

FSA money moves in several ways, and each is counted once. **Funding** (payroll or
a contribution into the FSA) adds to the account but never changes what the
election lets you spend. A **direct payment** from the FSA card to a provider and a
**reimbursement** from the FSA to your bank account both use the election; a
**provider refund** credited back to the FSA card gives that much back. The FSA
Dashboard's **How used** column shows each part, for example "80.00 paid from the
card; 200.00 reimbursed; 30.00 refunded to the card". The medical expense is counted
where it was charged, whichever way it was paid: a charge on a credit card that you
later pay from the bank, then claim from the FSA, is one expense, and neither the
card payment nor the reimbursement is a second one. In Plan, an FSA account's
funding and spending appear as one net **Benefit funding** row, so the cash bridge
explains every dollar the FSA covered.

When you link a transaction to a claim, BreadSched offers the roles that fit it. A
payment made with the FSA card is **Paid from the FSA card**: the claim records it as
both the healthcare payment and its reimbursement. A provider refund to the FSA card
is **Refunded to the FSA card**: the claim records it as a refund and as money
repaid to the FSA, so the claim is not left over-reimbursed. A charge paid by card or
bank is a **Healthcare payment**, and the money the FSA later sends to your bank is
an **FSA reimbursement**.

An FSA administrator's statement, imported or reconciled, brings FSA transactions
you did not enter yourself. When one clearly belongs on a claim, the FSA Dashboard
lists it under **Proposed claim links** with the claim and the role it takes
(**Paid from the FSA card**, **FSA reimbursement**, or **Refunded to the FSA card**)
and why; select **Link selected** to link the checked ones. A line is proposed only
when exactly one claim fits it with a matching amount, and each claim is proposed at
most once at a time; anything else waits for you in Review. A reimbursement is
proposed once the claim has the payment you made, and a refund to the FSA card even
after the claim is fully reimbursed. Import and reconciliation say when proposals are
waiting.

Claims change after they are filed. When a corrected EOB arrives, change the
claim's EOB and say why in **EOB change note**: the claim keeps a history of each
change. A lower EOB after the FSA has paid leaves the claim **Over-reimbursed** by
the difference; when you pay that back, record the payment into the FSA account and
link it under **Repaid to the FSA** on the claim's FSA allocation. A repayment
counts against what the FSA reimbursed and gives that much of the year's election
back; it is never counted as payroll funding. A higher EOB reopens a reimbursed
claim for the rest. **Close claim** stops pursuing what is left (the claim shows
**Closed** and what it gave up, and needs no more attention), and **Reopen** takes
it up again; both are kept in the history. See
the [desktop](guide/desktop.md#reimbursable-expenses),
[browser](guide/web.md#reimbursable-expenses), and
[command-line](guide/cli.md#fsa-claims) guides.

### Savings goals

A savings goal is money you want set aside by a date, such as a new roof or a
holiday ([desktop](guide/desktop.md#savings-goals),
[browser](guide/web.md#savings-goals), [command line](guide/cli.md#savings-goals)).
A goal names the account that holds (or will hold) its
money, a target amount, a start date, and a target date.

A goal works like a pending bill. From its start date, each income you receive sets
aside a share of what the goal still needs: that income's share of all the income
expected between the goal's start date and its target date. So by the target date
the whole target is set aside. Twelve equal monthly paychecks toward a 1,200 goal
set aside 100 each. Income that was scheduled but never arrived sets nothing aside.
If no income is scheduled before the target date, the gap is set aside evenly by
day instead.

You can allocate extra money to a goal at any time, for example a bonus. It is set
aside in full on its date, and later income spreads only what is still missing.
Allocations cannot add up to more than the target.

What is set aside is an earmark, not the whole account. One savings account can
hold money for several goals and for other purposes, and a goal never creates or
changes transactions. Moving money into the goal's account is an ordinary transfer
you record, never an expense.

The target date is a milestone: nothing is spent then, and the whole target stays
set aside. When you have used the money (say, after buying the roof), close the goal
to release its earmark. You can reopen a closed goal. Only goals in the reporting
currency are supported, held in an asset account.

Goals are pinned: every scenario carries every open goal. A scenario can change a
goal's target amount or target date, or leave the goal out, without changing the goal
itself or any other scenario. The change applies from the goal's start date, as if
the scenario's target had always applied.

A scenario can also model what the goal is for: give a purchase date, on or after
the target date, and the expense or asset account it buys into (a roof repair
expense, say, or a car asset). On that date the scenario moves the target, as that
scenario sets it, out of the goal's account into the purchase account, and from that
month the goal sets nothing aside. The purchase shows in the scenario's Plan and
Projection as a one-off; it never posts, is never raised by inflation, and other
scenarios, including Base, are unchanged. Leaving the goal out of a scenario drops
its purchase too.

In **Projection**, each month shows what goals have set aside by its end. The
projection starts from what each goal has actually set aside, then applies the same
rule to the scenario's own projected income, month by month, so a scenario with
more income or a later target sets money aside more slowly. Each goal's target
month appears as a milestone. It says whether the projected cash then covers
everything set aside for goals in cash accounts. If cash covers bills but not goal
money, the projection names the first month that happens. A goal held in a
non-cash account, such as a brokerage account, is compared instead with that
account's projected balance in its target month: the milestone says whether the
balance covers everything goals in that account have set aside. An account left
out of projections has no projected balance, so its goals are listed without a
comparison. The projection CSV export
adds each month's goal money and the cash left after it.

In **Plan**, goals whose target date falls in the range are listed with their
target (as the scenario changes it), what they have set aside so far, and what is
left. Money moved into a goal's account is a transfer, never an expense.

### Reimbursable expenses

A receivable tracks an expense you paid out of pocket and what an insurer,
employer, or other payer is expected to send back, without ever rewriting the
expense or counting the reimbursement as new income
([desktop](guide/desktop.md#reimbursable-expenses),
[browser](guide/web.md#reimbursable-expenses),
[command line](guide/cli.md#reimbursable-expenses)).

What the payer still owes is held in a **Receivable** account. When you track an
expense, BreadSched moves the amount expected back (or the whole expense, if you
gave no expected amount) out of the expense account and into the receivable
account. So your spending shows only what you will really bear, and your net worth
includes what is owed. Liquidity, Available, and the emergency fund never count it,
because you cannot spend it yet. Money back moves it out of the receivable account
again; money back beyond what was owed stays a refund in the expense account. A
write-off returns the unpaid balance to the expense. A dispute posts nothing.
BreadSched keeps these moving transactions itself and recomputes them whenever the
receivable or a linked transaction changes, including after an import; they are
marked in their notes, and editing or deleting one as a transaction is refused
(change the receivable instead). You can choose the receivable account; by default
BreadSched uses, or creates, a "Reimbursements Receivable" account under Assets for
the expense's currency. An imported GnuCash receivable is used only when you
choose it.

When money comes back, record it as a credit to the same expense account, as an
ordinary refund, and link it to the receivable. BreadSched proposes such a credit
for one open receivable only if all of these hold:

- it is in the same account and currency as the expense;
- it is dated on or after the expense;
- it is no more than what is still owed.

If a credit could belong to more than one receivable, it is proposed only when the
payer's name appears in its description. Nothing is linked until you accept. After
an import, and when you reconcile the account a deposit landed in, BreadSched tells
you how many such credits are waiting for review.

Status is always recomputed from the linked splits plus any dispute or write-off,
never stored: open, then partially reimbursed once some money is back, disputed
while a balance remains and a dispute is recorded, written off once you record
giving up on the remainder (even after a partial reimbursement), or settled once the
linked reimbursements cover what is owed (the expected amount, or the whole
expense). Unlinking, disputes, and deleting a receivable never change your linked
transactions. If an expense you track is also on an FSA claim, the receivable shows
a warning, so the same cost is not expected back twice.

When an insurer pays part of a bill and the FSA pays the rest, link the two instead:
on the FSA claim, choose the receivable as the payer that covers part. The claim and
the receivable then show one allocation of the bill: what the payer pays (the
expected amount, or what it actually paid once that is more), what the FSA covers,
and what is left for you. The FSA share stays zero until you enter the claim's EOB
responsibility, and never exceeds what the payer leaves. If the payer and the EOB
together come to more than was paid (say the insurer paid more than expected), the
claim shows **Needs review**. Nothing is refused or rewritten; adjust the expected
amount or the EOB, or write off the difference. The link is made only on the claim,
never guessed, and changes no transactions; deleting the receivable removes it.

A GnuCash re-import that
would delete a transaction a receivable depends on is refused, the same protection
FSA claims already have.

## Import and GnuCash interoperability

BreadSched imports GnuCash SQLite and compressed XML books and preserves source
identifiers where possible. It supports accounts, transactions, commodities, dated
prices, schedules, bounded formulas, reconciliation state, and selected metadata.
Unsupported source details remain visible instead of being silently simplified.
Commodities with the same mnemonic in different namespaces remain distinct during
import and re-import. If a price refers only to an ambiguous mnemonic, inspect its
import warning and resolve the source identifier before relying on that quote.
Imports run as one atomic undo step; a cancelled import writes nothing
([desktop](guide/desktop.md#import-files), [browser](guide/web.md#import-files),
[command line](guide/cli.md#import-files)).

Re-import updates source-owned data and removes source transactions that have
disappeared. A deleted source transaction still referenced by a BreadSched
reconciliation or FSA claim is retained and reported for review, and one that is
reconciled is held for your decision (see
[Review GnuCash changes to reconciled transactions](#review-gnucash-changes-to-reconciled-transactions)).
Local planning
decisions are not overwritten by source refreshes. BreadSched does not connect to
banks itself; see [Bank downloads through AqBanking](#bank-downloads-through-aqbanking).

### Keep GnuCash and BreadSched side by side

Because import is one-way, choose one application as the ledger of record for
each period rather than recording the same activity in both.

**While GnuCash remains the ledger of record:**

- Enter, edit, and delete transactions in GnuCash, then re-import. For an imported
  transaction with no split reconciled in BreadSched, re-import restores
  GnuCash's date, description, number, accounts, amounts, memos, and reconcile
  state. A BreadSched edit to those facts does not survive; the import summary
  counts the transaction as refreshed.
- You may reconcile statements in either application. A split reconciled in
  BreadSched keeps its Reconciled state and statement date across re-import, even
  though GnuCash still shows it unreconciled.
- If GnuCash changes a transaction that has a split reconciled in BreadSched,
  re-import leaves the transaction unchanged and holds the GnuCash version for
  review (see below). Changes to its date, description, number, or currency, or
  to a reconciled split's account, amount, quantity, memo, or action, are held.
  Changes confined to its other splits apply normally.
- Use BreadSched for planning work: account types and Dashboard groups, Plan
  matches and rejections, BreadSched notes, split planning/FSA/investment
  classifications, FSA claims, estimates, scenarios, and schedule planning
  timelines. These survive re-import.
- Do not post schedules or add transactions in BreadSched for activity you also
  record in GnuCash, unless you write them back (next section). BreadSched-created
  transactions are otherwise never written to GnuCash and are never removed by
  re-import, so recording the same activity in both produces duplicates.

**To make BreadSched the ledger of record:** re-import once from the final
GnuCash book, verify and back up the BreadSched book, and from then on record
activity only in BreadSched. Keep the GnuCash file as a read-only archive;
importing a later GnuCash copy would restore GnuCash's values over any imported
unreconciled transactions you have since edited.

### Write changes back to a GnuCash book

BreadSched can write your changes back to the GnuCash book you imported, whether it
is saved as SQLite or as XML (GnuCash's default, compressed or not)
([desktop](guide/desktop.md#write-changes-back-to-gnucash),
[browser](guide/web.md#write-changes-back-to-gnucash),
[command line](guide/cli.md#write-changes-back-to-gnucash)). Close the book in
GnuCash first. The preview lists, for each transaction, exactly what would be
written:

- transactions you entered in BreadSched, with any number of splits, when every
  account exists in GnuCash and the transaction is in a currency the book has (a
  split in a security or foreign-currency account keeps its own quantity);
- edits to transactions that came from GnuCash: date, description, number, split
  memos and actions, amounts, accounts, and added or removed splits;
- transactions you deleted in BreadSched, which are deleted in GnuCash too, with
  their notes and other details; and
- reconcile marks you set in BreadSched.

A transaction GnuCash has reconciled only takes reconcile marks: other edits to it,
and deleting it, are listed as not written, as is a transaction in a GnuCash lot
(investment lots). Anything else that cannot be written is listed too, with the
reason. Nothing is written until you choose which transactions to write.

BreadSched refuses if GnuCash has the book open or if the book changed since you
last imported it; import it again first. Before writing it copies the book to a
backup folder next to your BreadSched book (keeping 10 backups unless you choose
another number) and writes everything in one step. An XML book is rewritten only
where a chosen transaction changes; the rest of the file stays exactly as GnuCash
wrote it. BreadSched then reads the book back to confirm GnuCash will see exactly
what you have, and restores the copy if anything fails. Changes you did not choose
stay as they are in BreadSched and are offered again next time. Each release is
tested by opening written books in GnuCash itself.

### Review GnuCash changes to reconciled transactions

When re-import holds GnuCash changes, the import summary reports how many, and each
interface offers the review ([desktop](guide/desktop.md#review-held-gnucash-changes),
[browser](guide/web.md#review-held-gnucash-changes),
[command line](guide/cli.md#review-held-gnucash-changes)). Each row lists the
transaction and what GnuCash changed:

- **Keep BreadSched version** leaves the transaction as reconciled. That GnuCash
  version is not raised again; a later, different GnuCash change is.
- **Use GnuCash version** applies the change, keeping BreadSched notes, Plan
  links, and classifications. A split in a completed BreadSched statement stays
  Reconciled when its account and amount are unchanged. If GnuCash would change
  that split's account or amount, or remove it, the row names the statement
  instead of offering this choice: reopen that statement first.
- **Decide later**, the default, asks again next time.

A transaction deleted in GnuCash is held the same way when any of its splits is
reconciled, whether you reconciled it in GnuCash or in BreadSched. Its row says
**Deleted in GnuCash**, and the choices read **Keep the transaction** (not asked
again while GnuCash still lacks it), **Delete it here too**, and **Decide later**.
Deleting is refused while a BreadSched reconciliation, FSA claim, or receivable
refers to the transaction; the row names what still uses it. An unreconciled
transaction deleted in GnuCash is still removed on re-import, as before.

Applying commits all rows as one undo step; nothing is written if any row is
refused.

### QIF and OFX statements

QIF and OFX/QFX imports infer decimal and date conventions from whole-file evidence;
when a format is ambiguous, choose an explicit override. Import problems are
reported per record when safe recovery is possible. Re-importing a QIF or OFX/QFX
statement refreshes only the statement account's side of each transaction. A
category you chose or split after the first import is kept. If the bank corrects an
amount, a single category follows the new amount. A transaction you split across
several categories is left unchanged, with a warning to review it.

An OFX/QFX file from a brokerage imports its transactions too. BreadSched creates
an account under **Assets** named for the brokerage and account number, with a
**Cash** account and one account per security you traded (using a security already
in your book when the ticker matches). Purchases and sales move units and cash,
with commissions and fees in **Expenses:Investment Fees**; reinvested dividends add
units from **Income:Investment Income**; dividends, interest, and cash deposits or
withdrawals post to cash. Gains are not calculated: a sale is recorded at its
proceeds.

Some brokerage activity is not imported, and is not planned for now: option
purchases, sales, exercises, assignments, and expiries; transfers of shares in or
out; stock splits; return of capital; margin interest; and cash or share moves
between a brokerage account's sub-accounts. Each such record is listed in the import
summary under **Skipped records by reason** with its date and the brokerage's
description (for example *"A stock split is not imported; enter it yourself"*),
and a later import of the same file reports it as repeated rather than new. Enter
these yourself in the brokerage's accounts.

A QIF export with investment accounts (Quicken types **Invst** or **Port**) is
imported the same way: each becomes an account under **Assets** with **Cash** and
one account per security, using the export's security list for tickers. Buys,
sells, reinvested and cash dividends, interest, capital-gain distributions,
miscellaneous income and expenses, and cash transfers are imported; a **BuyX**,
**DivX**, or other "X" action moves the money through the named account (for
example **[Checking]**). Transfers to an investment account from a bank register go
to its **Cash** account. Share transfers (**ShrsIn**/**ShrsOut**), stock splits
(**StkSplit**), employee stock option grants, vesting, exercises, and expiries,
option trades, and reminders are not imported; each is listed as skipped in the
same way, for you to enter yourself.

When a QIF export contains several accounts, each transfer between them appears in
both registers. BreadSched imports it once: the copy with the same date and
opposite amount in the other account is matched and not imported again (an
investment account's record is the one kept), and the import summary says how
many copies were matched. A transfer whose other side is not in the file is
imported as usual.

### Import a CSV statement

Most banks and card issuers can export a statement as CSV, but every layout
differs, so BreadSched asks which columns hold which facts rather than guessing
([desktop](guide/desktop.md#import-a-csv-statement),
[browser](guide/web.md#import-a-csv-statement),
[command line](guide/cli.md#import-a-csv-statement)). Choose the account the
statement belongs to, then map the date column and either one signed amount column
(negative for money out, or for a card charge) or separate debit and credit
columns. A description and memo column are optional.

Always preview first. The preview lists every row with its status:

- **new** rows will be imported;
- **imported** rows are already in the book from an earlier import of the same
  rows and are left exactly as they are, including any category you chose since;
- **possible duplicate** rows match a transaction already in that account on the
  same date and amount, for example one you typed in or imported from OFX. They
  are held back unless you include them;
- **possible transfer** rows look like the other side of a transfer already
  imported from another account's statement: the opposite amount, within three
  days, still posted against **Uncategorized CSV** or **Uncategorized OFX**. The
  reason names that account and date. Linking possible transfers turns each pair
  into one transfer between the two accounts; otherwise the row is imported as new
  and the other transaction is left as it is. A transaction you have categorized is
  never offered;
- **invalid** rows give the line number and the reason, such as a date or amount
  that cannot be read, and are skipped.

The file's encoding (UTF-8 or Windows-1252), delimiter, date order, and decimal
convention are detected from the whole file and shown in the preview. If every date
could be read either day-first or month-first, the preview stops and asks you to
choose. You can override detection, and invert the sign for exports that show money
out as a positive number. The whole import is one undo step. New rows are posted
against **Uncategorized CSV** under Expenses or Income for you to categorize. Two
identical rows on the same day remain two transactions.

Three more columns are optional. A **category** column posts each row to an account
you already have, named by its full name such as `Expenses:Groceries`, or by its own
name when no other account shares it; a row whose category names no account, or
several, is invalid and says so, and an empty cell uses **Uncategorized CSV**. A row
with a category is not offered as a possible transfer. A **payee** column sets the
payee when it matches one you already have, by name or by the same description
matching payees use; an unknown payee is noted in the preview and the row imports
without one. A **currency** column must match the account's currency; a row in
another currency is invalid rather than imported at the wrong value. BreadSched
never creates accounts, payees, or currencies from these columns. Re-importing a
row already imported leaves it untouched even if its category cell has changed.

A statement that splits one charge across several categories, such as a warehouse
store receipt or a payroll deposit, can be mapped with **split columns** instead of
one category column: a category column and an amount column for each split. Each
filled pair becomes one split of the transaction, its amount written in the same
sign as the row's amount (an export that shows money out as positive is inverted,
splits included). The filled splits must add up exactly to the row's amount; if
they do not, or a pair has a category without an amount (or the reverse), or a
category names no account, the row is invalid and the preview says why. BreadSched
never adds a balancing split. A row with no filled pair uses **Uncategorized CSV**,
and a split row is not offered as a possible transfer.

### Bank downloads through AqBanking

BreadSched does not connect to banks directly, and does not bundle AqBanking (the
online-banking library GnuCash uses). AqBanking mainly serves German and other
European banks through FinTS/HBCI and EBICS; its United States route, OFX Direct
Connect, is being withdrawn by major banks. If your bank works with AqBanking, set
it up with AqBanking's own tools, which keep your bank login, PIN, and TAN
handling, then download a statement and export it as CSV:

```bash
aqbanking-cli request --transactions -c statement.ctx --fromdate=20260901
aqbanking-cli export -c statement.ctx --exporter=csv --profile=default -o statement.csv
```

Import `statement.csv` as a [CSV statement](#import-a-csv-statement) into the
matching account, choosing the **date** column for the date, **value_value** for
the amount, **remoteName** for the description, and **purpose** for the memo. The
preview and duplicate review work as for any CSV statement, so downloading
overlapping days again adds nothing twice
([command line](guide/cli.md#import-a-csv-statement)).

### Payees

A payee records who a transaction was with, separately from its description, so
"CORNER GROCER #1234" and "Corner Grocer 0987" can both belong to **Corner Grocer**
while each keeps the text its statement printed
([desktop](guide/desktop.md#payees), [browser](guide/web.md#payees),
[command line](guide/cli.md#payees)).

A payee has a name and one or more example descriptions. BreadSched ignores case,
punctuation, and any word containing a digit, so "CORNER GROCER #1234" matches every
description that reduces to "corner grocer"; proposals show that matched key.
Matching is exact after that, never a guess, and one description key can belong to
only one payee. Proposals list transactions that have no payee yet and write
nothing. Accepting assigns the payee in one undo step, and a transaction that
already has a payee is never changed. Deleting a payee clears it from its
transactions. Re-importing from GnuCash, OFX, QIF, or CSV keeps the payees you
assigned. The register shows each transaction's payee, and you can set or clear it
in the transaction editor or the register row.

### Categorization rules

Imported transactions start in **Uncategorized CSV** or **Uncategorized OFX**. A
categorization rule proposes a category for them, matched by payee or by
description (with the same matching as payees: case, punctuation, and words
containing digits are ignored) ([desktop](guide/desktop.md#categorization-rules),
[browser](guide/web.md#categorization-rules),
[command line](guide/cli.md#categorization-rules)). Rules are ordered: the first rule
that matches decides, and proposals name any later rule that would have chosen
differently so you can reorder them. Only transactions still on those placeholders
are ever proposed, so a category you chose yourself is never replaced, and a
transaction split across several placeholder lines is left for you. Nothing changes
until you accept, and accepting is one undo step.

## Print, export, and inspect

Dashboard, Plan, and Projection print what the view shows, with the controls and
calculation already applied. The desktop prints through the system print dialog,
for a printer, a preview, or a PDF file, and can also open the same report in your
web browser; the browser interface prints its page
([desktop](guide/desktop.md#print-and-export), [browser](guide/web.md#print)).
Plan printing leads with the cash outlook and omits the private book path.
Optional category detail begins on a new page. Transactions
can be exported, and Projection exposes its dated state and explanations in its
supported outputs.

## Protect and recover a book

Native books use the `.breadsched` suffix and SQLite storage. The application
version and the separate native schema version appear in the version output and
Verify diagnostics. Regularly:

- **back up** the book: an independent verified backup (linked documents live in
  the attachment folder beside the book, which a book backup does not include);
- **verify** it: SQLite integrity and financial relationships such as balanced
  transactions, valid references, precision, scheduled realization, and
  reconciliation consistency;
- when needed, **restore** a backup as a new book: the backup is verified and
  written to a different path, never over the open live book.

After you upgrade to a version with a newer native schema, a book from an earlier
alpha is migrated the first time it is opened for writing: the desktop and browser
do this when they open it, and on the command line `breadsched migrate` does it. A
verified backup of the old book is written next to it first (for a book the
Flatpak reaches only through the file chooser, in BreadSched's data folder; see
[Install on Linux](guide/desktop.md#install-on-linux)), and the desktop
application says where. Read-only commands never migrate; they ask you to migrate
instead.

See [desktop](guide/desktop.md#protect-and-recover-a-book) and
[command line](guide/cli.md#protect-and-recover-a-book).

A writable book has a sidecar lock. A second process may open it read-only but
cannot become a competing writer. A stale same-host lock is reclaimed when its
recorded process no longer exists. A read-only open, each browser page, and each
desktop projection works from a copy of the book taken when it starts, so what it
shows belongs to one saved state even if another window saves meanwhile; the next
read shows the save.

Do not keep the only copy of a book in a synchronization location that is unsafe for
SQLite. BreadSched warns about recognized OneDrive, iCloud Drive, Dropbox, and Google
Drive roots. Keep independent backups on separate storage.

## Troubleshooting

- If a book opens read-only, check whether another BreadSched process has it open and
  inspect the lock information before assuming the lock is stale.
- If imported values or dates are ambiguous, rerun the import with an explicit
  decimal or date-order choice instead of editing many misread transactions.
- If Plan and actuals disagree, open value detail and the actual-resolution queue.
  Check dates, account types, explicit split purposes, and occurrence matches.
- If Projection warns that a period does not reconcile, inspect its opening state,
  dated events, accruals, and closing state. BreadSched refuses to hide that error.
- Before reporting a problem, run Verify and record the BreadSched version. Never
  attach an unsanitized financial book to a public issue.

Each interface part ends with its own troubleshooting notes.
