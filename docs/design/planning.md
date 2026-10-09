# Dated planning and scenarios

Part of the [BreadSched design](../../DESIGN.md).

## Event-driven Plan

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

## Schedules and formulas

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

## Due and missed occurrences

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

## Payroll

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

## Loans and mortgages

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

## Escrow

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

## Credit-card payments

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

## Scenarios and Projection

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

`Projection.runway()` returns a `CashRunway`: the horizon in months, the months
covered before the first month that closes with negative cash, that month, the
lowest cash and its month, the first goal shortfall, and each drawdown account the
engine recorded as running out (`Projection.depletions`, set the first time a
drawdown withdraws less than it asked for). `presentation.runway_lines` and
`runway_comparison_text` word it for GTK's projection notes, the browser
(`runway`, `runway_notes`, and the comparison's `runway` and `runway_comparison`),
print, and the `project` and `compare` commands.

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

## Historical estimates

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

## Savings goals

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

## Budget jars

`engine.budget_jars` treats every scheduled outflow and every savings goal as a jar
and reports dated fills and draws; nothing is stored. A schedule's jar is per account
it spends into: its expense legs, or, with none, its non-cash asset or liability
legs (a loan payment). Each occurrence is filled by the dated income occurrences of
its cycle (`cash_flow.income_occurrences`, so missed past income fills nothing and
future income is expected), in proportion to each income's share of the cycle's
income, exactly the Dashboard's bill reserve; the shares are rounded to the
reporting fraction with the last taking the remainder. With no income in the cycle
the whole amount fills on the cycle's first day. The occurrence's planned draw is on
its planned date, and the actual draw is the matched transaction's legs in that
account on its posting date (`planning.scheduled_events`). Every other posting to a
jar account in the jar's currency, from the account's first cycle start, is an
actual draw too (`_unmatched_draws`, #312), so a bundle's actual equals the Plan's
category actual for that account: it joins the account's only jar, or its only
estimate, and otherwise a `JarKind.UNMATCHED` "Unmatched spending" jar with no fills.
A matched transaction's leg is drawn once, by its occurrence. Occurrences up to
`_HORIZON_DAYS` after the range are read so a later-due occurrence still fills inside
it. A goal's fills are the changes in `savings_goals.goal_progress(...).set_aside` on
income, allocation, target, and period-end dates; closing it draws the whole earmark.
`reporting_periods` only groups those dated events. A scheduled jar's level carries
in every earlier event: occurrences are read from the earliest schedule's start, and
the opening level is every fill less every actual draw dated before the range, so
earlier leftovers and overspending count and an unmatched occurrence's money stays in
the jar until matched or skipped; a goal's level is its earmark. Jars bundle by account and currency, and totals never add
currencies together. `report_layout.budget_jars_layout` prints the report; the GTK
`BudgetJarsDialog` (Plan → Budget Jars), the browser (`/api/budget-jars`), and
`breadsched jars` show it.

## Retirement drawdown

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

