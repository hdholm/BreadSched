# BreadSched changelog

This file records completed BreadSched milestones. Current and proposed work
belongs in `ROADMAP.md`.

Entries from 0.2.0a190 onward are complete. Earlier versions are condensed: each
version from 0.2.0a86 through 0.2.0a189 keeps one line naming its outcomes and a
link to its release notes in `docs/releases/`, which hold the full description, and
work up to 0.2.0a85 is summarized by topic. Schema changes, compatibility limits, and
significant security or correctness changes are called out in each condensed
section. Commits and pull requests hold the complete history.

## 0.2.0a262 - 2026-10-08

- **First live quote runs.** Against the real services, the live tests fetched
  every TSP fund from tsp.gov, ECB rates, and Alpha Vantage, and stored them
  through the service, CLI, web route, and an installed `breadsched`. Through
  Finance::Quote, its current CPAN release (1.71) passed everything, while the
  1.59 that Ubuntu 24.04 packages was refused by every keyless US stock source
  (Yahoo and MarketWatch with 401, stooq unparseable, Google "not found"), and
  its `usa` and `nasdaq` groups stop with "IEXCloud API_KEY not defined".
- The round-trip test tries keyless sources in turn (`marketwatch`, `stooq`,
  `googleweb`, `yahoo_json`, `usa`, `nasdaq`) and fails, listing every reason,
  only if none answers; `BREADSCHED_FQ_METHOD` still checks exactly one source.
- The *Live quote sources* workflow runs against both Finance::Quote versions:
  CPAN decides the result, and the distribution package reports without failing it.
- The Finance::Quote status in Online quotes (desktop, browser, and `breadsched
  quotes --list`) names the installed version, and the guide tells users with
  an old package how to install the current one.

## 0.2.0a261 - 2026-10-08

- **GTK tests stay off the desktop.** Every test run, including a plain `pytest -n
  auto`, now gives each pytest process a private Xvfb and GTK's X11 backend before
  GTK loads (`tests/private_display.py`, started from `tests/conftest.py`), as CI
  and `make` already did. On a GNOME/Wayland desktop, a dozen workers opening
  windows on the real compositor crashed GTK inside its Wayland event handling.
  `GUI_VISIBLE=1` still shows the windows; without `Xvfb` the current display is
  used. CONTRIBUTING, the Makefile, and the validation design part describe it.
- The workflows use `actions/upload-artifact@v7` and `actions/download-artifact@v8`,
  which run on Node 24; v4 ran on the deprecated Node 20. Our steps pass only
  `name`, `path`, `if-no-files-found`, and `retention-days`, whose meaning is
  unchanged, and v8 rejects a download whose digest does not match.

## 0.2.0a260 - 2026-10-08

- **Dialogs stay in front at start-up (#295).** Alerts and the due and GnuCash
  change reviews wait until the main window is on screen
  (`widgets.presented.when_presented`), so a modal dialog can no longer open behind
  the window it blocks. Opening a book that needs a schema upgrade (the remembered
  book at start-up, Open, or a file passed to `breadsched-gtk`) now presents the
  window first with an "Upgrading …" page naming the book and its schema, then
  migrates; a failed upgrade returns to the start screen and says why.
  `stored_schema_version()` reads a book's schema without opening it for writing,
  and `breadsched migrate` uses it.

## 0.2.0a259 - 2026-10-08

- **Live quote tests.** `tests/test_online_quotes_live.py` exercises the real
  sources end to end: every listed tsp.gov fund, ECB rates crossed into several
  reporting currencies, Alpha Vantage (your key or its `demo` key), Finance::Quote's
  TSP reader against the native one and another of its sources, then the service,
  CLI, web route, and an installed `breadsched`; the GTK suite fetches live TSP
  prices through the Online quotes dialog. Tests marked `network` run only with
  `BREADSCHED_NETWORK_TESTS=1` and otherwise skip with that reason. A weekly *Live
  quote sources* workflow runs them with Finance::Quote installed.
- CONTRIBUTING and AGENTS state the principle: write the complete tests even when
  the current environment cannot run them, gate them by the missing capability,
  and report where they run.
- **0.2.0a258 was not published.** Its release run installed the `.deb` as
  `./<absolute path>`, which apt rejects (CI's own package job used a relative
  path), and a mirror outage stopped the Windows installer job. The release
  workflow now installs the path `build-package.sh` prints, and a release test
  holds it; the Debian/Ubuntu and Fedora packages first ship with this version.

## 0.2.0a258 - 2026-10-08

- **Debian/Ubuntu and Fedora packages.** `packaging/linux/build-package.sh` turns
  the wheel into `breadsched_<version>_all.deb` or `breadsched-<version>-1.noarch.rpm`:
  the package in a private `/usr/lib/breadsched`, launchers for the system Python
  3, and the desktop entry, metainfo, and icon. They depend on the distribution's
  PyGObject, pycairo, and GTK 4, and recommend Finance::Quote, which (unlike in the
  Flatpak) online quotes can use. CI builds, installs, smoke-tests
  (`smoke-test.sh`), and removes each in Ubuntu 24.04 and Fedora containers, and
  each release publishes both from its tested wheel with checksums.
  Each compiles its Python files after installing and deletes the bytecode caches
  before removal, so removing a package leaves nothing in `/usr/lib/breadsched`.
- The roadmap's packaging item is complete.

## 0.2.0a257 - 2026-10-08

- **Online quotes in the desktop and browser.** **Accounts → Online Quotes…**
  (GTK) and **Online quotes…** (browser) list each security and foreign currency
  with its quote source, keep the Alpha Vantage key, show whether Finance::Quote
  can be used, and **Get quotes**. The dialogs list what was stored and what failed
  and why. GTK fetches in a background worker and stores on the main thread through
  `quotes.Prefetched`. New routes: `GET /api/quotes` and `POST /api/quotes/source`,
  `/api/quotes/key`, `/api/quotes/update`; the key is never returned, and
  `settings.ini` is made owner-readable when it is saved. Fetched prices print at
  their full precision (`presentation.price_text`) here and in `breadsched quotes`.
- The roadmap's online-quotes item is complete; Fedora RPM and Ubuntu DEB packages
  are now item 1.

## 0.2.0a256 - 2026-10-08

- **Online quotes (service, sources, and command line).** A commodity's
  `quote_source` asks for online prices:
  - `tsp`: Thrift Savings Plan funds from tsp.gov.
  - `currency`: ECB reference rates crossed into the reporting currency.
  - `alphavantage`: Alpha Vantage, with the key from settings or
    `ALPHAVANTAGE_API_KEY`.
  - Any other name: a Finance::Quote method, run through an installed Perl
    Finance::Quote outside the Flatpak.

  `gen/services/quotes.update_quotes` stores each answer as a dated `last` price
  with source `Online: <origin>`, updating rather than duplicating the same day's
  price and reporting each failed source per commodity. `breadsched quotes`
  (`--list`, `--dry-run`, `--json`) fetches, and `breadsched quote-source` sets or
  clears a source. GnuCash import carries each commodity's quote source (only when
  GnuCash fetches quotes for it) and refreshes it on re-import. The source is stored
  only when set, so there is no schema change.
