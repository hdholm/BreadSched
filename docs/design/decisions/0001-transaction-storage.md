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

## Re-evaluation with the book server (2026-10-24)

The book server (decision [0003](0003-book-server.md), then tried on the branch
`claude/experimental-book-server`) changes how the book is used. One server holds
the book for every interface. Each browser or HTTP read opens its own read-only
snapshot, and the book API sends stored forms of records over the wire: the
transaction or schedule an edit is based on travels as its serialized JSON. So the
decision was re-checked for both alternatives, normalized tables and a minimized
JSON blob, which until now was only a suggestion.

`scripts/storage_benchmark.py` now measures a third layout. It is the current blob
and `split_index`, but the JSON leaves out every key whose value is the one reading
would supply if the key were missing. The defaults are learned from `from_dict`
itself, and the benchmark checks that every transaction reads back exactly. Two
runs with 30,000 transactions, best of 7, with SQLite 3.45.1 and Python 3.11 on a
4-core Linux container:

| measure | current | normalized / current | minimized JSON / current |
|---|---:|---:|---:|
| file size (MiB) | 52.6 | 0.57 | 0.62 |
| mean transaction JSON (bytes) | 992–997 | — | 0.51 |
| bulk write (s) | 1.24–1.65 | 1.46–1.48 | 1.16–1.44 |
| load every transaction (s) | 1.15–1.17 | 1.48–1.50 | 0.91–1.01 |
| busiest register (s) | 0.86–0.94 | 1.23–1.39 | 0.72 |
| account balance (ms) | 51–54 | 0.99–1.06 | 0.95–1.00 |
| one edit commit (ms, median) | 1.57–1.81 | 1.08 | 0.87–1.25 |

**Normalized tables: still rejected, now more firmly.**
- **Speed:** the book server puts loading on the request path. Every Plan,
  Projection, or report request reads a fresh snapshot, so loading 1.5 times
  slower is paid per request, not once per session.
- **Revisions:** per-record versions (an open question in 0003) do not need columns.
  Each stored transaction already carries a change marker in its JSON.
- **One writer:** sharing a book does not change SQLite's single-writer model, so
  normalization does not help concurrency either.

**Minimized JSON: worth doing, and more so with the server.**
- **Smaller, no slower to read:** it keeps the layout and the verified index, and
  takes most of normalization's size gain (38% smaller files). Loading is no slower
  and registers were about 28% faster, since half as much JSON is decoded.
- **Smaller on the wire:** the stored form is also what the book API sends when a
  record travels (an edit's `source`, a schedule lookup), so the same encoding would
  halve those payloads for HTTP clients.
- **Cost:** writes are slower in this script, because each write filters every key
  in Python. A precomputed table of defaults per class would cut that cost, and
  writes are not on the interactive path.
- **Risk:** an omitted key means "the default", so changing a default in `from_dict`
  would silently change old books. Minimized storage would therefore need the
  defaults pinned by a test and versioned with `_schema`, and a migration whenever
  one changes.

**Outcome:**
- **Normalized tables:** the decision stands for normalized tables.
- **Minimized JSON:** implemented (`gen.db.compact`), on `main` from 0.2.0a308.
  - **What is left out:** only keys that every reader, including earlier
    versions, already reads with a constant default.
  - **Always written:** required keys (handles, accounts, dates, values,
    quantities, the change marker, `_schema`). Omitting a quantity equal to its
    value would have saved more, but earlier readers require the key.
  - **Compatibility:** no schema change and no migration. Books written before
    keep their full blobs, which read and verify alike.
  - **Pinned defaults:** the defaults are written out in code, and
    `tests/test_compact_storage.py` fails if `from_dict` ever supplies a different
    value.
  - **Exact full records:** storage expands each blob back to the full form when it
    reads one for comparison or the undo journal, so no-op writes, undo, and redo
    see exact records.
