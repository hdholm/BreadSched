# BreadSched Household Financial Manager roadmap

This file is the **authoritative source for unfinished BreadSched feature work**.
Completed milestones are retained in [`CHANGELOG.md`](CHANGELOG.md).

Published releases are listed on the repository's GitHub Releases page; each
carries GitHub pre-release metadata as well as versioned notes and verified
artifacts. A version is released only when `main` carries its release notes; a
merge without new notes publishes nothing.

## Before the first Beta

Every item planned for this stage is delivered; see `CHANGELOG.md` through
0.2.0a273. New defects and requests found before the first Beta are added here.

## At the first Beta

- **Signed builds** from the first Beta. Sign `setup.exe` and the installed
  launchers through a hardware-backed signing service (Azure Artifact Signing
  under the maintainer's name, or SignPath Foundation's free open-source program;
  the choice is still being evaluated), verify the signatures in CI, and compute
  `SHA256SUMS` over the signed files. Alphas stay unsigned.
- **Submit the Flatpak to Flathub** (the maintainer's account and review; releases
  already attach a tested bundle). Keep wheel and source releases available.
- **Documentation site.** Evolve the packaged Markdown user guide (an overview and
  desktop, browser, and command-line parts) into a versioned `docs/` site with
  generator-backed navigation and search, keeping the guide usable as a standalone
  document and packaged for offline help; a documentation generator must not
  become a runtime requirement.
- **First-run tour.** A guided, skippable tour for opening or importing a book,
  reviewing the Dashboard's setup state, due items, category remaining, backup, and
  Plan, using the same offline guide content with contextual GTK and web entry
  points and no tutorial transactions written into a real book.

## After the first Beta

The rest of this file is unscheduled and comes after the first Beta.

### Architecture

- Split oversized modules/functions where ownership is clear. Split large GUI test
  modules only when the resulting fixture ownership and runtime isolation improve;
  do not optimize for a line-count threshold alone.

### Register workflow

- Consider whether split transactions can be supported with explicit per-split
  categorization rules rather than a guess.

### Scheduled transactions and loans

- Continue widening safe editing only where complete split/recurrence/import
  semantics can be round-tripped accurately.

- Consider additional custom recurrence patterns only when their occurrence
  identity, bounded generation, import mapping, editing, and round trip are all
  unambiguous.

- Consider richer formula assistance only on top of the existing safe formula
  language; never add Python `eval` or a second formula dialect.

### Investment and retirement modeling

- Use per-account and dated account-specific return/rate assumptions throughout
  projection UI/comparison/explanations.

- Keep deterministic projection as the normal model; any later Monte Carlo engine
  should remain a separate optional analysis.

### Projection and scenarios

- Add deeper account explanations to Projection.

- Evolve assumptions toward extensible dated rules (salary changes, retirement,
  pensions/Social Security, temporary expenses, mortgage payoff, changing
  return or inflation regimes) instead of hard-coded special cases.

- Add a custom per-schedule growth rate only if it can be explained cleanly
  within the scenario-assumption model.

- Improve projection caching/reuse without storing stale calculated scenario
  results.

### FSA / benefit accounts and claims

- Generalize the existing claim linkage where appropriate for non-FSA
  reimbursements while retaining benefit-year rules only for FSA claims. Model
  receivable creation/settlement against balanced ledger splits or an explicit
  planning-only claim, with partial payments, payer identity, evidence, denial,
  correction, and write-off. Re-import must preserve BreadSched-owned claim links
  and reconciliation; test direct vendor credits, bank reimbursements, and
  insurer/employer payments as distinct flows.

- Expand claim/import automation without conflating benefit availability with the
  custodial account ledger balance.

### Import and GnuCash interoperability

- Continue representative GnuCash compatibility fixtures for accounts,
  transactions, reconciliation, commodities, schedules, formula loans, and unusual
  but valid structures.

- Add reviewed commodity/security mapping where imported identifiers cannot be
  matched safely and extend price import to additional source formats where present.

- Import scheduled transactions from additional formats where represented
  reliably.

- Cover richer transfer/category mapping and real-world QIF/OFX deviations.

- **Outbound interoperability and portable archives.** Define documented,
  loss-minimizing exports for supported household ledger/planning data and a
  versioned portable archival format with a human-readable manifest, integrity
  verification, provenance, re-import tests, and explicit disclosure of anything
  that cannot be represented. Preserve opaque imported structures where practical;
  do not claim GnuCash round-trip equivalence beyond demonstrated fixtures.

### Storage, integrity, and recovery

- Write transaction JSON compactly, omitting fields at their default values (as
  `Split.importer_added` already is), to recover most of the 43% size difference
  that [decision 0001](docs/design/decisions/0001-transaction-storage.md) measured
  without its load-time cost. Re-run `scripts/storage_benchmark.py` to confirm.
- Longer term, separate planning resolutions/classifications from imported ledger
  records where doing so materially simplifies synchronization and ownership.

### GTK, web parity, and reporting

- Continue real GTK runtime testing for selections, dialogs, focus transitions,
  model replacement, multiple windows, and GTK API-version differences.

- Improve first-run UX, preferences, actionable errors, icons/resources, and
  native desktop polish without moving financial logic into GUI code.

- **Accessibility baseline.** Audit complete keyboard operation,
  logical focus order and visible focus, screen-reader names/relationships for
  controls and tables, text/UI scaling, and contrast. Add automated coverage where
  reliable and keep a short manual GTK checklist; accessibility remains required
  for release quality but is intentionally below the current financial workflows.

### Packaging and release quality

- Keep macOS behavior isolated behind a small platform layer and evaluate a native
  macOS artifact now that releases carry the Flatpak.

- Improve crash recovery, diagnostic logging, and privacy-safe error reporting.

### Project governance and community health

- Extend the pull-request template and issue forms as contribution patterns
  emerge. Add a code of conduct when the project is ready to invite a broader
  contributor community rather than copying one without an enforcement/contact plan.
