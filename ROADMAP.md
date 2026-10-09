# BreadSched Household Financial Manager roadmap

This file is the **authoritative source for unfinished BreadSched feature work**.
Completed milestones are retained in [`CHANGELOG.md`](CHANGELOG.md).

Published releases are listed on the repository's GitHub Releases page; each
carries GitHub pre-release metadata as well as versioned notes and verified
artifacts. A version is released only when `main` carries its release notes; a
merge without new notes publishes nothing.

## Before the first Beta

Everything in this section is required before the first Beta, in this order. Each
slice uses shared calculations and covers GTK, web, CLI, and printable output
wherever that behavior is exposed, and preserves imported source ownership and
round-trip limits, exact money, and explicit missing-currency valuations.

1. **Tax-year outputs** (calendar year, US-oriented). Realized gains by tax year,
   short- and long-term; totals for categories or tags marked tax-relevant; and
   income totals by source.
2. **Budget jars.** Every savings goal and every scheduled estimate is a jar filled
   from planned income, as Dashboard reserves are today, and drawn down by the
   actual transactions it plans for. Reporting bundles jars by period (month,
   quarter, year) and account rather than by individual transaction or estimate,
   comparing what each period planned with what happened. Time periods only group
   dated events; nothing is converted into fictional monthly cells. A goal remains
   a shadow sub-account holding real money plus the scheduled contributions toward
   it.
   The User Guide gains one coherent explanation of the whole model, from
   scheduled transactions, estimates, and goals (the jars), through the actual
   transactions that settle or draw on them, to how both feed the Plan and
   Projection, with a worked example that follows one paycheck and one estimate
   through every view.
3. **Visualizations.** Today GTK has one Cairo line chart (Projection, Expense
   Explorer, Net worth history) and the browser has none. Build a chart model in
   non-GUI code (series, periods, exact values, labels) produced by the engines,
   drawn by the GTK Cairo widget and by inline SVG in the browser, and included in
   the shared print layout:
   - **Budget jars:** planned against actual by period for each jar and account
     (paired bars), and current jar fill levels with targets.
   - **Cash and Projection:** projected cash with the runway and first shortfall
     marked, scenario comparison overlays, and stacked account balances.
   - **Spending:** category spending over time and its share of the total, from
     the Expense Explorer's data.
   - **Net worth:** history with its composition (assets and liabilities by
     group).
   - **Goals and holdings:** goal progress toward target and date; holdings by
     cost and market value with unrealized gain.

   Every chart has a table of its exact values beside it (for accuracy and
   screen readers), colors that keep contrast in light and dark themes, and
   tooltips with exact amounts; charts never compute their own totals.

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
