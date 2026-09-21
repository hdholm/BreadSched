# BreadSched design overview and decisions

This document records the architectural principles and important design decisions
that explain how BreadSched works. It describes the current intended design; it is
**not** a list of future work. All pending work belongs in [`ROADMAP.md`](ROADMAP.md).
Task-oriented user operation belongs in the packaged
[`USER_GUIDE.md`](src/breadsched/USER_GUIDE.md), while the README remains the
product and developer entry point.

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
GTK quick posting/deletion use
the same boundary as the full GTK editor and web. GTK and web translate stable
service errors through shared presentation-owned wording; service error codes and
field paths contain no English API prose.

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

The web presentation is split into three boundaries. ``web.server.Api`` translates
plain request values to application/domain calls, ``web.resources`` declares routes
and strictly parses one typed value per query field, and ``web.transport`` owns HTTP
authentication, framing, body limits, status mapping, and static delivery. The
transport never returns unexpected exception text: it logs the exception with a
correlation identifier and returns only that identifier with a stable error code.
JSON writes require one non-negative ``Content-Length`` no larger than 64 KiB and do
not accept transfer encodings. Browser CSS and JavaScript are packaged static assets,
all events are registered from JavaScript, and charts construct SVG through namespaced
DOM nodes rather than interpolating markup. This permits a directive-specific Content
Security Policy with no inline-script or inline-style exception.

The server owns exactly one writable database connection and serializes every write
through it. Each file-backed GET opens a short-lived SQLite read-only connection,
giving projection and other read work an isolated snapshot without holding the global
request lock; closing that connection neither acquires nor releases the writer's book
lock. In-memory books cannot be reopened, so their GET requests deliberately fall back
to the serialized writer connection.

## Persistence verification

Normal writes are verified incrementally from the records already captured by the
database transaction. Changed objects are checked for their own domain invariants
and derived-index rows, and deletions check reverse references that could make
untouched objects invalid. A changed ledger transaction verifies only its own
``split_index`` rows rather than rebuilding the complete index.

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

## Exact financial representation

### Double entry

Transactions are collections of splits and must balance atomically. Persistent
storage must never expose a partially written transaction. Imports that repair or
skip damaged source records must report what was changed rather than silently
inventing semantics.

### Money

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

Ledger reads preserve that identity internally. Account, recursive, class-total,
net-worth, cash-on-hand, and register-running arithmetic uses tagged transaction
values and refuses to combine material values with different currency identifiers.
The long-standing scalar APIs unwrap the checked result to ``Money`` for callers
that only present one reporting currency. An empty zero has no economic dimension
to net and therefore adopts the first material amount's commodity.

### Security quantities and dated valuation

A split has two exact rational dimensions: ``value`` is expressed in the
transaction currency and balances the double-entry transaction, while ``quantity``
is expressed in the account commodity. These must not be collapsed. Historical
ledger value remains an accounting fact even when a security's market price changes.

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

The reporting currency is explicit book metadata when configured, otherwise USD
when present, then the first currency commodity. This initial layer intentionally
requires a direct security-to-reporting-currency quote. Foreign-exchange graphs,
automatic quote retrieval, lot/cost-basis accounting, and projected market prices
are separate concerns and must not be approximated by treating monetary amounts as
prices or quantities.

New native books contain a stable USD commodity and select it as their default
currency. A legacy native book without any currency remains readable; its first
transaction-service write creates that same default commodity and metadata inside
the transaction's atomic database change.

Rounding uses the selected commodity's declared ``fraction`` rather than assuming
cents. Scheduled posting uses the schedule currency; Projection, Dashboard, loan,
inference, and historical-estimate interfaces use the reporting currency; exact
allocation accepts the caller's commodity fraction. Stored ``Money`` remains
rational and unrounded until one of these minor-unit boundaries is crossed.

### Investment activity semantics

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
the selected horizon; variance and actual summary totals include only the applicable
horizon through the report's as-of date. A wholly future horizon reports those two
summary values as not applicable, not zero.

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

When an actual resolves a planned occurrence, BreadSched preserves the original
occurrence identity, planned date, and expected value so later schedule changes do
not rewrite historical variance.

