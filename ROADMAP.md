# BreadSched roadmap

This file is the **single authoritative source for unfinished BreadSched work**.
Completed milestones are retained in [`CHANGELOG.md`](CHANGELOG.md).

Published alpha releases are listed on the repository's GitHub Releases page; each
carries GitHub pre-release metadata as well as versioned notes and verified
artifacts. A version is released only from the commit that first carries its notes.


## Product direction

BreadSched aims to become a **general household-finance application**. It should
cover accounts and registers, reconciliation, scheduled transactions, planning,
scenarios, projection, investments and retirement, and common imports. GnuCash
compatibility matters while these household workflows mature; business accounting
features remain outside the product scope.

Until that household feature set is sufficiently complete, **GnuCash compatibility
is a first-class requirement**. GTK4 is the canonical interface and Linux is the
primary native desktop target; the web interface remains a supported parity surface.
Cross-cutting workflow logic belongs in shared services rather than presentation code.


## Prioritized delivery

These are the current priorities, subject to review as field evidence changes.
Each slice should use shared calculations and cover GTK, web, CLI, and printable
output wherever that behavior is exposed. Preserve GnuCash source ownership and
round-trip limits, exact money, and explicit missing-currency valuations.

1. **P1 — GTK chrome and layout cleanup.** A field report identified overlapping
   navigation, cramped layout, no color to distinguish rows, numeric values sitting
   against a pane's edge, and register entry that looked dialog-only. Delivered: the
   toolbar no longer duplicates sidebar categories (Plan/Accounts/Projection were
   both a toolbar button and a sidebar row; the sidebar is now the only place
   category navigation lives), the sidebar starts narrower now that it carries no
   duplicated actions, every ColumnView-based table (register, accounts, dashboard,
   scheduled, upcoming) carries a shared `.data-table` class with subtle
   at-rest row banding so rows are distinguishable without hovering or a second
   read, every numeric value keeps clear space from its column or pane edge, the
   Plan grid's numbers now share that same spacing, and the register's dialog
   button is now labeled to make clear it is for extra splits/notes/reconciliation
   -- an ordinary two-split entry already posts directly from the register's quick
   entry with no dialog (delivered in 0.2.0a140). Remaining: audit every dialog for
   unbounded growth the way issue #140 found on the Dashboard (a dialog whose
   content is driven by book data should never grow past a sane bound); consider
   further color to distinguish transaction/account states (reconciled, scheduled
   vs. posted, over/under budget) without duplicating GnuCash's specific look,
   which is not a goal; and review whether other dialogs would benefit from the
   same width/margin discipline. GTK4 is the canonical interface, so this work is
   GTK-first; carry a matching web adjustment only where the same confusion exists
   there.
2. **P1 — Reimbursable expenses and receivables.** The domain model, engine,
   service, native schema (9), and CLI (`breadsched receivables`) are delivered:
   a receivable tracks an expense and what an insurer, employer, or other payer
   owes as linked but distinct facts, shows open, partial, disputed, written-off,
   and settled status (always recomputed, never stored) with age and an optional
   expected cash date, never counts a reimbursement as new income, never erases
   the original expense, and coordinates with GnuCash re-import the same way FSA
   claims do. Remaining: GTK and web presentation surfaces (a dialog and register
   integration mirroring payees/rules), reconciling a reimbursement deposit to a
   receivable with exact partial amounts and currency evidence during bank
   import/reconciliation, and explicit coordination with FSA claims when the same
   expense could be claimed through either.
3. **P0 — Windows installer.** Prioritized over further Linux packaging: a Linux
   development environment already installs BreadSched easily from source, while
   Windows users have no equivalent path. Provide a Windows installer with the GTK
   runtime and the same book/upgrade and file workflows; test clean installs,
   upgrades, launch, and uninstalls on supported Windows CI. Publish
   signed/checksummed artifacts and concise installation instructions only after
   their release gates are proven. The Flatpak manifest, its installed-sandbox CLI
   gate, desktop entry, AppStream metadata, icon, and sandboxed GTK smoke already
   run in CI; remaining Linux work (validating GTK file-chooser portals and
   printing inside the sandbox, and publishing the installer) follows the Windows
   installer. Keep wheel/source releases available throughout.
