# Interoperability and source ownership

Part of the [BreadSched design](../../DESIGN.md).

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

## Ownership on re-import

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

GnuCash records a share split as one split that changes shares with no value. A
BreadSched transaction needs two splits, so the importer adds a zero-value,
zero-quantity leg in `Equity:Share splits` (creating it, and a placeholder
`Equity` if the book has none) with `Split.importer_added` set and a handle derived
from the source GUID, kept on re-import, so a refresh compares equal. Write-back
leaves importer-added splits out of the target transaction and out of its
round-trip comparison (`written_facts`), so GnuCash keeps its one split, and an
edit to the transaction's other facts still writes back. `importer_added` is
serialized only when set, so every other split's stored form is unchanged.
`ImportResult.share_splits` counts them. A lone split with neither value nor
shares is still skipped as "only one split, with no value".

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

## Locally reconciled imported transactions

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

## GnuCash write-back

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

- Importer-added splits (a share split's balancing leg) are never written or
  compared.
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

## Statement formats

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

Optional `category` and `currency` columns resolve against the book
without inventing anything (`_Resolver`). A category matches a non-placeholder
account other than the target by full name, else by a name exactly one such
account has; no match or several make the row invalid with that reason, and an
empty cell keeps the Uncategorized CSV placeholder. A currency code other than the
target account's currency (or the reporting currency for an account without one)
makes the row invalid. The resolved category replaces the placeholder counter
split, and a row with a category is never offered as a possible transfer.
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

## Exchange rates entered by hand

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

## Format choices, uploads, and dates

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

## Import date integrity

Required source dates are never synthesized. Missing or malformed posting/start dates are reported against the source record and skipped rather than silently using the current date. Optional dates remain optional.

## Direct bank connections

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

