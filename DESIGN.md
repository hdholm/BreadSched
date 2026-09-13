# BreadSched design overview and decisions

This document records the architectural principles and important design decisions
that explain how BreadSched works. It describes the current intended design; it is
**not** a list of future work. All pending work belongs in [`ROADMAP.md`](ROADMAP.md).
User operation belongs in the README and, as that documentation grows, dedicated
user/in-application help.

## Product boundary

BreadSched is a household-finance application. Its long-term direction is to cover
the household ledger, planning, scenario, projection, reconciliation, investment,
and related workflows needed to replace GnuCash for a household without attempting
to reproduce GnuCash's business-accounting breadth.

Until that household feature set is sufficient, GnuCash compatibility is a core
architectural constraint rather than a one-time migration feature. Imported data
must retain enough identity and semantics for users to continue maintaining an
existing GnuCash book while using BreadSched-specific planning and projection.

GTK4 is the reference interface and Linux is the primary native desktop target.
The web interface is required to maintain functional parity. Other platforms may
ultimately use GTK packaging or a web-based presentation, but those are delivery
choices over the same application services and financial engines.

## Layering

The intended dependency direction is:

```text
Domain objects / value types
        ↓
Persistence and repositories
        ↓
Financial engines and application/use-case services
        ↓
GTK4 / web / CLI presentations
```

Financial calculations belong below the UI layers. GTK and web should present the
same operations rather than reimplement business workflows independently. As the
application-service layer is strengthened, use cases such as resolving an actual,
saving a claim, reconciling an account, or editing a schedule should have one
implementation called by every presentation.

## Persistence verification

Normal writes are verified incrementally from the records already captured by the
database transaction. Changed objects are checked for their own domain invariants
and derived-index rows, and deletions check reverse references that could make
untouched objects invalid. A changed ledger transaction verifies only its own
``split_index`` rows rather than rebuilding the complete index.

``verify_book()`` remains the exhaustive diagnostic for explicit verification,
backup/restore validation, migration checks, tests, and corruption investigation.
This separation is deliberate: correctness checks on ordinary edits should scale
with the change, not with the lifetime size of the household ledger.

## Exact financial representation

### Double entry

Transactions are collections of splits and must balance atomically. Persistent
storage must never expose a partially written transaction. Imports that repair or
skip damaged source records must report what was changed rather than silently
inventing semantics.

### Money

Ledger values use exact rational arithmetic to preserve imported GnuCash numeric
values and avoid binary floating-point error. Commodity precision, account-specific
SCU, and eventually multi-commodity valuation are distinct concerns from the exact
stored ledger fraction.

Rates are dimensionless and distinct from money amounts. The ``Rate`` domain type
is Decimal-compatible for persistence and presentation, while ``Money`` remains an
exact rational ledger quantity. Multiplying two monetary amounts is invalid; scaling
a monetary amount requires a dimensionless scalar/rate, and dividing one monetary
amount by another yields an exact dimensionless ratio.

## Event-driven planning

BreadSched's Plan is derived from actual, scheduled, and estimated dated events.
A month is a reporting window, not a stored planning cell. This is important for
cash flow: an annual insurance premium, weekly groceries, and a twice-monthly pay
schedule retain their real timing instead of being converted into fictional monthly
transactions.

Scheduled commitments and estimates use the same underlying event model. Estimates
accepted from historical analysis become planned activity themselves; rerunning the
analysis therefore asks only for residual unplanned need rather than repeatedly
suggesting the same amount.
Historical category actuals supply gross inferred need, including actuals matched
to an earlier schedule. The selected future plan supplies coverage: committed
schedules and planning-only estimates contribute their category splits once, using
the next twelve planning months as a calendar-month profile. Historical scheduled
occurrences are not also subtracted, since a schedule may have ended or changed
amounts. Longer recurrence cycles and partial-year transitions need explicit
cadence-aware coverage before this profile can represent them reliably.

When an actual resolves a planned occurrence, BreadSched preserves the original
occurrence identity, planned date, and expected value so later schedule changes do
not rewrite historical variance.

## Scheduled transactions and formulas

