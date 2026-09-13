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

### Security quantities and dated valuation

A split has two exact rational dimensions: ``value`` is expressed in the
transaction currency and balances the double-entry transaction, while ``quantity``
is expressed in the account commodity. These must not be collapsed. Historical
ledger value remains an accounting fact even when a security's market price changes.

A commodity price is a first-class dated object identifying the security, quote
currency, exact positive price, source, and quote type. As-of valuation selects the
latest direct quote on or before the requested date and multiplies it by the exact
account-commodity quantity. It quantizes only the resulting presentation value to
the quote currency fraction. Investment and Retirement accounts with a non-currency
commodity use this market value in current-value presentations; absent a compatible
quote, they explicitly fall back to ledger value. Ordinary accounts are never
silently revalued. Projection uses the as-of market value as its opening state, then
applies its explicit dated flows and return assumptions; a quote is not itself a
future return assumption.

The reporting currency is explicit book metadata when configured, otherwise USD
when present, then the first currency commodity. This initial layer intentionally
requires a direct security-to-reporting-currency quote. Foreign-exchange graphs,
automatic quote retrieval, lot/cost-basis accounting, and projected market prices
are separate concerns and must not be approximated by treating monetary amounts as
prices or quantities.

## Event-driven planning

BreadSched's Plan is derived from actual, scheduled, and estimated dated events.
A month is a reporting window, not a stored planning cell. This is important for
cash flow: an annual insurance premium, weekly groceries, and a twice-monthly pay
schedule retain their real timing instead of being converted into fictional monthly
transactions.

Plan presentation state is per-book metadata because its selected horizon and saved
scenario refer to that book. GTK and web share the same From, Through, reporting
period, measure, scenario, and comparison record. Applying controls persists them;
merely editing controls does not rewrite the Plan or its financial events.

Plan totals preserve hierarchy and dimensions. A displayed category row totals its
own rollup across the selected reporting periods. Section column totals use only
outermost active category rollups, so a parent and descendant cannot both contribute
the same ledger value. Planning flows total separately because contributions,
benefit funding, debt principal, and similar flows explain household commitments but
are not interchangeable with Income/Expense categories. The bottom grand total is
net cash change computed from the event stream, not a sum of unlike category and
balance-sheet values. Planned totals cover the selected horizon; variance totals
include only reporting periods that have begun by the report's as-of date.

Scheduled commitments and estimates use the same underlying event model. Historical
analysis produces an unsaved schedule draft; Base and saved-scenario UIs must route
that draft through their ordinary schedule editor before persistence. The draft
preserves inferred cadence and seasonal month amounts, but the user's reviewed
values are authoritative. Cancelling performs no write. Once saved, the estimate
becomes planned activity itself, so rerunning analysis asks only for residual
unplanned need rather than repeatedly suggesting the same amount.
Historical category actuals supply gross inferred need, including actuals matched
to an earlier schedule. The selected future plan supplies coverage: committed
schedules and planning-only estimates contribute their category splits once, using
the next twelve planning months as a calendar-month profile. Historical scheduled
occurrences are not also subtracted, since a schedule may have ended or changed
amounts. Calendar-month matching retains the exact future year/month rather than
collapsing it to a month number. Sustained full coverage through the remainder of
that rolling year may bound a monthly bridge estimate before the replacement starts;
an isolated future event cannot do so. Annual, biennial, and triennial history keeps
its inferred interval and advances the last observed date to the next due date.
One observed event alone is insufficient evidence for recurrence and is therefore
offered only as a reviewed one-time draft.

When an actual resolves a planned occurrence, BreadSched preserves the original
occurrence identity, planned date, and expected value so later schedule changes do
not rewrite historical variance.

## Scheduled transactions and formulas

Schedules are templates for dated future events. Recurrence, occurrence overrides,
skips, amount changes, and formula-driven splits are part of the schedule semantics.
Imported schedules must be preserved losslessly when BreadSched cannot reproduce
them safely.

Unsupported source structure is data, not permission to guess. BreadSched retains
the original formula text and source recurrence representation, exposes an
actionable read-only reason, and excludes the definition from planning, projection,
and posting. A protected definition can be copied exactly with a new identity and
cleared completed/skipped-occurrence state; this does not claim the copied source
structure has become executable. Editing/translation is enabled only after the
current recurrence and bounded formula engines can validate the result.

