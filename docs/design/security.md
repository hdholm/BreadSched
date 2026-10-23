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

Security hardening belongs in normal acceptance criteria, including transport-level
regression tests for rejected foreign origins/hosts, missing tokens, and invalid
write content types.

