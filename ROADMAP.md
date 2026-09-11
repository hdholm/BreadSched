# BreadSched roadmap

This file is the canonical backlog for future BreadSched development.  It is kept
in the repository deliberately: project plans discussed during development should
not exist only in chat history.

**Maintenance rule:** every patch that completes, changes, discovers, splits, or
reprioritizes roadmap work must update this file in the same patch.  Completed
items should either be removed or moved briefly to the completed section so this
remains a useful description of work that is still outstanding.

Status below is current through patch 0126
(`accounts: preserve imported account precision`).

## Near-term correctness and daily-use work

### Scheduled-transaction editing

- Continue expanding schedule editing beyond the fixed shapes BreadSched can
  currently round-trip safely. Unsupported schedules must remain fully inspectable
  in a read-only detail view rather than becoming inaccessible because **Edit** is
  disabled. Imported fixed daily/weekly/monthly/yearly/semi-monthly rules with
  uncommon multipliers can now retain their exact recurrence while other fields are
  edited; keep extending this approach where a rule can be round-tripped losslessly.
- Distinguish genuinely unsupported schedule shapes from schedules that are merely
  imported from GnuCash or represented differently internally. Fixed schedules may
  legitimately contain repeated account legs; patches 0109-0110 make those editable,
  preserve per-split memos, and round-trip the designated funding leg only once.
- Expand the schedule editor so every fixed schedule that can be represented safely
  by BreadSched can be edited without losing recurrence, split, formula, override,
  or import metadata. Fixed balance-sheet transfers with exactly one explicit
  planning-purpose leg and an ordinary funding leg are editable as of 0113.
  Unambiguous two-account asset-to-asset transfers are editable as of 0114.
  Patch 0115 extends the same lossless model to fixed two-account asset/liability
  and liability/liability transfers by preserving the positive ledger leg rather
  than inferring direction from an account's normal balance. Patch 0116 extends
  this to multi-leg fixed balance-sheet transfers. Patch 0117 adds an explicit
  normal/opposite ledger-direction control for additional fixed legs, so imported
  loan/principal-style and other balance-sheet splits that intentionally run against
  an account's normal balance direction can be round-tripped without being forced
  into the balancing funding leg. Patch 0118 allows fixed schedules with multiple
  explicit planning-purpose legs to remain editable: one planning leg is the primary
  amount and the others remain explicit additional planning legs, with an ordinary
  balancing funding split. Continue with other fixed shapes only where direction and
  meaning can be preserved without guessing.
- Formula schedules support protected metadata editing. Base schedules support
  validated direct editing of formula expressions and named scalar variables through
  the existing safe evaluator (0121), and scenario-owned formula schedules gained the
  equivalent validated editing in 0122. Split accounts and amount timelines remain
  protected; expand formula editing further only where imported semantics can be
  preserved and validated safely.
- Preserve unsupported custom recurrence data rather than enabling an editor that
  would silently simplify it.
- Add fixture/regression coverage for native and imported schedules, including
  single-split-looking and multi-split schedules, formula schedules, recurrence
  variants, overrides, and bounded schedules.

### Account editor and Accounts view

- Expand the account editor to deal safely with **all account forms imported from
  GnuCash**.  Every account shape the GnuCash importers can create should be
  inspectable and editable without destructive normalization or loss of imported
  semantics.  Add representative GnuCash fixtures and round-trip/regression tests
  as the supported surface grows.
- The Accounts view exposes **Planning role** as a visible column as of 0123.
  Account, Type, Planning role, Description, and Balance are sortable while the
  tree-list sorter keeps children within their parent rather than flattening the
  chart of accounts. Preserve that hierarchy-aware behavior as more account columns
  are added.
