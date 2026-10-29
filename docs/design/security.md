# Security

Part of the [BreadSched design](../../DESIGN.md).

Financial books are sensitive local data. Import content, schedule formulas, and
web requests are untrusted inputs. The loopback web interface must defend against
browser-origin attacks and DNS rebinding rather than assuming loopback binding alone
is sufficient.

The local web server therefore uses defense in depth: it binds only to loopback,
rejects non-loopback ``Host`` values, rejects foreign ``Origin`` values, requires
``application/json`` for ordinary writes (or bounded ``application/octet-stream``
for the dedicated import upload), and requires an unguessable token generated for
each server process on every API request. The launcher supplies that token in the
fragment of the initial local URL (so it is never sent as part of the HTTP request);
the page moves it into ``X-BreadSched-Token`` request headers and removes it from the
visible URL. Static assets do not need the token, but they still require a trusted
Host.

The experimental versioned resources under `/api/v1` (#357) follow the same rules:
trusted Host and Origin, the per-server token on every request (including the
change stream at `/api/v1/events`), and `application/json` for every write.
`PATCH` is accepted only for `/api/v1` resources. A write with an `If-Match` naming
an older book revision is refused with 412 and changes nothing. The browser's
`EventSource` cannot send the token header, so the page does not use the stream yet;
it is for programmatic clients such as `breadsched.api.http_client`.

A writable server publishes its address for other BreadSched processes of the same
user (#358); they prove who they are with the account's device token, kept `0600` in
its config folder, which the host's user directory knows only by its digest (decision
0004):
- **Where:** one record per book in the per-user runtime directory, never beside the
  book, since a book folder may be shared or synced. The file is `0600` in a `0700`
  directory on POSIX.
- **When it counts:** only while the book's writer lock still carries the lock
  token the record names and, on this host, its process is alive. Other records are
  removed as stale.
- **Explicit server:** `--server` takes its token from a file or the environment,
  never from the command line, where other users could read it in the process list.

A device without a token signs in with a user name and password (decision 0004):
`scrypt` hashes, throttled attempts, and a session in an `HttpOnly`,
`SameSite=Strict` cookie (`Secure` through the TLS proxy) that the existing Host,
Origin, and content-type checks still guard. Optional two-step sign-in adds an RFC
6238 code (one step either side, never a step twice) or a single-use recovery code
(kept as digests) after the password; a wrong code is a failure like a wrong
password, throttled the same way. The secret is kept in the user directory (`0600`),
which a backup of the host's settings must therefore protect. A temporary password must be changed
before any other request is served. Each request is served in the caller's role in
the book (owner, editor, viewer, or entry; decision 0004), checked before every book
operation, and a user given no role there is refused. A server holding several books
answers a request for the one its `X-BreadSched-Book` header names; `GET
/api/v1/books` lists only those the caller has a role in, and someone the server does
not know is refused before it says whether a named book is there. Only the computer
account, from its own computer (its device token or the server's local token), may
ask the host to open a book by path or let one go; the host record that lets a later
`serve` find it is user-only, like the server records.

Reaching a book from another device goes only through a TLS reverse proxy on the
same host, with authentication of its own; the server itself never listens beyond
loopback. Decision 0003 documents the method, the checks the proxy must keep
(host, buffering, logging), an illustrative nginx configuration, and the server
changes still open. For a proxy (#358):
- **Trusted origin:** `--trusted-origin https://host` adds that one origin to the
  loopback origins a page may write from. Only an `https://` origin with no path is
  accepted.
- **Token file:** `--token-file PATH` keeps the token in a file the server creates
  `0600`, so it is not printed and survives restarts. A file other users can read,
  or a token shorter than 32 characters, is refused.
- **HTTPS client:** `--server https://...` verifies the proxy's certificate against
  the system's authorities or `--ca-file`, requires TLS 1.2 or later, and presents
  `--client-cert` (and `--client-key`) when the proxy asks. A certificate that does
  not verify is a `server.unreachable` failure, never a fallback to plain HTTP.

Every request served over HTTP, `/api/v1` included, may link or relink a document
only by web address or inside the attachment folder; decision 0003 notes the copy
path (`attach_file`) that still reads any file the server's user can read.

GTK shares its open book only when the user turns on **Preferences → Sharing**. It
then listens on the loopback interface with the same per-server token, trusted-Host,
and trusted-Origin checks as the web server, and publishes the same user-only server
record.

Security hardening belongs in normal acceptance criteria, including transport-level
regression tests for rejected foreign origins/hosts, missing tokens, and invalid
write content types.

