# BreadSched Household Financial Manager design

This document describes BreadSched as it is implemented: its boundaries, who owns
each concern, the invariants the code keeps, and the reasons for its main choices.
It is not a history and not a list of future work: completed changes are in
[`CHANGELOG.md`](CHANGELOG.md), user-facing rules and steps are in the
[User Guide](src/breadsched/USER_GUIDE.md), and unfinished work is in
[`ROADMAP.md`](ROADMAP.md). Detailed acceptance evidence lives in the tests named
here, in `docs/quality/`, and in the versioned notes under `docs/releases/`.

Contents:

1. [Product boundary](#product-boundary)
2. [Architecture and ownership](#architecture-and-ownership)
3. [Domain model and exact money](#domain-model-and-exact-money)
4. [Storage, transactions, and recovery](#storage-transactions-and-recovery)
5. [Dated planning and scenarios](#dated-planning-and-scenarios)
6. [Valuation and reporting](#valuation-and-reporting)
7. [Household workflows](#household-workflows)
8. [Interoperability and source ownership](#interoperability-and-source-ownership)
9. [User interfaces](#user-interfaces)
10. [Security](#security)
11. [Validation, packaging, and releases](#validation-packaging-and-releases)

## Product boundary

BreadSched is a household-finance application. Its long-term direction is to cover
the household ledger, planning, scenario, projection, reconciliation, investment,
and related workflows needed for household finance without attempting
to introduce business-accounting breadth.

GnuCash compatibility is a core architectural constraint rather than a one-time
migration feature. Imported data
must retain enough identity and semantics for users to continue maintaining an
existing GnuCash book while using BreadSched-specific planning and projection.

GTK4 is the reference interface and Linux is the primary native desktop target.
The web interface is required to maintain functional parity. Other platforms may
ultimately use GTK packaging or a web-based presentation, but those are delivery
choices over the same application services and financial engines.

## Architecture and ownership

Dependencies point one way:

```text
Domain objects and value types        gen/lib
        ↓
Persistence                           gen/db
        ↓
Financial engines                     gen/engine
Application services                  gen/services
        ↓
Presentations: GTK4, web, CLI, print  gui/, web/, cli/, plugins/export/
```

| Concern | Owner | Consumers must not |
|---|---|---|
| Exact values, accounts, transactions, schedules, scenarios, claims, goals | `gen/lib` domain types | reinterpret units or signs |
| Native book, schema, migrations, undo, locks, snapshots | `gen/db` (`DbSQLite`) | open a second writer or write outside a `DbTxn` |
| Calculations: ledger, valuation, conversion, completeness, planning, activity, projection, dashboard, FSA, receivables, goals | `gen/engine` | recalculate a total in an adapter |
| Every financial or domain write, with validation and its complete database transaction | `gen/services` (typed requests, `ServiceResult`, stable error codes) | assemble splits, claim objects, or schedules themselves |
| Import and export formats | `plugins/importer`, `plugins/export` | bypass the import service's preflight and ownership rules |
| Wording of service errors and shared report sentences | `presentation.py` and its gettext catalog | invent their own financial wording |
| GTK windows, dialogs, printing, background jobs | `gui/` | touch widgets from a worker thread |
| HTTP routes, query parsing, response shape | `web/resources.py` and the `web/*_resource.py` adapters | perform validation or writes the service owns |
| HTTP authentication, framing, limits, static files | `web/transport.py` | import financial engines |
| Command-line parsing and text/JSON output | `cli/*_commands.py`, dispatched by `cli/main.py` | keep its own copy of a financial rule |
| Report layout shared by GTK printing and the browser | `plugins/export/report_layout.py` | recompute values from the view |

Background work: a Projection or other long calculation runs in a worker with its
own read-only snapshot and hands an immutable result back to the GTK main loop;
imports run on the single writable connection under a modal workflow (see
[Background work](#background-work)).

### Application services

Financial calculations belong below the UI layers. GTK and web present the same
operations rather than reimplementing business workflows independently.
Cross-interface financial mutations—including resolving an actual, saving a claim,
reconciling an account, and editing a schedule—have one service implementation used
by every presentation that exposes them.

The application-service boundary lives in ``breadsched.gen.services``. Public use
cases accept typed request dataclasses and return ``ServiceResult`` values containing
either a typed result or stable ``ServiceError`` codes with field paths. Human-readable
GTK/web wording is adapter-owned and is not part of the service contract. Plan range,
scenario selection, primary/comparison calculation, and baseline/scenario schedule
writes use this boundary. Schedule services clone the submitted candidate, apply the
shared editability and timeline guards, and own the complete ``DbTxn`` so adapters
cannot leave a partially validated write open.

Every literal service error code has one English message in the shared presentation
catalog. Presentations pass that message identifier through gettext at their boundary;
compiled locale catalogs are packaged with the application and never alter the stable
service code or field path. Catalog tests keep newly introduced service codes from
falling back to machine identifiers and reject translations for unknown messages.

Fixed schedule editors submit ``FixedScheduleInput`` rather than assembling ledger
splits themselves. The service resolves account roles and ledger signs, balances the
funding leg, applies planning/investment classifications, preserves safe fields from
the existing definition, and then invokes the mutation boundary. Formula editors
submit only the indexed expressions, variables, recurrence, and ordinary metadata
that formula ownership permits; the service clones all protected structure. Baseline
and scenario editors use the same construction contracts.

GTK account and schedule dialog constructors only orchestrate construction. Focused
helpers snapshot eligible choices, build related control groups, and load initial or
read-only state in callback-safe order; validation and financial meaning remain in
the existing adapter and service boundaries. This makes construction order explicit
without turning widget helpers into a second workflow implementation.

Schedule duplication and deletion also use this service boundary. Exact copies retain
editor-protected custom structure, while deletion rejects stale identities and live
scenario references before removing the baseline definition. Historical-estimate CLI
writes use the same generic schedule save/delete contracts as interactive editors.

Transaction creation and editing likewise submit ``TransactionInput`` and
``TransactionSplitInput`` values. The transaction service reconstructs editable
splits from a stored source, preserves imported and reconciliation metadata, keeps
account-commodity quantity distinct from transaction-currency value, and rejects
unlike-currency values before balance arithmetic. A quantity is separately tagged
and required whenever an account commodity differs from the transaction currency.
The service also enforces the hidden-account retention rule, validates investment
classifications, and owns the complete write transaction. An optional FSA claim
attachment is committed inside that same boundary, so an invalid attachment cannot
leave an otherwise successful ledger posting behind. CLI posting/edit/delete and
GTK quick posting/deletion use the same boundary as the full GTK editor and web.
GTK and web translate stable service errors through shared presentation-owned
wording; service error codes and field paths contain no English API prose.

Account lifecycle and settings mutations likewise cross one typed service boundary.
The service validates identity and parent chains, relationships to commodities,
linked assets and card payment accounts, FSA period overlap, and source-owned fields.
For a new account, the account and optional opening-balance transaction commit in one
database transaction; deletion converts protected or referenced-account failures to
a stable service error.

That service ownership applies to financial and domain mutations. Presentation-only
book settings, such as Dashboard grouping and an applied Plan display range, may use
the ordinary metadata boundary described under Storage and transactions; this does
not make presentation code an owner of ledger or planning semantics.

### Web adapters and transport

The web presentation is split into three boundaries. ``web.resources`` declares
routes and strictly parses one typed value per query field, each resource module
translates plain request values to application/domain calls, and ``web.transport``
owns HTTP authentication, framing, body limits, status mapping, and static delivery.
Every route calls a resource-module function directly with the request context,
``web.context.Api``, which carries only the open book; ``web.server`` re-exports the
entry points. The
transport never returns unexpected exception text: it logs the exception with a
correlation identifier and returns only that identifier with a stable error code.
Each resource module (`web/*_resource.py`: book summary and verification, accounts
and securities, Plan, Plan detail, Expense Explorer, Dashboard, Projection, scenarios,
schedules and due review, loans, Review, registers, reconciliation, FSA, claims,
receivables, goals, payroll, payees, rules, tags and documents, imports, write-back,
net worth, guide) is a thin adapter. Controls several adapters share have one parser:
``web.controls`` turns a browser amount into `Money` and a `ServiceError` into a
resource error with presentation-owned wording, and ``web.schedule_controls`` parses
the recurrence, exception, and split controls of the baseline and scenario schedule
editors. A read translates typed query values into a shared service
or engine call and serializes the typed result; a write parses JSON into the
service's typed request and maps its `ServiceResult` to a response. Neither
calculates financial totals nor validates what the service validates, and route
tests prove that a rejected write leaves the stored object
unchanged. Detached drafts (Projection controls, scenario explanations) are
calculated without being saved; only explicit save routes persist them.

JSON writes require one non-negative ``Content-Length`` no larger than 64 KiB and do
not accept transfer encodings. A write rejected before its body is used (untrusted,
wrong content type, unknown route, or invalid upload query) first reads and drops a
body of valid, in-limit length. Closing with request data unread makes the operating
system reset the connection, and a Windows client then loses the error response.
Browser CSS and JavaScript are packaged static assets,
all events are registered from JavaScript, and charts construct SVG through namespaced
DOM nodes rather than interpolating markup. This permits a directive-specific Content
Security Policy with no inline-script or inline-style exception.
The page's JavaScript is one classic script per area, loaded in the order
``web.transport.SCRIPTS`` lists: ``core.js`` (page state, DOM and number helpers,
the authenticated API calls), ``controls.js`` (editors several views share), one
script each for accounts and registers, entry, schedules and paychecks, imports, the
Plan, scenarios, Review, the Projection, household records, Verify and the guide,
and the Dashboards, then ``app.js`` (navigation and start-up). Classic scripts share
one global scope, so a view calls another area's function directly, but top-level
code runs only in ``app.js``, after every renderer is defined; a test enforces both
the order and that rule, and a browser test opens every view.
The transport serves only the named packaged static assets: the page, the
stylesheet, and those scripts. It resolves each
fixed filename and verifies it remains beneath the static root before checking or
reading it; arbitrary request paths cannot select a filesystem file.

The server owns exactly one writable database connection and serializes every write
through it. Each file-backed GET opens its own read-only snapshot (see
[Journaling and read snapshots](#journaling-and-read-snapshots)) without holding the
global request lock; closing it neither
acquires nor releases the writer's book lock. In-memory books cannot be reopened, so
their GET requests deliberately fall back to the serialized writer connection.

Console diagnostics resolve stderr when emitted, since a captured stream can
close while a web request thread remains active. Unexpected web failures send
their sanitized correlation-ID response even if a logging handler fails.

### Command line

`cli/main.py` only builds the argument parser and dispatches: it maps user-fixable
errors (`CommandError`, `DbError`, a missing or unreadable file) to exit status 2
without a traceback and configures logging. Each area's subcommands live in one
module that defines both their parsers, in a `register(add)` function, and their
handlers: `book_commands` (create, back up, restore, migrate, verify, guide, export,
read GnuCash, start web or GTK), `import_commands` (book, CSV, held changes,
write-back, inference), `ledger_commands` (accounts, registers, balances, rates,
transactions, rules, payees, tags, attachments), `plan_commands` (schedules,
Review, due review, Plan matches, activity, estimates, paychecks),
`benefit_commands` (FSA claims, receivables, goals), and `projection_commands`
(projections, scenarios, comparison, net worth, Dashboard). `cli.common` holds the
helpers they share (date parsing, JSON and table output, book and account lookup)
and imports no command module. Like the web adapters, a command parses arguments
into a typed service request or engine call and formats the result; architecture
tests check that the entry point defines no handlers and that every command's
handler is in the module that registers it.

### GTK and web parity

GTK4 defines the reference workflow and interaction model. The web UI must maintain
functional parity, but parity means common capabilities and semantics, not two
independent implementations of financial business logic.

Shared engines and application services own behavior; interface layers own
presentation, interaction state, and platform-specific concerns.

### Background work

GTK long-running work has an explicit ownership boundary. Projection workers open
their own SQLite connection in read-only mode and return immutable calculation
results to the GTK thread through `GLib.idle_add`; they never touch widgets. Import
workers use the application's existing sole writable `DbSQLite` instance because a
second writer would violate the book lock. The modal import workflow prevents other
GTK edits while that worker owns its one batch transaction, and cancellation raises
through importer progress checkpoints so the transaction rolls back in full. The
importer's `notify=False` contract suppresses its complete notification boundary,
including any aggregate post-import event. After a successful commit, the dialog
delivers one coalesced database/undo-state notification on the GTK main loop. A
worker must never invoke a callback that can rebuild a GTK model.

## Domain model and exact money

### Double entry

Transactions are collections of splits and must balance atomically. Persistent
storage must never expose a partially written transaction. Imports that repair or
skip damaged source records must report what was changed rather than silently
inventing semantics.

### Money, rates, and amounts

Ledger values use exact rational arithmetic to preserve imported GnuCash numeric
values and avoid binary floating-point error. Commodity precision, account-specific
SCU, and multi-commodity valuation are distinct concerns from the exact
stored ledger fraction.

Rates are dimensionless and distinct from money amounts. The ``Rate`` domain type
wraps a finite ``Decimal`` for exact persistence and presentation rather than
subclassing it: direct and reflected arithmetic stays a ``Rate`` instead of silently
decaying to an untyped decimal. ``Money`` remains an exact rational ledger quantity.
Multiplying two monetary amounts is invalid; scaling a monetary amount requires a
dimensionless scalar/rate, and dividing one monetary amount by another yields an
exact dimensionless ratio.

``Amount`` pairs one exact ``Money`` scalar with a commodity identifier. Combining
or comparing two amounts requires identical commodity identity; scaling retains the
tag, and dividing like amounts produces an exact dimensionless ratio. Unlike
commodities become comparable only after an explicit dated conversion has returned
a new amount in the reporting currency.

Text becomes `Money` only at a boundary that knows its format. The core
constructor accepts one unambiguous numeric syntax; GTK and web entry go through the
shared amount-input boundary, which detects unambiguous decimal conventions from the
text, uses the GTK process locale only to break a tie, and takes the browser's
decimal convention explicitly from the web client. Entered amounts stay text until
exact server-side parsing, so JavaScript floating point never touches financial
input. Strict English thousands grouping is accepted; ambiguous comma-decimal forms
are rejected rather than silently re-scaled. Equality with Python numbers obeys
Python's equality and hash contract, and projection assumptions use `Rate`, so a
percentage cannot masquerade as a ledger amount.

Ledger reads preserve that identity internally. Account, recursive, class-total,
net-worth, cash-on-hand, and register-running arithmetic uses tagged transaction
values and refuses to combine material values with different currency identifiers.
The long-standing scalar APIs unwrap the checked result to ``Money`` for callers
that only present one reporting currency. An empty zero has no economic dimension
to net and therefore adopts the first material amount's commodity.

The reporting currency is explicit book metadata when configured, otherwise USD
when present, then the first currency commodity.

New native books contain a stable USD commodity and select it as their default
currency. A legacy native book without any currency remains readable; its first
transaction-service write creates that same default commodity and metadata inside
the transaction's atomic database change.

Rounding uses the selected commodity's declared ``fraction`` rather than assuming
cents. Scheduled posting uses the schedule currency; Projection, Dashboard, loan,
inference, and historical-estimate interfaces use the reporting currency; exact
allocation accepts the caller's commodity fraction. Stored ``Money`` remains
rational and unrounded until one of these minor-unit boundaries is crossed.

### Account types

An account has one user-visible, BreadSched-owned type. Its accounting class, debit
or credit display sign, liquidity, and household-planning behavior are derived from
that type. A separate user-editable planning role or account kind is deliberately
not part of the model: it exposed implementation detail, permitted combinations
with no distinct meaning, and required users to reconcile two classifications.

The visible balance-sheet types are Cash, Bank, Asset, Investment, Retirement,
FSA/benefit, Escrow, Receivable, Credit card, Loan, and Liability. Receivable is
money others owe back (GnuCash `RECEIVABLE` maps to it): an asset for net worth,
never cash-like, so it is excluded from liquidity and the emergency fund. Income, Expense, and Equity
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

An imported account records the exact latest GnuCash source type and source GUID.
The source GUID is separate from the BreadSched object handle because importing into
an initialized book adopts the existing root and compatible top-level placeholders.
That mapping is durable: subsequent imports resolve the retained source GUID before
falling back to a same-parent name/type match, so a source rename or move refreshes
the adopted account rather than creating a duplicate. Full verification reports two
accounts claiming the same source GUID.

Read-only source provenance also retains typed account fields that BreadSched does
not interpret, including SQLite account flags/slots and nested XML slot paths. This
prevents a smaller editor from destructively normalizing information merely because
it has no household workflow yet. GTK and web expose the provenance separately from
editable BreadSched configuration; CLI JSON carries the same fields. Native accounts
have no source identity or source fields.
Initial source mapping is conservative: BANK to Bank, CASH to Cash, ASSET/CURRENCY/
RECEIVABLE to Asset, CREDIT to Credit card, LIABILITY/PAYABLE to Liability,
STOCK/MUTUAL to Investment, the flow/equity/root types directly, and TRADING to
Technical. Historical CHECKING, SAVINGS, and MONEYMRKT types map to Bank,
CREDITLINE maps to Credit card, and CD maps to general Asset. A new unknown source
type is retained exactly but begins as Technical so it cannot acquire invented
planning semantics before review. GnuCash alone does not identify an FSA, Escrow,
Retirement account, or Loan; those types require explicit selection or stronger
reviewed evidence.

Inference precedence is:

1. explicit split planning-purpose override;
2. account type plus transaction direction/context;
3. ordinary Income/Expense behavior;
4. otherwise neutral.

Transfers that carry no household planning meaning should remain neutral.

An account is a chart root because its account type is `ROOT`, not merely because its
parent field is empty. This distinction prevents damaged/imported parentless ordinary
accounts from disappearing from engines that intentionally skip the root. Full book
verification reports parentless non-root accounts when an explicit root exists, while
lightweight rootless books remain valid for tests/tools that intentionally omit a chart
root.

### Security quantities and prices

A split has two exact rational dimensions: ``value`` is expressed in the
transaction currency and balances the double-entry transaction, while ``quantity``
is expressed in the account commodity. These must not be collapsed. Historical
ledger value remains an accounting fact even when a security's market price changes.
When `save_transaction` changes an existing split's value without being given a
quantity, the result depends on the account's commodity:

- In the transaction currency (or with no commodity), the new value is also the
  new quantity. That also repairs a quantity an earlier edit left behind.
- In another commodity, the change is refused with
  `transaction.quantity.conversion` rather than stored with a mismatched
  quantity.

An unchanged value keeps its stored quantity, so imported GnuCash detail
survives an edit that does not touch the amount.

A commodity price is a first-class dated object identifying the security, quote
currency, exact positive price, source, and quote type. As-of valuation selects the
latest direct quote on or before the requested date and converts a commodity-tagged
exact account quantity to a quote-currency-tagged amount. The price rejects units of
another commodity. Valuation quantizes only the resulting presentation value to
the quote currency fraction. Investment and Retirement accounts with a non-currency
commodity use this market value in current-value presentations; absent a compatible
quote, they explicitly fall back to ledger value. Ordinary accounts are never
silently revalued. Projection uses the as-of market value as its opening state, then
applies its explicit dated flows and return assumptions; a quote is not itself a
future return assumption.

### Investment activity

Investment activity is an optional split classification, separate from both the
account type and the planning-flow classification. The account type says what the
holding is; the investment activity says why one exact dated movement changed it;
a planning-flow classification says how a balance-sheet movement appears in Plan.
These dimensions may coexist on one split without changing its double-entry value
or commodity quantity.

The supported activity vocabulary is contribution, taxable withdrawal, retirement
distribution, reinvested dividend, reinvested interest, investment fee, and
retirement rollover. Contributions and reinvested income increase a holding;
withdrawals, distributions, and fees decrease it. Taxable withdrawals are invalid
inside a Retirement context, while retirement distributions require that context.
A rollover requires at least two distinct retirement-context accounts and its
classified investment legs must sum to zero. These rules are shared validation used
by GTK and web transaction, baseline-schedule, and scenario-schedule writes.

Projection records the signed holding movement once for reconciliation, then
attributes that same movement to the appropriate explanatory bucket. Contributions,
withdrawals, retirement distributions, investment income, fees, and gross rollover
amounts are therefore explanatory views of the state transition, not extra money
applied to it. Rollover legs net to zero in total holdings. Market growth remains a
separate accrual effect. This separation prevents contributions from being mistaken
for performance and prevents a rollover from being reported as a withdrawal plus a
new contribution.

For compatibility with existing unclassified books, a positive investment movement
is inferred as a contribution and a negative movement as a taxable withdrawal or
retirement distribution according to account context. That fallback cannot infer
dividends, interest, fees, or rollovers and must not invent tax or cost-basis facts.
Explicit classifications are BreadSched-owned. Stable transaction split identities
retain them across GnuCash re-import; scheduled splits have no stable source identity,
so re-import retains a classification only when the account occurs exactly once in
both the prior and incoming template.

## Storage, transactions, and recovery

### Versions and migrations

The **application version** and **native data-format version** serve different
purposes and never advance in lockstep. The application version reported by
`breadsched --version` identifies the installed build for bug reports, packaging,
and release notes and reports the native compatibility window beside it. The
release workflow compares the installed wheel's line with
`versioning.version_summary()` run on the tested source, so the check can never
carry a stale schema window. Book
verification includes the same application and schema details in its human and JSON
diagnostics. The integer data-format/schema version determines whether a
native book can be opened or must be migrated; it is currently 10. A behavior-only
release changes only the application version. A persistent representation change
increments the data-format version and supplies an explicit migration.

SQLite is the native persistence engine. The current application writes schema 10 and
can migrate schemas 6, 7, 8, and 9 before decoding primary objects. Schema 10 added the
`savings_goal` table (9→10); schema 9 added the `receivable` table (8→9); schema 8 added the `payee` table (7→8); schema 7 added
reconciliation sessions (6→7). An explicit sequential registry and durable ledger,
transactional runner, verified pre-migration backup hook, and versioned fixture make
that compatibility boundary testable. Migration infrastructure is a durable
architectural capability even when an individual obsolete transformation is allowed
to expire.

Read-only opens never migrate. `breadsched migrate` opens a book writable once,
which runs the migration and its pre-migration backup, and reports the old and new
schema and the backup path; the Windows upgrade test uses it on the book made by
the previous release.

The supported migration window currently covers four preceding data-format
versions: the registry retains 6→7, 7→8, 8→9, and 9→10, and the application accepts
schemas 6 through 10. Versioned fixtures for schemas 6, 7, 8, and 9 prove each step.
The window widens only when a real schema migration is needed; there is no no-op
format bump. A migration must run before ordinary decoding, fail atomically, preserve
a verified backup, and leave enough version evidence to diagnose or retry safely.
The mechanism and supported migration steps are not removed merely because the
application version advances. Stable releases may require a still wider promise.
This native-book policy is independent of external GnuCash, QIF, OFX, and QFX import
compatibility.

### Journaling and read snapshots

The storage priorities are atomic financial writes, explicit format rejection,
verified backups and recovery, undo/redo integrity, and realistic performance on
long household histories.

Native books deliberately use SQLite `DELETE` journaling with `synchronous=FULL`,
not WAL. BreadSched has one explicit writer, while read-only snapshots may be taken
between its commits. Keeping rollback journaling preserves the
single-file book model, avoids persistent `-wal`/`-shm` companions that are easy to
separate during manual copying or cloud synchronization, and gives interrupted
writes SQLite's established rollback recovery path. Any future WAL change requires
tested checkpoint, backup, sidecar, and crash-recovery semantics rather than being a
performance toggle.

One calculation must read one committed generation of the
book: accounts, transactions and splits, schedules, scenarios, prices, and metadata.
A read-only open (`DbSQLite.load(path, "r")`, used by every web GET and the GTK
projection worker) therefore copies the whole file into an in-memory database with
SQLite's backup API and closes the file connection. The backup copies all pages
under one shared lock and restarts if the writer commits during it, so the copy is
one generation; the account cache is loaded from the copy, and the copy is
`query_only`. A write committed later is invisible to that reader and visible to the
next open. Two alternatives were rejected. A plain read-only connection sees each
query's own generation, so one request could combine an old
account cache with new rows, or a balance read before and after a write. A read
transaction held for the whole calculation would be one generation too, but under
`DELETE` journaling it keeps a shared lock that stops the writer committing for as
long as the read runs, and the writer's busy timeout would then fail saves during a
long projection; a detached copy holds the lock only while copying. The cost is the
book's size in memory per open reader and a copy on each open, measured at about the
same time as the integrity check every open already runs (0.23 s either way on a
60 MB book of 30,000 transactions). The full integrity check stays on every open,
snapshot copies included, as a deliberate choice for data safety even though on a
large book it costs more than the copy (232 ms against 76 ms for the copy alone on
that book); revisit only if open time becomes a real problem. `load_for_verification`
still reads the file itself, so damage is diagnosed where it is. Tests: `tests/test_db.py`
`TestReadSnapshots` and the web `test_one_get_reads_one_generation_even_when_a_write_lands_midway`.

### Single writer

Only one writer may own a native book at a time. Writable opens canonicalize the
book path, then acquire a sidecar lock containing host/process identity and a random
ownership token before SQLite is opened. Canonical identity prevents alternate path
spellings or symlink aliases from becoming competing writers. A competing writer
fails with an explicit read-only alternative; read-only opens remain allowed. A
stale lock is reclaimed automatically only when it belongs to the same host and its
recorded process no longer exists. Lock removal verifies the ownership token so one
process cannot delete another writer's lock.

### Verification, backup, and restore

Normal writes are verified incrementally from the records already captured by the
database transaction. Changed objects are checked for their own domain invariants
and derived-index rows, and deletions check reverse references that could make
untouched objects invalid. A changed ledger transaction verifies only its own
``split_index`` rows rather than rebuilding the complete index. These checks live
in `gen/db/change_verification.ChangeVerification`, a `DbBase` subclass that
`DbSQLite` extends; they read through the public object API and the open connection
(`_require`, declared abstract there), so the storage module holds storage and
transactions only.

``verify_book()`` remains the exhaustive diagnostic for explicit verification,
backup/restore validation, tests, and corruption investigation.
This separation is deliberate: correctness checks on ordinary edits should scale
with the change, not with the lifetime size of the household ledger.

The exhaustive domain pass materializes accounts, commodities, scenarios,
transactions, and split ownership once, then dispatches that immutable snapshot to
responsibility-specific checkers. This keeps cross-object checks consistent while
letting each diagnostic family evolve without turning the public verification entry
point into a second persistence implementation.

Exhaustive verification checks exact transaction balance and references, global
split identity, commodity and account-SCU precision, currency roles, scheduled
fixed-split balance, unique occurrence realization, reconciliation snapshots, and
derived indexes. Projection independently refuses to return a reporting month whose
opening stocks, dated movements, accruals, and closing stocks do not reconcile.
These checks diagnose facts; they do not round or repair imported ledger data.

Backup and restore use SQLite's backup API rather than filesystem copying. Restore
verifies the source logically and physically, holds the same canonical destination
writer lock used by a live book, creates a pre-restore backup before an authorized
replacement, writes and re-verifies a temporary database, removes stale SQLite
sidecars, and only then atomically installs it. A restore therefore cannot replace
the pathname beneath another live writer. Verification opens malformed books in a
special tolerant read-only mode so damage is reported rather than decoded into
ordinary engine state.

`gen/db/book_lock.BookWriterLock` owns the single-writer `<book>.lock` file,
`gen/db/backups` the backup, restore (given the verifier and the pre-restore copy
as callables), migration-backup path, and read-snapshot copies, and
`gen/db/storage_verification` the derived-column and `split_index` checks that
`verify_book()` adds to the domain pass. None of them imports the backend, and
`DbSQLite` keeps the same public methods, delegating to them.

Verification should protect invariants without imposing whole-book work on every
small edit. Cross-cutting metadata that participates in financial workflows must
obey the same transaction/undo rules as ordinary primary objects. A direct metadata
write is therefore forbidden while a ``DbTxn`` is active unless that write is
explicitly attached to the active transaction; transactional metadata participates
in rollback, undo, and redo.

### Presentation settings and financial records

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
classifications and the claim row atomically, and one undo reverses both. GTK and web
submit typed claim, allocation, link, and rejection inputs rather than constructing
or persisting claim domain objects. Expected validation failures cross that service
boundary only as stable codes and field paths.

### Platform user paths

Per-user settings belong in the platform's normal configuration location rather than
a Linux-specific `~/.config` path: XDG config on Linux/Unix, `%APPDATA%` on Windows,
and `~/Library/Application Support` on macOS. Documents discovery likewise respects
XDG `user-dirs.dirs` and common Windows OneDrive redirection. Because SQLite files
are unsafe as an only copy on many sync/network filesystems, known sync roots are
detected and opening a book there emits a durability warning.

## Dated planning and scenarios

### Event-driven Plan

BreadSched's Plan is derived from actual, scheduled, and estimated dated events.
Scheduled occurrences retain the schedule's transaction currency (or the book's
reporting currency for legacy untagged schedules). Candidate matching requires
the actual transaction currency to agree before comparing gross numeric amounts.
For an already linked actual with a different currency, occurrence variance is
unavailable; this does not convert Plan period totals or scenario estimates.
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
the same ledger value. Income and expense detail uses positive budget magnitudes;
the separate signed Income less expenses row exposes the operating result.

Category-report construction keeps event accumulation separate from presentation-row
assembly. Category hierarchy roll-up, cash-bridge rows, planning-flow rows, mortgage
rows, and as-of variance calculation are independent transformations over the same
exact period totals; the final report still executes the cash-conservation check.

The primary reconciliation is a signed, non-overlapping spendable-cash bridge:
income received minus ordinary expense, plus retirement distributions, minus
retirement saving, benefit funding, debt principal, and escrow funding, plus an
explicit residual for timing/financing differences. The bridge must equal cash
movement derived directly from every event's spendable-cash splits for planned and
actual measures. The residual is intentionally visible rather than silently forcing
credit-card purchases or similar expense/cash timing differences into another row.
Opening and ending balances anchor the bridge to ledger cash, and exact-dated event
application identifies the minimum projected spendable-cash balance and its date.

Planning-purpose rows are balance-sheet classifications, not one additive financial
dimension, so they have no mixed grand total. A transaction may carry the same
retirement-distribution purpose on both the investment source and cash destination;
the cash leg is only the counterpart and must not appear as a second logical flow.
The non-cash source remains the inspectable planning-purpose row, while the bridge
shows the distribution once as a positive cash contribution. Planned totals cover
the selected horizon.

`engine/activity` dates planned occurrences and actual ledger activity into display
periods (`build_activity_report`); `engine/category_report` turns that report into
the Plan's rows (`build_category_report`: income and expense categories, the cash
bridge, planning flows, mortgage payments), the projected spendable-cash position,
and the currency notes and completeness. `activity` never imports it, and the
classification helpers both need (`split_totals`, `economic_planning_flow_amounts`,
`redundant_cash_flow_split`, `inferred_planning_flow`, `escrow_planning_flows`,
`mortgage_payment`) are public in `activity`, so no private name crosses the
boundary; an architecture test checks both.

Plan detail lives in `engine/plan_detail`, beside the grid it explains: `explain_category_period`, `explain_planning_flow_period`, and
`explain_mortgage_payment_period` rebuild one cell from `build_activity_report` and
the same public classification helpers the grid uses (`planning_flow_decision`,
`escrow_planning_flows`, `mortgage_payment`), so a drill-down cannot disagree with
its cell. `activity` never imports the explanations, and an architecture test keeps
private helpers from crossing that boundary.

Classification decisions are report data, not presentation guesses. Plan detail
names the account type and accounting class that caused an Income/Expense split to
appear, and distinguishes an explicit split planning purpose from the narrow
account-type/direction inference used for Retirement, FSA, and Loan movements.
Resolution explanations similarly distinguish pending expected occurrences,
unresolved actuals, explicit unexpected decisions, historical actuals, and matched
occurrences. GTK and web render those shared reasons. Corrections write the explicit
split purpose through the ordinary transaction or schedule editor; they do not add
a separate classification record or mutate the account's ledger type.

Review decisions cross one typed service boundary for CLI, GTK, and web. Matching,
candidate rejection, occurrence skipping, unexpected classification, and attaching
an actual to an FSA claim validate stable identities before owning their respective
atomic transaction, schedule, or claim write. Expected stale-state failures use
stable codes and field paths; candidate ranking remains read-only engine logic.

`engine.review_explain` explains the ranking for every interface. Each candidate gets
reasons (shared accounts, amount and date offsets, description words in common) and a
confidence: *close* within two days and 5% of the expected amount, otherwise
*possible*. With no candidate, `no_candidate_reason` searches 60 days either side for
an open occurrence sharing an account and reports the first applicable cause: a
nearby one in another currency, every nearby one rejected, the nearest outside the
seven-day window, or no schedule using the accounts. `fsa_hint` classifies an FSA
movement with `fsa_flows` and says whether and how to attach it to a claim. The
wording for each action is `presentation.REVIEW_ACTION_HELP`; GTK shows it as
tooltips and a help line, the web as titles and a list, and `breadsched review`
under its read-only listing.

When an actual resolves a planned occurrence, BreadSched preserves the original
occurrence identity, planned date, and expected value so later schedule changes do
not rewrite historical variance.

### Schedules and formulas

Schedules are templates for dated future events. Recurrence, occurrence overrides,
skips, whole-schedule and per-leg amount changes, and formula-driven splits are part
of the schedule semantics. Per-leg timelines store exact signed ledger amounts and
must remain balanced at every effective boundary; their source follows each planned
split into Plan and Projection explanations.
Imported schedules must be preserved losslessly when BreadSched cannot reproduce
them safely.

The representative schedule-fidelity matrix defines the current ownership and
execution boundary across native books and generated GnuCash SQLite/XML books:

| Schedule fact | Native definition | Supported GnuCash definition | Unsupported GnuCash definition |
|---|---|---|---|
| Recurrence, bounds, weekend rule | Persist and execute | Refresh from source and execute | Preserve original source structure; read-only and never execute |
| Enabled/automatic/advance flags | Persist and execute | Refresh from source | Preserve with the protected definition |
| Split accounts, memos, amounts/formulas | Persist exactly | Refresh from source; execute only through the bounded formula engine | Preserve inspectably; never execute |
| Growth policy and dated amount rules | Persist exactly | BreadSched-owned and retained on source refresh | Retained, but cannot make a protected definition executable |
| Skips, one-time adjustments, local formula inputs | Persist exactly | BreadSched-owned and retained on source refresh | Retained as local context only |
| Split planning/investment classifications | Persist exactly | Retain only when the account identifies one split unambiguously before and after refresh | Never guess across ambiguous/restructured splits |

GnuCash may express one schedule as a union of multiple recurrence rows. BreadSched
has no equivalent recurrence union, so both importers retain all rows,
report one actionable reason, and prevent planning or posting. Selecting the first
row would be a lossy semantic change, even when that row is independently supported.

Unsupported source structure is data, not permission to guess. BreadSched retains
the original formula text and source recurrence representation, exposes an
actionable read-only reason, and excludes the definition from planning, projection,
and posting. A protected definition can be copied exactly with a new identity and
cleared completed/skipped-occurrence state; this does not claim the copied source
structure has become executable. Editing/translation is enabled only after the
current recurrence and bounded formula engines can validate the result.

Editable schedule presentation is an engine-owned projection, not a UI heuristic.
The projection names the primary, funding, and additional fixed splits together
with their ledger directions, or the exact formula split indices whose expressions
may change. GTK and web consume that projection. Formula saves clone the complete
definition and replace only validated expressions, named variables, recurrence,
and ordinary metadata; split accounts and formula-owned amount timelines are never
accepted from the request payload.

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

Any UI that resolves a formula schedule for display must choose a real occurrence
date and use the schedule's complete occurrence context. Resolving a split against
only its persisted named variables omits derived `period`/`i`, incorrectly reports
valid imported loan formulas as unusable, and can display zero. Presentation may
normalize the formula for safe evaluation but must preserve its source text.

The formula language is parsed through a restricted evaluator, never Python
`eval()`. Formula expressions are treated as untrusted imported/user input and must
have bounded, predictable evaluation behavior. Expression depth, node count, and
power magnitude are bounded, raw and normalized text have fixed limits, and all
decimal construction and arithmetic run in a local 64-digit context with bounded
exponents independent of process settings. Non-finite values and syntax, decimal,
arithmetic, recursion, or resource failures cross the API boundary as
`FormulaError`. GnuCash colon-delimited argument syntax and grouping commas are
normalized without rewriting ordinary comma-delimited function calls.

Formula-driven loans own their payment arithmetic. Projection must not separately
inflate a formula loan payment or add generic liability interest to a liability
whose interest is already represented by schedule formulas.

### Due and missed occurrences

Due and missed occurrences are decided through `gen.services.due_review`. It lists
`schedule.due_occurrences` through the review date, grouped by schedule, which
already excludes plan-only estimates, disabled schedules, and unusable imported
definitions. A decision names only a schedule handle, a date, and post, skip, or
defer. `resolve_due` recomputes the currently due set immediately before writing
and refuses a whole batch that names a date no longer due, a missing schedule, or
the same date twice, so a stale GTK window, browser page, or script cannot post an
occurrence that another surface already posted or skipped. Accepted posts and skips
commit in one database transaction and one undo step; defer writes nothing. GTK's
due dialog, the web review panel, and the `due-review` command are adapters over this
service; the older unreviewed post-all helpers remain for callers that explicitly
post automatic schedules.

### Payroll

`engine.payroll` reads a paycheck from an ordinary schedule; there is no payroll
schedule type. A schedule is a paycheck as of a date when, with its amounts resolved
for that date, it credits at least one income account (gross), debits at least one
Bank or Cash account (net deposit), and every other leg is a debit into an expense
(**tax** when the full account name contains "tax", otherwise **deduction**), a
non-cash asset (**saved**), or a liability (**repayment**). Anything else, including
an unresolvable formula, is not a paycheck, and the breakdown is `None` rather than a
guess. The tax rule is a naming heuristic; it only chooses a group label and the
default lines a pay change scales. `services.payroll.paychecks` lists enabled,
usable schedules whose recurrence has not ended before the date.

A `PayrollTemplate` (income account, deposit account, usual gross, lines each a fixed
amount or a percentage of gross) is BreadSched-owned book metadata under
`payroll_templates`, saved and deleted by `services.payroll` in one undoable metadata
transaction; names are unique ignoring case. `compute_paycheck` rounds each
percentage line to the cent and refuses a non-positive line or net. A paycheck
created from a template goes through `save_fixed_schedule` as an ordinary fixed
schedule; an investment-account line is classified as a contribution and a liability
line pays it down.

A pay change (`plan_pay_change`) is computed from the breakdown on its start date:
the new gross (several income legs are scaled in proportion), each other line set,
scaled by new/old gross, or kept, the first deposit absorbing the difference. The
service saves it as `ScheduledSplitAmountChange` entries on each changed leg at the
start date (gross, net, and every changed line), through `save_schedule`, so the
schedule's balance validation and undo apply and earlier occurrences keep their
amounts. It refuses a start before the recurrence, formula legs, whole-schedule
amount changes, seasonal amounts, one-time amounts on or after the start (all of
which would rescale the legs), an account on two legs, and any per-leg change after
the start, rather than reinterpreting them.

### Loans and mortgages

Loan creation is an application-service workflow around `LoanTerms`: GTK and web
collect the same lender-facing terms, preview the shared amortization table, and
submit a typed `SaveLoan` request. The service validates account roles and owns the
atomic schedule/opening-balance mutation. The stored schedule uses `ipmt`/`ppmt`
formulas and an optional opening liability rather than freezing the preview into
fixed splits.

A mortgage payment is one balanced transaction described along several financial
dimensions. BreadSched must show the complete amount leaving the payment account
because that is the household's dated liquidity requirement, while also classifying
the transaction's components so expense, debt, escrow, and net-worth reporting remain
economically correct. The complete payment and its components are two descriptions
of the same dollars: a parent payment row is informational and must never be added to
its children in a section or grand total.

The approved representative monthly payment is:

| Component | Amount | Ledger and economic effect |
| --- | ---: | --- |
| Principal | `$800` | Checking decreases and the mortgage liability decreases |
| Interest | `$1,150` | Checking decreases and interest expense increases |
| Escrow funding | `$450` | Checking decreases and the restricted Escrow asset increases |
| **Complete payment** | **`$2,400`** | **Checking decreases once by `$2,400`** |

Plan must present the `$2,400` prominently as cash required. Its classified detail is
`$1,150` ordinary interest expense, `$450` escrow planning expense, and `$800`
debt-principal flow. Net cash change is negative `$2,400`; it is not the payment plus
those three components. Projection applies the balanced ledger effects on the payment
date: Checking falls by `$2,400`, the mortgage liability falls by `$800`, Escrow rises
by `$450`, and immediate ledger net worth falls only by the `$1,150` interest. The
linked house's market value does not change because a payment occurred; equity rises
through the separate reduction in debt.

Escrow intentionally has different Plan and ledger timing. Monthly escrow funding is
the household commitment Plan recognizes, while Projection retains it as movement
from spendable cash to a restricted asset. When accumulated Escrow later pays tax or
insurance, Projection reduces the asset and recognizes the ledger expense and
net-worth effect, but Plan does not count a second expense because the funding was
already planned. Shortages, refunds, vendor credits, and cash returns retain the
direction-sensitive escrow rules rather than being forced through the ordinary
monthly-payment case.

Dashboard and other liquidity views use the complete `$2,400` pending obligation,
not merely its interest component. Plan, Projection, Dashboard, comparisons, GTK,
web, and printable reports must consume shared mortgage-payment report data so every
surface uses the same non-additive grouping. One actual mortgage transaction resolves
the entire scheduled occurrence even when the realized principal/interest/escrow
allocation differs from the estimate; variance retains the expected and actual
component detail without creating several competing occurrences.

Extra-principal payments, lender fees, escrow adjustments, refinancing, sale, and
origination remain explicitly distinguishable. In particular, purchasing a
`$400,000` house with an `$80,000` down payment and a `$320,000` mortgage creates a
`$400,000` asset, reduces Checking by `$80,000`, and creates a `$320,000` liability.
Excluding closing costs, immediate net worth is unchanged: the purchase price is not
a `$400,000` household expense, the down payment is an asset conversion, and the loan
is financing. Closing costs and later interest are expenses; principal remains debt
reduction. Every report must therefore count each dollar exactly once within each
financial measure while keeping the full dated cash requirement visible.

### Escrow

Escrow is a restricted asset kind even when GnuCash stores it as `BANK`. Projection
therefore tracks its balance as a holding rather than spendable cash. Funding an
escrow asset from cash is recognized as household planning expense at funding time.
A later expense-account payment out of escrow reduces the asset but subtracts the
covered portion from planning expense, including proportional treatment of partial
escrow payments. The ledger transaction remains unchanged and balanced throughout.

Escrow recognition is a shared transaction-boundary interpretation, not a mutation
of splits or account classes. It first removes transfers between escrow accounts,
then allocates draws across positive expense legs and vendor credits across escrow
restorations. A remaining positive escrow movement is funding only when the event
has a spendable-cash outflow or income source; otherwise it is a manual/balance-sheet
adjustment. A remaining negative movement reverses planning expense only when it is
returned to spendable cash; otherwise it is an adjustment. Allocations are exact and
proportional when multiple escrow or expense accounts share one transaction.

This distinction makes combined mortgage payments dimensionally clear without
settling the separate mortgage Plan-presentation design question: interest remains
ordinary expense, escrow funding is an `ESCROW_FUNDING` planning flow and expense,
and principal remains a `DEBT_PRINCIPAL` flow that reduces the liability. Plan detail,
Projection detail, web responses, and printable Projection reports use explanations
from the same recognition result. Projection does not hide or clamp a negative
escrow holding; it preserves the reconciled state and emits a warning when an event
creates or worsens the shortfall. Exact imported GnuCash ledger facts remain source
owned, while the locally selected Escrow account type remains BreadSched owned on
re-import.

### Credit-card payments

Credit-card payment configuration is account-owned: paid-in-full versus carried
balance, usual carried payment, payment day, and optional Bank/Cash payment account
are one durable definition. Scheduled and Upcoming derive a non-persisted
`AccountPaymentDefinition` from it, and Dashboard consumes that same definition.
This avoids a copied schedule becoming a conflicting second source of truth. A
stable derived handle supports selection, but the editor returns to the account and
the derived occurrence is never posted automatically.

Before the due date, the current obligation is the live balance for a paid-in-full
card or the lesser of live balance and usual payment for a carried card. Once
unpaid and overdue, that occurrence is frozen at the account balance on its due
date; a second next-cycle occurrence contains only subsequent card activity, so the
statement amount is held exactly once. Any positive card payment funded by a
cash-like account resolves the overdue occurrence, even when partial, and the next
occurrence then uses the entire current balance normally. Merchant refunds and
payment reversals do not resolve it. Any enabled usable explicit/imported schedule with a
positive leg against the card suppresses the derived definition. The derived
definition intentionally does not generate an indefinite Projection recurrence:
future statement balances are not known, while the underlying purchases and an
explicit repayment plan are already the explainable forecast inputs. A finished
bounded schedule stops suppressing the account definition once all of its
occurrences have been posted or skipped.

### Scenarios and Projection

Scenarios change assumptions and planned activity without rewriting the base ledger.
They may add, alter, suppress, or override planned schedules. Assumptions may be
dated and account-specific so future behavior can change without hard-coded
retirement or lifecycle special cases in the engine.

Projection advances state through dated financial events and the intervals between
them. Base is the canonical expected plan, or "reality": it is derived from the
current ledger, book-level baseline schedules and estimates, and Base assumptions.
A saved scenario is an alternative layered over that state, never a second ledger
or an independent copy of Base. Reopening any scenario therefore recomputes it
against the current book, so new actuals and unmodified baseline schedules remain
honest inputs.

Scenario differences are explicit and explainable. Schedule replacements and
suppressions are sparse overrides of Base, while scenario-only events add to it.
Annual and account-specific assumptions use the same rule: a newly derived scenario
inherits each Base value until the user enables and changes that particular override.
Changing Base then flows through every inherited field without disturbing local
overrides. Plan, Projection, comparisons, and both scenario managers expose whether
an effective value came from Base, the saved scenario, or one of its dated overrides.

`engine/projection` calculates; `engine/projection_result` holds what it returns
(`Projection`, `MonthRow`, `MonthLedger`, `ProjectionProgress`, the month-detail
types, and `compare`), which `projection` re-exports. The bridge, print, CSV
export, and presentation import only the result module, and an architecture test
keeps the result module from importing the engine.

Each reporting month's `MonthLedger` records opening and closing stocks per account
and the exact flows and effects between them, and the engine raises rather than
return a month whose `reconciles()` check fails. `engine.projection_bridge` states
that identity for people without recomputing anything: from one ledger, or a run of
consecutive ledgers (a month or the whole horizon), it builds a `StockBridge` for
Cash (opening + planned cash flow + cash interest), Investments (opening + planned
movements + performance), Debts (opening + principal movement + interest), and Net
worth (cash + investments − debts, so planned flows net of transfers + cash interest
+ performance − debt interest). Each bridge reports its explained total and the
unexplained difference, which is zero for any month the engine accepted; the terms
are summed from the ledgers, so a defect would show as a non-zero difference rather
than being absorbed. GTK's month explanation, the web month report and projection
response (`bridges`), and `breadsched project --bridge` all render these bridges.

Scenario records persist an explicit inheritance flag and stable sets of field/account overrides;
cached projection results are never stored. A saved scenario may name Base or another
saved scenario as its assumption parent. Resolution walks that chain from Base through
each parent and then applies the child's stable field/account overrides; provenance
continues to name the scenario that supplied each effective value. Writes reject
missing parents and cycles, and deletion refuses a scenario that still has children,
so reparenting is always explicit. Dated periods and scenario events are local to
their owning scenario; they are not inherited, because those lists have no identity
that would let a child merge them unambiguously. An older record that stored a
complete assumption snapshot loads each stored value as a local override, so an
established forecast cannot change on upgrade.

Saved-scenario lifecycle writes use one typed service across CLI, GTK, and web.
Creation/update, duplication, deletion, reparenting, and baseline-schedule
suppression validate stable scenario identity, unique names, parent existence and
cycles, dependent children, and source schedules before an atomic write. Base
assumption persistence and dated overrides share a separate projection-assumption
service because Base is book metadata rather than a saved scenario record.

Reporting periods aggregate projection state but do not drive it.

Schedule growth is explicit enough to be explainable. The current model supports
`auto`, `none`, `income`, and `inflation`; `auto` is a compatibility/default policy,
not an excuse to hide ambiguous economics. Formula schedules default to fixed
nominal behavior. Mixed gross-to-net payroll grows as one balanced income event.

Economic-sense tests are required in addition to bookkeeping reconciliation. A
projection that balances mathematically can still be financially wrong.

### Historical estimates

`engine/estimates` gathers a category's planned and actual history from the book and
turns it into proposals; the statistics it applies (robust sample, typical amount,
recurrence and next start, cadence, trend, seasonality, and confidence, with their
evidence types) are pure functions in `engine/estimate_history`, which never reads
the book. `estimates` calls them through the module (`estimate_stats.…`), and an
architecture test keeps `estimate_history` free of database and planning imports.

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
the next twelve planning months as a calendar-month profile. One monthly-history
pass records gross activity, applied coverage, residuals, escrow adjustments,
transaction counts, and month-of-year samples before any proposal scoring. This
observation result is kept separate from cadence, trend, seasonality, confidence,
funding, and final proposal assembly. Historical scheduled
occurrences are not also subtracted, since a schedule may have ended or changed
amounts. Calendar-month matching retains the exact future year/month rather than
collapsing it to a month number. Sustained full coverage through the remainder of
that rolling year may bound a monthly bridge estimate before the replacement starts;
an isolated future event cannot do so. Annual, biennial, and triennial history keeps
its inferred interval and advances the last observed date to the next due date.
One observed event alone is insufficient evidence for recurrence and is therefore
offered only as a reviewed one-time draft.

Estimator confidence is evidence, not a probability claim. It combines completed-
month coverage, sample depth, the proportion retained after conservative anomaly
handling, and median absolute deviation relative to the typical amount. Outlier
removal requires at least six active months and must retain at least three; every
exclusion is reported to the user. Calendar-month cadence inference recognizes
stable multi-month intervals independently of day-of-month drift, while weekly and
multi-year rules retain their dedicated date-gap semantics.

Those thresholds, confidence weights, cadence and trend tolerances, seasonal
criteria, variability bands, and funding-candidate tie-breaks belong to one immutable
versioned rule object passed through the estimator. Each proposal's structured
evidence contains the complete applied policy and its stable version; an accepted
draft retains that evidence. A policy change therefore receives a new version and is
rechecked against the independent historical-estimation golden book, whose authored
calculations cover spike handling, funding selection, recurrence anchors,
seasonality, trend selection, and confidence arithmetic. The rule version is
explanatory evidence, not a claim that the confidence score is a probability.

Historical estimation also interprets transactions before aggregating account
history. Flow-account legs paired with reinvested dividend/interest, investment-fee,
or rollover activity are investment bookkeeping rather than recurring household
income or expense. Funding-account inference retains occurrence frequency and uses
spendable cash only as the deterministic tie-breaker, so a loan-principal,
retirement, or restricted-asset leg cannot displace an equally observed cash
counterpart. The same transaction-boundary exclusions apply to historical cadence
evidence and matching future coverage.

Planning-relevant balance-sheet legs are estimated separately from flow-account
categories. The estimator uses the same explicit-or-inferred classification as
Plan for retirement saving/distributions, debt principal, and benefit/FSA funding;
taxable investment contributions and withdrawals retain their investment-activity
classification. A proposal identity includes account, counterpart, and purpose so
opposite activity on one holding remains independently reviewable. Future coverage
matches the account and classifications before it is subtracted, and duplicate
cash-counterpart annotations on a retirement distribution count only once.

### Savings goals

A `SavingsGoal` (`gen/lib/savings_goal.py`, table `savings_goal`) names an asset
account, a target amount, a start date, a target date, dated extra allocations, and
an optional closing date. The goal owns no ledger transactions: what it sets aside is
an earmark on its account derived on demand, never stored, so a back-dated income
posting or allocation is reflected immediately.

`engine.savings_goals.goal_progress` computes the earmark the way a bill's reserve is
computed, with the same income rules (`engine.cash_flow`: an income occurrence counts
once posted, or while still in the future), so a missed paycheck funds neither a bill
nor a goal. From the start date the timeline is cut at each allocation; in each
segment the open gap is funded in proportion to *income received in the segment /
income expected through the target date* (elapsed time when no income is expected).
Allocations are set aside in full on their dates, the whole target is set aside from
the target date on, a closed goal sets nothing aside, and amounts stay exact until
they are quantized once to the reporting fraction. Goals are limited to
reporting-currency asset accounts, so no conversion is implied.

The Dashboard subtracts `goals_held` from Available alongside the liquidity
requirement: the whole earmark of a goal in a cash-like account, but only the part
of a non-cash account's earmarks its balance does not yet cover. Liquid, months
covered, and the emergency fund are unchanged. Transfers into a goal's account stay
transfers and are never projected as expenses.

Goals are pinned across scenarios. `Scenario.goal_overrides` changes a goal's target,
date, or inclusion in one scenario, applied to copies by
`engine.goal_projection.effective_goals`; the goal itself is never written.
`project_goals` runs after the event projection: it starts from the actual earmark
the day before the scenario, funds each month's gap from that scenario's own
projected income, and holds the whole target from the target month. Projection rows
carry `goals_set_aside`, `goals_held`, and `cash_after_goals`, milestones record
whether projected cash covers goal money, and `first_goal_shortfall` is the first
month cash covers bills but not goals. A goal in a non-cash asset account is
compared with that account's projected closing balance (`MonthLedger.closing_holdings`,
passed to `project_goals`) in its target month against every earmark on that account
then (`GoalMilestone.account_close` and `account_held`); an account excluded from
projection has no balance, so `covered` stays `None`. `GoalMilestone.as_dict` is the
one JSON shape the web and CLI return. Plan lists goals whose target date falls in its
range with what they have actually set aside.

A goal override can also model the purchase (`GoalOverride.purchase_on`,
`purchase_account`). `planning.goal_purchase_events` turns it into a scenario-only
`ONE_OFF` planned event (placeholder, two balanced splits: the target into the
purchase account, out of the goal's account), so Plan and Projection share it and it
is never escalated. `project_goals` sets nothing aside from the purchase month on,
and the milestone records the purchase. `services.savings_goals.set_goal_override`
refuses an incomplete purchase, a date before the target date, and an account that
is not a non-placeholder expense or asset other than the goal's own; leaving the
goal out drops the purchase.

`services.savings_goals` owns validation and every write (save, allocate, close,
reopen, delete, and scenario overrides, which a goal deletion removes in the same
undo step); allocations cannot exceed the target, and deleting a goal's account is
refused by reference verification. `presentation` gives GTK, web, CLI, and print the
same status and milestone wording.

### Retirement drawdown

`Scenario.drawdowns` holds `Drawdown` rules: monthly withdrawals from a holding
account into spendable cash from `start` (on its day of the month, clamped to short
months) until an optional `end`. A rule withdraws either `annual_amount` / 12, grown
by the scenario's expense inflation at each anniversary of `start` when `escalate`
(the rate in effect on that anniversary, from the dated assumption timeline), or
`annual_rate` / 12 of the account's projected balance on the withdrawal date. The
amount depends on projected state, so the projection's `_Drawdowns` sizes each
withdrawal inside the event loop: it advances balances to the date, after that day's
planned events, then applies an ordinary scenario-only `ONE_OFF` placeholder event
(two balanced splits), so ledger classification (a retirement account's withdrawal
is a retirement distribution), the conservation bridge, and month detail need no
special case. A withdrawal is capped at the balance, with one "runs out of money"
warning; a rule whose account is not a projected holding, or whose target is not
projected spendable cash, is left out with a warning. Drawdowns never post, are not
Plan rows, and are not inherited by child scenarios. Rules are stored inside the
scenario's serialized document, so there is no schema change; a version without
drawdowns ignores them and drops them if it saves that scenario.
`services.scenarios.save_drawdown` and `remove_drawdown` own validation and writes:
the source must be a non-cash asset and the target spendable cash
(`drawdown_accounts`, both excluding placeholders and projection-excluded accounts),
exactly one of a positive amount or a rate in (0, 1], and an end not before the start.
`Scenario.account_references` names every account a scenario uses (rates, opening
overrides, one-offs, dated periods, goal purchase accounts, and both drawdown
accounts); whole-book and change verification both use it, so deleting any of
those accounts is refused. The GTK `DrawdownsDialog` (from the scenario manager) and
the browser's **Retirement drawdowns** section call the same services through
`/api/scenario/drawdown/save` and `/delete`, whose adapter only parses input (a
browser percent is divided by 100 as a decimal, never a float) and returns the
scenario payload with its `drawdowns`; `/api/scenarios` lists `drawdown_sources` and
`drawdown_targets`.

## Valuation and reporting

### Currency conversion and valuation

The internal currency-conversion result selects the latest eligible direct quote
on or before the requested date for one exact source/target pair. If absent, it
inverts the latest eligible reverse-pair quote with an exact rational factor. A
direct quote always wins over a reverse quote even if its date is older; path,
selected quote date, source, and type are carried without inventing a new quote.
Identity conversion needs no quote. An absent pair returns no amount. The result
stays exact so a report can aggregate converted amounts before applying the target
currency fraction once. Multi-hop routes have no implicit precedence.

The activity report applies that conversion once, before aggregation. For each
currency it selects the one quote applicable on the report as-of date and scales
event and transaction split values by the exact rate. Every Plan figure is then
derived from converted splits: period totals, category rows, drill-down detail,
planning flows, the cash bridge, the projected cash position, and Expense Explorer.
Evidence for each quote used (rate, date, source, and direct or inverse path) is
kept on the report. An event or transaction with no applicable quote is excluded
from every total and listed as unconverted by period. Categories affected by an
exclusion stay visible, and Expense Explorer suppresses their Remaining.
`currency_notes` renders these facts as the same sentences in GTK, web, CLI, and
print. `gen/engine/conversion.py` owns the converter, evidence, and notes, so
Projection applies the same policy at its opening valuation date (the day before
the horizon). It converts events before applying them and excludes a
foreign-currency opening balance whose valuation reports a missing quote. The
converter's notes are recorded as Projection warnings, which every surface
already shows. Comparisons difference two converted projections. Each note
states the quote's age on the as-of date through `valuation.quote_age_label`,
without a staleness cutoff, and states that conversion is not rounded to cents
before aggregation (Plan stays exact; Projection keeps its internal eight-place
precision).

A security is valued with its latest as-of quote in the reporting currency. Only if
it has none is its latest as-of quote in any other currency used: the market value
is computed exactly in that quote currency and then converted once with
`convert_currency` (direct, else inverse) as of the same date. This is one exchange
hop from the security's own quote currency, never a chain through a third currency.

Currency conversion is **direct rate only**, a deliberate product decision: a
currency converts to another only through that pair's own as-of quote, or the
inverse of the reverse pair. No bridge or intermediate currency is ever used, even
when quotes through a third currency would connect the two, so a missing pair
always remains an explicit missing quote rather than an inferred chain.
`AccountValuation.exchange` carries that conversion's evidence; when it is missing
the valuation is flagged `missing_quote`, keeps the market value in the quote
currency (so Projection reports it as unconverted), and is left out of totals.
`valuation.quote_evidence` produces the one evidence line (security quote date and
source, then the exchange quote's date, source, and inverse path, or the missing
rate) shared by the GTK and web Accounts views, CLI `accounts`, and Dashboard group
members, so no surface implies a conversion that was not performed. Foreign-exchange graphs,
automatic quote retrieval, lot/cost-basis accounting, and projected market prices
are separate concerns and must not be approximated by treating monetary amounts as
prices or quantities.

Ordinary foreign-currency account valuation uses the latest direct quote on or
before the as-of date, then the latest eligible reverse pair if no direct applies,
retaining the exact converted amount until presentation. Its result carries quote
date, source, inversion path, and age relative to the valuation date. GTK, web,
and CLI account views disclose that age. No automatic age cutoff excludes a quote;
future-dated quotes selected without an explicit as-of date have a negative age
and are labeled as dated ahead. Without either quote the result keeps the original
tagged ledger amount and exposes the missing quote. Aggregate reports
for the account chart, cash, and net worth sum exact tagged values only when
every nonzero component is in the reporting currency with any required pair quote.
The aggregate result carries missing-quote account handles and returns no total
when incomplete. Presentation does not replace a missing total with zero or add
ledger fallbacks in another currency.
Dashboard's configured group valuation uses the same exact aggregate result for
each selected account subtree. A missing quote is propagated through generated
group headings; position totals and dependent liquidity outputs are suppressed
at the report boundary rather than presenting a partial internal calculation.
Fallback spendable cash uses the same aggregate. The remaining bill and income
rows are independent of current-balance valuation and stay visible. GTK, web,
CLI, and print share the missing-account disclosure; no imported quote is edited.

The account valuation result carries the selected quote date and source for market
values and explicitly marks a security whose reporting-currency quote is missing.
Accounts views in GTK and web show that evidence or the ledger-value fallback.
This does not establish a stale-price threshold or convert foreign-currency ledger
amounts; quote selection still uses the latest applicable reporting-currency price.

### Valuation completeness

`engine/completeness` gives every reporting-currency result one structured
coverage value: `Completeness(status, policy, excluded, as_of)`. The status is
**complete** (every input converted, including a genuine zero, which needs no
quote), **partial** (a subtotal of what converted), or **unavailable** (withheld).
Each `Excluded` item records the kind (balance, planned, actual, posting), the
account or event label, the unconverted amount in its own commodity, the date,
whether an exchange rate or a security price is missing, and the involved account
handles, and derives the corrective action. Adapters render `label` and `detail()`
and serialize `as_dict()`; none infers coverage from note text, and the web
transport stays free of engine imports, so resources call `as_dict()` themselves.

The policy is chosen per report rather than forced to be uniform. Plan, Expense
Explorer and Projection use `SUBTOTAL`: a plan with one unconvertible schedule is
still useful for everything else, provided the gap is labelled beside the number.
Balances, net worth, and the Dashboard use `WITHHOLD`, because a net worth that
silently omits an account is a wrong answer rather than a smaller one. A comparison
uses `combine()`, which keeps the worse status and both sides' evidence, so a
difference between two partial values is never shown as complete. Projection
coverage is per month: an excluded opening balance affects every month, and an
excluded event affects its month and every later one, since balances carry it
forward. Plan coverage is per period and per category cell (from the existing
`unconverted_accounts`), and the through-as-of summary counts only exclusions dated
by the as-of date. Missing valuation and temporal non-applicability stay separate:
a future period's variance is `None` (not applicable) whether or not its inputs
converted.

A security with no price at all lacks a *price* and is described by its unit
quantity; one priced only in another currency lacks an *exchange rate* and is
described by its foreign market value. Projection opens an unpriced security at its
ledger value; only foreign-currency balances without a rate are excluded there.

### Reporting terms

Reporting terms have one definition, computed once in `engine/activity` and only
labelled by GTK, web, CLI, and export. Period cells are whole-period figures:
**period actual** includes every posting dated in the period, even after the as-of
date, and **period variance** compares it with the whole period's plan for periods
that have started. The summary instead stops both operands at the same date:
`planned_cash_through_as_of` sums the cash change of expectations dated on or before
the as-of date, `actual_cash_through_as_of` sums postings on or before it, and
`cash_variance_through_as_of` is their difference, so the summary never compares an
actual that stops at the as-of date with a variance that does not. Expectations count whole on their
dates rather than being prorated across a period: proration would invent daily
spending for bills that fall on one date, and it would differ between month, quarter,
and year grouping, whereas dated expectations give the same through-as-of figures
under every grouping. The accepted consequence is that a bill paid before its planned
date reads as spending ahead of plan until that date. Period actual keeps
future-dated postings so that a period's column still reconciles to its register,
and a posting's detail says when it is counted in period figures but not through
as-of. A wholly future horizon reports the through-as-of values as not applicable,
not zero.

### Dashboard

`engine/dashboard` assembles the view from account groups and totals;
`engine/dashboard_bills` owns the scheduled-bill side it uses (cycle lengths and
`BillRow` normalisation, missed-occurrence grouping, income-weighted reserves, and
the pending cash flow behind liquidity) and never imports `dashboard`, which
re-exports `BillRow`, `MissedGroup`, `group_missed`, and `DAYS_PER_MONTH` for its
callers.

Dashboard totals never depend on groups. Net worth, Assets, and Debts are always the
whole-book reporting-currency valuation of every asset and liability account
(`valuation.aggregate_value`), and Liquid is always every non-placeholder cash-like
account. A missing or incompatible quote withholds the dependent figures with their
evidence (see [Valuation completeness](#valuation-completeness)). Groups only arrange
accounts into rows; they never change totals, near-term needs, or the bill and income
lists, and a group's kind (including "liquid") only labels its rows. Each unavailable
field carries a reason that separates missing quotes from absent setup: without
recognized committed outgoings, emergency fund, shortfall, and months covered are
unavailable rather than zero, because unscheduled spending is not estimated. Coverage
notes (such as a card with a balance but no payment setup,
`schedule.unconfigured_card_balances`) reach every presentation and print.

A group row's `note` carries only its directly selected accounts' own valuation
notes; descendants included by inference are `members`, one line each with any quote
evidence, shown in the GTK tooltip, the web card title, and JSON. Long row labels and
card text are capped and wrap, because an ellipsized GTK label still requests its full
natural width.

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

#### Bills, income, and reserves

The general Dashboard presents committed bills and expected income in separate
dated lists. Income stays positive and has no Hold-now value. Monthly and annual
normalization remains useful for comparison and emergency-fund sizing, but it is
never substituted for dated income when calculating liquidity.

A scheduled credit-card purchase remains an expense in Plan and Projection but is
not itself a spendable-cash bill. The generated account-payment row represents the
card's current ledger balance and deliberately excludes unposted future purchases.
Once those purchases post, the later payment obligation incorporates them. This
keeps expense recognition and cash timing visible without counting both the
purchase and payment as immediate liquidity requirements.

Missed occurrences are grouped only for presentation. `Dashboard.bills` and
`Dashboard.incomes` keep one dated row per unresolved occurrence, and liquidity,
hold, and emergency calculations use those rows unchanged.
`Dashboard.display_bills` and `display_incomes` pass them through
`group_missed`, which collapses two or more overdue rows of the same schedule (or
the same generated account payment) into one `MissedGroup` at the oldest date.
The group sums amounts and holds, reports the schedule's normalized monthly and
annual figures once, and keeps every `(date, amount)` pair. A single overdue row and
current rows stay ungrouped. GTK, web, CLI, and print render these lists and show
the recurrence's own words (`frequency`) rather than the internal decimal month
cycle, which printed as `1.0000`.

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

Credit-card accounts synthesize dated pending payments when they have a positive
balance, a payment day, and no enabled explicit schedule already paying that card.
A paid-monthly card uses its full balance. A revolving card uses its usual payment,
capped at its balance. An account-payment row holds its exact amount rather than an
income-accrued fraction and does not display a monthly or annual normalization.
This is an account-tied presentation row, not an invented ledger or scheduled-
transaction object.

### Expense Explorer

Expense exploration is a read-only service built from the same typed Plan query.
Category periods and section totals reuse Plan's rollups. A selected category cell
uses Plan's detail calculation for occurrences and actual transactions; merchant
groups fold trimmed transaction descriptions case-insensitively in memory and sum
those actual contributions. The category's plan is never allocated to merchants.
The service checks merchant and detail totals against the Plan cell before returning.

GTK's Explorer dialog and the web Plan explorer use the same read-only service.
The web API exposes its typed values and one selected category cell; presentation
code draws comparison and trend charts without calculating financial totals.
Web printing includes the applied explorer state with the Plan page. GTK prints
its selected category and period from a self-contained report using the same service
result as its tables; HTML escaping keeps imported descriptions as text.

Expense Explorer derives Remaining from the full-period category plan and actual
contributions through the report's as-of date. A partial period retains its
full-period Period actual and Period variance separately (see
[Reporting terms](#reporting-terms)). Parent categories use the same
Plan rollups, while section totals sum only outermost category rows. A foreign
expense event without Plan currency conversion suppresses Remaining for that
category and its ancestors, with an explicit reason. Future-only periods are
unavailable. Optional rollover starts with zero at the selected horizon, carries
only completed prior periods, and displays Carry in + period plan - actual through
as-of = Remaining. A period with missing conversion blocks subsequent carry.

Spending over time (`ExpenseExplorer.spending`) is one `SpendingPoint` per Plan
period: the section's total plan and actual, `future` (starts after as-of),
`partial` (contains as-of), `currency_incomplete` (a category in the period has
unconverted foreign activity), and the actual split across top-level categories.
A lone root category, normally the book's `Expenses` account, is replaced by its
immediate children, with anything posted to the root itself kept under the root's
handle; the service asserts that the split sums exactly to the period actual. GTK
draws it with the shared `LineChart` (a dashed as-of marker, the selected period
shaded, `index_at` mapping a click to a period), the web page with an SVG whose
period hit areas are keyboard-focusable buttons, and the printable report as a
table. Selecting a period drives the existing comparison and merchant drill-down,
so every drill-down stays on the shared Plan values.
Income over time (`ExpenseExplorer.income`) is built by the same `_over_time`
helper from the Plan report's income rows and `category_totals(INCOME, ...)`, so
its periods, as-of flags, currency flags, top-level split, and exact
reconciliation assertion match spending's; `income_categories` supplies the
names. GTK, web, and the printable report show it as a second chart and table
after spending. Selecting one of its periods selects that period for the whole
explorer; the category comparison remains expense-only. The drill-down accepts an
income category too: `explain_category_period` already explains income cells, so
the same reconciliation asserts that its planned events and actuals equal the Plan
cell, and actuals group by payer (`ExpenseDrilldown.income`, "Unknown payer" for a
blank description). GTK and the web page show it as **Income detail**, chosen
independently of the expense trend category. The web page prints itself, so its
Income detail is printed as shown; the GTK printout passes the chosen income
drilldown to `expense_explorer_report(explorer, income_detail)`, which appends it
only when it is an income drilldown.

### Net worth history and change

Net worth history (`services/net_worth.query_net_worth_history`) values every
asset and liability account with `valuation.account_value` on each period's end,
using Plan's display buckets (`activity.reporting_periods`). The period containing
the as-of date is valued on that date and marked partial; later periods are
omitted because the ledger has no future balances (Projection forecasts them).
Values are summed per top-level account tree and kind; any account without a
reporting-currency value withholds that point's totals and change and is named in
`missing`, never converted by guesswork. Each account's ledger balance is carried
from one point to the next by adding only the splits since the previous date
(`ledger.balance_amount(since=...)`), and `valuation.account_value(ledger_amount=...)`
values that balance, so each split is read once however many periods are shown;
a 30,000-transaction book's twelve months take well under a second (a performance
test guards it). Tests assert that every complete point equals
`valuation.net_worth` on its date, so it always matches the Dashboard's valuation. The CLI `net-worth` command, the web
`/api/net-worth-history` resource (typed month parsing only) and Dashboard
section, the GTK Dashboard **History** dialog, and the printable report all render
the same points; the GTK chart plots only complete points.
`query_net_worth_change(start, end, today)` explains one change. It values net worth
at the end of `start - 1` and on `min(end, today)` the way a history point is valued.
It then groups every asset and liability split dated in that window by transaction,
summing the raw signed values, since those sum to net worth. The window's splits are
read once (`split_rows` also returns each transaction's date and description, so no
transaction is decoded). They feed both the postings and the closing ledger balances,
which are the opening balances plus the window. A performance test covers a month
that holds a 30,000-transaction history. A transaction that nets
to zero is a transfer between the household's own accounts; it is counted in
`transfers` and left out. A foreign-currency effect is converted with
`valuation.convert_currency` at the posting date. Without an applicable quote the
effect is `None` and named in `missing`, and `posted` and `revaluation` are
withheld. Otherwise `revaluation = change - posted`: the price and exchange-rate
movement on holdings, including the gap between a security's cost and its market
value. So `posted + revaluation == change` exactly, by construction. The CLI
`net-worth-change` (with `--csv`), web `/api/net-worth-change` (from/through ISO
dates; the payload carries the shared CSV text for download), the GTK history
dialog's Change buttons, `html_report.net_worth_change_report`, and
`csv_export.net_worth_change_csv` all render that one result.

### Printable reports

Printing is a presentation of an already calculated view, not another financial
engine. A printable Dashboard, Plan, or Projection consumes the same structured
engine result held by the visible GTK view, so printing cannot silently substitute
different dates, grouping, measure, scenario, assumptions, or values. A synchronous
print boundary (`ViewManager._printable_view`) waits for an in-flight background
calculation before it reads the visible view's result; a bounded timeout reports
failure instead of reusing a stale or previously opened report.

Each report is laid out once, in `plugins/export/report_layout.py`, as a
renderer-neutral `ReportDocument`: sections of headings, paragraphs, summary cards,
tables (columns flagged numeric; cells carrying text, sign, indent, a note inline or
below, and hover text; rows styled heading/section/total/grand), and a line chart.
`html_report.render_html` renders it for the browser, and `gui/report_printer.py`
draws it natively, so both routes print the same words and numbers. A view offers
`printable_report()` and derives `printable_html()` from it.

**Print** (`Ctrl+P`) runs a `Gtk.PrintOperation` in points, landscape by default,
with the page setup and settings chosen earlier in the session. Its dialog offers
the platform's printers, preview, and printing to a PDF file; a report with an
optional section adds a **Report** tab (`create-custom-widget`) with its checkbox.
`ReportPrinter.paginate` measures every block with a Pango context at 72 dpi, so
one unit is one point, and flows items onto pages: tables split only between rows
and repeat their heading row on each continued page, a heading keeps with the
content after it, an optional section starts a new page, and every page carries
"title · page N of M" at its foot. Column widths start from each column's widest
content (bold rows measured bold); text columns take spare width or wrap down to a
floor, numbers never wrap, and a table that still does not fit shrinks its type
toward a 5.5 pt minimum and then scales. `printing.export_pdf` draws the same pages
straight to a PDF 1.4 file, which tests and the Windows installer check use.
**File → Print in Browser…** is the fallback: a private,
owner-readable HTML preview opened in the default browser and removed when the
application exits. The dialog reports (Net Worth History, its change detail, and
Expense Explorer) have layouts of their own and print through
`printing.print_document`, which opens the browser preview only when GTK printing
fails. A cell may span columns (`Cell.span`, used by the net worth change totals)
and hold line breaks (merchant transactions, top-level account values).

Plan printing has two explicit layers. The default print surface contains the
scenario/horizon context, liquidity cards, and signed cash bridge needed to locate
a shortfall. Positive budget categories, mortgage requirements, and informational
balance-sheet classifications form an optional appendix selected in the preview.
The appendix begins on a new page, repeats static table headings, and uses compact
numeric spacing; sticky screen headers must never enter print layout. Printed Plan
headers omit the book path. Browser-added URL/date/page margins remain controlled by
the browser print dialog.

The web interface prints its current rendered view directly. Print-specific CSS
removes navigation and editing actions, restores tables hidden by screen scroll
regions, and preserves text, tables, and SVG charts as scalable output. Both paths
keep formatting and pagination in presentation code while all monetary values,
totals, classifications, and comparisons remain engine-owned.

## Household workflows

### Payees

A `Payee` (`gen/lib/payee.py`, table `payee`) is a named identity with a list of
normalized description keys. `Transaction.payee` holds the accepted payee's handle
and is BreadSched-owned: GnuCash, OFX, QIF, and CSV re-import carry it forward
through `import_review.merge_local_state`, and no import rewrites the description.
`engine.payees.match_key` casefolds and NFKC-normalizes a description, splits on
punctuation, and drops every word containing a digit, so store numbers, card
suffixes, and references do not split one merchant. Matching is exact on that key,
never fuzzy, so every proposal names the key that produced it. The service
(`gen/services/payees.py`) keeps names unique (case-insensitively) and gives each key
to at most one payee, so a proposal is never ambiguous; a conflicting key is
refused. `preview_payee_proposals` lists transactions without a payee whose key
matches and writes nothing. `apply_payee_proposals` recomputes the proposals and
assigns only the requested transactions that still match and still have no payee,
in one undo step, so a stale preview cannot replace a choice made since.
`assign_payee` sets or clears one transaction explicitly; `delete_payee` clears the
payee from its transactions and deletes it in one undo step. Book verification
reports `transaction.missing_payee` and `payee.duplicate_match_key`.
`TransactionInput` carries `payee` with an explicit `set_payee` flag, so an editor
that does not show the payee keeps it, and an unknown payee is refused. GTK's Payees
dialog, the web **Payees** view (`web/payee_resource.py`), the registers, and the CLI
are adapters over this service; categorization rules and entry autocomplete can
match on the payee.

### Categorization rules

Rules live in book metadata (`categorization_rules`) as one ordered list of
`CategoryRule(handle, category, payee | key)`, so they need no schema change and
older builds ignore them; rule edits are metadata writes inside a database
transaction and therefore undoable. `engine/categorization.py` owns
`placeholder_handles()` (the Uncategorized CSV/OFX accounts, also used by CSV
transfer review) and `propose_categories`, which considers only transactions with
exactly one split on a placeholder: a category chosen by the user or the source is
never proposed again, and split transactions are not guessed. A rule matches by
payee handle or by `payees.match_key` of the description; the first matching rule
in list order decides and every later rule naming a different category is reported
as a conflict with its position. `services/categorization.py` validates rules (one
match kind, a non-empty key, an existing payee, a non-placeholder income or expense
category, no duplicate match, a valid position) and applies accepted proposals by
recomputing them and replacing only the placeholder split's account, keeping its
value, memo, and handle, in one undo step. GTK's Rules dialog, the web **Rules**
view (`web/rules_resource.py`), and `breadsched rules` are adapters over this
service.

A description rule may also carry `set_payee` (stored beside the other fields, so
older builds ignore it). Its proposal's `payee` is that handle only while the
transaction has no payee; accepting sets it only if the transaction still has none
and the payee still exists, and reports the count (`AppliedCategories.payees_set`).
A payee rule cannot set a payee (`rule.set_payee.payee_match`): it matched because
the payee was already set. `services/payees.delete_payee` removes rules matching the
deleted payee and clears it from rules that would set it, in the same undoable
transaction, so no rule points at a missing payee.

### Tags and linked documents

Tags reuse `PrimaryObject.tags` on `Transaction`; linked documents are
`Transaction.attachments` (BreadSched-owned locations, in order) and
`Transaction.source_link` (GnuCash's `doclink`, or `assoc_uri` before GnuCash 4). Both
live in the transaction's JSON, so books without them load with empty defaults.

- **Ownership.** Re-import carries `tags` and `attachments` forward like the payee;
  `source_link` is source-owned and refreshed. The source link enters the import
  fingerprint only when set, so older imports still compare unchanged.
- **Locations.** `engine/attachments.py` resolves a web address (never fetched), a
  `file:` URI, an absolute path, or a relative path. BreadSched's relative locations
  resolve against the book's attachment folder (by default `<book stem> attachments`
  beside the book); GnuCash's resolve against the GnuCash linked-files folder. Links
  are kept as written and files are never copied between the two.
- **Missing files are a state, not damage.** Verification does not fail on a missing
  document; the link is kept so that it can be restored or relinked.
- **Writes.** `services/attachments.py` owns every change in one undoable
  transaction: tag normalization (whitespace collapsed, case-insensitive duplicates
  dropped, the book's existing spelling reused), linking, copying a file into the
  folder under a name that never overwrites, unlinking (files are never deleted), and
  relinking. `TransactionInput.tags` of `None` keeps the stored tags, so editors that
  do not show them never drop them.
- **Browser trust boundary.** Any page holding the token could call the web routes, so
  linking from the browser accepts only a web address or a location that resolves
  (symbolic links included) inside the attachment folder
  (`services.contained_location`); otherwise the routes would become a file reader.
  Uploads have a plain file name checked twice, content is served only for a location
  the transaction lists, every response carries `X-Content-Type-Options: nosniff`, and
  only PDF, image, and plain-text documents open in the page.
- **Not included.** Backup and restore cover the book, not the attachment folder, as in
  GnuCash, and nothing is written back to GnuCash.

### Reimbursable expenses

A `Receivable` (`gen/lib/receivable.py`, table `receivable`) tracks an out-of-pocket
expense and what an insurer, employer, or other payer is expected to reimburse, as
linked but distinct facts. It never rewrites the expense split it references, and a
reimbursement is never new income: like a refund, it is a negative split in the
*same* expense account, so it offsets net spending without touching the original.
`engine/receivables.receivable_summary` never stores a status; it recomputes it from
the linked splits plus any dispute or write-off: **written off** (a deliberate,
terminal decision), **settled**, **disputed**, **partial**, or **open**. What is owed
is the expected amount, or the whole linked expense, never more than the expense.

Expected receipts: `receivables.expected_receipt` gives an open or partly reimbursed
receivable with a receivable account, a reporting-currency posting, and an
`expected_cash_date` on or after today the amount still owed and the cash account
that paid its expense (a card's `card_payment_account` for a card-paid expense);
disputed, overdue, settled, and written-off receivables have none.
`planning.receivable_receipt_events` turns each into a placeholder `ONE_OFF` planned
event (cash +remaining, receivable account −remaining) in every scenario's events, so
Plan and Projection count it as cash only from its date, never escalate it, and leave
net worth unchanged.

Gross and net cost: an expense category's Plan actual is already the net household
cost, because the tracking posting moves what is owed out of the category and each
reimbursement credit is cancelled by its posting. `receivables.plan_adjustments`
names the receivables' owned postings and linked reimbursement splits;
`activity.build_activity_report` copies those splits (converted like the rest) into
`ActualActivity.reimbursable_splits`, and `category_report.build_category_report` negates their
expense-class values into `CategoryActivity.reimbursable`, rolled up like `actual`.
So `gross = actual + reimbursable` is what was spent, and `reimbursable` is what was
reimbursed or is still owed less write-offs; nothing is added twice, because a
reimbursement's credit and its posting cancel in both figures.
`own_reimbursable` (the account alone) selects `CategoryReport.reimbursable_categories`,
so a parent category is not listed beside its children. `plan_detail` sums the same
splits into `CategoryPeriodDetail.reimbursable`. `presentation.plan_reimbursable_text`
and `plan_detail_cost_text` give the sentences GTK, web, print, and
`receivables --costs` show; a range holding only a write-off of an earlier expense
reads as a raised net cost with no gross cost.

Scenario expectations: `Scenario.reimbursement_overrides` maps a receivable handle to
a `ReimbursementOverride` (what the payer pays in that scenario, zero for nothing,
and the expected date). `engine/reimbursement_outlook.scenario_receipts` applies it
to each `expected_receipt` (an amount is capped at what is still owed; a date before
today is ignored), and `planning.receivable_receipt_events` turns each into one
balanced placeholder event: cash +paid, receivable −owed, and the largest linked
expense split's account +shortfall (exactly where a recorded write-off posts). So
the receivable still empties on that date, and the shortfall is a projected expense
in Plan and Projection for that scenario only. `reimbursement_outlook` gives each
receipt in a projection's range its gross cost (`ReceivableSummary.expense_total`),
earlier reimbursements and write-offs, what the scenario expects, the shortfall,
and `net_cost = gross − reimbursed − expected`; `Projection.reimbursements` carries
it and `presentation.reimbursement_outlook_text` words it for GTK (projection
notes), the browser (`reimbursements` and `reimbursement_notes` on the projection
payload), print, and `project`. `services.scenarios.set_reimbursement_override`
refuses a receivable not currently expected, an amount outside zero to what is owed,
and a past date, and clears the change when both are empty; deleting a receivable
drops every scenario's change to it in the same undo step. The GTK receivables
dialog's **In scenario** row and the browser's (`/api/receivable/scenario`, with
`scenarios` and each receivable's `scenario_changes` on `/api/receivables`) only
parse input and call that service; `presentation.reimbursement_override_text` words
each change.

`services/receivables.py` validates every write: linked splits must be in
expense-class accounts, expense links positive and reimbursement links negative, and
no split linked twice. The database refuses to delete a transaction a receivable
links to (`receivable.missing_transaction`), as it does for FSA claims, and a
GnuCash re-import that would delete one is reported instead.

What is still owed is held in a **Receivable account** (`Receivable.account`): part
of net worth, never spendable, so it never counts toward liquidity. BreadSched owns
the reclassification transactions that put it there, planned by
`engine.receivables.planned_postings` from the receivable alone:

| Event | Posting | Date |
| --- | --- | --- |
| Tracking | receivable +owed, expense −owed (allocated over the linked expense splits) | incurred date |
| Reimbursement linked | expense +amount, receivable −amount | the credit's date |
| Write-off | expense +amount, receivable −amount (the largest expense split's account) | write-off date |
| Dispute | nothing | — |

Reimbursements and write-offs apply oldest first and never take the receivable
below zero; money back beyond what was owed stays a refund. Posting handles are
deterministic (UUID5 of the receivable and event), so every service write recomputes
and diffs the set inside the same database transaction, the transaction service
does the same when a linked split is edited, and `sync_all_receivables` catches up
after an import. Owned postings cannot be edited through the transaction service
(`transaction.receivable_posting`) and are never offered as link candidates. The
account must be a Receivable account in the linked splits' single currency; by
default the first BreadSched-native one in that currency is reused or "Reimbursements
Receivable" is created under Assets. A receivable without an account posts nothing
until its next change. The Dashboard sums open reporting-currency balances as
`receivables_owed`, with `receivables_attention` for disputed or overdue ones.

Reimbursement proposals (`engine.receivables.propose_reimbursements`) match an
unlinked credit to a receivable still owed something only when the credit is in an
expense account the receivable's expense used, dated on or after it, in the same
currency, and no larger than what remains; a credit that fits several receivables is
proposed only when exactly one payer's name appears in its description. Credits are
allocated oldest first, and acceptance recomputes the proposals and links only
choices still on offer. After an import and at the top of statement reconciliation,
one shared sentence (`presentation.reimbursement_notice`) points at waiting
proposals; it never links anything.

GTK's **Reimbursable Expenses** dialog, the register's **Track as Reimbursable…**,
the web **Reimbursables** page (`web/receivable_resource.py`), and `breadsched
receivables` gather input and render these results only.

### FSA accounts and claims

FSA benefit-year and claim detail belongs to the dedicated FSA Dashboard. The
shared FSA engines stay authoritative, and an FSA account placed in a Dashboard group
still contributes its benefit availability there. A Dashboard FSA figure is the
remaining election of every plan year applicable on the as-of date, never the
custodial ledger balance.

**Plan rules.** A funding year (`FsaFundingYear`, stored in the account's JSON) may
carry a `carryover_limit`, a `grace_through` date, or both; the two are not
exclusive. A grace period must end between the plan year's end and its run-out.
`engine.fsa.year_status` carries the unused election, up to the limit, into the
account's next funding year once the earlier year's run-out ends: the closed year
reports it as `carried_over` and forfeits only the rest, and the next year reports it
as `carried_in`. Nothing is carried before the run-out ends, because claims may still
use the money. A grace period extends `service_through`, so a grace-period service can
be claimed against either year and the household chooses; grace-period claims tagged
to the earlier year reduce what it can carry over.

**Dependent care.** `Account.fsa_dependent_care` marks a dependent care FSA. Its
`year_status` availability is the year's funding, at most the election, less what was
used, and it never carries in or over (`services.accounts` refuses a carryover limit
with `account.fsa.dependent_care.carryover`). A claim whose allocations are all on
dependent care FSAs takes what was paid as its responsibility when no EOB is entered,
so it never waits for an EOB, and it stays open rather than out of funds while a
funding year it draws on can still receive contributions.

**Flows.** `engine.fsa_flows.classify` gives every split on an FSA account one
`FsaFlowKind` from its sign and the other splits' accounts: positive and tagged with a
funding year is a claim's *repayment*; positive from an expense account (and no income
account) is a *provider refund*; any other positive split is *funding*; negative to an
expense account is a *direct payment*; any other negative split is a
*reimbursement*; a movement only between FSA accounts is a *transfer*.
`year_status` counts only funding dated in the plan year as `funded`, and sets
`used` to direct payments plus reimbursements less provider refunds and repayments,
each reported separately (`presentation.fsa_usage_text` words them for every
interface). The medical expense stays on its expense split, so a card charge later
paid from the bank and reimbursed by the FSA is one expense; the card payment is a
transfer and the reimbursement a balance-sheet movement. Plan infers every
non-transfer FSA flow as `BENEFIT_FUNDING`, so the benefit row is the account's net
movement and the cash bridge has no residual for FSA-paid expense.

**Claims.** An `FsaClaim` is a first-class transactional object (see
[Presentation settings and financial records](#presentation-settings-and-financial-records))
linking payments, provider refunds, and per-funding-year allocations with their
reimbursements, rejections, and repayments. `engine.fsa_claims.claim_summary`
recomputes paid, refunded, reimbursed, repaid, remaining, and status. FSA claims and
receivables share link resolution (`engine.split_links`); FSA reimbursements need no
reclassification because the money is already in the FSA asset, which is not
cash-like.

- **Payer and FSA together.** `FsaClaim.receivable` names the receivable whose payer
  covers part of the same expense; it is set only in the claim screens, never
  inferred. `claim_summary` then splits the net paid amount into the payer share (what
  is owed, or what was actually reimbursed if more), the FSA share (zero until an EOB
  responsibility is entered, then capped), and the household's share. An
  over-allocation is reported as *Needs review*; nothing is refused, and no posting
  depends on the split. Without that link, a receivable whose expense is also a claim
  payment produces a warning rather than a refusal, because such a split can be
  legitimate.
- **Proposed links.** `engine.fsa_claim_proposals.propose_claim_links` finds FSA
  splits no claim links whose flow needs a claim (direct payment, reimbursement,
  provider refund) and proposes the one claim that suggests it in that flow's role
  with an amount match, oldest first and at most one movement per claim per batch;
  anything ambiguous is left to Review. `services.claims.accept_claim_links`
  recomputes before linking, as `accept_reimbursements` does for receivables, and
  import and reconciliation surfaces report waiting proposals through
  `presentation.claim_link_notice`. `suggest_claims_for_transaction` considers a fully
  reimbursed claim only for refunds, which often arrive after the FSA has paid.
- **Paired roles.** `fsa_claims.attachment_roles` lists the roles a transaction can
  take, paired roles first. `direct_payment` links a direct FSA payment as both the
  claim's payment and its allocation's reimbursement; `direct_refund` links a provider
  refund to the FSA card as both a refund and a repayment, so the claim nets to what
  was actually used. Suggestions rank these ahead of single roles for the same
  transaction.
- **Repayments.** Splits paying money back into the allocation's FSA account are
  linked as repayments and tagged with the funding year. `year_status` takes a tagged
  positive split off `used` (reported as `repaid`) instead of treating it as payroll
  funding; only claim saves set that tag. More reimbursed than the claim allows is
  *Over-reimbursed*.
- **History and closing.** `FsaClaim.events` records EOB changes (with the previous
  and new responsibility and a note), closings, and reopenings. A save never replaces
  that history; `close_claim` and `reopen_claim` alone change it. A closed claim
  reports its unclaimed rest as `forgone`, but figures that disagree or money owed
  back keep it flagged.
- **Report and attention.** `engine.fsa_claim_report.claim_report` groups claims by
  status, account, funding year, or provider without writing, and gives each claim
  stable attention codes: `review`, `eob` (no EOB 30 days after service), `deadline`
  (money still to reimburse within 30 days of a run-out), `rejected`, `over`, and
  `reopened`. The Dashboard carries the claims needing attention as
  `claim_alerts`.

The GTK claims dialog, the web claim editor (`web/fsa_claim_resource.py`), and
`breadsched claims` only parse input and render these results.

### Statement reconciliation

A reconciliation is a persisted, account-scoped statement session, not transient UI
state. It owns an exact ending balance, statement date, checked split handles,
lifecycle status, timestamps, and an append-only lifecycle audit. Only asset and
liability posting accounts participate; income, expense, equity, roots, and
placeholders do not represent statement balances.

The shared reconciliation service derives the opening balance from previously
reconciled or frozen splits through the statement date. Non-void, unreconciled and
cleared splits through that date are candidates; Cleared candidates start checked.
The displayed difference is always statement ending balance minus the account's
natural-sign opening-plus-checked balance. GTK and web merely present this shared
calculation. Their mutations submit typed start, update, complete, cancel, and reopen
requests through the application-service boundary. Domain failures retain stable
codes and field paths while adapter-owned wording remains presentation-only.

Finishing is permitted only at an exact zero difference. It atomically changes every
checked split to Reconciled with the statement date and records completion of the
session. Cancelling records the abandoned session without changing ledger state.
Only the latest completed statement may be reopened, which atomically returns its
recorded splits to Cleared and preserves the lifecycle audit. This ordering prevents
a correction from invalidating later statement openings invisibly. Full book
verification reports missing, cross-account, duplicated, or subsequently changed
splits referenced by reconciliation history. Imported reconcile states remain
ledger facts and are included in opening balances rather than rewritten on import.

## Interoperability and source ownership

GnuCash compatibility is a core constraint rather than a one-time migration: a
household must be able to keep maintaining an existing GnuCash book while using
BreadSched's planning. Imported data therefore keeps its source identity (GUIDs)
and semantics, and structures BreadSched cannot reproduce are preserved rather than
normalized to fit a smaller editor. Importers and exporters are plugins under
`plugins/importer` and `plugins/export`, driven by one typed import service.
Compatibility claims are limited to structures demonstrated by fixture-based
round-trip evidence.

The rule that runs through this section is ownership: every fact is either
**source-owned** (refreshed from the source on re-import) or **BreadSched-owned**
(kept across re-import), and neither side silently overwrites the other.

Imported commodity identity uses the namespace and mnemonic pair; CURRENCY and
ISO4217 namespaces may share an existing currency with the same mnemonic. Source
GUIDs resolve within an import, while an ambiguous bare mnemonic does not resolve
to an arbitrary commodity for a price. Re-import reuses the matching pair without
changing the native schema.

### Ownership on re-import

BreadSched-owned planning state must not be destroyed by re-import. On a matching
GnuCash account GUID, source-owned chart fields (name, source type, parent,
commodity, code, description, source notes, placeholder/hidden state, and commodity SCU)
may refresh from the source, while the BreadSched account type, FSA funding years,
local account notes, projection-rate overrides, projection exclusion, dashboard grouping,
linked-asset/card behavior, usual payment, and payment day are retained. A source
type change is reported. If its accounting class conflicts with the retained
BreadSched type, the conflict requires review rather than silently changing local
semantics or display signs.

Account notes follow the same ownership split as transaction notes. GnuCash notes
are inspectable source provenance and refresh on re-import; BreadSched notes are
editable planning context and survive independently. Compatibility loading moves a
legacy shared note to source provenance only when the retained typed `slot:notes`
value proves that origin, avoiding a guess that could discard a local note.

That ownership boundary is enforced at edit time as well as import time. GTK and
CLI refuse changes to an imported account's source-controlled name, parent, code,
description, commodity/SCU, placeholder, and hidden state. Local planning fields
remain editable. This prevents a successful local edit from appearing durable only
to be silently replaced by the next source refresh.

The representative account-fidelity fixture is generated against the GnuCash
SQLite schema and exercises these rules together rather than only as isolated
fields: nested STOCK/MUTUAL holdings with a security commodity and non-default
precision; assets locally modeled as FSA; liabilities locally modeled as Loan;
historical money-market, receivable, and payable types; an unknown valid source
type; hidden state; typed slots; hierarchy changes; and a second source refresh.
Any expansion of imported-account editing must extend this matrix with the source
form and the expected local/source ownership result.

Imported-schedule editing follows the same rule. Any newly editable schedule shape
must first be added to the generated native/SQLite/XML fidelity matrix, including a
save/reload/source-refresh assertion. Presentation layers consume a shared
editability decision; they do not independently infer that a source definition is
safe from the number of splits or a familiar-looking recurrence label.

On a matching GnuCash transaction GUID, source-owned ledger facts (dates,
descriptions, numbers, accounts, values, quantities, memos/actions, and reconcile
state) and source transaction notes may refresh from the source. Source notes have
their own read-only field because BreadSched-authored transaction notes,
plan-resolution/link state, rejected matches, and split planning/FSA classifications
are retained across re-import. Split-level annotations are retained only when the
same source split GUID still exists, so a materially replaced source split cannot
inherit stale BreadSched state. Transactions with a split reconciled in BreadSched
follow the narrower rule in *Locally reconciled imported transactions* below.

Import reporting follows that ownership boundary. A successfully read source
transaction is **new** when its stable identity is absent, **refreshed** when any
source-owned ledger fact differs, and **unchanged** otherwise; split classifications
use the same source-owned comparison and also report removed source splits. Skipped
records retain stable identities and reasons per source path. Their history is
updated inside the import transaction, so a failed/rolled-back import cannot claim
that an issue was introduced or resolved. A later run can consequently distinguish
new, repeated, and now-resolved source problems without parsing warning prose.

Import initiation is an application-service workflow shared by CLI, GTK, and web.
The typed request owns source preflight, importer selection, explicit format
overrides, execution options, and recording the last successful source. Adapters
retain only transport parsing, background-job presentation, and result rendering;
stable preflight codes and field paths are independent of their English wording.

Import reports: `ImportResult.skip` records each rejected record's reason and
subject, and `warn` records everything else in `notices` (both still go to
`warnings`, kept whole for callers that log every message). `problems()` groups
skipped records by reason, most frequent first, as `ImportProblem` values with the
count and the first three subjects; `detail()` writes one line per reason with
those examples (each cut to 60 characters) and then only the notices, so a file
with hundreds of rejected rows no longer buries the warnings that matter under one
line per row. The CLI's JSON and the browser's import and CSV import responses
carry `problems` too, and the CLI adds `notices`.

Transaction deletion synchronization uses a separate complete-scan inventory keyed
by the stable GnuCash chart-root identity, with the canonical source path only as a
fallback. A previously observed transaction GUID that is absent from the source is
removed in the same atomic import operation, unless it is reconciled (held for
review; see *Locally reconciled imported transactions*). A skipped but still present source
record counts as observed and is never mistaken for a deletion. Reconciliation
sessions and FSA claims are durable BreadSched audit data, so a missing source
transaction referenced by either is retained and reported as a conflict instead of
creating a dangling reference. Moving or renaming the same GnuCash book does not
reset its stable inventory when its root GUID is available.

### Locally reconciled imported transactions

A statement reconciled in BreadSched is a durable local assertion about imported
ledger facts, so the refresh rule above is narrowed for it. `gen.engine.import_review`
owns the rule and the importer's shared sink applies it to SQLite and XML books:

- The reconcile state and statement date of a split reconciled locally are
  BreadSched-owned while its account, value, and quantity are unchanged. An
  unchanged refresh therefore leaves completed BreadSched statements valid and
  reopenable, even though GnuCash still reports those splits as unreconciled.
- A refresh that changes a *protected* fact is withheld. Protected facts are the
  transaction's date, description, number, and currency, and each locally
  reconciled split's account, value, quantity, memo, and action, or its removal.
  Changes confined to splits not reconciled locally, and read-only source notes,
  apply normally. The withheld version is serialized with a fingerprint of its
  source facts in the `import.reconciled_review` book metadata key, written inside
  the import transaction so it is undoable and needs no schema change.
- `gen.services.import_review` lists pending versions and applies one batch of
  keep/use-source/later decisions atomically. Keeping records the fingerprint, so
  the same source version is not raised again. Using the source version merges
  BreadSched annotations as a normal refresh does. It is refused, before anything
  is written, while a completed BreadSched statement selected a split whose
  account, value, or quantity would change or disappear. Reopening that statement
  first keeps `verify` and statement reopening consistent.
- GTK presents the batch when a book opens (before the due-schedule review) and
  after an import that held changes; web and CLI expose the same service.
- A source deletion of a transaction with any Reconciled split (reconciled in
  GnuCash or BreadSched) is held too, as a `HeldChange` with `deleted` set, the
  fixed fingerprint `deleted`, and no incoming version. Its GUID stays in the
  deletion inventory, so every later import re-evaluates it: a kept deletion is
  counted as kept and not raised again, a pending one stays pending, and a GUID
  that reappears in the source is imported normally and clears the entry. Using
  the source deletes the transaction in the review's undoable batch, refused as
  `import.review.deletion_referenced` if a reconciliation, FSA claim, or
  receivable (`import_review.deletion_references`) has come to refer to it.
  Referenced transactions are retained outright on import, and unreconciled ones
  are removed.

Transactions with no split reconciled locally keep the general rule: local edits to
source-owned facts are replaced and counted as refreshed. Transactions created in
BreadSched have no source GUID and are never listed in the deletion inventory;
they reach GnuCash only through an explicit write-back (below), so recording the
same activity in both applications without one duplicates it. The User Guide therefore asks users to choose one ledger of record
per period. SQLite acceptance tests cover restored facts for unreconciled
transactions, native transactions that never reach the source, and the reconciled
hold/keep/apply/refuse contract.

### GnuCash write-back

Write-back is explicit, previewed, and works on SQLite and XML books
(`plugins/export/gnucash_writeback.py`, `services/gnucash_writeback.py`, CLI
`breadsched gnucash-writeback`). Each GnuCash import (either format) records the
book's resolved path, the SHA-256 of the exact bytes read, and its book GUID in
the `gnucash.writeback.source` metadata, inside the import's undoable
transaction. Preview and apply both refuse when that record is missing, the file
is gone or is another book, its bytes changed since the import (import again
first), or GnuCash holds its lock (the SQLite `gnclock` table, or an XML book's
`<book>.LCK` file). Because the source is proven unchanged, every difference is a
local edit.

The plugin is four layers, each importing only those below it (an architecture
test checks this, and that no private name crosses a module): `gnucash_source`
reads either format into `SourceBook` and holds the fingerprint and `preflight`
refusals; `gnucash_writeback_plan` compares BreadSched with it
(`plan_writeback`, `WritebackChange`, `TargetTxn`); `gnucash_book_writers` holds
`write_sqlite` and `write_xml`; and `gnucash_writeback` backs up, applies,
verifies, records the result, and re-exports the public API. The importers record
the fingerprint through `gnucash_source`.

Both formats are read into one neutral view (`SourceBook`: accounts with their
commodity and SCU, currencies, and every real transaction with its splits, lots,
and reconcile state; XML template transactions are excluded). Planning compares
BreadSched with that view and produces one operation per transaction:

- **new**: a transaction not from GnuCash (not in this book's import inventory)
  whose every account maps to GnuCash (`Account.source_guid`), in a currency the
  book has, with at least two splits. Values use the currency's fraction; a split
  in the transaction currency writes quantity equal to value, and one in a
  security or foreign-currency account writes its own quantity at the account's
  SCU. BreadSched handles become the GnuCash GUIDs, so re-import recognizes them.
  A `date-posted` gdate slot is written as GnuCash does.
- **edit**: any difference in date (and its `date-posted` slot), description,
  number, split account, value, quantity, memo, action, or added or removed
  splits, unless a GnuCash split of the transaction is reconciled (`y`) or in a
  lot, when only reconcile state may change. A removed split's slots go with it.
- **reconcile state** `n`/`c`/`y`; the reconcile date is set when a split becomes
  `y`.
- **delete**: a GUID in this book's inventory that BreadSched no longer has,
  unless it was skipped on import (it was never here), reconciled in GnuCash, or in
  a lot. The transaction, its splits, and every slot they own (following nested
  frames) are removed.

A source-deleted transaction BreadSched kept, a changed currency, an account not in
GnuCash, and amounts finer than GnuCash allows are listed as unsupported with a
reason and never written. Dates use the book's own post-date style (14-digit or
`YYYY-MM-DD HH:MM:SS` in SQLite, `YYYY-MM-DD HH:MM:SS +0000` in XML) at GnuCash's
neutral 10:59:00.

GnuCash SQL books store posting and schedule dates as zone-less UTC timestamps.
GnuCash 2.6.10 and later write 10:59:00 UTC; earlier versions wrote the user's
local midnight in UTC, so a book from a household in UTC+2 holds 22:00:00 on the
previous day. `gnucash_common.parse_gnc_sql_posting_date` therefore reads a time of
day from 11:00:00 UTC on as the next calendar day and anything earlier as the same
day: GnuCash's neutral-time range (UTC-10:59 to UTC+13:00) read in reverse, wrong
only for UTC-11, UTC-12, and UTC+14. The SQLite importer uses it for transaction
post dates and schedule start and end dates, and write-back uses it to read the
book's current dates, so a comparison never reports a day's shift as a change.
Price timestamps (a quote's real time) and XML dates (which carry their own offset)
keep `parse_gnc_date`, which takes the date as written.

Applying recomputes the preview, re-reads the book, and copies it to
`<book>-gnucash-backups/<source>.<timestamp>.bak`. SQLite runs every chosen
statement in one `BEGIN IMMEDIATE` transaction (a changed row count aborts). XML
is edited as text: each chosen transaction's `gnc:transaction` block (found by
GUID, outside `gnc:template-transactions`) is parsed, changed in place so unknown
elements and slots survive, and serialized in GnuCash's own two-space layout with
the book's own line endings (a CRLF book stays CRLF); new
blocks follow the last transaction; deletions remove the block; the transaction
`gnc:count-data` is adjusted; the file is written to a temporary file (gzipped if
it was) and swapped in with `os.replace`. Every other byte is unchanged. The book
is then read back: each written transaction must equal the planned one
(`writeback.verify.failed`), and read with the importer's mapping must equal
BreadSched's (`writeback.roundtrip.mismatch`); either failure restores the backup.
It deliberately does not re-import, because an import would restore GnuCash's
values over local edits that were not chosen. Instead it records the new
fingerprint and updates the import inventory (new transactions join it, deleted
ones leave it). Backups beyond `gnucash.writeback.keep_backups` (default 10,
1-1000) are removed oldest first after a successful write.

`tests/test_gnucash_writeback_real.py` runs the full set of edits on SQLite and XML
books that GnuCash 5.5 created (`tests/fixtures/gnucash/`, regenerated with
`make_book.py`), proves a fresh import reproduces the edited book and that an XML
write leaves untouched bytes identical, and opens the written book in real GnuCash
through `scripts/gnucash_book_report.py` to compare every transaction and account
balance. The CI job **GnuCash write-back in real GnuCash** installs
`python3-gnucash` and requires that check (`BREADSCHED_REQUIRE_GNUCASH`); elsewhere
it runs when the bindings are available and is skipped otherwise.

GTK's `GnuCashWritebackDialog` (File → Write Changes to GnuCash…) and the web
Import page's **Write changes to GnuCash** panel present the same preview: one
unticked checkbox per writable transaction with its detail lines, the "Not
written" list with reasons, and the backup-retention setting. Both write only the
ticked handles through `apply_writeback`; the web adapter
(`web/gnucash_writeback_resource.py`: `GET /api/gnucash/writeback`,
`POST /api/gnucash/writeback`, `POST /api/gnucash/writeback/settings`) only
parses JSON, and a route test proves rejected requests change neither book.

### Statement formats

External amount text is parsed at the boundary that knows its format. Core `Money`
construction accepts only unambiguous numeric text; importers and user interfaces
must not silently reinterpret locale punctuation. QIF/OFX import examines the full
source for a consistent decimal convention before parsing records. QIF date order is
likewise inferred once from file-wide evidence (month-first or day-first), never guessed
record by record. Conflicting conventions are reported, and ambiguous files may use an
explicit importer format override. Year-first QIF dates remain inherently unambiguous.

OFX and QIF statements own only their source account's side of a transaction.
`ImportSink.keep_local_categories` gives an existing transaction's counterpart
splits, including their split identities, to the refreshed record, so a
recategorized or split counterpart survives re-import. A single counterpart takes
the corrected statement amount. Several counterparts under a changed amount are
left unchanged with a warning rather than re-apportioned. GnuCash imports keep full
source ownership of every split.

A new OFX bank or card row is held back as a possible duplicate
(`ImportResult.possible_duplicates`, also a skipped record) when
`plugins/importer/duplicates.DuplicateGuard` finds an unclaimed transaction in the
source account on the same date with the same split value. The guard indexes the
account once, leaves out every handle this statement produces (computed by
`ofx._row_handle` in a pre-pass, so identical rows and re-imports never match
themselves), and lets each existing transaction claim at most one row. A row whose
handle already exists is a re-import and is never checked. `include_duplicates`
(import service, CLI, GTK, browser) turns the guard off. QIF bank, cash, and card
rows get the same guard. A QIF handle depends on the categories the row resolves
to, so there is no pre-pass: the import loop writes rows whose handle already
exists, collects every handle the file produces and defers the new rows, then
builds one guard per source account that leaves out the whole family before
deciding the deferred rows. The file's own rows therefore never match each other,
in whatever order they appear. QIF investment records are not checked.

CSV statements (`plugins/importer/csv_import.py`) are read through an explicit
column mapping. Encoding, delimiter, date order, and decimal convention use the
same whole-file evidence rule, and an all-ambiguous date column is refused rather
than assumed. Line endings are normalized after decoding, so a Windows export and a
Unix copy of one statement yield the same text and the same row identities. `read_statement` classifies rows without writing: new, already
imported, possible duplicate, or invalid. `import_rows` writes exactly those
classified rows in one batch transaction. A row's identity is a UUID5 of the target
account, date, amount, description, memo, and occurrence number. Re-importing an
existing identity leaves that transaction untouched rather than routing it through
the refreshing `ImportSink.transaction` path, so a category chosen after import is
never reverted. A possible duplicate is another transaction with a split in the
target account on the same date and value, outside the row's own identity family.

Optional `category`, `payee`, and `currency` columns resolve against the book
without inventing anything (`_Resolver`). A category matches a non-placeholder
account other than the target by full name, else by a name exactly one such
account has; no match or several make the row invalid with that reason, and an
empty cell keeps the Uncategorized CSV placeholder. A currency code other than the
target account's currency (or the reporting currency for an account without one)
makes the row invalid. A payee resolves by name or `payees.match_key` through
`payee_index`; an unknown payee is a non-blocking `CsvRow.note` and the row
imports without one. The resolved category replaces the placeholder counter split,
the payee is passed to `ImportSink.transaction(payee=...)` (set only on a new
transaction), and a row with a category is never offered as a possible transfer.
These cells are not part of the row identity, so an accepted row is never
re-categorized by re-import.
Split columns (`CsvMapping.splits`, pairs of category and amount columns) replace
the single category column; mapping both, or a pair missing one column, is the
`import.csv.split.mapping` error. Per row, `_row_splits` skips a pair with both
cells empty, refuses a half-filled pair, resolves each category through the same
`_Resolver.category`, parses the amount with the file's detected number format
(split amount cells count as evidence for it), applies `invert` to it as to the row,
and drops a zero amount. The filled amounts must sum exactly to the row's amount;
otherwise the row is invalid with both totals in its reason, and no balancing split
is added. `import_rows` posts the target leg and one counter leg per split (negated),
so the transaction balances by construction. A split row is never offered as a
possible transfer, and its splits are not part of the row identity.
A possible transfer is a remaining new row whose value equals the placeholder split
of a two-split, reporting-currency transaction elsewhere: one split in another
asset or liability account, the other in an import placeholder (`placeholder_handles()`:
Uncategorized CSV or Uncategorized OFX), dated within `TRANSFER_WINDOW_DAYS` (3).
Rows and sides are paired one to one, nearest date first. With `link_transfers`,
`import_rows` replaces that placeholder split with a split in the target account
whose handle is a UUID5 of the row identity, inside the same batch; preview treats a
target-account split with that handle as already imported. Without it the row is
imported as new. Categorized transactions are never candidates, so an accepted
category is never rewritten. `ImportResult.transactions_linked` counts links.
The shared service in `gen/services/csv_import.py` validates the file and account
before any read. `inspect_csv` reports only the detected encoding, delimiter,
columns, and first rows for choosing a mapping. The web adapter
`web/csv_import_resource.py` parses JSON into a `CsvImportRequest` and translates
results; `/api/import/csv/inspect`, `/api/import/csv/preview`, and
`/api/import/csv` never validate or write outside the service. A browser upload of a
`.csv` file is staged under the book's uploads directory and returns its path
without importing, because a CSV needs a mapping first. The GTK
`CsvImportDialog` (File → Import CSV Statement…) calls the same three service
functions and suggests columns from header names as the web view does.

Imported quotes pass the same contract as manual ones (`plugins/importer/quotes.py`).

- **GnuCash** prices come from the book's price database.
- **QIF** `!Type:Prices` lines (`"SYMBOL",price,"date"`) are in the reporting
  currency, since QIF names none.
- **OFX** takes security-list `SECINFO` prices (dated `DTASOF`) and position
  `INVPOS` prices (dated `DTPRICEASOF`). A position's `UNIQUEID` is mapped to a
  ticker through the security list, and the quote currency is `CURSYM` or the
  statement's `CURDEF`. An investment statement without a bank account still
  imports its prices.

A quote must name a security already in the book by its unique symbol (the
importer never invents a security), be in a currency the book knows, and be
positive; anything else is reported as a skipped price. It is stored with source
`qif` or `ofx` under a handle derived from source, security, currency, and date.
Re-importing therefore updates it in place, and it never replaces a quote entered
in BreadSched (source `breadsched`).

Investment accounts from OFX and QIF share `plugins/importer/brokerage.py`: a
brokerage becomes an Assets child with a `Cash` bank sub-account and one `MUTUAL`
or `STOCK` sub-account per traded security, all under stable uuid5 handles from
the caller's format. A security is matched by ticker to the one book security with
that symbol, else created (fraction 10000) in a namespace from its kind (`FUND`,
`STOCK`, `BOND`, or `SECURITY`).

An OFX file with an `INVACCTFROM` brokerage account is imported by
`plugins/importer/ofx_investment.py`; the account is named for the institution and
account tail, and securities come from the statement's security list. Records map to balanced
transactions dated `DTTRADE` (`DTPOSTED` for cash):

- `BUY*`/`SELL*` (not options): cash `TOTAL`; commission, fees, load, and taxes
  to `Expenses:Investment Fees`; the security split takes the units as quantity
  and the balancing value (`-TOTAL - costs`), so the statement's rounding is kept
  and no cost basis or realized gain is invented. Unsigned sold units are negated;
  a missing `TOTAL` is `-(units × price) - costs`.
- `REINVEST`: `Income:Investment Income` for `-|TOTAL|` against the security
  (and any costs); cash is untouched.
- `INCOME`/`INVEXPENSE`: cash against `Investment Income`/`Investment Fees`.
- `INVBANKTRAN`: cash against `Uncategorized OFX`, as in a bank statement.
- Options, `TRANSFER`, `SPLIT`, journals, return of capital, and margin interest
  are skipped with a per-kind reason.

Transaction handles use the same `(account id, FITID)` identity as bank records,
and re-import goes through `keep_local_categories` with the cash account as the
statement side, so a recategorized income or fee split survives. Security prices
in the same file are recorded afterwards, so newly created securities are priced.

A QIF `!Account` of type `Invst`, `Port`, or `401(k)/403(b)` (or a `!Type:Invst`
section) is a brokerage named for the account (`plugins/importer/qif_investment.py`).
Investment records name securities by full name (`Y`); a `!Type:Security` list,
read in a pre-pass wherever it appears, maps the name to a ticker (`S`) and kind
(`T`: mutual fund types become `MUTUAL`/`FUND`). Actions follow Quicken's
magnitudes: `Buy` (cash `-T`, commission `O` to Investment Fees, security
`T - O`), `Sell` (cash `T`, security `-(T + O)`, units negated), `Reinv*` (the `L`
category or Investment Income funds the units), `Div`/`IntInc`/`CG*`/`MiscInc`
(cash against the `L` category or Investment Income), `MiscExp` (against the `L`
category or Investment Fees), `XIn`/`XOut`/`Cash` (against the `L` account or
category). An `X` suffix moves cash through the `L` account instead of the
brokerage's cash, and a bank register's `[Brokerage]` transfer resolves to that
cash account rather than creating a bank account of the same name. Share
transfers, splits, options, grants, and reminders are skipped with their action
name. Handles follow the QIF content identity with an occurrence counter, and
re-import uses `keep_local_categories` against the brokerage cash.

A multi-account QIF export writes each transfer in both registers. Within
one file, a plain (unsplit) `[Account]` bank record is paired with its mirror: the
same date, the opposite amount, and the two account names swapped. Investment
records win: a pre-pass records the bank-register key each `XIn`/`XOut` or
`X`-suffixed action implies (money entering the brokerage left the other account),
and a matching bank record is dropped. Between two bank registers the first record
in file order is imported and its mirror dropped; each pairing consumes one match,
so repeated equal transfers pair one-to-one and unmatched records import as
before. Dropped copies are counted in `ImportResult.transfers_paired` and reported
in the import detail. Because the first side's identity is unchanged, re-import is
stable.

An OFX bank or card transaction may carry its own exchange rate. `CURRATE` is the
number of statement-currency (`CURDEF`) units per unit of `CURSYM`, so it is
stored as a `CURSYM` quote priced in `CURDEF`, dated `DTPOSTED`, with source `ofx`
and GnuCash price type `transaction`, exactly like a manual exchange rate. Its
handle depends on the two currencies and the date, so one rate per currency pair
and day is kept (the last in the file) and re-import refreshes it. Under
`CURRENCY` the amounts are in `CURSYM` and are converted to the account currency
at that rate, rounded to the currency's fraction; under `ORIGCURRENCY` they are
already in `CURDEF` and only the quote is recorded. A `CURRENCY` transaction
without a positive rate or currency code is skipped rather than posted as if it
were in `CURDEF`. The foreign currency commodity is created if the book lacks it.

### Exchange rates entered by hand

The shared manual FX quote write accepts exact source and target currency handles,
a date, and a positive target-units-per-source-unit rate. One BreadSched-owned
quote per pair and date is updated in a database transaction, never by overwriting
an imported quote. The existing as-of read prefers a same-day manual quote, then
the imported quote if the manual entry is undone. CLI rate entry resolves an exact
currency handle or a unique currency mnemonic and reports an exact rational rate;
ambiguous codes require a handle. The web write adapter accepts currency handles,
textual exact rates, and an ISO date, then delegates validation and the complete
transaction to the shared valuation operation. The Accounts control refreshes
existing quote evidence and missing-quote displays after saving. The GTK Accounts
**Exchange rate…** dialog is a presentation adapter over the same operation: it
parses the rate with the shared locale-aware amount parser, shows the latest direct
quote for the selected pair, and surfaces the operation's validation message without
writing when the pair, rate, or date is invalid.

### Format choices, uploads, and dates

Importers infer date and number conventions from whole-file evidence where possible
rather than guessing per record. When evidence is ambiguous, both reference GTK4
and parity web workflows expose explicit overrides and pass those choices into the same
importer implementation. The web view accepts either a local path visible to the
BreadSched process or an authenticated, bounded browser upload. Uploaded files use
a stable per-book path keyed by the browser filename in a sibling `<book>.uploads`
directory, preserving source ownership on repeated uploads. The transport enforces
the loopback token, Host, Origin, content type, and body limit; the existing typed
import service still owns importer selection and parsing. A failed import restores
the prior uploaded file so its remembered source remains usable.

### Import date integrity

Required source dates are never synthesized. Missing or malformed posting/start dates are reported against the source record and skipped rather than silently using the current date. Optional dates remain optional.

### Direct bank connections

- **Reach.** AqBanking's strength is FinTS/HBCI and EBICS (Germany and nearby
  countries). Its US/Canada route, OFX Direct Connect, is being withdrawn by major
  banks (Bank of America ended OFX in September 2025; Chase has dropped Direct
  Connect) in favor of aggregators, so it would not serve the main US household
  case.
- **Cost.** It is a C library (GPL-2/GPL-3, compatible with BreadSched's
  AGPL-3.0-or-later) with Gwenhywfar underneath and no maintained Python binding.
  An adapter would drive `aqbanking-cli`, and bundling it would add both
  libraries and their configuration to the Windows installer and the Flatpak.
- **Credentials.** PINs, TANs, and bank setup would stay in AqBanking's own
  configuration and prompts, so BreadSched would add a second place where banking
  secrets live without owning their safety.

The supported route is therefore the existing reviewed CSV import: the user runs
`aqbanking-cli request` and `aqbanking-cli export --exporter=csv --profile=default`
and imports the file with the mapping the User Guide gives. The acceptance test
`test_an_aqbanking_cli_export_imports_with_the_documented_mapping` imports unedited
aqbanking-cli 6.5.4 output (`tests/fixtures/aqbanking/`) and checks that a
repeated download adds nothing. Revisit a built-in adapter only with evidence of
users whose banks AqBanking serves and who cannot use this route.

## User interfaces

### GTK window, toolbar, and tabs

The main window has no sidebar, which duplicated the View menu and took width from
every view. The toolbar is arranged around the current view: actions that work
anywhere (`viewmanager.TOOLBAR`: open or import a book, undo, redo, new
transaction, print), the current view's name and its own command icons
(`view_tools`), one toggle per other view (`viewmanager.CATEGORIES`), and the book's
account and transaction counts. The toolbar scrolls horizontally rather than setting
the window's minimum width. The View menu and the view icons both target the
stateful `win.show-category` action, and `show_category()` is the single navigation
entry point, so the active icon and menu item always agree however a view was
reached.

View commands are declared once in `viewmanager.VIEW_ACTIONS`. Each becomes a
`win.<view>-<name>` action, is listed in **Actions**, and appears in the toolbar
only while its view is current. **Actions** lists the current view's commands
first, then the commands that work anywhere, then every other view's commands under
**Other Views**, so each command has exactly one item. The menu model is rebuilt only
when the view changes: opening a submenu moves focus into its popover, and
rebuilding the same view's model then destroyed the open submenu and crashed GTK.
Buttons stay in a view only where their state depends on it (Review due, Save as
scenario, Reconcile) or where they act on a table's selected row.

Below the toolbar, a tab bar lists each open view, one tab per open register, and
one per pinned scenario Projection (`ViewManager._tabs`). Register tabs are
`RegisterView`s in their own stack, and `_views["register"]` always names the one
shown, so callers that address "the register" keep working. A pinned
`ProjectionView` keeps its scenario and only recalculates when inherited Base
assumptions change, while the unpinned Projection and Plan follow the selected
scenario. Closing a register with unsaved typing asks first; opening another book
closes every tab, because tabs name the old book's accounts. Each book's tabs are
remembered in `views.ini` (section `open-tabs`, keyed by a digest of the book path)
and restored after the Dashboard opens, skipping deleted accounts and scenarios. The
browser has no tab bar: `openInNewTab` opens a browser tab with the view and account
or scenario in its query string.

### Tables, dialogs, and bounded sizes

Every table is built with `_base.table_section()`: a heading row with the table's
own column chooser (never in the view toolbar, where identical icons could not be
told apart) above a scrolled `Gtk.ColumnView` carrying the `data-table` class, which
stripes rows so they are distinguishable at rest. Numeric cells carry `numeric` for
tabular figures and a small right padding. Tables never scroll sideways: a narrowing
window gives each column between its minimum and natural width, text cells ellipsize,
and amount cells never do, so figures are never truncated. The appearance corrects
concrete overlap and legibility problems; reproducing GnuCash's look is not a goal.

Windows must stay usable on small screens whatever the book holds:

- Dialogs keep minimum sizes within 800 × 600 even with very long names, 150 payees,
  and 40-split transactions (`TestDialogsFitTheScreen`); long forms move their body
  into a scroller (`widgets.bounded.scroll_body`) and keep the button row outside.
- Views keep within a 1024 × 700 work area (`TestBoundedSizes`). Drop-downs come
  from `widgets.choice.bounded_dropdown`, whose button ellipsizes in the middle at 36
  characters (GTK's default made the longest account path the minimum width), view
  toolbars scroll, Dashboard cards wrap in a `Gtk.FlowBox`, and the main stacks are
  not homogeneous, so the window's minimum is the shown page's.
- Every secondary window is a `widgets.bounded.BoundedWindow`, which opens at its
  natural size capped to 90% of the monitor, because a wrapping label's natural width
  is its whole text on one line.
- Dashboard sections are separate cards sized to their content up to 900 × 420
  pixels, beyond which the table scrolls inside its card.

### Registers and entry

The register lists `ledger.register` rows in date order and opens scrolled to the
end like a check register, without selecting or expanding a row. Register windows
are independent consumers of the one open `DbSQLite` connection: each owns its
account, filter, selection, and expansion, while database signals refresh all of
them after a commit. The main window detaches secondary windows before the book
closes, so no callback reaches a replaced database. Register column headings are a
shared engine mapping, so GTK and web name the same ledger directions.

Entry autocomplete comes from one service, `services/autocomplete.suggest_entry`,
so GTK and web never infer different templates. A description matches on its
normalized key (`payees.match_key`) or a chosen payee matches exactly; candidates
must use the entry's account and currency and only visible, postable accounts, and
the latest wins. The proposal carries accounts, values, and memos only (never
reconcile state, source identity, notes, planning purpose, investment activity, or
claims), fills only fields the user has not touched, names its source, and never
writes. The web route is `GET /api/entry/suggest`.

#### Blank entry row

GTK and web registers take new entries in a blank row at the bottom, modelled on
GnuCash's interaction rather than its appearance. The row is a narrow adapter to
the shared transaction service, not a parallel transaction model:

- **Model.** The last row is a sentinel payload (`gui/views/blank_entry.py`), not a
  database object, kept after the sorted transactions in a flattened list model, so
  it stays last under any sort or filter and never enters balances or exports. Its
  entry widgets are created once per register and moved into cells, so typed values
  survive repaints.
- **Cells and keys.** Date (defaulting to the last date entered), Num, Description,
  Payee, Transfer (visible, postable accounts), and Increase/Decrease entries titled
  with the account's headings. Tab moves in column order, Enter commits, Escape
  resets, and typing never changes the selection.
- **Commit.** Two balancing `TransactionSplitInput`s with a positive exact amount
  (direction from the cell typed in), or one per split line, go through
  `save_transaction` as one atomic change and one undo step. Fewer than two splits,
  an imbalance, an incomplete line, or no split in the register's account is refused
  with the typed values kept and focus on the offending field.
- **Split lines.** The Split toggle expands the row into lines with a memo, account,
  and amount each, a trailing empty line, and a running imbalance. Collapsing back
  is refused while more than two lines hold a split.
- **Editing in place.** F2 loads the selected transaction into an edit-mode row: a
  two-split transaction edits as one row, any other shape as split lines that record
  their stored split handles. Saving passes `existing_handle` and `source`, so
  handles, planning purposes, investment activity, and notes the row does not show
  are kept; an emptied line removes its split. Autocomplete never runs over a stored
  transaction.
- **Leaving.** Switching accounts, opening another transaction, or closing a
  register window with unsaved typing asks Save / Discard / Cancel first.
- **Web.** The browser register has the same row, split lines, and in-place editing
  through `POST /api/register/entry`. It keeps amounts as BigInt micro-units and
  posts exact `[numerator, denominator]` pairs, so a comma-decimal browser cannot be
  misread, and keeps drafts across re-renders.

The full editor remains the place for complex metadata; the row's editor icon hands
its contents to `TransactionDialog.prefill`.

### Web tables

The web `table()` helper accepts `<tr>` elements or arrays of cell values; an array
row becomes one `<tr>` whose cells follow the header's numeric alignment. Views
therefore cannot leak loose text into a `<tbody>`. `tests/test_web_browser.py`
renders the Dashboard in headless Chromium, where available, to check real rows and
formatted group totals.

### Packaged guide

The guide is split by interface so that desktop menus, browser pages, and
command options are not mixed in one text, while every interface still shows all
four parts. `breadsched.user_guide` (no GTK import) owns the part list, reads each
part from the installed package, forms GitHub-style heading anchors
(`heading_slug`), and resolves relative Markdown links by file name
(`resolve_link`), so the links between files work the same in the repository, the
desktop, and the browser. Interface parts carry steps and link to the overview for
rules; the overview links to each interface's steps. `tests/test_user_guide.py`
fails on any link to a missing part or heading.

- GTK **Help → User Guide** (`gui/user_guide.py`) presents the parts in a bounded,
  scrollable native window with a linked toggle per part. Links are text tags; a
  click follows an internal link to its part and heading mark, or opens an external
  one in the default browser.
- The browser's **Guide** page reads `GET /api/guide?part=` (`web/guide_resource.py`)
  and renders the guide's small Markdown subset as DOM nodes, never as HTML text.
- Contextual help: `user_guide.HELP_TOPICS` maps each workflow topic to a heading
  present in both the desktop and browser parts, and `help_target(topic, interface)`
  gives the part and anchor. GTK dialogs place `widgets.help.help_row(topic)` at
  their top; its button calls the application's `show_guide(topic)`, which reuses
  the guide window. Browser views (`VIEW_HELP` in `app.js`) and sections
  (`helpHeading` in `core.js`) open `?view=Guide&help=<topic>` in a new tab, which
  reads `GET /api/guide?topic=` for the browser part and its `anchor`, so an open
  dialog or form keeps its contents. Tests require every heading to exist in both
  parts and every topic used by either interface to be in the table.
- `breadsched guide [overview|desktop|web|cli]` prints a part; `--list` names them.

The Markdown files remain the only content source: each surface performs a
deliberately conservative presentation transform instead of maintaining a second
embedded copy or requiring network access or a Markdown-rendering runtime
dependency.

### Synthetic sample book

The synthetic learning book is generated on request by `gen.sample_book` into a
new path; CLI is only its entry point. Its reference date anchors the previous
month's balanced ledger transactions and next month's recurring Plan examples.
It uses the normal schema, account, schedule, scenario, and Dashboard configuration
APIs, so it does not require a special database format or contaminate a real book.
Random object handles do not affect the reproducible account names, dates, amounts,
or financial results. Existing paths are refused before opening.

## Security

Financial books are sensitive local data. Import content, schedule formulas, and
web requests are untrusted inputs. The loopback web interface must defend against
browser-origin attacks and DNS rebinding rather than assuming loopback binding alone
is sufficient.

The local web server therefore uses defense in depth: it binds only to loopback,
rejects non-loopback ``Host`` values, rejects foreign ``Origin`` values, requires
``application/json`` for ordinary writes (or bounded ``application/octet-stream``
for the dedicated import upload), and requires an unguessable token generated for
each server process on every API request. The launcher supplies that token in the
fragment of the initial local URL (so it is never sent as part of the HTTP request);
the page moves it into ``X-BreadSched-Token`` request headers and removes it from the
visible URL. Static assets do not need the token, but they still require a trusted
Host.

Security hardening belongs in normal acceptance criteria, including transport-level
regression tests for rejected foreign origins/hosts, missing tokens, and invalid
write content types.

## Validation, packaging, and releases

### Independent financial acceptance books

Plan and Projection also have a small acceptance corpus independent of their unit
fixtures. Each golden book is a human-readable declaration with stable account,
transaction, schedule, and scenario identifiers. Its financial assumptions and
hand calculations live beside static expected Plan and Projection results; product
code never generates those expectations.

The tests materialize each declaration as native SQLite, close the writer, reopen the
book, and only then call the shared Plan service and Projection engine. The corpus
covers exact dated cash timing and matched actual retention, classified balance-sheet
flows, mortgage/escrow non-additivity, actual/365 accrual and stock conservation, and
scenario schedule/rate overlays without mutating Base. Large captured user books are
not golden fixtures: compatibility imports, formula schedules, multi-currency,
historical-estimator thresholds, and presentation rendering keep their focused test
ownership.

### Mutation testing

A separate Linux CI job mutation-tests only reporting-currency selection in
`gen/engine/currency.py` and commodity-tagged arithmetic in `gen/lib/amount.py`.
It copies source and selected tests into a fresh temporary workspace before each
run. The measured baseline is a score over tested, non-equivalent mutants, with
untested, skipped, timed-out, or suspicious outcomes failing the gate. This focused
contract keeps mutation testing independent of the platform matrix and ordinary
core test runtime. The measured selection and one reviewed equivalent case are
recorded in `docs/quality/mutation-baseline.md`.

### Performance and property tests

Performance regressions are guarded at the operation boundary rather than by
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

Money has the same layer (`tests/test_money_properties.py`): arithmetic agrees with
exact rational arithmetic, quantizing and `to_decimal` both round half away from
zero at any denominator, `allocate` never loses or invents a minor unit and keeps
shares within one unit of each other, and the GnuCash pair and text forms read back
what they wrote. `tests/test_import_fuzz.py` feeds generated QIF, OFX, and CSV
statements built from plausible fragments, truncations, and noise to the importers:
an import must finish or refuse with a ValueError (the CSV service with a failed
result), write nothing when it refuses, leave a book that `verify_book()` accepts,
and add nothing when the same file is imported again. Both run in the ordinary
suite with bounded example counts.

### GTK runtime availability

GUI tests distinguish an unavailable GTK4 runtime from a code failure. Both missing
PyGObject (`ImportError`) and an installed PyGObject without the GTK4 typelib
(`ValueError` from `gi.require_version`) skip the GTK module cleanly; once GTK4 is
available, runtime/widget failures remain real test failures. CI separately installs
PyGObject while deliberately removing the GTK4 typelib, then runs launcher help,
version, and real-launch checks so a partial system installation cannot turn test
collection or startup into a traceback.

### Packaging

BreadSched requires Python 3.11 or later (`requires-python`), and CI tests 3.11
through 3.14 on Linux, macOS, and Windows; the Flatpak and Windows installer
bundle their own interpreter. Python 3.10 support was dropped in 0.2.0a233
(alpha software, and 3.10 reaches end of life in October 2026), which also removed
the `tomli` fallback.

**Flatpak.** The manifest builds the application into `/app` with the GNOME 49 SDK
and grants Documents access (the default book location) and network access (the
loopback web interface). Desktop integration files under `data/` are named by the
application id: a desktop entry launching `breadsched-gtk`, AppStream metainfo, and
a scalable icon that the GTK application also sets as its window icon. CI builds the
Flatpak from the checkout, installs it from a local repository, validates the
desktop files with `desktop-file-validate` and `appstreamcli`, and, with the network
unshared, runs the CLI (Dashboard, CSV export, backup, restore, imported-book
integrity, competing-writer lock) and `scripts/flatpak_gtk_smoke.py` (every view,
every guide part, the icon, settings under `~/.var/app/<id>/config`, and each
printable report through `Gtk.PrintOperation` to PDF).

Portals are exercised for real. `scripts/flatpak_portal_check.sh` starts the
document portal, grants the installed app an existing schema 6 book and a book that
does not exist yet (as Open and Save do), and migrates, writes, and verifies them
through `/run/user/<uid>/doc/<id>/<name>`. `scripts/flatpak_desktop_checks.sh` puts
the real `xdg-desktop-portal` frontend in front of `scripts/portal_test_backend.py`,
a backend that answers FileChooser and Print requests as a person would, and runs
`scripts/flatpak_desktop_checks.py` in the sandbox: Open, Export Transactions,
Back Up Book, and Print on every printable view go through the application's own
actions and the portal, and the host checks the caller's app id, where each file
landed, and that each report arrived as a PDF.

A document-portal directory holds only the chosen file: a file created beside it is
kept by the portal as a hidden temporary and never appears under its own name. So
`user_paths.companion_path` sends every file that belongs beside a book or chosen
file (pre-migration and pre-restore backups, the web upload folder, import logs)
to `<data directory>/beside-documents/<document id>/` when
`user_paths.portal_document_id` recognises a portal path, and
`attachments.attachment_folder` has no default for such a book. SQLite's own
`-journal` and the writer lock still live beside the book as portal temporaries,
which is sufficient while the portal runs. `presentation.book_open_notice` tells
the desktop user where backups go. The portal's print dialog cannot show an
application tab, so `printing.print_report` asks about a report's optional section
first when `uses_print_portal()` (Flatpak, or `GDK_DEBUG=portals`).

The release workflow builds the same manifest from the tested commit into a
single-file bundle whose runtime comes from Flathub, installs it, checks the
installed version, and publishes it with its checksum in `SHA256SUMS`.

**Windows installer** (`packaging/windows/`). The installer carries its own runtime
rather than asking users to assemble Python and GTK: `build-installer.sh` installs
the wheel into an MSYS2 UCRT64 prefix beside Python, GTK 4, PyGObject, and cairo,
stages that prefix under `runtime\` without development files, adds launchers and
the icon, and compiles `breadsched.nsi`, naming the x86-unicode NSIS plugin directory
(nsDialogs, nsExec) with `!addplugindir`. MSYS2's NSIS 3.13 ships no plugins, so CI
and release install the official NSIS build with Chocolatey, and the script compiles
with that release's own `makensis.exe` so the stubs and plugins match (mixing
MSYS2's makensis with the official plugins produced an installer that hung in a
silent upgrade). MSYS2's makensis is used only with plugins of its own; otherwise
the script stops before compiling. Both installer jobs time out after 30 minutes.

- It installs per user (no administrator rights) under
  `%LOCALAPPDATA%\Programs\BreadSched` and registers under HKCU. Books never live
  in the installation directory, so neither upgrade nor uninstall touches them.
- An upgrade replaces `runtime\` wholesale, so no stale module survives. GLib starts
  `runtime\bin\gdbus.exe` as a session bus that outlives the application, so the
  installer and uninstaller first run `stop-helpers.ps1`, which stops only the
  `gdbus.exe` inside this installation.
- Adding the command line to `PATH` is opt-in (`/ADDTOPATH`), remembered under
  `HKCU\Software\BreadSched`, and adds only the installation directory, so the
  bundled `python.exe` and DLLs never shadow other programs. NSIS truncates long
  strings and expands `%VARIABLES%`, so `packaging/windows/user_path.py` edits the
  raw registry value with its original type and broadcasts `WM_SETTINGCHANGE`.
- CI (`test-installer.ps1`, outside MSYS2 with a bare `PATH`) upgrades from the
  newest published installer after checking its `SHA256SUMS` entry, then installs
  silently, runs the CLI, sample book, verification, guide, GTK smoke, and
  `scripts/windows_desktop_checks.py` (the native Open and Save dialogs, and PDF
  output of every printable view through `printing.export_pdf`), reinstalls, tests
  the `PATH` option, and uninstalls, requiring the book to remain and the runtime
  and registration to be gone.
- The release's `windows-installer` job builds and tests from the tested `main`
  commit and hands only the installer and a one-line checksum file to the
  publisher, which never executes them.

### Releases

A release is selected explicitly by a checked-in `docs/releases/vVERSION.md`; an
alpha version increment alone is not a release request, and a merge without new
notes publishes nothing. After the full CI push run on `main` succeeds, a read-only
preparation job requires that exact tested commit still to be the tip of `main`,
validates the notes against the application and schema constants
(`versioning.version_summary()`, never a hard-coded window), builds and installs the
wheel, and computes SHA-256 checksums. A separate write-capable job checks the
tested identity again, validates the transferred artifact names and hashes as data
without running them, and creates the annotated tag and GitHub release; PEP 440
alphas also carry GitHub's pre-release flag. An existing tag must target the same
commit, and a version already tagged on an earlier commit selects nothing rather
than failing. Every ordinary CI job has a read-only repository token.

### Documentation

Each document has one job: `README.md` orients and links; this document describes
the implemented design; the packaged User Guide (`src/breadsched/USER_GUIDE.md` for
rules, `src/breadsched/guide/` for each interface's steps) is the offline help;
`ROADMAP.md` is the only list of unfinished work; `CHANGELOG.md` records completed
changes concisely; tests, `docs/quality/`, and `docs/releases/` hold detailed
evidence. `CONTRIBUTING.md` defines how each pull request reviews these documents.
`tests/test_documentation.py` checks local links and anchors in the top-level
documents, and `tests/test_user_guide.py` checks the guide's links.
