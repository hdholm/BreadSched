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

A writable server publishes its address and token for other BreadSched processes of
the same user (#358):
- **Where:** one record per book in the per-user runtime directory, never beside the
  book, since a book folder may be shared or synced. The file is `0600` in a `0700`
  directory on POSIX.
- **When it counts:** only while the book's writer lock still carries the lock
  token the record names and, on this host, its process is alive. Other records are
  removed as stale.
- **Explicit server:** `--server` takes its token from a file or the environment,
  never from the command line, where other users could read it in the process list.

Reaching a book from another device goes only through a TLS reverse proxy on the
same host, with authentication of its own; the server itself never listens beyond
loopback. Decision 0003 documents the method, the checks the proxy must keep
(origin, host, buffering, logging), an illustrative nginx configuration, and the
server changes still open.

GTK shares its open book only when the user turns on **Preferences → Sharing**. It
then listens on the loopback interface with the same per-server token, trusted-Host,
and trusted-Origin checks as the web server, and publishes the same user-only server
record.

Security hardening belongs in normal acceptance criteria, including transport-level
regression tests for rejected foreign origins/hosts, missing tokens, and invalid
write content types.

