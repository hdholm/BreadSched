# BreadSched roadmap

This file is the **single authoritative source for future BreadSched work**. Project
plans discussed during development belong here rather than only in chat history.

**Maintenance rule:** every patch that completes, changes, discovers, splits, or
reprioritizes roadmap work must update this file in the same patch.

The current accepted baseline is **0203 — classified historical planning flows**.
Unchecked field reports below are requests or suspected regressions, not claims
that a root cause has already been confirmed.

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
  claims are first-class persisted records and save atomically with reimbursement
  split classifications.
- [x] **0138 — First expanded quality gates.** A dedicated serial performance gate
  measures single-write cost on a synthetic 30,000-transaction household book;
  Hypothesis-based recurrence properties exercise randomized intervals, dates,
  weekend adjustments, serialization, and occurrence identity. Core xdist runs
  exclude the dedicated performance marker, which executes once in CI.

## Remaining hardening work

- [x] **0157 — Keep paid-in-full card payment days editable.** A card cleared each
  month still has a payment due day. Only the usual carried-balance payment amount
  depends on the cleared-in-full setting; the due day remains editable and persists
  when the setting changes or the editor is reopened.
- [x] **0157 — Bound recently closed FSA years on the Dashboard.** GTK and web use
  the shared query: show open years and at most one recently closed year per FSA
  account, only through 90 days after its run-out deadline (or year end when no
  run-out exists). Older years remain available in history. This supersedes the
  earlier request to hide every closed year immediately.

## Immediate field-report priorities

1. **NEXT — Account and schedule fidelity.** Continue lossless editing and fixture coverage
   for imported account and schedule forms, bounded by what can be round-tripped
   without guessing.

## Dashboard balances and group hierarchy

- [x] **0177 — Account-controlled emergency-fund expenses.** Add an explicit account
  setting for whether activity against that account must be carried when sizing the
  emergency fund; do not try to infer whether an expense stops when household
  income stops. Only the qualifying legs of actual, scheduled, and estimated
  transactions tied to opted-in eligible accounts contribute to emergency-fund
  outgoings, without counting their cash/funding counterparts again. Cash, Bank,
  Investment, FSA, Income, and Equity accounts are always non-emergency and do not
  expose the setting. Asset and Retirement are also fixed-excluded; Escrow, Expense,
  Loan, general Liability, and carried-balance Credit card default included and may
  opt out. Paid-in-full cards remain liquidity-only. Apply the setting to positive
  economic legs, counting loan principal and interest as components without counting
  cash funding, and count escrow funding without counting its later draw again.
  Preserve the choice across GnuCash re-import; expose GTK/web controls and shared
  Dashboard explanations. Advance the alpha version to `0.2.0a16`.

- [x] **0161 — FSA group availability.** Show remaining funds for the applicable plan
  year(s), using the shared FSA funding-year calculations at the Dashboard's as-of
  date, instead of the custodial account ledger balance. Cover overlapping plan
  years/run-out periods, exhausted and expired years, and missing year definitions.
  GTK and web must present the same result and make unavailable year data explicit.
- [x] **0161 — Colon-separated group paths.** Accept account-style paths in configuration
  and account group fields. For example, `Investments:Plan A` and `Investments:Plan B`
  display as `Plan A` and `Plan B` under an `Investments` heading. Each leaf shows its
  own total; each heading shows the sum of its children and any directly assigned
  accounts. Support deeper paths and persist the full paths while displaying local
  names. Compute the hierarchy and totals in the engine for GTK/web/CLI parity.
- [x] **0161 — Account-subtree deduplication.** A grouped parent account includes its whole
  subtree exactly once. Explicitly selected descendants must not appear or be added
  again beneath the same grouped parent. Resolve overlapping group assignments
  consistently so heading/grand totals and liquidity calculations cannot count a
  descendant twice. Cover parent-plus-child selections, nested descendants,
  repeated handles, and mixed assets/liabilities with generic regression fixtures.
- [x] **0161 — Paid-off loans.** Omit paid-off loan entries from the Dashboard, including
  loans linked to an asset. Preserve the asset's own visibility and value exactly
  once; a linked asset must not keep a paid-off loan row visible. Test multiple
  loans on one asset and distinguish a zero loan balance from a fully repaid loan
  that still has stale future schedule occurrences.

## Other hardening work

- [x] **0138–0156 — Expanded quality gates.**
  - [x] **0154 — Add CLI/web to the mandatory mypy gate.** Correct CLI and web type
    errors and annotate comparison/dashboard results. The extended gate follows
    imports for type information but suppresses errors in imported GUI modules.
  - [x] **0156 — Add GUI modules to mandatory mypy coverage.** Correct optional
    value narrowing, collection annotations, and per-account Rate assignments;
    share dialog-close refresh callbacks and repair the invalid exception class
    in opening-balance parsing. The extended gate checks all presentation modules
    and their imports without a suppressed GUI baseline. Dynamic PyGObject APIs
    still require the GTK runtime tests.
    Projection controls also preserve per-account rates when rebuilding assumptions.
  - [x] **0155 — Make formatting mandatory.** Apply Ruff formatting across source,
    tests, and examples; enforce `make format-check` in `make check` and CI.
    `make fmt` now applies both Ruff lint fixes and formatting.
  - [x] Keep Host/Origin/token web-security regressions in the standard gate.
  - [x] Add property-based recurrence tests around generated occurrence identity,
    weekend adjustment, and serialization. Continue adding properties as new
    collision/edge cases are discovered.
  - [x] Add realistic storage/projection performance gates on one shared synthetic
    30,000-transaction household history: an ordinary commit stays below 0.5 seconds
    and a 30-year projection stays below a deliberately generous 3-second budget.
    Add report-specific benchmarks as those paths are hardened.
  - [x] Continue pytest-xdist rollout: core/non-GTK uses `pytest -n auto`; GTK stays
    serial and the realistic performance gate runs separately with `-n 0`.
  - [x] Preserve `make test-ordered` / `pytest -n 0` as a deterministic diagnostic path.
