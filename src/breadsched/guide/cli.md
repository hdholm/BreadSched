# Command-line guide

This part covers the `breadsched` command. What each feature does, and the rules
behind it, is in the [overview](../USER_GUIDE.md). Most of this work can also be
done in the [Desktop guide](desktop.md) or the [Browser guide](web.md).

Every command takes the book path first. Many commands support `--json` for
structured output, and `-v` (or `-vv`) logs progress and warnings to stderr. Use
`breadsched --help` and `breadsched COMMAND --help` for the exact command surface in
your installed release.

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
```

Start the other interfaces on the same book with `breadsched gui household.breadsched`
([Desktop guide](desktop.md)) or `breadsched web household.breadsched`
([Browser guide](web.md)).

`breadsched dashboard --json` lists every missed date and amount of a schedule
shown once on the Dashboard (`missed_bills` and `missed_income`). `breadsched
activity` prints the foreign-currency note after the Plan activity; `--as-of DATE`
chooses which exchange rates apply, and its JSON adds `conversions` and
`unconverted`. Account output identifies whether a foreign balance used a direct or
an inverse rate.

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

## Estimates and schedules

```bash
breadsched estimate household.breadsched suggest --json
breadsched scheduled household.breadsched
```

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
are waiting after the import.

### Import a CSV statement

Refer to a column by its header name, or by number (1 for the first) with
`--no-header`. Always preview first:

```bash
breadsched import-csv household.breadsched statement.csv --account Checking \
    --date Date --amount Amount --description Description --preview
```

Run the same command without `--preview` to import. Other options:

- `--debit` and `--credit` instead of one signed `--amount` column; `--memo`;
- `--category`, `--payee`, and `--currency` for the optional columns;
- `--include-duplicates` imports possible duplicates instead of holding them back;
- `--link-transfers` turns each possible transfer into one transfer between the two
  accounts;
- `--date-format day-first` or `month-first` when every date is ambiguous;
- `--number-format`, `--encoding`, and `--delimiter` override detection, and
  `--invert` flips exports that show money out as a positive number.

See [Import a CSV statement](../USER_GUIDE.md#import-a-csv-statement).

### Review held GnuCash changes

```bash
breadsched import-review household.breadsched            # list held changes
breadsched import-review household.breadsched --keep TRANSACTION
breadsched import-review household.breadsched --use-gnucash TRANSACTION
breadsched import-review household.breadsched --keep-all  # or --use-gnucash-all
```

### Write changes back to GnuCash

Close the book in GnuCash first. Preview, then write chosen transactions or all:

```bash
breadsched gnucash-writeback household.breadsched
breadsched gnucash-writeback household.breadsched --apply 1a2b3c4d
breadsched gnucash-writeback household.breadsched --all
```

Choose how many backups to keep with `--keep-backups N` (10 by default). See
[Write changes back to a GnuCash SQLite book](../USER_GUIDE.md#write-changes-back-to-a-gnucash-sqlite-book).

## Payees

```sh
breadsched payees book.breadsched --add "Corner Grocer" --match "CORNER GROCER #1234"
breadsched payees book.breadsched --preview
breadsched payees book.breadsched --accept-all       # or --accept TRANSACTION
breadsched payees book.breadsched                    # list payees and their counts
breadsched payees book.breadsched --delete "Corner Grocer"
```

`--match` takes an example description; the preview shows the matched key for each
proposal and writes nothing.

## Categorization rules

```sh
breadsched rules book.breadsched --add-description "CORNER GROCER #1234" --category "Expenses:Groceries"
breadsched rules book.breadsched --add-payee "City Power" --category "Expenses:Utilities" --position 1
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
```

Link the split that records the cost with `--attach-expense`, and the split that
credits money back (a deposit's other split posted to the same expense account,
exactly like an ordinary refund) with `--attach-reimbursement`; `--split-index` is
the split's 1-based position within `--transaction`. `--account` chooses the
Receivable account; without it BreadSched uses the default for the expense's
currency. The list shows what is owed, the account, and any FSA-claim overlap, and
warns under the table when an expense is also on an FSA claim. When an FSA claim
covers the rest of a receivable's bill (linked on the claim in the desktop or
browser), a line under the table shows what the payer, the FSA, and you each pay,
and `--json` lists it under `shared_costs`. See
[Reimbursable expenses](../USER_GUIDE.md#reimbursable-expenses).

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
```

The list shows each goal's account, target date, target, what is set aside, what
remains, and its status: **saving**, **fully set aside**, **starts** a later date, or
**closed**. **saving (spread by day: no income scheduled)** means no income is
scheduled before the target date, so the gap is spread by day. Below the table are the total
set aside and the part held from spendable cash. A goal is named by its exact name
or a unique handle prefix. `breadsched dashboard` shows the same total as **Set
aside for goals**. See [Savings goals](../USER_GUIDE.md#savings-goals).

## Projection and scenarios

```bash
breadsched project household.breadsched --years 10
breadsched scenario household.breadsched save --name "Lower returns" --parent Base \
    --investment-return 0.04
breadsched scenario household.breadsched list
breadsched compare household.breadsched Base "Lower returns"
```

`scenario` also reparents and deletes scenarios; `compare` compares two saved
scenarios year by year. See
[Projection and scenarios](../USER_GUIDE.md#projection-and-scenarios).

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
`<book>.pre-migration-v<old schema>.bak`, and `--json` reports it.

## Troubleshooting

- The command line works without GTK; if the desktop application will not start,
  see [Start the desktop application](desktop.md#start-the-desktop-application).
- Add `-v` or `-vv` to a command to see its progress and warnings.