- **Dependency policy.** [Decision 0002](docs/design/decisions/0002-dependencies-and-online-quotes.md)
  keeps `gen/` standard-library only and lets adapters and presentations use a
  third-party package once a decision record justifies it. CONTRIBUTING and the
  architecture design part record the rule; quotes needed no new package.
- The roadmap adds Fedora RPM and Ubuntu DEB packages, which can use Finance::Quote
  where the Flatpak cannot, and narrows online quotes to the desktop and browser
  surfaces.

## 0.2.0a255 - 2026-10-08

- **Storage normalization measured and declined.**
  [Decision 0001](docs/design/decisions/0001-transaction-storage.md) records the
  comparison of the current JSON-plus-`split_index` layout with normalized
  transaction and split tables on a 30,000-transaction household history. The
  normalized layout was 43% smaller but loaded every transaction 1.5 to 1.7 times
  slower, failing the roadmap's no-worse-than-5% bar, so transactions stay JSON.
  `scripts/storage_benchmark.py` repeats the measurement. Compact JSON (omitting
  default fields) is listed after the first Beta as the cheaper route to smaller
  books.
- The storage design part named schema 10 as current; it is 11.

## 0.2.0a254 - 2026-10-08

- **The browser register edits like the desktop one.** Clicking a row edits it in
  place. Up/Down in a field save the row being left when it changed and edit the
  row above or below (the blank row below the last), keeping a row that cannot
  save. Typed account paths list their matches under the account choice, and a
  click chooses one. A new **R** column shows each row's reconcile state; clicking
  it posts to `POST /api/register/cleared`, which calls the shared
  `toggle_cleared` service. `GET /api/register` rows carry `reconcile`.
- The roadmap's register item is complete and removed; storage normalization is
  now item 1.

## 0.2.0a253 - 2026-10-08

- **The desktop register edits like GnuCash's.** A single click (or F2) edits a
  transaction in place. Up/Down move between split lines and then between
  transactions, saving the one being left through `save_transaction` (a refused
  save keeps the user there with the reason); below the last transaction is the
  blank row. A typed account path lists its matches under the register, and
  clicking one chooses it.
- **R column.** Each register row shows its split's reconcile state; clicking
  toggles not cleared ↔ cleared through the new `toggle_cleared` reconciliation
  service, which also adds a newly cleared entry to (or removes it from) an open
  statement in the same change. Reconciled, frozen, and void entries are refused
  (`reconciliation.split.locked`).
- The roadmap's register item is now web register equivalence; the GTK behaviors
  are built on the existing column view with persistent editor widgets rather than a
  separate grid widget (see the user-interfaces design part).

## 0.2.0a252 - 2026-10-08

- **GnuCash stock splits import.** A GnuCash share split (one split that changes
  shares with no value) was skipped as "only one split, with no value", so its
  shares were missing. It now imports with an importer-added zero-value leg in
  `Equity:Share splits` (created on first use), and cost basis scales the lots as a
  share split. The leg is marked `importer_added` (stored only when set), keeps its
  handle on re-import, and GnuCash write-back never sends or compares it.
  `breadsched import` reports `share_splits`.
- **Print in Browser leaves the desktop File menu** on Linux and Windows, which
  print natively. It stays on macOS as the placeholder for native macOS printing;
  the dialog reports still fall back to the browser when GTK printing fails, and
  the web interface keeps its own printing.

## 0.2.0a251 - 2026-10-08

