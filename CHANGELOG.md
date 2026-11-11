# BreadSched changelog

What each release changed for the people using it, newest first. Unfinished work
is in [`ROADMAP.md`](ROADMAP.md); how the application is built is in
[`DESIGN.md`](DESIGN.md). The record starts at the first beta: the alpha series
(0.2.0a1 through 0.2.0a321) is kept only in the repository's history and its
GitHub releases.

## Unreleased

Preparing the first beta.

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
