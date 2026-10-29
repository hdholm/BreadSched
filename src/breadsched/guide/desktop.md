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
import a GnuCash book into a new BreadSched book. With no argument it reopens the
book you used last, and the window shows the BreadSched logo with **Opening** and the
book's name until it is ready (or that it is upgrading, for an older book). The equivalent commands are
`breadsched gui` and `python -m breadsched.gui`. **File → New Book** creates a book
at any time, and **File → Import GnuCash Book into New Book** starts from GnuCash.

The desktop application needs a GTK 4 runtime and PyGObject supplied by your
operating system. If they are missing, `breadsched-gtk --help` names the packages to
install; the [command line](cli.md) and the [browser interface](web.md) keep working
without GTK.

Press `F1` or choose **Help → User Guide** to open this guide. The switcher at the
top shows the overview or the desktop, browser, or command-line part, and a link to
another part opens it. **Help** in an import, reconciliation, schedule, paycheck,
rule, reimbursable, goal, Expense Explorer, or net worth history window opens
the guide at that window's section.

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

### Install on Linux

On Ubuntu 24.04 or later, Debian 12 or later, or Fedora, the release's native package
uses your system's Python 3 and GTK 4. Unlike the Flatpak, it can use Finance::Quote
for [online quotes](#online-quotes), which it installs as a recommended package.
Check the file against `SHA256SUMS`, then install it with your package manager,
which also fetches what it needs:

```sh
sudo apt install ./breadsched_<version>_all.deb         # Debian, Ubuntu
sudo dnf install ./breadsched-<version>-1.noarch.rpm    # Fedora
```

BreadSched then appears in your application menu, and `breadsched` is the
[command line](cli.md). Installing a newer package upgrades it. Removing it
(`sudo apt remove breadsched` or `sudo dnf remove breadsched`) never removes a book.

The Flatpak bundle (`BreadSched-<version>.flatpak`) installs BreadSched with its
own GTK runtime from Flathub. Check it against the release's `SHA256SUMS`
(`sha256sum BreadSched-<version>.flatpak`), then install it for your user:

```sh
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --user BreadSched-<version>.flatpak
```

BreadSched then appears in your desktop's application menu. Installing a newer
bundle the same way upgrades it and keeps your books; `flatpak uninstall --user
org.breadsched.BreadSched` removes the program, never a book. From a terminal,
`flatpak run org.breadsched.BreadSched` starts the desktop application and
`flatpak run --command=breadsched org.breadsched.BreadSched` is the
[command line](cli.md).

The Flatpak may use your **Documents** folder directly, and that is the best place
for books. A book anywhere else can still be opened, created, or saved through the
file chooser, but BreadSched can then reach only that one file: backups it makes
before upgrading or restoring over the book go to
`~/.var/app/org.breadsched.BreadSched/data/breadsched/beside-documents/` instead of
beside the book, the book gets no default attachment folder, and BreadSched says so
when it opens the book.

## Find your way around

The main window opens at the size you last left it, maximized if it was; a
dialog you resized opens at that size next time. Sizes never exceed the screen
and can be forgotten in [Preferences](#preferences). BreadSched fits a small
laptop screen however long your account names and notes are: a long name in a choice list is shortened in the middle when chosen (the list
shows it in full), wide toolbars scroll sideways, the Dashboard's summary cards wrap
onto more rows, and dialogs open within the screen and scroll inside. Scrollbars are
always shown, so a list with more out of view says so. A long choice list, such as
every list of accounts, has a search field when it opens: type part of a name to
keep only the choices that contain it.

The toolbar starts with the commands that work anywhere (undo, redo, new
transaction, and print); open and import a book from the **File** menu or the start
screen. Next comes one icon for each other work area; the view you are in has no
icon, since you are already there. The name of the view you are in, in bold, then
divides those icons from that view's own command icons, such as **Reconcile** in a
register. On the Dashboard and Accounts, two short lines after them count the
book's accounts and transactions. The **View** menu lists every area.
The **Actions** menu follows the view too: the current view's commands come first,
under its name, then the commands that work anywhere, and every other view's
commands under **Actions → Other Views**. The **Reports** menu lists every report
wherever it is calculated: **Net Worth History…**, **Holdings and Cost Basis…**,
**Realized Gains…**, **Tax Year…**, **Explore Expenses…**, and **Budget Jars…**.
A menu item that asks for more before it acts ends in "…". The toolbar command
icons are, for example, **New Account**, **Edit Account**, **Security Price**, and
**Exchange Rate** in Accounts, **Manage FSA Claims** on the FSA Dashboard (also in
the Actions menu in every view),
**Configure Dashboard** on the Dashboard, **New Scheduled**, **Suggest**, and
**New Loan** in Scheduled, **New Scenario**, **Scenarios**, and **Explore** in Plan,
and **Compare**, **Export**, and **New tab** in Projection. Each icon stands for one
command, so the same picture never means two things. **View → Hide Empty
Accounts** and **View → Show Hidden Accounts** filter the account tree, and **View
→ Double Line** shows every register's notes line. Buttons that act on a selected
row, such as a schedule's **View / Edit…**, stay beside their table.

**New Transaction…** (the toolbar's **Transaction**, or Ctrl+T) opens the
transaction editor; with a register shown, the new transaction starts in that
register's account. Each split row has an account, a memo, and two amount columns
named for that account, as its register names them (for example **Payment** and
**Charge** for a credit card, **Expense** and **Rebate** for an expense), so you
never need a minus sign. A blank row shows what it would take to balance the
others, and saving fills it in. When every row has an amount and they do not
balance, a new row appears holding the rest, waiting for you to choose its
account; it goes again if you balance the others yourself.

Categorization rules and reimbursable expenses open from **Actions →
Categorization Rules…** and **Actions → Reimbursable Expenses…**.

On the Dashboard, account groups, pending bills, and expected income are separate
sections, each as wide as its own columns; they sit side by side when the window has
room and stack when it does not. A long group line is shortened with "…"; hover over
it to read it and the accounts it covers. Activate a missed-schedule row to open its
schedule. When savings goals have set money aside, a **Set aside for goals** card
shows the total (see [Savings goals](../USER_GUIDE.md#savings-goals)).

### Net worth history

Choose the **History** toolbar icon (or **Net Worth History…** in the menus) while
the Dashboard is shown. **Group by** switches between months, quarters, and years;
the charts above the table show assets, debts, and net worth, and net worth's
composition by group. Hover over a chart for exact amounts or over a period to see
its group values, and **Print…** prints the charts and table through the system
print dialog. Choose a period's **Change** (or **Explain** for the first period) to list the
postings behind it below the table; that section has its own **Print…** and
**Export CSV…**. See [Net worth history](../USER_GUIDE.md#net-worth-history).

Each table has its own column chooser (the "⋯" button at the right of that table's
heading); its tooltip names the table, and the columns you hide are remembered. The
Dashboard's tables are the exception: their columns are chosen in **Configure
Dashboard** (see [Configure the Dashboard](#configure-the-dashboard)).
The register is a ledger grid with GnuCash's fixed columns, so it has no chooser.
When you make the window narrower, text columns such as descriptions shorten (with
"…") so every column, including every amount, stays visible. On a small screen, a
long dialog scrolls its form, and its Save and Cancel buttons stay at the bottom.

Dialogs work the same way throughout: **Escape** closes one as its **Cancel** or
**Close** button does, **Enter** in a one-line field chooses its main button (such as
**Save**), and **Help** opens this guide at its section. Every **Delete** asks first.

Every view you open, and the register of every account you open, gets a tab in the
bar below the toolbar. Choosing a tab puts the keyboard in what it shows (in a
register, the cell under the cursor). **Ctrl+Page Up** and **Ctrl+Page Down** move
between tabs. Drag a tab onto another to put it in that place, or move the current
tab one place with **Ctrl+Shift+Page Up** and **Ctrl+Shift+Page Down** (**View → Move
Tab Left** and **Move Tab Right**). **Ctrl+W** closes the current one, and **Ctrl+F** (**Edit → Find in
Register**) goes to the register's filter, which keeps the transactions whose date,
number, description, notes, tags, memos, accounts, or amounts (such as 67 or 67.00)
contain what you type. Choose a tab to go back to it; each register tab keeps its
own place, filter, and half-typed entry. Opening an account that already has a tab
returns to that tab. To open another register beside the one you are in, choose
**New tab** in the register's toolbar (**Actions → Open Register in New Tab**): it
shows an account that has no tab yet, and its account list switches it to any
account, including one another tab already shows. The × on a tab closes it; closing a register with an unsaved
entry asks first, and closing the last tab returns to the Dashboard.

In Projection, **New tab** (**Actions → Open Scenario in New Tab**) keeps the
scenario shown in a tab of its own, titled **Projection:** and the scenario's
name, so you can switch between futures without choosing again. The main
**Projection** tab follows the scenario chosen in Plan; a scenario tab keeps its
own, and choosing another scenario in it retitles the tab. Base assumptions you
change anywhere reach every tab whose scenario uses them.

Each book remembers its tabs: opening it again brings back the same views,
registers, and scenario tabs, in the same order, with the same tab selected. A tab for an account or
scenario deleted since is left out. Another book has its own tabs. Only the selected
tab is drawn when the book opens, so many open tabs no longer slow opening it; the
others are drawn when you choose them. Registers, Accounts, and the Plan are read in
the background instead: they are ready when you switch to them, and after a change the
Plan keeps showing what it had, marked **Updating…**, until the new figures are in.

Registers can also open in independent windows; their account, filter, and cursor
do not replace the main window's register state.

## Accounts

In Accounts, **New Account** and **Edit Account** (toolbar icons, or the Actions
menu) open the account editor: its name, type, parent, code, description, and notes,
whether it is a placeholder or hidden, and the settings its type has, such as a
card's payment terms, a loan's asset, an FSA's funding years, or an investment's
return and cost method. **Delete** asks first, and refuses an account the book still
needs, saying why. See [Accounts](../USER_GUIDE.md#accounts).

## Configure the Dashboard

On the Dashboard, **Configure Dashboard** (toolbar icon or the Actions menu) holds
everything the Dashboard can be told, on three pages. All of it is your own
arrangement of this book: anyone else it is shared with keeps theirs.

- **General**: the two horizons, **Liquid for** so many days of bills and how many
  months of outgoings the **Emergency fund covers**, and which sections show:
  Account groups, Pending bills, Expected income, Pending reimbursables (off until
  you turn it on), and Savings goals. The summary cards always show and name the
  horizons they were judged against.
- **Groups**: each group's name (a `:` nests one group in another), its kind, and
  its accounts. A group lists only its own accounts, each with a remove button;
  **Add an account…** is a searchable list (type part of a name) and **Add** puts
  the chosen account in the group. An account is in at most one group, so adding it
  to one takes it out of another, and the dialog says so. **Suggest from accounts**
  proposes groups from the chart of accounts.
- **Columns**: the columns each Dashboard table shows. These are yours, remembered
  on this computer rather than in the book, and change as soon as you tick them.

**Save** stores the horizons, sections, and groups in the book; **Cancel** leaves
them as they were. Groups only arrange what the Dashboard shows; its totals cover the
whole book. Printing the Dashboard prints the sections it shows. See
[Dashboard and near-term cash](../USER_GUIDE.md#dashboard-and-near-term-cash).

## Exchange rates and security prices

In Accounts, choose the **Exchange Rate** toolbar icon (or **Actions → Exchange
Rate…**). The dialog shows the latest recorded quote for the
chosen pair, accepts the rate in your usual decimal format, and refuses the same
currency twice, a non-positive rate, or an invalid date without saving anything.

For an Investment or Retirement account, use **Security Price** (a toolbar icon in
Accounts, or **Actions → Security Price…** there) to define a security and record an
exact dated price. For a security with a quote source, **Get online quote** fetches
its current price into the form for you to check; it is saved only when you choose
**Save price**. See
[Security prices and current value](../USER_GUIDE.md#security-prices-and-current-value).
**Actions → Holdings and Cost Basis…** in Accounts lists each holding's shares, cost,
market value, and unrealized gain; expand a holding for its lots, sales, transfers,
share splits, and notes
(see [Holdings and cost basis](../USER_GUIDE.md#holdings-and-cost-basis)). An
Investment or Retirement account's editor chooses **Cost of shares sold**: first in,
first out, or average cost.

## Choose the lots a sale sells

In **Holdings and Cost Basis**, expand a holding and choose **Choose Lots…** beside a
sale. Enter the shares to sell from each lot listed (the lots held just before the
sale) and choose **Save**; shares left unassigned are sold by the account's method,
and **Use the Account's Method** clears the choice. **Realized Gains…** shows every
sale with the lots it took and totals by year; choose a year, open a sale to choose
its lots, or **Print…** the report. See
[Specific lots and realized gains](../USER_GUIDE.md#specific-lots-and-realized-gains).

## Tax year

In Accounts, choose **Actions → Tax Year…** and pick a **Year**. It lists the year's
realized gains as short-term and long-term (by sale, with each part's purchase date
or *Various*), the totals of the tax-relevant accounts and tags, and income by
source. **Tax-Relevant Accounts and Tags…** lists every tag and account with a check
box; an account checked by GnuCash's *tax related* mark says so until you change it.
**Print…** prints the report. See [Tax year](../USER_GUIDE.md#tax-year).

## Budget jars

In Plan, choose **Actions → Budget Jars…**. It covers the Plan's From and Through
range and starts with its Group by, which you can change. It lists all jars by
period, then each account; open an account to see its charts (planned against
actual, and jar levels against their targets; hover over a column for its amount)
and each jar. **Print…** prints the report with its charts. See [Budget jars](../USER_GUIDE.md#budget-jars).

## Online quotes

In Accounts, choose **Actions → Online Quotes…**. Each security and foreign currency
has a **Quote source**: `tsp` for Thrift Savings Plan funds, `alphavantage` for
stocks and funds, `currency` for exchange rates, or a Finance::Quote method such as
`usa` or `nyse`. The arrow in each source opens the list of sources you can use
here, Finance::Quote's methods included; typing narrows it. Saving a source that is
not on the list keeps it but says why it will not fetch. Leave it empty for no
online quotes. Enter your Alpha Vantage API key if
you use that source; it is kept in your settings, readable only by you, never in the
book. **Save sources** stores your choices, and **Get quotes** also fetches every
quote in the background and lists what was stored and what failed and why, beside
the buttons so it stays in view. The line under Finance::Quote says whether it can
be used here and how many methods it offers; the Flatpak cannot use it. See
[Online quotes](../USER_GUIDE.md#online-quotes).

## Enter transactions in a register

The register is a ledger grid laid out like GnuCash's: each transaction is one
ruled line of plain text, the transaction you are on is shaded, and the cell you
are typing in is outlined. Select an account to open its register. It opens at
the bottom, on the blank transaction after the most recent entry, with the cursor
in its date ready for the next entry; posting keeps you there, and changes made
elsewhere leave your place alone.

Type a new entry straight into the blank transaction:

1. Enter the date (it starts as the date you last entered, or today), an optional
   number, and a description.
2. Type the other account under **Transfer**.
3. Type a positive amount under the heading that describes the effect on this
   account, such as **Deposit** or **Withdrawal**. Typing in one clears the other.
4. Press **Enter** to save it. A new blank transaction waits for the next entry,
   keeping the date.

Arriving in a cell selects everything in it, so typing (or Backspace) replaces a
proposed amount or account; **Left** or **Right** goes to its start or end instead.
**Tab** and **Shift+Tab** move between cells; **Tab** past the last cell saves
the transaction, like **Enter**. From a transfer to an expense, **Tab** goes straight
to the money-out column (**Withdrawal** or **Charge**), where the expense belongs;
after that one step it follows the usual order. **Escape** puts the transaction back as it was
(the blank one empties). The register's **Enter** and **Cancel** buttons, and
**Actions → Enter Transaction** and **Cancel Transaction**, do the same as the keys,
so you can save or abandon what you typed without them. Dates, amounts, and **Num** take the shortcuts in
[Typing shortcuts in the register](../USER_GUIDE.md#typing-shortcuts-in-the-register):
for example `+` for the next day or `100/3` for an amount.

The **Transfer** cell completes as you type, as GnuCash does. `Ex` fills in
`Expenses` with the added letters selected, so you can keep typing over them; `:`
accepts that level and moves to the next, so `Ex:Gr` reaches
`Expenses:Groceries`. The accounts that still match are listed under the cell:
**Up** and **Down** choose one, and **Tab** or **Enter** takes it (clicking one
does too). The description completes the same way from earlier entries in this
register; when you leave it, the earlier transaction's account and amount (and its
notes, if you have typed none) are filled in, and the line under the register says
where they came from (see
[Transactions and registers](../USER_GUIDE.md#transactions-and-registers)).

If something is missing or wrong, the line under the register says what; your
typing stays, and the cursor moves to that cell. A hidden or placeholder account
takes no new entries, and the line under the register says why.

To change an existing transaction, click any of its cells or move to it with
**Up** and **Down**; type, then press **Enter** to save it (the cursor moves on)
or **Escape** to put it back. As in GnuCash, **Up**, **Down**, **Page Up**, and
**Page Down** save the transaction you are leaving if you changed it; if it cannot
be saved, you stay on it and the line under the register says why. **Ctrl+End**
returns to the blank transaction.

A transaction with more than two splits shows **-- Split Transaction --** as its
transfer; when you move onto it, its split lines open beneath it, each with a
memo, an account (completed the same way), and an amount under the account's own
Increase or Decrease heading, and an empty line waits for another split. Choose
**Split** in the toolbar to open an ordinary transaction (or the one you are
typing) the same way, or to fold two lines back into one. While the splits do not
balance, the empty last line shows the amount left, in the column that balances
them: choose its account (or type a memo) and it takes that amount, or type a
different one. **Enter** saves only once they balance. A new transaction needs one split in this register's account. To move an
existing one entered in the wrong account, such as a purchase on the wrong credit
card, change that split line's account to the right one and press **Enter**: the
transaction moves there and leaves this register, and the line under the register
says where it went.

### Shares in an investment register

The register of an account that holds a security, such as a mutual fund imported
from GnuCash, shows **Shares** and **Price** for each transaction before the
amounts, and **Shares Held** after each one instead of a balance. The line at the
bottom gives the shares you hold and what they are worth at the latest price,
with that price's date and source; with no price yet it says so and shows the
amount in the books instead.

To record a purchase or sale, type any two of **Shares**, **Price**, and the
amount under **Buy** or **Sell**, and the third is worked out when you leave the
cell: shares and a price give the amount (to the cent), shares and an amount give
the price, and a price and an amount give the shares (to the fraction of a share
the account records). Changing the shares or the price later recomputes the
amount; changing the amount recomputes the price. Shares are entered as a number
of shares; whether they are bought or sold follows the amount's column. A
transaction needs its shares: without them it is not saved, and the cursor moves
to **Shares**. A share split, which changes shares with no amount, shows its shares
and no price.

In the transaction editor, a split in such an account has a **Shares** entry
beside its amount; a negative number means shares sold or moved out.

**View → Double Line** (also in **Edit → Preferences…**) adds a second line under
every transaction in every register, the blank one included, for its notes, as in
GnuCash: Tab moves from the last amount to the notes line, and Enter saves what you
typed there with the transaction. The choice is remembered. With it off, saving keeps the notes a transaction already has.
Whether you are entering a new transaction or changing one, **Enter** (or **Tab**
past the last cell) saves it and moves to the next transaction.

**Row shading** in **Edit → Preferences…** keeps rows apart: **Shade alternate
transactions** (a transaction's notes and split lines share its shade), **Shade
alternate lines**, or **No shading**. Either change redraws every open register
and leaves the keyboard in the register's cell, so you can keep typing.

Typing in **Transfer** without a `:` also lists accounts that contain what you
typed anywhere in their name, after those whose names begin with it: `groc` lists
`Expenses:Groceries`. Choose one from the list; the cell completes only names that
begin with what you typed.

The pencil icon opens the transaction under the cursor in the full transaction
editor, for a claim or other details; on the blank transaction it opens a
new one filled in with what you typed, including every split line. Saving there
empties the blank transaction, and cancelling leaves it as it was. If you switch
accounts, open another transaction, or close a register window while a
transaction holds unsaved typing, BreadSched asks whether to save it, discard it,
or stay.

The **R** column shows each entry's reconcile state in this account: **n** (not
cleared), **c** (cleared), or **y** (reconciled). Click **n** or **c** to switch
between them, for example while ticking off a statement. If a statement is open
for this account, a newly cleared entry dated on or before it is also checked
there. A reconciled entry changes only by reopening its statement (see
[Reconcile a statement](#reconcile-a-statement)).

In the full editor, a new transaction is proposed the same way: when you leave the
description and no split has an amount or memo yet, every split of the latest
matching transaction (with its accounts, amounts, and memos) is filled in, and its
notes too if you have typed none; a note says where it came from. Edit anything before choosing **Save**.

### Tags and linked documents

The full transaction editor opens on **Entry** (date, description, number, and
notes) above the splits; **Tags & documents** is its second tab, counting the tags
and documents a transaction has. **Planning detail**, beside **Add split**, shows
each split's planning purpose, and its investment activity for an investment or
retirement account; a split that already has either shows it anyway.

On **Tags & documents**, type tags under **Tags**, separated by commas; they
are saved with the transaction. The register filter also matches tags.

**Documents** lists the transaction's linked documents once it has been saved. A
missing file is shown in red, marked **missing**, and its tooltip says where
BreadSched looked. **Attach file…** copies a file into the book's attachment folder
and links it. Type a web address and choose **Link address** to link a page.
**Open** opens a document with your usual application or browser, **Relink…**
points a moved or missing file at where it is now, and the remove button unlinks
it without deleting the file. A document linked in GnuCash is shown dimmed and can
only be opened. Each change is saved at once and can be undone with **Edit → Undo**.
See [Tags and linked documents](../USER_GUIDE.md#tags-and-linked-documents).

## Find transactions

**Edit → Find Transactions…** (Ctrl+Shift+F) searches every register. **Text**
matches anything a transaction holds, as the register filter does. **Account**
narrows it to one account, with its subaccounts unless you untick **Include
subaccounts**. **Dates** bound it.

**Amount** finds a split of exactly that size, either direction, so 67 finds a
67.00 charge and never check 1067; **At least** and **At most** give a range.
**Direction** keeps money in (debits) or money out (credits). **Tags** keeps
transactions that have every tag listed.

**Sort by** and **Descending** order the rows. **Subtotal by** groups them by
account, month, year, or tag, with a subtotal under each group; the last line is
the total, per currency. A row is one split, so a transaction can appear once for
each leg that matches. Activate a row to open its register. See
[Find transactions](../USER_GUIDE.md#find-transactions).

## Reconcile a statement

1. Choose **Reconcile** in the register's toolbar or **Actions → Reconcile…**.
2. Enter the statement date and ending balance. Compare **Starting balance**, the
   account's reconciled balance on that date, with the statement's previous balance.
   A charge or credit on the statement that is not yet in the book, such as interest,
   can be added here: enter its description, the account it belongs to, and the
   amount (positive raises the balance, as interest charged on a card does), then
   choose **Add**. It is added cleared, so it starts checked.
3. Choose **Start reconciliation**. Entries are listed in two columns, **Funds In**
   and **Funds Out**, each with its checked total; below them are the starting,
   reconciled, and ending balances and the difference. Entries dated after the
   statement are listed below an **After** heading, shaded, in case a date is wrong.
   To find an entry, order a column with the buttons above it: **✓** (checked
   first), **Date**, **Description**, or **Amount**; choose the same one again to
   reverse it. Each column keeps its own order, and later entries stay below the
   heading.
4. Check entries until the difference is zero. Click a row (or its check) to check
   or uncheck it, or press Enter or Space on it. A click selects only that row, in
   both columns; Ctrl-click adds or removes a row and Shift-click selects a range in
   one column, without checking anything. Then choose **Check Selected** (or
   **Uncheck Selected**) to check them all at once. Double-click a row to edit its
   transaction; the double-click leaves its check as it was.
5. Choose **Finish** to mark the checked splits reconciled.

The window does not block the rest of BreadSched: you can enter a missed scheduled
transaction or correct one in a register while it is open, and the lists follow.
**New Transaction…** enters one in this account dated on the statement, and **Edit
Transaction…** opens the transaction of the selected row you clicked last. Choosing Reconcile again brings
the open window forward.

Checks are saved as you make them. **Postpone** closes the window and keeps the
statement, which reopens where you left off; **Abandon…** discards the statement
without changing any transaction.

If a deposit in that account looks like money back on a
[reimbursable expense](#reimbursable-expenses), the dialog says how many such
credits are waiting.

## Scheduled activity

Use **New Scheduled** for a fixed or formula-driven commitment or estimate, and
**Suggest** for reviewable estimate drafts from history. **Frequency** is **Every**
a number and a unit: days, weeks, months, months on the same weekday, months on the
last weekday, or years; **twice a month** and **once** take no number. The first due
date sets the day. Amounts can be calculations such as `76.84*1.06`. The editor opens
on **Basics** (name, kind, accounts, amount, and when it repeats); **Splits** holds the
memos and additional splits, **Future changes** the future, seasonal, skipped, and
one-time amounts (its tab counts them, so a schedule with some says so), and
**Advanced** the projection growth, planning purposes, weekend rule, posting, and
enter and remind days. **Investment activity** appears there only when the schedule
uses an investment or retirement account, or already has one. A scenario estimate's
editor is laid out the same way. Choose the **New Loan**
toolbar icon in Scheduled to create a loan (see
[Create a loan](../USER_GUIDE.md#create-a-loan)).

### Paychecks and pay changes

Choose the **Payroll** toolbar icon in Scheduled (or **Payroll…** in the Actions
menu); it opens on the selected schedule when that is a paycheck. **Breakdown** shows
the paycheck's lines as of a date and its take-home share. **Pay change** takes the
date and new gross, and for each line **Keep**, **Scale with gross**, or **Set to** an
amount; **Preview** fills the After column without saving, and **Save pay change**
saves it as one undo step. **Templates** edits payroll templates (each line an amount
such as 85.50 or a percentage such as 6.2%), fills one from the selected paycheck, and
creates a new paycheck from one. See
[Paychecks and pay changes](../USER_GUIDE.md#paychecks-and-pay-changes).

### Loans

**New Loan** in Scheduled sets up a loan from its name, amount borrowed, annual
rate, term, and first payment, with the loan, interest expense, and paying
accounts; **Create loan** saves the loan account and its payment schedule. See
[Create a loan](../USER_GUIDE.md#create-a-loan).

### Review due transactions

BreadSched asks when a book opens (after any held GnuCash changes), and **Actions →
Review Due Transactions…**, or the **Review Due Transactions…** button in Upcoming,
opens the same review; if nothing is due, it says so. Choose each date's action, or one for all
of a schedule's dates, then **Apply**. A schedule with several dates lists them under
**Show the dates**, so a long list of missed dates opens quickly. If anything was posted, **Posted
transactions** lists it split by split; choose **Edit…** to change one in the
transaction editor or **Delete** to remove it, then **Close**. See
[Review due and missed transactions](../USER_GUIDE.md#review-due-and-missed-transactions).

### Enter or skip the next occurrence

Select a schedule in Scheduled and choose **Enter Next** to post its next
occurrence now, or **Skip Next** to mark it done without posting; hover either to
see the date and amount. An entered transaction opens in **Posted transactions**,
and **Edit → Undo** reverses either. In the schedule editor, **Enter in advance
(days)** and **Remind in advance (days)** set how long before its date an
occurrence joins the review; its rows say *(in advance)* or *(reminder)*. See
[Review due and missed transactions](../USER_GUIDE.md#review-due-and-missed-transactions).

## Plan and projection

Set the Plan controls and apply them; they are yours, shared with the browser when you
use it on this computer, while anyone else the book is shared with keeps their own. Select a
value to inspect its occurrences and actual splits, and use **Resolve actuals…** to
match an actual, mark it unexpected, or review it. In **Resolve actuals**, each
candidate lists why it is offered, the buttons explain themselves on hover, and the
line under them says what the available actions do. The Plan summary gives planned,
actual, and variance through the as-of date; **Show** chooses Plan, Period actual,
or Period variance for the table (see
[Reporting terms](../USER_GUIDE.md#reporting-terms)). The foreign-currency note sits
under the Plan summary. A period or cell that leaves out an amount without a quote
reads **partial**, and its tooltip lists what is left out; Projection, the
Dashboard, net worth history, and Expense Explorer show the same labels and lists
(see [Complete, partial, and unavailable values](../USER_GUIDE.md#complete-partial-and-unavailable-values)).

In Projection, **Explain month…** opens a month: its **How this month reconciles** section
shows the Cash, Investments, Debts, and Net worth bridges for that month, and
**Across the whole projection** the same from the first month to the last (see
[How a projection reconciles](../USER_GUIDE.md#how-a-projection-reconciles)).

The notes under the Projection chart begin with the
[cash runway](../USER_GUIDE.md#cash-runway); with **Compare** they also say which
scenario's cash lasts longer.

Projection runs longer calculations in the background and lets you cancel them.
Use **New Scenario** and **Scenarios** in Plan, and **Compare** in Projection. To
keep several scenarios open at once, use **New tab** in Projection (see
[Find your way around](#find-your-way-around)).

To model [retirement drawdown](../USER_GUIDE.md#retirement-drawdown), choose a saved
scenario in **Scenarios**, then **Edit retirement drawdowns…**. **Add…** asks for the
account to withdraw from, the cash account to pay into, the start date, an optional
end date, and either **A fixed yearly amount** (with **Rise with expense inflation
each year**) or **A yearly share of the balance (%)**. **Edit…** changes the chosen
drawdown and **Remove** deletes it; a refused entry explains why and changes nothing.

### Explore expenses

Apply the Plan controls first, then choose the **Explore** toolbar icon while Plan
is shown. The window opens at once and says it is calculating while it works out the
Plan behind it; **Cancel** closes it. The choices sit two to a row above the
results, and **Print…** and **Close** are at the bottom. Click a period on any **Spending over time** or **Income over time**
chart, or on the **Category trend** chart, to make it the comparison period (choose an **Income detail** category to list that
period's dated income), use **Sort categories** to order the category rows, and choose a
**Category trend**. Changing these uses what was already calculated, so the window
updates straight away. **Carry prior periods** turns rollover on, which calculates
again. See
[Explore expenses](../USER_GUIDE.md#explore-expenses).

## Savings goals

Choose **Savings Goals…** in the menus. Each goal shows its account, target date,
target, what is set aside, what remains, and its status. To add a goal, fill in the
name, the account that holds the money, the target amount, the date to start saving,
and the target date, then choose **Add goal**. **Edit** loads a goal into the form;
save it with **Save changes**. With a goal loaded, **Allocate to goal** sets extra
money aside on the date you enter. **Close** releases a goal's money (for example
after the purchase), **Reopen** undoes that, and **Delete** removes the goal; Edit →
Undo restores it. **Show closed goals** includes closed goals in the list. On the
Dashboard, the **Savings goals** table lists each open goal; activate a row to open
this window.

To change a goal in one scenario, load it with **Edit**, choose the scenario after
**In scenario**, and enter a different target amount or target date, or check
**Leave out**; then choose **Apply to scenario**. With both fields empty and **Leave
out** unchecked, the scenario follows the goal unchanged again. To model the
purchase in that scenario, also enter the purchase date (**Buy on**) and choose the
account under **Purchase in that scenario**. **Scenario changes**
lists every change. Projection shows each goal's target month and what goals have
set aside in its notes, and Plan lists goals whose target date falls in its range.
See [Savings goals](../USER_GUIDE.md#savings-goals).

## Reimbursable expenses

Select the transaction in a register and choose **Actions → Track as
Reimbursable…**. Typing not yet entered is saved first. The date, description, and
expense amount are filled in; enter who owes you and choose **Add receivable**, and
the expense is linked. Nothing is recorded until then. A charge recorded straight
to a Receivable account (a cost your employer will repay, say) is tracked the same
way, and **Held in** starts at that account; see [Two ways to record a reimbursable
cost](../USER_GUIDE.md#two-ways-to-record-a-reimbursable-cost). If the transaction
cannot be tracked (it neither spends on an expense nor charges a Receivable account,
or it could not be saved), an alert says why.

**Actions → Reimbursable Expenses…** lists every receivable with its expense, what
is reimbursed, written off, and still remaining, its status, and its age. Choose
**Open** on one to change it. **Held in** chooses the receivable account (the default
is one per currency). It lists only accounts whose type is **Receivable**; the note
under it names any account that looks like a receivable but is not offered (an
**Asset** account named for receivables, or a placeholder). To use such an account,
change its type to **Receivable** in **Edit Account**. **Link expense** adds more expense splits and **Link
reimbursement** links a credit that paid money back. **Proposed reimbursements**
lists credits that clearly belong to one open receivable, checked; choose **Accept
selected** to link them; a credit to the receivable account itself, such as a
paycheck line, is proposed the same way. Record a dispute or write off part of the
balance on the same screen: the picker after the reason chooses the expense account
the write-off becomes, which a charge recorded to a Receivable account must name.
A warning appears when a linked expense is also on an FSA claim.

When FSA statement lines clearly belong on claims, the FSA Dashboard lists them
first under **Proposed claim links**, each checked; **Link selected** links them.
Import and reconciliation results say when proposals are waiting.

The FSA Dashboard lists open claims with a **Needs attention** column saying what
is left to do, under a line counting them; the Dashboard shows the count as **FSA
claims needing attention**. **Claims report** totals every claim, and **Group by**
switches between status, FSA account, funding year, and provider.

An FSA account's funding years are edited in the account dialog. Each year's second
line takes the plan's **Carryover limit** and **Grace through** date, either or
both, and **Dependent care FSA** marks a dependent care plan; the FSA Dashboard's
**Carried in** and **Carried over** columns show where
unused election went, and **How used** shows what was paid from the FSA card,
reimbursed to your bank, and refunded to the card.

In the transaction editor's **FSA claim (optional)** row and in Review, the role list
offers only what fits the transaction: **Paid from the FSA card** and **Refunded to
the FSA card** for money moving between the FSA card and a provider, **Healthcare
payment** and **Provider refund** for a charge paid another way, and **FSA
reimbursement** or **Repaid to the FSA** for money between the FSA and your bank.

In **Manage FSA Claims**, a changed EOB is kept in the claim's history with the
**EOB change note**. Link money you paid back into the FSA under **Repaid to the
FSA** in the claim's allocation. **Close claim** stops pursuing what is left and
**Reopen claim** takes it up again, each with the reason typed beside them; the line
above shows the claim's status, anything repaid or still to repay, and what a closed
claim gave up.

When the FSA pays what the insurer does not, open **Manage FSA Claims** on the FSA
Dashboard, choose the receivable under **Payer covers part**, and save the claim.
The claim and the receivable then show what the payer, the FSA, and you each pay,
and **Needs review** when that comes to more than was paid. See
[Reimbursable expenses](../USER_GUIDE.md#reimbursable-expenses).

To plan for the payer paying less, nothing, or later in one saved scenario, open the
receivable and use the **In scenario** row: choose the scenario, enter the amount
expected back (0 for nothing) and the date, and choose **Apply to scenario**. Leave
both empty to expect what the receivable says again. The line below lists every
scenario's change, and that scenario's Projection shows the gross and net cost (see
[Reimbursable expenses](../USER_GUIDE.md#reimbursable-expenses)).

The **Plan** lists each expense category a receivable changed under **Reimbursable
expenses: gross and net cost**, below the savings goals, and a Plan cell's detail
window shows its gross cost and what was reimbursed or expected back under the Plan,
Actual, and Variance line.

## FSA claims

**Actions → Manage FSA Claims…** opens the claims editor from any view (on the FSA
Dashboard it is also a toolbar icon): each claim's service date, provider,
description, and EOB amount, the payments and refunds it links, and how much of it
each FSA funding year covers. The window does not block the rest of BreadSched, so it
can stay open beside a register; transactions you enter meanwhile are offered at
once, and choosing it again brings the same window forward.

The chooser at the top starts with **New claim**; **New** chooses it and empties the
form, and a saved claim then appears in the chooser with where it stands. In each
list of transactions, the ones linked to the claim stay at the top, each with
**Remove**. Below them, **Add** links another: search by date, description, or
amount, and choose whether to see transactions within 60 days of the service date
(the default), in the claim's benefit year, or at any date. Linking a payment made
with the FSA card links the FSA side too, so the claim counts as reimbursed by it.
**Close claim** and **Reopen claim** keep a reason, and **Delete** asks first.

The FSA Dashboard's **FSA claims** lists every claim of a benefit year that can still
be claimed, settled or not, so a claim already filed is easy to tell from a new one,
and any older claim still open. See
[FSA and benefit accounts](../USER_GUIDE.md#fsa-and-benefit-accounts).

## Import files

**File → Import GnuCash Book into Current Book…** (`Ctrl+I`) reads GnuCash, QIF,
and OFX/QFX files. The detected format is shown as soon as you choose the file,
before anything is written, and explicit decimal and date-order choices override
detection when a file is ambiguous. Imports run in the background as one
atomic undo step. Leave the open book and source file in place until completion or
cancellation; a cancelled import writes nothing. After an import, BreadSched says
how many reimbursement proposals are waiting. **Include possible duplicates** also
imports QIF and OFX rows otherwise held back as possible duplicates.

### Import a CSV statement

Choose **File → Import CSV Statement…** and choose the file; BreadSched reads its
columns at once, shows the first rows, and suggests the mapping. Adjust the account,
columns, and options, choose **Preview**, then **Import**. Clearing **First row is a
header** rereads the file with numbered columns. **Link possible transfers** joins
each possible-transfer pair. **Add split columns** adds a category and an amount
column picker for one split of each row; add one pair per split and leave the
**Category column** empty. See
[Import a CSV statement](../USER_GUIDE.md#import-a-csv-statement).

### Review held GnuCash changes

When a re-import holds GnuCash changes to (or deletions of) reconciled
transactions, the review opens after the import and whenever the book opens, before
the review of due scheduled transactions. Choose each row's decision, then
**Apply**. A deletion's row offers **Keep the transaction** or **Delete it here
too**.

### Write changes back to GnuCash

Close the book in GnuCash, then choose **File → Write Changes to GnuCash…**. Tick
the transactions to write and choose **Write selected**. **Backups to keep** sets how
many backups are kept. See
[Write changes back to a GnuCash book](../USER_GUIDE.md#write-changes-back-to-a-gnucash-book).

## Categorization rules

Choose **Actions → Categorization Rules…**. Enter an example description under
**Matching description**, choose the category, and choose **Add rule**. **Up** and
**Down** change a rule's priority and **Delete** removes it
(**Edit → Undo** restores it). The **Proposals** list shows each transaction's
proposed category, the deciding rule, and any later rule that would have chosen
differently; every proposal starts checked. Clear any you do not want and choose
**Accept selected**.

## Print and export

Print Dashboard, Plan, or Projection from the toolbar or **File → Print Current
View** (`Ctrl+P`). The system print dialog opens: choose a printer or print to a
PDF file (on Windows, **Microsoft Print to PDF**), preview the pages where the
dialog offers it, and set the paper and orientation (landscape at first; your
choices are kept until you quit). Tables continue across pages
with their column headings repeated, and each page shows the report name and page
number. For the Plan, the dialog's **Report** tab has **Include category detail
when printing**, which adds the budget categories on pages of their own. The
Flatpak prints through your desktop's own print dialog, which has no **Report**
tab, so there BreadSched first asks **Summary only** or **Include category
detail**.

On macOS, **File → Print in Browser…** opens the same report as a page in your
web browser instead, for its own print dialog, until BreadSched prints natively
there. The **Print…** buttons in Expense Explorer and
Net Worth History use the system print dialog too: Expense Explorer prints
spending and income over time with their charts, the selected category's trend
chart and the period with merchant detail, and the chosen Income detail. If the system print dialog cannot be used,
these open in your web browser instead. Use **File → Export Transactions** for
transaction data.

## Preferences

**Edit → Preferences…** (Ctrl+,) gathers the settings that are not part of day-to-day
work. They are kept in your own settings, not in a book:

- **Book**: the book BreadSched opens when it starts. **Choose…** picks another,
  and **Forget** starts without one.
- **Registers**: **Double Line** (also **View → Double Line**) and **Row shading**,
  for every register.
- **Online quotes**: the Alpha Vantage API key, the same key **Online Quotes…**
  keeps (see [Online quotes](#online-quotes)).
- **Dashboard**: **Configure Dashboard…** opens the open book's Dashboard settings
  (see [Configure the Dashboard](#configure-the-dashboard)); they are your own for
  the book.
- **Windows**: **Forget remembered window sizes** returns every window to its own
  size. BreadSched remembers the main window's size (and whether it was maximized)
  and an independent register window's size each time you close them, and the
  size of any other window you resize, for every book.
- **Sharing** (experimental): **Let other BreadSched commands use the open book**
  lets commands such as `breadsched due-review` and `breadsched scheduled-next` work
  on the book open in this window, through it, instead of stopping because the book
  is already open. Their changes appear here at once and can be undone here. It is
  off unless you turn it on; see
  [Share a book with a running server](cli.md#share-a-book-with-a-running-server-experimental).
- **Users** (experimental): says "Only you" until someone else may use this
  computer's books. **Users and devices…** sets your own password (needed only to
  sign in from another device), adds a person with a temporary password they replace
  when they first sign in, resets or removes them, and lists your devices and scripts:
  **Add a device or script…** makes a token shown only once, and **Revoke** stops
  one at once. With a book open it also lists **Who may use** it: you own it, and
  each other person has a role (owner, editor, viewer, or entry, which only adds
  transactions) that you can change, take away, or give with **Give someone
  access…**. The same can be done with `breadsched users`, `password`, `token`, and
  `share` (see [Users and devices](cli.md#users-and-devices-experimental)).
  **Two-step sign-in** can be turned on there for your own sign-ins from other
  devices (the secret for an authenticator app, its first code, then recovery codes
  shown once), and turned off for someone who lost their phone. When
  someone changes a book you share, **Undo** names them ("Undo: Add transaction
  (Eddie)"); an editor or owner may undo anyone's change.

**Save** stores them all and redraws open registers; **Cancel** or Escape changes
nothing.

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
