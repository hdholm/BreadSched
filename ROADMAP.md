# BreadSched roadmap

This file is the **single authoritative source for unfinished BreadSched work**.
Completed milestones and their durable acceptance contracts are retained in
[`CHANGELOG.md`](CHANGELOG.md).

**Maintenance rule:** every pull request that completes, changes, discovers, splits,
or reprioritizes roadmap work must update this file. Completed items move to the
changelog in the same pull request.

The current accepted baseline is **0222 — web resource split and boundary
hardening**.
Unchecked field reports are requests or suspected regressions, not
claims that a root cause has already been confirmed.

## Product direction

BreadSched is intended to become a **household-finance replacement for GnuCash**
over the longer term, not a business-accounting clone. It should cover household
accounts/registers, reconciliation, scheduled transactions, planning, scenarios,
projection, investments/retirement, and common imports while leaving business
accounting features out of scope.

Until that household feature set is sufficiently complete, **GnuCash compatibility
is a first-class requirement**. GTK4 is the canonical interface and Linux is the
primary native desktop target; the web interface remains a supported parity surface.
Cross-cutting workflow logic belongs in shared services rather than presentation code.

## Immediate priorities

The web-boundary architecture gate is complete. Continue moving cross-interface
workflows behind typed services before broadening financial semantics; a touched
workflow may not add new presentation-owned rules.

1. **0223 — Service adoption, structured errors, and localization seam.**
   - [x] Put transaction creation/editing, split reconstruction, investment
     validation, hidden-account retention, and optional claim attachment behind one
     typed, atomic service used by GTK and web. Preserve native round trips and
     imported split metadata; enforce the adapter boundary with architecture tests.
   - [x] Route reconciliation start, selection/balance updates, completion,
     cancellation, and reopening through typed services with stable codes and field
     paths while retaining the shared exact calculation and atomic ledger/audit
     writes.
   - [x] Put FSA claim construction, allocation/link/rejection validation, saving,
     and deletion behind typed services used by GTK and web. Keep linked split
     classification and claim persistence atomic, with stable validation codes.
   - [ ] Migrate the remaining cross-interface mutations in coherent slices.
     Loan creation, imports, Review, saved-scenario lifecycle, projection assumptions,
     transaction adapters, and account lifecycle/settings are complete; continue
     with the next coherent mutation slice. A
     touched cross-interface workflow may not add new presentation-owned financial
     rules.
   - Centralize error codes, field paths, and presentation-message mapping. Introduce
     gettext only after English service prose has ceased to be an API, then add
     extraction/catalog checks and locale smoke tests for GTK and web.
   - Decompose the oversized verification, category-report, estimate, and dialog
     functions by responsibility while moving their rules; line count is a signal,
     not an acceptance test. Preserve behavior with characterization tests before
     structural edits.

2. **0224 — Commodity-tagged amounts and scalar rates.** Complete the amount work
   below before foreign exchange, lots, or deeper investment modeling.

3. **0225 — Wider data-format compatibility and release discipline.** Apply the
   version/migration policy below when the next native format change is needed, and
   begin tagged releases with human-readable release notes.

## Architecture and correctness

- [ ] Harden `Money` and amount handling:
  - [ ] Remove hard-coded cents where account/commodity precision differs.
  - [ ] Keep `Money` as the exact rational scalar used to preserve GnuCash numerics;
    introduce a commodity-tagged `Amount(value, commodity)` at ledger/service
    arithmetic boundaries. Reject addition, comparison, and netting across unlike
    commodities unless an explicit dated conversion has produced a reporting-
    currency amount.
  - [ ] Preserve the distinct split dimensions: transaction-currency `value` and
    account-commodity `quantity`. Do not replace them with one ambiguous amount.
  - [ ] Replace `Rate`'s `Decimal` subclassing, or override its complete arithmetic
    surface, so operations cannot silently decay to an untyped `Decimal`; add static
    and runtime closure tests.

- [ ] Bound formula resources before parsing/evaluation: cap normalized and raw input
  length, use an explicit local `Decimal` context with precision and exponent limits,
  normalize all resource failures to `FormulaError`, and test adversarial bases,
  exponents, nesting, and imported formulas without rejecting representative GnuCash
  loan expressions.