Import acceptance for a GnuCash formula is defined by the same bounded AST evaluator
that executes it, not by a second character whitelist. Safe arithmetic, supported
financial functions, and the occurrence variables `period`/`i` therefore behave the
same in SQLite and XML imports. Expressions outside that language never gain broader
execution privileges merely because they came from a trusted local book.

Schedule lifecycle operations distinguish a reusable definition from its ledger
history. A duplicate receives a new stable handle and clears `last_posted` and skip
state, while retaining the template's split accounts, values, memos, planning
purposes, formulas, and dated amount rules for review. A draft made from an actual
likewise copies every financial split but starts as an unsaved one-time definition;
changing its recurrence is an explicit user decision. Deleting a definition is one
undoable database transaction and never deletes actuals already posted from it.
Deletion is refused while a saved scenario override still names the definition,
rather than leaving an ambiguous dangling replacement. A later authoritative
re-import may restore a deleted imported definition with its stable source identity.

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

An FSA account contributes the remaining election availability of every plan
year applicable on the Dashboard's as-of date, including overlapping run-out and
current years. Its custodial ledger balance is not a proxy for available benefits.
Missing or inapplicable funding-year data remains explicitly unavailable rather
than silently falling back to the ledger. A liability is treated as a paid-off loan
only when it is loan-classified or asset-linked, has prior ledger activity, and has
no remaining balance. Such a loan and stale future repayment schedules are omitted,
while its linked asset remains visible.

An asset/loan link does not create an implicit Dashboard group. Once either side is
explicitly assigned through book configuration or the account's group field, the
engine adds unassigned, visible companions from that relationship and treats the
result as a property group. Explicit membership in another group is never stolen.
This preserves the no-default-groups rule while making one deliberate property
selection sufficient for value, debt, equity, and LTV. The group's loan end is the
latest final occurrence of its enabled, finitely bounded repayment schedules;
unbounded schedules do not imply a payoff date.

FSA benefit-year and claim detail belongs to the dedicated FSA Dashboard rather
than the general household Dashboard. This separation is presentational: shared FSA
engines remain authoritative, and an explicitly configured FSA account can still
contribute its benefit availability to a user-defined balance group.

Emergency-fund sizing is an explicit household classification, not an inference
from whether past spending happened to correlate with income. Expense, Loan,
general Liability, Escrow, and carried-balance Credit card accounts may opt out and
default to included. Cash, Bank, Asset, Investment, Retirement, FSA, Income, Equity,
Root, Technical, and paid-in-full cards are structurally excluded. The setting is
BreadSched-owned and survives source re-import.

Only positive economically meaningful legs contribute. Cash/bank funding legs do
not; loan principal and interest are distinct components whose sum is counted once;
escrow funding counts when its account is included, while the later escrow-funded
expense is suppressed; and a paid-in-full card payment affects liquidity without
becoming a second expense. Actual history changes the run rate only after the user
accepts it as a schedule or estimate. Future commitments and accepted estimates
therefore remain the dated source of emergency outgoings.

### Pending cash flow and income-triggered reserves

The general Dashboard presents the next unresolved or future occurrence of each
income and bill schedule as dated pending cash flow. Income stays positive and has
no Hold-now value. Monthly and annual normalization remains useful for comparison
and emergency-fund sizing, but it is never substituted for dated income when
calculating liquidity.

A bill reserve covers exactly one billing cycle. For the occurrence at the end of
that cycle, the engine finds all scheduled income events after the preceding bill
occurrence and through the due date. Each received income event reserves the exact
proportion

``bill amount * event income / total eligible cycle income``.

Future income participates in the denominator; past income participates only after
it has been posted or the schedule's last-posted marker confirms receipt. A missed
past income event therefore cannot reserve cash that did not arrive. If no eligible
income is known for the cycle, existing cash is the only known source and the whole
bill is held conservatively.

For a bill inside the liquidity horizon, the bill amount already subsumes its
current-cycle reserve and is counted only once, less income actually scheduled
within the horizon. Income cannot make required liquidity negative. A bill outside
the horizon protects only its accrued reserve. An overdue bill remains a current
gross obligation; its Hold-now value belongs to the next occurrence and is added
separately, so the displayed obligation cannot disappear merely because a new
cycle began.