- [ ] Harden `Money` and amount handling:
  - [x] **0140 — Core parsing/comparison safety.** Ambiguous locale-formatted strings are rejected instead of silently mis-scaled; equality/hash behavior follows Python's numeric contract; non-numeric equality does not raise.
  - [x] **0141 — QIF/OFX number-format parsing.** Detect period-vs-comma decimal conventions from the complete import file, parse grouping explicitly, reject conflicting conventions, and allow an explicit importer override for ambiguous files.
  - [x] **0142 — QIF date-order parsing.** Detect month-first versus day-first ordering from complete-file evidence, reject conflicting evidence, preserve year-first dates, and allow an explicit importer override for all-ambiguous files.
  - [x] **0153 — Locale-aware GTK/web amount entry.** Route typed amounts through one boundary parser, accept period- or comma-decimal input without weakening core `Money`, send the browser decimal convention with writes, and remove JavaScript floating-point conversion from FSA amount entry.
  - [x] **0152 — Dimensional Money/Rate semantics.** Reject `Money * Money`, make
    `Money / Money` an exact dimensionless ratio, and represent scenario growth,
    inflation, return, and interest assumptions with a Decimal-compatible `Rate`
    type while preserving existing serialized scenario data.
  - [ ] Remove hard-coded cents where account/commodity precision differs.
  - [x] **0167 — Initial commodity-safe valuation boundary.** Preserve exact split
    quantity separately from transaction-currency value, apply only direct dated
    security-to-reporting-currency quotes, and retain ledger value explicitly when
    no compatible quote exists. Multi-currency conversion and lot accounting remain
    separate work below.
- [x] **0146 — Platform-correct user paths.** Settings use XDG/APPDATA/macOS
  Application Support as appropriate; Documents discovery honors XDG user dirs and
  common Windows OneDrive redirection; recognized cloud-sync roots emit an SQLite
  durability warning when a book is opened there.
- [x] **0145/0150 — Inter-process writer lock.** Writable native books use an owned
  sidecar lock, competing writers fail with an explicit read-only alternative,
  read-only opens remain allowed, stale same-host locks are safely reclaimed, and
  canonical path identity prevents a symlink alias from bypassing the writer lock.
- [x] **0165 — Clean schema-4 event-planning core.** Remove the retired monthly
  Budget domain, its alternate projection engine, compatibility commands/routes,
  and obsolete product naming while preserving all external GnuCash/QIF/OFX/QFX
  import paths. The temporary alpha-schema cleanup retained here was superseded and
  removed by 0190. Advance the alpha version to `0.2.0a4`.
- [x] **0166 — Single account types and linked properties.** Normalize the then-current
  account representation to one visible account type. On the Dashboard, an
  explicitly assigned asset or loan pulls in visible,
  otherwise-unassigned companions from its stored link so
  property value, debt, equity, and LTV remain together without restoring inferred
  default groups. Report the latest bounded enabled repayment date as the loan end
  across engine, GTK, web, and CLI, and advance the alpha version to `0.2.0a5`.
- [x] **0167 — Dated security prices and current valuation.** Add schema-6 exact,
  dated commodity prices and indexed split quantities; preserve GnuCash SQLite/XML
  prices by stable GUID; expose locale-aware GTK/web security price entry; and use
  shared as-of market valuation for Investment/Retirement accounts in Accounts,
  Dashboard groups, net worth, and Projection opening state. Generated Dashboard
  path headings expose only rolled-up equity/total, leaving property value, owed,
  LTV, and loan end on the specific leaf. Repair the web Plan script syntax exposed
  by executable JavaScript checking, and advance the alpha version to `0.2.0a6`.
- [x] **0168 — Dated pending cash flow and separate FSA Dashboard.** Move benefit-year
  availability and open claims to dedicated GTK/web FSA Dashboard views. Show
  pending scheduled income alongside bills with no Hold-now amount. Replace elapsed-
  time bill accrual with exact income-event allocation over one bill cycle, retain
  overdue bills as liquidity obligations while reserving for their next occurrence,
  and conservatively hold a full bill when no cycle income is known. Generate
  account-tied card payments from the configured day and current balance/usual
  payment without duplicating explicit schedules. Use dated income rather than a
  monthly average for liquidity. Normalize GnuCash's zero multiplier for one-time
  scheduled transactions at both import boundaries, and advance the alpha version
  to `0.2.0a7`.
- [x] **0169 — Import memory and hidden-account entry safety.** Remember the last
  successful import source separately for each destination book across CLI, GTK,
  and web entry points; preselect it without automatically importing or writing to
  the source. Omit hidden accounts from new GTK/web transaction choices, reject
  crafted web writes that name them, and preserve visibly labelled hidden accounts
  when an existing GTK transaction is edited. Advance the alpha version to
  `0.2.0a8`.
- [x] **0170 — Scheduled lifecycle and navigation.** Add shared operations and GTK/
  web workflows for undoable definition deletion, safe reviewed duplication, and
  unsaved one-time drafts made from existing ledger transactions. Preserve every
  draft split's account, value, memo, and planning purpose; give copies independent
  identities and occurrence state; retain posted actuals on deletion; and refuse
  deletion that would strand scenario overrides. Activate Dashboard and Upcoming
  rows into their schedule/account workflow without posting. Preserve schedule
  memos through the web editor, protect hidden accounts at new-schedule boundaries,
  and advance the alpha version to `0.2.0a9`.
