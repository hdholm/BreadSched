# 0003: A book server that owns a book for every interface

Part of the [BreadSched design](../../../DESIGN.md); see
[Architecture and ownership](../architecture.md#book-server-experimental).

- **Status:** experimental, 2026-10-23. Work happens on the
  `claude/experimental-book-server` branch and does not land on `main` until its
  full impact is understood (#354).
- **Decision under trial:** a *book server* owns one open book: its storage, undo
  history, and change notifications. The command line, the browser, and GTK reach a
  book only through one API, the `BookApi` protocol. An interface can use an
  in-process server, or a server started on its own that several interfaces share.

## Context

Validation and every write already belong to `gen/services`, and each interface is
an adapter over them. What is still duplicated is:

- how results become JSON: the command line's `--json` and each web adapter each
  had their own shape;
- reads that bypass the services, in the command line and in GTK;
- input lookups, such as finding a schedule by name.

Each process also opens the book itself and holds its writer lock alone. So the
browser and GTK cannot share a book, and a command run while the web server is
open fails on the lock.

## Decision

1. **Protocol and contract (#355).**
   - `breadsched.api.protocol.BookApi` lists what a book server offers. Its requests
     and results are the services' own typed dataclasses.
   - `breadsched.api.contract.ApiError` is the one error envelope. A missing object
     is a 404; anything else is a 400. The browser's `ResourceError` is now this
     same class.
   - `breadsched.api.views` builds the one JSON form of each response, and
     `to_json` encodes the leaves.
2. **In-process server (#356).**
   - `breadsched.api.local.LocalBookServer` implements the protocol over the
     services and one `DbSQLite`.
   - It observes the book's storage signals. Every change, including undo, redo,
     and writes by other code on the same book, raises its revision by one and
     reaches each subscriber as a `Change` (revision, kinds, undo label).
   - The command line and the browser move onto it one area at a time.
3. **HTTP transport (#357).**
   - The protocol is served as versioned `/api/v1` resources, with record
     revisions (`ETag`/`If-Match`) and a server-sent events stream of changes.
   - A client implementing the same protocol hides which kind of server a caller
     has.
   - The revision must then be stored with the book, so that changes made by
     another process count; the in-process server counts only what it observes.
4. **A shared, independent server (#358).**
   - `breadsched serve BOOK` runs a server that no interface owns.
   - The book lock records its endpoint and a token file only the user can read,
     so other interfaces connect to it instead of failing on the lock.
5. **GTK on the protocol (#359).** GTK moves view by view, keeping the register
   performance tests green, then may connect to a shared server.

## Where it stands

Scheduled transactions are the pilot area. These now go through `LocalBookServer`
and `breadsched.api.views`:

- the command line's `due-review`, `scheduled-next`, `scheduled-advance`, and
  `scheduled --post`;
- the browser's due review, next-occurrence routes, each definition's `next`,
  schedule delete and duplicate, and post-scheduled;
- GTK's due review, Enter Next and Skip Next, and schedule delete and duplicate
  (phase 5, below).

Saving from the schedule editors (`save_fixed_schedule`, `save_formula_schedule`)
goes through the server from the browser and GTK too. Over HTTP, the editors'
typed requests travel in `api.schedule_codec`'s strict JSON form:
- **Encoding:** every field is kept, with the stored forms of recurrences, dated
  amount changes, and the schedule an edit is based on.
- **Decoding:** a value of the wrong type is a 400, never a guess.
- **Cost:** this codec is the measure of moving a large editor. It is about 300
  lines for the fixed and formula schedule requests, each field written once each
  way. Transactions, scenarios, and the Plan will need the same.

Transactions followed:
- **What moved:** saving and deleting a transaction go through the server from GTK
  (the register, the transaction editor, and Posted transactions), the browser's
  register and entry form, and the command line's `add`, `edit`, and `delete`.
- **Codec:** `api.transaction_codec`, about 200 lines, carries `SaveTransaction` over
  HTTP. Each split's value and account-commodity quantity stay distinct amounts.
- **A bug it found:** writing it showed that the book API's money encoding rounded
  to cents, which would have changed share quantities and prices. Money is now
  written exactly (`contract.exact_money`): decimal text when it terminates, `n/d`
  otherwise.
- **Lookups joined the API:** finding an account (`find_account`) or a transaction
  (`find_transaction`, by handle or unique prefix) and the editors' form of a stored
  transaction (`editable_transaction`) are book API reads. So `add`, `edit`, and
  `delete` work through a shared server too. `edit` changes the editable form
  (keeping the two-split rule for amounts) and saves it.

Ledger reads followed, as a shared service (`gen.services.ledger_reads`):
- **What moved:** an account's register with running balances
  (`register`: start, end, tag, limit), one account's balance (`account_balance`,
  optionally with its descendants), and the book's cash and net worth with the
  quotes it lacked (`balance_summary`). The command line's `register` and `balance`
  use them, through a shared server when one holds the book.
- **Resources:** `GET /api/v1/currency`, `/accounts/lookup`, `/transactions/lookup`,
  `/transactions/{handle}/editable`, `/accounts/{handle}/register`,
  `/accounts/{handle}/balance`, and `/balances`. A bad query value is a 400 from
  the strict query parser, never a guess.
- **JSON:** `register --json` and `balance --json` print what they printed before.
  The `/api/v1` register adds each line's split handle.
- **Still to move:** the browser's and GTK's registers read the book directly; they
  move when their register views do.

Accounts followed:
- **Reads:** `accounts` (every account as stored, with its full name), `account`
  (one, by handle), and `chart` (the tree, depth first, each line valued in the
  reporting currency with its missing quotes, quote date, source, age, conversion
  path, and quote evidence, and its ledger balance). A foreign balance without a
  quote stays `None` with the account named in `missing_quotes` on both transports.
- **Writes:** `save_account` and `delete_account`. Over HTTP, `api.account_codec`
  carries `SaveAccount` with the account, and the account an edit is based on, in
  their stored forms. That made the codec about 80 lines, against 200 for
  transactions: a record whose stored form is complete needs no field-by-field
  codec.
- **Resources:** `GET` and `POST /api/v1/accounts`, `GET` and
  `DELETE /api/v1/accounts/{handle}`, and `GET /api/v1/chart` (`as_of`, `hidden`).
- **Command line:** `accounts` and `account list|add|edit|remove` use them, with
  unchanged JSON.
- **Browser:** the chart of accounts (`/api/accounts`) is the server's `chart`, and
  the account settings saves (type, emergency fund, card payment, FSA years, cost
  basis) save through the server, with unchanged responses.
- **GTK:** the account editor saves and deletes through `book_server(db)`, and shows
  a refusal's message as before. The browser's and GTK's registers still read the
  book.

Categorization rules followed, in all three interfaces at once:
- **One read:** `categorization_rules` returns the rules in priority order, the
  current proposals, and the category choices, each with its account's full name.
  The command line, the browser, and GTK's rules dialog format that one answer, so
  no interface looks up names in the book itself.
- **Writes:** `add_rule`, `move_rule`, `delete_rule`, and
  `apply_category_proposals`, each as one undo step, as before.
- **Change kind:** rules live in the book's metadata, which raises no storage signal.
  The server names a rule write's change `categorization-rule` itself, so a
  subscriber can tell it apart from the "reload" that an empty `kinds` means.
- **Resources:** `GET` and `POST /api/v1/rules`, `PATCH` and
  `DELETE /api/v1/rules/{handle}`, and `POST /api/v1/rules/accept`. The browser's
  `/api/rules` responses are unchanged.

Tags followed: `tag_counts`, `transactions_with_tag` (stored forms), and `set_tags`
(`GET /api/v1/tags`, `GET /api/v1/tags/transactions`, and
`PATCH /api/v1/transactions/{handle}/tags`). The command line's `tags` and the
browser's tag editor use them. The browser's answer still lists the transaction's
documents from the book it serves: linked documents have not moved, because linking
a file copies it from the client's disk, which a separate server cannot read.

Savings goals followed, in all three interfaces:
- **One read:** `savings_goals(as_of, include_closed)` returns the report (each goal
  in its stored form with its earmark), each goal's scenario changes, the accounts a
  goal can be held in or buy into, the scenarios, and the reporting currency for the
  chart. The command line's `goals`, the browser's goals screen, and GTK's goals
  dialog format that one answer; the chart is drawn from it on each side.
- **Writes:** saving, allocating, closing, reopening, deleting, and a scenario's
  change to a goal (`set_goal_override`, answered with the scenario as stored).
  `find_savings_goal` takes a handle, unique handle prefix, or name.
- **Resources:** `GET` and `POST /api/v1/goals`, `GET /api/v1/goals/lookup`,
  `POST /api/v1/goals/{handle}/allocations`, `/close`, `/reopen`, and `/override`,
  and `DELETE /api/v1/goals/{handle}`.

Statement reconciliation followed, in the browser and GTK (the command line has no
reconciliation command):
- **One read:** `reconciliation(account)` returns the open session's calculated
  state (balances and every candidate entry), the account's sessions newest first,
  and how many reimbursement and FSA claim link proposals wait on the account.
- **Writes:** starting, updating (checked entries and the ending balance),
  completing, cancelling, and reopening a session, and the register's cleared
  toggle (`toggle_cleared`), used by the browser's and GTK's registers.
- **Resources:** `GET /api/v1/accounts/{handle}/reconciliation`,
  `POST /api/v1/reconciliations`, `PATCH /api/v1/reconciliations/{handle}`,
  `POST /api/v1/reconciliations/{handle}/complete`, `/cancel`, and `/reopen`, and
  `POST /api/v1/transactions/{handle}/splits/{split}/cleared`.

A generic typed codec followed (`api.typed_codec`), because hand-written codecs were
the main cost of each move (about 300 lines for schedules, 200 for transactions):
- **What it does:** `to_data` writes any typed request or result as JSON data, and
  `from_data(type, data)` reads it back from its type alone: money exactly, dates,
  enums, unions, nested dataclasses, and records in their own stored forms.
- **Strict reading:** an unknown field, a missing required one, or a value of the
  wrong type is a `ValueError` naming where it is, so a server never guesses at
  what a client sent.
- **Cost now:** an operation needs its route and one line each way.

Receivables were the first area moved with it, in all three interfaces:
- **One read:** `receivables_view(as_of)` returns each receivable's standing, its
  linked splits as the ledger has them (with account names), its shared costs with
  an FSA claim, and its scenario changes; the reimbursement proposals; the splits
  that could be linked; and the receivable accounts and scenarios to choose from.
  `receivable_candidates(receivable)` gives one receivable's linkable splits.
- **Writes:** saving, linking (as an expense or a reimbursement) and unlinking a
  split, disputing and clearing a dispute, writing off, deleting, accepting
  proposals, and a scenario's change to an expected reimbursement.
- **Resources:** `GET` and `POST /api/v1/receivables`, `/lookup`, `/candidates`,
  `POST /api/v1/receivables/{handle}/splits`,
  `DELETE /api/v1/receivables/{handle}/splits/{transaction}/{split}`, `POST` and
  `DELETE /api/v1/receivables/{handle}/dispute`, `/write-offs`,
  `DELETE /api/v1/receivables/{handle}`, `GET /api/v1/reimbursement-proposals`,
  `POST /api/v1/reimbursement-proposals/accept`, and
  `POST /api/v1/scenarios/{handle}/reimbursements`.
- **Still local:** `receivables --costs`, a Plan report, opens the book itself
  until the Plan's reports move.

Scenarios and projection assumptions followed, with the typed codec carrying each
scenario in its stored form:
- **Reads:** `scenarios` (every scenario as stored), `find_scenario` (by handle or
  name), `drawdown_accounts`, and `base_assumptions`.
- **Writes:** saving a scenario (and saving it after checking its assumptions),
  duplicating, deleting, leaving a baseline schedule out, saving and removing a
  drawdown, saving the base assumptions, and saving or deleting a dated assumption
  period. Base assumptions live in the book's metadata, so the server names their
  change `base-assumptions` itself.
- **Interfaces:** the command line's `scenario` and `drawdown`; the browser's
  scenario management, drawdown, period, and suppress routes; GTK's scenario,
  scenario manager, and drawdown dialogs, the Plan view's suppress, the Projection
  view's save, and the shared Base assumptions.
- **Still local:** projecting, comparing, and the Plan read the book directly, and
  the browser's scenario estimate editor (`save_fixed_scenario_schedule`) has not
  moved.

Payroll followed:
- **Reads:** `payroll(as_of)` (`PayrollView`: paychecks, saved templates, the
  accounts a template may use, and their full names) and `paycheck(schedule,
  as_of)`, one schedule read as a paycheck.
- **Writes:** drafting a template from a paycheck (nothing saved), saving and
  deleting a template, creating a paycheck schedule from a template, and previewing
  or applying a dated pay change. Templates live in the book's metadata, so the
  server names their change `payroll-template` itself. The typed codec now reads a
  `Mapping` field (a pay change's per-account amounts) as an object.
- **Resources:** `GET /api/v1/payroll`, `GET /api/v1/schedules/{handle}/paycheck`,
  `POST /api/v1/payroll/templates/draft`, `POST /api/v1/payroll/templates`,
  `DELETE /api/v1/payroll/templates/{name}`, `POST /api/v1/payroll/paychecks`,
  `POST /api/v1/payroll/pay-changes/preview`, and `POST /api/v1/payroll/pay-changes`.
- **Interfaces:** the command line's `payroll`, the browser's payroll page, and
  GTK's Payroll dialog.

FSA claims followed, all through the typed codec:
- **Reads:** `fsa_claims(as_of)` (`FsaClaimsView`: every claim's stored form and
  recomputed standing, the allocation accounts' names, and the splits, FSA accounts,
  and payers a claim could link), `find_fsa_claim` (handle or unique prefix),
  `fsa_claim_report(ClaimReportQuery)`, `benefit_years(as_of)`, and
  `claim_link_proposals(account)`.
- **Writes:** saving, deleting, closing, and reopening a claim, and accepting
  claim-link proposals.
- **Resources:** `GET` and `POST /api/v1/fsa-claims`, `/lookup`, `/report`,
  `DELETE /api/v1/fsa-claims/{handle}`, `POST /api/v1/fsa-claims/{handle}/close` and
  `/reopen`, `GET /api/v1/fsa-years`, `GET /api/v1/fsa-claim-proposals`, and
  `POST /api/v1/fsa-claim-proposals/accept`.
- **Interfaces:** the command line's `claims`; the browser's FSA claims and FSA
  Dashboard; GTK's FSA Dashboard, the FSA claims dialog's claim list and writes, and
  the import dialogs' claim-link notices.
- **Still local:** the GTK claims editor builds its link candidates, funding-year
  windows, and the standing of the claim on screen from the book directly.

Plan review (resolving actuals against planned occurrences) followed:
- **Reads:** `review_queue` (the pending actuals) and `review_detail(transaction)`
  (its explained candidates, FSA hint, the reason none is offered, and the FSA
  claims and roles it could attach through), and for the command line
  `plan_occurrence(key)`, `unresolved_occurrences(start, end)`, and
  `match_candidates(transaction, window_days)`.
- **Writes:** match, reject, skip, mark unexpected, and attach to an FSA claim.
- **Resources:** `GET /api/v1/review`, `GET /api/v1/review/{transaction}`,
  `POST /api/v1/review/{transaction}/match`, `/reject`, `/skip`, `/unexpected`, and
  `/claim`, `GET /api/v1/plan/occurrences` and `/lookup`, and
  `GET /api/v1/transactions/{handle}/plan-matches`.
- **Interfaces:** the command line's `review` and `plan-*` commands, the browser's
  Review, and GTK's Review view.

GnuCash changes held back from reconciled transactions followed: `held_imports` and
`resolve_held_imports` (`GET /api/v1/held-imports` and
`POST /api/v1/held-imports/resolve`) serve the command line's `import-review`, the
browser's held-change review, and GTK's review dialog and its prompts. Importing a
file still opens the book itself, since the file is read where the command runs.

A sale's lots and the tax marks followed: `sale_lots` and `set_sale_lots`
(`GET` and `POST /api/v1/transactions/{handle}/splits/{split}/lots`) and `tax_marks`
and `set_tax_marks` (`GET` and `PATCH /api/v1/tax-marks`) serve the command line's
`sale-lots` and `tax-marks`, the browser's lot chooser and tax marks, and GTK's Choose Lots
Sold and tax-marks dialogs. The typed codec now reads a `Sequence` field (a sale's
picks) back as a tuple. The holdings, realized-gains, and tax-year reports still
read the book directly.

Online quotes followed, split so the book server never reaches the network:
`quote_plan` (every commodity and its quote source, the requests a fetch would make,
and the reporting currency; `GET /api/v1/quotes`), `store_quotes` (`StoreQuotes`:
quotes the caller fetched, `POST /api/v1/quotes`), and `set_quote_source`
(`PATCH /api/v1/commodities/{handle}/quote-source`). The command line's `quotes` and
`quote-source`, the browser's quotes page (whose adapter fetches), and GTK's Online
quotes dialog (whose worker fetches) fetch where they run and hand the answers to
the server, which stores them with their provenance as before.

Creating a loan followed: `loan_choices` (the accounts a loan may use and the
book's amount precision), `preview_loan` (a checked payment, total interest, and
first payments), and `save_loan` (`GET /api/v1/loans/choices`,
`POST /api/v1/loans/preview`, and `POST /api/v1/loans`) serve the browser's loan
form and GTK's loan dialog save. GTK still previews in the dialog from the engine,
which reads only the terms, and there is no loan command.

Manual exchange rates followed, with a service of their own
(`services.exchange_rates`) that checks a quote before `engine.valuation` writes it
and names each refusal with a code: `currencies`, `find_currency` (exact handle or
unambiguous code), and `save_exchange_rate` (`GET /api/v1/currencies`, `/lookup`,
and `POST /api/v1/exchange-rates`) serve the command line's `rate`, the browser's
manual quote, and GTK's exchange-rate dialog save.

The Dashboard's settings followed: `dashboard_settings` and `save_dashboard_settings`
(`GET` and `PATCH /api/v1/dashboard/settings`) carry its groups with their member
accounts (a `DashboardSettings` view, since the stored form keeps the members on the
accounts themselves) and its horizons. They serve the browser's group editor and
GTK's group dialog and horizon spinners; the server names the change
`dashboard-config`. The Dashboard itself is still computed from the book.

Linked documents followed: `attachment_report` (all or one transaction's documents,
checked against the file system), `link_attachment`, `attach_file`,
`unlink_attachment`, `relink_attachment`, `set_attachment_folder`, and
`set_source_link_folder` (`GET /api/v1/attachments`,
`POST /api/v1/transactions/{handle}/attachments`, `/files`, `/remove`, `/relink`, and
`PATCH /api/v1/attachment-folders`). A file is named by its path as the server sees
it, so `attachments --add FILE` through a server on another machine needs a path
that machine can read; the typed codec now carries paths as text. The command line's
`attachments`, the browser's document links and uploads, and GTK's transaction
dialog documents go through the server; showing a document's contents still reads
the file directly.

The net worth reports followed: `net_worth_history` and `net_worth_change`
(`GET /api/v1/net-worth` and `/net-worth/change`) return the services' own history
and change, completeness evidence included, to the command line's `net-worth` and
`net-worth-change`, the browser's net worth page, and GTK's net worth dialog.

The Plan and the Expense Explorer followed, each read taking a typed `PlanQuery`:
`plan` and `expense_explorer` (`POST /api/v1/plan` and `/expense-explorer`) return
the services' own `PlanQueryResult` and `ExpenseExplorer`, which the typed codec
carries whole; `reporting_currency` (`GET /api/v1/reporting-currency`) names the
currency their charts are drawn in. They serve the browser's Plan and Expense
Explorer and GTK's Plan view. GTK's Expense Explorer still computes in a background
worker with its own read connection, and changes its drilldown without recomputing
the Plan, so it has not moved.

Projecting followed: `project(scenario)` and `projection_month(scenario, index)`
(`POST /api/v1/projection` and `/projection/month`) take a saved scenario or a draft
and return the engine's own `Projection` and month detail, which the typed codec
carries whole. They serve the command line's `project` and `compare` and the browser's Projection
page, which now builds its drafts from the server's scenarios and base assumptions,
names its accounts from `accounts`, checks per-account rates the same way, and saves
through the assumption operations. GTK still projects in a background worker with its
own read connection.

The investment reports followed: `holdings(as_of)`, `realized_gains(year, account)`,
and `tax_year(year)` (`GET /api/v1/holdings`, `/realized-gains`, and `/tax-year`)
return views from `gen/services/investment_reports` that carry the engine's report
whole with the account names and currency mnemonics its layouts show (and, for
holdings, the charts), so no adapter reads the book to label them. The engines'
`currency_labels`, `as_dict`, and `holdings_charts` now take those names and
mnemonics instead of the book. They serve the command line's `holdings`,
`realized-gains`, and `tax-year`, the browser's Holdings, Realized Gains, and Tax Year
pages, and GTK's dialogs of the same names; an unknown account is a 404 rather than a
report with no sales.

Budget jars followed the same way: `budget_jars(start, end, period, today)`
(`GET /api/v1/budget-jars`) returns a view from `gen/services/budget_jars` carrying
the engine's report with its currency mnemonics, from which each interface takes its
labels and charts; a range that ends before it starts is refused there
(`jars.range.invalid`). It serves the command line's `jars`, the browser's Budget
jars page, and GTK's Budget Jars dialog.

The Dashboard itself followed: `dashboard(as_of, liquidity_days, emergency_months)`
(`GET /api/v1/dashboard`) returns the engine's own `Dashboard`, computed once by
`dashboard_config.dashboard_board`, which the typed codec carries whole with its
configuration, so the summary, unavailable reasons, groups, and bill and income
rows every interface shows come from one place. The two horizons look at other
values without saving them, as before; a negative one is refused. It serves the
command line's `dashboard`, the browser's Dashboard (whose group editor now lists
accounts from `accounts`, by full name), and GTK's Dashboard view.

Plan versus actuals followed: `activity(start, end, period, as_of)`
(`GET /api/v1/activity`) returns a view from `gen/services/activity` with the engine's
`ActivityReport` and the currency notes that disclose its quotes and exclusions. It
serves the command line's `activity`; GTK and the browser show these figures through
the Plan, which is already on the server.

Two command-line reads followed: `due_occurrences(as_of, days)`
(`GET /api/v1/schedules/due`) lists what is already due for `scheduled` without
`--post`, and `reimbursable_costs(start, end)` (`GET /api/v1/receivables/costs`)
returns the Plan's own reimbursable category rows for `receivables --costs`, so
both work while another process holds the book.

Reading what is typed into a register followed: `suggest_entry` (with the full names
of the suggestion's accounts), `entry_amount`, `entry_trade` (`ReadEntryTrade`),
`entry_accounts`, and `next_num` (`GET /api/v1/entry/...`) serve the browser's
register and entry form and GTK's register and transaction editor, so a proposal,
an arithmetic amount, a filled trade, a completed account, and the next check number
come from the server both ways. Typed dates need no book and stay a local parse.
GTK's register otherwise still reads the book directly.

Security prices followed, with a service of their own: `gen/services/security_prices`
checks a manual price before `engine.valuation` writes it and names each refusal with
a stable code (`security.price.invalid`, `security.symbol.duplicate`, and so on), so a
refused price leaves the book unchanged; until now the engine raised plain errors
and the browser adapter checked the price itself. `securities()` and
`save_security_price` (`GET /api/v1/securities`, `POST /api/v1/security-prices`) serve
the browser's and GTK's security price editors. Both still list the quote currencies
from the book, in book order, since the first is the editor's default.

GnuCash write-back followed: `writeback_preview`, `apply_writeback`,
`writeback_keep_backups`, and `set_writeback_keep_backups`
(`GET`/`POST /api/v1/gnucash/writeback`, `GET`/`PATCH .../settings`) serve the command
line's `gnucash-writeback`, the browser's write-back page, and GTK's write-back
dialog. A previewed change travels without the write it would perform
(`WritebackChange.operation` is left out), so the server recomputes the plan when it
writes, as before.

The browser's book summary and Verify page followed: `book_summary` and `verify_book`
(`GET /api/v1/book/summary` and `/book/verification`) return the book's counts, cash,
net worth (each with its missing quotes), scenarios, and `BookVerification`. The
command line's `verify` and GTK's verification dialog still check the file through a
connection of their own (`DbSQLite.verify_path`), which also works while another
process holds the book.

The browser's register followed: `register_view(account, limit)`
(`GET /api/v1/accounts/{handle}/register-view`) returns the engine's register rows
with the full name of every account their splits use, each transaction's linked
documents, and, for a security account, its shares, valuation, and quote evidence
(`gen/services/register_view`), so the browser's register no longer reads the book;
its simple entry form now names its two accounts through `find_account` and its
currency through `transaction_currency` as well, and its row editor reads the stored
transaction (`find_transaction`) and a split's security (`account`) through the
server.

The Plan presentation every interface restores followed: `plan_settings` and
`save_plan_settings` (`GET`/`PATCH /api/v1/plan/settings`) read and keep the period,
grouping, measure, and scenarios. Saving now refuses a range that ends before it
starts or a scenario the book no longer has (`plan.end.before_start`,
`plan.scenario.not_found`, `plan.comparison.not_found`), so GTK's Plan reports the
refusal instead of storing it. GTK's Plan view and the browser's Plan page use them.

Historical estimates followed: `historical_estimates(months, min_active_months,
scenario, as_of)` and `accept_historical_estimate` (`GET`/`POST
/api/v1/historical-estimates`) come from `gen/services/historical_estimates`, which
now checks an acceptance and names each refusal (`estimate.proposal.unavailable`,
`estimate.months.invalid`, `estimate.scenario.not_found`, and so on); the browser
used to write the engine's estimate directly. Each proposal travels with its draft as
a schedule editor view (`gen/services/schedule_editing`: the definition, its edit
projection, and the class of each account it uses), so the browser's simple-editor
fields come from the view (`simple_parts`) rather than the book. The typed codec now
carries a `float` (a statistic such as a confidence, never an amount) as a JSON
number. They serve the command line's `estimate suggest`, the browser's historical
estimates, and GTK's historical estimates dialog; GTK still opens the proposal in its
own schedule editor, which reads the book.
GTK's register still does; it needs the bulk reads and pushed changes the
consequences below name before it can move without a round trip per row.

Posting without a review gained a service of its own (`due_review.post_due`), so
the browser's post-scheduled and `scheduled --post --json` now answer with the same
`{"posted", "transactions": [{"transaction", "schedule", "date", "description"}]}`.

As a result:

- `due-review --json` now prints exactly what `GET /api/due-review` returns
  (`{"schedules": [...]}`).
- Decisions answer with posted, skipped, deferred, and the transaction handles on
  both interfaces.
- `tests/test_book_server.py` proves both of these on one book.
- Architecture tests keep the moved adapters off the services they no longer call.

Phase 3 (#357) has begun:
- **Stored revision:** the revision is kept with the book (`DbSQLite.revision()`,
  metadata key `book_revision`). It rises in the same SQL transaction as each
  recorded change, undo, or redo, so every process and every read snapshot agrees on
  it.
- **`/api/v1` resources:** the pilot operations are served under `/api/v1`, each
  response carrying its revision as an `ETag`. A write with a stale `If-Match` gets
  412 `book.revision.stale` and changes nothing.
- **Change stream:** `/api/v1/events` streams changes. A client that reconnects
  after missing some first receives a change with no kinds, meaning reload.
- **HTTP client:** `HttpBookClient` implements `BookApi` over HTTP. The book-server
  tests run the same contract against it and the in-process server.

Phase 4 (#358) has begun:
- **Server record:** `breadsched serve BOOK`, and `breadsched web` when writable,
  publish a server record (`breadsched.api.discovery`): one user-only file per
  book in the runtime directory (`$XDG_RUNTIME_DIR/breadsched/servers`, or the
  data directory's `run` folder), named by a hash of the book's canonical path. It
  holds the address, the token, and the process. The token never goes beside the
  book.
- **When a record counts:** the record names the server's writer lock by the
  lock's random token (`DbSQLite.writer_lock_token`). It counts only while the
  book's lock file carries that token and, on this host, the process is alive. Any
  other record is removed. The token, not the host name, identifies the holder,
  because a host name can change while a server runs.
- **Connecting:** `breadsched.api.connect.open_book_api` uses an explicit
  `--server`, else the recorded server, else an in-process server. The command
  line's book-server commands use it, so they work while a server holds the book.

Phase 5 (#359) has begun:
- **GTK on the protocol:** GTK's due review (`DueDialog`, built from
  `book.due_review` and applied with `book.resolve_due`), the Scheduled view's
  Enter Next and Skip Next, and the Upcoming view's review count use the book
  server. `api.local.book_server(db)` gives the one in-process server over the
  book GTK holds.
- **Sharing the GTK book:** with **Preferences → Sharing** on, GTK serves its
  writable book the way `breadsched serve` does and publishes a server record, so
  commands for that book work through the window.
  - **Main-loop executor:** the HTTP server runs on its own thread. Every request
    that uses the book is run on the GTK main loop through the transport's
    executor (`gui.book_sharing.main_loop_executor`), so the book is only ever
    touched there.
  - **Undo:** storage signals, redraw-on-change, and undo behave as for an edit
    made in the window. Undo stays one history per book: a command's change can be
    undone in GTK.

Revisions are whole-book: any change makes every `ETag` stale. Per-record versions
remain open if whole-book conflicts prove too coarse once several interfaces share a
book.

## Storage

The book server was checked against [decision 0001](0001-transaction-storage.md)
(transactions as JSON with a derived split index). The book server makes loading
part of every read request, which strengthens the case against normalized tables.
It also sends stored record forms over the wire, which strengthens the case for a
minimized JSON encoding. The measurements and reasons are in 0001's re-evaluation.

## Serving through a TLS reverse proxy (nginx)

`breadsched serve` and `breadsched web` listen on the loopback interface only, over
plain HTTP. They refuse any other address, and that stays the rule. Reaching a book
from another device, such as a phone or a second computer on the household network,
is reasonable only through a reverse proxy on the same host. The proxy terminates
TLS and passes requests to the loopback port. nginx is the documented example, and
any proxy that can do the same works.

**What protects the book, and what the proxy must keep:**

1. **TLS:** only the proxy is reachable from the network, over TLS 1.2 or later (1.3
   preferred), with HSTS. The book server stays on `127.0.0.1` with a fixed `--port`.
2. **Who may connect:** the per-server token is the book server's only credential,
   and anyone holding it has full use of the book. On a network, the proxy adds an
   authentication of its own. Client certificates (`ssl_verify_client on`) are
   preferred; HTTP basic authentication over TLS with an address allow-list is the
   minimum. A book should never be exposed to the internet.
3. **Host:** nginx's default upstream `Host` is `127.0.0.1:PORT`, which the server's
   Host check (a DNS-rebinding defence) accepts. The proxy's own `server_name`
   takes over that defence for the public name, so a default server that answers
   any other name with 444 is needed.
4. **Origin:** a page served through the proxy sends `Origin: https://books.example.lan`.
   The server accepts loopback origins, and the one public origin it is started with
   (`--trusted-origin https://books.example.lan`); any other origin is refused with
   403. The proxy passes `Origin` through unchanged. Cross-site requests stay blocked
   twice over: the server checks the origin, and it requires the token in a custom
   header, which a foreign page cannot send without a CORS preflight the server never
   grants.
5. **Request limits:** `client_max_body_size 32m` matches the import upload limit
   (the server still enforces its own limits).
6. **Change stream:** `/api/v1/events` must not be buffered, and its read timeout
   must exceed the 15-second keep-alive.
7. **Logs:** the token travels in the `X-BreadSched-Token` header, and in the URL
   fragment only when the page first opens. Fragments are never sent to a server, so
   the proxy must not log request headers.
8. **Other local users:** they can still reach the loopback port and are kept out
   only by the token. A Unix-socket listener readable only by the user would close
   that gap. It is not yet supported (an open item).

An illustrative configuration (replace the names, port, and certificate paths):

```nginx
server {
    listen 443 ssl default_server;
    ssl_reject_handshake on;                       # any other name: no answer (nginx 1.19.4+)
}

server {
    listen 443 ssl;
    http2 on;
    server_name books.example.lan;
    ssl_certificate     /etc/nginx/tls/books.example.lan.pem;
    ssl_certificate_key /etc/nginx/tls/books.example.lan.key;
    ssl_protocols TLSv1.3;
    ssl_client_certificate /etc/nginx/tls/household-clients-ca.pem;
    ssl_verify_client on;                          # client certificates
    add_header Strict-Transport-Security "max-age=31536000" always;
    client_max_body_size 32m;
    access_log /var/log/nginx/breadsched.log;      # the default format logs no headers

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
    }

    location = /api/v1/events {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}
```

Start the server with:

```bash
breadsched serve BOOK --port 8765 --trusted-origin https://books.example.lan \
    --token-file ~/.config/breadsched/books.token
```

The token is kept in that file (created `0600` if absent), is not printed, and stays
the same across restarts. Open `https://books.example.lan/#token=TOKEN` in the
browser. Without `--token-file`, the printed address carries a new token each start.

From another machine's command line, name the proxy as the server:

```bash
breadsched balance BOOK --server https://books.example.lan --token-file books.token \
    --ca-file household-ca.pem --client-cert laptop.pem --client-key laptop.key
```

`HttpBookClient` verifies the proxy's certificate (against `--ca-file`, or the
system's authorities), requires TLS 1.2 or later, and presents the client
certificate the proxy asks for. On the serving host itself, commands keep using the
loopback address from the server record, so the proxy is never in their way.

**Still open:** a Unix-socket listener readable only by the user, to close the
local-user gap in item 8. The proxy would then reach the server through the socket
(`proxy_pass http://unix:/run/user/1000/breadsched/books.sock`).

**Linked documents over HTTP.** A server reached over HTTP (the browser's routes and
`/api/v1` alike) links or relinks a document only by web address or inside the
attachment folder (`contained_location`), so a token holder cannot link a file such
as `~/.ssh/id_ed25519` and then read it back through the document route; only an
in-process caller (GTK, or a command with no server) may link any file.
**Still open:** copying a file in (`attach_file`,
`POST /api/v1/transactions/{handle}/attachments/files`) still reads a path as the
server sees it, which the command line's `attachments --add FILE` through a local
server needs. Through a proxy, a token holder could copy in any file the server's
user can read. Until that is narrowed (for example, refusing it for requests that
arrive through a trusted origin, or uploading the file's bytes instead), give the
token only to the user's own devices.

## Consequences and open questions

- **JSON shapes change** for the moved commands, so scripts that read the old
  `due-review --json` list must read `schedules` instead. Each area that moves
  changes the same way, and the release that brings this to `main` must say so.
- **Undo**: today one undo history belongs to the book. Shared editing must decide
  whether undo stays global or becomes per client.
- **Security**: an independent server stays on the loopback interface, with a token
  file only the user can read. Anything wider needs real authentication and TLS.
- **Long operations** (imports, quote fetches, projections) need jobs with progress
  and cancellation once a client is a separate process.
- **Flatpak**: a sandboxed GTK client reaching a server outside the sandbox needs
  network permission; a server inside it needs portal access to the book.
- **Speed**: in process, the protocol adds a function call; over HTTP, the GTK
  register needs bulk reads and pushed changes, not a round trip per keystroke.
