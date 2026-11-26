# BreadSched changelog

What each release changed for the people using it, newest first. Unfinished work
is in [`ROADMAP.md`](ROADMAP.md); how the application is built is in
[`DESIGN.md`](DESIGN.md). The record starts at the first beta: the alpha series
(0.2.0a1 through 0.2.0a321) is kept only in the repository's history and its
GitHub releases.

## Unreleased

## 2026.11.26.0

The sixth beta, published as a GitHub pre-release; its Windows installer and Linux
packages are still not code-signed, so check them against `SHA256SUMS`. Books move
to native schema 16 when first opened for writing, after a backup; 2026.11.25.0 and
earlier cannot open a schema-16 book.

- **Choose how the desktop shows dates.** **Edit → Preferences → Dates** shows
  every desktop date (registers, lists, dialogs, date fields, reports, the
  Dashboard, and printing) as your computer's locale writes dates, which is the new
  default, or as `2026-03-15`, `03/15/2026`, `15/03/2026`, or `15.03.2026`. Dates
  shown day first are typed day first too (`15/3`); `2026-03-15` is always accepted.
  Tables still sort dates by date. The browser, command line, exports, and the book
  itself are unchanged.
- **Every desktop date field reads dates as the register does.** The savings goal's
  **Buy on** field took only `2026-03-15` and lacked the register's date keys; it now
  works like the others. No field's hint or message asks for `YYYY-MM-DD` any more:
  each shows how a date is written in the chosen style.
- **Desktop settings are all in Edit → Preferences.** Preferences has a **General**
  tab (what it held before) and a **Dashboard** tab with everything Configure
  Dashboard held: horizons, items shown, groups, and columns. The Dashboard's
  toolbar icon and Actions menu item for Configure Dashboard are gone. If an account
  is chosen in a group's list but **Add** was not pressed, **Save** asks whether to
  add it first, and **Cancel** asks before closing without it. The browser's
  **Configure Dashboard…** is unchanged.
- **A Dashboard group holding a sub-account of another group's account now shows.**
  When one group held a parent account (say `Assets:Investments`) and another held
  one of its sub-accounts (`Assets:Investments:Brokerage`), the second group
  disappeared from the Dashboard though Configure Dashboard still listed it. The
  sub-account is now counted in its own group and left out of the parent's row, so
  each account is counted once; the parent's group total falls by that amount.
  Net worth, Assets, Debts, and Liquid do not change.
- **Track a transaction as an FSA claim.** **Track as FSA Claim…** on a desktop
  register row, **FSA claim…** in the browser register, and `breadsched claims
  --draft`/`--track` start a new claim from a transaction you already recorded, as
  **Track as Reimbursable** does for reimbursable expenses. The service date,
  provider, healthcare payment, FSA account, and funding year are filled in from the
  transaction and its accounts (a payment made with the FSA card is linked as **Paid
  from the FSA card**), and each choice is explained; nothing is saved until you save
  the claim. A transaction already on a claim opens that claim.

- **A schedule's changes are versions of the whole transaction.** The schedule
  editors' **Future changes** has one versions editor in place of **Future
  amounts**, **Per-leg future amounts**, **Seasonal month amounts**, and **One-time
  amounts**. Each version is every leg of the transaction, typed as a register's
  splits, so a change can add a leg, drop one, or change any amount: from a date on,
  on one date only, or every year in one month. An amount may be a formula over
  `base` (the leg's amount before the version) and `base_total` (the transaction's),
  together with a formula schedule's own variables; a new month version starts as
  every leg `base * 1.0`, ready for a factor. Month versions apply on top of the
  dated version in force, and one-date versions on top of both. The desktop, the
  browser (also for formula schedules), and scenario schedules all edit them, and
  `breadsched scheduled-versions` lists, adds, and removes them.
- **Books move to native schema 16.** Opening a book first saves a backup, then
  keeps each schedule's (and each scenario schedule's) future, per-leg, seasonal,
  and one-time amounts as versions with the same amounts. One result differs: a
  seasonal month amount used to stop once a later whole-schedule amount change took
  effect; as a month version it keeps applying on top of it. Earlier releases
  cannot open a schema-16 book; keep the backup to go back.
