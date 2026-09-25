# BreadSched changelog

This file records completed BreadSched milestones Current and proposed work
belongs in `ROADMAP.md`.

## Unreleased

- **Scenario schedule web write adapter.** Move scenario-only and baseline override
  request construction and result translation into the web schedule adapter.
  Preserve existing error, editability, and atomic service behavior. Advance
  application version to `0.2.0a94`; native schema version 7 is unchanged.

- **Fixed-schedule web write adapter.** Move fixed baseline schedule request
  construction and result translation from the main API class to a presentation
  adapter. Keep editability checks, typed service ownership, error messages, and
  persistence behavior unchanged. Advance application version to `0.2.0a93`;
  native schema version 7 is unchanged.

- **Plan web report boundary.** Move typed Plan query translation and response
  projection, including scenario comparison and all reported sections, into a
  read-only resource adapter. Preserve the existing route, response, and service
  error mapping. Advance application version to `0.2.0a92`; native schema version 7
  is unchanged.

- **Plan detail response boundary and trailer discipline.** Extract the read-only
  category, planning-flow, and mortgage detail response from the web API class to
  a dedicated adapter while preserving the HTTP contract and shared calculation.
  Document adjacent DCO/AI trailers and verification of parsed and remote messages.
  Advance application version to `0.2.0a91`; native schema version 7 is unchanged.

- **Expense Explorer documentation and PR completeness.** Expand the packaged
  guide and README with the existing GTK/web comparison, trend, merchant detail,
  and print workflow. Require every PR to update relevant tests, the roadmap,
  changelog, design, guide, and README for all changes it makes. Add a focused
  regression assertion for the guide's category and merchant example. This
  documentation/test-only change leaves application version `0.2.0a90` and native
  schema version 7 unchanged.

- **Expense Explorer web responsibility split.** Move read-only query
  translation and response serialization from `web.server.Api` into a focused
  resource adapter. Keep route parsing, shared financial semantics, API shape, and
  user-visible behavior intact. Advance application version to `0.2.0a90`; native
  schema version 7 is unchanged.

- **Bounded money mutation gate.** Measure engine reporting-currency selection
  and library commodity-tagged Amount operations with selected financial tests. A
  fresh Linurun generated 123 mutants: 118 killed, four surviving message-only
  mutations, and one explicitly reviewed equivalent reporting-fraction call; none
  lacked tests or timed out. CI gates the measured 118/122 eligible baseline in a
  separate ten-minute job, with a clean workspace on each run. Add focused currency
  and tagged-amount tests. This test-only milestone leaves application version
  `0.2.0a89` and native schema version 7 unchanged.

- **Expense Explorer GTK, web, and printing.** Expose shared Plan expense values as
  category comparison bars, time-series trends, exact-value tables, and merchant
  drilldown in both interfaces. Web Plan printing includes the applied explorer;
  the GTK Explorer prints its selected category, period, and merchant contributions.
  The shared contract follows the selected Plan horizon, month/quarter/year grouping,
  as-of boundary, and scenario plan while actuals remain ledger facts. Parent and
  child rows do not double-count section totals. Merchant grouping trims descriptions,
  compares them case-insensitively, labels blanks Unknown merchant, and is temporary;
  category plans remain unallocated to merchants. Multi-split transactions, refunds,
  escrow suppression, and unresolved actuals retain Plan's existing treatment. A
  category estimate of 100 and merchant purchases of 120 show a 20 category overage,
  with no merchant budgets. Advance application version to `0.2.0a89`; native
  schema version 7 is unchanged.

- **Expense Explorer shared contract.** Add a typed read-only query over Plan
  expense categories, periods, section totals, selected-cell occurrences, and
  temporary merchant actual groups. Reconcile the drilldown to its Plan cell,
  without allocating a category budget to merchants or storing group rules.
  Advance application version to `0.2.0a88`; native schema version 7 is unchanged.

- **Versioned historical-estimation rules.** Consolidate anomaly
  thresholds, confidence weights, cadence/trend/seasonal criteria, variability
  bands, and deterministic funding tie-breaks into one immutable
  `historical-estimates-v1` policy passed to the estimator. Include the complete
  applied policy and version in structured proposal and saved-draft evidence, and
  show the version in review explanations. Add an independent reopened-book golden
  with authored calculations for an isolated spike, a funding tie, monthly anchors,
  a repeated seasonal profile, a material trend, and confidence arithmetic; prove a
  deliberately changed named policy against the same book. Advance the application
  version to `0.2.0a87`; native schema version 7 is unchanged.

- **Independent Plan and Projection golden books.** Add five small,
  human-readable native-book declarations covering cash timing and matched actuals,
  classified planning flows, mortgage/escrow treatment, actual/365 projection
  accruals, and scenario replacement/suppression/one-off overlays. Store independent
  calculation notes and static expected Plan/Projection results beside each book;
  materialize each fixture into native SQLite, close and reopen it, then exercise the
  shared service/engine boundaries. Prove monthly and quarterly aggregation,
  planning-flow and mortgage non-additivity, scenario immutability and assumption
  provenance, and monthly stock conservation. This test/documentation milestone
  leaves application version `0.2.0a86` and native schema version 7 unchanged.

## 0.2.0a86 - 2026-09-21

- **Packaged user guide and in-application help.** Move task-oriented user
  information out of the README into one expanded, standalone Markdown guide that
  is shipped as package data. Add an offline GTK **Help → User Guide** (`F1`) reader
  for that same source, keep one guide window per application instance, and trim the
  README to product orientation, development installation/invocation, project links,
  and book-safety essentials. Advance the alpha version to `0.2.0a86`; native schema
  version 7 is unchanged.

## 0.2.0a85 - 2026-09-20

- **release discipline — Publish only tested, documented `main`.** Add an
  opt-in release workflow driven by successful `main` CI, exact-version release-note
  validation, accepted-tip and tag checks, installed-wheel smoke testing, SHA-256
  verification, annotated tags, and immutable GitHub releases with sdist/wheel
  artifacts. Add the human-reviewed `0.2.0a85` notes; application and schema versions
  are unchanged by this workflow/documentation slice.

- **version diagnostics — Report build and native compatibility together.**
  Keep package and native schema versions independent while showing the application
  version, current schema 7, and supported schema 6–7 window in both CLI/GTK version
  output and machine-readable book verification. Advance the alpha version to
  `0.2.0a85`; native schema version 7 is unchanged.

- **reporting-precision completion — Remove the remaining cent assumptions.**
  Carry the reporting currency fraction through Dashboard normalization, reserves,
  and emergency-fund sizing; credit-card payment inference; and historical category,
  classified-flow, trend, seasonal, and cadence estimates. Add whole-unit regressions
  and advance the alpha version to `0.2.0a84`; native schema version 7 is unchanged.

- **commodity-precision slice — Use declared minor units.** Centralize
  reporting and commodity fraction lookup; let exact allocation accept a commodity
  fraction; quantize scheduled posting, projection event application/balance checks,
  and loan payments/previews to the relevant declared fraction rather than cents.
  Route web and GTK loan construction through the reporting-currency fraction and
  add whole-unit and thousandth-unit regressions. Advance the alpha version to
  `0.2.0a83`; native schema version 7 is unchanged.

- **ledger-amount boundary — Reject mixed-currency aggregation.** Expose
  tagged account, recursive, class-total, net-worth, and cash-on-hand balances while
  retaining compatibility `Money` results. Carry transaction currency through the
  derived split query, register running balances, ordinary-account valuation, and
  market-value aggregation. Material amounts in unlike currencies now fail instead
  of silently netting; empty zero balances remain neutral. Consolidate reporting-
  currency selection and add mixed-currency balance/register regressions. This
  completes the commodity-tagged ledger/service boundary and distinct value-versus-
  quantity work. Advance the alpha version to `0.2.0a82`; native schema version 7
  is unchanged.

