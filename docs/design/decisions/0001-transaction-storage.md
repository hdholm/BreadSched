# 0001: Keep transactions as JSON with a derived split index

Part of the [BreadSched design](../../../DESIGN.md); see
[Storage, transactions, and recovery](../storage.md).

- **Status:** accepted, 2026-10-08
- **Decision:** keep each transaction as one JSON blob in `txn`, with
  `split_index` derived from it in the same database change. Do not move to
  normalized transaction and split tables now.

## Context

The roadmap asked for normalized `txn`/`split` tables (typed columns, foreign
keys, CHECK constraints) to be built and measured on a separate branch, and merged
only if they are no worse than 5% on size and speed and measurably better on at
least one, or remove real complexity, such as keeping `split_index` in step with
the transaction blob.

## Measurement

`scripts/storage_benchmark.py` writes the same synthetic 20-year household history
to both layouts with the SQL the storage layer would use, checks that each layout
returns every transaction exactly as written, and times each operation (best of
seven runs; median of 50 single edits). The history has 30,000 transactions,
about 15% of them four- or five-split paychecks and the rest two-split purchases
across 60 accounts, with older entries reconciled. The candidate keeps the
transaction's typed fields in columns and its rarely used, BreadSched-owned fields
in a JSON `extra` column per row. The benchmark measures storage layouts only:
the undo journal and verification apply equally to either. Measured with SQLite
3.45.1 and Python 3.11 on a 4-core Linux container:

| measure | blob + split_index (current) | normalized tables (candidate) | candidate / current |
|---|---:|---:|---:|
| file size (MiB) | 52.629 | 29.945 | 0.57 |
| bulk write (s) | 1.376 | 1.934 | 1.41 |
| load every transaction (s) | 1.146 | 1.927 | 1.68 |
| busiest register (s) | 0.972 | 1.026 | 1.06 |
| account balance (ms) | 50.969 | 51.523 | 1.01 |
| one edit commit (ms, median) | 1.624 | 2.219 | 1.37 |

Single runs vary by up to about 30% on writes. Load-every-transaction and the
size difference were stable across runs.

## Decision and reasons

- **Speed fails the bar.** Loading every transaction, which Projection, Plan,
  reports, verification, and export do through `iter_transactions`, is about 1.5
  to 1.7 times slower, and bulk writes and single edits were slower in most runs. Assembling each `Transaction` from a row per split costs more
  in Python than decoding one JSON document. Registers and balances, which already
  use `split_index`, are unchanged within noise.
- **Size is the one clear gain** (43% smaller), but it comes mostly from JSON
  repeating every key in every blob, which a more compact JSON encoding could reduce
  without a new layout (see below).
- **Complexity is not removed.** Normalizing would retire `split_index`
  synchronization but add row assembly, a column-to-field mapping that every model
  change must update, a JSON `extra` column for the many BreadSched-owned fields
  that are not worth columns, and a one-way migration. The derived index is written
  in the same database transaction as its blob and is checked by
  `storage_verification.split_index_issues` and the storage-safety tests, so it
  cannot silently diverge.

## Consequences

- `split_index` stays derived and verified; any query that needs a split's account,
  date, value, or quantity uses it.
- Re-run `python scripts/storage_benchmark.py` before revisiting this decision, for
  example if load-all stops being on the hot path or if Python-side assembly gets
  cheaper.
- A compact encoding of transaction JSON (omitting fields at their default values,
  as `Split.importer_added` already is) is the cheaper route to smaller books. It
  is listed after the first Beta in the roadmap.
