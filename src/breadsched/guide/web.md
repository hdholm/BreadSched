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
**Enter**, **Import**, **Payees**, **Rules**, **Reimbursables**, **Goals**, **Verify**, and
**Guide**. **Guide** shows this guide: choose the overview or the desktop, browser,
or command-line part, and a link to another part opens it there.

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

## Enter transactions in the register

The register has a blank row at the bottom for a new entry, **Split** for split
lines with an imbalance line, and **Edit** on any row to change it in place, with
**Enter** to save and **Escape** to cancel. Leaving the description fills in the
proposal from an earlier matching transaction (see
[Transactions and registers](../USER_GUIDE.md#transactions-and-registers)). Choosing
another account, or editing another row, while something is typed asks before
discarding it. Choose a payee in the row's **Payee** list.

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

### Review due transactions

**Review due transactions…** is in Scheduled and, when something is due, on
Dashboard. Choose each date's action, or one for all of a schedule's dates, then
**Apply**. See
[Review due and missed transactions](../USER_GUIDE.md#review-due-and-missed-transactions).

## Plan and projection

Set the Plan controls and apply them; they are shared with the desktop application.
The Plan response carries a `currency` object with the conversion evidence shown in
the foreign-currency note. Value details follow the chosen scenario.

**Manage scenarios…** in Plan opens the scenario list, which shows each effective assumption and its source, along with
the saved dated periods and eligible account-specific rate choices. In
**Projection**, open a month to inspect its cash movement, holdings and liabilities,
dated events, and assumption sources.

### Explore expenses

Apply the Plan controls first, then scroll to **Expense Explorer** on the Plan page,
below the cash outlook. Choose a period in the **Spending over time** or **Income
over time** chart or table to make it the comparison period (the **Income detail**
list below the income chart shows that period's dated income), use **Sort by** to order the category rows, and
choose a **Category trend**. The chart labels plan in blue and actual in orange. The
page also shows the selected category's Plan, Actual, and Variance beneath the
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
scenario** (with nothing entered, the scenario follows the goal unchanged).
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

When the FSA pays what the insurer does not, open the FSA claim editor, choose the
receivable under **Payer covers part**, and save the claim. The claim list and the
receivable then show what the payer, the FSA, and you each pay, and **Needs
review** when that comes to more than was paid. See
[Reimbursable expenses](../USER_GUIDE.md#reimbursable-expenses).

## Import files

In **Import**, select a QIF, OFX/QFX, or GnuCash file from the browser (up to
32 MiB), or enter a path visible to the BreadSched server. Uploaded files are
retained beside the native book in a `<book>.uploads` directory; keep that directory
with the book if you want to repeat an import from its remembered path. Uploading
the same filename again refreshes the same source identity; different filenames are
separate sources. Import options and warning details are shared with the path-based
workflow.

### Import a CSV statement

In **Import**, use the **CSV statement** section. Choose the file from the browser
(it is kept beside the book in `<book>.uploads`) or enter a path, then choose **Read
columns**. BreadSched shows the detected encoding and delimiter with the first rows
and suggests columns from their headers; check each suggestion. Choose the account
and any date order, decimal, or sign options, then **Preview** to see every row's
status. **Link possible transfers** joins each possible-transfer pair. **Import**
writes the previewed rows as one undo step, and the preview refreshes to show them
as already imported. See
[Import a CSV statement](../USER_GUIDE.md#import-a-csv-statement).

### Review held GnuCash changes

The review is offered after an import, when the page loads, and from **Review held
GnuCash changes…** on the Import page. Choose each row's decision, then **Apply**.

### Write changes back to GnuCash

Close the book in GnuCash, then open **Import** and use **Write changes to
GnuCash**. Tick the transactions to write and choose **Write selected**. **Backups
to keep** sets how many backups are kept. See
[Write changes back to a GnuCash SQLite book](../USER_GUIDE.md#write-changes-back-to-a-gnucash-sqlite-book).

## Payees

Open **Payees**. Enter a name and one or more example descriptions (one per line)
and choose **Add payee**. The **Proposals** list shows each transaction without a
payee whose description matches, with the matched key; every proposal starts
checked. Clear any you do not want and choose **Accept selected**. **Edit** loads a
payee into the form; **Delete** removes it and clears it from its transactions.

## Categorization rules

Open **Rules**. Choose whether a rule matches a description (enter an example) or a
payee, choose the category, and choose **Add rule**. **Up** and **Down** change a
rule's priority and **Delete** removes it. The **Proposals** list shows each
transaction's proposed category, the deciding rule, and any later rule that would
have chosen differently; every proposal starts checked. Clear any you do not want
and choose **Accept selected**.

## Print

**Print** prints the current view with navigation and actions removed. On Plan it
includes the applied Expense Explorer with its selected comparison and detail.

## Troubleshooting

- If the web interface reports an internal error, include its correlation ID in
  the report; the server returns that ID even when diagnostic output is unavailable.
- **Verify** checks the open book, like `breadsched verify`.