- **transaction-amount boundary — Keep value and quantity dimensions
  explicit.** Give every new native book a stable default USD commodity and route
  CLI, web, and GTK transaction writes through commodity-tagged split values.
  Validate transaction currency, reject unlike-currency netting, retain account-
  commodity quantity as a separately tagged input, and require it when the account
  commodity differs from the transaction currency. Preserve legacy empty-book
  compatibility by adding the default commodity atomically with the first service
  write. Advance the alpha version to `0.2.0a81`; native schema version 7 is
  unchanged.

- **commodity-amount foundation — Reject dimensionally invalid
  arithmetic.** Add an immutable `Amount(value, commodity)` whose arithmetic,
  comparison, ratios, scaling, and quantization retain or enforce commodity
  identity. Convert security units only through an exact dated `CommodityPrice`,
  rejecting a quote for another commodity, and carry tagged quantity and reporting-
  currency evidence through market valuation while preserving the existing `Money`
  presentation result. Add runtime and mypy closure contracts. Advance the alpha
  version to `0.2.0a80`; native schema version 7 is unchanged.

- **Missing-GTK runtime CI slice — Exercise the partial-install boundary.** Add
  a Linujob with PyGObject installed and the GTK4 typelib explicitly absent.
  Distinguish that state from a usable GTK4 namespace in launcher tests, and prove
  help, version, and a real book launch return the documented missing-runtime error
  without collection failure or traceback. Application version `0.2.0a79` and
  native schema version 7 are unchanged.

- **Formula resource-bounds slice — Deterministic untrusted arithmetic.** Cap
  formula text before and after GnuCash normalization, evaluate every literal,
  variable, operator, and finance function in a fixed local decimal context, and
  reject non-finite or out-of-range results. Normalize syntax, decimal, arithmetic,
  recursion, and memory failures to `FormulaError`. Add adversarial length, base,
  exponent, value, nesting, and ambient-context tests while retaining the
  representative imported mortgage formula suite. Advance the alpha version to
  `0.2.0a79`; native schema version 7 is unchanged.

- **rate-closure slice — Keep dimensionless arithmetic typed.** Replace
  `Rate`'s `Decimal` inheritance with a finite wrapped decimal whose direct and
  reflected arithmetic, unary operations, rounding, quantization, and `divmod`
  remain `Rate` values. Retain exact decimal persistence, comparison, hashing, and
  presentation formatting; reject rate/money addition while preserving monetary
  scaling and division. Add runtime coverage plus a mypy-checked arithmetic contract
  to the core type-check gate. Advance the alpha version to `0.2.0a78`; native schema
  version 7 is unchanged.

- **dialog-structure slice — Separate editor construction by
  responsibility.** Reduce the account and schedule dialog constructors to ordered
  orchestration over choice loading, related control groups, timeline/recurrence
  sections, actions, and initial-state loading. Preserve widget creation order,
  callbacks, imported/read-only behavior, and request construction under the
  existing GTK characterization suite. Together with the verification,
  category-report, and estimate slices, this completes the 0223 structural work.
  Advance the alpha version to `0.2.0a77`; native schema version 7 is unchanged.

- **estimate-structure slice — Isolate historical observation.** Move
  per-category monthly ledger scanning, escrow recognition, future-plan coverage,
  and residual/gross evidence collection into one typed history result consumed by
  proposal scoring. Preserve proposal amounts, cadence, evidence, ordering, and
  conservative filtering under the historical-estimate characterization suite.
  Advance the alpha version to `0.2.0a76`; native schema version 7 is unchanged.

- **category-report-structure slice — Separate report assembly.** Extract
  category hierarchy roll-up, period variance calculation, cash-bridge rows,
  planning-flow rows, and mortgage-payment rows from the event accumulation path.
  Preserve exact dated planned/actual values, ordering, future-period null variance,
  and cash-bridge conservation under the existing Plan characterization suite.
  Advance the alpha version to `0.2.0a75`; native schema version 7 is unchanged.

- **verification-structure slice — Responsibility-specific diagnostics.**
  Decompose exhaustive domain verification into a read-once state snapshot and
  focused commodity, account, reconciliation, price, transaction, schedule, and
  scenario checkers. Preserve issue codes, messages, handles, ordering, and the
  non-destructive public entry point under the existing corruption-characterization
  suite. Advance the alpha version to `0.2.0a74`; native schema version 7 is
  unchanged.

- **error-catalog slice — Presentation-owned localization seam.** Map every
  stable application-service error code and field path to human-readable wording at
  one shared presentation boundary, removing the remaining web-only schedule map.
  Load that English wording through packaged gettext catalogs without exposing prose
  in the service contract. Add literal-code coverage, compiled-catalog validation,
  and Spanish locale smoke tests through both web and GTK error paths. Advance the
  alpha version to `0.2.0a73`; native schema version 7 is unchanged.

- **schedule-lifecycle slice — Complete cross-interface mutation
  migration.** Route GTK/web schedule duplication and deletion plus CLI estimate
  add/remove through typed schedule services. Preserve protected schedule structure,
  reject stale handles and live scenario references with stable errors, and enforce
  adapter boundaries with service and architecture regressions. Together with the
  preceding 0223 slices, GTK and web now perform no direct financial/domain mutation
  persistence and all cross-interface CLI mutations use shared services. Advance the
  alpha version to `0.2.0a72`; native schema version 7 is unchanged.

- **account-service slice — Typed account lifecycle and settings.** Route
  GTK and CLI account add/edit/delete plus web type, emergency-fund, card-payment,
  and FSA-year updates through one typed service. Validate account identity, parent
  chains, sibling names, commodities, linked assets, card relationships, funding
  periods, and imported source-owned fields with stable errors. Keep account creation
  and its optional opening balance atomic, and normalize protected/in-use deletion
  failures. Add service and architecture regressions. Advance the alpha version to
  `0.2.0a71`; native schema version 7 is unchanged.

- **transaction-adapter slice — Complete shared transaction writes.** Route
  CLI posting/edit/delete, GTK register quick posting, and dialog deletion through
  the typed transaction service already used by the full GTK editor and web. Preserve
  split identity and imported metadata on edits, provide stable stale-delete errors,
  and enforce the boundary with service, CLI, and architecture regressions. Advance
  the alpha version to `0.2.0a70`; native schema version 7 is unchanged.

- **projection-assumption-service slice — Typed assumption writes.** Route
  CLI, GTK, and web Base and saved-scenario assumption persistence plus dated-regime
  creation, replacement, and deletion through shared typed services. Validate scalar
  and account-specific rates, account roles, projection horizons, scenario identity,
  and period indexes with stable codes and field paths; keep saved-scenario commits
  atomic through the lifecycle boundary. Add service and architecture regressions.
  Advance the alpha version to `0.2.0a69`; native schema version 7 is unchanged.

- **scenario-service slice — Typed saved-scenario lifecycle.** Route CLI,
  GTK, and web saved-scenario creation/update, duplication, deletion, reparenting,
  and baseline-schedule suppression through shared typed services. Validate names,
  identities, parent chains, cycles, children, and source schedules with stable
  codes and field paths while retaining atomic writes. Add service and architecture
  regressions. Advance the alpha version to `0.2.0a68`; native schema version 7 is
  unchanged.

- **Review-service slice — Typed plan-resolution decisions.** Route CLI,
  GTK, and web match, reject, skip, unexpected, and FSA-attachment mutations through
  shared typed services. Validate stale transactions and occurrences with stable
  codes and field paths, retain atomic transaction/schedule/claim writes, and keep
  candidate ranking as read-only engine logic. Add service, adapter, architecture,
  and web error regressions. Advance the alpha version to `0.2.0a67`; native schema
  version 7 is unchanged.

- **import-service slice — One typed import workflow.** Route CLI, GTK,
  and web source validation, importer selection, format overrides, execution, and
  last-successful-source recording through one typed service. Preserve background
  progress and GTK main-thread notification behavior, expose preflight failures as
  stable codes and field paths, and enforce the boundary with service, adapter, and
  architecture regressions. Advance the alpha version to `0.2.0a66`; native schema
  version 7 is unchanged.