4. **P1 — Finish currency handling.** Manual exchange rates and disclosed as-of
   conversion across Plan, Expense Explorer, Projection, comparisons, and prints are
   delivered. Remaining: map imported exchange-rate and security-price quotes
   (GnuCash, OFX, QIF) through the same reviewed quote contract; decide an explicit
   multi-hop policy before enabling any conversion through a third currency; and
   value securities quoted in a non-reporting currency (a direct reporting-currency
   price is still required). Do not create a second monthly budget ledger.
5. **P2 — Interoperability and analysis.** Scope safe GnuCash write-back for
   simple user edits, broader reporting and spending-over-time charts, then
   scenario-aware pinned savings targets. Investigate AqBanking as an optional
   integration, and add transaction tags/attachments with private-data and
   portability controls. Detailed acceptance contracts follow below.


## Architecture and correctness

- Split oversized modules/functions as part of the service/resource ownership
  work, especially the remaining seams in `web/server.py` (for example transaction
  entry, register, and reconciliation handlers still parse JSON inline). Continue
  consolidating web control parsers where ownership is clear. Split large GUI test modules
  only when the resulting fixture ownership and runtime isolation improve; do not
  optimize for a line-count threshold alone.


## Register workflow

- Improve register appearance and information density while keeping account-type
  debit/credit terminology clear.

- Consider whether a categorization rule should also be able to set a payee, and
  whether split transactions can be supported with explicit per-split rules
  rather than a guess.

- Add transaction tags and optional attachments with search/filter/export support.
  Define book-relative storage, size/type limits, backup/restore and archive
  inclusion, privacy-safe diagnostics, and behavior on GnuCash re-import or
  missing external files. Do not imply GnuCash supports an unproven round trip.


## Plan and planning-flow reporting

- Ensure planning classifications feed Plan, Projection explanations, scenario
  comparison, and Dashboard consistently.

- Add savings goals as pinned, scenario-aware target events with dated
  contributions and a target date; explain progress separately from spendable cash
  and avoid counting transfers as expenses.

- Add a spending-over-time chart and broader reports with drill-down to the
  exact dated events, category hierarchy, selected scenario, as-of boundary,
  currency completeness, and matching printable/exported totals.

- Carry reimbursable expense and receivable status into Plan, Projection, and
  Dashboard liquidity: distinguish incurred expense, collectible asset, and
  expected dated cash receipt. A disputed or overdue claim must not be treated as
  spendable cash. Show gross cost and net household cost without double-counting
  reimbursements, including scenario changes and write-offs.


## Scheduled transactions and loans

- Continue widening safe editing only where complete split/recurrence/import
  semantics can be round-tripped without guessing.

- Consider additional custom recurrence patterns only when their occurrence
  identity, bounded generation, import mapping, editing, and round trip are all
  unambiguous.

- Add payroll templates and richer payroll editing.

- Consider richer formula assistance only on top of the existing safe formula
  language; never add Python `eval` or a second formula dialect.


## Investment and retirement modeling

- Add retirement drawdown behavior and dated scenario support for withdrawal
  patterns.

- Use per-account and dated account-specific return/rate assumptions throughout
  projection UI/comparison/explanations.

- Add investment lots and cost basis.

- **Cross-cutting multi-currency valuation and import.** Preserve exact source
  amounts and quote metadata, and never combine unlike currencies silently. The
  remaining work is listed under P1 item 2: reviewed import of exchange-rate and
  security-price quotes, an explicit multi-hop conversion policy, and securities
  quoted in a non-reporting currency.

- Add optional online quote retrieval with explicit provenance, staleness, and
  failure behavior; manual and imported quotes must remain usable offline.

- Keep deterministic projection as the normal model; any later Monte Carlo engine
  should remain a separate optional analysis.


## Projection and scenarios

- Add a custom per-schedule growth rate only if it can be explained cleanly within
  the scenario-assumption model.

- Improve projection caching/reuse without storing stale calculated scenario
  results.

- Add richer charts, cash-runway comparisons, and deeper account explanations.

- Polish Plan/Projection scenario comparisons.