Credit-card accounts synthesize a monthly pending payment when they have a positive
balance, a payment day, and no enabled explicit schedule already paying that card.
A paid-monthly card uses its full balance. A revolving card uses its usual payment,
capped at its balance. This is an account-tied presentation row, not an invented
ledger or scheduled-transaction object.

## Account types and imported source types

An account has one user-visible, BreadSched-owned type. Its accounting class, debit
or credit display sign, liquidity, and household-planning behavior are derived from
that type. A separate user-editable planning role or account kind is deliberately
not part of the model: it exposed implementation detail, permitted combinations
with no distinct meaning, and required users to reconcile two classifications.

The visible balance-sheet types are Cash, Bank, Asset, Investment, Retirement,
FSA/benefit, Escrow, Credit card, Loan, and Liability. Income, Expense, and Equity
retain their ledger meanings. Root is structural and Technical preserves imported
bookkeeping accounts that should not participate in ordinary household planning.
Cash and Bank deliberately share a liquid accounting class but remain distinct
workflow types: institutional accounts have statement, reconciliation, import, and
payment-source behavior that physical cash does not. Asset means non-liquid general
value. Credit card remains a revolving payment channel even when it carries a
balance; Loan is amortizing debt; Liability is the generic fallback that assumes
neither workflow.

Investment describes market-valued holdings. Retirement describes the restricted
or tax-advantaged wrapper and controls contribution/distribution planning. In a
hierarchical chart a Retirement parent may contain Investment children, whose
activity inherits retirement context; a flat retirement account simply has the
Retirement type. FSA and Escrow remain first-class types because neither can be
modelled correctly as a generic asset: FSA availability follows elections and
claims, while Escrow recognizes expense when funded and suppresses duplicate
expense recognition when disbursed.

An imported account also records the exact latest GnuCash source type. This
read-only provenance supports re-import, diagnostics, and future round-trip work but
does not drive planning after initial mapping. Native accounts have no source type.
Initial source mapping is conservative: BANK to Bank, CASH to Cash, ASSET/CURRENCY/
RECEIVABLE to Asset, CREDIT to Credit card, LIABILITY/PAYABLE to Liability,
STOCK/MUTUAL to Investment, the flow/equity/root types directly, and TRADING to
Technical. GnuCash alone does not identify an FSA, Escrow, Retirement account, or
Loan; those types require explicit selection or stronger reviewed evidence.

Inference precedence is:

1. explicit split planning-purpose override;
2. account type plus transaction direction/context;
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
GnuCash account GUID, source-owned chart fields (name, source type, parent,
commodity, code, description, notes, placeholder/hidden state, and commodity SCU)
may refresh from the source, while the BreadSched account type, FSA funding years,
projection-rate overrides, projection exclusion, dashboard grouping,
linked-asset/card behavior, usual payment, and payment day are retained. A source
type change is reported. If its accounting class conflicts with the retained
BreadSched type, the conflict requires review rather than silently changing local
semantics or display signs.

On a matching GnuCash transaction GUID, source-owned ledger facts (dates,
descriptions, numbers, accounts, values, quantities, memos/actions, and reconcile
state) and source transaction notes may refresh from the source. Source notes have
their own read-only field because BreadSched-authored transaction notes,
plan-resolution/link state, rejected matches, and split planning/FSA classifications
are retained across re-import. Split-level annotations are retained only when the
same source split GUID still exists, so a materially replaced source split cannot
inherit stale BreadSched state.

Import reporting follows that ownership boundary. A successfully read source
transaction is **new** when its stable identity is absent, **refreshed** when any
source-owned ledger fact differs, and **unchanged** otherwise; split classifications
use the same source-owned comparison and also report removed source splits. Skipped
records retain stable identities and reasons per source path. Their history is
updated inside the import transaction, so a failed/rolled-back import cannot claim
that an issue was introduced or resolved. A later run can consequently distinguish
new, repeated, and now-resolved source problems without parsing warning prose.

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