- **loan-service slice — Typed, atomic loan creation.** Route GTK and web
  loan mutations through one typed service that validates lender terms and account
  roles with stable codes and field paths before creating the formula schedule and
  optional opening balance atomically. Keep preview arithmetic in the shared engine,
  centralize adapter wording, and enforce the boundary with service and architecture
  regressions. Advance the alpha version to `0.2.0a65`; native schema version 7 is
  unchanged.

- **claim-service slice — Typed FSA claim lifecycle.** Make GTK and web
  submit typed claim, allocation, link, and rejection inputs to one shared mutation
  service instead of constructing or persisting claim objects. Preserve atomic claim
  and linked-split updates, expose expected validation through stable codes and field
  paths, and route deletion through the same boundary. Add service and architecture
  regressions. Advance the alpha version to `0.2.0a64`; native schema version 7 is
  unchanged.

- **reconciliation-service slice — Structured statement mutations.** Route
  GTK and web statement start, selection/balance updates, completion, cancellation,
  and reopening through typed application-service requests and results. Give every
  expected domain refusal a stable code and field path while retaining exact shared
  calculations, atomic ledger/audit writes, and backward-compatible engine errors.
  Extend architecture and service regressions. Advance the alpha version to
  `0.2.0a63`; native schema version 7 is unchanged.

- **transaction-service slice — Atomic typed ledger entry.** Route GTK and
  web transaction creation/editing through one typed service with stable error
  codes and field paths. Preserve imported and reconciliation-owned split metadata,
  exact transaction value versus account quantity, and hidden accounts already
  referenced by an edited entry. Commit optional FSA claim attachment in the same
  database transaction, and centralize adapter-owned error wording. Add parity,
  rollback, architecture, hidden-account, and native round-trip regressions.
  Advance the alpha version to `0.2.0a62`; native schema version 7 is unchanged.

- **follow-up — Portable web read-path narrowing.** Express the
  file-backed read-path guard with explicit `None` and in-memory checks so every
  supported mypy version narrows the path to `str` before opening the read-only
  database. Advance the alpha version to `0.2.0a61`; runtime behavior and native
  schema version 7 are unchanged.

- **Web resource split and boundary hardening.** Separate strict typed
  query/resource routing and authenticated HTTP transport from the financial web
  adapter. Bound and validate request framing, map expected failures to stable
  status/code/field responses, and return only correlation identifiers for
  unexpected failures. Package external browser assets, construct charts without
  interpolated markup, and enforce a directive-specific Content Security Policy
  without inline exceptions. Keep one serialized writer while file-backed GET and
  Projection requests use short-lived SQLite read-only connections; prove concurrent
  reads, read/write visibility, cleanup, shutdown, and writer-lock ownership. Add
  socket, packaging, security, concurrency, and architecture regressions. Advance
  the alpha version through `0.2.0a58`–`0.2.0a60`; the native schema remains version
  7.

- **Typed service foundation and first vertical slices.** Add shared,
  presentation-neutral service contracts with typed values and stable error codes
  and field paths. Put baseline and scenario schedule construction, validation, and
  transaction ownership behind those services, including fixed and protected
  formula definitions, recurrence details, amount timelines, account roles, and
  balancing. Make GTK and web schedule adapters submit the same typed requests and
  make both Plan surfaces consume one typed query result. Enforce those boundaries
  with architecture tests and prove baseline/scenario request parity while retaining
  native and GnuCash save, reload, and source-refresh fidelity coverage. Advance the
  alpha version to `0.2.0a57`; the native schema remains version 7.

- **Architecture and contribution-policy review.** Reconcile the
  architecture, persistence, migration, money/rate, formula, web-security,
  concurrency, packaging, documentation, testing, and governance review against the
  live tree and convert each accepted, narrowed, staged, or rejected recommendation
  into ordered roadmap acceptance criteria. Make the shared-service extraction an
  architecture gate before new financial features. Distinguish the application
  version used for bug/release identity from the native data-format version used for
  migration, and commit to retaining two predecessor formats when the next real
  schema change occurs. Remove the dead `.gpr.py` package-data pattern rather than
  implying support for runtime-scanned plugins. Require author DCO sign-offs and
  `Assisted-by:` disclosure for material AI assistance, normalize the maintainer's
  historical/GitHub author alias to the canonical DCO identity with `.mailmap`, and
  enforce that canonical identity through trusted-base pull-request CI and a review
  checklist. No application version change because this milestone changes project
  policy and repository/packaging metadata, not runtime behavior.

- **Category-specific inference.** Require supported category history
  before inferring weekly, fortnightly, or seasonal behavior; preserve stable
  category posting-day/weekday anchors; and explain sparse or noisy evidence that
  is deliberately rejected. Add sparse-history, false-positive, and accepted
  seasonal-estimate convergence regressions. Advance the alpha version to
  `0.2.0a55`.

- **Interactive estimate adjustment.** Build GTK and web historical
  review drafts through the same engine service, then allow amount, cadence, first
  date, seasonal month amounts, and category planning classification to be changed
  before saving. Apply one shared acceptance guard to both surfaces and retain the
  original structured evidence on accepted Base and scenario estimates. Advance
  the alpha version to `0.2.0a54`.

- **Structured estimate evidence.** Give every historical estimate one
  shared evidence model covering each selected or excluded history month, gross
  activity, applied future-plan coverage, residuals, observed cadence dates,
  trend, seasonality, named confidence factors, and ranked funding-account
  candidates. Present the shared summaries in GTK and expose the full structure
  through web/API and the read-only `estimate suggest` CLI. Advance the alpha
  version to `0.2.0a53`.

- **Advanced deterministic recurrences.** Add GnuCash-compatible nth-
  weekday and last-weekday monthly rules with canonical fifth-weekday behavior,
  stable occurrence identities, bounds, weekend adjustment, native round trips,
  and SQLite/XML import convergence. Expose only these proven-safe patterns in
  shared editability, GTK, web, scenarios, projections, and loan cadence handling.
  Advance the alpha version to `0.2.0a52`.

- **Per-leg amount timelines.** Add exact effective-dated changes to
  individual fixed schedule legs, including signed funding and deduction amounts.
  Preserve timelines through native serialization, safe editors, scenario copies,
  and unambiguous GnuCash source refresh; reject effective combinations that do not
  balance. Carry each leg's amount source into shared Plan and Projection
  explanations across GTK, web, and API. Advance the alpha version to `0.2.0a51`.

- **Restored migration infrastructure.** Restore the explicit sequential
  registry and durable ledger, transactional runner, verified pre-migration backup,
  immediately preceding schema-6 fixture, rollback tests, and recovery guidance.
  Retain the rolling one-version alpha policy without coupling the safety mechanism
  to a new native schema. Advance the alpha version to `0.2.0a50`.

- **Matrix-proven schedule editing.** Give GTK and web one shared
  editable projection for primary, funding, and additional fixed splits, including
  planning-only and unambiguous balance-sheet transfers. Add web formula editing
  for validated expressions, named variables, recurrence, and metadata while
  protecting formula-owned accounts and amount timelines. Cover native fixed
  parity, crafted formula payloads, and GnuCash SQLite/XML save, reload, and source
  refresh ownership. Advance the alpha version to `0.2.0a49`.

- **Shared schedule editability contract.** Centralize the fixed,
  formula, and read-only decision with one actionable reason in the schedule
  engine. Use that result in GTK selection/edit flows, web/API definition payloads,
  and the web mutation guard so presentation layers cannot independently guess
  whether an imported definition is safe to rewrite. Advance the alpha version to
  `0.2.0a48`.

