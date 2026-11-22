# BreadSched changelog

What each release changed for the people using it, newest first. Unfinished work
is in [`ROADMAP.md`](ROADMAP.md); how the application is built is in
[`DESIGN.md`](DESIGN.md). The record starts at the first beta: the alpha series
(0.2.0a1 through 0.2.0a321) is kept only in the repository's history and its
GitHub releases.

## Unreleased

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
