# BreadSched changelog

This file records completed BreadSched milestones. Current and proposed work
belongs in `ROADMAP.md`.

Entries from 0.2.0a190 onward are complete. Earlier versions are condensed: each
version from 0.2.0a86 through 0.2.0a189 keeps one line naming its outcomes and a
link to its release notes in `docs/releases/`, which hold the full description, and
work up to 0.2.0a85 is summarized by topic. Schema changes, compatibility limits, and
significant security or correctness changes are called out in each condensed
section. Commits and pull requests hold the complete history.

## 0.2.0a211 - 2026-09-30

- **Linux releases attach a tested Flatpak bundle; the sandbox's file chooser,
  printing, and books outside Documents are validated.** The release workflow
  builds `BreadSched-<version>.flatpak` from the tested commit, installs it, checks
  its version, and publishes it with its checksum in `SHA256SUMS`. CI now runs the
  real document portal and `xdg-desktop-portal` frontend beside the installed
  Flatpak: a stand-in portal backend answers as a person would while the
  application's own Open, Export Transactions, Back Up Book, and Print actions go
  through the portal. It also creates, migrates, writes, and verifies books outside
  Documents through the document portal, and the GTK smoke prints each report to PDF.
  Corrections found this way: a book reached through the document portal is only
  that file, so a pre-migration or pre-restore backup written beside it became a
  hidden portal temporary and never reached the named file; such backups, the web
  upload folder, and import logs now go to
  `~/.var/app/org.breadsched.BreadSched/data/breadsched/beside-documents/`, the book
  gets no default attachment folder, and the desktop says where backups go. The
  portal's print dialog cannot show the Plan's **Report** tab, so the Flatpak now
  asks **Summary only** or **Include category detail** before printing.
  `breadsched migrate` reports the backup it actually wrote. No schema change.

## 0.2.0a210 - 2026-09-30

- **FSA funding, direct payments, reimbursements, and provider refunds are each
  counted once.** A new `engine.fsa_flows` classifies every FSA split as funding,
  direct payment, reimbursement, provider refund, repayment, or transfer. Benefit
  years report `direct_payments`, `reimbursements`, and `provider_refunds`, and
  `used` is their net less repayments. Corrections: a provider refund credited to
  the FSA card was counted as payroll funding and did not restore the election; Plan
  treated it as benefit funding, and it left FSA-paid expense as an unexplained
  cash-bridge residual. Plan now shows one net Benefit funding row per FSA account.
  Claims gain paired roles, **Paid from the FSA card** (`direct_payment`) and
  **Refunded to the FSA card** (`direct_refund`), which link both sides of a card
  payment or refund so the claim is neither under-reimbursed nor over-reimbursed;
  Review and the transaction editors offer only roles that fit the transaction, with
  the paired role first. The FSA Dashboard (GTK and web) adds a **How used** column,
  the web FSA years gain the usage fields, and `breadsched claims --years` lists
  benefit years with the same breakdown. No schema change.