- **Imported schedule fidelity matrix.** Add representative generated
  native, GnuCash SQLite, and GnuCash XML schedule contracts covering unusual and
  bounded recurrences, weekend movement, formulas, flags, local overrides, and
  source refresh. Preserve multiple GnuCash recurrence rules as an opaque read-only
  definition instead of silently selecting one; retain BreadSched-owned schedule
  timelines, exceptions, formula inputs, completion state, and unambiguous split
  classifications across source refresh. Advance the alpha version to `0.2.0a47`.

- **Separate Dashboard income and bills.** Present committed pending
  bills and expected income in distinct lists across GTK, web, API, CLI, and print,
  and document why future credit-card purchases remain planning expenses without
  becoming immediate cash obligations. Advance the alpha version to `0.2.0a46`.

- **Statement-based credit-card obligations.** Freeze an unpaid overdue
  occurrence at the card balance on its due date and hold only subsequent activity
  for the next occurrence. Let any positive payment from a cash-like account,
  including a partial payment, resolve the overdue occurrence while excluding
  refunds and reversals. Hold each account-payment row exactly once, suppress
  monthly/annual display amounts, and retain explicit-schedule precedence without
  double-counting card purchases as immediate cash bills. Advance the alpha version
  to `0.2.0a45`.

- **Separate commitments from estimates.** Keep planning estimates in
  Plan and Projection without presenting them as due or overdue obligations in
  Dashboard or Upcoming. Split Scheduled into commitments/account payments and
  estimates, and expose committed versus estimate-inclusive income and outgoings
  through the shared Dashboard model, GTK, web, API, CLI, and print output. Advance
  the alpha version to `0.2.0a44`.

- **Repository contribution guidance and history split.** Make
  `CONTRIBUTING.md` the complete contributor and agent workflow, add a repository-
  wide `AGENTS.md` pointer, move completed roadmap history here, and keep
  `ROADMAP.md` limited to unfinished outcomes. No application version change.

### Architecture and hardening

- **Formula-loan economic consistency.** Formula-driven schedules are not
  escalated by generic expense inflation, and liabilities whose interest is already
  represented by formula schedules do not also receive generic liability interest.

- **Explicit schedule growth policy.** Baseline and scenario schedules
  persist `auto`, `none`, `income`, or `inflation`; mixed gross-to-net payroll grows
  correctly in automatic mode; GTK and web expose the same policy controls.

- **Recurrence occurrence numbering.** Formula period numbers are carried
  from nominal occurrence generation and are not re-derived from weekend-adjusted
  dates. Semi-monthly and adjusted schedules retain the correct ordinal. The old
  10,000-step `next_after()` cutoff is removed.

- **Start-of-period loan mathematics.** `due=1` `ipmt()`/`ppmt()` behavior
  matches independent annuity-due reference values and repays correctly.

- **Formula evaluator hardening.** Fractional powers work; normal function
  commas are not mistaken for GnuCash grouping commas; expression depth/node/power
  complexity is bounded; evaluator failures normalize to `FormulaError`.

- **Local web security.** Loopback Host/Origin validation, per-server API
  token, and JSON-only writes protect the web parity surface from hostile local
  browser requests and DNS-rebinding-style access.

- **Re-import ownership boundary.** GnuCash-owned account/transaction fields
  may refresh while BreadSched-owned planning, projection, FSA, resolution, notes,
  and split classifications survive stable-GUID re-import.

- **Incremental write verification.** Normal commit/undo/redo validates
  changed records and reverse references instead of scanning the whole book. Full
  `verify_book()` remains the exhaustive diagnostic.

- **Transactional metadata/FSA claims.** Direct metadata commits cannot
  escape an active `DbTxn`; transactional metadata participates in undo/redo; FSA
  claims are first-class persisted records and save atomically with reimbursement
  split classifications.

- **First expanded quality gates.** A dedicated serial performance gate
  measures single-write cost on a synthetic 30,000-transaction household book;
  Hypothesis-based recurrence properties exercise randomized intervals, dates,
  weekend adjustments, serialization, and occurrence identity. Core xdist runs
  exclude the dedicated performance marker, which executes once in CI.


### Dashboard and application hardening

- **Keep paid-in-full card payment days editable.** A card cleared each
  month still has a payment due day. Only the usual carried-balance payment amount
  depends on the cleared-in-full setting; the due day remains editable and persists
  when the setting changes or the editor is reopened.

- **Bound recently closed FSA years on the Dashboard.** GTK and web use
  the shared query: show open years and at most one recently closed year per FSA
  account, only through 90 days after its run-out deadline (or year end when no
  run-out exists). Older years remain available in history. This supersedes the
  earlier request to hide every closed year immediately.


### Dashboard balances and group hierarchy

- **Account-controlled emergency-fund expenses.** Add an explicit account
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


- **FSA group availability.** Show remaining funds for the applicable plan
  year(s), using the shared FSA funding-year calculations at the Dashboard's as-of
  date, instead of the custodial account ledger balance. Cover overlapping plan
  years/run-out periods, exhausted and expired years, and missing year definitions.
  GTK and web must present the same result and make unavailable year data explicit.

- **Colon-separated group paths.** Accept account-style paths in configuration
  and account group fields. For example, `Investments:Plan A` and `Investments:Plan B`
  display as `Plan A` and `Plan B` under an `Investments` heading. Each leaf shows its
  own total; each heading shows the sum of its children and any directly assigned
  accounts. Support deeper paths and persist the full paths while displaying local
  names. Compute the hierarchy and totals in the engine for GTK/web/CLI parity.

- **Account-subtree deduplication.** A grouped parent account includes its whole
  subtree exactly once. Explicitly selected descendants must not appear or be added
  again beneath the same grouped parent. Resolve overlapping group assignments
  consistently so heading/grand totals and liquidity calculations cannot count a
  descendant twice. Cover parent-plus-child selections, nested descendants,
  repeated handles, and mixed assets/liabilities with generic regression fixtures.

- **Paid-off loans.** Omit paid-off loan entries from the Dashboard, including
  loans linked to an asset. Preserve the asset's own visibility and value exactly
  once; a linked asset must not keep a paid-off loan row visible. Test multiple
  loans on one asset and distinguish a zero loan balance from a fully repaid loan
  that still has stale future schedule occurrences.


- **Expanded quality gates.**
  - **Add CLI/web to the mandatory mypy gate.** Correct CLI and web type
    errors and annotate comparison/dashboard results. The extended gate follows
    imports for type information but suppresses errors in imported GUI modules.
  - **Add GUI modules to mandatory mypy coverage.** Correct optional
    value narrowing, collection annotations, and per-account Rate assignments;
    share dialog-close refresh callbacks and repair the invalid exception class
    in opening-balance parsing. The extended gate checks all presentation modules
    and their imports without a suppressed GUI baseline. Dynamic PyGObject APIs
    still require the GTK runtime tests.
    Projection controls also preserve per-account rates when rebuilding assumptions.
  - **Make formatting mandatory.** Apply Ruff formatting across source,
    tests, and examples; enforce `make format-check` in `make check` and CI.
    `make fmt` now applies both Ruff lint fixes and formatting.
  - Keep Host/Origin/token web-security regressions in the standard gate.
  - Add property-based recurrence tests around generated occurrence identity,
    weekend adjustment, and serialization. Continue adding properties as new
    collision/edge cases are discovered.
  - Add realistic storage/projection performance gates on one shared synthetic
    30,000-transaction household history: an ordinary commit stays below 0.5 seconds
    and a 30-year projection stays below a deliberately generous 3-second budget.
    Add report-specific benchmarks as those paths are hardened.
  - Continue pytest-xdist rollout: core/non-GTK uses `pytest -n auto`; GTK stays
    serial and the realistic performance gate runs separately with `-n 0`.
  - Preserve `make test-ordered` / `pytest -n 0` as a deterministic diagnostic path.

- **Core parsing/comparison safety.** Ambiguous locale-formatted strings are rejected instead of silently mis-scaled; equality/hash behavior follows Python's numeric contract; non-numeric equality does not raise.

