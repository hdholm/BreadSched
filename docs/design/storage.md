# Storage, transactions, and recovery

Part of the [BreadSched design](../../DESIGN.md).

## Versions and migrations

The **application version** and **native data-format version** serve different
purposes and never advance in lockstep. The application version reported by
`breadsched --version` identifies the installed build for bug reports, packaging,
and release notes and reports the native compatibility window beside it. The
release workflow compares the installed wheel's line with
`versioning.version_summary()` run on the tested source, so the check can never
carry a stale schema window. Book
verification includes the same application and schema details in its human and JSON
diagnostics. The integer data-format/schema version determines whether a
native book can be opened or must be migrated; it is currently 14. A behavior-only
release changes only the application version. A persistent representation change
increments the data-format version and supplies an explicit migration.

SQLite is the native persistence engine. The application writes schema 14 and
migrates schema 13 before decoding primary objects. The 13→14 step writes each
whole-transaction plan link as split allocations: a transaction linked to an
occurrence (by `planned_occurrence`, or posted from a schedule) gains
`plan_allocations` on its plan legs, in full, with its planned date and amount. The
step checks that no transaction was lost and that every link names the occurrence it
named before (see [matching](matching.md)).

An explicit sequential registry (`gen.db.migrations`), a durable migration ledger, a
transactional runner, and a verified pre-migration backup make that boundary
testable. A migration runs before ordinary decoding, fails atomically, preserves a
verified backup, and leaves enough version evidence to diagnose or retry safely.
There is no no-op format bump: the schema rises only for a real change of
representation.

Read-only opens never migrate. `breadsched migrate` opens a book writable once,
which runs the migration and its pre-migration backup, and reports the old and new
schema and the backup path; the Windows upgrade test uses it on the book made by
the previous release.

The window starts at schema 13, the schema of the last alpha series. Books from the
earlier alphas (schemas 6 through 12) are refused with a message naming the window;
opening one once with the last 0.2.0 alpha brings it forward. From the first beta,
a migration step is kept for at least as long as a release that wrote its source
schema is supported. `tests/test_db.py` (`TestSchemaCompatibility`) holds the
runner's guarantees on a schema-13 book and `tests/test_plan_allocations.py` the
step itself. This native-book policy is independent of external GnuCash, QIF, OFX,
and QFX import compatibility.

## Journaling and read snapshots

The storage priorities are atomic financial writes, explicit format rejection,
verified backups and recovery, undo/redo integrity, and realistic performance on
long household histories.

Native books deliberately use SQLite `DELETE` journaling with `synchronous=FULL`,
not WAL. BreadSched has one explicit writer, while read-only snapshots may be taken
between its commits. Keeping rollback journaling preserves the
single-file book model, avoids persistent `-wal`/`-shm` companions that are easy to
separate during manual copying or cloud synchronization, and gives interrupted
writes SQLite's established rollback recovery path. Any future WAL change requires
tested checkpoint, backup, sidecar, and crash-recovery semantics rather than being a
performance toggle.

One calculation must read one committed generation of the
book: accounts, transactions and splits, schedules, scenarios, prices, and metadata.
A read-only open (`DbSQLite.load(path, "r")`, used by every web GET and the GTK
projection worker) therefore copies the whole file into an in-memory database with
SQLite's backup API and closes the file connection. The backup copies all pages
under one shared lock and restarts if the writer commits during it, so the copy is
one generation; the account cache is loaded from the copy, and the copy is
`query_only`. A write committed later is invisible to that reader and visible to the
next open. Two alternatives were rejected. A plain read-only connection sees each
query's own generation, so one request could combine an old
account cache with new rows, or a balance read before and after a write. A read
transaction held for the whole calculation would be one generation too, but under
`DELETE` journaling it keeps a shared lock that stops the writer committing for as
long as the read runs, and the writer's busy timeout would then fail saves during a
long projection; a detached copy holds the lock only while copying. The cost is the
book's size in memory per open reader and a copy on each open, measured at about the
same time as the integrity check every open already runs (0.23 s either way on a
60 MB book of 30,000 transactions). The full integrity check stays on every open,
snapshot copies included, as a deliberate choice for data safety even though on a
large book it costs more than the copy (232 ms against 76 ms for the copy alone on
that book); revisit only if open time becomes a real problem. `load_for_verification`
still reads the file itself, so damage is diagnosed where it is. Tests: `tests/test_db.py`
`TestReadSnapshots` and the web `test_one_get_reads_one_generation_even_when_a_write_lands_midway`.

