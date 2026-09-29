# Desktop guide

This part covers the GTK desktop application. What each feature does, and the rules
behind it, is in the [overview](../USER_GUIDE.md). The same work can be done in the
[Browser guide](web.md), and most of it from the [Command-line guide](cli.md).

## Start the desktop application

Open a native BreadSched book:

```bash
breadsched-gtk household.breadsched
```

With no book argument, the application offers to create a book, open a book, or
import a GnuCash book into a new BreadSched book. The equivalent commands are
`breadsched gui` and `python -m breadsched.gui`. **File → New Book** creates a book
at any time, and **File → Import GnuCash Book into New Book** starts from GnuCash.

The desktop application needs a GTK 4 runtime and PyGObject supplied by your
operating system. If they are missing, `breadsched-gtk --help` names the packages to
install; the [command line](cli.md) and the [browser interface](web.md) keep working
without GTK.

Press `F1` or choose **Help → User Guide** to open this guide. The switcher at the
top shows the overview or the desktop, browser, or command-line part, and a link to
another part opens it.

### Install on Windows

The Windows installer (`BreadSched-<version>-setup.exe`) needs no administrator
rights and nothing else installed: it carries its own Python and GTK. It installs
for the current user under `%LOCALAPPDATA%\Programs\BreadSched` and adds
**BreadSched** to the Start menu. The installation folder also holds
`breadsched.cmd` for the [command line](cli.md) and `breadsched-gtk.cmd`.
To type `breadsched` in any new Command Prompt or PowerShell window, tick **Add
the breadsched command to PATH** on the installer's Components page (off by
default). This adds only the installation folder to your own `PATH`; later
installs keep your choice unless you change it, and uninstalling removes it. For
an unattended install, run the installer with `/S /ADDTOPATH`.
Installing a newer version over an older one replaces the program and keeps your
books, and uninstalling (from Windows Settings or the Start menu) never removes a
book. Download it from the release page and check it against the release's
`SHA256SUMS` (in PowerShell, `Get-FileHash BreadSched-<version>-setup.exe`). It is
not yet code-signed, so Windows SmartScreen may ask you to confirm before it runs.

## Find your way around

The toolbar starts with the commands that work anywhere (open, import, undo, redo,
new transaction, and print). Next come the name of the view you are in and that
view's own command icons, then one icon for each other work area; the view you are
in has no icon, since you are already there. After those icons, two short lines
count the book's accounts and transactions. The **View** menu lists every area.
The **Actions** menu follows the view too: the current view's commands come first,
under its name, then the commands that work anywhere, and every other view's
commands under **Actions → Other Views**. The toolbar command icons are, for
example, **New Account**, **Edit Account**, **Security Price**, and **Exchange
Rate** in Accounts, **Manage FSA Claims** on the FSA
Dashboard, **Configure Dashboard Groups** on the Dashboard, **New Scheduled**,
**Suggest**, and **New Loan** in Scheduled, **New Scenario**, **Scenarios**, and
**Explore** in Plan, and **Compare** and **Export** in Projection. **View → Hide
Empty Accounts** and **View → Show Hidden Accounts** filter the account tree.
Buttons that act on a selected row, such as a schedule's **View / Edit…**, stay
beside their table.

Payees, categorization rules, and reimbursable expenses open from **Actions →
Payees…**, **Actions → Categorization Rules…**, and **Actions → Reimbursable
Expenses…**.

On the Dashboard, account groups, pending bills, and expected income are separate
sections, each as wide as its own columns; they sit side by side when the window has
room and stack when it does not. A long group line is shortened with "…"; hover over
it to read it and the accounts it covers. Activate a missed-schedule row to open its
schedule.

### Net worth history