- **QIF/OFX number-format parsing.** Detect period-vs-comma decimal conventions from the complete import file, parse grouping explicitly, reject conflicting conventions, and allow an explicit importer override for ambiguous files.

- **QIF date-order parsing.** Detect month-first versus day-first ordering from complete-file evidence, reject conflicting evidence, preserve year-first dates, and allow an explicit importer override for all-ambiguous files.

- **Locale-aware GTK/web amount entry.** Route typed amounts through one boundary parser, accept period- or comma-decimal input without weakening core `Money`, send the browser decimal convention with writes, and remove JavaScript floating-point conversion from FSA amount entry.

- **Dimensional Money/Rate semantics.** Reject `Money * Money`, make
  `Money / Money` an exact dimensionless ratio, and represent scenario growth,
  inflation, return, and interest assumptions with a Decimal-compatible `Rate`
  type while preserving existing serialized scenario data.

- **Initial commodity-safe valuation boundary.** Preserve exact split
  quantity separately from transaction-currency value, apply only direct dated
  security-to-reporting-currency quotes, and retain ledger value explicitly when
  no compatible quote exists. Multi-currency conversion and lot accounting remain
  separate work below.

- **Platform-correct user paths.** Settings use XDG/APPDATA/macOS
  Application Support as appropriate; Documents discovery honors XDG user dirs and
  common Windows OneDrive redirection; recognized cloud-sync roots emit an SQLite
  durability warning when a book is opened there.

- **Inter-process writer lock.** Writable native books use an owned
  sidecar lock, competing writers fail with an explicit read-only alternative,
  read-only opens remain allowed, stale same-host locks are safely reclaimed, and
  canonical path identity prevents a symlink alias from bypassing the writer lock.

- **Clean schema-4 event-planning core.** Remove the retired monthly
  Budget domain, its alternate projection engine, compatibility commands/routes,
  and obsolete product naming while preserving all external GnuCash/QIF/OFX/QFX
  import paths. The temporary alpha-schema cleanup retained here was superseded and
  removed by 0190. Advance the alpha version to `0.2.0a4`.

- **Single account types and linked properties.** Normalize the then-current
  account representation to one visible account type. On the Dashboard, an
  explicitly assigned asset or loan pulls in visible,
  otherwise-unassigned companions from its stored link so
  property value, debt, equity, and LTV remain together without restoring inferred
  default groups. Report the latest bounded enabled repayment date as the loan end
  across engine, GTK, web, and CLI, and advance the alpha version to `0.2.0a5`.

- **Dated security prices and current valuation.** Add schema-6 exact,
  dated commodity prices and indexed split quantities; preserve GnuCash SQLite/XML
  prices by stable GUID; expose locale-aware GTK/web security price entry; and use
  shared as-of market valuation for Investment/Retirement accounts in Accounts,
  Dashboard groups, net worth, and Projection opening state. Generated Dashboard
  path headings expose only rolled-up equity/total, leaving property value, owed,
  LTV, and loan end on the specific leaf. Repair the web Plan script syntaexposed
  by executable JavaScript checking, and advance the alpha version to `0.2.0a6`.

- **Dated pending cash flow and separate FSA Dashboard.** Move benefit-year
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

- **Import memory and hidden-account entry safety.** Remember the last
  successful import source separately for each destination book across CLI, GTK,
  and web entry points; preselect it without automatically importing or writing to
  the source. Omit hidden accounts from new GTK/web transaction choices, reject
  crafted web writes that name them, and preserve visibly labelled hidden accounts
  when an existing GTK transaction is edited. Advance the alpha version to
  `0.2.0a8`.

- **Scheduled lifecycle and navigation.** Add shared operations and GTK/
  web workflows for undoable definition deletion, safe reviewed duplication, and
  unsaved one-time drafts made from existing ledger transactions. Preserve every
  draft split's account, value, memo, and planning purpose; give copies independent
  identities and occurrence state; retain posted actuals on deletion; and refuse
  deletion that would strand scenario overrides. Activate Dashboard and Upcoming
  rows into their schedule/account workflow without posting. Preserve schedule
  memos through the web editor, protect hidden accounts at new-schedule boundaries,
  and advance the alpha version to `0.2.0a9`.

- **Reviewed historical-estimate drafts.** Make Suggest from History open
  an unsaved, populated Base or saved-scenario schedule editor instead of writing
  immediately. Preserve inferred cadence and seasonal values through GTK/web review;
  Save the user's final values and make Cancel a no-op. Keep one GTK/web suggestion
  window per book/context, foreground it on repeated requests, and advance the alpha
  version to `0.2.0a10`.

- **Cadence-safe historical coverage.** Match planned coverage to exact
  year/months in the rolling future window. Bound monthly bridge estimates before a
  sustained part-year replacement begins without letting an isolated future event
  hide recurring need. Infer annual, biennial, and triennial amounts and next dates
  without annualizing active-month values; represent a lone completed event only as
  a reviewed one-time draft; and advance the alpha version to `0.2.0a11`.

- **Imported transaction notes.** Preserve GnuCash transaction-level
  notes separately from split memos and BreadSched-authored notes in both SQLite
  and XML imports. Refresh source notes while retaining local notes on re-import;
  expose both in GTK transaction details and web registers, allow GTK/web entry of
  local notes, and advance the alpha version to `0.2.0a12`.

- **Safe SQLite scheduled formulas.** Replace SQLite import's character
  whitelist with validation by the same bounded formula engine used at runtime.
  Preserve supported arithmetic, financial functions, and `period`/`i` occurrence
  variables dynamically with XML parity, retain the no-arbitrary-code boundary,
  and advance the alpha version to `0.2.0a13`.

- **Printable current reports.** Add one GTK Print current view action
  for the applied Dashboard, Plan, and Projection, using private self-contained HTML
  previews that retain report values, totals, annual assumptions, charts, and active
  Projection comparisons and can print or save as PDF. Add browser-native printing
  for every current web view with print styling that removes navigation and expands
  scrollable report tables. Keep Base and scenario estimate draft types distinct in
  the full-cache extended mypy gate. Advance the alpha version to `0.2.0a14`.

- **Wait for printable Projection state.** Synchronize the GTK print
  boundary with an in-flight Projection worker before reading the visible result.
  Report a bounded wait failure rather than silently leaving the preceding preview
  open, strengthen the GUI regression to require one new preview per action, and
  advance the alpha version to `0.2.0a20`.

- **Responsive Projection and import work.** Move GTK Projection and
  import work off the main thread, give Projection an independent read-only worker
  connection, deliver progress/results with `GLib.idle_add`, and cooperatively
  cancel calculations or atomically roll back imports. Retain deliberate
  `DELETE`/`FULL` SQLite durability under the single-writer book lock and advance
  the alpha version to `0.2.0a18`.

### Planning, scenarios, and projection

- Plan is derived from actual, scheduled, and estimated **dated** transactions,
  not stored monthly budget cells.

- Base and saved scenarios support dated assumptions and scenario-specific
  estimate add/alter/suppress behavior.

- Every account has one semantic BreadSched type, with accounting class and
  planning behavior derived from it. Exact imported GnuCash source type is retained
  separately and explicit split planning purpose remains an override.

- Schedule growth policy is persisted for baseline/scenario schedules and exposed
  in GTK4 and web.

- Formula-loan projection is protected from double interest and generic inflation,
  and recurrence/formula period numbering is stable across date adjustments.


### Scheduled transactions

- Occurrences support bounds, future-effective amounts, skips, one-time overrides,
  and fixed multi-split schedules.

- Every GTK Scheduled row is inspectable; unsupported shapes open a read-only
  detail view rather than becoming inaccessible.

- Repeated account legs and per-split memos round-trip in fixed schedules.

- Initial GTK Scheduled selection correctly enables View/Edit without requiring a
  second click.

- Imported recurrence multipliers and hidden recurrence details such as end-of-
  month/semi-month firing are preserved when unchanged.

