# BreadSched roadmap

This file is the **single authoritative source for future BreadSched work**. Project
plans discussed during development belong here rather than only in chat history.

**Maintenance rule:** every patch that completes, changes, discovers, splits, or
reprioritizes roadmap work must update this file in the same patch.

Status is current through patch **0143 — `import: expose ambiguous format choices`**.

## Status legend

- [x] Completed and expected to remain true.
- [ ] Remaining work.
- **NEXT** marks the highest-priority unfinished work.

## Product direction

BreadSched is intended to become a **household-finance replacement for GnuCash**
over the longer term, not a business-accounting clone. It should eventually cover
the household workflows needed for day-to-day use — accounts/registers,
reconciliation, scheduled transactions, planning, scenarios, projection,
investments/retirement, and common household imports — while deliberately leaving
GnuCash's business-oriented accounting features out of scope.

Until that household feature set is sufficiently complete, **GnuCash compatibility
is a first-class requirement**. Users must be able to keep GnuCash as the ledger
system of record while using BreadSched's planning, scenario, projection, FSA, and
analysis features without losing imported semantics or BreadSched-owned work on
re-import. Compatibility is transitional architecture, not a permanent limitation.

**Interface decision:** GTK4 is the canonical/reference interface and Linux is the
primary native desktop target. The web interface remains a supported parity
interface for financial behavior and major workflows. Windows and macOS may
eventually be served either by packaged GTK4 or by a web-based desktop/view layer,
but that must not demote GTK4 or permit different financial semantics.

Cross-cutting workflow logic should increasingly live in shared application/service
operations used by GTK, web, and CLI. UI parity means shared use cases and financial
semantics, not duplicate business rules in presentation code.

# 1. Architecture-hardening phase

## Completed hardening milestones

- [x] **0127 — Formula-loan economic consistency.** Formula-driven schedules are not
  escalated by generic expense inflation, and liabilities whose interest is already
  represented by formula schedules do not also receive generic liability interest.
- [x] **0128–0129 — Explicit schedule growth policy.** Baseline and scenario schedules
  persist `auto`, `none`, `income`, or `inflation`; mixed gross-to-net payroll grows
  correctly in automatic mode; GTK and web expose the same policy controls.
- [x] **0130 — Recurrence occurrence numbering.** Formula period numbers are carried
  from nominal occurrence generation and are not re-derived from weekend-adjusted
  dates. Semi-monthly and adjusted schedules retain the correct ordinal. The old
  10,000-step `next_after()` cutoff is removed.
- [x] **0131 — Start-of-period loan mathematics.** `due=1` `ipmt()`/`ppmt()` behavior
  matches independent annuity-due reference values and repays correctly.
- [x] **0132 — Formula evaluator hardening.** Fractional powers work; normal function
  commas are not mistaken for GnuCash grouping commas; expression depth/node/power
  complexity is bounded; evaluator failures normalize to `FormulaError`.
- [x] **0133 — Local web security.** Loopback Host/Origin validation, per-server API
  token, and JSON-only writes protect the web parity surface from hostile local
  browser requests and DNS-rebinding-style access.
- [x] **0134 — Re-import ownership boundary.** GnuCash-owned account/transaction fields
  may refresh while BreadSched-owned planning, projection, FSA, resolution, notes,
  and split classifications survive stable-GUID re-import.
- [x] **0135 — Incremental write verification.** Normal commit/undo/redo validates
  changed records and reverse references instead of scanning the whole book. Full
  `verify_book()` remains the exhaustive diagnostic.
- [x] **0136 — Transactional metadata/FSA claims.** Direct metadata commits cannot
  escape an active `DbTxn`; transactional metadata participates in undo/redo; FSA
  claims are first-class persisted records migrated from legacy metadata and save
  atomically with reimbursement split classifications.
- [x] **0138 — First expanded quality gates.** A dedicated serial performance gate
  measures single-write cost on a synthetic 30,000-transaction household book;
  Hypothesis-based recurrence properties exercise randomized intervals, dates,
  weekend adjustments, serialization, and occurrence identity. Core xdist runs
  exclude the dedicated performance marker, which executes once in CI.

## Remaining hardening work

- [ ] **NEXT — Finish quality-gate expansion.**
  - [ ] Extend mypy coverage to CLI and web, then GUI with an explicit baseline where
    necessary. `make typecheck-extended` now exposes the CLI/web audit scope; reduce
    those errors before moving it into the mandatory `make check` gate.
  - [ ] Eliminate current formatting debt, then add `ruff format --check` to the
    mandatory local/CI gate. A `make format-check` audit target exists now.
  - [x] Keep Host/Origin/token web-security regressions in the standard gate.
  - [x] Add property-based recurrence tests around generated occurrence identity,
    weekend adjustment, and serialization. Continue adding properties as new
    collision/edge cases are discovered.
  - [x] Add a realistic storage performance gate: one ordinary commit on a synthetic
    30,000-transaction book must remain below a deliberately generous 0.5-second
    budget. Add projection/report benchmarks separately as those paths are hardened.
  - [x] Continue pytest-xdist rollout: core/non-GTK uses `pytest -n auto`; GTK stays
    serial and the realistic performance gate runs separately with `-n 0`.
  - [x] Preserve `make test-ordered` / `pytest -n 0` as a deterministic diagnostic path.
