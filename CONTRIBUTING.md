# Contributing to BreadSched

BreadSched manages financial records and projections, so changes should favor
correctness, explainability, and preservation of user data over convenience.
These expectations apply to every contribution, including patches generated with
an AI assistant.

## Development principles

- Keep accounting and planning rules in the domain/engine layers. GTK, web, and
  CLI code should present shared services rather than reimplement financial logic.
- Preserve exact monetary arithmetic and double-entry invariants. Do not introduce
  binary floating-point calculations for money.
- Preserve imported or otherwise unsupported data losslessly. If an editor cannot
  safely reproduce a structure, expose it read-only rather than silently
  normalizing or discarding it.
- Maintain GTK/web parity when a feature is intended to exist on both surfaces.
- Treat the repository's [`ROADMAP.md`](ROADMAP.md) as the canonical backlog. A
  pull request that completes, changes, discovers, splits, or reprioritizes roadmap
  work must update the roadmap in the same pull request. Move completed outcomes
  and their acceptance contracts to [`CHANGELOG.md`](CHANGELOG.md).
- Keep documentation roles distinct: `README.md` is the user-facing overview and
  entry point; `DESIGN.md` records current architecture and rationale; `ROADMAP.md`
  is the only future-work list; and `CHANGELOG.md` preserves completed milestones.
  Update the appropriate documents when a pull request changes behavior, design,
  completed history, or pending work rather than letting those descriptions drift.

## Tests are part of the change

New functionality and bug fixes should, to the extent practicable, include tests
that demonstrate the intended behavior and prevent regression. Prefer a focused
regression test that fails for the original defect and passes after the fix.

Tests must be generic. Do not copy names, account identifiers, transaction labels,
amounts, dates, memos, institutions, or other user-specific data into fixtures just
because real user data exposed a defect. Reduce the case to the smallest neutral
example that still exercises the behavior. For example, a bug involving two splits
against the same expense account should be represented with neutral labels such as
`component A` and `component B` and arbitrary balanced values.

When a bug is specific to imported structure, use synthetic or deliberately
sanitized fixture data that reproduces the relevant format semantics without
embedding personal financial information.

Core tests may run concurrently under pytest-xdist. Tests must therefore use
pytest-provided temporary paths or other worker-local resources and must not assume
exclusive ownership of fixed ports, filenames, environment state, or process-global
mutable objects. GTK runtime tests remain serial until the roadmap explicitly moves
them to a parallel-safe stage.

Randomized/property-based tests should use generic generated data and state the
invariant they protect. Realistic performance tests belong under the `performance`
marker and must time only the operation under test, not fixture/book construction.
Performance thresholds should be loose enough for ordinary CI variability while
still detecting the regression class they were introduced to prevent.

## Pull-request workflow

BreadSched development uses focused GitHub pull requests. When work is naturally
sequential, use a stacked series whose bases preserve the intended review order.
Retarget a dependent pull request to `main` after its parent merges.

Before opening or updating a pull request:

1. Start from the latest accepted repository state, not from stale excerpts or a
   synthetic reconstruction when the real tree is available.
2. Make one coherent change at a time and update `ROADMAP.md` and `CHANGELOG.md`
   when required.
   Advance the application alpha version when a commit changes code or runtime
   behavior. Documentation-only commits do not require a version change.
3. Add or update focused tests for new functionality or bug fixes where
   practicable.
4. Run `git diff --check` while developing and `git show --check` on the final
   commit.
5. Compile changed Python modules.
6. Run focused tests, then the broadest practical suite. GTK runtime tests on a
   real GTK environment are authoritative for GUI behavior.
7. Run the repository quality gates, especially Ruff and mypy. `make check` is the
   preferred final local verification when dependencies are available. The
   `make typecheck-extended` gate covers CLI, web, and GUI modules, including
   diagnostics from their imports. GTK runtime tests remain necessary: dynamic
   PyGObject APIs without type stubs cannot be fully checked by mypy.
8. Preserve a single coherent commit where practical and verify its parent and tree
   before publication.
9. Push a named feature branch and open a pull request against `main` or the exact
   preceding branch in a documented stack. Record scope, tests, version, related
   issues, and dependency/merge order in the description.
10. Monitor the complete GitHub Actions run. Correct failures on the same branch and
    refresh every dependent stacked branch so its parent is exact.

Use `Fixes #N` or `Closes #N` only on the pull request that completes the entire
issue. Earlier members of a stack should use `Related to #N` so merging a partial
slice cannot close the issue prematurely.

## Instructions for coding agents

These instructions apply to every automated or interactive coding agent working in
this repository. Read this file completely before changing files.

1. Inspect the current Git state, open pull requests, open GitHub issues, and the
   unfinished roadmap before choosing work. Reconcile newly reported defects with
   existing milestones instead of silently duplicating or displacing them.
2. Confirm a field report against current code and tests. Distinguish a reproduced
   defect from a requested behavior change, and add a focused failing acceptance
   test before changing financial semantics where practicable.
3. Keep accounting, persistence, and projection rules in shared domain/service
   layers. Apply presentation changes consistently to GTK, web, CLI, API, and
   printable output wherever that behavior is exposed.
4. Preserve unrelated user changes and existing commit boundaries. Do not rewrite,
   discard, or conceal work merely to simplify a branch.
5. Run the focused test slice while developing, then all gates described above.
   GitHub Actions on its supported platforms, including the GTK job, is the final
   authority when a required runtime is unavailable locally.
6. Maintain `README.md`, `DESIGN.md`, `ROADMAP.md`, and `CHANGELOG.md` according to
   their roles. Documentation and acceptance tests are part of the implementation,
   not deferred cleanup.
7. Submit work through the pull-request workflow above, link the relevant issue,
   and state any unverified platform/runtime explicitly.

## Static and style hygiene

- Keep Ruff import ordering and formatting clean.
- Run `make fmt` to apply lint fixes and formatting. `make check` and CI enforce
  `ruff format --check` across source, tests, and examples alongside lint checks.
- Keep lines within the configured length limit.
- Avoid mypy type reuse problems such as assigning incompatible meanings to one
  local variable.
- Do not commit generated files, build artifacts, or `__pycache__` directories.
- Do not regress the versions of GitHub Actions already in use without a deliberate
  reason and corresponding roadmap/documentation update.

## Commit and review scope

A patch should be small enough that its accounting, persistence, or UI effects can
be understood during review. If a change uncovers a larger design problem, fix the
safe, well-understood part and record the remaining work in `ROADMAP.md` rather
than expanding the patch unpredictably.

For changes that affect imported GnuCash data, scheduled transactions, scenarios,
projection, reconciliation, or persistence, explicitly consider round-trip and
backward-compatibility behavior before making the data editable.