- **Register typing shared by both registers.** `engine/entry_input` and
  `services/entry_input` read register text for GTK directly and for the browser
  through `GET /api/entry/date`, `/api/entry/amount`, `/api/entry/accounts`, and
  `/api/entry/num`:
  - Dates take GnuCash's shortcuts while the field holds a whole date (`+`/`-` a
    day, `]`/`[` a month, `t`, `m`/`h`, `y`/`r`) and short forms relative to the
    date shown (`15`, `3/15`, `3/15/27`).
  - Amounts accept arithmetic (`12.50+3*2`) through the safe formula language,
    rounded half up to the currency's unit; a negative result moves to the other
    column, and a single number stays exact.
  - Account choices complete typed paths segment by segment (`Ex:Gr` →
    Expenses:Groceries) in the Transfer picker and split lines.
  - `+`/`-` in **Num** step the number, continuing from the register's last one
    when empty.
- The browser register's date is now a text field so the shortcuts can be typed.

## 0.2.0a250 - 2026-10-08

- **Payees removed; the description is a transaction's only name, as in GnuCash.**
  Native schema advances from 10 to 11. The 10→11 migration converts each
  categorization rule that matched a payee into one rule per description key that
  payee claimed, in the same position; drops `set_payee` from rules and `payee` from
  transactions; and drops the `payee` table, after the usual verified
  pre-migration backup. Gone with them: the payee model, engine, service, and
  verification checks; the GTK Payees dialog and **Actions → Payees…**; the browser
  **Payees** view and its routes (`/api/payees`, `/api/payee/save`,
  `/api/payee/delete`, `/api/payees/accept`, `/api/transaction/payee`); `breadsched
  payees`; the Payee column and picker in both registers and the transaction
  editor; rule matching by payee and `--add-payee`/`--set-payee`; autocomplete by
  payee; and the CSV import payee column (`--payee`) with its row note.
- `match_key` moved to `engine/description_keys`, shared by categorization rules,
  reimbursement proposals, and entry autocomplete.

## 0.2.0a249 - 2026-10-08

- **Storage safety tests.** `tests/test_storage_safety.py` covers the code every
  book-format migration relies on: each commit-time refusal and deletion check,
  derived-index disagreement, the writer lock's stale, foreign, unreadable, and
  replaced lock files and its POSIX and Windows liveness probes, and injected
  backup and restore failures, each checked to leave no temporary file, partly
  installed book, or lock behind. Line coverage rose from 71% to 100% for the book
  lock, 80% to 99% for backups, and 77% to 95% for change verification. No
  application code changed; nothing the tests exercised needed a fix.

## 0.2.0a248 - 2026-10-08

- **Wording split by area.** `presentation.py` (808 lines) is now the
  `presentation` package: `messages` (service-error wording and the gettext
  catalog), `notices` (book-open, Review, and post-import notices), `benefits`
  (FSA, claims, shared costs, reimbursements), `planning` (goals, Plan, Projection,
  runway), and `investments` (holdings, lots). The package re-exports every public
  name, so no caller changed; an architecture test checks that the modules import
  no interface code and that every public name stays re-exported.
- **Design split by topic.** `DESIGN.md` keeps the overview, product boundary, and
  an index; each other part (architecture, domain model, storage, planning,
  valuation and reporting, household workflows, interoperability, user interfaces,
  security, validation and releases) is its own document under `docs/design/`,
  with cross-part links rewritten and checked by the documentation link test.
  CONTRIBUTING's document review covers the parts with `DESIGN.md`.

## 0.2.0a247 - 2026-10-06

- **Cost basis follows transfers and share splits.** Shares moved between two of
  the book's security accounts of the same security are no longer a sale and a new
  purchase: the sending account removes lots by its method without realizing a gain,
  and the receiving account adopts those exact lots with their purchase dates and
  cost (`LotMove`, re-derived from the sender, never stored). A transaction that
  changes shares at zero value with every other leg zero is a share split: each
  lot's shares scale by the same exact factor and its cost stays, where before it
  opened a zero-cost lot (or, for a reverse split, realized a loss). Transfers and
  splits are listed in the GTK Holdings dialog, the browser (`moves` with text in
  `/api/holdings`), and `holdings --lots`. Shares moved out beyond the recorded
  purchases are named, and reach the other account at zero cost, rather than guessed.
- The roadmap records that a GnuCash stock split stored as a single zero-value
  split is still skipped (and reported) on import.

## 0.2.0a246 - 2026-10-06

- **Average cost basis.** An Investment or Retirement account can use average cost
  instead of first in, first out (`Account.cost_basis_method`, `"fifo"` by default,
  stored in the account's serialized data with no schema change). Each sale then
  takes the same exact fraction of every open lot, so each sold share costs the
  average of every share held. The account service refuses an unknown method
  (`account.cost_basis_method.invalid`) without changing the stored account. Chosen
  in the GTK account editor (**Cost of shares sold**), the browser's account
  settings (`/api/account/cost-basis`, refused for non-security accounts), and
  `breadsched account edit --cost-basis {fifo,average}`; holdings text, `/api/holdings`,
  and `holdings --json` name the method.
- **GnuCash re-import keeps local account settings.** Re-importing a GnuCash book
  no longer clears a Flexible Spending Account's dependent-care flag (lost since
  0.2.0a224); it and the new cost basis method are kept like the FSA plan years.

## 0.2.0a245 - 2026-10-06

