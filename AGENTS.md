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
3. Preserve unrelated user changes and existing commit boundaries. Do not rewrite,
   discard, or conceal work merely to simplify a branch.
4. Run the focused test slice while developing, then all gates described above.
   GitHub Actions on its supported platforms, including the GTK job, is the final
   authority when a required runtime is unavailable locally.
5. For **each PR**, review every major document and record its disposition as
   described in `CONTRIBUTING.md` (Document review): update each document the
   PR makes inaccurate or incomplete, and give a concrete reason for each one that
   needs no change. Add or update tests for changed behavior. Read `AGENTS.md` and
   `CONTRIBUTING.md` and update them when their instructions change.
   Documentation and acceptance tests are part of the implementation and must
   be kept current, not deferred for cleanup later. Keep documentation specific
   and tests meaningful.
6. Submit work through the pull-request workflow above, link the relevant issue,
   and state any unverified platform/runtime explicitly.
7. For AI-assisted commits, place the human `Signed-off-by:` and `Assisted-by:`
   lines adjacent in the terminal trailer block, with no blank line between them.
   Before reporting a PR as validated, verify that `git interpret-trailers --parse`
   returns both trailers, confirm the pushed commit message matches, and report
   every required CI check as successful or explicitly pending/failed.
8. For a web response, keep typed query parsing in `web.resources`, the handler in
   a `web/*_resource.py` adapter (the `web.context.Api` context only carries the open
   book), financial calculations in shared services/engines, and preserve the
   complete response and error contract with route-level tests.
9. For web financial writes, keep request parsing and response translation in a
   presentation adapter, and leave validation and the complete database write in
   the shared service. Test that rejected inputs preserve the stored object.
   Apply the same check to scenario-only estimates and baseline schedules.
10. When changing valuation displays, expose the quote date and provenance or
    explicit missing-quote fallback in both GTK and web. Do not imply that a
    missing foreign-currency conversion has been performed.

More-specific `AGENTS.md` files may add instructions for their own subtrees. When
present, follow both sets; the more-specific file governs only its directory scope.
