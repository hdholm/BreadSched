# Command-line guide

This part covers the `breadsched` command. What each feature does, and the rules
behind it, is in the [overview](../USER_GUIDE.md). Most of this work can also be
done in the [Desktop guide](desktop.md) or the [Browser guide](web.md).

Every command takes the book path first. Many commands support `--json` for
structured output, and `-v` (or `-vv`) logs progress and warnings to stderr. Use
`breadsched --help` and `breadsched COMMAND --help` for the exact command surface in
your installed release; `breadsched --help` lists the commands grouped by area
(book, import, ledger, Plan, claims and goals, projection).

On Windows, the installer puts `breadsched.cmd` in its installation folder. Run it
from there, or choose **Add the breadsched command to PATH** when installing (see
[Install on Windows](desktop.md#install-on-windows)) to type `breadsched` anywhere.

## Read this guide

```bash
breadsched guide              # the overview
breadsched guide desktop      # or web, or cli
breadsched guide --list       # the parts and their titles
```

The command prints the guide packaged with this release, so it matches the
installed BreadSched version. It needs no book.

## Create and inspect books

```bash
breadsched init household.breadsched
breadsched accounts household.breadsched
breadsched register household.breadsched "Assets:Checking Account" --limit 20
breadsched balance household.breadsched --as-of 2026-06-30
breadsched dashboard household.breadsched --json
breadsched activity household.breadsched --start 2026-06-01 --end 2026-12-31 \
    --as-of 2026-06-30
breadsched holdings household.breadsched --lots   # lots, sales, transfers, splits
breadsched realized-gains household.breadsched --year 2026   # sales, lots, totals
breadsched sale-lots household.breadsched 3f2a91c0 # lots a sale could sell
breadsched sale-lots household.breadsched 3f2a91c0 --lot 9be41d07=5   # name lots
breadsched sale-lots household.breadsched 3f2a91c0 --clear   # the account's method
breadsched jars household.breadsched --start 2026-01-01 --end 2026-06-30 \
    --period quarter                              # fills, draws, and levels
breadsched tax-year household.breadsched --year 2026   # gains by term, totals, income
breadsched tax-marks household.breadsched --account "Expenses:Medical=on" \
    --tag "Charity=on"                            # off, or gnucash for an account
breadsched account household.breadsched edit --name "Assets:Brokerage" \
    --cost-basis average                          # or fifo (the default)
```

Start the other interfaces on the same book with `breadsched gui household.breadsched`
([Desktop guide](desktop.md)) or `breadsched web household.breadsched`
([Browser guide](web.md)).

`breadsched dashboard --json` lists every missed date and amount of a schedule
shown once on the Dashboard (`missed_bills` and `missed_income`). `breadsched
activity` ends with planned cash, actual cash, and variance through the as-of date
(see [Reporting terms](../USER_GUIDE.md#reporting-terms)), then the foreign-currency
note; `--as-of DATE` sets that date and chooses which exchange rates apply. Its JSON
adds `planned_cash_through_as_of`, `actual_cash_through_as_of`,
`cash_variance_through_as_of`, `conversions`, and `unconverted`. Account output identifies whether a foreign balance used a direct or
an inverse rate.

Reports that leave out an amount without a quote say so as text (see
[Complete, partial, and unavailable values](../USER_GUIDE.md#complete-partial-and-unavailable-values)). `activity` and `project` print the partial label and each excluded
amount with the fix; `dashboard`, `net-worth`, and `net-worth-change` print what a
withheld total leaves out; `compare` adds a coverage column. In `--json`, the
activity report, each activity period, projection and its rows, dashboard and its
groups, and each net worth point and change carry a `completeness` object with
`status` (`complete`, `partial`, or `unavailable`), `policy`, `label`, `detail`,
and each `excluded` input's account or event, currency, amount, date, missing
quote kind, and action. `project --csv` ends each month with its status.

### Synthetic sample book

```bash
breadsched sample sample.breadsched --as-of 2026-09-15
```

The command refuses an existing path. `--as-of YYYY-MM-DD` chooses a repeatable
reference month. See [Synthetic sample book](../USER_GUIDE.md#synthetic-sample-book).

## Net worth history

```bash
breadsched net-worth household.breadsched --start 2026-01-01 --end 2026-12-31 \
    --period month --as-of 2026-06-30
```

Each row is one period end (or the `--as-of` date within the last period), with
assets, debts, net worth, the change, and a note naming any account whose quote
was missing. `--json` adds each point's top-level account values. See
[Net worth history](../USER_GUIDE.md#net-worth-history).

```bash
breadsched net-worth-change household.breadsched --start 2026-06-01 --end 2026-06-30 \
    --csv june-change.csv
```

`net-worth-change` lists the transactions that changed net worth from `--start`
through `--end` (or `--as-of`), then the opening and closing net worth, the postings
total, and the market and exchange-rate changes that reconcile them. `--csv` writes
the same rows and totals.

## Exchange rates

```bash
breadsched rate household.breadsched --from EUR --to USD --date 2026-01-02 --value 1.25
```

The value means target units per one source unit. Use `--json` to obtain the exact
rational rate, date, source, and quote handle. An ambiguous currency code requires
its exact handle. JSON output also carries the signed number of days since each
quote (`quote_age_days`), with negative days identifying a future-dated quote.

## Online quotes

```bash
breadsched quote-source household.breadsched G tsp        # fetch G Fund prices
breadsched quote-source household.breadsched VTSAX alphavantage
breadsched quote-source household.breadsched GBP currency
breadsched quotes household.breadsched --list             # what would be fetched
breadsched quotes household.breadsched --dry-run          # fetch, show, store nothing
breadsched quotes household.breadsched                    # fetch and store
```

`quote-source` with no source turns online quotes off for that commodity. Any
source other than `tsp`, `alphavantage`, or `currency` is a Finance::Quote method
and needs Finance::Quote installed outside the Flatpak; `--list` says whether it
is. Set the Alpha Vantage key as `alphavantage_api_key` in the `[quotes]` section of
`settings.ini`, or in `ALPHAVANTAGE_API_KEY`. `quotes` exits with status 1 when
every source failed. See
[Online quotes](../USER_GUIDE.md#online-quotes) for the rules.

## Estimates and schedules

```bash
breadsched estimate household.breadsched suggest --json
breadsched scheduled household.breadsched
```

### Paychecks and pay changes

```bash
breadsched payroll household.breadsched
breadsched payroll household.breadsched --show "Acme paycheck" --as-of 2026-04-10
breadsched payroll household.breadsched --save-template Acme --from-schedule "Acme paycheck"
breadsched payroll household.breadsched --create "Acme paycheck 2" --template Acme \
    --start 2026-10-02 --every week --interval 2 --gross 3100
breadsched payroll household.breadsched --pay-change "Acme paycheck" --gross 3300 \
    --start 2026-04-10 --scale "Expenses:Taxes:Federal" --set "Expenses:Health=90" --preview
```

With no option, `breadsched payroll` lists every paycheck (gross, withheld, net,
take-home) and the saved templates. `--pay-change` scales the taxes by default; each
`--scale ACCOUNT` replaces that default with the accounts named, and `--set
ACCOUNT=AMOUNT` sets a line. Leave out `--preview` to save. `--json` gives the same
data. See [Paychecks and pay changes](../USER_GUIDE.md#paychecks-and-pay-changes).

### Review actual transactions

```bash
breadsched review household.breadsched
breadsched review household.breadsched --transaction 3f2a --json
```

`breadsched review` lists each actual transaction waiting for a decision, the
planned items Review offers with **Close match** or **Possible match** and the
reasons, or why nothing is offered, and what each action does. Decide in the desktop
application or the browser.

### Review due transactions

```bash
breadsched due-review household.breadsched
breadsched due-review household.breadsched --post Rent@2026-09-01 --skip Gym
breadsched due-review household.breadsched --post-all
```

`--post` and `--skip` take a schedule id prefix or exact name, optionally with
`@YYYY-MM-DD` for one date; `--post-all` and `--skip-all` decide everything due. See
[Review due and missed transactions](../USER_GUIDE.md#review-due-and-missed-transactions).

## Import files

```bash
breadsched import household.breadsched accounts.gnucash
breadsched import household.breadsched statement.qfx
```

GnuCash, QIF, and OFX/QFX files are detected from their contents; `--format`
chooses the importer when detection cannot. QIF and OFX/QFX decimal and date
conventions are inferred from the whole file; when a file is too ambiguous for that,
import it from the [desktop](desktop.md#import-files) or [browser](web.md#import-files),
which offer explicit choices. JSON output includes how many reimbursement proposals
are waiting after the import, `problems` (each skip reason with its `count` and up
to three `examples`), `notices` (warnings not about one skipped record), and
`possible_duplicates`: QIF and OFX rows held back because
the account already has a transaction on that date for that amount;
`--include-duplicates` imports them.

### Import a CSV statement

Refer to a column by its header name, or by number (1 for the first) with
`--no-header`. Always preview first:

```bash
breadsched import-csv household.breadsched statement.csv --account Checking \
    --date Date --amount Amount --description Description --preview
```

Run the same command without `--preview` to import. Other options:

- `--debit` and `--credit` instead of one signed `--amount` column; `--memo`;
- `--category` and `--currency` for the optional columns;
- `--split CATEGORY=AMOUNT` maps one split's category and amount columns, repeated
  for each split (instead of `--category`), for example
  `--split "Cat 1=Amt 1" --split "Cat 2=Amt 2"`. The preview shows each row's
  splits, and `--json` lists them under `splits`;
- `--include-duplicates` imports possible duplicates instead of holding them back;
- `--link-transfers` turns each possible transfer into one transfer between the two
  accounts;
- `--date-format day-first` or `month-first` when every date is ambiguous;
- `--number-format`, `--encoding`, and `--delimiter` override detection, and
  `--invert` flips exports that show money out as a positive number.

For a statement downloaded and exported with `aqbanking-cli` (see
[Bank downloads through AqBanking](../USER_GUIDE.md#bank-downloads-through-aqbanking)):

```bash
breadsched import-csv household.breadsched statement.csv --account Checking \
    --date date --amount value_value --description remoteName --memo purpose --preview
```

See [Import a CSV statement](../USER_GUIDE.md#import-a-csv-statement).

### Review held GnuCash changes

```bash
breadsched import-review household.breadsched            # list held changes
breadsched import-review household.breadsched --keep TRANSACTION
breadsched import-review household.breadsched --use-gnucash TRANSACTION
breadsched import-review household.breadsched --keep-all  # or --use-gnucash-all
```

A held GnuCash deletion is listed as `Deleted in GnuCash` (`"deleted": true` with
`--json`); `--use-gnucash` deletes the transaction here too, and `--keep` keeps it.

### Write changes back to GnuCash

Close the book in GnuCash first. Preview, then write chosen transactions or all:

```bash
breadsched gnucash-writeback household.breadsched
breadsched gnucash-writeback household.breadsched --apply 1a2b3c4d
breadsched gnucash-writeback household.breadsched --all
```

Choose how many backups to keep with `--keep-backups N` (10 by default). See
[Write changes back to a GnuCash book](../USER_GUIDE.md#write-changes-back-to-a-gnucash-book).

## Tags and linked documents

```sh
breadsched tags book.breadsched --transaction 3f2a --set "Tax, Home repair"
breadsched tags book.breadsched                      # every tag and its count
breadsched tags book.breadsched tax                  # transactions tagged Tax
breadsched register book.breadsched Checking --tag tax
breadsched attachments book.breadsched --transaction 3f2a --add ~/Scans/receipt.pdf
breadsched attachments book.breadsched --transaction 3f2a --add https://example.com/invoice
breadsched attachments book.breadsched --missing     # documents that cannot be found
breadsched attachments book.breadsched --transaction 3f2a \
    --relink receipt.pdf --to ~/Archive/receipt.pdf
breadsched attachments book.breadsched --transaction 3f2a --remove receipt.pdf
```

`--set ""` removes every tag. `--add` copies a file into the attachment folder;
add `--link` to link it where it is instead. `--remove` unlinks a document and
keeps the file. `--folder DIR` moves where relative documents are looked up (a
relative folder is taken from the book's folder; `--folder ""` restores the
default). `--gnucash-folder DIR` sets where relative GnuCash linked documents live,
matching GnuCash's *Path head for linked files*. The register's `--tag` filter
keeps the full running balance, and its `--json` rows include each transaction's
tags and document count.

## Categorization rules

```sh
breadsched rules book.breadsched --add-description "CORNER GROCER #1234" --category "Expenses:Groceries"
breadsched rules book.breadsched --add-description "City Power" --category "Expenses:Utilities" --position 1
breadsched rules book.breadsched                     # list rules in priority order
breadsched rules book.breadsched --preview           # proposals, deciding rule, conflicts
breadsched rules book.breadsched --accept-all        # or --accept TRANSACTION
breadsched rules book.breadsched --move 2 --to 1
breadsched rules book.breadsched --delete 2
```

## Reimbursable expenses

```sh
breadsched receivables book.breadsched --add "Acme Insurance" --incurred 2026-09-01 \
    --description "Doctor visit" --expected 150.00 [--account "Assets:Owed to me"]
breadsched receivables book.breadsched --attach-expense RECEIVABLE \
    --transaction TRANSACTION --split-index 1
breadsched receivables book.breadsched --attach-reimbursement RECEIVABLE \
    --transaction TRANSACTION --split-index 2
breadsched receivables book.breadsched --dispute RECEIVABLE --on 2026-09-20 \
    --note "Insurer denied the claim"
breadsched receivables book.breadsched --clear-dispute RECEIVABLE
breadsched receivables book.breadsched --write-off RECEIVABLE --amount 25.00 \
    --on 2026-10-01 --reason "Deductible"
breadsched receivables book.breadsched                # list with status and age
breadsched receivables book.breadsched --delete RECEIVABLE
breadsched receivables book.breadsched --proposals         # credits that look like money back
breadsched receivables book.breadsched --accept-proposals  # link every current proposal
breadsched receivables book.breadsched --costs 2026-01-01 2026-12-31  # gross and net cost
breadsched receivables book.breadsched --scenario Careful --expect RECEIVABLE \
    --amount 90 --on 2026-12-15   # in that scenario only; --amount 0 for nothing
breadsched receivables book.breadsched --scenario Careful --expect RECEIVABLE  # undo
```

`project` lists each expected reimbursement's gross and net cost in its notes and,
with `--json`, under `reimbursements`.

Link the split that records the cost with `--attach-expense`, and the split that
credits money back (a deposit's other split posted to the same expense account,
exactly like an ordinary refund) with `--attach-reimbursement`; `--split-index` is
the split's 1-based position within `--transaction`. `--account` chooses the
Receivable account; without it BreadSched uses the default for the expense's
currency. The list shows what is owed, the account, and any FSA-claim overlap, and
warns under the table when an expense is also on an FSA claim. When an FSA claim
covers the rest of a receivable's bill (linked on the claim in the desktop or
browser), a line under the table shows what the payer, the FSA, and you each pay,
and `--json` lists it under `shared_costs`.

`--costs START END` lists each expense category a receivable changed between two
dates with its gross cost, the amount reimbursed or expected back, and the net
household cost, the figures the Plan shows (`--json` gives `gross`,
`reimbursable`, and `net`). See
[Reimbursable expenses](../USER_GUIDE.md#reimbursable-expenses).

## FSA claims

`breadsched account BOOK edit --name "Assets:Dependent care FSA" --dependent-care yes`
marks a dependent care FSA (see
[FSA and benefit accounts](../USER_GUIDE.md#fsa-and-benefit-accounts)); the FSA year
listing (`claims BOOK --years`) marks it, and its `--json` adds `dependent_care`.

`breadsched claims BOOK` totals FSA claims by status, then lists each claim that
needs attention and why (no EOB after 30 days, a claim deadline within 30 days, a
rejected reimbursement, or figures that need review). `--by account`, `--by year`,
or `--by provider` groups them another way; `--account`, `--year` (a funding
year's start date), `--provider`, and `--status` narrow the list; `--attention`
keeps only claims needing attention; `--as-of` reports on another day; `--json`
gives each claim with its `attention` codes and text, and what was repaid to the
FSA, is still to repay, or was given up. `breadsched dashboard` shows how many
claims need attention.

`breadsched claims BOOK --proposals` lists FSA transactions, such as imported
statement lines, that clearly belong on one claim, with the claim, role, and why;
`--link-proposals` links them all. `--account` narrows either to one account.

`breadsched claims BOOK --years` lists open and recently closed FSA benefit years:
the election, what was funded and used, **How used** (paid from the card,
reimbursed, refunded to the card, repaid), and what remains. `--account` and
`--as-of` apply, and `--json` gives each part as a separate field.

```sh
breadsched claims BOOK --close 3f2a --on 2026-05-01 --reason "Not worth appealing"
breadsched claims BOOK --reopen 3f2a --note "Appeal won"
breadsched claims BOOK --history 3f2a
```

`--close` stops pursuing what is left to reimburse on a claim (named by its handle
or the start of one), `--reopen` takes it up again, and `--history` lists the
claim's EOB changes, closings, and reopenings.

## Savings goals

```sh
breadsched goals book.breadsched --add "New roof" --account "Assets:Savings" \
    --target 12000 --by 2027-06-30 [--start 2026-07-01] [--description "Metal roof"]
breadsched goals book.breadsched --allocate "New roof" --amount 1500 --on 2026-09-01 \
    [--memo "Bonus"]
breadsched goals book.breadsched [--as-of 2026-09-15] [--all] [--json]
breadsched goals book.breadsched --close "New roof" --on 2027-07-10
breadsched goals book.breadsched --reopen "New roof"
breadsched goals book.breadsched --delete "New roof"
breadsched goals book.breadsched --override "New roof" --scenario Lean \
    [--target 15000] [--by 2027-12-31] [--leave-out]
breadsched goals book.breadsched --override "New roof" --scenario Lean \
    --buy-on 2028-01-15 --buy-into "Expenses:Home repair"
```

The list shows each goal's account, target date, target, what is set aside, what
remains, and its status: **saving**, **fully set aside**, **starts** a later date, or
**closed**. **saving (spread by day: no income scheduled)** means no income is
scheduled before the target date, so the gap is spread by day. Below the table are the total
set aside and the part held from spendable cash. A goal is named by its exact name
or a unique handle prefix. `breadsched dashboard` shows the same total as **Set
aside for goals**. `--override` changes a goal in one saved scenario; with neither
`--target`, `--by`, `--buy-on`, nor `--leave-out`, it clears the change. `--buy-on`
with `--buy-into` models the purchase in that scenario. `breadsched project`
prints each goal's milestone and the first month cash stops covering goal money,
and its `--json` output adds `goal_milestones` and per-month `goals_set_aside`,
`goals_held`, and `cash_after_goals`. See
[Savings goals](../USER_GUIDE.md#savings-goals).

## Projection and scenarios

```bash
breadsched project household.breadsched --years 10
breadsched scenario household.breadsched save --name "Lower returns" --parent Base \
    --investment-return 0.04
breadsched scenario household.breadsched list
breadsched compare household.breadsched Base "Lower returns"
```

`project --bridge` adds the Cash, Investments, Debts, and Net worth bridges across
the whole projection, and `--bridge 2027-03` those for one month; with `--json` they
are under `bridges` (see
[How a projection reconciles](../USER_GUIDE.md#how-a-projection-reconciles)).
`scenario` also reparents and deletes scenarios; `compare` compares two saved
scenarios year by year and then says which one's cash lasts longer. `project` ends
with the [cash runway](../USER_GUIDE.md#cash-runway) (under `runway` with `--json`).
See
[Projection and scenarios](../USER_GUIDE.md#projection-and-scenarios).

`drawdown` lists, saves, and removes a saved scenario's
[retirement drawdowns](../USER_GUIDE.md#retirement-drawdown):

```bash
breadsched drawdown household.breadsched save --scenario "Retire at 62" \
    --account "Assets:Retirement 401k" --into Assets:Checking \
    --start 2036-07-01 --annual-amount 42000
breadsched drawdown household.breadsched save --scenario "Retire at 62" \
    --account Assets:Brokerage --into Assets:Checking --start 2036-07-01 \
    --annual-rate 4% --end 2050-12-31
breadsched drawdown household.breadsched list --scenario "Retire at 62"
breadsched drawdown household.breadsched remove --scenario "Retire at 62" --handle 1a2b3c4d
```

`--level` keeps a fixed amount from rising with inflation; `--handle` with `save`
replaces an existing drawdown.

## Walkthrough: set up a household

These commands build a small, invented household from an empty book: accounts with
opening balances, a credit card paid from checking, monthly income and spending
estimates, a savings goal, and a five-year Base scenario. Run them in an empty
folder; every name and amount is an example to replace with your own. See
[Walkthrough: set up a household](../USER_GUIDE.md#walkthrough-set-up-a-household).

```bash
breadsched init household.breadsched
breadsched account household.breadsched add --name Checking --type BANK --parent Assets \
    --opening 4200 --opening-date 2026-09-30
breadsched account household.breadsched add --name Savings --type BANK --parent Assets \
    --opening 12000 --opening-date 2026-09-30
breadsched account household.breadsched add --name "Retirement 401k" --type RETIREMENT \
    --parent Assets --opening 85000 --opening-date 2026-09-30
breadsched account household.breadsched add --name "Credit Card" --type "CREDIT CARD" \
    --parent Liabilities --opening -650 --opening-date 2026-09-30 \
    --payment-account Assets:Checking --payment-day 20 --usual-payment 650
breadsched account household.breadsched add --name Salary --type INCOME --parent Income
breadsched account household.breadsched add --name Groceries --type EXPENSE --parent Expenses
breadsched account household.breadsched add --name Housing --type EXPENSE --parent Expenses
breadsched account household.breadsched add --name Utilities --type EXPENSE --parent Expenses
breadsched estimate household.breadsched add --name Pay --account Income:Salary \
    --funded-from Assets:Checking --amount -5200 --every month --start 2026-10-25
breadsched estimate household.breadsched add --name Rent --account Expenses:Housing \
    --funded-from Assets:Checking --amount 1850 --every month --start 2026-10-01
breadsched estimate household.breadsched add --name Groceries --account Expenses:Groceries \
    --funded-from Assets:Checking --amount 650 --every month --start 2026-10-05
breadsched estimate household.breadsched add --name Utilities --account Expenses:Utilities \
    --funded-from Assets:Checking --amount 240 --every month --start 2026-10-12
breadsched goals household.breadsched --add "New roof" --account Assets:Savings \
    --target 9000 --by 2027-09-30 --start 2026-10-01
breadsched scenario household.breadsched save --name Base --years 5 --start 2026-10-01
breadsched dashboard household.breadsched --as-of 2026-10-06
breadsched activity household.breadsched --start 2026-10-01 --end 2026-12-31 --as-of 2026-10-06
breadsched project household.breadsched --scenario Base
```

An opening balance is the account's own value on the opening date: a negative
balance on a liability is money owed. An estimate's `--amount` is what each
occurrence posts to `--account`, so an expense is positive and income is negative.
Estimates feed the Plan, Dashboard, and Projection but never post; record what
actually happens with `add`, an import, or Review. Loans (with principal and
interest) and transfers into savings or retirement are scheduled in the
[desktop](desktop.md#scheduled-activity) or [browser](web.md#scheduled-activity).

## Walkthrough: compare scenarios

Continuing in the same folder, this saves a scenario under Base with lower returns,
higher inflation, and a later roof, then compares the two year by year. See
[Walkthrough: compare scenarios](../USER_GUIDE.md#walkthrough-compare-scenarios).

```bash
breadsched scenario household.breadsched save --name "Lower returns" --parent Base \
    --years 5 --start 2026-10-01 --investment-return 0.03 --inflation 0.035
breadsched goals household.breadsched --override "New roof" --scenario "Lower returns" \
    --by 2028-03-31
breadsched project household.breadsched --scenario "Lower returns"
breadsched compare household.breadsched Base "Lower returns"
```

A child scenario inherits everything from its parent except what it changes; Base
itself is unchanged. `compare` lists each year's net worth in both scenarios and the
difference.

## Protect and recover a book

```bash
breadsched verify household.breadsched
breadsched migrate household.breadsched
breadsched backup household.breadsched household.backup
breadsched restore household.backup restored-household.breadsched
breadsched --version
```

`breadsched --version` shows the application version and the native schema version.
After upgrading to an alpha with a newer schema, read-only commands such as
`verify` and `accounts` refuse an older book instead of changing it; run
`breadsched migrate` once (or open the book in the desktop application) to bring it
to the current schema. It first writes a verified backup next to the book, named
`<book>.pre-migration-v<old schema>.bak` (in the Flatpak, for a book reached only
through the file chooser, under
`~/.var/app/org.breadsched.BreadSched/data/breadsched/beside-documents/`), and
prints where; `--json` reports it as `backup`.

## Troubleshooting

- The command line works without GTK; if the desktop application will not start,
  see [Start the desktop application](desktop.md#start-the-desktop-application).
- Add `-v` or `-vv` to a command to see its progress and warnings.