## Single writer

Only one writer may own a native book at a time. Writable opens canonicalize the
book path, then acquire a sidecar lock containing host/process identity and a random
ownership token before SQLite is opened. Canonical identity prevents alternate path
spellings or symlink aliases from becoming competing writers. A competing writer
fails with an explicit read-only alternative; read-only opens remain allowed. A
stale lock is reclaimed automatically only when it belongs to the same host and its
recorded process no longer exists. Lock removal verifies the ownership token so one
process cannot delete another writer's lock. The server holding a book publishes where to reach it, and a command for the same
book connects to it instead of failing on the lock (see
[the book server](architecture.md#book-server)). The lock itself is unchanged and
still admits one writer.

## Minimized transaction JSON

A stored transaction leaves out each key
whose value is the constant default that `Transaction.from_dict` and
`Split.from_dict` read when the key is missing (`gen.db.compact`; the measurements are
[below](#why-transactions-stay-json-with-a-derived-index)).
- **What it saves:** about half the JSON per transaction, so books get smaller.
- **Compatibility:** every version reads the result unchanged, and earlier full
  blobs keep working beside minimized ones.
- **Reads inside storage:** `_read` expands a blob back to the full form, so writes
  that change nothing are still skipped and the undo journal holds complete records.
- **SQL over the blob:** `json_extract` reads an omitted key as null, which is the
  default for every key that SQL inspects.

## Book revision

A book carries a change counter, `DbSQLite.revision()`, stored under the
metadata key `book_revision` (no schema change). Every committed database
transaction that changed a record, and every undo or redo, raises it by one inside
the same SQL transaction, so a read-only snapshot sees exactly the revision of the
generation it reads and every process agrees on it. It is written outside the undo
records, so undoing a change moves the counter forward rather than back. Metadata
written outside a database transaction (presentation settings) does not count. The
book server's `ETag` and change events report this value; see
[the book server](architecture.md#book-server). `tests/test_book_server.py`
covers reopening, undo, and a read-only snapshot.

Undo shows what it would undo: `DbSQLite.undo_preview` (the book server's
`undo_preview`, `POST /api/v1/undo/preview`, a POST because only the writer holds the
undo history) names the step and counts its records by kind, so the desktop confirms
a step of 200 or more changes first.
`undo(progress, announce=False)` replays such a step on a worker, reporting its
progress, and `announce_undo` then raises its change signals on GTK's loop.

## Verification, backup, and restore

Normal writes are verified incrementally from the records already captured by the
database transaction. Changed objects are checked for their own domain invariants
and derived-index rows, and deletions check reverse references that could make
untouched objects invalid. A changed ledger transaction verifies only its own
``split_index`` rows rather than rebuilding the complete index. These checks live
in `gen/db/change_verification.ChangeVerification`, a `DbBase` subclass that
`DbSQLite` extends; they read through the public object API and the open connection
(`_require`, declared abstract there), so the storage module holds storage and
transactions only.

``verify_book()`` remains the exhaustive diagnostic for explicit verification,
backup/restore validation, tests, and corruption investigation.
This separation is deliberate: correctness checks on ordinary edits should scale
with the change, not with the lifetime size of the household ledger.

`tests/test_storage_safety.py` holds the safety net that every book-format
migration depends on. It drives each commit-time refusal (a changed object naming a
missing account, commodity, currency, split, transaction, or receivable; a deletion
leaving such a reference; an index row disagreeing with its blob) and checks the
book verifies unchanged afterwards; it exercises the writer lock's stale, foreign,
unreadable, and replaced lock files and its POSIX and Windows liveness probes; and
it injects failures into backup and restore (a failed integrity check, copy, or
verification, and a damaged destination) to check that no temporary file, partly
installed book, or lock is left behind and the existing book is untouched.

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

Backup and restore use SQLite's backup API rather than filesystem copying. Restore
verifies the source logically and physically, holds the same canonical destination
writer lock used by a live book, creates a pre-restore backup before an authorized
replacement, writes and re-verifies a temporary database, removes stale SQLite
sidecars, and only then atomically installs it. A restore therefore cannot replace
the pathname beneath another live writer. Verification opens malformed books in a
special tolerant read-only mode so damage is reported rather than decoded into
ordinary engine state.

`gen/db/book_lock.BookWriterLock` owns the single-writer `<book>.lock` file,
`gen/db/backups` the backup, restore (given the verifier and the pre-restore copy
as callables), migration-backup path, and read-snapshot copies, and
`gen/db/storage_verification` the derived-column and `split_index` checks that
`verify_book()` adds to the domain pass. None of them imports the backend, and
`DbSQLite` keeps the same public methods, delegating to them.

Verification should protect invariants without imposing whole-book work on every
small edit. Cross-cutting metadata that participates in financial workflows must
obey the same transaction/undo rules as ordinary primary objects. A direct metadata
write is therefore forbidden while a ``DbTxn`` is active unless that write is
explicitly attached to the active transaction; transactional metadata participates
in rollback, undo, and redo.

## Presentation settings and financial records

Per-book presentation conveniences that do not change financial meaning may remain
ordinary metadata. The last successful import source is one such value: GTK, web,
and CLI update the same key only after a successful import. GTK/web may preselect it,
but remembering a path grants neither permission to repeat the import nor permission
to write to the source. Hidden accounts follow a presentation
rule only: new-entry choices omit them unless the desktop's **Show Hidden Accounts**
is on, and an editor must retain and visibly identify a hidden account already
referenced by an existing split. Hiding never disables an account: no service
refuses one for a transaction, schedule, card payment, or loan.

Financial workflow records should not be stored as opaque metadata collections when
they have their own identity and lifecycle. FSA claims are first-class primary
objects: one claim save transaction can update linked reimbursement split
classifications and the claim row atomically, and one undo reverses both. GTK and web
submit typed claim, allocation, link, and rejection inputs rather than constructing
or persisting claim domain objects. Expected validation failures cross that service
boundary only as stable codes and field paths.

## Platform user paths

Per-user settings belong in the platform's normal configuration location rather than
a Linux-specific `~/.config` path: XDG config on Linux/Unix, `%APPDATA%` on Windows,
and `~/Library/Application Support` on macOS. Documents discovery likewise respects
XDG `user-dirs.dirs` and common Windows OneDrive redirection. Because SQLite files
are unsafe as an only copy on many sync/network filesystems, known sync roots are
detected and opening a book there emits a durability warning.

## Why transactions stay JSON with a derived index

Each transaction is one JSON document, with a derived `split_index` table for any
query that needs a split's account, date, value, or quantity. Normalized
transaction and split tables (typed columns, foreign keys, CHECK constraints) were
measured against this layout and not adopted.

`scripts/storage_benchmark.py` writes the same synthetic 20-year household history
(30,000 transactions, about 15% of them four- or five-split paychecks, across 60
accounts) to each layout with the SQL the storage layer would use, checks that each
returns every transaction exactly as written, and times each operation (best of
seven runs; median of 50 single edits). With SQLite 3.45 and Python 3.11 on a 4-core
Linux container:

| measure | full JSON + split_index | normalized / full | minimized JSON / full |
|---|---:|---:|---:|
| file size (MiB) | 52.6 | 0.57 | 0.62 |
| mean transaction JSON (bytes) | 992–997 | — | 0.51 |
| bulk write (s) | 1.24–1.65 | 1.46–1.48 | 1.16–1.44 |
| load every transaction (s) | 1.15–1.17 | 1.48–1.50 | 0.91–1.01 |
| busiest register (s) | 0.86–0.94 | 1.23–1.39 | 0.72 |
| account balance (ms) | 51–54 | 0.99–1.06 | 0.95–1.00 |
| one edit commit (ms, median) | 1.57–1.81 | 1.08 | 0.87–1.25 |

- **Normalized tables are slower where it matters.** Loading every transaction,
  which Projection, Plan, reports, verification, and export do through
  `iter_transactions`, is about 1.5 times slower: assembling each `Transaction` from
  a row per split costs more in Python than decoding one JSON document. The book
  server reads a fresh snapshot for each such request, so the cost would be paid per
  request.
- **They remove no complexity.** Normalizing would retire `split_index`
  synchronization but add row assembly, a column-to-field mapping every model change
  must update, a JSON column for the many fields not worth columns, and a one-way
  migration. The derived index is written in the same database transaction as its
  blob and checked by `storage_verification.split_index_issues`, so it cannot
  silently diverge. One writer per book is SQLite's model either way.
- **Minimized JSON takes most of the size gain** without a new layout, which is why
  [it is what the storage layer writes](#minimized-transaction-json): files are
  about 38% smaller, loading is no slower, registers are faster because half as much
  JSON is decoded, and the same form is what the book API sends when a record
  travels.

Re-run the benchmark before revisiting this, for example if loading everything
stops being on the hot path.
