# BreadSched roadmap

This file is the **single authoritative source for unfinished BreadSched work**.
Completed milestones are retained in [`CHANGELOG.md`](CHANGELOG.md).

The current released application baseline is **`v0.2.0a86`**.


## Product direction

BreadSched is intended to become a **general household-finance application**
over the longer term, not a clone of GNUCash.  Although it is intended to have
many similar features to GNUcash it is not anticipated to have any of the
business oriented features, and intends to implement more personal finance
features than GNUcash. It should cover household
accounts/registers, reconciliation, scheduled transactions, planning, scenarios,
projection, investments/retirement, and common imports while leaving business
accounting features out of scope.

Until that household feature set is sufficiently complete, **GnuCash compatibility
is a first-class requirement**. GTK4 is the canonical interface and Linux is the
primary native desktop target; the web interface remains a supported parity surface.
Cross-cutting workflow logic belongs in shared services rather than presentation code.


## Immediate priorities

The remaining priority is **Responsibility-based decomposition.** Continue
reducing the remaining oversized `web/server.py` and related seams as the
work above touches them; do not optimize for line count alone.


## Architecture and correctness

- Split oversized modules/functions as part of the service/resource ownership
  work, especially the remaining seams in `web/server.py`. The read-only Expense
  Explorer response has its own resource adapter; continue with Plan detail and
  other workflows when responsibility boundaries are clear.
  Plan detail and the Plan response now have dedicated read-only resource adapters.
  Fixed baseline and scenario schedule writes now have a typed-request web adapter;
  continue consolidating shared web control parsers where that improves ownership.
  Continue with other workflows when responsibility boundaries
  are clear. Split large GUI test modules
  only when the resulting fixture ownership and runtime isolation improve; do not
  optimize for a line-count threshold alone.


## Register workflow

- Improve register appearance and information density while keeping account-type
  debit/credit terminology clear.


## Plan and planning-flow reporting

- Ensure planning classifications feed Plan, Projection explanations, scenario
  comparison, and Dashboard consistently.


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
  exchange-rate paths, staleness policy, and cross-report conversion semantics.

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

- Add OFX investment transactions.

- Add useful QIF investment/security records.

- Add reviewed commodity/security mapping where imported identifiers cannot be
  matched safely and extend price import to additional source formats where present.

- Import scheduled transactions from additional formats where represented
  reliably.

- Expand duplicate/re-import tests, including cross-file duplicate heuristics.

- Improve import summaries/problem reporting.

- Add browser-native file upload to the web import view

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

- Finish cross-platform packaging/release workflows, Linux first, with Windows/
  macOS behavior isolated behind small platform-specific layers. Evaluate Flatpak as
  the primary GTK/Linux artifact and prove portals, file import/export, printing,
  settings, backups, and offline operation inside the sandbox before selecting it.

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
