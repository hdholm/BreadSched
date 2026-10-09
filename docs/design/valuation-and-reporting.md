# Valuation and reporting

Part of the [BreadSched design](../../DESIGN.md).

## Currency conversion and valuation

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
automatic quote retrieval, and projected market prices are separate concerns and must not be approximated by treating monetary amounts as
prices or quantities.

Cost basis (`engine/cost_basis`) is derived, never stored, like a receivable's
status: each security account's splits are read in date order, a split adding
shares opens a lot at its transaction-currency value, and a split removing shares
consumes lots first in, first out with exact rational shares (`Money * Fraction`),
or, when `Account.cost_basis_method` is `"average"`, takes the same fraction of every
open lot so each sold share costs the pool's average. The method is a per-account
setting validated by the account service (`COST_BASIS_METHODS`) and kept across a
GnuCash re-import, like the dependent-care flag; sales are
recalculated, never re-stored, when it changes. Each sale records proceeds, cost,
and any shares sold beyond the recorded purchases. A split whose transaction gives
the opposite quantity to another security account of the same commodity is a
transfer (`LotMove`): the sender removes lots by its method without realizing
anything, and the receiver re-derives the sender as of that date and adopts the
exact lots it sent, keeping their purchase dates (so first in, first out stays in
purchase order). Within a day, splits adding shares are read before splits removing
them, so the result never depends on split-handle order. Shares moved both ways
between the same two accounts on one day cannot be matched to lots and stay a named
sale and purchase at their recorded values; for longer same-day cycles,
re-derivation carries a visiting set of (account, date) pairs and falls back rather
than recursing. A transaction whose other legs are all zero in value and
quantity, changing shares at zero value while shares are held, is a share split:
every lot's shares scale by the same exact factor and its cost stays. A GnuCash share
split imports in this shape: its one split plus an importer-added zero-value leg in
`Equity:Share splits` (see the interoperability part).
`HoldingCostBasis` compares the open lots' cost with `account_value`'s market value
only when that value is a market quote in the same currency, and names zero-cost
arrivals, uncovered sales, mixed currencies, and a missing or foreign quote instead
of approximating. `presentation.holding_cost_text` words it for the GTK
`HoldingsDialog`, the browser (`/api/holdings`, shares as exact decimal text), and
`breadsched holdings`.

A sale may name its lots instead (specific identification). The choice is the
only stored part: `Split.lot_picks`, a tuple of `LotPick(lot, quantity)` on the
selling split, serialized only when set, where `lot` is the handle of the
transaction that opened the lot (a lot keeps it through sales, share splits, and
transfers) and `quantity` is shares as held at the sale. `cost_basis` takes the
named parts first and sells any remainder by the account's method; a pick the lot
can no longer honour is a named problem, never an error. Each `RealizedGain` keeps
the lot parts it took (`lots`), whether it named them (`specific`), and its
`split`. `lots_before_sale` re-derives the open lots just before a sale for
choosers, and `services.lots.set_sale_lots` validates picks against them (known
lot, positive shares, no more than the lot holds or the sale sold, each lot once)
and stores them as one undo step; rejected input changes nothing. The choice is
BreadSched-owned: `import_review.merge_local_state` keeps it on re-import, edits
copy it with the split, and it is not among the source facts write-back compares.
`engine.realized_gains` gathers every sale (optionally one year or account) with
its lots and totals by year and currency, never adding currencies together;
`report_layout.realized_gains_layout` prints it, the GTK `RealizedGainsDialog` and
`SaleLotsDialog`, the browser (`/api/realized-gains`, `/api/holdings/sale-lots`),
and `breadsched realized-gains` and `sale-lots` show and change it.