- [x] **0139 — Repair stale legacy-budget CLI lookups.** Documented `activity`,
  `plan-unresolved`, and `plan-matches` budget filters use the supported database
  iteration API instead of a nonexistent `get_budget_by_name` method, with CLI
  regressions covering the documented options.
- [ ] Continue reducing CLI/web typing debt and move the extended mypy audit into the
  mandatory gate once the scope is clean or has a narrowly documented baseline.
- [ ] Harden `Money` and amount handling:
  - [x] **0140 — Core parsing/comparison safety.** Ambiguous locale-formatted strings are rejected instead of silently mis-scaled; equality/hash behavior follows Python's numeric contract; non-numeric equality does not raise.
  - [x] **0141 — QIF/OFX number-format parsing.** Detect period-vs-comma decimal conventions from the complete import file, parse grouping explicitly, reject conflicting conventions, and allow an explicit importer override for ambiguous files.
  - [x] **0142 — QIF date-order parsing.** Detect month-first versus day-first ordering from complete-file evidence, reject conflicting evidence, preserve year-first dates, and allow an explicit importer override for all-ambiguous files.
  - [ ] Extend explicit locale-aware amount parsing to GTK/web user-entry boundaries rather than asking the core `Money` constructor to guess locale.
  - [ ] Remove dimensionally meaningless `Money * Money` behavior as a distinct rate type is introduced.
  - [ ] Introduce a distinct rate concept.
  - [ ] Remove hard-coded cents where account/commodity precision differs.
  - [ ] Define commodity/currency-safe arithmetic and valuation boundaries.
- [ ] Add platform-correct settings/default-book paths and warnings for unsafe synced
  locations where SQLite durability is questionable.
- [ ] Add an inter-process book lock with clear read-only/failure behavior when a
  second writer attempts to open the same book.
- [ ] Finish the deliberate legacy Budget-domain migration after mapping every
  remaining CLI/Dashboard/cash-flow/Projection dependency.
- [ ] Move long-running Projection/import work off the GTK main thread, with a
  read-only worker connection, `GLib.idle_add` result delivery, cancellation, and a
  deliberate WAL/recovery policy.
- [ ] Split oversized modules where it improves ownership/testability, especially web
  routing and very large GUI test modules.

# 2. Completed product capabilities to preserve

## Planning, scenarios, and projection

- [x] Plan is derived from actual, scheduled, and estimated **dated** transactions,
  not stored monthly budget cells.
- [x] Base and saved scenarios support dated assumptions and scenario-specific
  estimate add/alter/suppress behavior.
- [x] Accepted historical estimates count as already planned activity when analysis
  is rerun, so estimation converges on residual need.
- [x] Account planning roles are primary, with explicit split planning purpose as an
  override; Retirement, FSA/benefit, Loan/debt, and Investment roles feed planning
  semantics.
- [x] Schedule growth policy is persisted for baseline/scenario schedules and exposed
  in GTK4 and web.
- [x] Formula-loan projection is protected from double interest and generic inflation,
  and recurrence/formula period numbering is stable across date adjustments.

## Scheduled transactions

- [x] Occurrences support bounds, future-effective amounts, skips, one-time overrides,
  and fixed multi-split schedules.
- [x] Every GTK Scheduled row is inspectable; unsupported shapes open a read-only
  detail view rather than becoming inaccessible.
- [x] Repeated account legs and per-split memos round-trip in fixed schedules.
- [x] Initial GTK Scheduled selection correctly enables View/Edit without requiring a
  second click.
- [x] Imported recurrence multipliers and hidden recurrence details such as end-of-
  month/semi-month firing are preserved when unchanged.
- [x] Fixed planning transfers, ordinary balance-sheet transfers, multi-leg transfers,
  opposite-direction additional legs, and multiple planning-purpose legs are editable
  when they can be rebuilt losslessly.
- [x] Base and scenario formula schedules allow validated formula/variable editing
  while protected split/account/timeline semantics remain intact.

## Accounts and GnuCash account fidelity

- [x] Accounts view includes Planning role; Account, Type, Planning role, Description,
  and Balance are hierarchy-aware sortable columns.
- [x] Account editor preserves ordinary non-placeholder parents without allowing
  cycles.
