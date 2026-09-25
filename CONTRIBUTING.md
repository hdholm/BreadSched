# Contributing to BreadSched

BreadSched manages financial records and projections, so changes should favor
correctness, explainability, and preservation of user data over convenience.
These expectations apply to every contribution, including patches generated with
an AI assistant.

## Developer Certificate of Origin and assisted work

Every commit contributed through a pull request must carry a `Signed-off-by:`
trailer certifying the [Developer Certificate of Origin, version 1.1](https://developercertificate.org/).
The name and email in that trailer must match the commit author's canonical identity
after applying the repository's `.mailmap`. Create it with `git commit --signoff` (or
`git commit -s`) after setting the author identity that should appear in project
history. A sign-off is a contributor certification, not a substitute for reviewing
the patch.

The human contributor remains the commit author and is responsible for checking
financial behavior, tests, licensing, privacy, and source provenance even when an AI
tool helped. Any commit materially assisted by an AI coding, writing, or review tool
must also identify the tool in an `Assisted-by:` trailer, for example:

```text
Signed-off-by: A. Contributor <contributor@example.com>
Assisted-by: OpenAI Codex <codex@openai.com>
```

Keep these two lines adjacent in the final trailer block: a blank line between
them can cause `git interpret-trailers --parse` to omit one. Verify both parsed
trailers and the pushed commit message before describing a PR as validated.

Use `Assisted-by:`, not `Co-authored-by:`, for an AI tool: the tool does not make the
DCO certification or take authorship responsibility. Name each materially used tool;
do not add an `Assisted-by:` trailer for ordinary editor completion or formatting.
Maintainers may ask how generated work was validated, but contributors must not put
prompts, user financial data, credentials, or other sensitive material in commit
messages or pull requests.

## Development principles

- Keep accounting, persistence, and projection rules in shared domain/service  
  layers. GTK, web, and CLI code should present shared services rather than
  reimplement financial logic. Apply presentation changes consistently to GTK,
  web, CLI, API, and printable output wherever that behavior is exposed.
- Keep web route parsing in `web.resources` and read-only response adapters outside
  the main `Api` class. A boundary extraction should preserve the full response,
  saved controls, scenario comparison, and error contract in route-level tests.
- For web financial writes, presentation adapters may assemble typed service inputs,
  but the shared service must own financial validation and the entire transaction.
  Cover a rejected request with a before/after persistence assertion.
  Scenario schedule changes require that same assertion against the saved scenario,
  including its override list.
- Preserve exact monetary arithmetic and double-entry invariants. Do not
  introduce binary floating-point calculations for money.
- Valuation presentation must disclose the selected quote's date and source, or
  a missing reporting-currency quote and ledger fallback. A quote disclosure is
  not a currency-conversion rule; never silently add unlike currencies.
- Preserve imported or otherwise unsupported data losslessly. If an editor
  cannot safely reproduce a structure, expose it read-only rather than silently
  normalizing or discarding it.
- Maintain GTK/web parity when a feature is intended to exist on both surfaces.
- Treat the repository's [`ROADMAP.md`](ROADMAP.md) as the canonical backlog.
  Every pull request must update the roadmap to reflect its effect on pending
  work and move completed outcomes and acceptance contracts to
  [`CHANGELOG.md`](CHANGELOG.md).
- Keep documentation roles distinct:
  - `README.md` is the product/developer entry point;
  - `src/breadsched/USER_GUIDE.md` is the packaged task-oriented user guide;
  - `DESIGN.md` records current architecture and rationale;
  - `ROADMAP.md` is the only future-work list; and
  - `CHANGELOG.md` preserves completed milestones.
  Update the documents in every pull request to reflect all changes made by
  that pull request, including documentation-only or maintenance work.

## Tests are part of the change

Every pull request must update relevant tests and run them. For documentation-only
changes, strengthen or add a focused test for a documented contract, example, or
documentation integration; keep tests meaningful rather than asserting wording.
Prefer a focused regression test that fails for the original defect and passes after
the fix.

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

The Linux-only bounded mutation gate covers reporting currency selection and
commodity-tagged Amount behavior. Install `mutmut==3.8.0` in the development
environment and run `python scripts/check_mutation_baseline.py` to reproduce the
fresh measured baseline. The script creates its own temporary checkout to avoid
reusing mutation cache. Review survivors and update the documented exemption and
ratchet only after a deliberate measurement, not merely to make a failing gate pass.

## Pull-request workflow

BreadSched development uses focused GitHub pull requests. When work is naturally
sequential, use a stacked series whose bases preserve the intended review order.
Retarget a dependent pull request to `main` after its parent merges.

Use `Fixes #N` or `Closes #N` only on the pull request that completes the entire
issue. Earlier members of a stack should use `Related to #N` so merging a partial
slice cannot close the issue prematurely.

Before opening or updating a pull request:

1. Start from the 'main' branch on GitHub.
2. Make one coherent change at a time. **Every pull request must update tests,
   `ROADMAP.md`, `CHANGELOG.md`, `DESIGN.md`, `src/breadsched/USER_GUIDE.md`, and
   `README.md` to reflect every change the pull request makes.** Review and update
   `AGENTS.md` and `CONTRIBUTING.md` as needed to keep repository instructions
   accurate. Record the disposition of all these files in the pull-request
   description; do not make content-free edits. For internal changes without a new
   user action, update the user-facing documents with a brief, accurate status or
   clarification rather than inventing a feature.
   Advance the application alpha version when a commit changes code or runtime
   behavior. Documentation-only commits do not require a version change.
3. Add or update focused tests that verify the relevant behavior or documented
   contract, including for documentation-only work.
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
9. Verify every commit has the required DCO sign-off and add `Assisted-by:` whenever
   an AI tool materially contributed. Parse both adjacent trailers with
   `git interpret-trailers --parse` and check the remote commit message.
10. Push a named feature branch and open a pull request against `main` or the exact
   preceding branch in a documented stack. Record scope, tests, version, related
   issues, and dependency/merge order in the description.
11. Monitor the complete GitHub Actions run. Correct failures on the same branch and
    refresh every dependent stacked branch so its parent is exact.

## Releases

Releases are opt-in, not automatic for every alpha increment. To select a tested
application version for release, add `docs/releases/vVERSION.md` using the exact
application version and the required compatibility, upgrade/rollback, and verified-
artifact headings enforced by `scripts/check_release_notes.py`.

After that notes file reaches `main`, the release workflow waits for the complete CI
push run to succeed and verifies that the tested commit is still the tip of `main`.
It then builds and installs the distribution, checks the installed application/schema
report, creates an annotated tag on that exact commit, publishes the human-reviewed
notes, and attaches the wheel, source distribution, and `SHA256SUMS`. An existing tag
must resolve to the same commit; an existing release is never overwritten.

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