- **Smaller command and window modules.** The payee, rule, tag, and attachment
  commands moved from `cli/ledger_commands` (1,215 lines) to
  `cli/categorization_commands` (603); ledger commands keep accounts, registers,
  balances, rates, and transactions (636). The window's toolbar, views, and view
  commands (`TOOLBAR`, `CATEGORIES`, `ViewAction`, `VIEW_ACTIONS`,
  `view_action_name`) moved from `gui/viewmanager` to a GTK-free `gui/view_catalog`,
  which `viewmanager` re-exports and the application now imports. Architecture
  tests check both. No behavior changed.

## 0.2.0a244 - 2026-10-06

- **Holdings, cost basis, and gains.** `engine/cost_basis` derives each security
  account's lots from its splits (first in, first out, exact shares), realized
  gains per sale and by year, and the unrealized gain against the latest market
  quote in the same currency; zero-cost arrivals, sales beyond recorded purchases,
  mixed currencies, and missing or foreign quotes are named, never approximated.
  The GTK Accounts view gains **Actions → Holdings and Cost Basis…**
  (`HoldingsDialog`), the browser's Accounts page **Holdings and cost basis…**
  (`/api/holdings`), and the command line `breadsched holdings [--lots]
  [--as-of]` (new `cli/investment_commands`). Nothing is stored.

## 0.2.0a243 - 2026-10-06

- **Cash runway in every projection and comparison.** `Projection.runway()`
  (`CashRunway`) gives how many months cash covers before it first goes negative
  (or that it lasts), the lowest cash and when, the first goal shortfall, and each
  drawdown account that runs out (`Projection.depletions`). `runway_lines` and
  `runway_comparison_text` show it in the GTK projection notes, the browser's
  **Cash runway** section and comparison, the printed Projection and comparison,
  and after `project` (`runway` in `--json`) and `compare`.

## 0.2.0a242 - 2026-10-06

- **The SQLite backend's lock, copies, and index checks have their own modules.**
  `gen/db/sqlite` (1,641 lines) also held the writer lock, backup and restore, read
  snapshots, and the derived-index checks. `BookWriterLock` (`gen/db/book_lock`),
  `backup_connection`, `restore_backup`, `snapshot_of`, and `migration_backup_path`
  (`gen/db/backups`), and `derived_column_issues` and `split_index_issues`
  (`gen/db/storage_verification`) now hold them; `DbSQLite` keeps `backup_to`,
  `restore_backup`, and `verify_book` as delegates (1,277 lines). An architecture
  test checks that the new modules never import the backend. No behavior changed.

## 0.2.0a241 - 2026-10-06

- **GnuCash write-back is split into layers.** `plugins/export/gnucash_writeback`
  (1,467 lines) read both book formats, planned, wrote, and verified. Reading and
  the fingerprint moved to `gnucash_source` (442 lines), planning to
  `gnucash_writeback_plan` (378), and the SQLite and XML writers to
  `gnucash_book_writers` (470); `gnucash_writeback` (303) backs up, applies,
  verifies, records, and re-exports the same public API. Helpers that cross a module
  became public (`preflight`, `table_names`, `xml_namespaces`, `write_sqlite`,
  `write_xml`, and others). The importers record the fingerprint through
  `gnucash_source`. An architecture test checks that each layer imports only those
  below it. No behavior changed.

## 0.2.0a240 - 2026-10-06

- **Projection results have their own module.** `engine/projection` (1,389 lines)
  held both the calculation and the types it returns. `Projection`, `MonthRow`,
  `MonthLedger`, `ProjectionProgress`, `ProjectionAccountDetail`,
  `ProjectionMonthDetail`, `ComparisonRow`, and `compare` moved to
  `engine/projection_result` (456 lines), which never imports the engine;
  `projection` re-exports them (984 lines). The conservation bridge, print, CSV
  export, and presentation import only the result module, and an architecture test
  checks the boundary and the re-exports. No behavior changed.

## 0.2.0a239 - 2026-10-06

- **The Plan's category report has its own module.** `engine/activity` (1,689 lines)
  both dated activity into periods and built the Plan's rows. `CategoryReport`,
  `CategoryActivity`, `CashBridgeActivity`, `CashPosition`, `PlanningFlowActivity`,
  `MortgagePaymentActivity`, `build_category_report`, `currency_notes`, the
  projected spendable-cash position, and the row builders moved to
  `engine/category_report` (865 lines), which reads `activity` and is never imported
  by it; `activity` is now 889 lines. The classification helpers both use became
  public (`split_totals`, `economic_planning_flow_amounts`,
  `redundant_cash_flow_split`) instead of crossing as private names. The services,
  GTK Plan view, web, CLI, print, and tests import from the new module, and an
  architecture test checks the boundary. No behavior changed.

## 0.2.0a238 - 2026-10-06