- [x] **0171 — Reviewed historical-estimate drafts.** Make Suggest from History open
  an unsaved, populated Base or saved-scenario schedule editor instead of writing
  immediately. Preserve inferred cadence and seasonal values through GTK/web review;
  Save the user's final values and make Cancel a no-op. Keep one GTK/web suggestion
  window per book/context, foreground it on repeated requests, and advance the alpha
  version to `0.2.0a10`.
- [x] **0172 — Cadence-safe historical coverage.** Match planned coverage to exact
  year/months in the rolling future window. Bound monthly bridge estimates before a
  sustained part-year replacement begins without letting an isolated future event
  hide recurring need. Infer annual, biennial, and triennial amounts and next dates
  without annualizing active-month values; represent a lone completed event only as
  a reviewed one-time draft; and advance the alpha version to `0.2.0a11`.
- [x] **0173 — Imported transaction notes.** Preserve GnuCash transaction-level
  notes separately from split memos and BreadSched-authored notes in both SQLite
  and XML imports. Refresh source notes while retaining local notes on re-import;
  expose both in GTK transaction details and web registers, allow GTK/web entry of
  local notes, and advance the alpha version to `0.2.0a12`.
- [x] **0174 — Safe SQLite scheduled formulas.** Replace SQLite import's character
  whitelist with validation by the same bounded formula engine used at runtime.
  Preserve supported arithmetic, financial functions, and `period`/`i` occurrence
  variables dynamically with XML parity, retain the no-arbitrary-code boundary,
  and advance the alpha version to `0.2.0a13`.
- [x] **0175 — Printable current reports.** Add one GTK Print current view action
  for the applied Dashboard, Plan, and Projection, using private self-contained HTML
  previews that retain report values, totals, annual assumptions, charts, and active
  Projection comparisons and can print or save as PDF. Add browser-native printing
  for every current web view with print styling that removes navigation and expands
  scrollable report tables. Keep Base and scenario estimate draft types distinct in
  the full-cache extended mypy gate. Advance the alpha version to `0.2.0a14`.
- [x] **0181 — Wait for printable Projection state.** Synchronize the GTK print
  boundary with an in-flight Projection worker before reading the visible result.
  Report a bounded wait failure rather than silently leaving the preceding preview
  open, strengthen the GUI regression to require one new preview per action, and
  advance the alpha version to `0.2.0a20`.
- [x] **0179 — Responsive Projection and import work.** Move GTK Projection and
  import work off the main thread, give Projection an independent read-only worker
  connection, deliver progress/results with `GLib.idle_add`, and cooperatively
  cancel calculations or atomically roll back imports. Retain deliberate
  `DELETE`/`FULL` SQLite durability under the single-writer book lock and advance
  the alpha version to `0.2.0a18`.
- [ ] Split oversized modules where it improves ownership/testability, especially web
  routing and very large GUI test modules.

# 2. Completed product capabilities to preserve

## Planning, scenarios, and projection

- [x] Plan is derived from actual, scheduled, and estimated **dated** transactions,
  not stored monthly budget cells.
- [x] Base and saved scenarios support dated assumptions and scenario-specific
  estimate add/alter/suppress behavior.
- [x] Every account has one semantic BreadSched type, with accounting class and
  planning behavior derived from it. Exact imported GnuCash source type is retained
  separately and explicit split planning purpose remains an override.
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

- [x] Accounts view uses the single BreadSched account type; Account, Type,
  Description, and Balance are hierarchy-aware sortable columns.
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
- [x] FSA claims are first-class transactional persisted objects with atomic
  undo/redo and linked split classifications.

## Import and interoperability

- [x] Native deterministic QIF and banking/credit-card OFX/QFX import exists.
- [x] Stable source identities protect re-import from unrelated record insertion.
- [x] GnuCash re-import preserves BreadSched-owned account/transaction/split state by
  stable source GUID while refreshing source-owned ledger facts.

## Dashboard / UI architecture

- [x] Dashboard groups are explicit and account-owned or configuration-owned; there
  are no inferred default groups. Hidden accounts are not direct group members.
- [x] Dashboard bills are schedule/Plan driven and do not depend on a hidden legacy
  current-budget selector.
- [x] GTK4 is the canonical/reference interface; web parity is required for financial
  behavior and major workflows.

# 3. Remaining product roadmap

## Account editor and Accounts view

- [x] **0188 — Durable imported-account provenance.** Retain the exact GnuCash
  source GUID independently from the BreadSched object handle so roots and
  pre-existing top-level accounts adopted during import continue to match after a
  source rename or move. Preserve exact unknown source type text and typed account
  fields for read-only inspection instead of normalizing them into BreadSched
  semantics. Map historical checking/savings/money-market/credit-line/CD types
  explicitly; put an unknown new type in Technical pending review. Expose ordinary
  and source-owned metadata through GTK, web, and CLI JSON; verify source-GUID
  uniqueness; cover SQLite, nested XML slots, and re-import. Advance the alpha
  version to `0.2.0a27`.
- [ ] **Fixture-driven imported-account fidelity.** Build one representative matrix
  of GnuCash account forms and relationships covering investment, debt, FSA,
  commodities/securities, and unusual valid metadata. Expand safe editing and
  explanations only for forms whose source semantics can demonstrably round-trip;
  expose every other supported form inspectably and read-only without destructive
  normalization. Add a fixture and round-trip/regression proof whenever the editable
  surface grows.

## Register workflow