- Fixed planning transfers, ordinary balance-sheet transfers, multi-leg transfers,
  opposite-direction additional legs, and multiple planning-purpose legs are editable
  when they can be rebuilt losslessly.

- Base and scenario formula schedules allow validated formula/variable editing
  while protected split/account/timeline semantics remain intact.


### Accounts and GnuCash account fidelity

- Accounts view uses the single BreadSched account type; Account, Type,
  Description, and Balance are hierarchy-aware sortable columns.

- Account editor preserves ordinary non-placeholder parents without allowing
  cycles.

- Imported commodity/security, hidden flag, account notes, and per-account
  commodity SCU/precision are visible/preserved.

- GnuCash re-import preserves BreadSched-owned account planning/projection/FSA
  configuration while refreshing source-owned account fields.


### FSA / benefit workflows

- FSA funding years, election, run-out, availability, used/remaining/forfeited
  concepts are separate from ordinary custodial ledger balance.

- Claims/service episodes support multiple payment links, allocations,
  reimbursements, refunds, rejections, and Review/Dashboard workflows.

- FSA claims are first-class transactional persisted objects with atomic
  undo/redo and linked split classifications.


### Import and interoperability

- Native deterministic QIF and banking/credit-card OFX/QFX import exists.

- Stable source identities protect re-import from unrelated record insertion.

- GnuCash re-import preserves BreadSched-owned account/transaction/split state by
  stable source GUID while refreshing source-owned ledger facts.


### Dashboard / UI architecture

- Dashboard groups are explicit and account-owned or configuration-owned; there
  are no inferred default groups. Hidden accounts are not direct group members.

- Dashboard bills are schedule/Plan driven and do not depend on a hidden legacy
  current-budget selector.

- GTK4 is the canonical/reference interface; web parity is required for financial
  behavior and major workflows.


### Account editor and Accounts view

- **Durable imported-account provenance.** Retain the exact GnuCash
  source GUID independently from the BreadSched object handle so roots and
  pre-existing top-level accounts adopted during import continue to match after a
  source rename or move. Preserve exact unknown source type text and typed account
  fields for read-only inspection instead of normalizing them into BreadSched
  semantics. Map historical checking/savings/money-market/credit-line/CD types
  explicitly; put an unknown new type in Technical pending review. Expose ordinary
  and source-owned metadata through GTK, web, and CLI JSON; verify source-GUID
  uniqueness; cover SQLite, nested XML slots, and re-import. Advance the alpha
  version to `0.2.0a27`.

- **Fixture-driven imported-account fidelity.** Build one representative matrix
  of GnuCash account forms and relationships covering investment, debt, FSA,
  commodities/securities, and unusual valid metadata. Expand safe editing and
  explanations only for forms whose source semantics can demonstrably round-trip;
  expose every other supported form inspectably and read-only without destructive
  normalization. Add a fixture and round-trip/regression proof whenever the editable
  surface grows. The generated SQLite contract covers nested STOCK/MUTUAL holdings,
  a security commodity and precision, liabilities, local Loan/FSA semantics,
  historical RECEIVABLE/PAYABLE/MONEYMRKT types, an unknown valid type, typed slots,
  source refresh, and preservation of local fields. This test-only milestone does
  not advance the application version.

- **Separate imported and local account notes.** Retain GnuCash account
  notes as read-only source provenance while keeping BreadSched-authored account
  notes independently editable across re-import. Expose both through GTK, web, and
  CLI JSON. Migrate the preceding shared field only when typed source metadata
  proves its origin, preserving ambiguous values as local rather than guessing.
  Advance the alpha version to `0.2.0a42`.

- **Protect imported source-owned chart fields.** Keep an imported
  account's name, parent, code, description, commodity/SCU, placeholder, and hidden
  state read-only in GTK and reject corresponding CLI edits. Continue allowing
  BreadSched-owned type, notes, grouping, projection, FSA, loan, and card decisions.
  Make the source/local boundary explicit so a local edit cannot appear durable and
  then disappear on re-import. Advance the alpha version to `0.2.0a43`.


### Register workflow

- **Hidden account choices.** Exclude hidden accounts from account lists for
  new transaction/split entry in GTK and web. When editing a transaction already
  referencing a hidden account, preserve and identify that existing selection;
  filtering must never silently replace a stored split account.

- **One split-based transaction model.** Present ordinary entry as two splits
  by default, with the same split model, validation, and editing path used for
  additional legs. Retain atomic balanced postings and avoid separate financial
  semantics for a two-account shortcut.


- **Independent registers.** Allow multiple register windows/views at the
  same time; filters, selection, edit state, and navigation remain local to each
  window/view.

- **Inline basic entry.** Support entering ordinary basic/two-sided
  transactions directly in the register, similar to GnuCash, with full split editing
  available when needed.

- **One atomic transaction path.** Preserve atomic double-entry validation
  for all inline editing. Share account-specific direction labels across GTK/web,
  exclude hidden transfer accounts, and advance the alpha version to `0.2.0a31`.


### Reconciliation

- **Add first-class account reconciliation:** statement date, ending balance,
  cleared/reconciled state, running difference, completion, cancel/restart, and
  auditable persistence.

- Put reconciliation rules in shared domain/application services first; GTK and
  web are presentations of the same workflow.

- Cover reopened/corrected statements and preservation of imported reconcile state.
  Persist statement sessions and append-only lifecycle events; finish and reopen
  split changes atomically, require latest-first correction, and verify stored split
  references. GTK and web registers share the same exact difference calculation.
  Advance the alpha version to `0.2.0a24` and native schema to 7.


### Historical estimator

- **Restore estimate convergence and monthly future coverage.** Gross
  need comes from historical actuals; the selected future plan contributes the
  coverage, once, for each calendar month. Both commitments and accepted estimates
  count, even if they began after history; expired, inactive, and scenario-suppressed
  schedules do not. Cover updated amounts, partial/full acceptance, and explain
  historical median, applied future coverage, and remaining median separately.

- **Long-cycle and partial-year future coverage.** The next twelve planning
  months establish one future sample of each calendar month. A schedule every two
  or three years, a seasonal schedule that only begins partway through the future
  year, or a scenario with changing coverage needs cadence-aware matching of
  historical need against the exact future windows. Avoid suppressing an uncovered
  near-term month because a later year has a scheduled occurrence, and avoid
  projecting a completed one-time expense as recurring. Define and test those
  cases before extending the current one-year window.

- **Count all future split categories.** Future committed and estimated
  multi-split events contribute each income/expense leg, including repeated legs
  and refunds to the same category, with exact signed totals.

- **Review before acceptance.** The suggestion's action opens the
  populated Add Scheduled Transaction editor for adjustments to amount, accounts,
  recurrence, dates, and splits. Commit only after Save; Cancel must leave no new
  schedule. Reanalysis must use the actual saved values.

- **One suggestion window per book/context.** Repeating Suggest from History
  should foreground the existing window, not create another. Handle closing,
  changing books, and scenario changes without stale references.

- **Stable Add placement in GTK.** Place each suggestion's Add button
  before its description so resizing does not detach the action from its item.
  The web table already displays its action in the proposal's own row.


- **Robust cadence, outliers, and confidence.** Recognize stable
  multi-month recurrence despite day-of-month drift; conservatively exclude and
  disclose isolated amount anomalies only with sufficient history; and score
  confidence from coverage, depth, retained evidence, and robust variability.
  Advance the alpha version to `0.2.0a32`.

- **Historical-estimator transaction semantics.** Interpret an entire
  transaction before extracting ordinary Income/Expense history. Reinvested
  dividends/interest, investment fees, and rollovers do not become recurring
  household income or expense suggestions. When a multi-split loan payment,
  retirement contribution, or benefit allocation contains several balance-sheet
  counterparts, retain the observed recurring counterpart and prefer spendable
  cash to break equal-evidence ties. Apply the same exclusions to future coverage
  and cadence evidence. Advance the alpha version to `0.2.0a40`.