- As of 0124, the account editor preserves ordinary non-placeholder parents,
  imported commodities/securities, and the GnuCash hidden flag. Parent choices
  exclude the edited account and its descendants so imported hierarchies can be
  retained without permitting cycles. Patch 0125 imports and exposes GnuCash account
  notes in both XML and SQLite books and round-trips them through the GTK editor.
  Patch 0126 preserves GnuCash's per-account commodity SCU (account precision) in
  XML and SQLite imports, native persistence, and the GTK editor instead of
  silently replacing it with the commodity-wide default. Continue exposing
  remaining imported account metadata and semantics losslessly rather than
  silently resetting them on save.
- Improve account relationship editing and explanations as additional planning,
  investment, debt, FSA, commodity, and imported-account semantics are exposed.

### Register workflow

- Allow multiple register windows/views to be open at the same time.  Register
  selection, filters, edit state, and navigation must be local to each window/view
  rather than global application state.
- Improve register appearance and information density while keeping account-type
  debit/credit terminology clear.
- Support entering ordinary two-sided/basic transactions directly in the register,
  similar to GnuCash, with the full transaction/split editor available when needed.
- Preserve exact double-entry validation and never permit an inline edit to leave a
  partially written or unbalanced persistent transaction.

### Reconciliation

- Add a first-class account reconciliation workflow modeled on statement
  reconciliation: statement date, ending balance, cleared/reconciled states,
  running difference, completion, cancel/restart, and auditable persistence.
- Put reconciliation rules in the domain/engine layer first; GTK and web should be
  presentations of the same state and operations.
- Cover reopened/corrected statements and preservation of imported reconciliation
  state.

### Dashboard and legacy Budget cleanup

- Continue removing obsolete user-facing Budget-era concepts now that Plan is
  derived from dated financial activity and scenarios.
- Map every remaining dependency on the legacy Budget domain before deleting it;
  migrate CLI, Dashboard, cash-flow, and Projection callers deliberately rather
  than removing pieces opportunistically.
- Keep Dashboard bills driven by enabled schedules/Plan activity rather than a
  hidden current-budget selection.

## Planning and estimation

### Historical estimator

- Keep accepted estimates convergent/idempotent: once a proposal is accepted into
  Base or a scenario it counts as already planned activity, so rerunning analysis
  proposes only the remaining residual need.  (The initial regression is covered
  by 0106; preserve this invariant as estimation becomes richer.)
- Detect irregular-but-recurring activity more reliably.
- Improve confidence scoring and outlier handling.
- Provide richer explanations of the historical basis, cadence, trend, seasonality,
  and residual calculation, with interactive adjustment before acceptance.
- Refine category-specific seasonality and cadence inference.
- Correctly interpret investment, retirement, debt-principal, and FSA-role history
  rather than treating all balance-sheet flows like ordinary Income/Expense data.

### Plan and planning-flow reporting

- Add clearer unresolved/unexpected indicators in Plan.
- Expand reports for retirement saving, retirement distributions, benefit/FSA
  funding, debt principal, and other economically meaningful balance-sheet flows.
- Provide printable/exportable Plan and scenario-comparison reports.
- Improve explanations of why an account role or explicit split purpose produced a
  particular planning classification.
- Ensure all planning classifications feed Plan, Projection explanations, scenario
  comparison, and Dashboard consistently.

## Scheduled transactions and loans

- Scenario-owned formula schedules support validated direct formula/variable editing
  while preserving scenario source linkage and protected amount-timeline semantics
  (0122). Keep this behavior aligned with the base-schedule formula editor.
- Consider richer formula assistance only on top of the existing safe expression
  language; do not introduce a second formula dialect or use Python ``eval``.
- Add an approachable loan/amortization creation workflow.
- Support per-leg amount timelines in fixed multi-split schedules.
- Support additional advanced/custom recurrence patterns where they can be modeled
  deterministically. Preserve imported fixed recurrence multipliers and hidden
  recurrence details (such as end-of-month firing) when they are not explicitly
  changed in the editor.
- Add payroll templates and richer payroll editing.
- Continue recurrence testing across month ends, leap years, bounded schedules,
  business-day behavior, skips, one-time overrides, and future-effective changes.

## Investment and retirement modeling

