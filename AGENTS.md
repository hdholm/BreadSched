# Agent instructions

These instructions apply to every automated or interactive coding agent workingi
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

More-specific `AGENTS.md` files may add instructions for their own subtrees. When
present, follow both sets; the more-specific file governs only its directory scope.

For every pull request, update relevant tests, `ROADMAP.md`, `CHANGELOG.md`,
`DESIGN.md`, `src/breadsched/USER_GUIDE.md`, and `README.md` to reflect all changes
in that PR, as described in `CONTRIBUTING.md`. Review this file and
`CONTRIBUTING.md` for instruction changes, and record each file's disposition in
the PR description. Keep documentation specific and tests meaningful.
