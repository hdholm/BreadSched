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

1. **P0 — Windows installer.** Prioritized over further Linux packaging. A
   per-user NSIS installer with its own MSYS2 Python and GTK runtime is built and
   tested in CI (clean install, CLI, desktop smoke, reinstall over itself,
   uninstall keeping books), and each release attaches it with its checksum.
   Every CI run and release also upgrades from the newest published installer.
   The command line can optionally be added to the user's `PATH`, and CI drives
   the native file chooser and printing in the installed copy. Code signing is
   deferred until a beta release is reasonable (see Packaging and release
   quality). The Flatpak manifest, its installed-sandbox CLI
   gate, desktop entry, AppStream metadata, icon, and sandboxed GTK smoke already
   run in CI; remaining Linux work (validating GTK file-chooser portals and
   printing inside the sandbox, and publishing the installer) follows the Windows
   installer. Keep wheel/source releases available throughout.
2. **P2 — Interoperability and analysis.** GnuCash write-back for simple edits
   is delivered (#174: SQLite books, previewed in GTK, web, and CLI, with
   configurable backups). Broader reporting (spending and income over time, net
   worth history and its drill-down) and scenario-aware pinned savings goals are
   delivered, as are transaction tags and linked documents. AqBanking was
   investigated and is supported through the reviewed CSV import rather than a
   built-in link (see `DESIGN.md`). GnuCash deletions of reconciled transactions
   are reviewed, and write-back covers XML books, multi-split transactions,
   amount and account changes, and deletions, checked in real GnuCash in CI.
   Further OFX investment activity (options, share transfers,
   splits, return of capital, margin interest, journals) is not planned while it
   stays documented and reported as skipped on import.
3. **Next — Interface, FSA, and scheduling.** Take the
   GTK/web parity and reporting items, then FSA/benefit accounts and claims, then
   scheduled transactions and loans (sections below). Scenario Projection tabs,
   remembered tabs per book, browser-tab counterparts, native GTK printing, and
   bounded GTK view and dialog sizes are delivered; on FSA, claim reports,
   Dashboard alerts for claims needing attention, and claim corrections
   (repayments, late EOB changes, closing and reopening) are delivered.


## Architecture and correctness

- Split oversized modules/functions as part of the service/resource ownership
  work, especially the remaining seams in `web/server.py`: the scheduled, loan,
  scenario, review, and import handlers still parse JSON inline on `Api`
  (register, entry, reconciliation, and FSA claims now have resource adapters). Continue
  consolidating web control parsers where ownership is clear. Split large GUI test modules
  only when the resulting fixture ownership and runtime isolation improve; do not
  optimize for a line-count threshold alone.


## Register workflow

- Consider whether a categorization rule should also be able to set a payee, and
  whether split transactions can be supported with explicit per-split rules
  rather than a guess.


## Plan and planning-flow reporting

- Ensure planning classifications feed Plan, Projection explanations, scenario
  comparison, and Dashboard consistently.

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

- Add optional online quote retrieval with explicit provenance, staleness, and
  failure behavior; manual and imported quotes must remain usable offline.

- Keep deterministic projection as the normal model; any later Monte Carlo engine
  should remain a separate optional analysis.


## Projection and scenarios

- Compare savings goals held in non-cash accounts with that account's projected
  balance (projection rows carry only summed holdings today), and let a scenario
  model a goal's purchase as a dated one-off after its target date.

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

- Support plan-specific carryover rules where applicable.

- Improve import/reconciliation treatment of external FSA transactions.

- Expand claim/import automation without conflating benefit availability with the
  custodial account ledger balance.


## Import and GnuCash interoperability

- Extend CSV import mapping to split columns (several category/amount pairs per
  row). Reject ambiguous mappings rather than inventing ledger accounts or
  balancing splits, as the category, payee, and currency columns already do.

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

- **Decide on the browser print fallback.** Every desktop printout now prints
  through GTK from the shared report layout. Decide whether **Print in Browser**
  is still needed once native printing has been used on macOS and inside the
  Flatpak sandbox.

## In-application help and documentation

- Add a guided, skippable first-run tour for opening/importing a book, reviewing
  the Dashboard's setup state, due items, category remaining, backup, and Plan.
  Use the same offline guide content, with contextual GTK/web entry points and
  no tutorial transactions written into a real book.

- Evolve the packaged Markdown user guide (an overview and desktop, browser, and
  command-line parts) into a versioned `docs/` site if its proven information
  architecture would benefit from generator-backed navigation and search.
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

- **Before the first beta: sign Windows releases.** Sign `setup.exe` and the
  installed launchers through a hardware-backed signing service (Azure Artifact
  Signing under the maintainer's name, or SignPath Foundation's free open-source
  program), then verify the signatures in CI and compute `SHA256SUMS` over the
  signed files. Alphas stay unsigned until then.

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