- A **pay change** is saved as a version from its start date. A paycheck with a
  month version, or a one-date version on or after that date, is refused as before.

- **Saving an FSA claim on the desktop shows that it took.** In **Manage FSA
  Claims**, **Save claim** is greyed out until something in the form changes, and
  again once the claim is saved, so you can tell it was saved before closing the
  window.

## 2026.11.25.0

The fifth beta, published as a GitHub pre-release; its Windows installer and Linux
packages are still not code-signed, so check them against `SHA256SUMS`. Books stay
at native schema 15, so 2026.11.22.1 still opens a book this version has used.

- **BreadSched has its own icon on Windows.** The Start-menu shortcut, the entry in
  Settings > Apps, and the installer and uninstaller show the BreadSched icon, where
  the shortcut showed Python's.
- **An uploaded document's name may not contain a line break.** Uploading a file
  whose name holds a line break or another control character is refused, and no
  response header the browser server writes can hold one (CodeQL
  `py/http-response-splitting`). No such header was ever sent: the name was always
  encoded.

## 2026.11.22.1

The fourth beta, published as a GitHub pre-release; its Windows installer and Linux
packages are still not code-signed, so check them against `SHA256SUMS`. Books move
to native schema 15 when first opened for writing, after a verified backup is
written beside them; 2026.11.22.0 cannot open a book once it has moved.

- **A note completes from the start of a word.** Typing `ch` in a note or memo
  completes to an earlier one with "Christmas" in it, but no longer to one with
  "which". And when a letter stops matching a completion found inside a note, the
  cell holds just what you typed, instead of keeping the start of that note: no
  more "whichristmas" to delete.

- **Blank goes to the new transaction.** A register's **Blank** toolbar button (and
  **Actions → Blank Transaction**), **Ctrl+End**, or **Shift+Page Down** moves to the
  blank transaction at the end, as GnuCash's Blank does, saving the transaction you
  leave if you changed it. The browser's register has the same button and keys.

- **Finishing a credit card statement offers to pay it.** The payment opens ready to
  change: what the statement still owes, dated today, from the card's **Paid from**
  account (in the transaction editor on the desktop, in a short form in the browser).
  Paying the whole statement settles the next payment due. Paying less asks once for
  the statement's minimum payment, which the statement keeps: at or above it the due
  is settled and the rest carries forward; below it the rest of the statement stays
  due. A schedule that pays the card counts the payment toward its next occurrence.

- **A bill paid by hand is not offered for posting again.** The due review no longer
  offers a scheduled occurrence that is closed or already paid in full, and offers
  one partly paid for what remains, posting only that.

- **Finish works in the browser's reconciliation.** It stayed disabled even when the
  difference was zero. The same mistake kept other buttons and checkboxes disabled
  or checked when they should not have been, in Match to Plan, the Plan's rollover
  choice, and the Projection's comparison list.

- **Every desktop window looks and answers the same way.** Each dialog has the
  same margins, its **Help** button at the top, and one row of buttons at the
  bottom right, in the same order everywhere: further actions such as **Print…**,
  then **Delete**, then **Cancel** or **Close**, then the main button, which Enter
  presses. **Print…** moved to that row in Budget Jars, Breadlet Reports, Realized
  Gains, Net Worth History, and Tax Year, and **Find** to the bottom of Find; the
  FSA claims, Net Worth History, and Reconcile buttons are no longer spread across
  the window. Windows that had no closing button (Rules, Reimbursable Expenses,
  Savings Goals, Payroll, Match to Plan) have **Close**. Notes fields have a frame.
  Button labels are in sentence case (**Choose lots…**, **Realized gains…**,
  **Reset layout**). Reconcile's **Postpone** is now **Close**, which still keeps
  the statement, and **Abandon** no longer has an ellipsis.

- **Schedules are entered like transactions on the desktop.** The schedule editor
  types its splits on the register's own split grid, as the transaction editor
  does, so a schedule can have any number of splits with their memos, planning
  purposes, and investment activity, and **Save** waits until they balance. Its
  number and notes are on **Basics**, and its tags and documents on **Tags &
  documents**, the transaction editor's own tab; every posting carries them. A
  scenario estimate, or an alternate of a baseline schedule, is edited in the same
  editor, and an alternate now keeps the baseline's per-leg future amounts instead
  of dropping them.