- **DESIGN describes the implemented architecture; the changelog is condensed
  (#237).** `DESIGN.md` is reorganized into product boundary, architecture and an
  ownership map, domain and exact money, storage and recovery, dated planning,
  valuation and reporting, household workflows, interoperability, interfaces,
  security, and validation. Duplicated money, valuation, and scenario sections are
  merged, reporting material moved beside reporting, and development history,
  stale statements, and aspirational text removed (2,881 → about 2,370 lines).
  This file keeps full entries from 0.2.0a190 and condenses older versions to one
  line each with a link to their release notes, plus schema, compatibility,
  security, and correction notes (2,774 → about 530 lines). The link checks now
  cover `DESIGN.md` and `CHANGELOG.md`. Documentation only.

## 0.2.0a209 - 2026-09-30

- **Every reported value says whether it is complete (#236).** A shared
  `Completeness` result (complete, partial, or unavailable, with each excluded
  balance, event, or posting, its currency, amount, date, the missing rate or price,
  and the fix) is attached to Plan periods, cells, totals, and through-as-of
  figures; Expense Explorer periods; Projection months and scenario comparisons;
  Dashboard net worth, liquid cash, and groups; balance aggregates; and net worth
  history points and changes. Plan, Explorer, and Projection show labelled partial
  subtotals; balances, net worth, and the Dashboard withhold. GTK, the browser, the
  command line, `--json`, printed reports, and the Projection CSV show the status
  as text beside the value; the Projection chart shades partial months and the net
  worth chart marks withheld points **n/a** instead of zero. A comparison is only as
  complete as both inputs. The User Guide gains "Complete, partial, and unavailable
  values" with each report's policy and an example.

## 0.2.0a208 - 2026-09-30

- **One meaning for each reporting term (#235).** The Plan summary variance is now
  actual through the as-of date less planned through the as-of date. Previously it
  summed period variances, so a transaction dated later in the current period
  appeared in the variance but not in the actual beside it. Planned amounts count
  whole on their dates, never prorated. Period cells keep whole-period figures,
  now labelled **Period actual** and **Period variance** in GTK, the browser,
  Expense Explorer, and printed reports; their detail marks postings dated after the
  as-of date. The browser, print layout, and `breadsched activity` (text and JSON)
  add planned change through the as-of date, and the User Guide gains a Reporting
  terms table with a worked example.
- **Contributor policy: review every document, change only what the change
  affects (#239).** Each pull request records a disposition for every major
  document: updated, reviewed with no change needed, or not applicable, each with a
  reason. Documents must be changed when a pull request makes them inaccurate, and
  are no longer edited only to satisfy an "update every file" rule. The
  pull-request template lists the documents, and a test keeps the template and the
  policy in agreement. Documentation-only work relies on the existing documentation
  checks rather than tests written to assert wording.
- **Release policy and roadmap match reality (#238).** The contributor guide no
  longer says every merge publishes a release: a release is published only for a
  version whose notes reach `main`. The roadmap now lists only unfinished work:
  delivered Windows installer, GnuCash write-back, reporting, tab, and FSA
  descriptions were removed, the browser-guide item was narrowed to contextual help
  links, and the remaining Linux packaging and macOS work was kept.

## 0.2.0a207 - 2026-09-30

- **Every read sees one saved state (#234).** A browser page, a report, or a desktop
  projection that ran while another window saved could combine data from before and
  after the save, such as an account's old name with its new balance. Each read now
  works from a copy of the book taken at one moment, so its results always belong to
  a single saved state; the next read sees the save. The book stays a single file,
  and a long read never holds up saving. Application version `0.2.0a207`; native
  schema remains 10.

## 0.2.0a206 - 2026-09-30

- **FSA carryover and grace periods.** A funding year can record the plan's rules
  for unused money, either or both. With a **carryover limit**, up to that much of
  the unused election moves into the account's next funding year once the run-out
  ends, and only the rest is forfeited. With a **grace period**, services up to its
  end can still be claimed against the earlier year, choosing the year on the
  claim; with both, the carryover is what grace-period claims left. Both are edited
  with the funding years (desktop account dialog, browser editor), and
  the FSA Dashboard shows what each year carried in and carried over.
- The browser test client allows slow machines more time per request, after a
  loaded Windows runner stalled one small request past ten seconds. Application
  version `0.2.0a206`; native schema remains 10.

## 0.2.0a205 - 2026-09-30

- **Repaying an FSA over-reimbursement.** When a corrected EOB is lower than what
  the FSA already paid, the claim shows **Over-reimbursed** and says how much to
  pay back, instead of a general **Needs review**. Link the payment back into the
  FSA under **Repaid to the FSA** on the claim's allocation (desktop, browser, or
  Review): it counts against what was reimbursed, and gives that much of the
  funding year's election back rather than counting as payroll funding.
- **Late EOB changes and reopened claims.** Changing an EOB already entered is kept
  in the claim's history with an **EOB change note**. A higher EOB after the FSA
  has paid reopens the claim for the rest, and it is flagged until reimbursed.
- **Closing a claim.** **Close claim** stops pursuing what is left to reimburse: the
  claim shows **Closed** and what it gave up, and needs no more attention.
  **Reopen** takes it up again; both are kept in the history. The command line
  gains `breadsched claims --close`, `--reopen`, and `--history`. The browser's
  claim requests moved into their own adapter. Application version `0.2.0a205`;
  native schema remains 10.

## 0.2.0a204 - 2026-09-30

- **FSA claims needing attention.** A claim now says when something is left to
  do: its figures need review, no EOB has been entered 30 days after the service,
  money is still to be reimbursed and a funding year's claim deadline is within
  30 days, or a reimbursement was rejected. The Dashboard counts these claims
  (desktop card, browser tile, `breadsched dashboard`, and the printout, which
  also lists them and now shows **Reimbursements due**), and the FSA Dashboard
  gives each one's reasons in a **Needs attention** column.
- **FSA claims report.** The FSA Dashboard (desktop and browser) totals every
  claim by status, FSA account, funding year, or provider: paid, reimbursed,
  rejected, and still to come. The new `breadsched claims` command prints the same
  report, filters it by account, funding year, provider, status, or attention,
  and lists why each claim needs attention. Application version `0.2.0a204`;
  native schema remains 10.

## 0.2.0a203 - 2026-09-30

- **Fixed a desktop crash from the Actions menu (#229).** Opening **Actions →
  Other Views** (reported from the register) printed GTK warnings and then
  crashed the application. Opening the submenu made the window briefly inactive,
  and the Actions menu was rebuilt underneath the open submenu; it is now rebuilt
  only when you change view.
- **Open another register tab from the register (#228).** **New tab** in the
  register's toolbar (**Actions → Open Register in New Tab**) opens a second
  register tab on an account that has no tab yet; its account list switches it
  to any account. Remembered tabs keep two tabs on one account. Application
  version `0.2.0a203`; native schema remains 10.

## 0.2.0a202 - 2026-09-30

- **The desktop fits a small screen.** A book with long account names, notes, and
  big split transactions no longer pushes the window off the screen. An audit found
  a register for a long-named account needing a window over 8,000 pixels wide and
  the Dashboard over 1,500; every view now fits in 1024 × 700. A long name shown
  in a choice list is shortened in the middle (its list still shows it in full),
  view toolbars scroll sideways, the Dashboard's summary cards wrap, and leaving
  a large view lets the window shrink again.
- **Dialogs open within the screen.** A dialog's opening size is capped to its
  monitor, so one with a long note no longer opens thousands of pixels wide;
  Savings Goals scrolls its forms. Application version `0.2.0a202`; native
  schema remains 10.

## 0.2.0a201 - 2026-09-30

- **Every desktop printout is native.** **Print…** in Net Worth History, its
  change detail, and Expense Explorer now uses the system print dialog with the
  same paginated layout as Dashboard, Plan, and Projection, and opens the browser
  page only if GTK printing fails. The net worth change totals span their label
  columns, and merchant transactions and top-level account values keep one line
  each. Application version `0.2.0a201`; native schema remains 10.

## 0.2.0a200 - 2026-09-30

- **Native printing.** On the desktop, **Print** (`Ctrl+P`) for Dashboard, Plan,
  and Projection now opens the system print dialog instead of a browser page:
  print, preview where the dialog offers it, or save a PDF, on landscape pages by
  default. Tables continue across pages with their headings repeated, a heading
  is never stranded at the foot of a page, wide Plan tables shrink to fit, and
  every page shows the report name and page number. The Plan's category detail
  is a checkbox on the dialog's **Report** tab and starts on a new page.
- **One layout for every printout.** The three reports are laid out once and
  drawn either natively or as the HTML page, so both say the same thing; **File
  → Print in Browser…** keeps the browser route. The Windows installer check
  now prints every report through the native renderer to PDF. Application
  version `0.2.0a200`; native schema remains 10.

## 0.2.0a199 - 2026-09-29

- **A Projection tab per scenario.** On the desktop, **New tab** in Projection
  (**Actions → Open Scenario in New Tab**) keeps the scenario shown in a tab of
  its own, titled with the scenario's name, so several futures stay one click
  apart. The main Projection tab still follows the scenario chosen in Plan; a
  scenario tab keeps its own, choosing another scenario in it retitles it, and
  Base assumption changes reach every tab that inherits them.
- **Each book remembers its tabs.** Reopening a book brings back its views,
  register tabs, and scenario tabs, with the same tab selected; a tab for an
  account or scenario deleted since is left out, and another book has its own
  tabs. They are kept with the other interface state in `views.ini`.
- **Browser tabs for registers and scenarios.** **Open in new tab** in the
  browser's Register (formerly **Open in new window**) and Projection opens that
  account or scenario in another browser tab, which keeps its own place.
  Application version `0.2.0a199`; native schema remains 10.

## 0.2.0a198 - 2026-09-29

- **Wider GnuCash write-back.** Write-back now works on XML books (GnuCash's
  default, compressed or not) as well as SQLite, and writes new transactions with
  any number of splits (a split in a security or foreign-currency account keeps its
  quantity); changed amounts, accounts, split actions, and added or removed splits
  of imported transactions; and transactions deleted in BreadSched, with their
  notes. A transaction GnuCash has reconciled, or one in a GnuCash lot, still only
  takes reconcile marks. An XML book is changed only where a chosen transaction
  changes; the rest of the file stays byte for byte as GnuCash wrote it, and an
  XML book open in GnuCash (its `.LCK` file) is refused. New transactions carry
  GnuCash's `date-posted` detail, which a date edit now updates too.
- **Checked in real GnuCash.** Tests run every kind of write on SQLite and XML
  books that GnuCash 5.5 created, prove a fresh import reproduces the edited book,
  and, in a new CI job, open the written book in GnuCash itself to compare every
  transaction and account balance. Application version `0.2.0a198`; native
  schema remains 10.

## 0.2.0a197 - 2026-09-29

- **GnuCash deletions of reconciled transactions are reviewed.** A transaction
  deleted in GnuCash is no longer removed silently when any of its splits is
  reconciled, in GnuCash or in BreadSched. It is held in the same review as
  GnuCash changes (desktop, browser, and `breadsched import-review`), marked
  **Deleted in GnuCash**, with **Keep the transaction**, **Delete it here too**
  (refused while a reconciliation, FSA claim, or receivable refers to it), and
  **Decide later**. A kept deletion is not asked again. Unreconciled deletions are
  still mirrored.
- **Skipped investment activity is named.** OFX and QIF investment records that
  are not imported (options, share transfers, stock splits, return of capital,
  margin interest, and sub-account journals; for QIF also grants, vesting, and
  reminders) are reported with their date and a plain reason such as *"A stock
  split is not imported; enter it yourself"*, and the User Guide lists them.
  Importing more of these is not planned for now. Application version
  `0.2.0a197`; native schema remains 10.

## 0.2.0a196 - 2026-09-29

- **AqBanking decision.** BreadSched will not build in a bank connection or bundle
  AqBanking: its strength is German and European FinTS/HBCI and EBICS, its US
  route (OFX Direct Connect) is being withdrawn by major banks, and bundling it
  would add two C libraries and a second home for banking credentials. The User
  Guide now shows how to download a statement with `aqbanking-cli`, export it as
  CSV, and import it with the reviewed CSV import, and an acceptance test imports
  unedited aqbanking-cli 6.5.4 output with that mapping. Application version
  `0.2.0a196`; native schema remains 10.

## 0.2.0a195 - 2026-09-29

- **Transaction tags.** A transaction can carry tags of your own, matched regardless
  of capitals and spelled as the book already spells them. Set them in the desktop
  transaction editor, from a browser register row (**Tags & documents…**), or with
  `breadsched tags`. The desktop register filter matches tags, and
  `breadsched register --tag` lists one tag's transactions with the full running
  balance.
- **Linked documents.** As in GnuCash, receipts, statements, and web pages are
  linked, not stored in the book. Attaching a file copies it into an attachment
  folder beside the book (never overwriting a file of the same name), or links it
  where it is; a web address is only linked. A missing file is marked **missing**,
  keeps its link, and can be relinked; removing a document never deletes the
  file. Documents can be opened from the desktop editor and the browser; the
  browser can link only web addresses and files inside the attachment folder, so
  it can never be used to read other files. `breadsched attachments` lists them, reports missing ones, and links, relinks, or
  unlinks them.
- **GnuCash linked documents.** A transaction's GnuCash *Linked Document* is
  imported as a link, never copied, and refreshed on re-import. Relative links
  resolve under GnuCash's *Path head for linked files* (the home folder by default,
  or `breadsched attachments --gnucash-folder`). Re-import keeps BreadSched's own
  tags and documents, and books imported earlier are still reported unchanged.
- **Export.** The transaction CSV export adds **tags** and **documents** columns.
  Application version `0.2.0a195`; native schema remains 10.

## 0.2.0a194 - 2026-09-29

- **Savings goals in Plan, Projection, and scenarios.** Goals are pinned: every
  scenario carries every open goal. A scenario can change a goal's target amount or
  target date, or leave it out, without touching the goal or other scenarios. This
  can be done from the desktop Savings Goals window, the browser Goals page, or
  `breadsched goals --override`. Each goal lists its scenario changes.
- **Projection.** It starts from what each goal has actually set aside, then sets
  aside a share of the scenario's own projected income each month until the target
  month. Each goal's target month is a milestone that says whether projected cash
  covers everything set aside for goals then, and the first month cash covers bills
  but not goal money is named. This appears on the desktop, in the browser, in the
  printout, and in `breadsched project` (with `--json`). The projection CSV export
  adds each month's goal money and the cash left after it.
- **Plan.** Goals reaching their target date in the Plan range are listed, with
  what they have set aside so far, on the desktop, in the browser, and in the
  printout. A test confirms money moved into a goal's account is never projected as
  an expense. Application version `0.2.0a194`; native schema remains 10.

## 0.2.0a193 - 2026-09-29

- **Savings goals.** A goal names an asset account, a target amount, a start date,
  and a target date. Like a pending bill, each income received from the start date
  sets aside a prorated share of what the goal still needs, so the whole target is
  set aside by the target date. Extra money can be allocated at any time, and later
  income spreads only the remaining gap. Income that never arrived sets nothing
  aside. With no scheduled income before the target date, the gap is spread by day.
  What is set aside is an earmark, so one account can serve several goals and other
  purposes. The target date is a milestone (nothing is spent), and closing a goal
  releases its earmark. The Dashboard holds goal earmarks out of Available, like
  bill reserves, and shows **Set aside for goals** on the desktop, in the browser,
  in the printout, and in `breadsched dashboard`. For a goal held in a non-cash
  account, only the part not yet moved there is held. The new `breadsched goals`
  command adds, funds, lists, closes, reopens, and deletes goals. Bill reserves and
  goals now share one rule for what income counts as received.
- **Savings goal editors.** The desktop **Savings Goals…** window and the browser
  **Goals** page add, edit, fund, close, reopen, and delete goals through the shared
  service. Each goal shows its account, target date, target, amount set aside,
  what remains, and status, and a rejected edit leaves the goal unchanged. The
  Dashboard now lists each open goal in a **Savings goals** table on the desktop,
  in the browser, and in the printout; on the desktop, activating a row opens the
  goals window. Every interface uses the same status words.
- **`breadsched migrate`.** Read-only commands refuse a book from an earlier schema
  rather than change it, and the command line had no way to migrate one; the new
  command migrates with the usual verified backup and reports it. The Windows
  upgrade test migrates the book made by the previous release this way before
  verifying it, which is what caught the gap.
- **Contributing.** Prefer fewer, larger pull requests carrying a complete feature
  across interfaces, since CI and each release take about half an hour.
  Application version `0.2.0a193`; **native schema 10** adds the `savings_goal`
  table, and schemas 6–9 migrate automatically with a verified backup.

## 0.2.0a191 - 2026-09-29

- **Net worth change drill-down.** Choosing a period's change in Net worth history
  (the Change button on the desktop and in the browser, or the new
  `breadsched net-worth-change` command) lists the transactions that changed net
  worth in that window. Each shows its date, description, the asset and debt
  accounts it touched, its currency, and its effect converted with the quote
  applicable on its date. A **Market and exchange-rate changes** line accounts for
  the rest, so postings plus market movement equal the change exactly. Transfers
  between your own accounts are counted and left out. A posting without a quote
  withholds the totals and is named. The desktop printout, the CSV export
  (desktop, browser download, `--csv`), and the screen show the same totals.
  Application version `0.2.0a191`; native schema remains 9.
- **Roadmap.** Windows code signing is deferred until a beta release is reasonable;
  alpha installers stay unsigned.

## 0.2.0a190 - 2026-09-29

- **Printed Income detail.** The desktop Expense Explorer's **Print…** now includes
  the chosen Income detail for the selected period: its dated scheduled occurrences
  and receipts by payer, with the period's planned and actual totals. The browser
  already printed it as part of the page. Application version `0.2.0a190`; native
  schema remains 9.

## 0.2.0a86 – 0.2.0a189 (2026-09-21 – 2026-09-29)

Compatibility in this range:

- **Native schema 8** (0.2.0a134) adds payees; **native schema 9** (0.2.0a143) adds
  receivables. Each migrates older supported schemas (6 onward) automatically with a
  verified backup; an alpha before the schema change cannot open the migrated book.
- **Security and release:** code-scanning remediation limits CI tokens and isolates
  releases (0.2.0a102); releases publish again after a pipeline fix (0.2.0a182);
  the Windows installer ships with releases from 0.2.0a183.
- **Financial corrections:** OFX/QIF re-import no longer recategorizes existing
  transactions (0.2.0a129); Plan totals convert or disclose foreign currency instead
  of adding unlike units (0.2.0a125); Dashboard totals cover the whole book
  regardless of groups (0.2.0a144).

Versions:

- [0.2.0a189](docs/releases/v0.2.0a189.md) (2026-09-29): Income detail; Net worth history is fast on large books.
- [0.2.0a188](docs/releases/v0.2.0a188.md) (2026-09-29): Net worth history.
- [0.2.0a187](docs/releases/v0.2.0a187.md) (2026-09-29): Income over time.
- [0.2.0a186](docs/releases/v0.2.0a186.md) (2026-09-29): Windows file chooser and printing are checked in the installed copy; Windows upgrades and uninstalls remove the whole runtime.
- [0.2.0a185](docs/releases/v0.2.0a185.md) (2026-09-29): Optional command line on `PATH` in the Windows installer; Rejected browser writes no longer reset the connection.
- [0.2.0a184](docs/releases/v0.2.0a184.md) (2026-09-29): Windows upgrades are tested from the published installer.
- [0.2.0a183](docs/releases/v0.2.0a183.md) (2026-09-29): Windows installer on releases.
- [0.2.0a182](docs/releases/v0.2.0a182.md) (2026-09-29): Releases publish again (#207).
- [0.2.0a181](docs/releases/v0.2.0a181.md) (2026-09-28): One bill split between a payer and the FSA (#192).
- [0.2.0a180](docs/releases/v0.2.0a180.md) (2026-09-28): Windows installer built and tested in CI.
- [0.2.0a179](docs/releases/v0.2.0a179.md) (2026-09-28): Tabs for open views and registers (#183).
- [0.2.0a178](docs/releases/v0.2.0a178.md) (2026-09-28): Desktop chrome follows the current view (#182).
- [0.2.0a177](docs/releases/v0.2.0a177.md) (2026-09-28): User guide by interface (#181).
- [0.2.0a176](docs/releases/v0.2.0a176.md) (2026-09-28): Receivable accounts (#170).
- [0.2.0a175](docs/releases/v0.2.0a175.md) (2026-09-28): Write changes to GnuCash from the desktop and browser (#174).
- [0.2.0a174](docs/releases/v0.2.0a174.md) (2026-09-28): Write simple changes back to GnuCash (#174).
- [0.2.0a173](docs/releases/v0.2.0a173.md) (2026-09-28): Direct exchange rates only (#173).
- [0.2.0a172](docs/releases/v0.2.0a172.md) (2026-09-28): Register and entry routes move to their own web adapter.
- [0.2.0a171](docs/releases/v0.2.0a171.md) (2026-09-28): Reconciliation routes move to their own web adapter.
- [0.2.0a170](docs/releases/v0.2.0a170.md) (2026-09-28): CSV category, payee, and currency columns.
- [0.2.0a169](docs/releases/v0.2.0a169.md) (2026-09-28): A QIF transfer listed in both accounts is imported once (#176).
- [0.2.0a168](docs/releases/v0.2.0a168.md) (2026-09-28): QIF investment accounts.
- [0.2.0a167](docs/releases/v0.2.0a167.md) (2026-09-28): OFX investment transactions.
- [0.2.0a166](docs/releases/v0.2.0a166.md) (2026-09-28): Spending over time in Expense Explorer.
- [0.2.0a165](docs/releases/v0.2.0a165.md) (2026-09-28): Securities quoted in another currency are valued.
- [0.2.0a164](docs/releases/v0.2.0a164.md) (2026-09-28): OFX foreign-currency transactions carry their exchange rate.
- [0.2.0a163](docs/releases/v0.2.0a163.md) (2026-09-28): QIF and OFX imports bring security prices.
- [0.2.0a162](docs/releases/v0.2.0a162.md) (2026-09-28): Imports and reconciliation point at waiting reimbursements.
- [0.2.0a161](docs/releases/v0.2.0a161.md) (2026-09-28): Proposed reimbursements.
- [0.2.0a160](docs/releases/v0.2.0a160.md) (2026-09-28): Track reimbursable expenses in the browser.
- [0.2.0a159](docs/releases/v0.2.0a159.md) (2026-09-28): Track reimbursable expenses on the desktop.
- [0.2.0a158](docs/releases/v0.2.0a158.md) (2026-09-28): The browser register gets the blank row, split lines, and in-place editing (#158, #148).
- [0.2.0a157](docs/releases/v0.2.0a157.md) (2026-09-28): Every desktop dialog fits a laptop screen (#148 dialog audit).
- [0.2.0a156](docs/releases/v0.2.0a156.md) (2026-09-28): Edit transactions in place in the register (#158).
- [0.2.0a155](docs/releases/v0.2.0a155.md) (2026-09-28): Editing an amount keeps each split consistent (#166).
- [0.2.0a154](docs/releases/v0.2.0a154.md) (2026-09-28): Enter split transactions in the register's blank row (#158).
- [0.2.0a153](docs/releases/v0.2.0a153.md) (2026-09-28): More view commands become toolbar icons (#156 follow-up).
- [0.2.0a152](docs/releases/v0.2.0a152.md) (2026-09-28): Type new entries in the register's blank row (#158).
- [0.2.0a151](docs/releases/v0.2.0a151.md) (2026-09-28): The register opens at its most recent entry (#157).
- [0.2.0a150](docs/releases/v0.2.0a150.md) (2026-09-28): View commands move into menus and toolbar icons (#156).
- [0.2.0a149](docs/releases/v0.2.0a149.md) (2026-09-28): View icons in the toolbar replace the sidebar (#155).
- [0.2.0a148](docs/releases/v0.2.0a148.md) (2026-09-28): Tables keep every column visible as the window narrows (#154).
- [0.2.0a147](docs/releases/v0.2.0a147.md) (2026-09-28): Dashboard sections and per-table column choosers (#152, #153).
- [0.2.0a146](docs/releases/v0.2.0a146.md) (2026-09-28): Quote evidence shows its date without "N days old" (#151).
- [0.2.0a145](docs/releases/v0.2.0a145.md) (2026-09-28): Dashboard group rows no longer list inferred sub-accounts (#150).
- [0.2.0a144](docs/releases/v0.2.0a144.md) (2026-09-28): Dashboard totals always cover the whole book (#149); Documentation.
- [0.2.0a143](docs/releases/v0.2.0a143.md) (2026-09-28): Reimbursable expenses and receivables (engine, service, CLI).
- [0.2.0a142](docs/releases/v0.2.0a142.md) (2026-09-28): GTK chrome cleanup: navigation, spacing, color, and numeric margins.
- [0.2.0a141](docs/releases/v0.2.0a141.md) (2026-09-28): Entry autocomplete in the desktop transaction editor.
- [0.2.0a140](docs/releases/v0.2.0a140.md) (2026-09-28): Entry autocomplete in quick entry.
- [0.2.0a139](docs/releases/v0.2.0a139.md) (2026-09-28): Categorization rule screens.
- [0.2.0a138](docs/releases/v0.2.0a138.md) (2026-09-28): Reviewed categorization rules.
- [0.2.0a137](docs/releases/v0.2.0a137.md) (2026-09-28): Fixed: the desktop Dashboard grew wider than the screen (#140).
- [0.2.0a136](docs/releases/v0.2.0a136.md) (2026-09-27): Payees in the register and editor.
- [0.2.0a135](docs/releases/v0.2.0a135.md) (2026-09-27): Payee screens.
- [0.2.0a134](docs/releases/v0.2.0a134.md) (2026-09-27): Payees.
- [0.2.0a133](docs/releases/v0.2.0a133.md) (2026-09-27): Transfer review during CSV import.
- [0.2.0a132](docs/releases/v0.2.0a132.md) (2026-09-27): GTK CSV statement import.
- [0.2.0a131](docs/releases/v0.2.0a131.md) (2026-09-27): Web CSV statement import.
- [0.2.0a130](docs/releases/v0.2.0a130.md) (2026-09-27): Web Dashboard tables and group totals render again (#132).
- [0.2.0a129](docs/releases/v0.2.0a129.md) (2026-09-27): OFX and QIF re-import keeps the user's categories (#129).
- [0.2.0a128](docs/releases/v0.2.0a128.md) (2026-09-27): CSV statement import with preview and duplicate review.
- [0.2.0a127](docs/releases/v0.2.0a127.md) (2026-09-27): Currency notes disclose quote age and rounding.
- [0.2.0a126](docs/releases/v0.2.0a126.md) (2026-09-27): Projection converts foreign-currency schedules and balances.
- [0.2.0a125](docs/releases/v0.2.0a125.md) (2026-09-27): Plan totals convert foreign currencies instead of adding them as reporting currency (#124).
- [0.2.0a124](docs/releases/v0.2.0a124.md) (2026-09-27): GTK manual exchange rates.
- [0.2.0a123](docs/releases/v0.2.0a123.md) (2026-09-27): Reviewed, duplicate-safe batch of due scheduled transactions (P1 complete).
- [0.2.0a122](docs/releases/v0.2.0a122.md) (2026-09-27): Missed schedule dates grouped on Dashboard.
- [0.2.0a121](docs/releases/v0.2.0a121.md) (2026-09-27): Flatpak desktop integration and sandboxed GTK smoke; Release runs no longer fail on merges without a version bump.
- [0.2.0a120](docs/releases/v0.2.0a120.md) (2026-09-27): First-run Dashboard for imported commitments (P0 complete).
- [0.2.0a119](docs/releases/v0.2.0a119.md) (2026-09-27): Reconciled transactions survive GnuCash re-import (#117); GnuCash coexistence guidance and entry-autocomplete planning; Flatpak offline file workflow gate.
- [0.2.0a118](docs/releases/v0.2.0a118.md) (2026-09-27): Flatpak build baseline; Imported-book Dashboard acceptance.
- [0.2.0a117](docs/releases/v0.2.0a117.md) (2026-09-27): Opt-in expense rollover.
- [0.2.0a116](docs/releases/v0.2.0a116.md) (2026-09-27): Category remaining through as-of.
- [0.2.0a115](docs/releases/v0.2.0a115.md) (2026-09-27): Dashboard group coverage.
- [0.2.0a114](docs/releases/v0.2.0a114.md) (2026-09-27): Synthetic learning book.
- [0.2.0a113](docs/releases/v0.2.0a113.md) (2026-09-26): First-run Dashboard position and commitment state; Roadmap and documentation review.
- [0.2.0a112](docs/releases/v0.2.0a112.md) (2026-09-26): Web manual exchange-rate entry.
- [0.2.0a111](docs/releases/v0.2.0a111.md) (2026-09-26): CLI manual currency quote entry.
- [0.2.0a110](docs/releases/v0.2.0a110.md) (2026-09-26): Exact manual FX quote write contract.
- [0.2.0a109](docs/releases/v0.2.0a109.md) (2026-09-26): GnuCash commodity identity on import.
- [0.2.0a108](docs/releases/v0.2.0a108.md) (2026-09-26): Currency-aware scheduled occurrence matching.
- [0.2.0a107](docs/releases/v0.2.0a107.md) (2026-09-26): Quote age disclosure without an automatic cutoff.
- [0.2.0a106](docs/releases/v0.2.0a106.md) (2026-09-26): Browser-selected web imports.
- [0.2.0a105](docs/releases/v0.2.0a105.md) (2026-09-26): Dashboard quote completeness.
- [0.2.0a104](docs/releases/v0.2.0a104.md) (2026-09-26): Exact inverse currency quotes.
- [0.2.0a103](docs/releases/v0.2.0a103.md) (2026-09-25): Complete account-summary currency totals.
- [0.2.0a102](docs/releases/v0.2.0a102.md) (2026-09-25): Code-scanning remediation and release isolation.
- [0.2.0a101](docs/releases/v0.2.0a101.md) (2026-09-25): Direct foreign-currency account valuation; Alpha release metadata.
- [0.2.0a100](docs/releases/v0.2.0a100.md) (2026-09-25): Exact direct currency conversion contract.
- [0.2.0a99](docs/releases/v0.2.0a99.md) (2026-09-25): Projection web report and comparison boundary.
- [0.2.0a98](docs/releases/v0.2.0a98.md) (2026-09-25): Projection month explanation web boundary.
- [0.2.0a97](docs/releases/v0.2.0a97.md) (2026-09-25): Scenario management web response boundary.
- [0.2.0a96](docs/releases/v0.2.0a96.md) (2026-09-25): Dashboard web response boundary.
- [0.2.0a95](docs/releases/v0.2.0a95.md) (2026-09-24): Security quote evidence in account views.
- [0.2.0a94](docs/releases/v0.2.0a94.md) (2026-09-24): Scenario schedule web write adapter.
- [0.2.0a93](docs/releases/v0.2.0a93.md) (2026-09-24): Fixed-schedule web write adapter.
- [0.2.0a92](docs/releases/v0.2.0a92.md) (2026-09-24): Plan web report boundary.
- [0.2.0a91](docs/releases/v0.2.0a91.md) (2026-09-24): Plan detail response boundary and trailer discipline; Expense Explorer documentation and PR completeness.
- [0.2.0a90](docs/releases/v0.2.0a90.md) (2026-09-24): Expense Explorer web responsibility split; Bounded money mutation gate.
- [0.2.0a89](docs/releases/v0.2.0a89.md) (2026-09-24): Expense Explorer GTK, web, and printing.
- [0.2.0a88](docs/releases/v0.2.0a88.md) (2026-09-24): Expense Explorer shared contract.
- [0.2.0a87](docs/releases/v0.2.0a87.md) (2026-09-24): Versioned historical-estimation rules; Independent Plan and Projection golden books.
- [0.2.0a86](docs/releases/v0.2.0a86.md) (2026-09-21): Packaged user guide and in-application help.

## 0.2.0a85 and earlier (to 2026-09-20)

Before 0.2.0a85 the project had no published releases; earlier alpha versions were
advanced per change without versioned release notes. 0.2.0a85 is the first release
with notes ([v0.2.0a85](docs/releases/v0.2.0a85.md)). The full history of this
period is in the repository's commits and pull requests. Its lasting outcomes:

- **Storage, integrity, and recovery.** Native schema 7 since 0.2.0a24, with the
  current-schema-only alpha storage policy: schema 3→7 migrations were removed and
  only schemas 6–7 opened, each migrated with a verified backup. Inter-process
  writer lock (a Windows probe that sends no signal since 0.2.0a34); incremental write
  verification; verified backup, restore-as-new, and **Verify book** in GTK, web,
  and CLI (0.2.0a19); the executable invariant set (balanced transactions, split
  identity, commodity roles and SCU representability, fixed-schedule balance,
  unique realization of planned occurrences, reconciliation snapshots) completed in
  0.2.0a33; explicit `ROOT` chart semantics; platform-correct user paths.
- **Exact money and commodities.** Dimensional `Money`/`Rate` arithmetic; tagged
  amounts that reject mixed-currency aggregation and keep value and quantity
  separate; declared commodity minor units instead of cents throughout reporting,
  scheduling, loans, and estimates (0.2.0a82–0.2.0a84); dated security prices and
  as-of valuation; locale-aware GTK and web amount entry; QIF/OFX number and date
  parsing that resolves ambiguity from the whole file.
- **Planning model.** Plan derived from exact-dated scheduled, estimated, and
  actual transactions rather than stored monthly cells (schema 4 event-planning
  core); Base is reality and saved scenarios are layered alternatives with dated
  assumptions and scenario schedules; one semantic account type per account;
  commitments separate from estimates; explicit schedule growth policy; formula
  loans protected from double interest and generic inflation; advanced recurrences
  with bounds, skips, overrides, and per-leg amount timelines; reviewed historical
  estimates with evidence and confidence; signed spendable-cash Plan bridge,
  mortgage cash requirements, escrow, and explainable classifications.
- **Scheduling and loans.** A shared schedule editability contract proven over an
  imported-schedule fidelity matrix; lossless preservation of unsupported custom
  recurrences and formulas; supported SQLite scheduled formulas; account-linked
  card payments and statement-based card obligations; create schedule from an
  actual; approachable loan creation.
- **Dashboard and FSA.** Explicit account-owned Dashboard groups with colon paths
  and subtree deduplication; separate bills, income, and income-triggered
  reserves; paid-off loans; a separate FSA Dashboard; FSA funding years, election,
  run-out, and claims as transactional objects with payments, allocations,
  reimbursements, and refunds.
- **Import and GnuCash interoperability.** Native QIF and bank/card OFX/QFX import;
  stable source identities so re-import never duplicates or clobbers
  BreadSched-owned account, transaction, and split state; imported account
  provenance and notes kept separate from local notes; GnuCash dated prices;
  source-deleted GnuCash transactions synchronized prospectively; missing GnuCash
  dates rejected rather than synthesized; remembered import sources and precise
  counts.
- **Interfaces.** GTK4 as the reference interface with web parity for financial
  behavior; one split-based transaction model, independent registers, inline
  basic entry, and one atomic transaction path; first-class reconciliation in
  shared services; responsive, cancellable Projection and import; printable
  current reports; clean skipping when the GTK4 typelib is missing.
- **Architecture and security.** Typed application services for every write
  (transactions, accounts, schedules, scenarios, assumptions, loans, claims,
  reconciliation, import, review), with web resources as thin adapters; a
  presentation-owned error catalog; loopback-only web access with a per-session
  token, Host and Origin checks; deterministic, resource-bounded formula
  evaluation; release discipline that publishes only tested, documented `main`
  with notes, checksums, and annotated tags.