`engine.tax_year` derives one calendar year (US-oriented) for tax preparation, and
stores nothing but the marks. Each sale's lots split into short-term and long-term
parts: a lot is long-term when sold after the anniversary of its purchase
(`holding_term`; a 29 February purchase's anniversary is 28 February), and a
transferred lot keeps its purchase date. A mixed sale's proceeds are shared by
shares, the short-term part rounded to the cent and the long-term part taking the
rest, so the parts add to the sale; shares sold beyond the recorded purchases are a
named problem and left out. Tax-relevant accounts total the year's change in each
marked account and those beneath it, as the account shows it, per commodity; an
account is marked by `Account.tax_relevant_override`, or without it by GnuCash's
`tax-related` slot as imported into `source_fields`. Tax-relevant tags (book
metadata `tax.tags`) total their transactions' expense splits as spent and income
splits as received, per currency. Income by source is every income account's year
total. `services.tax.set_tax_marks` changes account and tag marks as one undo step,
editing copies so a rejected request changes nothing. `report_layout.tax_year_layout`
prints it; the GTK `TaxYearDialog` and `TaxMarksDialog`, the browser
(`/api/tax-year`, `/api/tax-marks`), and `breadsched tax-year` and `tax-marks` show
and change it.

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

### Online quotes

`gen/services/quotes.update_quotes` asks the fetcher passed to it for every
commodity whose `quote_source` is set, except the reporting currency. It stores each
answer as a `CommodityPrice` whose `source` is `Online: <origin>` (`tsp.gov`, `ECB`,
`Alpha Vantage`, `Finance::Quote <method>`) and whose `quote_type` is `last`, so
valuation discloses it like any other quote. A price for the same commodity,
currency, date, and source is updated in place. A quote in a currency the book
lacks, or with no positive price, is reported and not stored. ECB rates are crossed
through the euro into the reporting currency and rounded to 10 decimal places. The
network code lives in `plugins/quotes` (see
[decision 0002](decisions/0002-dependencies-and-online-quotes.md)). The GTK
**Online Quotes** dialog fetches in a `BackgroundJob` worker that never touches the
book and stores the answers on the main thread through `quotes.Prefetched`; the web
adapter (`web/quote_resource.py`) and the CLI fetch and store in one call. The Alpha
Vantage key lives in `settings.ini` (made owner-readable when saved), never in the
book, and the web API reports only whether one is set.

## Valuation completeness

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

## Reporting terms

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

## Dashboard

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

### Bills, income, and reserves

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

## Expense Explorer

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
draws it with the older `LineChart` (a dashed as-of marker, the selected period
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

## Net worth history and change

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

## Printable reports

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

**Charts as data.** An engine describes a chart as a `gen.engine.chart_model.ChartModel`:
categories, series of exact `Money` values (None where unavailable), a currency
label, and per series a categorical colour slot chosen by what the series is, so a
series keeps its colour whatever else is shown. Charts never compute totals; their
values are the engine's report values, and `report_layout.chart_blocks` always puts
the table of those values beside the chart (`ModelChart` then a `Table`).
`presentation.charts` holds the one categorical palette (eight hues in a fixed
order, validated for colour-blind separation; a dark step of each hue for dark
themes) and the shared geometry (`chart_bar_layout`: round ticks including zero,
columns at most 24 units wide with a 2-unit gap, a rounded data end and a square
foot on the baseline). GTK draws a model with `widgets.model_chart` (`paint_chart`,
also used by the native printer; the dark steps when the theme's ink is light;
hover shows a column's exact amount), the HTML export with `model_chart_svg`, and the
browser with `modelChart` in `core.js` (SVG with a `<title>` per column and the table
in the same figure; the browser has one light theme). Legends and axis text use ink
colours, never a series colour. Budget jars is the first report charted this way:
per account, planned against actual draws by period (slots 1 and 2) and each jar's
level beside its target (slots 3 and 4).

A model's `kind` is `BARS`, `LINE`, `STACKED`, or `SHARE`. A line chart may carry `ChartMarker`s (a
labelled vertical rule at one category) and `partial_from`, the first category whose
values leave something out, shaded with `partial_note` beside it.
`chart_line_layout` places lines edge to edge; `chart_label_indices` keeps a long
axis to at most eight labels, always including the last, and the first and last
labels of a line stay inside the plot. Hovering a line chart lists every series'
exact value at the nearest category (GTK tooltip; a transparent hit area with a
`<title>` per category in SVG). `projection_result.projection_chart` charts cash,
investments, and net worth by month (slots 1 to 3), marks the first cash shortfall
and, when it comes first, the month goals' earmarks exceed cash, overlays a compared
scenario's net worth (slot 4), and shades months whose values are partial. The GTK
Projection view, the browser Projection page (including its comparison overlay), and
the printed Projection use it; the browser keeps the monthly values in a **Chart
values** toggle beside the chart, and printing puts them in the optional
**Projection chart values** section, since the year-end table is always printed.

`STACKED` draws one column per category with its series stacked in order, positive
values up from zero and negative ones down; only the outermost segment in each
direction has the rounded end, and segments that continue are separated by a
1-unit gap. `SHARE` is the same column with each value drawn as its percentage of
the category's `totals` entry, the total the engine reported (a percent scale; a
category without a positive total draws nothing), so a share chart never sums its
own series. Each layout reports every category's band (`bands`) for hit testing:
a view created with `on_select` (GTK) or `onSelect` (browser) shades the selected
band and makes each band a target (in the browser, a focusable button whose
`<title>` lists every series' value), so a chart can choose the period a screen
shows. A column chart shades `partial_from` from that category's band. The table
beside a chart with totals adds a **Total** column; a share chart's cells are
percentages. `expense_explorer.spending_charts` draws the Expense Explorer's
spending (and income) from its own `SpendingPoint`s: plan and actual lines (slots
1 and 2, a rule at the as-of date's period), actual stacked by top-level category,
and each category's share, the categories ranked by actual over the range with
those past the seventh combined as **Other** (slot 8), still reconciling to each
period's actual exactly. `category_trend_chart` draws one category's plan and
period actual. GTK, the browser, and the printed explorer use them; the period
comparison's paired bars remain the explorer's own.

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
The browser route, a private, owner-readable HTML preview opened in the default
browser and removed when the application exits, is the placeholder for native macOS
printing: only on macOS does the File menu offer **Print in Browser…** (Linux and
Windows print natively). The dialog reports (Net Worth History, its change detail,
Expense Explorer, Realized Gains, Tax Year, and Budget Jars) have layouts of their own and print through
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
regions, and preserves text, tables, and SVG charts as scalable output. A report
dialog marked `printable-dialog` (Realized gains, Tax year, Budget jars) prints on its own: its **Print**
button sets `body.printing-dialog` for the print, which hides the page and shows only
that dialog, and `screen-only` controls are left out. Both paths
keep formatting and pagination in presentation code while all monetary values,
totals, classifications, and comparisons remain engine-owned.