Choose the **History** toolbar icon (or **Net Worth History…** in the menus) while
the Dashboard is shown. **Group by** switches between months, quarters, and years;
hover over a period to see its top-level account values, and **Print…** previews the
table. See [Net worth history](../USER_GUIDE.md#net-worth-history).

Each table has its own column chooser (the "⋯" button at the right of that table's
heading); its tooltip names the table, and the columns you hide are remembered.
When you make the window narrower, text columns such as descriptions shorten (with
"…") so every column, including every amount, stays visible. On a small screen, a
long dialog scrolls its form, and its Save and Cancel buttons stay at the bottom.

Every view you open, and the register of every account you open, gets a tab in the
bar below the toolbar. Choose a tab to go back to it; each register tab keeps its
own place, filter, and half-typed entry. Opening an account that already has a tab
returns to that tab. The × on a tab closes it; closing a register with an unsaved
entry asks first, and closing the last tab returns to the Dashboard. Opening
another book starts with fresh tabs.

Registers can also open in independent windows; their account, filter, selection,
and expanded row do not replace the main window's register state.

## Exchange rates and security prices

In Accounts, choose the **Exchange Rate** toolbar icon (or **Actions → Exchange
Rate…**). The dialog shows the latest recorded quote for the
chosen pair, accepts the rate in your usual decimal format, and refuses the same
currency twice, a non-positive rate, or an invalid date without saving anything.

For an Investment or Retirement account, use **Security Price** (a toolbar icon in
Accounts, or **Actions → Security Price…** there) to define a security and record an
exact dated price. See
[Security prices and current value](../USER_GUIDE.md#security-prices-and-current-value).

## Enter transactions in a register

Select an account to open its register. It opens scrolled to the most recent entry
at the bottom; posting a new entry keeps you there, and changes made elsewhere leave
your place alone. The register filter also matches payee names.

The last row of every register is a blank transaction. Type a new entry straight
into it:

1. Enter the date (it starts as the date you last entered, or today), an optional
   number, and a description. Choose a payee if you use one.
2. Choose the other visible account under **Transfer**.
3. Type a positive amount under the heading that describes the effect on this
   account, such as **Deposit** or **Withdrawal**. Typing in one clears the other.
4. Press **Enter** to save it. The row empties for the next entry, keeping the
   date.

**Tab** and **Shift+Tab** move between the row's fields, and **Escape** clears
the row. If something is missing or wrong, the line under the register says
what. Your typing stays in place, and the cursor moves to that field. The row is
greyed out, with the reason shown in its Description cell, for a hidden or
placeholder account. When you leave the description, the proposal from an earlier
matching transaction is filled in, and the line under the register says where it
came from (see [Transactions and registers](../USER_GUIDE.md#transactions-and-registers)).

For more than two splits, choose **Split** at the end of the row. The row opens
into one line per split, each with a memo, an account, and an amount under the
account's own Increase or Decrease heading. What you had typed carries into the
first two lines, and an empty line waits at the bottom for the next split. An
**Imbalance** line shows how far the splits are from balancing. Enter saves the
transaction only once it reads **Balanced** and one split is in this register's
account. Choose **Split** again to fold two lines back into a single row.

The pencil icon beside **Split** opens the full transaction editor filled in with
what you typed, including every split line. Saving there empties the row, and
cancelling leaves it as it was. If you switch accounts, open another
transaction, or close a register window while the row holds typing, BreadSched
asks whether to save it, discard it, or stay.

To change an existing transaction without opening a window, select it and press
**F2** (or choose **Actions → Edit Transaction in Place**). Its row
turns into the same fields as the blank row, and a transaction with more than two
splits opens its split lines underneath. Edit anything, then press **Enter** to
save, or **Escape** to put the row back as it was. The pencil icon opens the same
transaction in the full editor. Double-clicking a transaction also opens the full
editor.

In the full editor, a new transaction is proposed the same way: when you leave the
description or choose a payee, and no split has an amount or memo yet, every split
of the latest matching transaction (with its accounts, amounts, and memos) is filled
in, along with its payee if you have not chosen one, and a note says where it came
from. Edit anything before choosing **Save**. Choose a payee under **Payee**, or
**(no payee)** to clear it.

## Reconcile a statement

1. Choose **Reconcile…** from the register.
2. Enter the statement date and ending balance.
3. Check eligible entries until the exact difference is zero.
4. Finish the session to mark the selected splits reconciled.

If a deposit in that account looks like money back on a
[reimbursable expense](#reimbursable-expenses), the dialog says how many such
credits are waiting.

## Scheduled activity

Use **New Scheduled** for a fixed or formula-driven commitment or estimate, and
**Suggest** for reviewable estimate drafts from history. Choose the **New Loan**
toolbar icon in Scheduled to create a loan (see
[Create a loan](../USER_GUIDE.md#create-a-loan)).

### Review due transactions

BreadSched asks when a book opens (after any held GnuCash changes), and **Review
due…** in Scheduled opens the same review. Choose each date's action, or one for all
of a schedule's dates, then **Apply**. See
[Review due and missed transactions](../USER_GUIDE.md#review-due-and-missed-transactions).

## Plan and projection

Set the Plan controls and apply them; they are shared with the browser. Select a
value to inspect its occurrences and actual splits, and use **Resolve actuals…** to
match an actual, mark it unexpected, or review it. The foreign-currency note sits
under the Plan summary.

Projection runs longer calculations in the background and lets you cancel them.
Use **New Scenario** and **Scenarios** in Plan, and **Compare** in Projection.

### Explore expenses

Apply the Plan controls first, then choose the **Explore** toolbar icon while Plan
is shown. Click a period on the **Spending over time** or **Income over time** chart
to make it the comparison period, use **Sort categories** to order the category rows, and choose a
**Category trend**. **Carry prior periods** turns rollover on. See
[Explore expenses](../USER_GUIDE.md#explore-expenses).

## Reimbursable expenses

Select the transaction in a register and choose **Actions → Track as
Reimbursable…**. The date, description, and expense amount are filled in; enter who
owes you and choose **Add receivable**, and the expense is linked.

**Actions → Reimbursable Expenses…** lists every receivable with its expense, what
is reimbursed, written off, and still remaining, its status, and its age. Choose
**Open** on one to change it. **Held in** chooses the receivable account (the default
is one per currency). **Link expense** adds more expense splits and **Link
reimbursement** links a credit that paid money back. **Proposed reimbursements**
lists credits that clearly belong to one open receivable, checked; choose **Accept
selected** to link them. Record a dispute or write off part of the balance on the
same screen. A warning appears when a linked expense is also on an FSA claim.

When the FSA pays what the insurer does not, open **Manage FSA Claims** on the FSA
Dashboard, choose the receivable under **Payer covers part**, and save the claim.
The claim and the receivable then show what the payer, the FSA, and you each pay,
and **Needs review** when that comes to more than was paid. See
[Reimbursable expenses](../USER_GUIDE.md#reimbursable-expenses).

## Import files

**File → Import GnuCash Book into Current Book…** (`Ctrl+I`) reads GnuCash, QIF,
and OFX/QFX files. The detected format is shown as soon as you choose the file,
before anything is written, and explicit decimal and date-order choices override
detection when a file is ambiguous. Imports run in the background as one
atomic undo step. Leave the open book and source file in place until completion or
cancellation; a cancelled import writes nothing. After an import, BreadSched says
how many reimbursement proposals are waiting.

### Import a CSV statement

Choose **File → Import CSV Statement…** and choose the file; BreadSched reads its
columns at once, shows the first rows, and suggests the mapping. Adjust the account,
columns, and options, choose **Preview**, then **Import**. Clearing **First row is a
header** rereads the file with numbered columns. **Link possible transfers** joins
each possible-transfer pair. See
[Import a CSV statement](../USER_GUIDE.md#import-a-csv-statement).

### Review held GnuCash changes

When a re-import holds GnuCash changes to reconciled transactions, the review opens
after the import and whenever the book opens, before the review of due scheduled
transactions. Choose each row's decision, then **Apply**.

### Write changes back to GnuCash

Close the book in GnuCash, then choose **File → Write Changes to GnuCash…**. Tick
the transactions to write and choose **Write selected**. **Backups to keep** sets how
many backups are kept. See
[Write changes back to a GnuCash SQLite book](../USER_GUIDE.md#write-changes-back-to-a-gnucash-sqlite-book).

## Payees

Choose **Actions → Payees…**. Enter a name and one or more example descriptions (one
per line) and choose **Add payee**. The **Proposals** list shows each transaction
without a payee whose description matches, with the matched key; every proposal
starts checked. Clear any you do not want and choose **Accept selected**. **Edit**
loads a payee into the form so you can rename it or change its descriptions;
**Delete** removes it and clears it from its transactions (**Edit → Undo** restores
it).

## Categorization rules

Choose **Actions → Categorization Rules…**. Choose whether a rule matches a
description (enter an example) or a payee, choose the category, and choose **Add
rule**. **Up** and **Down** change a rule's priority and **Delete** removes it
(**Edit → Undo** restores it). The **Proposals** list shows each transaction's
proposed category, the deciding rule, and any later rule that would have chosen
differently; every proposal starts checked. Clear any you do not want and choose
**Accept selected**.

## Print and export

Print Dashboard, Plan, or Projection from the toolbar or **File → Print Current
View** (`Ctrl+P`). BreadSched opens a self-contained preview in the default browser;
use the browser print dialog for a printer or PDF. In Expense Explorer, **Print…**
previews spending and income over time and the selected category and period with
merchant detail. Use **File → Export Transactions** for transaction data.

## Protect and recover a book

Use these File menu commands regularly:

- **Back Up Current Book…** creates an independent verified backup.
- **Verify Current Book** checks the book's integrity and financial relationships.
- **Restore Backup as New Book…** verifies a backup, writes it to a different path,
  and opens the result. It will not overwrite the currently open live book.

## Troubleshooting

- If GTK is unavailable, run `breadsched-gtk --help` and install the named GTK 4 and
  PyGObject packages. The [command line](cli.md) continues to work without GTK.
- **Edit → Undo** reverses the last change, including a whole import or a batch of
  accepted proposals.