- **Change a scenario's expected reimbursement on the desktop and in the browser.**
  An open receivable gains an **In scenario** row (scenario, amount expected back,
  date, **Apply to scenario**) and a line listing every scenario's change, in the GTK
  receivables dialog and on the browser's Reimbursables page
  (`/api/receivable/scenario`; `/api/receivables` adds `scenarios` and each
  receivable's `scenario_changes`). Both call `set_reimbursement_override`, so a
  refused change leaves the scenario as it was; empty fields clear the change.

## 0.2.0a237 - 2026-10-06

- **Projection shows reimbursable expenses' gross and net cost, and scenarios can
  change what a payer is expected to send.** `Scenario.reimbursement_overrides`
  (`ReimbursementOverride`: amount paid, zero for nothing, and expected date) changes
  an expected reimbursement in one scenario; what it does not expect is projected as
  written off on that date back into the expense (`planning.receivable_receipt_events`
  via `engine/reimbursement_outlook.scenario_receipts`), so the receivable still
  empties and the shortfall is projected spending. `Projection.reimbursements` lists
  each receipt's gross cost, earlier reimbursements and write-offs, what the scenario
  expects, and the net household cost, worded by
  `presentation.reimbursement_outlook_text` in GTK, the browser (`reimbursements`,
  `reimbursement_notes`), print, and `project`. `set_reimbursement_override` validates
  and stores changes; `receivables --scenario NAME --expect RECEIVABLE [--amount]
  [--on]` sets or clears one; deleting a receivable drops them.

## 0.2.0a236 - 2026-10-06

- **Retirement drawdowns in the desktop and browser.** The desktop scenario manager
  gains **Edit retirement drawdowns…**, a list with add, edit, and remove, and an
  editor for the source and cash accounts, dates, and either a fixed yearly amount
  (rising with inflation or level) or a yearly share of the balance. The browser's
  Manage scenarios page gains the same **Retirement drawdowns** section through
  `/api/scenario/drawdown/save` and `/delete`; `/api/scenarios` lists the eligible
  accounts. Both use the shared drawdown services, so a refused entry changes
  nothing. **Save as scenario** in Projection keeps a scenario's drawdowns.

## 0.2.0a235 - 2026-10-06

- **Retirement drawdown in scenarios.** `Scenario.drawdowns` withdraws monthly from
  a holding account into spendable cash from a start date until an optional end:
  a fixed yearly amount that rises with the scenario's expense inflation on each
  anniversary (or stays level), or a yearly share of the account's projected
  balance on each withdrawal date. The projection sizes each withdrawal from the
  state on its date and applies it as a scenario-only placeholder event, so a
  retirement account's withdrawal is a retirement distribution and the ledger and
  bridges reconcile; a withdrawal is capped at the balance, with a "runs out of
  money" warning. `services.scenarios.save_drawdown`, `remove_drawdown`, and
  `drawdown_accounts` validate and store rules, refusing without changing the
  scenario; the new `breadsched drawdown` command lists, saves, and removes them.
  Rules are stored in the scenario document, with no schema change.
- **Scenario account references in one place.** `Scenario.account_references`
  replaces three copies of the same list in book and change verification and adds
  drawdown accounts and a goal override's purchase account, which an account
  deletion previously left dangling.

## 0.2.0a234 - 2026-10-06

- **Older GnuCash SQLite books keep their dates east of UTC.** GnuCash before 2.6.10
  stored a posting date as local midnight in UTC, so a book from a household ahead
  of UTC (Europe, Asia, Australia, New Zealand) imported every transaction and
  schedule a day early. `parse_gnc_sql_posting_date` reads a time of day from 11:00
  UTC on as the next day (GnuCash's own neutral-time range in reverse), for SQLite
  post dates, schedule start and end dates, and write-back's reading of the book.
  Prices and XML dates are read as before. Re-importing such a book corrects the
  dates.

## 0.2.0a233 - 2026-10-06

- **Property-based money tests and fuzzed malformed imports.**
  `tests/test_money_properties.py` checks over generated amounts that money
  arithmetic is exact, rounding is half away from zero at any denominator,
  allocation keeps every minor unit, and the GnuCash and text forms read back.
  `tests/test_import_fuzz.py` feeds generated malformed QIF, OFX, and CSV statements
  to the importers: each finishes or refuses cleanly, writes nothing when it
  refuses, leaves a book that verifies, and re-imports without adding transactions.
- **Unreadable CSV and QIF price text.** The fuzzing found that a `csv.Error`
  (on Python 3.10, any NUL byte; on every version, an oversized field) escaped the
  CSV importer and QIF price sections as an unexplained error. A CSV file the
  reader refuses is now refused (`import.csv.file.unreadable`, "The file is not
  CSV text") and an unreadable QIF price line is skipped and named. The NUL inputs
  stay pinned as explicit examples in the fuzz tests.
- **Python 3.10 support dropped.** `requires-python` is now `>=3.11`; CI tests
  3.11–3.14; Ruff and mypy target 3.11 (`datetime.UTC`); the `tomli` fallback and
  dependency are gone. Ruff's `StrEnum` rewrite (UP042) is ignored because it would
  change the text of existing string enums.

## 0.2.0a232 - 2026-10-06

- **Categorization rules can set a payee.** A description rule may name a payee
  (`CategoryRule.set_payee`, `AddRule.set_payee`); its proposal sets that payee on a
  transaction with none, never replacing one, and accepting reports how many it set
  (`AppliedCategories.payees_set`). A payee rule cannot set one
  (`rule.set_payee.payee_match`); a missing payee is refused
  (`rule.set_payee.not_found`). Deleting a payee now removes rules matching it and
  clears it from rules that set it, in the same undo step. GTK and the browser add
  **Also set payee** and a **Sets payee** column; the CLI adds `rules --set-payee`
  and `set_payee`, `payee`, and `payees_set` in JSON.

## 0.2.0a231 - 2026-10-06

- **Dashboard bills have their own module.** Bill cycles, `BillRow`, missed-occurrence
  grouping, income-weighted reserves, and the pending cash flow behind liquidity
  (about 470 lines) moved from `engine/dashboard` to `engine/dashboard_bills`;
  `dashboard` re-exports the public names. No behavior changed.
- **Historical-estimate statistics have their own module.** The pure history
  statistics (robust sample, typical amount, recurrence inference and next start,
  cadence, trend, seasonality, and confidence, about 520 lines with their evidence
  types) moved from `engine/estimates` to `engine/estimate_history`, which never
  reads the book; an architecture test keeps it that way. The golden estimate tests
  pass unchanged. No behavior changed.

## 0.2.0a230 - 2026-10-06

- **Import summaries group problems by reason.** Each skip reason is one line with
  its count and the first three records it stopped (`ImportResult.problems()`,
  `ImportProblem`), most frequent first, and the warnings that follow are only those
  not about one skipped record (`ImportResult.notices`), so a file with many
  rejected rows no longer hides the other warnings. GTK, the browser, and the CLI
  show the new summary; CLI JSON adds `problems` and `notices`, and the browser's
  import and CSV import responses add `problems`.

## 0.2.0a229 - 2026-10-06

- **Not published.** Pull request #266 (0.2.0a230) merged while this version's
  release run was still building, and the release workflow publishes only a
  commit that is still the tip of `main`, so 0.2.0a229 has no tag, release, or
  artifacts. Its changes were first released in 0.2.0a230.
- **Plan shows reimbursable expenses' gross and net cost.** An expense category's
  Plan actual is the net household cost; the Plan now also lists, under
  **Reimbursable expenses: gross and net cost**, each category a receivable changed
  in the range with its gross cost, what was reimbursed or is still expected back,
  and the net cost (`CategoryActivity.reimbursable`, `gross`,
  `CategoryReport.reimbursable_categories`, from `receivables.plan_adjustments`).
  A reimbursement is never counted twice and a write-off stays in the net cost. A
  Plan cell's detail gives the same figures (`CategoryPeriodDetail.reimbursable`,
  `gross`), in GTK, the browser (`reimbursable` on `/api/plan`, `gross`,
  `reimbursable`, and `cost_text` in a detail's `summary`), and print;
  `receivables --costs START END` lists them on the command line.
- **The categorization rules dialog fits a laptop screen with larger fonts.** Its
  rule and proposal lists ask for 120 and 160 pixels at least (from 140 and 200) and
  still grow with the window; it needed 604 of the audit's 600 pixels on a desktop
  with larger fonts. `make test-gui` and `make test-gui-parallel` run the GTK tests
  on a virtual display through `xvfb-run` when it is installed, so no windows flash
  on screen; `GUI_VISIBLE=1` shows them.

## 0.2.0a228 - 2026-10-06

- **Commit-time verification has its own module.** The checks that refuse a write
  batch leaving the book inconsistent (about 650 lines) moved from `gen/db/sqlite`
  to `gen/db/change_verification.ChangeVerification`, which `DbSQLite` now extends.
  No behavior changed.
- **Plan detail has its own engine module.** The three Plan cell explanations, their
  detail types, and their explanation helpers (about 600 lines) moved from
  `engine/activity` to `engine/plan_detail`; the classification helpers both modules
  need (`planning_flow_decision`, `escrow_planning_flows`, `mortgage_payment`) became
  public in `activity`. GTK, web, the Expense Explorer service, and the tests import
  the explanations from `plan_detail`. An architecture test keeps `activity` free of
  the explanations and private helpers inside it. No behavior changed.

## 0.2.0a227 - 2026-10-06

- **QIF imports hold back possible duplicates.** A new QIF bank, cash, or card row
  matching a transaction already in the account from elsewhere on the same date for
  the same amount is held back and reported, as OFX rows are, one row per existing
  transaction. New rows are decided after the whole file's handles are known, so an
  export's own rows never match each other in any order. `include_duplicates` and
  the CLI, GTK, and browser choices now apply to QIF too. QIF investment records are
  not checked.

## 0.2.0a226 - 2026-10-06

- **OFX imports hold back possible duplicates.** A new OFX/QFX bank or card row
  matching a transaction already in the account from elsewhere (typed in, from a CSV,
  or from a statement with a different FITID) on the same date for the same amount is
  held back and reported (`ImportResult.possible_duplicates`), one row per existing
  transaction; the statement's own rows never match themselves. `include_duplicates`
  on the import service, CLI `import --include-duplicates`, and **Include possible
  duplicates** in the GTK import dialog and the browser import form (JSON and upload
  query `include_duplicates`) import them. QIF is not covered yet.

## 0.2.0a225 - 2026-10-06

- **Expected reimbursements in Plan and Projection.** A receivable with an expected
  date becomes a planned one-off receipt on that date: what is still owed moves from
  the receivable account into the cash account that paid the expense (the card's
  paying account for a card-paid expense; `receivables.expected_receipt`,
  `planning.receivable_receipt_events`). Projected cash rises only from the
  expected date and net worth is unchanged. Disputed, overdue, settled, and
  written-off receivables are not counted on. The receipt is never posted and is
  replaced by the real, linked reimbursement.

## 0.2.0a224 - 2026-10-06

- **Dependent care FSAs.** An FSA account can be marked dependent care
  (`Account.fsa_dependent_care`; GTK account dialog, browser FSA editor, CLI
  `account --dependent-care yes|no`). Its availability is what has been contributed
  that year (at most the election) less what was used, rather than the whole
  election; a claim on it needs no EOB, so what was paid is what the FSA owes, and it
  stays open while contributions can still arrive instead of reading as out of funds;
  a carryover limit is refused (`account.fsa.dependent_care.carryover`). The FSA
  Dashboard and `claims --years` mark the account; web and CLI JSON add
  `dependent_care`.
- **Fixed:** the browser's account settings routes (FSA years, type, emergency
  fund, card payment) edited the stored account object in place, so a save the
  service refused could leave the open book's copy changed until reload. They now
  edit a copy.

## 0.2.0a223 - 2026-10-06

- **A scenario can model a savings goal's purchase.** A goal override can name a
  purchase date (on or after the target date) and an expense or asset account; that
  scenario's Plan and Projection then show a one-off moving the target out of the
  goal's account into it (`planning.goal_purchase_events`), never escalated and never
  posted, and the goal sets nothing aside from that month. The milestone says when
  the scenario buys it. Base and other scenarios are unchanged; leaving the goal out
  drops its purchase. The service refuses an incomplete purchase, an early date, or
  an unsuitable account (`savings_goal.purchase.*`) without changing the scenario.
  CLI `goals --override … --buy-on DATE --buy-into ACCOUNT`; GTK **Purchase in that
  scenario**; browser **Buy on** and **Buy into**; `/api/savings-goals` adds
  `purchase_accounts`.

## 0.2.0a222 - 2026-10-06

- **Walkthroughs.** The guide's overview walks through setting up a household
  (accounts with opening balances, a card with its payment, income and spending,
  loans and transfers, a savings goal, the Dashboard and Plan, and a Base
  projection) and comparing scenarios (a child of Base with different assumptions
  and a scenario-only goal change), linking each step to its section. The CLI guide
  gives both as commands, and a test runs them in order exactly as printed, so they
  cannot drift from the command line.
- `breadsched estimate --help` now says what `--amount` means: what each occurrence
  posts to `--account`, positive for an expense and negative for income.

## 0.2.0a221 - 2026-10-06

- **Goals in non-cash accounts are compared with the account's projected balance.**
  A savings goal held in a brokerage or other non-cash asset account now has a
  Projection milestone that compares that account's projected closing balance in the
  target month with everything goals in that account have set aside then
  (`GoalMilestone.account_close`, `account_held`, `covered`), instead of saying it is
  not compared. An account excluded from projection is still listed without a
  comparison, and says so. The web and CLI milestone JSON now come from one
  `GoalMilestone.as_dict` and add `cash_account`, `account`, `account_name`,
  `account_close`, and `account_held`.
- **GTK tests run in parallel in CI.** The session-bus GTK run now uses two xdist
  workers (`make test-gui-parallel`); the serial run stays as the diagnostic path.
  Test application identities include the xdist worker, so processes sharing a bus
  never collide.

## 0.2.0a220 - 2026-10-06

- **Contextual help.** Import (file, CSV, held GnuCash changes, write-back),
  reconciliation, schedule, due review, paycheck, Expense Explorer, net worth history,
  payee, rule, reimbursable, and goal workflows have a **Help** button that opens the
  guide at their section: in GTK, the guide window at the desktop part's heading; in
  the browser, a new tab on **Guide** at the browser part's heading, leaving the form
  as it was. One shared table, `user_guide.HELP_TOPICS`, names each topic's heading;
  `GET /api/guide?topic=` resolves it for the browser. Tests require each heading to
  exist in both parts and each interface's topics to come from the table. Help
  buttons are hidden when printing a browser page.

## 0.2.0a219 - 2026-10-06

- **CSV split columns.** A CSV statement row can post to several categories: map a
  category column and an amount column for each split (`CsvMapping.splits`; CLI
  `import-csv --split CATEGORY=AMOUNT`, repeated; **Add split columns** in the GTK
  dialog and the browser's CSV section). The filled splits, in the row's sign
  convention, must add up exactly to the row's amount; a mismatch, a half-filled
  pair, or an unknown category makes the row invalid with its reason, and no
  balancing split or account is invented. Mapping split columns together with one
  category column is refused (`import.csv.split.mapping`). Split rows are not offered
  as transfers, and re-import still never changes an accepted row. Previews show
  each row's splits (CLI table and `--json`, GTK, browser).
- **Security policy, issue forms, and code owners.** `SECURITY.md` routes
  vulnerabilities to GitHub's private vulnerability reporting and repeats the rule
  against attaching unsanitized financial data. Bug and feature issue forms require
  confirming that no real financial data is included; blank issues are off and the
  chooser links the private report. `.github/CODEOWNERS` names the maintainer.

## 0.2.0a218 - 2026-10-06

- **The browser page's script is split by area.** The 5,500-line `app.js` is now
  fourteen classic scripts in `web/static/`: `core.js` (state, DOM and number
  helpers, API calls), `controls.js` (shared schedule editors), `accounts`, `entry`,
  `schedules`, `imports`, `plan`, `scenarios`, `review`, `projection`, `household`,
  `book`, and `dashboard`, and `app.js`, now only navigation and start-up, loaded
  last. `web.transport.SCRIPTS` lists them in load order and is the transport's
  allowlist; `index.html` loads them in that order. New tests check the order, that
  only `app.js` runs top-level code, and (in a browser) that every view loads.
- **Fixed:** the browser's Verify page always showed "Could not load: main is not
  defined" instead of its result; it now reports the book and **Verify again**
  reruns it.
- **Windows installer build.** MSYS2's NSIS 3.13 ships no plugins, so `makensis`
  could not find nsDialogs. The CI and release jobs install the official NSIS build
  (`choco install nsis`), and the build script compiles with that release's own
  `makensis.exe` and x86-unicode plugins (an installer built by MSYS2's makensis
  with the official plugins hung in a silent upgrade), falls back to MSYS2's
  makensis only with plugins of its own, and stops with the NSIS files it found when
  there are none. Both installer jobs now time out after 30 minutes.

## 0.2.0a217 - 2026-10-06

- **The command line is split by area.** The 4,900-line `cli/main.py` now only builds
  the parser and dispatches. Each area's subcommands, parsers and handlers together,
  moved to `cli/book_commands`, `import_commands`, `ledger_commands`,
  `plan_commands`, `benefit_commands`, and `projection_commands`, with shared
  helpers in `cli.common`. Every subcommand's options and help are unchanged;
  `breadsched --help` now lists the commands grouped by area. New architecture
  tests keep handlers out of the entry point and beside their parsers. The GTK
  New Book action imports `cmd_init` from `cli.book_commands`.

## 0.2.0a216 - 2026-10-05

- **Every web request handler is a resource adapter.** The scheduled, historical
  estimate, due-review, loan, scenario (assumptions, dated periods, events), Review,
  import, account, security, book summary, Dashboard settings, Plan settings, and
  projection handlers moved off `web.server.Api` into `web/*_resource.py` modules
  (new `schedule_resource`, `loan_resource`, `review_resource`, `import_resource`,
  `account_resource`, `book_resource`; `scenario_resource`, `projection_resource`,
  `plan_resource`, and `dashboard_resource` gained their writes). Routes call those
  functions directly; `web.context.Api` now carries only the open book, and
  `web.server` re-exports the entry points. Shared browser controls have one parser:
  `web.controls` (amounts, service errors, `ResourceError`) and
  `web.schedule_controls` (recurrence, exceptions, split rows), replacing the
  `schedule_write_resource` protocol over `Api` helpers and two duplicate recurrence
  parsers. Removed dead Plan date helpers and a `transport`/`server` import cycle.
  Behavior is unchanged except that a one-time schedule's editor may now leave the
  end date or count filled; both are ignored, as the scenario editor already did.
  The web and GTK Dashboard tests that failed on any day a sample occurrence fell
  due (they counted that day's pending bill as missed) now count only overdue dates.
  No schema change.

## 0.2.0a215 - 2026-10-01

- **Projections show how every balance reconciles.** A new
  `engine.projection_bridge` states the engine's conservation check for people: for
  a month or the whole horizon, Cash, Investments, Debts, and Net worth each show
  opening + planned events + interest, performance, or debt interest = closing, with
  an **Unexplained** line that is always zero. GTK's month explanation adds **How this
  month reconciles** and **Across the whole projection**; the web month report and
  `/api/projection` gain `bridges`, shown in the month dialog and under the chart;
  `breadsched project --bridge [YYYY-MM]` prints them. No schema change.

## 0.2.0a214 - 2026-10-01

- **Payroll templates and pay changes.** A new `engine.payroll` reads any schedule
  shaped like a paycheck (gross into income, net into Bank or Cash, and taxes,
  deductions, savings, or repayments between) as of a date, groups its lines, and
  computes take-home. `services.payroll` keeps payroll templates in the book (each
  line a fixed amount or a percentage of gross), describes an existing paycheck as a
  template, creates a fixed paycheck schedule from one, and previews or saves a pay
  change from a date as per-leg future amounts (taxes scale with gross by default;
  any line can be scaled, set, or kept; the net deposit balances). GTK adds
  **Payroll…** to Scheduled; the web adds a **Payroll** view and `/api/payroll`
  routes; the new `breadsched payroll` command does the same. No schema change.

## 0.2.0a213 - 2026-10-01

- **FSA statement lines are proposed for their claims.** A new
  `engine.fsa_claim_proposals` proposes a claim link for each unlinked FSA payment
  from the card, reimbursement, or provider refund that exactly one claim fits with a
  matching amount (one movement per claim per batch; the rest stays in Review).
  `accept_claim_links` recomputes before linking. The GTK and web FSA Dashboards list
  **Proposed claim links** with checkboxes and **Link selected**
  (`POST /api/fsa/claim-links/accept`); import and reconciliation, in GTK and the
  web, say when proposals are waiting; `breadsched claims --proposals` lists them
  and `--link-proposals` links them. Correction: claim suggestions skipped fully
  reimbursed claims entirely, so a provider refund arriving after the FSA had paid
  was never suggested, in Review or elsewhere. No schema change.

## 0.2.0a212 - 2026-09-30

- **Review explains its suggestions and actions.** A new
  `engine.review_explain` gives each candidate planned item a confidence (**Close
  match** within two days and 5%, otherwise **Possible match**) and its reasons:
  shared accounts, amount and date offsets, and description words in common. When
  there is no candidate it says why (nearest item outside the seven-day window, in
  another currency, all rejected, or no schedule uses the accounts). A transaction
  that moves FSA money names its flow and how to attach it to a claim. Match,
  Reject, Skip, Unexpected, and Attach to FSA claim each explain what they do
  (`presentation.REVIEW_ACTION_HELP`). GTK Resolve actuals and the web Review show
  the reasons, hints, and help; `/api/review` carries `confidence`, `label`,
  `reasons`, `no_candidate_reason`, `fsa_hint`, and `action_help`; the new
  `breadsched review` lists the same read-only. No schema change.

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