- **Text you delete from a note or memo stays deleted.** A description, note, or
  memo completed from an earlier one, with its start then deleted, no longer
  brings the deleted text back at the next letter typed: once anything is deleted,
  the cell stops completing until you leave it or empty it. Account cells keep
  completing.

- **A reconciled split warns before you change it**, at the first keystroke into
  its account, amount, shares, or price, in a register or the transaction editor,
  as GnuCash does. **Cancel** keeps the cell; **Change it** allows the change, and
  saving does not ask again. Before, the question came only when saving.

- **A charge or credit from the statement takes its own date.** When reconciling on
  the desktop, **Add…** opens the line in the transaction editor, dated on the
  statement date, so its date, amount, and splits can be changed before it is
  saved; it is still added cleared, and checked when it falls within the
  statement. In the browser, each line has a date that follows the statement date
  until you change it. Adding a line in the browser no longer fails with a request
  error.

- **Schedules carry a number, tags, notes, and documents**, and every transaction
  a schedule posts carries them: the rent schedule's lease, its ACH reference, its
  *Home* tag. **Make scheduled…** takes a transaction's own with it. Set them in
  the desktop schedule editor, in the browser's (**Tags & notes**), or with
  `breadsched scheduled-details`; link a schedule's documents on the desktop or
  with `breadsched attachments --schedule`. Books move to native schema 15 when first opened for writing, after
  a verified backup is written beside them. A release before this one (schema 14)
  cannot open a book once it has moved; keep the backup if you may go back.

- **One transaction editor, typed like the register.** Every desktop window that
  enters or changes a transaction now uses the same editor: New Transaction, a
  register's Edit Transaction…, reconciliation, and Posted transactions after the
  due review. Its splits are typed on the register's own grid, one line per split,
  with the same keys as a register's split lines:
  - the memo and account complete as you type;
  - an amount may be arithmetic;
  - the empty last line holds the rest of an unbalanced entry;
  - a fund's line takes shares and price.

  **Planning detail** adds planning-purpose and investment-activity columns that
  complete from their lists. The amount columns are named as the register you
  opened it from names them. The split rows with their own account drop-downs,
  **Add split**, and remove buttons are gone: type a new split on the empty last
  line, and empty a line to remove it. **Posted transactions** now lists what was
  posted and opens the chosen one in that editor, with its **Entry** and **Tags &
  documents** tabs, **Save**, and **Delete…**, instead of a list of splits with
  **Edit…** buttons.

## 2026.11.22.0

The third beta, published as a GitHub pre-release; its Windows installer and Linux
packages are still not code-signed, so check them against `SHA256SUMS`. Books are
unchanged (native schema 14), so it opens every book the earlier betas do. Every
change is on the desktop.

- **The toolbar and the tab row scroll by arrows**, as GnuCash's tabs do, instead of
  a scrollbar under them. When either is wider than the window, a left and a right
  arrow appear at its ends (greyed out at the start and the end), the mouse wheel
  scrolls it too, and choosing a tab brings it into view.
- **A desktop that stops responding says so, and can be quit and reported.** If
  BreadSched stops responding for 20 seconds, a separate window asks whether to
  wait or quit, and where it was stuck is written to a hang log. After quitting,
  the next start offers to open a GitHub bug report with the log's start filled in.
  Opening or upgrading a book, which blocks the window on purpose, is allowed three
  minutes, and the question then says what BreadSched has been doing.
  A dialog hidden behind another window, which can look like a freeze, now comes
  forward with the main window, and **View → Bring All Windows Forward** brings
  every window forward.
- **The row under the pointer stands out** in the desktop's tables. Every other row
  is shaded, and the theme's hover shade was as faint, so a shaded row under the
  pointer looked like any other. The row under the pointer is now tinted with a
  light shade of the selection color, distinct from both the shading and the
  selected row.
- **Share prices has room to type** on the desktop. Entering a schedule that buys or
  sells shares asks for their prices in a window that was always the same height,
  so a note above the table (a fund with no online quote, say) could squeeze the
  table to a single line until the window was enlarged. The window now opens as
  tall as its table needs, within the screen, and the table always shows its
  heading and about three legs, scrolling the rest.