Schedules are templates for dated future events. Recurrence, occurrence overrides,
skips, amount changes, and formula-driven splits are part of the schedule semantics.
Imported schedules must be preserved losslessly when BreadSched cannot reproduce
them safely.

A recurrence occurrence has both a nominal date and an adjusted cash date, plus a
stable one-based occurrence number. Weekend/business-day adjustment may move the
cash date across a month boundary, but it must never change that ordinal. Formula
period variables such as ``period`` and GnuCash-compatible ``i`` use the recurrence
ordinal, not a number reconstructed from the adjusted calendar date.

External amount text is parsed at the boundary that knows its format. Core `Money`
construction accepts only unambiguous numeric text; importers and user interfaces
must not silently reinterpret locale punctuation. QIF/OFX import examines the full
source for a consistent decimal convention before parsing records. QIF date order is
likewise inferred once from file-wide evidence (month-first or day-first), never guessed
record by record. Conflicting conventions are reported, and ambiguous files may use an
explicit importer format override. Year-first QIF dates remain inherently unambiguous.

The formula language is parsed through a restricted evaluator, never Python
`eval()`. Formula expressions are treated as untrusted imported/user input and must
have bounded, predictable evaluation behavior. Expression depth, node count, and
power magnitude are bounded; evaluator failures cross the API boundary as
`FormulaError`. GnuCash colon-delimited argument syntax and grouping commas are
normalised without rewriting ordinary comma-delimited function calls.

Formula-driven loans own their payment arithmetic. Projection must not separately
inflate a formula loan payment or add generic liability interest to a liability
whose interest is already represented by schedule formulas.

## Projection

Projection advances state through dated financial events and the intervals between
them. Saved scenarios store assumptions and alternate planned events, not cached
projection results. Reopening a scenario recomputes it against the current book.

Reporting periods aggregate projection state but do not drive it.

Schedule growth is explicit enough to be explainable. The current model supports
`auto`, `none`, `income`, and `inflation`; `auto` is a compatibility/default policy,
not an excuse to hide ambiguous economics. Formula schedules default to fixed
nominal behavior. Mixed gross-to-net payroll grows as one balanced income event.

Economic-sense tests are required in addition to bookkeeping reconciliation. A
projection that balances mathematically can still be financially wrong.

## Dashboard aggregation

Dashboard group names are account-style colon-delimited paths. The engine builds
the hierarchy and aggregate totals; GTK, web, and CLI only render the resulting
local names, depths, headings, and values. A selected chart parent owns its entire
account subtree. Selected descendants and repeated handles are removed before
balance calculation, including across groups, so headings, net worth, and liquidity
cannot count the same ledger value twice.

An FSA-kind account contributes the remaining election availability of every plan
year applicable on the Dashboard's as-of date, including overlapping run-out and
current years. Its custodial ledger balance is not a proxy for available benefits.
Missing or inapplicable funding-year data remains explicitly unavailable rather
than silently falling back to the ledger. A liability is treated as a paid-off loan
only when it is loan-classified or asset-linked, has prior ledger activity, and has
no remaining balance. Such a loan and stale future repayment schedules are omitted,
while its linked asset remains visible.

## Ledger types and account kinds

Traditional Income/Expense account classes do not capture every household planning
flow. Each account therefore has two independent classifications. Its ledger type
(`BANK`, `ASSET`, `CREDIT`, and the other GnuCash-compatible values) controls debit
and credit signs, account class, and source interoperability. Its BreadSched account
kind (`Ordinary`, `Retirement`, `FSA`, `Loan`, `Investment`, or `Escrow`) controls
household planning behavior. Optional explicit split purposes can override the
inferred behavior without changing the underlying double-entry transaction.

Older books' `planning_role` field migrates to the canonical `kind` field when read.
The old write endpoint remains temporarily available for older web clients, but the
domain model, persistence, and current interfaces use account kind. A GnuCash source
type is recorded separately and re-import may refresh the ledger type without
erasing the BreadSched kind. A source change across ledger classes is retained for
review rather than silently making a kind invalid.

Inference precedence is:

1. explicit split planning-purpose override;
2. account kind plus transaction direction/context;
3. ordinary Income/Expense behavior;
4. otherwise neutral.

