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
  unchanged JSON. The browser's and GTK's account editors still save directly.

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
   The server accepts loopback origins only, so its writes would be refused. Until
   the server takes a configured trusted origin (an open item), the proxy checks the
   origin itself and rewrites it:
   - an `Origin` that is neither absent nor exactly the public origin is refused with
     403;
   - an accepted one is replaced by `http://127.0.0.1:PORT`.

   Cross-site requests stay blocked twice over: the proxy checks the origin, and the
   server requires the token in a custom header, which a foreign page cannot send
   without a CORS preflight the server never grants.
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
map $http_origin $breadsched_origin {
    ""                            "";
    "https://books.example.lan"   "http://127.0.0.1:8765";
    default                       "refuse";
}

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

    if ($breadsched_origin = "refuse") { return 403; }

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Origin $breadsched_origin;
        proxy_http_version 1.1;
    }

    location = /api/v1/events {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Origin $breadsched_origin;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}
```

Start the server with `breadsched serve BOOK --port 8765`. Open the printed address
with `https://books.example.lan` in place of `http://127.0.0.1:8765`, keeping the
`#token=` fragment. The token changes every time the server starts.

**Not yet supported through a proxy:**
- **HTTP client:** `HttpBookClient` (and so `--server`) speaks plain HTTP only.
  Reaching a proxied server from another machine's command line needs HTTPS with
  certificate verification, and client certificates where the proxy requires them.
- **Local commands:** on the serving host itself, commands keep using the loopback
  address from the server record, so the proxy is never in their way.
- **Server-side changes still needed** to make the proxy's origin rewriting
  unnecessary and close the local-user gap:
  - a configured trusted origin, such as `--trusted-origin https://books.example.lan`;
  - a Unix-socket listener;
  - the token taken from a file rather than printed.

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