- Evolve assumptions toward extensible dated rules (salary changes, retirement,
  pensions/Social Security, temporary expenses, mortgage payoff, changing return or
  inflation regimes) instead of hard-coded special cases.

- Expose the verified projection-conservation identity in user-facing detail:
  opening state + dated flows + interest/performance/assumption effects = closing
  state. Keep the engine invariant executable while making every term inspectable.


## FSA / benefit accounts and claims

- Generalize the existing claim linkage where appropriate for non-FSA
  reimbursements while retaining benefit-year rules only for FSA claims. Model
  receivable creation/settlement against balanced ledger splits or an explicit
  planning-only claim, with partial payments, payer identity, evidence, denial,
  correction, and write-off. Re-import must preserve BreadSched-owned claim links
  and reconciliation; test direct vendor credits, bank reimbursements, and
  insurer/employer payments as distinct flows.

- **Funding, direct payment, and indirect reimbursement flows.** Model payroll
  splits funding an FSA separately from benefit availability and medical expense.
  Cover direct FSA-to-medical-expense payments, FSA reimbursements through a bank
  account, and medical charges paid via bank/credit-card chains. Link the claim,
  payment, and reimbursement without counting expense or benefit usage twice;
  preserve service dates, funding-year attribution, refunds, and reconciliation.

- Improve Review suggestions and action explanations.

- Add stronger Dashboard alerts for claims needing attention.

- Handle over-reimbursement, reopened claims, late EOB changes, and correction
  workflows.

- Add claim/report views by account, funding year, provider, and status.

- Support plan-specific carryover rules where applicable.

- Improve import/reconciliation treatment of external FSA transactions.

- Expand claim/import automation without conflating benefit availability with the
  custodial account ledger balance.


## Import and GnuCash interoperability

- Extend CSV import mapping to optional payee, category, currency, and split
  columns. Reject ambiguous mappings rather than inventing ledger accounts or
  balancing splits.