This remains one of the largest functional gaps.

- Model scheduled investment contributions and distributions explicitly.
- Separate contributions, market/investment performance, dividends/interest,
  fees, and withdrawals in both state transitions and explanations.
- Distinguish taxable investment withdrawals, retirement distributions, and
  rollovers; retirement-to-retirement rollovers must remain economically neutral.
- Add retirement drawdown behavior and scenario support for changing withdrawal
  patterns over time.
- Use per-account and dated account-specific return/rate assumptions throughout
  Projection UI, comparison, and explanation.
- Add richer account-level Projection explanations so changes in value can be
  traced to flows versus performance.
- Add investment lots and cost basis.
- Add a commodity/security price layer that distinguishes quantity, price, value,
  exchange rate, and assumed return.
- Keep deterministic projection as the normal model; any later probabilistic or
  Monte Carlo engine should remain a separate optional analysis rather than being
  mixed into ordinary forecasts.

## Projection and scenarios

- Improve projection caching/reuse without making saved scenarios store stale
  calculated results.
- Add cancellation/progress reporting for expensive projections.
- Add richer charts, cash-runway comparisons, and deeper account explanations.
- Polish scenario comparisons across Plan and Projection.
- Continue evolving assumptions toward extensible dated rules (for example salary
  changes, retirement dates, pensions/Social Security, temporary expenses,
  mortgage payoff, and changing return/inflation regimes) rather than hard-coded
  special cases.
- Keep every projected change explainable as opening state + dated financial flows
  + interest/performance/assumption effects = closing state.

## FSA / benefit accounts and claims

- Improve Review suggestions and action explanations for claims.
- Add stronger Dashboard alerts for claims needing attention.
- Handle over-reimbursement, reopened claims, late EOB changes, and related
  correction workflows.
- Add claim/report views by FSA account, funding year, provider, and status.
- Support plan-specific carryover rules where applicable.
- Improve import/reconciliation treatment of external FSA transactions.
- Expand claim/import automation without conflating benefit availability with the
  custodial account ledger balance.

## Import and GnuCash interoperability

- Add OFX investment transactions.
- Add useful QIF investment/security records.
- Strengthen account matching across imports and re-imports.
- Add commodity/security mapping and price information where source formats allow.
- Import scheduled transactions from additional formats where they are represented
  reliably.
- Expand duplicate/re-import tests, including cross-file duplicate heuristics.
- Improve import summaries and problem reporting.
- Cover richer transfer/category mapping and additional real-world QIF/OFX format
  deviations.
- Preserve stable deterministic source identity when unrelated records are inserted
  into later exports.
- Continue representative GnuCash compatibility fixtures for accounts,
  transactions, reconciliation state, commodities, schedules, formula loans, and
  unusual but valid imported structures.
- Add multi-currency valuation and exchange-rate/price handling; currently imported
  denominations are preserved but cross-currency valuation is not modeled.

## Storage, integrity, and recovery

- Maintain explicit schema migration infrastructure as persistent models evolve,
  with transactional migrations, migration tests, and automatic safety backups.
- Strengthen backup/restore and crash-recovery UX.
- Test database integrity and WAL/recovery behavior around interrupted writes and
  failed migrations.
- Keep database concurrency ownership/locking explicit and testable for desktop and
  web access.
- Expand executable financial invariants: balanced transactions, no orphaned
  splits, exact monetary arithmetic, commodity consistency, schedule idempotency,
  reconciliation preservation, and projection conservation.
- Add a user-visible **verify book**/diagnostic workflow before a stable release.

## GTK, web parity, and reporting

- GTK remains the primary/reference UI; keep web parity an explicit acceptance
  criterion for financial behavior and major workflows.
- Continue real GTK runtime testing for selections, dialogs, focus transitions,
  model replacement, multiple windows, and GTK API-version differences.
- Improve first-run UX, preferences, actionable error handling, icons/resources,
  and native desktop polish without moving financial logic into the GUI.