- **Scheduled transactions sort by name again** on the desktop: the **Name**
  heading of the commitments and estimates tables sorts like every other column,
  schedules by name and each schedule's splits by account (#427).
- **Scheduled keeps your place** on the desktop. After you edit a schedule (or
  enter, skip, match, or duplicate one), it stays selected and in view instead of
  the list jumping back to its first row. Only one row is selected across the
  commitments and estimates tables, so the buttons act on the row that shows as
  selected; before, choosing an estimate that already looked selected left them
  acting on a commitment.
- **Commands are where you expect them** on the desktop, in every view, as in
  GnuCash and Gramps. The menus list every command; the toolbar shows the current
  view's, those on the selected row first (**Edit**, **Delete**, **Duplicate**,
  then the view's own), greyed out until a row is selected; and right-clicking a
  row (or Shift+F10, or the Menu key) lists the commands on it. Buttons inside a
  view now only change what it shows: pickers, filters, ranges and their **Apply**,
  and the register's **Enter**, **Cancel**, and **Split**. Before, a view's row
  commands were buttons beside its table while its larger tools were toolbar
  icons, and several were on no menu at all.
  - Scheduled's **View / Edit…**, **Enter Next**, **Skip Next**, **Match…**,
    **Duplicate…**, and **Delete…**, Review's six buttons, Plan's **Resolve
    actuals…**, **Add estimate…**, **Alter baseline…**, and **Suppress
    baseline…**, Projection's **Explain month…** and **Save Base as Scenario…**
    (now **Save Scenario**), and the FSA Dashboard's **Link selected** are toolbar
    icons and Actions menu items. Each FSA claim's **Review claim** button is gone:
    select the claim and choose **Edit Claim**, or double-click it.
  - New: **Open Register** and **Delete Account…** in Accounts, and **Delete
    Transaction…** for the register's cursor transaction (Ctrl+Z restores it).
    The account selected stays selected when Accounts is redrawn after an edit.
  - **Upcoming is now the first table in Scheduled**, above the definitions, with
    its look-ahead and overdue note: the separate Upcoming view is gone (a book
    that had it in a tab reopens Scheduled there), and **Review Due** is a
    Scheduled toolbar icon. The toolbar has no icons of its own for the FSA
    Dashboard and Review, which stay on the View menu and are opened by **Manage
    FSA Claims…** and **Resolve Actuals…**.
  - A register in a window of its own keeps **Edit**, **New**, **New Window**, and
    **Reconcile…** as buttons, since it has no toolbar.

## 2026.11.18.0

The second beta, published as a GitHub pre-release; its Windows installer and Linux
packages are still not code-signed, so check them against `SHA256SUMS`. Books are
unchanged (native schema 14), so it opens every book the first beta does.

- **Arrange the Dashboard.** Each Dashboard item is now a tile on a grid four
  columns wide, and **Arrange Dashboard** (desktop) or **Arrange** (browser) places
  and sizes them: drag an item by its title to move it, drag its corner to make it
  1 to 4 columns wide and 1 to 8 rows tall (or use its arrows and size controls from
  the keyboard), hide it, add a hidden one back, or reset the layout. Items pack from
  the top left so a small one fills a gap, a long one scrolls inside its tile, a
  chart is drawn at its tile's size, and a narrow window or a phone stacks the same
  items in the same order. The arrangement is saved with your own Dashboard
  settings, the printed Dashboard follows its order, and a dashboard breadlet can
  name the size it starts at.

## 2026.11.11.0

The first beta. It is published as a GitHub pre-release; its Windows installer and
Linux packages are not yet code-signed, so check them against `SHA256SUMS`.

- **Calendar versions.** A version is now the day its release was prepared,
  `YYYY.M.D.PATCH` (for example `2026.11.10.0`), with the stage (`beta` or `stable`)
  beside it. It sorts after every 0.2.0 alpha, so packages upgrade in place.