- Extend the held-change review for locally reconciled transactions (#117) to
  GnuCash deletions. A source-deleted transaction is retained only while a
  BreadSched reconciliation or FSA claim refers to it, so one reconciled only in
  GnuCash is removed without review. Decide whether such deletions should be held
  with the same keep/use-GnuCash/decide-later choices.

- Define a narrow GnuCash write-back contract for simple supported edits only.
  Begin with an opt-in preview of exact source changes, source version/conflict
  checks, an independent backup, atomic write and read-back verification. Keep
  unsupported schedules, splits, reconciliation, and imported metadata read-only
  until round-trip fixtures prove preservation; never silently write to the
  source during normal import.

- Investigate AqBanking integration through a small optional adapter: supported
  platforms, consent and credential ownership, bank connection maintenance,
  transaction identity, failure/retry, and reconciliation against existing
  imports. Decide whether its dependency and packaging cost justify implementation
  before committing to a direct bank-link feature.

- Add OFX investment transactions.

- Add useful QIF investment/security records.

- Add reviewed commodity/security mapping where imported identifiers cannot be
  matched safely and extend price import to additional source formats where present.
  GnuCash re-import now matches namespace and mnemonic together, accepts equivalent
  currency namespaces, and rejects ambiguous bare mnemonic quote references;
  broader reviewed mappings remain open.

- Import scheduled transactions from additional formats where represented
  reliably.

- Expand duplicate/re-import tests, including cross-file duplicate heuristics.

- Improve import summaries/problem reporting.

- Cover richer transfer/category mapping and real-world QIF/OFX deviations.

- Continue representative GnuCash compatibility fixtures for accounts,
  transactions, reconciliation, commodities, schedules, formula loans, and unusual
  but valid structures.

- Investigate/cover older GnuCash SQLite timezone/date conventions.

- **Outbound interoperability and portable archives.** Define documented,
  loss-minimizing exports for supported household ledger/planning data and a
  versioned portable archival format with a human-readable manifest, integrity
  verification, provenance, re-import tests, and explicit disclosure of anything
  that cannot be represented. Preserve opaque imported structures where practical;
  do not claim GnuCash round-trip equivalence beyond demonstrated fixtures.


## Storage, integrity, and recovery

- **Evaluate normalized transaction/split source-of-truth storage without a
  big-bang rewrite.** Record an ADR and prototype the migration/query/round-trip
  consequences before changing the current blob-plus-derived-index design. The
  decision must compare normalized transaction and split tables with typed columns,
  foreign keys, and CHECK constraints against lossless unknown/imported fields,
  undo/redo, atomic writes, import refresh ownership, schema-evolution cost, and
  realistic performance. If normalization wins, introduce it through an explicit
  data-format migration with dual-representation verification during development;
  retain blobs only for opaque source extensions and document-shaped planning
  objects. Until then, `split_index` remains derived and must never silently diverge
  from its transaction blob.

- Longer term, separate planning resolutions/classifications from imported ledger
  records where doing so materially simplifies synchronization and ownership.


## GTK, web parity, and reporting

- Continue real GTK runtime testing for selections, dialogs, focus transitions,
  model replacement, multiple windows, and GTK API-version differences.

- **Run GTK tests safely in parallel.** Introduce bounded pytest-xdist concurrency
  only after each worker has isolated application IDs, settings, books/import files,
  display/session-bus resources where required, and GTK main-context lifecycle.
  Prove the suite is order-independent and free of process-global widget state;
  start with a conservative worker count and retain the serial GTK target as the
  deterministic diagnostic path.

- Improve first-run UX, preferences, actionable errors, icons/resources, and
  native desktop polish without moving financial logic into GUI code.

- **Low priority — Accessibility baseline.** Audit complete keyboard operation,
  logical focus order and visible focus, screen-reader names/relationships for
  controls and tables, text/UI scaling, and contrast. Add automated coverage where
  reliable and keep a short manual GTK checklist; accessibility remains required
  for release quality but is intentionally below the current financial workflows.

- Audit the remaining GTK views and dialogs for bounded natural sizes (the Dashboard
  group and card text were bounded for #140). Large content must scroll
  inside the current monitor work area, primary actions/window controls must remain
  reachable, and switching away from a large view must allow the main window to
  shrink again. Cover long notes, complex split editors, tables, and small-screen GTK
  behavior with runtime regressions.

- **Add native GTK printing.** Render the current structured Dashboard, Plan, and
  Projection state through a GTK-native/system print path without an HTML/browser
  intermediary. Share report layout inputs with existing output, paginate tables and
  notes with repeatable headings, support preview/printer/PDF destinations where the
  platform provides them, and keep the current HTML route as a compatibility fallback
  until the native path is available and tested on supported GTK runtimes.

## In-application help and documentation

- Add a guided, skippable first-run tour for opening/importing a book, reviewing
  the Dashboard's setup state, due items, category remaining, backup, and Plan.
  Use the same offline guide content, with contextual GTK/web entry points and
  no tutorial transactions written into a real book.

- Evolve the packaged Markdown user guide into a versioned `docs/` site if its
  proven information architecture would benefit from generator-backed navigation.
  Keep the guide usable as a standalone document and packaged for offline help; do
  not make a documentation generator a runtime requirement.

- Add a generic-household walkthrough that creates a comprehensive chart of
  accounts, recurring income/expenses, savings/debt/retirement flows, and Base plan.

- Add a multiple-scenario walkthrough that duplicates Base and compares alternate
  assumptions/scheduled estimates.

- Surface the packaged shared guide from the web interface and add contextual
  links from complex GTK/web workflows where they materially improve discovery.


## Packaging and release quality

- Deliver Flatpak and Windows installation per the prioritized acceptance contract.
  Keep macOS behavior isolated behind a small platform layer and evaluate a native
  macOS artifact after the Linux/Windows paths are reliable.

- Improve crash recovery, diagnostic logging, and privacy-safe error reporting.

- Add property-based monetary arithmetic tests and fuzz-style malformed-import
  tests where they add useful coverage.

- Keep documentation, versioning, and release notes synchronized with actual
  behavior.


## Project governance and community health

- Add `SECURITY.md`, privacy-aware issue forms, and `CODEOWNERS`; extend the
  initial pull-request template as contribution patterns emerge. Security and field-
  report forms must repeat the existing prohibition on uploading unsanitized
  financial books and provide a private vulnerability-reporting route. Add a code of
  conduct when the project is ready to invite a broader contributor community rather
  than copying one without an enforcement/contact plan.