- [x] **Hidden account choices.** Exclude hidden accounts from account lists for
  new transaction/split entry in GTK and web. When editing a transaction already
  referencing a hidden account, preserve and identify that existing selection;
  filtering must never silently replace a stored split account.
- [x] **0185 — One split-based transaction model.** Present ordinary entry as two splits
  by default, with the same split model, validation, and editing path used for
  additional legs. Retain atomic balanced postings and avoid separate financial
  semantics for a two-account shortcut.

- [x] **0192 — Independent registers.** Allow multiple register windows/views at the
  same time; filters, selection, edit state, and navigation remain local to each
  window/view.
- [ ] Improve register appearance and information density while keeping account-type
  debit/credit terminology clear.
- [x] **0192 — Inline basic entry.** Support entering ordinary basic/two-sided
  transactions directly in the register, similar to GnuCash, with full split editing
  available when needed.
- [x] **0192 — One atomic transaction path.** Preserve atomic double-entry validation
  for all inline editing. Share account-specific direction labels across GTK/web,
  exclude hidden transfer accounts, and advance the alpha version to `0.2.0a31`.

## Reconciliation

- [x] **0185 — Add first-class account reconciliation:** statement date, ending balance,
  cleared/reconciled state, running difference, completion, cancel/restart, and
  auditable persistence.
- [x] Put reconciliation rules in shared domain/application services first; GTK and
  web are presentations of the same workflow.
- [x] Cover reopened/corrected statements and preservation of imported reconcile state.
  Persist statement sessions and append-only lifecycle events; finish and reopen
  split changes atomically, require latest-first correction, and verify stored split
  references. GTK and web registers share the same exact difference calculation.
  Advance the alpha version to `0.2.0a24` and native schema to 7.

## Historical estimator

- [x] **0159 — Restore estimate convergence and monthly future coverage.** Gross
  need comes from historical actuals; the selected future plan contributes the
  coverage, once, for each calendar month. Both commitments and accepted estimates
  count, even if they began after history; expired, inactive, and scenario-suppressed
  schedules do not. Cover updated amounts, partial/full acceptance, and explain
  historical median, applied future coverage, and remaining median separately.
- [x] **0172 — Long-cycle and partial-year future coverage.** The next twelve planning
  months establish one future sample of each calendar month. A schedule every two
  or three years, a seasonal schedule that only begins partway through the future
  year, or a scenario with changing coverage needs cadence-aware matching of
  historical need against the exact future windows. Avoid suppressing an uncovered
  near-term month because a later year has a scheduled occurrence, and avoid
  projecting a completed one-time expense as recurring. Define and test those
  cases before extending the current one-year window.
- [x] **0159 — Count all future split categories.** Future committed and estimated
  multi-split events contribute each income/expense leg, including repeated legs
  and refunds to the same category, with exact signed totals.
- [x] **0171 — Review before acceptance.** The suggestion's action opens the
  populated Add Scheduled Transaction editor for adjustments to amount, accounts,
  recurrence, dates, and splits. Commit only after Save; Cancel must leave no new
  schedule. Reanalysis must use the actual saved values.
- [x] **0171 — One suggestion window per book/context.** Repeating Suggest from History
  should foreground the existing window, not create another. Handle closing,
  changing books, and scenario changes without stale references.
- [x] **0159 — Stable Add placement in GTK.** Place each suggestion's Add button
  before its description so resizing does not detach the action from its item.
  The web table already displays its action in the proposal's own row.

- [x] **0193 — Robust cadence, outliers, and confidence.** Recognize stable
  multi-month recurrence despite day-of-month drift; conservatively exclude and
  disclose isolated amount anomalies only with sufficient history; and score
  confidence from coverage, depth, retained evidence, and robust variability.
  Advance the alpha version to `0.2.0a32`.
- [ ] Provide richer explanations of history, cadence, trend, seasonality, and
  residual calculation, with interactive adjustment before acceptance.
- [ ] Refine category-specific seasonality/cadence inference.
- [x] **0202 — Historical-estimator transaction semantics.** Interpret an entire
  transaction before extracting ordinary Income/Expense history. Reinvested
  dividends/interest, investment fees, and rollovers do not become recurring
  household income or expense suggestions. When a multi-split loan payment,
  retirement contribution, or benefit allocation contains several balance-sheet
  counterparts, retain the observed recurring counterpart and prefer spendable
  cash to break equal-evidence ties. Apply the same exclusions to future coverage
  and cadence evidence. Advance the alpha version to `0.2.0a40`.
- [x] **0203 — Propose classified historical planning flows.** Add distinct,
  reviewable proposals for recurring retirement saving/distribution, investment
  contribution/withdrawal, debt principal, and benefit/FSA funding. Preserve the
  shared planning-flow or investment-activity classification in accepted Base and
  scenario estimates and subtract matching future classified coverage exactly once.
  Keep duplicated cash-counterpart annotations from producing a second retirement
  distribution proposal. Advance the alpha version to `0.2.0a41`.

## Plan and planning-flow reporting

- [x] **0160 — Account kinds and initial Escrow planning/projection.** This historical
  intermediate introduced a BreadSched account kind independent of the GnuCash
  ledger type; 0162 later replaced the two-field representation with one visible
  account type. Introduce Escrow for asset accounts. A
  scheduled contribution from cash to escrow is the planning expense at funding
  time even though the ledger debit increases an asset. Later scheduled payments
  from escrow to tax, insurance, or other expense accounts are draws against that
  already planned amount, not a second planning expense. Keep both ledger legs
  intact for balances and show escrow balance rising and falling on the correct
  dates in Projection; distinguish planned expense from cash movement and net-worth
  changes. Shared recognition covers scheduled and actual funding/draw cycles,
  partial payouts, category Plan totals, historical estimates, Dashboard grouping,
  and Projection state/explanations without changing ledger splits.