- [x] Imported commodity/security, hidden flag, account notes, and per-account
  commodity SCU/precision are visible/preserved.
- [x] GnuCash re-import preserves BreadSched-owned account planning/projection/FSA
  configuration while refreshing source-owned account fields.

## FSA / benefit workflows

- [x] FSA funding years, election, run-out, availability, used/remaining/forfeited
  concepts are separate from ordinary custodial ledger balance.
- [x] Claims/service episodes support multiple payment links, allocations,
  reimbursements, refunds, rejections, and Review/Dashboard workflows.
- [x] FSA claims are first-class transactional persisted objects with migration from
  legacy metadata and atomic undo/redo with linked split classifications.

## Import and interoperability

- [x] Native deterministic QIF and banking/credit-card OFX/QFX import exists.
- [x] Stable source identities protect re-import from unrelated record insertion.
- [x] GnuCash re-import preserves BreadSched-owned account/transaction/split state by
  stable source GUID while refreshing source-owned ledger facts.

## Dashboard / UI architecture

- [x] Dashboard account grouping honors planning roles.
- [x] Dashboard bills are schedule/Plan driven and do not depend on a hidden legacy
  current-budget selector.
- [x] GTK4 is the canonical/reference interface; web parity is required for financial
  behavior and major workflows.

# 3. Remaining product roadmap

## Account editor and Accounts view

- [ ] Expand the account editor to safely handle **all account forms imported from
  GnuCash** without destructive normalization or loss of imported semantics.
- [ ] Add representative GnuCash account fixtures and round-trip/regression tests as
  the supported surface grows.
- [ ] Continue exposing remaining imported account metadata/semantics losslessly.
- [ ] Improve account relationship editing/explanations for investment, debt, FSA,
  commodity/security, and imported-account semantics.

## Register workflow

- [ ] Allow multiple register windows/views at the same time; filters, selection,
  edit state, and navigation must remain local to each window/view.
- [ ] Improve register appearance and information density while keeping account-type
  debit/credit terminology clear.
- [ ] Support entering ordinary basic/two-sided transactions directly in the register,
  similar to GnuCash, with full split editing available when needed.
- [ ] Preserve atomic double-entry validation for all inline editing.

## Reconciliation

- [ ] Add first-class account reconciliation: statement date, ending balance,
  cleared/reconciled state, running difference, completion, cancel/restart, and
  auditable persistence.
- [ ] Put reconciliation rules in shared domain/application services first; GTK and
  web are presentations of the same workflow.
- [ ] Cover reopened/corrected statements and preservation of imported reconcile state.

## Historical estimator

- [ ] Detect irregular-but-recurring activity more reliably.
- [ ] Improve confidence scoring and outlier handling.
- [ ] Provide richer explanations of history, cadence, trend, seasonality, and
  residual calculation, with interactive adjustment before acceptance.
- [ ] Refine category-specific seasonality/cadence inference.
- [ ] Interpret investment, retirement, debt-principal, and FSA-role history correctly
  rather than treating all balance-sheet flows as ordinary Income/Expense activity.

## Plan and planning-flow reporting

- [ ] Add clearer unresolved/unexpected indicators in Plan.
- [ ] Expand reports for retirement saving/distributions, benefit/FSA funding, debt
  principal, and other economically meaningful balance-sheet flows.
- [ ] Add printable/exportable Plan and scenario-comparison reports.
- [ ] Improve explanations of account-role/split-purpose classification decisions.
- [ ] Ensure planning classifications feed Plan, Projection explanations, scenario
  comparison, and Dashboard consistently.

## Scheduled transactions and loans

- [ ] Continue widening safe editing only where complete split/recurrence/import
  semantics can be round-tripped without guessing.
- [ ] Preserve unsupported custom recurrence/formula structures losslessly in
  inspectable read-only form.
- [ ] Add fixture coverage for native/imported schedules, unusual recurrences,
  formulas, overrides, and bounded schedules.
- [ ] Add an approachable loan/amortization creation workflow.
- [ ] Support per-leg amount timelines in fixed multi-split schedules.
- [ ] Support additional deterministic advanced/custom recurrence patterns.
- [ ] Add payroll templates and richer payroll editing.
- [ ] Consider richer formula assistance only on top of the existing safe formula
  language; never add Python `eval` or a second formula dialect.

## Investment and retirement modeling

- [ ] Model scheduled investment contributions and distributions explicitly.
- [ ] Separate contributions, performance, dividends/interest, fees, and withdrawals
  in both state transitions and explanations.
- [ ] Distinguish taxable investment withdrawals, retirement distributions, and
  rollovers; retirement-to-retirement rollovers must remain economically neutral.
- [ ] Add retirement drawdown behavior and dated scenario support for withdrawal
  patterns.
