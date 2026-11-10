# BreadSched changelog

What each release changed for the people using it, newest first. Unfinished work
is in [`ROADMAP.md`](ROADMAP.md); how the application is built is in
[`DESIGN.md`](DESIGN.md). The record starts at the first beta: the alpha series
(0.2.0a1 through 0.2.0a321) is kept only in the repository's history and its
GitHub releases.

## Unreleased

Preparing the first beta.

- **Calendar versions.** A version is now the day its release was prepared,
  `YYYY.M.D.PATCH` (for example `2026.11.10.0`), with the stage (`beta` or `stable`)
  beside it. It sorts after every 0.2.0 alpha, so packages upgrade in place.
- **Older alpha books.** This version opens books written by 0.2.0a301 and later
  (native schemas 13 and 14). A book from an earlier alpha is refused with a message
  saying so: open it once with the last alpha, 0.2.0a321, which brings it forward,
  then open it here.
- **Reporting a problem.** Help → Report a Problem fills in the version and system
  for you, and the issue forms ask for less: what happened, in your own words.