- [ ] Split oversized modules/functions as part of the service/resource ownership
  work, especially `web/server.py`, `verify_domain`, `build_category_report`,
  `propose_historical_estimates`, and the account/schedule dialog constructors.
  Split large GUI test modules only when the resulting fixture ownership and runtime
  isolation improve; do not optimize for a line-count threshold alone.

## Independent acceptance evidence and test quality

- [ ] Add a Linux CI job with PyGObject installed but the GTK4 typelib deliberately
  absent. Keep `tests/test_launcher.py` in the core suite, make its “PyGObject exists”
  probe distinguish an importable GTK4 namespace, and prove help/version plus a real
  launch attempt report the missing-runtime condition without collection failure or
  traceback.

- [ ] Add small, human-reviewed golden books for Plan and Projection. Store the
  financial assumptions and hand-calculated expected dated flows, balances, and
  conservation terms beside each synthetic fixture so expected results are not
  generated by the implementation under test. Keep goldens reviewable and generic;
  do not substitute large captured user books.

- [ ] Add a bounded mutation-testing gate for `gen/engine` and `gen/lib`. Establish a
  measured baseline first, exclude equivalent/platform-only mutants explicitly, and
  ratchet the score in CI rather than imposing an arbitrary pass percentage that
  makes the suite slow or flaky.

- [ ] Consolidate historical-estimate thresholds, confidence weights, spike rules,
  cadence tolerances, seasonal criteria, and funding tie-breaks into immutable,
  documented rule objects passed to the engine. Preserve conservative defaults,
  expose the applied rule-set/version in structured evidence, and test rule changes
  against the independent goldens instead of scattering numeric constants through
  inference code.


## Register workflow

- [ ] Improve register appearance and information density while keeping account-type
  debit/credit terminology clear.

## Plan and planning-flow reporting

- [ ] Ensure planning classifications feed Plan, Projection explanations, scenario
  comparison, and Dashboard consistently.


## Scheduled transactions and loans

- [ ] Continue widening safe editing only where complete split/recurrence/import
  semantics can be round-tripped without guessing.

- [ ] Consider additional custom recurrence patterns only when their occurrence
  identity, bounded generation, import mapping, editing, and round trip are all
  unambiguous.

- [ ] Add payroll templates and richer payroll editing.

- [ ] Consider richer formula assistance only on top of the existing safe formula
  language; never add Python `eval` or a second formula dialect.


## Investment and retirement modeling

- [ ] Add retirement drawdown behavior and dated scenario support for withdrawal
  patterns.

- [ ] Use per-account and dated account-specific return/rate assumptions throughout
  Projection UI/comparison/explanations.

- [ ] Add investment lots and cost basis.

- [ ] **Cross-cutting multi-currency valuation and import.** Add automatic/manual
  exchange-rate and security-price handling across imported commodities, ledger
  valuation, Projection, Plan/reporting, and comparisons. Preserve exact source
  amounts and quote metadata; make reporting currency, quote source/date,
  staleness, missing-price behavior, conversion path, and rounding explainable;
  never combine unlike currencies in net worth silently.

- [ ] Add optional online quote retrieval with explicit provenance, staleness, and
  failure behavior; manual and imported quotes must remain usable offline.

- [ ] Keep deterministic projection as the normal model; any later Monte Carlo engine
  should remain a separate optional analysis.


## Projection and scenarios

- [ ] Add a custom per-schedule growth rate only if it can be explained cleanly within
  the scenario-assumption model.

- [ ] Improve projection caching/reuse without storing stale calculated scenario
  results.

- [ ] Add richer charts, cash-runway comparisons, and deeper account explanations.

- [ ] Polish Plan/Projection scenario comparisons.

- [ ] Evolve assumptions toward extensible dated rules (salary changes, retirement,
  pensions/Social Security, temporary expenses, mortgage payoff, changing return or
  inflation regimes) instead of hard-coded special cases.

- [ ] Expose the verified projection-conservation identity in user-facing detail:
  opening state + dated flows + interest/performance/assumption effects = closing
  state. Keep the engine invariant executable while making every term inspectable.


