# Architecture and ownership

Part of the [BreadSched design](../../DESIGN.md).

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
| Online quote sources (network, Finance::Quote bridge) | `plugins/quotes`, called through `gen/services/quotes` | fetch from the network in an engine or service, or store prices themselves |
| Wording of service errors and shared report sentences | `presentation/` (by area: `messages`, `notices`, `benefits`, `planning`, `investments`; the package re-exports every name) and its gettext catalog | invent their own financial wording |
| GTK windows, dialogs, printing, background jobs | `gui/` | touch widgets from a worker thread |
| HTTP routes, query parsing, response shape | `web/resources.py` and the `web/*_resource.py` adapters | perform validation or writes the service owns |
| HTTP authentication, framing, limits, static files | `web/transport.py` | import financial engines |
| Command-line parsing and text/JSON output | `cli/*_commands.py`, dispatched by `cli/main.py` | keep its own copy of a financial rule |
| Report layout shared by GTK printing and the browser | `plugins/export/report_layout.py` | recompute values from the view |

`gen/` is standard-library only. Adapters and presentations may use a third-party
package once a decision record justifies it, and network-facing ones are optional
extras ([decision 0002](decisions/0002-dependencies-and-online-quotes.md)).
`tests/test_architecture.py` names what each layer may import.

Background work: a Projection or other long calculation runs in a worker with its
own read-only snapshot and hands an immutable result back to the GTK main loop;
imports run on the single writable connection under a modal workflow (see
[Background work](#background-work)).

## Application services

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

## Web adapters and transport

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
and securities, Plan, Plan detail, Expense Explorer, Dashboard, Projection,
scenarios, schedules and due review, loans, Review, registers, reconciliation, FSA,
claims, receivables, goals, payroll, rules, tags and documents, imports, write-back,
net worth, holdings and gains, tax year, budget jars, guide) is a thin adapter.
Controls several adapters share have one parser: ``web.controls`` turns a browser
amount into `Money` and a `ServiceError` into a resource error with
presentation-owned wording, and ``web.schedule_controls`` parses the recurrence,
exception, and split controls of the baseline and scenario schedule
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
[Journaling and read snapshots](storage.md#journaling-and-read-snapshots)) without holding the
global request lock; closing it neither
acquires nor releases the writer's book lock. In-memory books cannot be reopened, so
their GET requests deliberately fall back to the serialized writer connection.

Console diagnostics resolve stderr when emitted, since a captured stream can
close while a web request thread remains active. Unexpected web failures send
their sanitized correlation-ID response even if a logging handler fails.

## Command line

`cli/main.py` only builds the argument parser and dispatches: it maps user-fixable
errors (`CommandError`, `DbError`, a missing or unreadable file) to exit status 2
without a traceback and configures logging. Each area's subcommands live in one
module that defines both their parsers, in a `register(add)` function, and their
handlers: `book_commands` (create, back up, restore, migrate, verify, guide, export,
read GnuCash, start web or GTK), `import_commands` (book, CSV, held changes,
write-back, inference), `ledger_commands` (accounts, registers, balances, rates,
transactions), `categorization_commands` (rules, tags, attachments),
`investment_commands` (holdings, cost basis, sale lots, and realized gains),
`tax_commands` (the tax year and its marks), `plan_commands` (schedules,
Review, due review, Plan matches, activity, budget jars, estimates, paychecks),
`benefit_commands` (FSA claims, receivables, goals), and `projection_commands`
(projections, scenarios, comparison, net worth, Dashboard). `cli.common` holds the
helpers they share (date parsing, JSON and table output, book and account lookup)
and imports no command module. Like the web adapters, a command parses arguments
into a typed service request or engine call and formats the result; architecture
tests check that the entry point defines no handlers and that every command's
handler is in the module that registers it.

## GTK and web parity

GTK4 defines the reference workflow and interaction model. The web UI must maintain
functional parity, but parity means common capabilities and semantics, not two
independent implementations of financial business logic.

Shared engines and application services own behavior; interface layers own
presentation, interaction state, and platform-specific concerns.

## Background work

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