Transfers that carry no household planning meaning should remain neutral.

## Scenario model

Scenarios change assumptions and planned activity without rewriting the base ledger.
They may add, alter, suppress, or override planned schedules. Assumptions may be
dated and account-specific so future behavior can change without hard-coded
retirement or lifecycle special cases in the engine.

## GnuCash interoperability

GnuCash compatibility is transitional infrastructure toward the standalone
household-finance goal, but it is also a long-lived import/interchange requirement.
Source GUIDs should remain stable identifiers where appropriate. Imported account,
transaction, schedule, formula, commodity, and reconciliation semantics should be
preserved rather than normalized simply because BreadSched exposes a smaller UI.

BreadSched-owned planning state must not be destroyed by re-import. On a matching
GnuCash account GUID, source-owned chart fields (name, type, parent, commodity, code,
description, notes, placeholder/hidden state, and commodity SCU) may refresh from
the source, while BreadSched-owned account kind, FSA funding years, projection-rate
overrides, projection exclusion, dashboard grouping, linked-asset/card behavior,
usual payment, and payment day are retained.

On a matching GnuCash transaction GUID, source-owned ledger facts (dates,
descriptions, numbers, accounts, values, quantities, memos/actions, and reconcile
state) may refresh from the source, while BreadSched-owned transaction notes,
plan-resolution/link state, rejected matches, and split planning/FSA classifications
are retained. Split-level annotations are retained only when the same source split
GUID still exists, so a materially replaced source split cannot inherit stale
BreadSched state.

Longer-term separation of imported ledger state from BreadSched classifications/resolutions is
preferred where it makes synchronization safer.

Escrow is a restricted asset kind even when GnuCash stores it as `BANK`. Projection
therefore tracks its balance as a holding rather than spendable cash. Funding an
escrow asset from cash is recognized as household planning expense at funding time.
A later expense-account payment out of escrow reduces the asset but subtracts the
covered portion from planning expense, including proportional treatment of partial
escrow payments. The ledger transaction remains unchanged and balanced throughout.

## Platform user paths

Per-user settings belong in the platform's normal configuration location rather than
a Linux-specific `~/.config` path: XDG config on Linux/Unix, `%APPDATA%` on Windows,
and `~/Library/Application Support` on macOS. Documents discovery likewise respects
XDG `user-dirs.dirs` and common Windows OneDrive redirection. Because SQLite files
are unsafe as an only copy on many sync/network filesystems, known sync roots are
detected and opening a book there emits a durability warning.

## Account-tree integrity

An account is a chart root because its account type is `ROOT`, not merely because its
parent field is empty. This distinction prevents damaged/imported parentless ordinary
accounts from disappearing from engines that intentionally skip the root. Full book
verification reports parentless non-root accounts when an explicit root exists, while
lightweight rootless books remain valid for tests/tools that intentionally omit a chart
root.

## Storage and transactions

SQLite is the native persistence engine. The design priorities are atomic financial
writes, deterministic migrations, backups before dangerous transformations,
recoverability, undo/redo integrity, and realistic performance on long household
histories.

Verification should protect invariants without imposing whole-book work on every
small edit. Cross-cutting metadata that participates in financial workflows must
obey the same transaction/undo rules as ordinary primary objects. A direct metadata
write is therefore forbidden while a ``DbTxn`` is active unless that write is
explicitly attached to the active transaction; transactional metadata participates
in rollback, undo, and redo.

Financial workflow records should not be stored as opaque metadata collections when
they have their own identity and lifecycle. FSA claims are first-class primary
objects: one claim save transaction can update linked reimbursement split
classifications and the claim row atomically, and one undo reverses both. Schema
migrations move legacy claim metadata into the primary-object table before normal
book use.


### Performance and randomized correctness gates

Performance regressions should be guarded at the operation boundary rather than by
timing unrelated setup. The performance gate therefore creates one realistic
synthetic 30,000-transaction history outside the measured interval, then times both
one ordinary commit and a 30-year projection. Budgets are intentionally much looser
than normal performance while remaining below the historical regressions they are
intended to catch.