## Scheduled transactions and formulas

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
does not yet have an equivalent recurrence union, so both importers retain all rows,
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
power magnitude are bounded, raw and normalized text have fixed limits, and all
decimal construction and arithmetic run in a local 64-digit context with bounded
exponents independent of process settings. Non-finite values and syntax, decimal,
arithmetic, recursion, or resource failures cross the API boundary as
`FormulaError`. GnuCash colon-delimited argument syntax and grouping commas are
normalized without rewriting ordinary comma-delimited function calls.

Formula-driven loans own their payment arithmetic. Projection must not separately
inflate a formula loan payment or add generic liability interest to a liability
whose interest is already represented by schedule formulas.

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

Loan creation is an application-service workflow around `LoanTerms`: GTK and web
collect the same lender-facing terms, preview the shared amortization table, and
submit a typed `SaveLoan` request. The service validates account roles and owns the
atomic schedule/opening-balance mutation. The stored schedule uses `ipmt`/`ppmt`
formulas and an optional opening liability rather than freezing the preview into
fixed splits.

### Mortgage cash-flow semantics

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

## Projection

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

The immediately preceding alpha representation stored a complete assumption
snapshot. On read, each of those existing values becomes a deliberate local override,
so upgrading cannot silently change an established forecast. New scenario records
persist an explicit inheritance flag and stable sets of field/account overrides;
cached projection results are never stored. A saved scenario may name Base or another
saved scenario as its assumption parent. Resolution walks that chain from Base through
each parent and then applies the child's stable field/account overrides; provenance
continues to name the scenario that supplied each effective value. Writes reject
missing parents and cycles, and deletion refuses a scenario that still has children,
so reparenting is always explicit. Dated periods and scenario events remain local to
their owning scenario until a separate, unambiguous identity-and-merge model is
defined for those lists.

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

### Separate bills, income, and income-triggered reserves

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

## Register presentation and basic entry

Register windows are independent presentation consumers of one open `DbSQLite`
connection. Each owns its account navigation, text filter, selection, and expansion
state while database signals refresh all consumers after a committed change. The
main window owns secondary register-window lifetimes and detaches them before the
book connection closes, preventing callbacks from reaching a replaced database.
Browser windows provide the equivalent independent presentation state and receive
the per-server token only in the URL fragment.

Inline quick entry is deliberately a narrow adapter to `Transaction.simple`: a
positive exact amount, date, description, displayed account, and visible transfer
account become exactly two balancing splits and pass through the ordinary atomic
database transaction. It is not a parallel transaction model. Complex metadata and
multi-split entry remain in the full editor. Register headings are a shared engine
mapping so GTK and web describe the same positive and negative ledger directions.

## Statement reconciliation

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

Compatibility claims are limited to structures demonstrated by fixture-based
round-trip evidence. BreadSched does not claim complete GnuCash round-trip
compatibility for structures it cannot yet export or reconstruct safely.

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
Any expansion of imported-account editing should extend this matrix with the source
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
inherit stale BreadSched state.

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

Transaction deletion synchronization uses a separate complete-scan inventory keyed
by the stable GnuCash chart-root identity, with the canonical source path only as a
fallback. A previously observed transaction GUID that is absent from the source is
removed in the same atomic import operation. A skipped but still present source
record counts as observed and is never mistaken for a deletion. Reconciliation
sessions and FSA claims are durable BreadSched audit data, so a missing source
transaction referenced by either is retained and reported as a conflict instead of
creating a dangling reference. Moving or renaming the same GnuCash book does not
reset its stable inventory when its root GUID is available.

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

The **application version** and **native data-format version** serve different
purposes and never advance in lockstep. The application version reported by
`breadsched --version` identifies the installed build for bug reports, packaging,
and release notes and reports the native compatibility window beside it. Book
verification includes the same application and schema details in its human and JSON
diagnostics. The integer data-format/schema version determines whether a
native book can be opened or must be migrated; it is currently 7. A behavior-only
release changes only the application version. A persistent representation change
increments the data-format version and supplies an explicit migration.

