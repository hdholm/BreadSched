# BreadSched roadmap

This file is the **single authoritative source for unfinished BreadSched work**.
Completed milestones and their durable acceptance contracts are retained in
[`CHANGELOG.md`](CHANGELOG.md).

**Maintenance rule:** every pull request that completes, changes, discovers, splits,
or reprioritizes roadmap work must update this file. Completed items move to the
changelog in the same pull request.

The current accepted baseline is **0219 — category-specific inference**.
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

The next focused slice is **commodity precision**: remove assumptions that every
account and commodity uses cents before expanding valuation and multi-currency work.

## Architecture and correctness

- [ ] Harden `Money` and amount handling:
  - [ ] Remove hard-coded cents where account/commodity precision differs.

- [ ] Split oversized modules where it improves ownership/testability, especially web
  routing and very large GUI test modules.


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

- [x] **Rolling alpha storage compatibility.** Restore and retain the explicit
  migration registry and ledger, transactional migration runner, verified
  pre-migration backup hook, and versioned fixtures. During the current
  limited alpha, each release need only migrate a book from the immediately
  preceding alpha format because alpha users are expected to update every release.
  Do not remove the infrastructure after an individual migration expires: beta and
  stable releases will require a wider compatibility window, and weakening the
  sequential-update assumption must be a policy change rather than an emergency
  reconstruction.

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
  macOS behavior isolated behind small platform-specific layers.

- [ ] Improve crash recovery, diagnostic logging, and privacy-safe error reporting.

- [ ] Keep Ruff, mypy, randomized tests, GTK runtime tests, end-to-end demo, and
  package build/install checks as release gates.

- [ ] Add property-based monetary arithmetic tests and fuzz-style malformed-import
  tests where they add useful coverage.

- [ ] Keep documentation, versioning, and release notes synchronized with actual
  behavior.