- [x] **0162 — One semantic account type and explicit dashboards.** Replace the
  user-visible ledger-type/account-kind pair with one BreadSched-owned type whose
  accounting class and planning behavior are derived. Retain exact GnuCash source
  type as read-only provenance; preserve the local type on re-import and report
  cross-class conflicts. Document every visible type
  and the mapping rationale. Remove inferred dashboard groups, honor only explicit
  account/config assignments, and omit hidden direct members. Begin PEP 440 alpha
  versioning at `0.2.0a1` from one authoritative version source.
- [x] **0163 — Persistent Plan controls and complete totals.** Store From, Through,
  Group by, Show, scenario, and comparison per book for GTK/web parity. Add a Total
  column for every category and planning-flow row, non-duplicating section totals
  for every reporting period, and a Net cash change grand-total row. Limit variance
  totals to applicable as-of periods and advance the alpha version to `0.2.0a2`.
- [x] **0164 — Safe Plan detachment.** Treat a null database as the normal
  book-close/view-detach lifecycle before attempting to restore persisted Plan
  controls. Cover direct detachment in the GTK regression suite and advance the
  alpha version to `0.2.0a3`.
- [x] **0187 — Escrow follow-through.** Distinguish cash/income-funded deposits,
  expense-covering draws, vendor-credit restorations, cash refunds, internal escrow
  transfers, and manual/Equity balance corrections without changing balanced ledger
  splits. Cash refunds reverse prior planning expense; restorations and corrections
  are neutral. Keep partial draws exact and proportional. Explain the shared result
  in GTK/web Plan and Projection detail and printable Projection reports; keep
  negative projected balances visible and warn when an event creates or worsens a
  shortfall. Combined loan payments recognize interest plus escrow funding while
  principal only reduces the liability. Cover scenario overrides and a representative
  GnuCash account retyped locally as Escrow and preserved on re-import. Advance the
  alpha version to `0.2.0a26`.
- [x] **0191 — Explainable Plan classifications.** Put shared, plain-language
  provenance in Plan cell details: name the account type behind category activity,
  distinguish explicit planning purposes from narrow account-type/direction
  inference, and explain pending, unresolved, matched, historical, and explicitly
  unexpected resolution states. Link Plan directly to Resolve actuals and expose
  split planning-purpose correction in the GTK transaction editor for web parity.
  Advance the alpha version to `0.2.0a30`.
- [x] **Row and column totals.** Totals across periods and down each period column
  cover planned, actual, and variance values consistently. Category section totals
  count outermost rollups once; planning flows remain separate; Net cash change is
  the grand total rather than a sum of unlike financial dimensions.
- [x] **0197 — Mortgage cash flow and liability projection.** Treat one scheduled
  mortgage transaction as one cash requirement with non-additive classified
  components. For the approved representative payment, show `$2,400` once as cash
  required, with `$1,150` interest expense, `$450` escrow funding, and `$800` debt
  principal; reduce Checking by `$2,400`, reduce the mortgage liability by `$800`,
  increase Escrow by `$450`, and reduce immediate net worth by `$1,150`. The parent
  payment is informational and must never be added to its children in section or
  grand totals. Preserve property value independently; count later escrow tax or
  insurance disbursements in ledger/net-worth state without counting a second Plan
  expense; and let one actual payment resolve the scheduled whole even when its
  component allocation differs. Cover GTK, web, printable reports, Plan,
  Projection, Dashboard liquidity, comparisons, extra principal, fees, escrow
  shortage/refund, origination, refinancing, and sale with shared report data.
  Advance the alpha version to `0.2.0a35`.
- [x] **0199 — Signed spendable-cash Plan bridge.** Lead GTK, web, and printable Plan
  reports with one signed reconciliation from income, ordinary expense, retirement
  distributions/saving, benefit funding, debt principal, escrow funding, and an
  explicit timing/financing residual to the existing spendable-cash result. Collapse
  equal-and-opposite retirement-distribution account legs into one logical flow;
  separate signed Income less expenses from positive budget magnitudes; remove the
  misleading mixed planning-flow total; and expose opening, ending, and exact-dated
  minimum projected spendable cash. Treat future-only actual and variance summaries
  as not applicable. Advance the alpha version to `0.2.0a37`.
- [x] **0200 — Decision-ready Plan printing.** Give the cash/liquidity summary
  clear visual priority, start category detail as a distinct appendix, repeat table
  headings without overlapping rows, improve numeric density and section spacing,
  and omit private book paths from printed headers. Keep category detail available
  behind an explicit print-preview option while making the default printout useful
  for locating cash shortfalls quickly. Repair scenario-event table cells that had
  collapsed into comma-separated text and advance the alpha version to `0.2.0a38`.

- [x] Add clearer unresolved/unexpected indicators in Plan.
- [x] Expand reports for retirement saving/distributions, benefit/FSA funding, debt
  principal, and other economically meaningful balance-sheet flows.
- [x] Print/export the applied Plan and displayed Projection comparisons through
  self-contained HTML reports, with browser PDF output and GTK/web parity.
- [x] Improve explanations of account-type/split-purpose classification decisions.
- [ ] Ensure planning classifications feed Plan, Projection explanations, scenario
  comparison, and Dashboard consistently.

## Scheduled transactions and loans

- [x] **0158 — Frequency terminology.** Display `Once` in GTK and web schedule
  and scenario editors without changing the saved recurrence identifier.