## FSA / benefit accounts and claims

- [ ] **Funding, direct payment, and indirect reimbursement flows.** Model payroll
  splits funding an FSA separately from benefit availability and medical expense.
  Cover direct FSA-to-medical-expense payments, FSA reimbursements through a bank
  account, and medical charges paid via bank/credit-card chains. Link the claim,
  payment, and reimbursement without counting expense or benefit usage twice;
  preserve service dates, funding-year attribution, refunds, and reconciliation.


- [ ] Improve Review suggestions and action explanations.

- [ ] Add stronger Dashboard alerts for claims needing attention.

- [ ] Handle over-reimbursement, reopened claims, late EOB changes, and correction
  workflows.

- [ ] Add claim/report views by account, funding year, provider, and status.

- [ ] Support plan-specific carryover rules where applicable.

- [ ] Improve import/reconciliation treatment of external FSA transactions.

- [ ] Expand claim/import automation without conflating benefit availability with the
  custodial account ledger balance.


## Import and GnuCash interoperability

- [ ] Add OFX investment transactions.

- [ ] Add useful QIF investment/security records.

- [ ] Add reviewed commodity/security mapping where imported identifiers cannot be
  matched safely and extend price import to additional source formats where present.

- [ ] Import scheduled transactions from additional formats where represented
  reliably.

- [ ] Expand duplicate/re-import tests, including cross-file duplicate heuristics.

- [ ] Improve import summaries/problem reporting.

- [ ] Add browser-native file upload to the web import view; 0143 provides functional parity through local paths visible to the BreadSched process.

- [ ] Cover richer transfer/category mapping and real-world QIF/OFX deviations.

- [ ] Continue representative GnuCash compatibility fixtures for accounts,
  transactions, reconciliation, commodities, schedules, formula loans, and unusual
  but valid structures.

- [ ] Investigate/cover older GnuCash SQLite timezone/date conventions.

- [ ] **Outbound interoperability and portable archives.** Define documented,
  loss-minimizing exports for supported household ledger/planning data and a
  versioned portable archival format with a human-readable manifest, integrity
  verification, provenance, re-import tests, and explicit disclosure of anything
  that cannot be represented. Preserve opaque imported structures where practical;
  do not claim GnuCash round-trip equivalence beyond demonstrated fixtures.


## Storage, integrity, and recovery

- [ ] **Application version and native data-format version are independent.** The
  package/application version (currently the `0.2.0aN` series) identifies the build
  for bug reports and releases. The integer native schema/data-format version
  (currently 7) alone controls book compatibility and migration. Display and
  diagnostic output should report both; a behavior-only application release must not
  bump the data format, and a data-format change must bump the schema even if the
  application remains in the same prerelease series.

- [ ] **Expand the migration window with the next data-format change.** When schema 8
  or the next schema is introduced, retain sequential migrations from the two
  immediately preceding data-format versions (for schema 8, both 6→7 and 7→8), so
  the current application accepts current-format books plus those two predecessor
  formats. Keep versioned fixtures for every supported starting format and prove
  direct open, sequential migration, one pre-migration backup, rollback, ledger
  evidence, and rejection outside the advertised window. Do not create a no-op
  schema bump merely to enact this policy.

- [ ] **Evaluate normalized transaction/split source-of-truth storage without a
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

- [ ] Longer term, separate planning resolutions/classifications from imported ledger
  records where doing so materially simplifies synchronization and ownership.


## GTK, web parity, and reporting

- [ ] Continue real GTK runtime testing for selections, dialogs, focus transitions,
  model replacement, multiple windows, and GTK API-version differences.

- [ ] **Run GTK tests safely in parallel.** Introduce bounded pytest-xdist concurrency
  only after each worker has isolated application IDs, settings, books/import files,
  display/session-bus resources where required, and GTK main-context lifecycle.
  Prove the suite is order-independent and free of process-global widget state;
  start with a conservative worker count and retain the serial GTK target as the
  deterministic diagnostic path.

- [ ] Improve first-run UX, preferences, actionable errors, icons/resources, and
  native desktop polish without moving financial logic into GUI code.