Recurrence correctness is also tested with generated inputs. Property-based tests
exercise many dates, intervals, weekend adjustments, and semi-monthly shapes while
checking generator-owned occurrence identity and serialization round trips. Named
regression cases remain valuable for explaining specific historical failures; the
property layer complements rather than replaces them.

Only one writer may own a native book at a time. Writable opens canonicalize the
book path, then acquire a sidecar lock containing host/process identity and a random
ownership token before SQLite is opened. Canonical identity prevents alternate path
spellings or symlink aliases from becoming competing writers. A competing writer
fails with an explicit read-only alternative; read-only opens remain allowed. A
stale lock is reclaimed automatically only when it belongs to the same host and its
recorded process no longer exists. Lock removal verifies the ownership token so one
process cannot delete another writer's lock.

## GTK test availability

GUI tests distinguish an unavailable GTK4 runtime from a code failure. Both missing
PyGObject (`ImportError`) and an installed PyGObject without the GTK4 typelib
(`ValueError` from `gi.require_version`) skip the GTK module cleanly; once GTK4 is
available, runtime/widget failures remain real test failures.

## GTK4 and web parity

GTK4 defines the reference workflow and interaction model. The web UI must maintain
functional parity, but parity means common capabilities and semantics, not two
independent implementations of financial business logic.

Shared engines and application/use-case services should own behavior; interface
layers should own presentation, interaction state, and platform-specific concerns.

## Security posture

Financial books are sensitive local data. Import content, schedule formulas, and
web requests are untrusted inputs. The loopback web interface must defend against
browser-origin attacks and DNS rebinding rather than assuming loopback binding alone
is sufficient.

The local web server therefore uses defense in depth: it binds only to loopback,
rejects non-loopback ``Host`` values, rejects foreign ``Origin`` values, requires
``application/json`` for writes, and requires an unguessable token generated for
each server process on every API request. The launcher supplies that token in the
fragment of the initial local URL (so it is never sent as part of the HTTP request);
the page moves it into ``X-BreadSched-Token`` request headers and removes it from the
visible URL. Static assets do not need the token, but they still require a trusted
Host.

Security hardening belongs in normal acceptance criteria, including transport-level
regression tests for rejected foreign origins/hosts, missing tokens, and invalid
write content types.

## Documentation boundaries

Documentation has three distinct jobs:

- `README.md`: user-facing overview and first operational entry point;
- `DESIGN.md`: current architecture, rationale, and durable design decisions;
- `ROADMAP.md`: the one authoritative list of incomplete/future work.

When a patch changes architecture, update this design document. When it adds,
completes, changes, or reprioritizes pending work, update the roadmap. Do not use
the README or design document as an alternate TODO list.

## Money and exact arithmetic

Money is exact rational arithmetic. Its core constructor accepts a single, unambiguous numeric syntax; locale-aware parsing belongs at UI/import boundaries. GTK and web user entry therefore pass through the shared amount-input boundary: unambiguous decimal conventions are detected from the text, GTK uses the process numeric locale only as an ambiguity tie-breaker, and the web client sends its browser decimal convention explicitly. User-entered amounts remain text until exact server-side parsing; JavaScript floating-point conversion is not part of financial input. Strict English thousands grouping is accepted for backward compatibility, but ambiguous comma-decimal forms must be rejected rather than silently re-scaled. Equality with Python numeric values must obey Python's equality/hash contract; textual representations are not numeric equality. ``Money * Money`` is deliberately rejected. ``Money / Money`` produces an exact ``Fraction`` ratio, while projection assumptions use ``Rate`` so percentages cannot masquerade as ledger amounts.

### Ambiguous import formats are user-resolvable

Importers should infer date/number conventions from whole-file evidence where possible,
not guess independently for each record. When evidence is ambiguous, both reference GTK4
and parity web workflows expose explicit overrides and pass those choices into the same
importer implementation. Web import currently operates on a local path visible to the
BreadSched process; transport convenience must not create a second import semantics layer.

## Import date integrity

Required source dates are never synthesized. Missing or malformed posting/start dates are reported against the source record and skipped rather than silently using the current date. Optional dates remain optional.
