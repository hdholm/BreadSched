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
native book can be opened or must be migrated; it is currently 10. A behavior-only
release changes only the application version. A persistent representation change
increments the data-format version and supplies an explicit migration.

SQLite is the native persistence engine. The current application writes schema 10 and
can migrate schemas 6, 7, 8, and 9 before decoding primary objects. Schema 10 added the
`savings_goal` table (9→10); schema 9 added the `receivable` table (8→9); schema 8 added the `payee` table (7→8); schema 7 added
reconciliation sessions (6→7). An explicit sequential registry and durable ledger,
transactional runner, verified pre-migration backup hook, and versioned fixture make
that compatibility boundary testable. Migration infrastructure is a durable
architectural capability even when an individual obsolete transformation is allowed
to expire.

Read-only opens never migrate. `breadsched migrate` opens a book writable once,
which runs the migration and its pre-migration backup, and reports the old and new
schema and the backup path; the Windows upgrade test uses it on the book made by
the previous release.

The supported migration window currently covers four preceding data-format
versions: the registry retains 6→7, 7→8, 8→9, and 9→10, and the application accepts
schemas 6 through 10. Versioned fixtures for schemas 6, 7, 8, and 9 prove each step.
The window widens only when a real schema migration is needed; there is no no-op
format bump. A migration must run before ordinary decoding, fail atomically, preserve
a verified backup, and leave enough version evidence to diagnose or retry safely.
The mechanism and supported migration steps are not removed merely because the
application version advances. Stable releases may require a still wider promise.
This native-book policy is independent of external GnuCash, QIF, OFX, and QFX import
compatibility.

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
process cannot delete another writer's lock.

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

## Platform user paths

Per-user settings belong in the platform's normal configuration location rather than
a Linux-specific `~/.config` path: XDG config on Linux/Unix, `%APPDATA%` on Windows,
and `~/Library/Application Support` on macOS. Documents discovery likewise respects
XDG `user-dirs.dirs` and common Windows OneDrive redirection. Because SQLite files
are unsafe as an only copy on many sync/network filesystems, known sync roots are
detected and opening a book there emits a durability warning.

