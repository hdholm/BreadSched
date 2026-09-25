# Agent instructions

These instructions apply to every automated or interactive coding agent working
in this repository. Read this file completely before changing files.

Repository-wide contributor instructions are maintained in
[`CONTRIBUTING.md`](CONTRIBUTING.md) and all instructions there also apply to
coding agents. Read that file completely before planning, editing, testing,
committing, or submitting work in this repository.

1. Inspect the current Git state, open pull requests, open GitHub issues, and
   the unfinished roadmap before choosing work. Reconcile newly reported defects
   with existing milestones instead of silently duplicating or displacing them.
2. Confirm a field report against current code and tests. Distinguish a
   reproduced defect from a requested behavior change, and add a focused failing
   acceptance test before changing financial semantics where practicable.
4. Preserve unrelated user changes and existing commit boundaries. Do not rewrite,
   discard, or conceal work merely to simplify a branch.
5. Run the focused test slice while developing, then all gates described above.
   GitHub Actions on its supported platforms, including the GTK job, is the final
   authority when a required runtime is unavailable locally.
6. For **each PR**, update tests, `ROADMAP.md`, `CHANGELOG.md`, `DESIGN.md`,
   `src/breadsched/USER_GUIDE.md`, and `README.md` to reflect all changes in that
   PR, including changes to workflow and documentation. Read `AGENTS.md` and this
   file and update them when their instructions change. Documentation and
   acceptance tests are part of the implementation, not deferred cleanup.
7. Submit work through the pull-request workflow above, link the relevant issue,
   and state any unverified platform/runtime explicitly.
8. For AI-assisted commits, place the human `Signed-off-by:` and `Assisted-by:`
   lines adjacent in the terminal trailer block, with no blank line between them.
   Before reporting a PR as validated, verify that `git interpret-trailers --parse`
   returns both trailers, confirm the pushed commit message matches, and report
   every required CI check as successful or explicitly pending/failed.
9. When extracting a read-only web response from `Api`, keep typed query parsing
   in `web.resources`, financial calculations in shared services/engines, and
   preserve the complete response and error contract with route-level tests.
10. For web financial writes, keep request parsing and response translation in a
    presentation adapter, and leave validation and the complete database write in
    the shared service. Test that rejected inputs preserve the stored object.
    Apply the same check to scenario-only estimates and baseline schedules.
11. When changing valuation displays, expose the quote date and provenance or
    explicit missing-quote fallback in both GTK and web. Do not imply that a
    missing foreign-currency conversion has been performed.

More-specific `AGENTS.md` files may add instructions for their own subtrees. When
present, follow both sets; the more-specific file governs only its directory scope.

For every pull request, update relevant tests, `ROADMAP.md`, `CHANGELOG.md`,
`DESIGN.md`, `src/breadsched/USER_GUIDE.md`, and `README.md` to reflect all changes
in that PR, as described in `CONTRIBUTING.md`. Review this file and
`CONTRIBUTING.md` for instruction changes, and record each file's disposition in
the PR description. Keep documentation specific and tests meaningful.