- [x] **0182 — Bounded complex-schedule inspection.** Put the GTK schedule details
  body in a two-axis scroller while keeping Close/Save controls outside it, so large
  imported and multi-split definitions cannot push actions off screen. Construct
  formula controls before recurrence-loading callbacks can validate them. Resolve
  displayed formula amounts with a representative occurrence's full `period`/`i`
  context, preserving valid GnuCash colon/grouping syntax without warning or
  rewriting its stored text. Advance the alpha version to `0.2.0a21`.
- [x] **0183 — Main-thread import completion.** Make every importer honor
  `notify=False` across its complete call boundary, including its final aggregate
  database-change event. GTK background imports now deliver exactly one coalesced
  notification after returning to the main loop, so GnuCash re-import cannot rebuild
  `GtkColumnView` models from a worker thread and trigger native GTK criticals or a
  segmentation fault. Cover SQLite/XML GnuCash, QIF, OFX, and the GTK callback-thread
  contract; advance the alpha version to `0.2.0a22`.
- [x] **0184 — Account-linked card payments.** Credit cards with payment days appear
  in Dashboard, Scheduled, and Upcoming through one non-persisted definition owned
  by the account. Paid-in-full cards use current balance; carried cards use the
  lesser of balance and usual payment. A BreadSched-owned Bank/Cash relationship is
  editable in CLI/GTK/web, inferable from payment history, preserved on re-import,
  and checked for dangling references. Recognized actual payments advance the due
  cycle, while unpaid overdue obligations remain due. Enabled explicit/imported
  payment schedules with unresolved or future occurrences suppress the derived
  definition without letting a completed bounded schedule suppress it forever. The
  derived definition is informational and is never auto-posted or repeated into
  Projection with an invented future balance.
  Advance the alpha version to `0.2.0a23`.
- [x] **Upcoming transaction activation.** Double-clicking upcoming activity in
  the Upcoming view or Dashboard should open its view/edit workflow. Identify the
  selected occurrence and distinguish editing it from editing the recurring
  definition; opening the editor must not post a future transaction automatically.
- [x] **Create schedule from actual.** GTK and web can populate an unsaved one-time
  schedule draft from an existing transaction for review. All split accounts,
  amounts, memos, and planning purposes are copied; the user chooses any recurrence
  or date changes before saving a new independent definition.
- [x] **0178 — Finish duplication of protected custom schedules.** Editable fixed/formula
  schedules can be duplicated into reviewed drafts with independent identity and
  occurrence state. Extend this safely to imported/custom structures that remain
  read-only because the current editor cannot round-trip every recurrence or split
  feature. Exact-copy review in GTK/web preserves protected source recurrence,
  formulas, splits, and metadata with a new identity while clearing completed and
  skipped occurrence state.
- [x] **Delete schedules.** Provide a discoverable deletion action with appropriate
  confirmation and atomic undo/redo. Retain already posted transactions and handle
  scenario overrides, pending occurrences, resolutions, and imported-source
  re-import behavior explicitly rather than leaving broken references.

- [ ] Continue widening safe editing only where complete split/recurrence/import
  semantics can be round-tripped without guessing.
- [x] **0178 — Preserve unsupported custom recurrence/formula structures losslessly**
  in inspectable read-only form. Retain original source representations and reasons;
  exclude them from planning, projection, and posting rather than silently mapping
  an unknown recurrence to monthly or replacing an unknown formula with a fixed
  value. Advance the alpha version to `0.2.0a17`.
- [ ] Add fixture coverage for native/imported schedules, unusual recurrences,
  formulas, overrides, and bounded schedules.
- [x] **0184 — Approachable loan/amortization creation.** Keep the existing GTK
  preview/create workflow and add web parity over the same `LoanTerms`, amortization
  preview, formula schedule, and optional opening-liability service. Exclude hidden
  accounts from all new-loan account choices.
- [ ] Support per-leg amount timelines in fixed multi-split schedules.
- [ ] Support additional deterministic advanced/custom recurrence patterns.
- [ ] Add payroll templates and richer payroll editing.
- [ ] Consider richer formula assistance only on top of the existing safe formula
  language; never add Python `eval` or a second formula dialect.

## Investment and retirement modeling

- [x] **0186 — Explicit investment activity.** Persist contribution, taxable
  withdrawal, retirement distribution, reinvested dividend/interest, fee, and
  rollover classifications on actual, scheduled, and scenario splits. Expose them
  through GTK/web editing and preserve BreadSched-owned classifications on
  unambiguous GnuCash re-import. Direct Bank-to-Investment schedules no longer need
  an artificial Income/Expense anchor.
- [x] **0186 — Reconciled Projection attribution.** Separate contributions,
  performance, investment income, fees, taxable withdrawals, retirement
  distributions, and rollovers in state transitions, month/account explanations,
  summaries, CSV/web output, and printable reports. Retain compatible inference for
  unclassified movements without introducing tax, lot, or cost-basis guesses.
- [x] **0186 — Retirement-context validation.** Taxable withdrawals and retirement
  distributions require the appropriate account context. A rollover requires two
  distinct retirement-context accounts whose classified legs balance; it remains
  economically neutral in total holdings. Advance the alpha version to `0.2.0a25`.
- [ ] Add retirement drawdown behavior and dated scenario support for withdrawal
  patterns.
- [ ] Use per-account and dated account-specific return/rate assumptions throughout
  Projection UI/comparison/explanations.
- [ ] Add investment lots and cost basis.
- [x] **0167 — Add the initial security/commodity price layer.** Separate exact
  quantity, dated direct price, current value, and assumed return without rewriting
  ledger value. GTK/web support manual entry and GnuCash SQLite/XML import.
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