SQLite is the native persistence engine. The current application writes schema 7 and
can migrate schema 6 before decoding primary objects. An
explicit sequential registry and durable ledger, transactional runner, verified
pre-migration backup hook, and versioned fixture make that compatibility boundary
testable. Migration infrastructure is a durable architectural capability even when
an individual obsolete transformation is allowed to expire.

The next representation change will widen the supported migration window to the two
immediately preceding data-format versions. If that next format is schema 8, the
registry will retain both 6→7 and 7→8 and the application will accept schemas 6, 7,
and 8. The window changes when a real schema migration is needed; there is no no-op
format bump. A migration must run before ordinary decoding, fail atomically, preserve
a verified backup, and leave enough version evidence to diagnose or retry safely.
The mechanism and supported migration steps are not removed merely because the
application version advances. Stable releases may require a still wider promise.
This native-book policy is independent of external GnuCash, QIF, OFX, and QFX import
compatibility.

Releases are selected explicitly by a checked-in `docs/releases/vVERSION.md`; an
alpha increment alone is not a release request. After the full CI push run succeeds,
the release workflow requires that exact tested commit still be the tip of `main`,
validates the notes against application and schema constants, builds and installs the
wheel, verifies SHA-256 checksums, and only then creates the annotated tag and GitHub
release. Release artifacts and notes are never silently replaced.

The storage priorities are atomic financial writes, explicit format rejection,
verified backups and recovery, undo/redo integrity, and realistic performance on
long household histories.

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

Native books deliberately use SQLite `DELETE` journaling with `synchronous=FULL`,
not WAL. BreadSched has one explicit writer, while short-lived read-only projection
connections may coexist between commits. Keeping rollback journaling preserves the
single-file book model, avoids persistent `-wal`/`-shm` companions that are easy to
separate during manual copying or cloud synchronization, and gives interrupted
writes SQLite's established rollback recovery path. Any future WAL change requires
tested checkpoint, backup, sidecar, and crash-recovery semantics rather than being a
performance toggle.

Backup and restore use SQLite's backup API rather than filesystem copying. Restore
verifies the source logically and physically, holds the same canonical destination
writer lock used by a live book, creates a pre-restore backup before an authorized
replacement, writes and re-verifies a temporary database, removes stale SQLite
sidecars, and only then atomically installs it. A restore therefore cannot replace
the pathname beneath another live writer. Verification opens malformed books in a
special tolerant read-only mode so damage is reported rather than decoded into
ordinary engine state.

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
classifications and the claim row atomically, and one undo reverses both. GTK and web
submit typed claim, allocation, link, and rejection inputs rather than constructing
or persisting claim domain objects. Expected validation failures cross that service
boundary only as stable codes and field paths.


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
available, runtime/widget failures remain real test failures. CI separately installs
PyGObject while deliberately removing the GTK4 typelib, then runs launcher help,
version, and real-launch checks so a partial system installation cannot turn test
collection or startup into a traceback.

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
previews are owner-readable and removed when the application exits. A synchronous
print boundary waits for an in-flight background calculation before it reads the
visible view's result; a bounded timeout reports failure instead of reusing a stale
or previously opened report.

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

Documentation has five distinct jobs:

- `README.md`: product orientation, development installation/invocation, and links;
- `src/breadsched/USER_GUIDE.md`: standalone task-oriented user documentation,
  packaged verbatim for offline application help;
- `DESIGN.md`: current architecture, rationale, and durable design decisions;
- `ROADMAP.md`: the one authoritative list of incomplete/future work;
- `CHANGELOG.md`: completed milestones and their durable acceptance contracts.

When a patch changes architecture, update this design document. When it adds,
changes, or reprioritizes pending work, update the roadmap. When it completes an
accepted outcome, move that contract to the changelog. Do not use the README or
design document as an alternate TODO list.

The GTK **Help → User Guide** action reads the installed package resource and
presents it in a bounded, scrollable native window. The Markdown file remains the
only content source: the application performs a deliberately conservative
presentation transform instead of maintaining a second embedded copy or requiring
a browser, network access, or a Markdown-rendering runtime dependency.

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
