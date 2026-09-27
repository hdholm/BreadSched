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

1. **P0 — Installable Linux and Windows builds.** A GNOME-runtime Flatpak manifest
   and installed-sandbox offline CLI gate now cover sample creation, verification,
   Dashboard, CSV export, QIF import, backup, restore, and competing-writer locks
   under Documents access. The Flatpak now installs a validated desktop entry,
   AppStream metadata, and icon, and a sandboxed GTK smoke covers offline views,
   help, icon resolution, and settings persistence. No installer is published yet.
   Validate GTK file-chooser portals and printing inside the sandbox. Provide a Windows
   installer with GTK runtime and the same book/upgrade and file workflows; test
   clean installs, upgrades, launch, and uninstalls on supported Windows CI.
   Publish signed/checksummed artifacts and concise installation instructions
   only after their release gates are proven. Keep wheel/source releases available.
2. **P1 — Finish currency handling.** GTK, web, and CLI now share manual FX
   entry through the exact quote contract. Plan totals, category detail, cash
   position, prints, and Expense Explorer Remaining/rollover convert once at the
   as-of quote and disclose quote date, source, and inversion, or list excluded
   amounts. Projection events, opening balances, and scenario comparisons use
   the same policy at the opening valuation date. Notes state each quote's age
   (without imposing a cutoff) and that conversion is not rounded to cents
   before aggregation. Continue imported quote mapping and decide multi-hop policy explicitly before enabling
   it; do not create a second monthly budget ledger.
3. **P1 — Payees, reviewed rules, and CSV import.** Introduce stable payee identity
   without rewriting imported descriptions; preview deterministic matching and
   categorization suggestions before acceptance. The user-mapped CSV importer
   (shared service and `breadsched import-csv`) now previews rows, validates
   date/amount/encoding, holds back possible duplicates, keeps a stable source
   identity for re-import without recategorizing, and imports as one undo step.
   The web Import view and the GTK Import CSV Statement dialog map, preview, and
   import CSV through the same service. Transfer review offers a row as the other
   side of an uncategorized transaction already imported into another account and
   links it only on explicit acceptance. Payees (native schema 8) are stable,
   renameable identities separate from descriptions; exact normalized-description
   keys propose a payee for unassigned transactions, and nothing is assigned until
   the user accepts (`breadsched payees`). Next: GTK and web payee review, then
   reviewed categorization rules. Keep rule priority and conflicts
   explainable; never silently recategorize previously accepted transactions.
   Offer entry autocomplete from earlier transactions as a visible, editable
   proposal that never commits without the user's save.
4. **P1 — Reimbursable expenses and receivables.** Track an expense and the
   amount owed by an insurer, employer, or other payer as linked but distinct
   facts. Show open, partial, disputed, written-off, and settled receivables,
   their ages and expected cash dates, without counting a reimbursement as new
   income or erasing the original expense. Reconcile deposits to claims with
   exact partial amounts, refunds, and currency evidence; coordinate with FSA
   claims and preserve imported ledger splits.
5. **P2 — Interoperability and analysis.** Scope safe GnuCash write-back for
   simple user edits, broader reporting and spending-over-time charts, then
   scenario-aware pinned savings targets. Investigate AqBanking as an optional
   integration, and add transaction tags/attachments with private-data and
   portability controls. Detailed acceptance contracts follow below.


## Architecture and correctness

- Split oversized modules/functions as part of the service/resource ownership
  work, especially the remaining seams in `web/server.py`. Expense Explorer,
  Plan detail, Plan, Dashboard, scenario-management listing, and Projection month,
  summary, and comparison responses have dedicated read-only resource adapters.
  Continue consolidating the remaining web control parsers where ownership is clear.
  Fixed baseline and scenario schedule writes now have a typed-request web adapter;
  continue consolidating shared web control parsers where that improves ownership.
  Continue with other workflows when responsibility boundaries
  are clear. Split large GUI test modules
  only when the resulting fixture ownership and runtime isolation improve; do not
  optimize for a line-count threshold alone.


## Register workflow

- Improve register appearance and information density while keeping account-type
  debit/credit terminology clear.