- [ ] **Low priority — Accessibility baseline.** Audit complete keyboard operation,
  logical focus order and visible focus, screen-reader names/relationships for
  controls and tables, text/UI scaling, and contrast. Add automated coverage where
  reliable and keep a short manual GTK checklist; accessibility remains required
  for release quality but is intentionally below the current financial workflows.

- [ ] Audit GTK views and dialogs for bounded natural sizes. Large content must scroll
  inside the current monitor work area, primary actions/window controls must remain
  reachable, and switching away from a large view must allow the main window to
  shrink again. Cover long notes, complex split editors, tables, and small-screen GTK
  behavior with runtime regressions.

- [ ] **Add native GTK printing.** Render the current structured Dashboard, Plan, and
  Projection state through a GTK-native/system print path without an HTML/browser
  intermediary. Share report layout inputs with existing output, paginate tables and
  notes with repeatable headings, support preview/printer/PDF destinations where the
  platform provides them, and keep the current HTML route as a compatibility fallback
  until the native path is available and tested on supported GTK runtimes.

## In-application help and documentation

- [ ] Move task-oriented user documentation into a versioned `docs/` site with link
  and build checks (evaluate MkDocs, but do not couple content to a generator before
  the information architecture is proven). Trim the README to product orientation,
  installation, a first-run path, safety/recovery essentials, and links. Keep
  `DESIGN.md` as the current architecture description; add short ADRs for new
  consequential decisions instead of mechanically converting historical prose.

- [ ] Build full user documentation and in-application help for Accounts, registers,
  Scheduled transactions, Plan, Review/Actuals, Projection, scenarios, account types,
  imports, reconciliation, and backup/recovery.

- [ ] Add a generic-household walkthrough that creates a comprehensive chart of
  accounts, recurring income/expenses, savings/debt/retirement flows, and Base plan.

- [ ] Add a multiple-scenario walkthrough that duplicates Base and compares alternate
  assumptions/scheduled estimates.

- [ ] Explain why BreadSched models budgets as dated planned events rather than
  arbitrary monthly cells, with annual/weekly/semi-monthly examples.

- [ ] Explain how actual transactions resolve planned/scheduled occurrences while
  preserving expected date/amount for variance history.

- [ ] Explain commitments vs estimates, convergent historical estimation, planning
  account types/split purposes, GnuCash compatibility, growth policies, formula schedules,
  and Projection explanations.

- [ ] Prefer versioned shared help content that GTK and web can both surface where
  practical; add smoke/link tests against stale views/terminology.


## Packaging and release quality

- [ ] Finish cross-platform packaging/release workflows, Linux first, with Windows/
  macOS behavior isolated behind small platform-specific layers. Evaluate Flatpak as
  the primary GTK/Linux artifact and prove portals, file import/export, printing,
  settings, backups, and offline operation inside the sandbox before selecting it.

- [ ] Tag releases from accepted `main`, publish release notes that state both the
  application version and native data-format version/compatibility window, attach
  verified artifacts, and document upgrade/rollback implications. Tags must follow
  tested commits rather than merely marking every alpha code increment.

- [ ] Improve crash recovery, diagnostic logging, and privacy-safe error reporting.

- [ ] Keep Ruff, mypy, randomized tests, GTK runtime tests, end-to-end demo, and
  package build/install checks as release gates.

- [ ] Add property-based monetary arithmetic tests and fuzz-style malformed-import
  tests where they add useful coverage.

- [ ] Keep documentation, versioning, and release notes synchronized with actual
  behavior.

## Project governance and community health

- [ ] Add `SECURITY.md`, privacy-aware issue forms, and `CODEOWNERS`; extend the
  initial pull-request template as contribution patterns emerge. Security and field-
  report forms must repeat the existing prohibition on uploading unsanitized
  financial books and provide a private vulnerability-reporting route. Add a code of
  conduct when the project is ready to invite a broader contributor community rather
  than copying one without an enforcement/contact plan.

- [ ] Retain the present documentation roles instead of requiring all four principal
  documents to change in every PR: update only the files whose user behavior,
  architecture, pending work, or completed acceptance contract changed. Do not
  replace milestone acceptance contracts with a label-generated changelog; release
  tooling may assemble notes from those reviewed contracts and PR metadata.