- **Propose classified historical planning flows.** Add distinct,
  reviewable proposals for recurring retirement saving/distribution, investment
  contribution/withdrawal, debt principal, and benefit/FSA funding. Preserve the
  shared planning-flow or investment-activity classification in accepted Base and
  scenario estimates and subtract matching future classified coverage exactly once.
  Keep duplicated cash-counterpart annotations from producing a second retirement
  distribution proposal. Advance the alpha version to `0.2.0a41`.


### Plan and planning-flow reporting

- **Account kinds and initial Escrow planning/projection.** This historical
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

- **One semantic account type and explicit dashboards.** Replace the
  user-visible ledger-type/account-kind pair with one BreadSched-owned type whose
  accounting class and planning behavior are derived. Retain exact GnuCash source
  type as read-only provenance; preserve the local type on re-import and report
  cross-class conflicts. Document every visible type
  and the mapping rationale. Remove inferred dashboard groups, honor only explicit
  account/config assignments, and omit hidden direct members. Begin PEP 440 alpha
  versioning at `0.2.0a1` from one authoritative version source.

- **Persistent Plan controls and complete totals.** Store From, Through,
  Group by, Show, scenario, and comparison per book for GTK/web parity. Add a Total
  column for every category and planning-flow row, non-duplicating section totals
  for every reporting period, and a Net cash change grand-total row. Limit variance
  totals to applicable as-of periods and advance the alpha version to `0.2.0a2`.

- **Safe Plan detachment.** Treat a null database as the normal
  book-close/view-detach lifecycle before attempting to restore persisted Plan
  controls. Cover direct detachment in the GTK regression suite and advance the
  alpha version to `0.2.0a3`.

- **Escrow follow-through.** Distinguish cash/income-funded deposits,
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

- **Explainable Plan classifications.** Put shared, plain-language
  provenance in Plan cell details: name the account type behind category activity,
  distinguish explicit planning purposes from narrow account-type/direction
  inference, and explain pending, unresolved, matched, historical, and explicitly
  unexpected resolution states. Link Plan directly to Resolve actuals and expose
  split planning-purpose correction in the GTK transaction editor for web parity.
  Advance the alpha version to `0.2.0a30`.

- **Row and column totals.** Totals across periods and down each period column
  cover planned, actual, and variance values consistently. Category section totals
  count outermost rollups once; planning flows remain separate; Net cash change is
  the grand total rather than a sum of unlike financial dimensions.

- **Mortgage cash flow and liability projection.** Treat one scheduled
  mortgage transaction as one cash requirement with non-additive classified
  components. For the approved representative payment, show `$2,400` once as cash
  required, with `$1,150` interest expense, `$450` escrow funding, and `$800` debt
  principal; reduce Checking by `$2,400`, reduce the mortgage liability by `$800`,
  increase Escrow by `$450`, and reduce immediate net worth by `$1,150`. The parent
  payment is informational and must never be added to its children in section or
  grand totals. Preserve property value independently; count later escrow taor
  insurance disbursements in ledger/net-worth state without counting a second Plan
  expense; and let one actual payment resolve the scheduled whole even when its
  component allocation differs. Cover GTK, web, printable reports, Plan,
  Projection, Dashboard liquidity, comparisons, extra principal, fees, escrow
  shortage/refund, origination, refinancing, and sale with shared report data.
  Advance the alpha version to `0.2.0a35`.

- **Signed spendable-cash Plan bridge.** Lead GTK, web, and printable Plan
  reports with one signed reconciliation from income, ordinary expense, retirement
  distributions/saving, benefit funding, debt principal, escrow funding, and an
  explicit timing/financing residual to the existing spendable-cash result. Collapse
  equal-and-opposite retirement-distribution account legs into one logical flow;
  separate signed Income less expenses from positive budget magnitudes; remove the
  misleading mixed planning-flow total; and expose opening, ending, and exact-dated
  minimum projected spendable cash. Treat future-only actual and variance summaries
  as not applicable. Advance the alpha version to `0.2.0a37`.

- **Decision-ready Plan printing.** Give the cash/liquidity summary
  clear visual priority, start category detail as a distinct appendix, repeat table
  headings without overlapping rows, improve numeric density and section spacing,
  and omit private book paths from printed headers. Keep category detail available
  behind an explicit print-preview option while making the default printout useful
  for locating cash shortfalls quickly. Repair scenario-event table cells that had
  collapsed into comma-separated text and advance the alpha version to `0.2.0a38`.

- Add clearer unresolved/unexpected indicators in Plan.

- Expand reports for retirement saving/distributions, benefit/FSA funding, debt
  principal, and other economically meaningful balance-sheet flows.

- Print/export the applied Plan and displayed Projection comparisons through
  self-contained HTML reports, with browser PDF output and GTK/web parity.

- Improve explanations of account-type/split-purpose classification decisions.

### Scheduled transactions and loans

- **Frequency terminology.** Display `Once` in GTK and web schedule
  and scenario editors without changing the saved recurrence identifier.

- **Bounded complex-schedule inspection.** Put the GTK schedule details
  body in a two-axis scroller while keeping Close/Save controls outside it, so large
  imported and multi-split definitions cannot push actions off screen. Construct
  formula controls before recurrence-loading callbacks can validate them. Resolve
  displayed formula amounts with a representative occurrence's full `period`/`i`
  context, preserving valid GnuCash colon/grouping syntawithout warning or
  rewriting its stored text. Advance the alpha version to `0.2.0a21`.

- **Main-thread import completion.** Make every importer honor
  `notify=False` across its complete call boundary, including its final aggregate
  database-change event. GTK background imports now deliver exactly one coalesced
  notification after returning to the main loop, so GnuCash re-import cannot rebuild
  `GtkColumnView` models from a worker thread and trigger native GTK criticals or a
  segmentation fault. Cover SQLite/XML GnuCash, QIF, OFX, and the GTK callback-thread
  contract; advance the alpha version to `0.2.0a22`.

- **Account-linked card payments.** Credit cards with payment days appear
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

- **Upcoming transaction activation.** Double-clicking upcoming activity in
  the Upcoming view or Dashboard should open its view/edit workflow. Identify the
  selected occurrence and distinguish editing it from editing the recurring
  definition; opening the editor must not post a future transaction automatically.

- **Create schedule from actual.** GTK and web can populate an unsaved one-time
  schedule draft from an existing transaction for review. All split accounts,
  amounts, memos, and planning purposes are copied; the user chooses any recurrence
  or date changes before saving a new independent definition.

- **Finish duplication of protected custom schedules.** Editable fixed/formula
  schedules can be duplicated into reviewed drafts with independent identity and
  occurrence state. Extend this safely to imported/custom structures that remain
  read-only because the current editor cannot round-trip every recurrence or split
  feature. Exact-copy review in GTK/web preserves protected source recurrence,
  formulas, splits, and metadata with a new identity while clearing completed and
  skipped occurrence state.

- **Delete schedules.** Provide a discoverable deletion action with appropriate
  confirmation and atomic undo/redo. Retain already posted transactions and handle
  scenario overrides, pending occurrences, resolutions, and imported-source
  re-import behavior explicitly rather than leaving broken references.

- **Preserve unsupported custom recurrence/formula structures losslessly**
  in inspectable read-only form. Retain original source representations and reasons;
  exclude them from planning, projection, and posting rather than silently mapping
  an unknown recurrence to monthly or replacing an unknown formula with a fixed
  value. Advance the alpha version to `0.2.0a17`.

- **Approachable loan/amortization creation.** Keep the existing GTK
  preview/create workflow and add web parity over the same `LoanTerms`, amortization
  preview, formula schedule, and optional opening-liability service. Exclude hidden
  accounts from all new-loan account choices.

### Investment and retirement modeling

