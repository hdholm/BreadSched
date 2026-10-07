# BreadSched Household Financial Manager roadmap

This file is the **authoritative source for unfinished BreadSched feature work**.
Completed milestones are retained in [`CHANGELOG.md`](CHANGELOG.md).

Published releases are listed on the repository's GitHub Releases page; each
carries GitHub pre-release metadata as well as versioned notes and verified
artifacts. A version is released only when `main` carries its release notes; a
merge without new notes publishes nothing.

## Prioritized delivery

These are the current priorities, subject to review as field evidence changes.
Each slice should use shared calculations and cover GTK, web, CLI, and printable
output wherever that behavior is exposed. Preserve imported source ownership and
round-trip limits, exact money, and explicit missing-currency valuations.

1. **Linux packaging.** Submit the Flatpak to Flathub (the maintainer's account
   and review; releases already attach a tested bundle). Keep wheel/source
   releases available throughout.
2. **Next — Interface, FSA, and scheduling.** Take the GTK/web parity and
   reporting items, then FSA/benefit accounts and claims, then scheduled
   transactions and loans (sections below).


## Architecture

- Split oversized modules/functions where ownership is clear. Split large GUI test
  modules only when the resulting fixture ownership and runtime isolation improve;
  do not optimize for a line-count threshold alone.


## Register workflow

- Consider whether split transactions can be supported with explicit per-split
  categorization rules rather than a guess.


## Plan and planning-flow reporting

- Ensure planning classifications feed Plan, Projection explanations, scenario
  comparison, and Dashboard consistently.

- Show reimbursable expenses' gross cost and net household cost in Projection
  (Plan shows both), including scenario changes to expected reimbursements.


## Scheduled transactions and loans

- Continue widening safe editing only where complete split/recurrence/import
  semantics can be round-tripped accurately.

- Consider additional custom recurrence patterns only when their occurrence
  identity, bounded generation, import mapping, editing, and round trip are all
  unambiguous.

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

- Add richer charts, cash-runway comparisons, and deeper account explanations.

- Evolve assumptions toward extensible dated rules (salary changes, retirement,
  pensions/Social Security, temporary expenses, mortgage payoff, changing
  return or inflation regimes) instead of hard-coded special cases.

- Add a custom per-schedule growth rate only if it can be explained cleanly
  within the scenario-assumption model.

- Improve projection caching/reuse without storing stale calculated scenario
  results.

- Polish Plan/Projection scenario comparisons.


## FSA / benefit accounts and claims

- Generalize the existing claim linkage where appropriate for non-FSA
  reimbursements while retaining benefit-year rules only for FSA claims. Model
  receivable creation/settlement against balanced ledger splits or an explicit
  planning-only claim, with partial payments, payer identity, evidence, denial,
  correction, and write-off. Re-import must preserve BreadSched-owned claim links
  and reconciliation; test direct vendor credits, bank reimbursements, and
  insurer/employer payments as distinct flows.

- Expand claim/import automation without conflating benefit availability with the
  custodial account ledger balance.

## Import and GnuCash interoperability

- Continue representative GnuCash compatibility fixtures for accounts,
  transactions, reconciliation, commodities, schedules, formula loans, and unusual
  but valid structures.

- Add reviewed commodity/security mapping where imported identifiers cannot be
  matched safely and extend price import to additional source formats where present.

- Import scheduled transactions from additional formats where represented
  reliably.

- Cover richer transfer/category mapping and real-world QIF/OFX deviations.

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



## Packaging and release quality

- Keep macOS behavior isolated behind a small platform layer and evaluate a native
  macOS artifact now that releases carry the Flatpak.

- **Before the first beta: sign Windows releases.** Sign `setup.exe` and the
  installed launchers through a hardware-backed signing service (Azure Artifact
  Signing under the maintainer's name, or SignPath Foundation's free open-source
  program), then verify the signatures in CI and compute `SHA256SUMS` over the
  signed files. Alphas stay unsigned until then.

- Improve crash recovery, diagnostic logging, and privacy-safe error reporting.

- Add property-based monetary arithmetic tests and fuzz-style malformed-import
  tests where they add useful coverage.



## Project governance and community health

- Extend the pull-request template and issue forms as contribution patterns
  emerge. Add a code of conduct when the project is ready to invite a broader
  contributor community rather than copying one without an enforcement/contact plan.
