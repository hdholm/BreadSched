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
- Treat the repository's [`ROADMAP.md`](ROADMAP.md) as the canonical backlog of
  unfinished work. When a pull request delivers part or all of a roadmap outcome,
  remove what it delivered (narrowing a bullet to what remains) rather than
  appending status to it, and record the delivered outcome in
  [`CHANGELOG.md`](CHANGELOG.md).
- Record each changelog entry under a `## VERSION - YYYY-MM-DD` heading for the
  application version that ships it, newest first. A pull request that advances
  the version adds that heading; one that does not adds its entry to the next
  version's section, creating the heading with the version it will ship in.
- Keep documentation roles distinct:
  - `README.md` is the product/developer entry point, not a place for feature
    walkthroughs (those belong in the User Guide and DESIGN.md);
  - `src/breadsched/USER_GUIDE.md` is the packaged user guide's interface-neutral
    overview, and `src/breadsched/guide/desktop.md`, `guide/web.md`, and
    `guide/cli.md` give each interface's steps. Put what a feature does and its
    rules in the overview and the steps in the matching part, and link between
    them;
  - `DESIGN.md` records current architecture and rationale;
  - `ROADMAP.md` is the only future-work list; and
  - `CHANGELOG.md` preserves completed milestones.
  Every pull request reviews these documents and records a disposition for each,
  as described in [Document review](#document-review).

## Tests are part of the change

Every pull request that changes behavior must add or update tests for that behavior
and run them. Prefer a focused regression test that fails for the original defect
and passes after the fix. Documentation-only work is validated by the existing
documentation checks (local links, packaged guide parts and their links, release
notes); add a test when the change introduces a contract such checks can hold, such
as a new executable example, packaged file, or cross-document link. Never write a
test that asserts preferred wording, and never edit a test file only to satisfy a
rule.

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

Rendered web views are checked in `tests/test_web_browser.py`, which drives
headless Chromium through Playwright and skips when Playwright or Chromium is not
installed. When you change how `app.js` builds a view, install `playwright` in the
development environment and run that file (set `BREADSCHED_CHROMIUM` to a Chromium
executable if Playwright cannot find one). Keep a static assertion in `test_web.py`
for anything the browser test guards, so CI without a browser still catches it.

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
2. Make one coherent change at a time. Review every major document and record its
   disposition in the pull-request description, as described in
   [Document review](#document-review). Advance the application alpha version when
   a commit changes code or runtime behavior; documentation-only commits do not
   require a version change.
3. Add or update focused tests for changed behavior (see
   [Tests are part of the change](#tests-are-part-of-the-change)).
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

## Document review

Every pull request **reviews** each of these documents against its changes and
records a **disposition** for each one in the pull-request description:

- `README.md`, `AGENTS.md`, `CONTRIBUTING.md`, `DESIGN.md`, `ROADMAP.md`,
  `CHANGELOG.md`, `src/breadsched/USER_GUIDE.md`, and each part in
  `src/breadsched/guide/` (`desktop.md`, `web.md`, `cli.md`).

A disposition is one of:

- **Updated** — what changed and why.
- **Reviewed; no change needed** — with a short, concrete reason, such as "no
  command-line behavior changed" or "makes no claim this change affects".
- **Not applicable** — only when the document cannot be affected, with the reason.

A document must be changed when the pull request makes it inaccurate or
incomplete: new or changed user behavior needs its rules in the User Guide overview
and its steps in each affected interface part; architecture, invariants, and
decisions need `DESIGN.md`; delivered roadmap outcomes leave `ROADMAP.md`; every
code or runtime change needs a `CHANGELOG.md` entry (and a version and release notes
as described below); a changed workflow or instruction needs `CONTRIBUTING.md` or
`AGENTS.md`. Otherwise "reviewed; no change needed" is the right answer: do not add
status sentences, restate implementation history, or make content-free edits just
to touch a document. A no-change disposition is still a review, not a blanket
exemption, and the documents must be accurate when the pull request merges.

Examples:

- **Runtime feature** (for example a new Dashboard card): User Guide overview and
  the desktop and web parts updated; the command-line part "reviewed; no change
  needed — no command-line behavior changed"; `DESIGN.md` updated if it adds or
  changes architecture; `CHANGELOG.md`, version, and release notes updated;
  `ROADMAP.md` updated if it delivers a listed outcome; `README.md` "reviewed; no
  change needed" unless it changes what the README promises.
- **Internal refactor** with no behavior change: `DESIGN.md` updated if module
  ownership or boundaries moved; the User Guide and its parts "reviewed; no change
  needed — no user-visible behavior changed"; `CHANGELOG.md` and the version
  updated because code changed.
- **CI or packaging change**: `CONTRIBUTING.md` or `DESIGN.md` updated where they
  describe the changed workflow; User Guide install steps updated only if what
  users download or run changed.
- **Documentation-only correction**: the corrected document updated; the others
  "reviewed; no change needed" unless they repeat the corrected claim; no version
  change; existing documentation checks run.

Report any verification gap (a platform or runtime not run locally) in the
pull-request description.

## Releases

Releases are opt-in, not automatic for every merge or alpha increment. To select a
tested application version for release, add `docs/releases/vVERSION.md` using the
exact application version and the required compatibility, upgrade/rollback, and
verified-artifact headings enforced by `scripts/check_release_notes.py`. A merge to
`main` whose version has no notes, or whose version is already tagged at an earlier
commit, runs the release workflow but publishes nothing.

After that notes file reaches `main`, the release workflow waits for the complete CI
push run to succeed and verifies that the tested commit is still the tip of `main`.
It then builds and installs the distribution, checks that the installed wheel's
`breadsched --version` line equals the one `breadsched.versioning.version_summary()`
gives for the tested source (so the check follows the native schema; it must never
hard-code a schema window), creates an annotated tag on that exact commit, publishes the human-reviewed
notes, and attaches the wheel, source distribution, the Windows installer (built,
installed over the newest published installer, exercised, and uninstalled on Windows
from the same commit by the workflow's `windows-installer` job), and `SHA256SUMS` covering all three. An existing tag
must resolve to the same commit; an existing release is never overwritten. A later
`main` commit that keeps an already-tagged version, such as a documentation-only or
CI change, is reported as not selected rather than failing the release run; advance
the application version to select a new release.

## Static and style hygiene

- Keep Ruff import ordering and formatting clean.
- Run `make fmt` to apply lint fixes and formatting. `make check` and CI enforce
  `ruff format --check` across source, tests, examples, scripts, and `packaging/`
  alongside lint checks.
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

The full CI matrix takes roughly half an hour on every pull request and again on
`main`, so prefer fewer, larger pull requests: a pull request should carry a
complete feature across its interfaces (service, CLI, GTK, web, and print) as
separate reviewable commits, rather than one pull request per layer. Keep each
commit coherent so the review can still follow it step by step.

For changes that affect imported GnuCash data, scheduled transactions, scenarios,
projection, reconciliation, or persistence, explicitly consider round-trip and
backward-compatibility behavior before making the data editable.