- **Explicit investment activity.** Persist contribution, taxable
  withdrawal, retirement distribution, reinvested dividend/interest, fee, and
  rollover classifications on actual, scheduled, and scenario splits. Expose them
  through GTK/web editing and preserve BreadSched-owned classifications on
  unambiguous GnuCash re-import. Direct Bank-to-Investment schedules no longer need
  an artificial Income/Expense anchor.

- **Reconciled Projection attribution.** Separate contributions,
  performance, investment income, fees, taxable withdrawals, retirement
  distributions, and rollovers in state transitions, month/account explanations,
  summaries, CSV/web output, and printable reports. Retain compatible inference for
  unclassified movements without introducing tax, lot, or cost-basis guesses.

- **Retirement-context validation.** Taxable withdrawals and retirement
  distributions require the appropriate account context. A rollover requires two
  distinct retirement-context accounts whose classified legs balance; it remains
  economically neutral in total holdings. Advance the alpha version to `0.2.0a25`.

- **Add the initial security/commodity price layer.** Separate exact
  quantity, dated direct price, current value, and assumed return without rewriting
  ledger value. GTK/web support manual entry and GnuCash SQLite/XML import.

### Projection and scenarios

- **Base is reality; saved scenarios are alternatives.** Treat Base as the one
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

- **Layer scenarios on scenarios.** Allow a saved scenario to name Base or
  another saved scenario as its assumption parent. Resolve the complete chain while
  retaining the original source of every annual and account-specific value. Preserve
  local override identity during reparenting, reject missing parents and cycles, and
  refuse parent deletion until its children are reparented. Expose the relationship
  through GTK, web, and CLI management. Dated periods and scenario events remain
  deliberately local rather than using an undefined list-merge rule. Advance the
  alpha version to `0.2.0a39`.

- **Add cancellation/progress for expensive projections.** GTK Projection
  uses a cancellable read-only worker and marshals progress/results back to GTK.

### FSA / benefit accounts and claims

- **Separate FSA Dashboard.** Move benefit-year availability and open
  healthcare claims out of the general Dashboard into dedicated GTK/web views while
  retaining the shared FSA calculation and claim engines.


### Import and GnuCash interoperability

- **Preserve and expose inactive schedule state.** Decode GnuCash
  SQLite textual false flags without treating them as truthy. Regressions cover
  inactive XML/SQLite schedules, toggling source state on re-import, and exclusion
  from due and projected activity. GTK and web editors display and save the active
  state explicitly, including for formula-backed schedules in GTK.

- **Account type preservation on re-import.** Refresh the exact
  source-owned GnuCash type while retaining the user-owned BreadSched type and stable
  identity. Report cross-class conflicts for review rather than silently changing
  planning behavior or display signs. Commodity/precision, schedules, and
  existing-transaction follow-through remain part of their dedicated roadmap work.

- **Supported scheduled formulas.** GnuCash formulas representable by the
  current safe formula language retain arithmetic/functions, recurrence ordinals,
  and per-leg dynamics in both import formats.

- **Unsupported scheduled-formula preservation.** Preserve expressions outside
  the current safe language with actionable reasons rather than silently discarding
  them or enabling unrestricted evaluation. Add reviewed translation/variable
  mapping only where semantics are known.

- **Transaction-level notes/memos.** GnuCash notes outside individual
  splits are imported and exposed by the transaction editor/display.
  Preserve and expose them separately from split memos, including on authoritative
  source updates, while respecting ownership of locally authored notes.

- **Remember the import source.** Each destination book remembers its last
  successfully imported GnuCash, QIF, OFX, or other supported source across CLI,
  GTK, and web entry points and preselects it on the next interactive import. A
  missing/moved source still allows reselection; remembered paths do not authorize
  an automatic import or writing to the source.

- **Precise import/re-import counts and skipped-item history.** Report matched
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

- **Synchronize source-deleted GnuCash transactions prospectively.** Record
  every transaction GUID seen by a complete successful SQLite/XML import, including
  skipped records, against the stable source-book identity. After that first
  baseline, remove transactions that disappear from the source and report exact
  removal counts. Retain and warn about source-deleted transactions still referenced
  by BreadSched reconciliation/FSA audit data. Keep inventory updates and deletions
  in the import's atomic undo operation and retain tracking when the same source file
  moves. Describe that retained-reference behavior unobtrusively in import workflows.

- **Strengthen adopted-account matching across GnuCash re-imports.**
  Persist the source GUID when an imported root or sibling is mapped onto a
  pre-existing BreadSched account, then resolve that identity before name/type
  matching on every later import.

- **Import GnuCash dated prices.** Preserve exact SQLite/XML security
  quotes, quote currency, date, type/source, and stable price GUIDs on re-import.

- **Expose ambiguous import-format choices.** GTK4 and web import workflows expose QIF date-order and QIF/OFX number-format overrides while keeping auto-detection as the default.

- **Reject missing GnuCash dates.** Required transaction and scheduled-transaction dates are reported and skipped instead of silently substituting today.

### Storage, integrity, and recovery

- **Current-schema-only alpha storage.** Remove obsolete schema 3→7
  migrations, the migration registry/ledger, pre-migration backup hook, and
  migration-only fixtures. Accept exactly current schema 7 and reject every other
  native schema explicitly in read-only and writable modes. Preserve external import,
  ordinary backup/restore, integrity checking, and crash recovery. Advance the alpha
  version to `0.2.0a29`.

- **Verifiable recovery workflows.** Add GTK backup, restore-as-new, and
  background Verify Book workflows plus a web Verify view; share physical/logical
  verification results with CLI. Lock restore destinations against live writers,
  verify temporary restored copies before atomic installation, remove stale journal
  sidecars, and preserve overwritten books first. Advance the alpha version to
  `0.2.0a19`.

- Strengthen backup/restore and crash-recovery UX with verified restore and
  discoverable GTK/CLI workflows.

- Test interrupted writes and recovery behavior under the chosen journaling/WAL
  policy.

- Keep database concurrency ownership/locking explicit and testable for desktop
  and web access.

- ** Probe Windows writer locks without signals.** Keep POSIX
  `os.kill(pid, 0)` liveness checks, but use a non-signaling Windows process-handle
  query so opening an already-locked book cannot send `CTRL_C_EVENT` to its owner.
  Retain live-owner rejection and stale-lock reclamation coverage across the CI
  platform matrix. Advance the alpha version to `0.2.0a34`.

- **Complete the current executable invariant set.** Verify global split
  identity, commodity/currency roles and exact SCU representability, fixed schedule
  balance, unique realization of each planned occurrence, and unambiguous schedule
  exceptions. Retain existing transaction/reference, reconciliation-snapshot, and
  runtime projection-conservation checks; diagnose without rewriting imported data.
  Advance the alpha version to `0.2.0a33`.

- Expand executable invariants: balanced transactions, no orphaned splits,
  commodity consistency, schedule idempotency, reconciliation preservation, and
  projection conservation.

- **Explicit chart-root semantics.** Only `ROOT` accounts are treated
  as roots by engines; a normal rooted chart reports parentless non-root accounts
  as integrity findings instead of silently dropping them from Projection.

- Add user-visible GTK/web **Verify book** diagnostics before a stable release.

### GTK, web parity, and reporting

- **Missing GTK4 typelib handling.** GUI test collection skips cleanly
  when PyGObject exists but `gi.require_version("Gtk", "4.0")` cannot load the GTK4
  typelib, matching the launcher's environment handling instead of aborting pytest.

- **Bound Projection notes.** Display each note as a separated item in a
  vertically scrolling region with a capped natural height, so a warning-heavy
  projection cannot enlarge the main window beyond the screen or make later views
  inherit that height.

- Print the current GTK Dashboard, Plan, and Projection and every current web
  view, preserving applied values while keeping print mechanics in the presentation
  boundary. Continue richer scenario/flow reporting as the underlying views grow.

- Keep Projection and import operations responsive with clear progress and
  cooperative cancellation; extend the same primitive to later expensive workflows.
