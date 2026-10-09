# Browser guide

This part covers the loopback web interface. What each feature does, and the rules
behind it, is in the [overview](../USER_GUIDE.md). The same work can be done in the
[Desktop guide](desktop.md), and most of it from the [Command-line guide](cli.md).

## Start the web interface

```bash
breadsched web household.breadsched
```

Then open the address it prints in your browser. The server listens only on this
computer and serves only its packaged page, script, and stylesheet. Do not expose
the development web server on an untrusted network. The web interface does not need
GTK. To create a book first, use the [command line](cli.md#create-and-inspect-books).

## Find your way around

The navigation bar offers the work areas (**Dashboard**, **FSA Dashboard**,
**Accounts**, **Register**, **Scheduled**, **Plan**, **Review**, **Projection**) and
**Enter**, **Import**, **Rules**, **Reimbursables**, **Goals**, **Verify**, and **Guide**. **Guide** shows this guide: choose the overview or the desktop, browser,
or command-line part, and a link to another part opens it there. **Help** on
**Scheduled**, **Payroll**, **Plan**, **Projection**, **Import**, **Rules**, **Reimbursables**, and **Goals**, and in the CSV statement, write-back,
reconciliation, due review, held GnuCash changes, Expense Explorer, and net worth
history sections, opens this guide at that section in a new browser tab, so the
form you were filling in stays as it was.

**Open in new tab** in Register and Projection opens that account's register, or
the scenario's projection, in another browser tab. Each browser tab keeps its own
account, scenario, and place, so you can look at two scenarios side by side while
the first tab carries on; this is the browser's counterpart of the desktop's
scenario tabs. Reloading a tab needs the address the server printed at start-up,
because the page removes its access token from the address bar.

On the Dashboard, a requested liquidity or emergency-fund horizon changes the
displayed calculation for that request. Reopening the view uses the saved Dashboard
settings; use the Dashboard settings controls to save a new horizon. Expand a
missed-schedule row to see each missed date and amount. When savings goals have set
money aside, a **Set aside for goals** tile shows the total (see
[Savings goals](../USER_GUIDE.md#savings-goals)).

### Net worth history

**Net worth history** is the last section of the Dashboard page. **Group by**
switches between months, quarters, and years, and expanding a period lists its
top-level account values. Choose a period's **Change** (or **Explain**) to list the
postings behind it; **Download CSV** saves them with their totals, and printing the
page includes them. See [Net worth history](../USER_GUIDE.md#net-worth-history).

## Exchange rates

In **Accounts**, choose **Exchange rate…**, select the source and target currencies,
enter an as-of date and the target units per source unit, then save. The account
display refreshes its quote date and source or missing-quote warning. The control
accepts currencies already in the book; it does not create a new currency or alter
ledger transactions. The account data also include the signed number of days since
each quote (`quote_age_days`), with negative days identifying a future-dated quote.

**Holdings and cost basis…** in **Accounts** lists each holding's shares, cost,
market value, and unrealized gain; open a holding for its lots, sales, transfers,
share splits, and notes
(`/api/holdings`, share counts as exact decimal text). An Investment or Retirement
account's settings choose **Cost of shares sold** (first in, first out, or average
cost; `/api/account/cost-basis`). See
[Holdings and cost basis](../USER_GUIDE.md#holdings-and-cost-basis).

## Choose the lots a sale sells

In **Holdings and cost basis…**, open a holding and choose **Choose lots…** beside a
sale. Enter the shares to sell from each lot held before the sale and choose
**Save**; unassigned shares sell by the account's method, and **Use the account's
method** clears the choice (`/api/holdings/sale-lots`). **Realized gains…** lists
every sale with the lots it took and totals by year, filtered by year, and **Print**
prints just the report (`/api/realized-gains`). See
[Specific lots and realized gains](../USER_GUIDE.md#specific-lots-and-realized-gains).

## Tax year

In **Accounts**, choose **Tax year…** and pick a **Year**: realized gains short- and
long-term, the tax-relevant accounts and tags, and income by source
(`/api/tax-year`). **Tax-relevant accounts and tags…** checks the accounts and tags
the year totals (`/api/tax-marks`), and **Print** prints just the report. See
[Tax year](../USER_GUIDE.md#tax-year).

## Budget jars

In **Plan**, choose **Budget jars…** below the controls: all jars by period, then
each account with its charts (planned against actual, and jar levels against their
targets, each with its table; hover over a column for its amount), with **Each jar**
to open them one by one (`/api/budget-jars`).
Change **Group by** in the dialog; **Print** prints just the report. See
[Budget jars](../USER_GUIDE.md#budget-jars).

## Online quotes

In **Accounts**, choose **Online quotes…**. Type each security's or currency's
**Quote source** (`tsp`, `alphavantage`, `currency`, or a Finance::Quote method),
enter your Alpha Vantage API key if you use it, and choose **Get quotes**. The
dialog lists what was stored and what failed and why. The key is saved in your
settings and never shown again (`/api/quotes`, `/api/quotes/source`,
`/api/quotes/key`, `/api/quotes/update`). See
[Online quotes](../USER_GUIDE.md#online-quotes).

## Enter transactions in the register

The register has a blank row at the bottom for a new entry, **Split** for split
lines with an imbalance line, and in-place editing of any row: click it (or choose
**Edit**), then **Enter** saves and **Escape** cancels. **Up** and **Down** in a
field save the row you are leaving if you changed it and edit the one above or
below; below the last transaction is the blank row, and a row that cannot be saved
keeps you there with the reason. While you type an account path into an account
choice, the matching accounts are listed under it; click one to choose it. The
**R** column shows **n**, **c**, or **y**; click **n** or **c** to mark an entry
cleared or not cleared (see
[Reconcile a statement](../USER_GUIDE.md#reconcile-a-statement)). Leaving the description fills in the
proposal from an earlier matching transaction (see
[Transactions and registers](../USER_GUIDE.md#transactions-and-registers)). Choosing
another account, or editing another row, while something is typed asks before
discarding it. The date, amounts, account choices, and **Num** take the same
[typing shortcuts](../USER_GUIDE.md#typing-shortcuts-in-the-register) as the
desktop register; the date is a text field, so type it rather than picking it
from a calendar.

### Tags and linked documents

A register row shows its tags and how many documents it links, in red when one is
missing. Choose **Tags & documents…** on a row to change them. Type tags separated
by commas and choose **Save tags**. **Attach file** copies the chosen file into the
book's attachment folder and links it; **Link** links a web address or a file that
is already in the attachment folder. For safety, the browser cannot link a file
anywhere else; use **Attach file**, or the desktop or command line, for those.
**Open** shows a PDF, image, or text file in a new tab and saves any other kind,
**Relink…** asks where in the attachment folder a moved or missing file is now, and
**Remove**
unlinks a document without deleting the file. A document linked in GnuCash can only
be opened. See [Tags and linked documents](../USER_GUIDE.md#tags-and-linked-documents).

## Reconcile a statement

Choose **Reconcile…** in the register, enter the statement date and ending balance,
check entries until the difference is zero, and finish. If a deposit in that account
looks like money back on a [reimbursable expense](#reimbursable-expenses), the page
says how many such credits are waiting.

## Scheduled activity

In **Scheduled**, enter a name, category, funding account, amount, and recurrence to
create a fixed schedule. Editing one can also set future amounts, one-time
exceptions, additional splits, and optional planning classifications. Review the
result before relying on a projection. Scenario-only estimates and scenario changes
to baseline schedules are made in the same view.

### Paychecks and pay changes

**Payroll** lists each schedule that reads as a paycheck as of a date, with gross,
withheld, net, and take-home share; choose one to see its lines. Below it, **Pay
change** takes a date and new gross, and for each line **Keep**, **Scale with gross**,
or **Set to** an amount: **Preview** shows the result without saving and **Save pay
change** saves it. **Payroll templates** lists, edits, and deletes templates, fills
one from the selected paycheck, and creates a new paycheck from one. See
[Paychecks and pay changes](../USER_GUIDE.md#paychecks-and-pay-changes).

### Review due transactions

**Review due transactions…** is in Scheduled and, when something is due, on
Dashboard. Choose each date's action, or one for all of a schedule's dates, then
**Apply**. See
[Review due and missed transactions](../USER_GUIDE.md#review-due-and-missed-transactions).

## Review

**Review** lists actual transactions waiting for a decision. Each planned item it
offers shows **Close match** or **Possible match** and the reasons beneath it; the
buttons explain themselves on hover, and **What each action does** lists them all.
When nothing is offered, the page says why.

## Plan and projection

Set the Plan controls and apply them; they are shared with the desktop application.
The Plan response carries a `currency` object with the conversion evidence shown in
the foreign-currency note. The summary cards give planned change, actual, and
variance through the as-of date; **Show** chooses Plan, Period actual, or Period
variance for the table (see [Reporting terms](../USER_GUIDE.md#reporting-terms)).
Value details follow the chosen scenario. A card, period, or cell that leaves out an
amount without a quote carries a **Partial** label; expand the label under the
cards to see each excluded amount and the rate or price to add. Projection, the
Dashboard, net worth history, and Expense Explorer use the same labels, and the
Projection chart shades partial months (see [Complete, partial, and unavailable values](../USER_GUIDE.md#complete-partial-and-unavailable-values)). Each resource carries a
`completeness` object for the value it describes.

**Manage scenarios…** in Plan opens the scenario list, which shows each effective assumption and its source, along with
the saved dated periods and eligible account-specific rate choices. In
**Projection**, open a month to inspect its cash movement, holdings and liabilities,
dated events, and assumption sources; **How this month reconciles** shows its Cash,
Investments, Debts, and Net worth bridges, and **How the projection reconciles**
under the chart shows them across the whole projection (see
[How a projection reconciles](../USER_GUIDE.md#how-a-projection-reconciles)). **Open in new tab** keeps the chosen
scenario's projection in another browser tab. **Cash runway** under the chart gives
the [cash runway](../USER_GUIDE.md#cash-runway), and a comparison says which
scenario's cash lasts longer.

Under **Retirement drawdowns** on the same page, **Add drawdown…** sets up a saved
scenario's [retirement drawdown](../USER_GUIDE.md#retirement-drawdown): the account to
withdraw from, the cash account to pay into, the start date, an optional end date, and
either a fixed yearly amount (optionally rising with expense inflation) or a yearly
share of the balance in percent. **Edit** and **Remove** change or delete a drawdown;
a refused entry explains why and changes nothing.

### Explore expenses

Apply the Plan controls first, then scroll to **Expense Explorer** on the Plan page,
below the cash outlook. Choose a period in any **Spending over time** or **Income
over time** chart (click it, or Tab to it and press Enter) or in its table to make
it the comparison period (the **Income detail** list below the income charts shows
that period's dated income), use **Sort by** to order the category rows, and choose
a **Category trend**. Each chart's exact values are under its **Chart values**
toggle. The page also shows the selected category's Plan, Period actual, and Period variance beneath the
merchant table. **Carry prior periods** turns rollover on. See
[Explore expenses](../USER_GUIDE.md#explore-expenses).

## Savings goals

**Goals** lists each goal's account, target date, target, what is set aside, what
remains, and its status. Fill in the form below the list and choose **Add goal**;
**Edit** loads a goal into the form for **Save changes**. Enter an amount beside a
goal and choose **Allocate** to set extra money aside today. **Close** releases a
goal's money, **Reopen** undoes that, and **Delete** removes the goal. **Show closed
goals** includes closed goals. The Dashboard lists each open goal under **Savings
goals**; choose a goal's name to open **Goals**. After **Edit**, the form below the
goal form changes that goal in one scenario: choose the scenario, enter a different
target amount or target date or check **Leave out**, and choose **Apply to
scenario** (with nothing entered, the scenario follows the goal unchanged). **Buy
on** and **Buy into** model the purchase in that scenario.
**Scenario changes** lists every change. **Projection** shows a **Savings goals**
section, and **Plan** lists goals reaching their target in its range. See
[Savings goals](../USER_GUIDE.md#savings-goals).

## Reimbursable expenses

Choose **Reimbursable…** on a register row to start a receivable from that expense,
or open **Reimbursables** to list, open, link, dispute, and write off receivables.
**Held in** chooses the receivable account (the default is one per currency).
**Proposed reimbursements** lists credits that clearly belong to one open
receivable, checked; choose **Accept selected** to link them. A warning appears when
a linked expense is also on an FSA claim, and the open receivable shows what is
owed and the account holding it.

**Plan** lists each expense category a receivable changed under **Reimbursable
expenses: gross and net cost**, and a Plan cell's detail shows its gross cost and
what was reimbursed or expected back below the Plan, Actual, and Variance cards.

**FSA Dashboard** first lists **Proposed claim links**, FSA statement lines that
clearly belong on one claim, each checked; **Link selected** links them. Import and
reconciliation say when proposals are waiting.

**FSA Dashboard** lists open claims with a **Needs attention** column, under a line
counting the claims that need it; the Dashboard shows the count as a tile.
**Claims report** totals every claim, grouped by the **Group by** choice: status,
FSA account, funding year, or provider.

The FSA funding-year editor in Accounts takes each year's carryover limit and
grace-period end, either or both, and **Dependent care FSA** marks a dependent care
plan, and the FSA Dashboard shows what each year carried in and carried
over, and under **How used** what was paid from the FSA card, reimbursed to your
bank, and refunded to the card.

When entering a transaction or resolving it in Review, the **FSA claim** role list
includes **Paid from the FSA card** and **Refunded to the FSA card**, which record
both sides of a card payment or refund on the claim at once.

In the FSA claim editor, a changed EOB is kept in the claim's history with the
**EOB change note**, and each allocation links money paid back into the FSA under
**Repaid to the FSA**. Each claim in the list has **Close claim** or **Reopen**,
with the reason typed beside it, and lists its history.

When the FSA pays what the insurer does not, open the FSA claim editor, choose the
receivable under **Payer covers part**, and save the claim. The claim list and the
receivable then show what the payer, the FSA, and you each pay, and **Needs
review** when that comes to more than was paid. See
[Reimbursable expenses](../USER_GUIDE.md#reimbursable-expenses).

To plan for the payer paying less, nothing, or later in one saved scenario, open the
receivable and use the **In scenario** row: choose the scenario, enter the amount
expected back (0 for nothing) and the date, and choose **Apply to scenario**. Leave
both empty to expect what the receivable says again. The line below lists every
scenario's change, and that scenario's Projection shows the gross and net cost (see
[Reimbursable expenses](../USER_GUIDE.md#reimbursable-expenses)).

## Import files

In **Import**, select a QIF, OFX/QFX, or GnuCash file from the browser (up to
32 MiB), or enter a path visible to the BreadSched server. Uploaded files are
retained beside the native book in a `<book>.uploads` directory; keep that directory
with the book if you want to repeat an import from its remembered path. Uploading
the same filename again refreshes the same source identity; different filenames are
separate sources. Import options and warning details are shared with the path-based
workflow. **Include possible duplicates** also imports QIF and OFX rows otherwise held back
as possible duplicates.

### Import a CSV statement

In **Import**, use the **CSV statement** section. Choose the file from the browser
(it is kept beside the book in `<book>.uploads`) or enter a path, then choose **Read
columns**. BreadSched shows the detected encoding and delimiter with the first rows
and suggests columns from their headers; check each suggestion. Choose the account
and any date order, decimal, or sign options, then **Preview** to see every row's
status. **Link possible transfers** joins each possible-transfer pair. **Add split
columns** adds a category and an amount column for one split of each row; add one
pair per split and leave **Category column** at (none). The preview's Category
column lists each row's splits. **Import**
writes the previewed rows as one undo step, and the preview refreshes to show them
as already imported. See
[Import a CSV statement](../USER_GUIDE.md#import-a-csv-statement).

### Review held GnuCash changes

The review is offered after an import, when the page loads, and from **Review held
GnuCash changes…** on the Import page. Choose each row's decision, then **Apply**.
A GnuCash deletion of a reconciled transaction is listed too, with **Keep the
transaction** or **Delete it here too**.

### Write changes back to GnuCash

Close the book in GnuCash, then open **Import** and use **Write changes to
GnuCash**. Tick the transactions to write and choose **Write selected**. **Backups
to keep** sets how many backups are kept. See
[Write changes back to a GnuCash book](../USER_GUIDE.md#write-changes-back-to-a-gnucash-book).

## Categorization rules

Open **Rules**. Enter an example description under **Matching description**,
choose the category, and choose **Add rule**. **Up** and **Down** change a rule's
priority and **Delete** removes it. The **Proposals** list shows each
transaction's proposed category, the deciding rule, and any later rule that would
have chosen differently; every proposal starts checked. Clear any you do not want
and choose **Accept selected**.

## Print

**Print** prints the current view with navigation and actions removed. On Plan it
includes the applied Expense Explorer with its selected comparison and detail.

## Troubleshooting

- If the web interface reports an internal error, include its correlation ID in
  the report; the server returns that ID even when diagnostic output is unavailable.
- **Verify** checks the open book, like `breadsched verify`, and lists any problems
  found; **Verify again** reruns the check.