- Add printable/exportable reports and richer scenario/flow reporting.
- Keep long-running operations responsive with clear progress/cancellation where
  appropriate.

## In-application help and documentation

- Add discoverable in-application help that explains normal BreadSched operation,
  terminology, and the relationship among Accounts, Scheduled transactions, Plan,
  Review/Actuals, Projection, scenarios, planning roles, and imports.
- Include practical walk-throughs for a generic household that build a comprehensive
  chart of accounts and cash-flow plan from scratch, then create and compare multiple
  scenarios with different income, expense, retirement, debt, and investment
  assumptions.
- Explain the application's important design choices in user terms. In particular,
  document why a cash-flow plan is derived from exact-dated actual, scheduled, and
  estimated transactions rather than stored as isolated monthly budget numbers, and
  how posted actuals relate back to the scheduled/planned activity they resolve.
- Document commitments versus estimates, residual historical estimates, planning
  roles, scenario overrides, reconciliation, imports, and Projection explanations
  with short examples and links from the relevant screens.
- Keep help content versioned with the application and covered by navigation/link
  tests so documentation does not silently drift away from the implemented model.

## Packaging and release quality

- Finish cross-platform packaging and reproducible release workflows, with Linux
  first and Windows/macOS behavior isolated behind small platform-specific layers.
- Improve backup/restore, crash recovery, diagnostic logging, and privacy-safe
  error reporting.
- Keep Ruff, mypy, randomized tests, GTK runtime tests, end-to-end demo, and package
  build/install checks as release gates.
- Add property-based tests for recurrence and monetary arithmetic and fuzz-style
  tests for malformed import data where they provide useful additional coverage.
- Keep documentation, versioning, and release notes synchronized with actual
  behavior.

## Recently completed / invariants to preserve

These are not backlog items, but are recorded briefly because later work must not
regress them:

- Plan is derived from actual, scheduled, and estimated dated transactions rather
  than stored monthly budget cells.
- Base and saved scenarios support dated assumptions and scenario-specific estimate
  add/alter/suppress behavior.
- Schedule occurrences support bounds, future-effective amounts, skips, and
  one-time overrides; fixed multi-split schedules are supported.
- Account planning roles are primary with explicit split purpose as an override;
  Retirement, FSA/benefit, Loan/debt, and Investment roles feed planning semantics.
- FSA funding years and claim/service episodes are modeled separately from ordinary
  ledger balance.
- QIF and banking/credit-card OFX/QFX imports are native and deterministic, with
  stable re-import identities.
- Accepted historical estimates count as already planned activity when analysis is
  rerun.
- Dashboard account grouping honors planning roles, and scheduled bills no longer
  depend on a hidden legacy current-budget selection.
- Every scheduled transaction can be opened from the GTK Scheduled view. Schedules
  that the fixed editor cannot safely round-trip are shown in a read-only detail
  view with recurrence, splits/accounts, formulas, variables, and overrides intact.
- Fixed multi-split schedules can use the same Income/Expense account on multiple
  legs and remain editable; per-split memos are preserved through the GTK editor.
- The GTK Scheduled view synchronizes its initially visible selection with action
  sensitivity, so **View / Edit…** is immediately available for the selected row.
- Imported fixed schedules with uncommon recurrence multipliers can remain editable;
  when the recurrence kind is retained, hidden GnuCash recurrence details such as
  end-of-month or semi-month firing days are preserved rather than normalized away.
- Fixed balance-sheet schedules with exactly one explicit planning-purpose leg and
  an ordinary funding leg can be edited without requiring a synthetic Income/Expense
  category; the planning purpose remains attached to the original split.
- Fixed schedules may contain multiple explicit planning-purpose legs; those legs
  now remain independently classified and editable while an ordinary balancing split
  continues to fund the combined transaction.
- Base formula schedules permit validated direct editing of formula expressions and
  named scalar variables; unchanged imported formulas that BreadSched cannot itself
  resolve remain preservable for metadata-only edits.