- **Older alpha books.** This version opens books written by 0.2.0a301 and later
  (native schemas 13 and 14). A book from an earlier alpha is refused with a message
  saying so: open it once with the last alpha, 0.2.0a321, which brings it forward,
  then open it here.
- **Import a date range or chosen accounts.** An import of a GnuCash, QIF, or
  OFX/QFX file can be limited to dates and to accounts, in the desktop, the browser,
  and `breadsched import` (`--since`, `--until`, `--account`). **A file imported
  before now starts at the day you last imported it**, so a regular import is
  quicker. This changes what a repeated import does by default: after editing or
  reconciling older transactions in GnuCash, choose **All dates** (`--since all`).
  Transactions outside the limit are left as they are and are never taken for
  deleted.
- **The Dashboard is the main view.** On the desktop its tab is pinned first and a
  book always opens on it, with the tabs you had open behind it. Before, a book
  reopened on whichever tab you last used.
- **Cash-flow chart on the Dashboard.** A line of the cash you will have on hand
  each day of the liquidity period, from the same bills and pay that *Needed*
  counts, with the lowest day marked. It is on the desktop, in the browser, and in
  the printed Dashboard, and **Configure Dashboard** can hide it.
- **The Dashboard is made of breadlets.** Every item on it, from the summary figures
  and the cash-flow chart to the bills, income, groups, reimbursables, FSA claims and
  goals, is a dashboard breadlet, shown, hidden and ordered the same way as any
  other, and drawn alike on the desktop, in the browser and on paper. Their rows and
  figures open what they are (a bill's schedule, the savings goals, the net worth
  history), their tables sort by any column, and the summary figures can now be
  hidden too. A group lists its accounts with their balances under it. In the
  browser, the net worth history moved from the bottom of the Dashboard to
  **Reports**; the **Net worth** figure opens it.
- **Reports in one place.** On the desktop every report opens in its own tab of one
  **Reports** window, so several can be open side by side; the **Reports** menu
  lists each report once. The Expense Explorer is a report now, in the browser too
  (**Plan** keeps an **Expense Explorer…** button that opens it with the Plan's
  range). It is called the Expense Explorer everywhere.
- **Grouped browser navigation.** The browser's bar holds **Dashboard**,
  **Reports**, and five menus (**Ledger**, **Schedule**, **Planning**, **Benefits**,
  **Book**) instead of nineteen buttons.
- **The same words everywhere.** A bill's jar on the Dashboard is **In the jar**
  (it was *Hold now*), what goal jars hold beyond spendable cash is *kept out of
  Available*, and each item on the Reimbursable Expenses page is a *reimbursable
  expense* (it was sometimes called a *receivable*; the account type keeps that
  name).
- **Savings goals speak of jars.** A goal's money is what is *in the jar*; **Add to
  jar** puts extra money in (it was *Allocate*), the Dashboard card is **In goal
  jars** (it was *Set aside for goals*), and a goal that has its whole target is
  **jar full**, in every interface and on the command line, as the guide has always
  described them.
- **A website.** <https://hdholm.github.io/BreadSched/> walks through BreadSched one
  optional step at a time, with pictures, explains money basics in plain language,
  and carries the whole user guide, this changelog, and the roadmap.
- **Breadlets: reports and Dashboard items as plug-ins.** Reports, Dashboard items,
  importers and exporters are now *breadlets*, listed and run the same way on the
  desktop (the **Reports** menu), in the browser (**Reports**), and on the
  command line (`breadsched breadlets`, `breadsched report`). New charted reports:
  **Spending by category**, a ring of where the money went, and **Income and
  spending**, each period's income beside its spending. Net worth over time, Budget
  jars, Tax year and Realized gains are reached there too. Every chart has the
  table of its exact values beside it. `breadsched plugins` is now
  `breadsched breadlets`.
- **Spending and income on the Dashboard.** Two Dashboard breadlets, **Spending this
  month** and **Income and spending** for the last six months, show below the
  Dashboard's sections; **Configure Dashboard** can hide them.
- **Add-on breadlets.** A Python package installed beside BreadSched can add its
  own breadlets; one that fails to load is named rather than stopping the
  application.
- **Reporting a problem.** Help → Report a Problem fills in the version and system
  for you, and the issue forms ask for less: what happened, in your own words.