- [x] **0198 — Base is reality; saved scenarios are alternatives.** Treat Base as the one
  canonical expected plan: current actual ledger state, baseline scheduled and
  estimated activity, and book-level Base assumptions. Saved scenarios must contain
  only deliberate differences from that plan. Unchanged ledger facts and baseline
  schedules already flow into every calculation; replace the current full snapshot
  of saved-scenario assumptions with explicit inherited values plus local overrides
  so later Base changes propagate wherever the scenario has not diverged. Make the
  effective source of every value explainable in Plan, Projection, comparison, and
  scenario-management UI.
  Preserve preceding-alpha scenario values as deliberate overrides and advance the
  alpha version to `0.2.0a36`.
- [x] **0201 — Layer scenarios on scenarios.** Allow a saved scenario to name Base or
  another saved scenario as its assumption parent. Resolve the complete chain while
  retaining the original source of every annual and account-specific value. Preserve
  local override identity during reparenting, reject missing parents and cycles, and
  refuse parent deletion until its children are reparented. Expose the relationship
  through GTK, web, and CLI management. Dated periods and scenario events remain
  deliberately local rather than using an undefined list-merge rule. Advance the
  alpha version to `0.2.0a39`.
- [ ] Add a custom per-schedule growth rate only if it can be explained cleanly within
  the scenario-assumption model.
- [ ] Improve projection caching/reuse without storing stale calculated scenario
  results.
- [x] **0179 — Add cancellation/progress for expensive projections.** GTK Projection
  uses a cancellable read-only worker and marshals progress/results back to GTK.
- [ ] Add richer charts, cash-runway comparisons, and deeper account explanations.
- [ ] Polish Plan/Projection scenario comparisons.
- [ ] Evolve assumptions toward extensible dated rules (salary changes, retirement,
  pensions/Social Security, temporary expenses, mortgage payoff, changing return or
  inflation regimes) instead of hard-coded special cases.
- [ ] Expose the verified projection-conservation identity in user-facing detail:
  opening state + dated flows + interest/performance/assumption effects = closing
  state. Keep the engine invariant executable while making every term inspectable.

## FSA / benefit accounts and claims

- [x] **0168 — Separate FSA Dashboard.** Move benefit-year availability and open
  healthcare claims out of the general Dashboard into dedicated GTK/web views while
  retaining the shared FSA calculation and claim engines.

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

- [x] **0158 — Preserve and expose inactive schedule state.** Decode GnuCash
  SQLite textual false flags without treating them as truthy. Regressions cover
  inactive XML/SQLite schedules, toggling source state on re-import, and exclusion
  from due and projected activity. GTK and web editors display and save the active
  state explicitly, including for formula-backed schedules in GTK.
- [x] **0162 — Account type preservation on re-import.** Refresh the exact
  source-owned GnuCash type while retaining the user-owned BreadSched type and stable
  identity. Report cross-class conflicts for review rather than silently changing
  planning behavior or display signs. Commodity/precision, schedules, and
  existing-transaction follow-through remain part of their dedicated roadmap work.
- [x] **0174 — Supported scheduled formulas.** GnuCash formulas representable by the
  current safe formula language retain arithmetic/functions, recurrence ordinals,
  and per-leg dynamics in both import formats.
- [x] **0178 — Unsupported scheduled-formula preservation.** Preserve expressions outside
  the current safe language with actionable reasons rather than silently discarding
  them or enabling unrestricted evaluation. Add reviewed translation/variable
  mapping only where semantics are known.
- [x] **0173 — Transaction-level notes/memos.** GnuCash notes outside individual
  splits are imported and exposed by the transaction editor/display.
  Preserve and expose them separately from split memos, including on authoritative
  source updates, while respecting ownership of locally authored notes.
- [x] **Remember the import source.** Each destination book remembers its last
  successfully imported GnuCash, QIF, OFX, or other supported source across CLI,
  GTK, and web entry points and preselects it on the next interactive import. A
  missing/moved source still allows reselection; remembered paths do not authorize
  an automatic import or writing to the source.
- [x] **0176 — Precise import/re-import counts and skipped-item history.** Report matched
  transactions overwritten/refreshed from authoritative GnuCash data separately
  from new transactions and new splits. Define unchanged matches clearly. Persist
  stable skipped-item identities and reasons per source so later reports separate
  newly skipped records from previously reported skips and records now imported
  successfully. Update this history atomically with import outcome and retain
  BreadSched-owned metadata through source-authoritative updates.
  Successful imports report new, source-refreshed, and unchanged transactions and
  splits (plus removed source splits). Per-source skipped identities and reasons are
  updated in the same transaction and distinguish new, repeated, and resolved
  failures. Existing aggregate counts remain available. Advance the alpha version
  to `0.2.0a15`.
- [x] **0189 — Synchronize source-deleted GnuCash transactions prospectively.** Record
  every transaction GUID seen by a complete successful SQLite/XML import, including
  skipped records, against the stable source-book identity. After that first
  baseline, remove transactions that disappear from the source and report exact
  removal counts. Retain and warn about source-deleted transactions still referenced
  by BreadSched reconciliation/FSA audit data. Keep inventory updates and deletions
  in the import's atomic undo operation and retain tracking when the same source file
  moves. Describe that retained-reference behavior unobtrusively in import workflows.

- [ ] Add OFX investment transactions.
- [ ] Add useful QIF investment/security records.
- [x] **0188 — Strengthen adopted-account matching across GnuCash re-imports.**
  Persist the source GUID when an imported root or sibling is mapped onto a
  pre-existing BreadSched account, then resolve that identity before name/type
  matching on every later import.