- [ ] Use per-account and dated account-specific return/rate assumptions throughout
  Projection UI/comparison/explanations.
- [ ] Add investment lots and cost basis.
- [ ] Add a security/commodity price layer separating quantity, price, value,
  exchange rate, and assumed return.
- [ ] Keep deterministic projection as the normal model; any later Monte Carlo engine
  should remain a separate optional analysis.

## Projection and scenarios

- [ ] Add a custom per-schedule growth rate only if it can be explained cleanly within
  the scenario-assumption model.
- [ ] Improve projection caching/reuse without storing stale calculated scenario
  results.
- [ ] Add cancellation/progress for expensive projections.
- [ ] Add richer charts, cash-runway comparisons, and deeper account explanations.
- [ ] Polish Plan/Projection scenario comparisons.
- [ ] Evolve assumptions toward extensible dated rules (salary changes, retirement,
  pensions/Social Security, temporary expenses, mortgage payoff, changing return or
  inflation regimes) instead of hard-coded special cases.
- [ ] Preserve the invariant that every projected change can be explained as opening
  state + dated flows + interest/performance/assumption effects = closing state.

## FSA / benefit accounts and claims

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
- [ ] Strengthen account matching across imports/re-imports.
- [ ] Add commodity/security mapping and price information where source formats allow.
- [ ] Import scheduled transactions from additional formats where represented
  reliably.
- [ ] Expand duplicate/re-import tests, including cross-file duplicate heuristics.
- [ ] Improve import summaries/problem reporting.
- [ ] Add browser-native file upload to the web import view; 0143 provides functional parity through local paths visible to the BreadSched process.
- [ ] Cover richer transfer/category mapping and real-world QIF/OFX deviations.
- [ ] Continue representative GnuCash compatibility fixtures for accounts,
  transactions, reconciliation, commodities, schedules, formula loans, and unusual
  but valid structures.
- [ ] Add multi-currency valuation and exchange-rate/price handling.
- [x] **0143 — Expose ambiguous import-format choices.** GTK4 and web import workflows expose QIF date-order and QIF/OFX number-format overrides while keeping auto-detection as the default.
- [ ] Report missing import dates explicitly rather than silently substituting today.
- [ ] Investigate/cover older GnuCash SQLite timezone/date conventions.

## Storage, integrity, and recovery

- [ ] Maintain explicit schema migrations with transactional migration tests and
  automatic safety backups as persistent models evolve.
- [ ] Strengthen backup/restore and crash-recovery UX.
- [ ] Test interrupted writes, failed migrations, and recovery behavior under the
  chosen journaling/WAL policy.
- [ ] Keep database concurrency ownership/locking explicit and testable for desktop
  and web access.
- [ ] Expand executable invariants: balanced transactions, no orphaned splits,
  commodity consistency, schedule idempotency, reconciliation preservation, and
  projection conservation.
- [ ] Add a user-visible **Verify book** / diagnostic workflow before a stable release.
- [ ] Longer term, separate planning resolutions/classifications from imported ledger
  records where doing so materially simplifies synchronization and ownership.

## GTK, web parity, and reporting

- [ ] Continue real GTK runtime testing for selections, dialogs, focus transitions,
  model replacement, multiple windows, and GTK API-version differences.
- [ ] Improve first-run UX, preferences, actionable errors, icons/resources, and
  native desktop polish without moving financial logic into GUI code.
- [ ] Add printable/exportable reports and richer scenario/flow reporting.
- [ ] Keep long-running operations responsive with clear progress/cancellation.

## In-application help and documentation

Documentation responsibilities are intentionally separated:

- `README.md` — concise user-facing overview and first operational entry point.
- `DESIGN.md` — current architecture, rationale, and durable design decisions.
- `ROADMAP.md` — this file; the sole authoritative future-work list.

Remaining documentation work:

- [ ] Build full user documentation and in-application help for Accounts, registers,
  Scheduled transactions, Plan, Review/Actuals, Projection, scenarios, planning
  roles, imports, reconciliation, and backup/recovery.
- [ ] Add a generic-household walkthrough that creates a comprehensive chart of
  accounts, recurring income/expenses, savings/debt/retirement flows, and Base plan.
- [ ] Add a multiple-scenario walkthrough that duplicates Base and compares alternate
  assumptions/scheduled estimates.
- [ ] Explain why BreadSched models budgets as dated planned events rather than
  arbitrary monthly cells, with annual/weekly/semi-monthly examples.
- [ ] Explain how actual transactions resolve planned/scheduled occurrences while
  preserving expected date/amount for variance history.
- [ ] Explain commitments vs estimates, convergent historical estimation, planning
  roles/split purposes, GnuCash compatibility, growth policies, formula schedules,
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