SQLite is the native persistence engine. Schema 3, written by BreadSched 0.2.0a3,
is the native compatibility baseline. Earlier development schemas have no supported
upgrade path and are rejected explicitly. Schema 3 receives one transactional,
pre-backed-up cleanup to schema 4, which removes the retired monthly Budget domain,
then a schema-5 account normalization. The latter rewrites every account blob from
the former ledger-type/account-kind representation to the single account type before
strict object decoding begins. It is intentionally safe for schema-4 books whose
earlier cleanup committed before strict decoding exposed an unconverted row. Merely
opening a schema-3 book in an earlier release did not rewrite untouched account rows,
so migrations cannot depend on a prior in-memory compatibility decoder having run.
Schema 6 adds first-class dated commodity prices and derived split-index quantity
columns. The transaction blob remains authoritative; migration backfills the new
quantity index from each split's exact stored quantity (or its value for records
that predate separate quantity serialization).
Future persistent-model changes still require explicit forward migrations from the
supported baseline. This native-book policy is independent of external GnuCash,
QIF, OFX, and QFX import compatibility.

The storage priorities are atomic financial writes, deterministic migrations,
backups before dangerous transformations, recoverability, undo/redo integrity, and
realistic performance on long household histories.

GTK long-running work has an explicit ownership boundary. Projection workers open
their own SQLite connection in read-only mode and return immutable calculation
results to the GTK thread through `GLib.idle_add`; they never touch widgets. Import
workers use the application's existing sole writable `DbSQLite` instance because a
second writer would violate the book lock. The modal import workflow prevents other
GTK edits while that worker owns its one batch transaction, and cancellation raises
through importer progress checkpoints so the transaction rolls back in full.

Native books deliberately use SQLite `DELETE` journaling with `synchronous=FULL`,
not WAL. BreadSched has one explicit writer, while short-lived read-only projection
connections may coexist between commits. Keeping rollback journaling preserves the
single-file book model, avoids persistent `-wal`/`-shm` companions that are easy to
separate during manual copying or cloud synchronization, and gives interrupted
writes SQLite's established rollback recovery path. Any future WAL change requires
tested checkpoint, backup, sidecar, and crash-recovery semantics rather than being a
performance toggle.

Verification should protect invariants without imposing whole-book work on every
small edit. Cross-cutting metadata that participates in financial workflows must
obey the same transaction/undo rules as ordinary primary objects. A direct metadata
write is therefore forbidden while a ``DbTxn`` is active unless that write is
explicitly attached to the active transaction; transactional metadata participates
in rollback, undo, and redo.

Per-book presentation conveniences that do not change financial meaning may remain
ordinary metadata. The last successful import source is one such value: GTK, web,
and CLI update the same key only after a successful import. GTK/web may preselect it,
but remembering a path grants neither permission to repeat the import nor permission
to write to the source. Hidden accounts follow a similarly conservative presentation
rule: new-entry choices omit them, while an editor must retain and visibly identify a
hidden account already referenced by an existing split.

Financial workflow records should not be stored as opaque metadata collections when
they have their own identity and lifecycle. FSA claims are first-class primary
objects: one claim save transaction can update linked reimbursement split
classifications and the claim row atomically, and one undo reverses both.


### Performance and randomized correctness gates

Performance regressions should be guarded at the operation boundary rather than by
timing unrelated setup. The performance gate therefore creates one realistic
synthetic 30,000-transaction history outside the measured interval, then times both
one ordinary commit and a 30-year projection. Timing limits are intentionally much looser
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

### Printable reports

Printing is a presentation of an already calculated view, not another financial
engine. A printable Dashboard, Plan, or Projection consumes the same structured
engine result held by the visible GTK view, so printing cannot silently substitute
different dates, grouping, measure, scenario, assumptions, or values. The GTK
application emits a private self-contained HTML preview and delegates printer/PDF
selection to the system browser; this avoids a separate GTK-only pagination model
and remains usable on GTK4 versions without a native printing API. Temporary
previews are owner-readable and removed when the application exits.

The web interface prints its current rendered view directly. Print-specific CSS
removes navigation and editing actions, restores tables hidden by screen scroll
regions, and preserves text, tables, and SVG charts as scalable output. Both paths
keep formatting and pagination in presentation code while all monetary values,
totals, classifications, and comparisons remain engine-owned.

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