- Add payees as first-class, reviewable transaction metadata; keep imported
  descriptions and source identifiers intact. Let matching and categorization
  rules propose category/payee assignments with preview, ordering, conflict
  explanations, and explicit acceptance. Handle transfers and split transactions
  without a guessed category or historical rewrite.

- Add entry autocomplete in GTK and web quick entry and the full editor. Typing a
  description (later, a payee) proposes the most recent matching transaction's
  transfer account, splits, and amount from a shared service; the user can accept,
  edit, or ignore every proposed field before an ordinary balanced save. Do not
  copy reconcile state, source identifiers, notes, Plan links, or FSA claims.
  Define matching, ordering, and multi-currency behavior, and cover it in tests.

- Add transaction tags and optional attachments with search/filter/export support.
  Define book-relative storage, size/type limits, backup/restore and archive
  inclusion, privacy-safe diagnostics, and behavior on GnuCash re-import or
  missing external files. Do not imply GnuCash supports an unproven round trip.


## Plan and planning-flow reporting

- Ensure planning classifications feed Plan, Projection explanations, scenario
  comparison, and Dashboard consistently.

- Per-category remaining amounts and optional rollover are delivered through the
  event-derived Expense Explorer period contract. A savings goal can later be a
  pinned, scenario-aware target event with dated contributions and target date;
  explain progress separately from spendable cash and avoid counting transfers
  as expenses.

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

- **Cross-cutting multi-currency valuation and import.** Add automatic/manual
  exchange-rate and security-price handling across imported commodities, ledger
  valuation, Projection, Plan/reporting, and comparisons. Preserve exact source
  amounts and quote metadata; make reporting currency, quote source/date,
  staleness, missing-price behavior, conversion path, and rounding explainable;
  never combine unlike currencies in net worth silently.
  Account views now disclose the selected security quote's source/date or an
  explicit missing reporting-currency quote with ledger fallback. Continue with
  exchange-rate paths and cross-report conversion semantics.
  An internal direct as-of currency conversion result now keeps exact amounts,
  quote provenance, and explicit missing-quote state. Ordinary account balances
  now use direct quotes where available and disclose a tagged ledger fallback.
  Account chart, cash, and net-worth totals now require complete reporting-currency
  valuations and disclose missing quote accounts rather than adding fallback values.
  Exact inverse pair quotes now supply ordinary account valuation only when no
  eligible direct quote exists, with inversion disclosed in account views.
  An exact manual FX quote save contract now validates the currency pair and
  preserves imported quotes. CLI and web rate entry accept a dated directional
  rate for known currencies, and the GTK Accounts dialog uses the same contract.
  Scheduled Plan occurrences now carry their transaction currency; actual matching
  excludes unlike currencies and occurrence variance is unavailable when a linked
  actual uses another currency. Complete currency conversion of Plan period totals,
  scenario estimates, Projection flows, and comparisons remains to be done.
  Account views now disclose quote age relative to their valuation date; there
  is no automatic age cutoff, so an old quote remains usable and visible.
  Dashboard configured groups and liquidity now suppress dependent figures on
  missing quotes across GTK, web, CLI, and print, while leaving unrelated bills
  and income visible. Extend complete-total semantics to Plan, Projection,
  comparisons, and their printable reports; review multi-hop policy, quote-age
  disclosure on future flows, and rounding across totals. Security prices still
  require a direct reporting-currency quote.

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

- Add reviewed CSV import using explicit column mapping and preview for date,
  amount/sign, account, payee, category, memo, currency, and split/transfer cases.
  Reject ambiguous mappings rather than inventing ledger accounts or balancing
  splits; preserve source rows and stable re-import identity for duplicate review.

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

- **Expand the migration window with the next data-format change.** When schema 8
  or the next schema is introduced, retain sequential migrations from the two
  immediately preceding data-format versions (for schema 8, both 6→7 and 7→8), so
  the current application accepts current-format books plus those two predecessor
  formats. Keep versioned fixtures for every supported starting format and prove
  direct open, sequential migration, one pre-migration backup, rollback, ledger
  evidence, and rejection outside the advertised window. Do not create a no-op
  schema bump merely to enact this policy.

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

- Audit GTK views and dialogs for bounded natural sizes. Large content must scroll
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
