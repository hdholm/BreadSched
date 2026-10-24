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
- **Still local:** the command line still looks up accounts and transactions in the
  book it opens. So `add`, `edit`, and `delete` go through the in-process server
  only, not a shared one, until those lookups join the API.

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