- [x] **0167 — Import GnuCash dated prices.** Preserve exact SQLite/XML security
  quotes, quote currency, date, type/source, and stable price GUIDs on re-import.
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
- [x] **0143 — Expose ambiguous import-format choices.** GTK4 and web import workflows expose QIF date-order and QIF/OFX number-format overrides while keeping auto-detection as the default.
- [x] **0144 — Reject missing GnuCash dates.** Required transaction and scheduled-transaction dates are reported and skipped instead of silently substituting today.
- [ ] Investigate/cover older GnuCash SQLite timezone/date conventions.
- [ ] **Outbound interoperability and portable archives.** Define documented,
  loss-minimizing exports for supported household ledger/planning data and a
  versioned portable archival format with a human-readable manifest, integrity
  verification, provenance, re-import tests, and explicit disclosure of anything
  that cannot be represented. Preserve opaque imported structures where practical;
  do not claim GnuCash round-trip equivalence beyond demonstrated fixtures.

## Storage, integrity, and recovery

- [x] **0190 — Current-schema-only alpha storage.** Remove obsolete schema 3→7
  migrations, the migration registry/ledger, pre-migration backup hook, and
  migration-only fixtures. Accept exactly current schema 7 and reject every other
  native schema explicitly in read-only and writable modes. Preserve external import,
  ordinary backup/restore, integrity checking, and crash recovery. Advance the alpha
  version to `0.2.0a29`.
- [ ] **Rolling alpha storage compatibility.** Before the next native schema change,
  restore and retain the explicit migration registry, transactional migration
  runner, pre-migration backup hook, and versioned fixtures. During the current
  limited alpha, each release need only migrate a book from the immediately
  preceding alpha format because alpha users are expected to update every release.
  Do not remove the infrastructure after an individual migration expires: beta and
  stable releases will require a wider compatibility window, and weakening the
  sequential-update assumption must be a policy change rather than an emergency
  reconstruction.
- [x] **0180 — Verifiable recovery workflows.** Add GTK backup, restore-as-new, and
  background Verify Book workflows plus a web Verify view; share physical/logical
  verification results with CLI. Lock restore destinations against live writers,
  verify temporary restored copies before atomic installation, remove stale journal
  sidecars, and preserve overwritten books first. Advance the alpha version to
  `0.2.0a19`.
- [x] Strengthen backup/restore and crash-recovery UX with verified restore and
  discoverable GTK/CLI workflows.
- [x] Test interrupted writes and recovery behavior under the chosen journaling/WAL
  policy.
- [x] Keep database concurrency ownership/locking explicit and testable for desktop
  and web access.
- [x] **0196A — Probe Windows writer locks without signals.** Keep POSIX
  `os.kill(pid, 0)` liveness checks, but use a non-signaling Windows process-handle
  query so opening an already-locked book cannot send `CTRL_C_EVENT` to its owner.
  Retain live-owner rejection and stale-lock reclamation coverage across the CI
  platform matrix. Advance the alpha version to `0.2.0a34`.
- [x] **0194 — Complete the current executable invariant set.** Verify global split
  identity, commodity/currency roles and exact SCU representability, fixed schedule
  balance, unique realization of each planned occurrence, and unambiguous schedule
  exceptions. Retain existing transaction/reference, reconciliation-snapshot, and
  runtime projection-conservation checks; diagnose without rewriting imported data.
  Advance the alpha version to `0.2.0a33`.
- [x] Expand executable invariants: balanced transactions, no orphaned splits,
  commodity consistency, schedule idempotency, reconciliation preservation, and
  projection conservation.
  - [x] **0148 — Explicit chart-root semantics.** Only `ROOT` accounts are treated
    as roots by engines; a normal rooted chart reports parentless non-root accounts
    as integrity findings instead of silently dropping them from Projection.
- [x] Add user-visible GTK/web **Verify book** diagnostics before a stable release.
- [ ] Longer term, separate planning resolutions/classifications from imported ledger
  records where doing so materially simplifies synchronization and ownership.

## GTK, web parity, and reporting

- [ ] Continue real GTK runtime testing for selections, dialogs, focus transitions,
  model replacement, multiple windows, and GTK API-version differences.
  - [x] **0149 — Missing GTK4 typelib handling.** GUI test collection skips cleanly
    when PyGObject exists but `gi.require_version("Gtk", "4.0")` cannot load the GTK4
    typelib, matching the launcher's environment handling instead of aborting pytest.
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
- [x] **0189 — Bound Projection notes.** Display each note as a separated item in a
  vertically scrolling region with a capped natural height, so a warning-heavy
  projection cannot enlarge the main window beyond the screen or make later views
  inherit that height.
- [x] Print the current GTK Dashboard, Plan, and Projection and every current web
  view, preserving applied values while keeping print mechanics in the presentation
  boundary. Continue richer scenario/flow reporting as the underlying views grow.
- [ ] **Add native GTK printing.** Render the current structured Dashboard, Plan, and
  Projection state through a GTK-native/system print path without an HTML/browser
  intermediary. Share report layout inputs with existing output, paginate tables and
  notes with repeatable headings, support preview/printer/PDF destinations where the
  platform provides them, and keep the current HTML route as a compatibility fallback
  until the native path is available and tested on supported GTK runtimes.
- [x] Keep Projection and import operations responsive with clear progress and
  cooperative cancellation; extend the same primitive to later expensive workflows.

## In-application help and documentation

Documentation responsibilities are intentionally separated:

- `README.md` — concise user-facing overview and first operational entry point.
- `DESIGN.md` — current architecture, rationale, and durable design decisions.
- `ROADMAP.md` — this file; the sole authoritative future-work list.

Remaining documentation work:

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
- [ ] Move the large completed-patch chronology to a changelog or milestone archive
  once doing so will materially improve roadmap review; keep this roadmap focused on
  remaining outcomes without losing the historical acceptance record.
