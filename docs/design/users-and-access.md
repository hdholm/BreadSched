# Users and access

Part of the [BreadSched design](../../DESIGN.md); see also
[Architecture and ownership](architecture.md#book-server) and [Security](security.md).

The book server knows *users*. Every request is made by a user (signed in, or the
computer's own account acting for itself), every book is shared with named users in
a role, and every display choice is personal. Used alone on one computer, none of
this shows: no sign-in, no password, and the user settings live in a **Users** page
of Preferences.

The reason is that a household shares books between people, not only between
programs: a single shared token cannot tell a family member from the owner or a
phone from the laptop, cannot say who made a change, and does not keep out other
accounts on the same computer.

## A book host with a user directory

- **One host, several books.** A *book host* holds the books it serves; each book is
  known by a stable book id (a UUID kept in the book's metadata), not only by its
  path. `breadsched serve` and GTK's sharing start or join the host for the
  computer account rather than one server per book. Each book still has its own
  writer lock, revision, and change stream.
- **A user directory** belongs to the host, in its data folder (`users.sqlite`,
  `0600`), never in a book: a book file may be copied, synced, or emailed, and must
  not carry anyone's credentials.
- **The host's own account.** The computer account that runs the host is a user from
  the start (named after the login, its display name taken from the system). It is
  the host's administrator.
- **Grants live with the host, keyed by book id.** A book copied to another computer
  carries no grants there; whoever opens the file has whatever its file permissions
  give. Access control protects the *server*; the operating system protects the
  *file*. The User Guide says so plainly.

## Who a request comes from

| Client | How it proves who it is |
| --- | --- |
| GTK or a command, book opened in process | Nobody to ask: the file's permissions are the access control, as before sharing |
| GTK or a command on the host's computer, same account | A *device token* in the account's private config folder (`0600`, beside the preferences), made automatically the first time |
| Another account on the same computer | Its own device token, made when it first signs in with its password; the port being reachable no longer matters |
| The page `breadsched web` opens | The launcher's device token, in the URL fragment as before sharing |
| A phone, iPad, or laptop through the proxy | Signs in with name, password, and (if turned on) a TOTP code; gets a session |
| A script with `--server` | A device token it was given (`breadsched token create`), from `--token-file` |

- **Device tokens and sessions are the same thing:** random, named ("Howard's
  iPad"), stored on the host only as a hash, with a creation time, last use, and an
  optional expiry, and listed and revocable in **Preferences → Users**.
- **The browser** keeps a session in a cookie that is `HttpOnly`, `Secure` (through
  the proxy), and `SameSite=Strict`. The existing Host, Origin, and JSON
  content-type checks stay, so the cookie adds no cross-site request risk. A phone
  stays signed in, which answers "resume sessions" for the web.
- **There is no shared API token.** `--token-file` names a device token, and the
  server record carries only the address. Whether a caller may name files on the
  host is a property of its device token: **may name files on the host**, given only to device
  tokens made on the host's computer by the host's own account. A password session,
  even the administrator's from a phone, sends files instead, so a stolen phone
  cannot read the host's disk.

## Passwords and TOTP

- **Hashing:** `hashlib.scrypt` (the standard library, so the core gains no dependency), with a per-user salt and parameters stored
  with the hash so they can be raised later.
- **Sign-in throttling:** after repeated failures, a growing delay per account and
  per client address, and a log entry. Failures never say whether the name exists.
- **TOTP (RFC 6238):** optional; each user turns it on for themselves. It needs only `hmac` and `hashlib` (about
  thirty lines). Enrolment shows the secret and an `otpauth://` link; no QR code is drawn, since that would
  need a dependency. Ten one-time
  recovery codes are shown once at enrolment.
- **Passwords travel only over TLS or loopback.** A sign-in through a non-TLS,
  non-loopback route is refused, so a misconfigured proxy fails closed.
- **No email.** A forgotten password is reset by the administrator, who sets a
  temporary one that must be changed at next sign-in. A forgotten administrator
  password is reset from the host's own computer account (`breadsched users
  reset-password`), which needs no password: that account owns the files anyway.

## Accounts

- **Creating:** the administrator creates a user (name, display name, temporary
  password). Only the administrator can; there is no self-registration.
- **Deleting:** removes the user's sessions, device tokens, and grants. Their past
  changes stay in the books (attributed to "former user *name*"). A book's owner
  cannot be deleted until ownership passes to someone else; the last administrator
  cannot be deleted.
- **Changing credentials:** each user changes their own password (old password
  required), turns TOTP on or off (current code required), and revokes their own
  devices; changing a password ends the user's other sessions.

## Books and roles

- **Roles per book:**
  - *owner*: everything, including what only the owner may do (below);
  - *editor*: reads and writes, and undoes and redoes;
  - *viewer*: reads;
  - *entry*: adds transactions (quick entry, as from a child's phone) and sees only
    what entering needs: the accounts it may enter against, the descriptions it may
    complete from, and its own recent entries. It cannot undo; a mistake is put right
    by an editor.
- **The creator is the owner.** The owner grants and revokes access for users the
  host knows, may pass ownership on, and alone may: delete or restore the book,
  write back to GnuCash, change the attachment and GnuCash link folders, and set
  book-wide settings.
- **Every operation declares its permission.** Each `BookApi` operation is tagged
  read, entry, write, or owner (and *names host files* for the ones that take a path);
  the transport checks the tag before calling. An architecture test requires a tag
  on every operation, so a new one cannot be added open.
- **A user's book list** is the books they hold a grant on. Opening another book
  from the host, from a phone, is choosing one from that list.

## Undo stays the book's, and names who

- **One undo and redo history per book,** as before sharing, in the host's memory (lost when
  the host stops). Editors and owners undo and redo any step, whoever made it: an
  editor could make the reversing change by hand anyway, so keeping undo to one's own
  changes would protect nothing. Viewers and entry users cannot undo.
- **Each step names its user,** so Undo says whose change it reverses ("Undo Alice's
  Groceries"), and a change notice says whose change it was.
- **No durable "entered by"** is kept on a transaction; the history is in the host's
  memory and ends when the host stops.

## Preferences: three kinds

| Kind | Examples | Where it lives |
| --- | --- | --- |
| Device | window size, column widths, banding | Per computer account or browser |
| Personal | every display choice: the Plan's range, period, measure, scenario, and comparison; the whole Dashboard configuration (its groups and their accounts, sections shown, liquidity and emergency horizons, reserve, and minimum bill); register double line; the selected scenario; last books | In the user directory, per user (and per book where it is about one book), so the phone and laptop agree |
| Book-wide | the data and the rules that make it: accounts, transactions, schedules, scenarios and assumptions, categorization rules, tax marks, payroll templates, receivables and goals, the attachment and GnuCash link folders, write-back settings | In the book; changing them needs the editor role (the folders and write-back, the owner) |

The rule: anything that only changes how a user *sees* shared data is personal, so
each person can look at the book the way that makes sense to them. `plan.view` and
the Dashboard configuration are kept in the user directory; a new user
starts from the book's current choices. Used alone, personal preferences are simply
stored for the one user.

## Where it shows: Preferences → Users

- **One user, not shared:** nothing changes. No sign-in, no password, and the Users
  page shows "Only you use this computer's books", a button to set a password (needed
  only before using a book from another device), and the device list.
- **When sharing is turned on** (Preferences → Sharing), the Users page lists
  users (administrator: add, remove, reset password), the user's own password, TOTP,
  and devices; each book's sharing (in the book's properties) lists who may use it
  and in which role.
- **The browser** shows a sign-in page only to a client without a device token or
  session; the page `breadsched web` opens is already signed in.
- **The command line** has `breadsched users` (list, add, remove, reset-password),
  `breadsched password`, `breadsched totp`, and `breadsched token` (create, list,
  revoke), and `breadsched share BOOK USER --role editor`.

## How it is built

### Identity (`breadsched.api.users`)

- **The user directory** is `users.sqlite` in the computer account's data folder
  (`0600`, in a `0700` folder). The computer account's own user is made, as its
  administrator, the first time it is needed.
- **The device token** is made the first time a program of the account needs it,
  kept `0600` as `device-token` in the account's config folder, and replaced if the
  host no longer knows it. It may name files on the host. Tokens are kept only as
  their SHA-256 digests, with when they were made and last used, and can be revoked.
  A use is written down at most once a minute (`USE_NOTED_EVERY`): every request
  asks who its caller is, and a write each time made requests arriving together wait
  for one another's lock.
- **The server** answers a request for the user its token belongs to: its own
  per-process token (and the local token of the page it opens) is the computer
  account; any other token must be one the directory issued. A token from another
  account's directory, or a revoked one, is refused (403).
- **The server record** carries only the address, process, and lock. A program that
  finds it connects with the account's device token
  (`HttpBookClient(..., host_files=True)`), which also lets it name files on the host.
- Single-user use needs none of this; `--token-file` keeps the server's own token
  for devices behind a proxy that were given it.

### Accounts and signing in

- **Users and passwords:** `add_user` (a temporary password the user must change),
  `remove_user` (never the computer account or the last administrator),
  `set_password` (the current one required once there is one), and `reset_password`;
  passwords are `scrypt` hashes with their salt and parameters. Changing or resetting
  a password ends the user's sessions.
- **Signing in** (`sign_in`) makes a session token that lapses after 30 days without
  use and is renewed by use. A wrong password and an unknown name fail alike
  (`signin.failed`, at the same cost); after five failures for a name, or from an
  address, each attempt must wait twice as long as the last, up to 15 minutes
  (`signin.throttled`).
- **Commands:** `breadsched users list|add|remove|reset-password`, `breadsched
  password`, and `breadsched token create|list|revoke`, acting on the computer
  account's own directory (so they need no password).
- **Signing in over HTTP:** `POST /api/v1/session` (`name`, `password`, `device`)
  answers who signed in, with the session token, and sets the browser's
  `breadsched_session` cookie (`HttpOnly`, `SameSite=Strict`, `Path=/`, and `Secure`
  when the page came through an `https://` origin). `DELETE /api/v1/session` signs
  out and clears it; `GET /api/v1/me` says who is signed in; `POST
  /api/v1/me/password` (`current`, `new`) changes one's own password and keeps the
  session asking. The server takes a token from `X-BreadSched-Token`, else the
  cookie; the Host and Origin checks and JSON content type still apply, so the
  cookie adds no cross-site risk. A failed sign-in is 401 (`signin.failed`), a
  throttled one 429; the client's address is the proxy's `X-Real-IP`, else the connection's.
- **A temporary password first:** while a user must change their password, every
  other request is refused (403).
- **The pages:** the full page and quick entry ask `/api/v1/me` first; without a
  token or session they show a sign-in form, and then the choice of a new password
  when one is due. The cookie keeps a phone signed in across reloads.
- **Preferences → Users** in GTK says "Only you" until there are others; **Users and
  devices…** (`gui/dialogs/users_dialog.py`) sets your own password, adds, resets,
  and removes people, and lists, makes (shown once), and revokes your device tokens,
  on the computer account's directory directly.

### Roles, grants, and a host for several books

- **Book ids:** `book_id()` (`GET /api/v1/book/id`, `gen/services/book_info`) is the
  book's permanent id, kept in its metadata as `book.id` and made the first time it
  is asked for in a writable book. A server notes the book it serves in the host's
  `books` table under that id.
- **Grants** live in the user directory (`grants`, by book id and user):
  `grant(book_id, name, role)`, `revoke_grant`, `grants`, and `role`. The computer
  account owns every book it holds and cannot be given a role; a removed user's
  grants go with them.
- **Permissions** (`api/permissions.py`): every `BookApi` operation is `ANY`,
  `LOOKUP`, `READ`, `ENTRY`, `WRITE`, or `OWNER`, and an architecture test requires
  the table to match the protocol exactly. A viewer has `ANY`, `LOOKUP`, and `READ`;
  an entry user `ANY`, `LOOKUP`, and `ENTRY` (and `save_transaction` only of a new
  transaction); an editor all but `OWNER`; an owner everything. Saving one's
  own Plan view or Dashboard is `READ` (see below).
- **Enforcement:** the transport works out the caller's role in the served book
  (`owner` for the computer account; else the grant, or none, which refuses the
  request), and sets it for the request on whichever thread runs the book
  (`acting_as`). The web layer's `Api.book` is a `GuardedBook` that checks it before
  every operation, refusing with 403 `book.permission.denied`; only the transport may
  reach the unguarded `Api.server`. The two routes that touch the book file
  itself (a document's content, which needs `READ`, and an import upload, which
  needs `WRITE` before the file is kept) check the role first, and an architecture
  test holds that list. In process nothing is checked.
- **The entry role's view:** `account_outline()` (`GET /api/v1/account-outline`) is
  the account tree with depths and no values, which quick entry now reads.
- **Sharing:** `breadsched share BOOK [USER --role ROLE | USER --remove]`, and **Who
  may use** the open book in GTK's **Users and devices**.
- **A host for several books:** `serve()` and `build_handler()` take one book or
  several, each with its own writer, lock (or GTK's executor), read snapshots,
  revision, and change stream; the same file twice is refused. A request names its
  book by id in `X-BreadSched-Book` (`api.host`); a host holding one book needs no
  header. Naming none of several is 400 `book.required`, an id the host does not hold
  404 `book.not_found`, but only for a caller the host knows. `GET /api/v1/books`
  lists the books the caller has a role in (id, name, role;
  `HttpBookClient.hosted_books()`). `breadsched serve BOOK [BOOK ...]` (and `web`)
  holds several and prints their ids; each book's server record carries its id, which
  `open_book_api` sends, so commands on the computer still find a book by its file.
  `--book-id` chooses the book for `--server`. The browser page asks which book when
  several are shared with you, keeps it in the address (`#book=`), and offers **Other
  books**.
- **One host per computer account:** a writable `serve` or `web` publishes a host
  record (`host.json` in the runtime folder, user-only: address, process, computer).
  A later `breadsched serve BOOK` that finds a live one *joins* it: it asks the host to
  open the book (`POST /api/v1/host/books`, `path`), stays until Ctrl+C or the host
  stops, and then asks it to let the book go (`DELETE /api/v1/host/books/{id}`). The
  host opens the book itself, so it holds the writer lock and publishes the book's
  record; it adds and removes books without stopping (`add_book`, `remove_book`, the
  book's writer work in progress finishing first). Only the computer account from
  this computer may do either (403 `host.local_only`); a book already open is 409
  `host.book.held`, a missing one 404 `host.book.missing`, and only books opened this
  way can be let go (404 `host.book.not_joined`). The joining command's own `--port`,
  `--trusted-origin`, `--token-file`, and `--open` do not apply, and it says so. A host
  record that does not answer is ignored, and the command serves the book itself.
  GTK's sharing serves only the book it has open and cannot be joined.
- **GTK opens a book the host holds** through the host (`gui/remote_book.py`): when
  the book's server record is live, `open_book` makes a `RemoteBook`, the book
  reached as the computer account's user with its device token, without a prompt.
  GTK's views and dialogs reach a book only through `book_server(db)`, which gives
  them the host's book API (`MainLoopBook`: one change stream, its notices delivered
  on GTK's main loop as the in-process server's are); `OpenBook` (the file or a
  `RemoteBook`) is what GTK holds, and the extended type check holds every GUI module
  to it. What GTK still did with the file itself now goes through book operations:
  editors' previews (`draft_transaction`, `draft_fixed_schedule`,
  `draft_formula_schedule`, `draft_scenario_estimate`), a pair's latest quote
  (`latest_exchange_rate`), Plan cells (`explain_plan_cell`), a register's holding
  (`register_view`), and the transaction and reporting currencies. Long reads run
  on a worker against the host (`worker_book`, and the snapshot reader with `api=`),
  which reads its own snapshots; an export or a backup reads a read-only snapshot of
  the file (`book_snapshot`). A remote book is shared already, so GTK does not share
  it again; a projection over the host shows no progress, only its result. 

### Personal preferences and attributed undo

- **Kept with the person,** in the user directory's `preferences` table (user, book id,
  key, JSON value), removed with the user, never in the book. The transport serves
  every request for its caller (`api.users.acting_for`); in process it is the computer
  account.
- **The Plan view** (`plan.view`: range, period, measure, scenario, comparison): `plan_settings`, `save_plan_settings`, and the Plan and Expense Explorer
  opened on the saved view use the caller's own, else the view a book saved before,
  else the defaults. Saving it changes nothing in the book (no revision), so it is a
  `READ` operation: a viewer keeps their own. A book without an id (read-only, in
  memory) still keeps its own.
- **The Dashboard:** its whole configuration (horizons, sections, groups,
  and the groups' members) is kept as the person's own for that one book, by the
  book's id, since each book's accounts and purpose differ (`dashboard`,
  `DashboardConfig.serialize_personal`, which lists each group's members, since the
  accounts carry only the book's). `dashboard`, `dashboard_settings`, and
  `save_dashboard_settings` use it, else the book's arrangement (the accounts'
  groups and the book's configuration), which is where everyone starts. Saving is
  `READ`. An account's Dashboard group is the reader's own as well: `accounts` and
  `account` show it, and `save_account` moves the account in the caller's
  arrangement while the book's account keeps its group, so the account editor and
  `breadsched account --group` act on one's own Dashboard. The book's arrangement
  no longer changes through a book with an id.
- **Undo names each step's user:** a book server serving someone makes its changes
  as theirs (`gen.db.base.authored_by`, set with `acting_for`), so each undo step
  (`DbTxn.author`, `undo_author`) and each change notice (`Change.by`, and
  `Change.undo_by` for the step left to undo) names who made it; in process no one
  is named. GTK's Undo says whose step it is ("Undo: Add transaction (Eddie)").
  Editors and owners undo anyone's step; viewers and entry users cannot (`WRITE`).
  The history stays in the host's memory, as before; a durable "entered by" on each
  transaction is not kept.

### Two-step sign-in

Optional, per user.

- **The code:** RFC 6238 with the standard library (`hmac`, SHA-1, six digits, 30
  seconds), one step either side of now, and never a step at or before the last one
  used, so a code cannot be replayed. The secret (20 random bytes, base32) is in the
  user directory's `second_factors` table; `start_two_step` makes it (with an
  `otpauth://` URI) and `confirm_two_step` turns it on with a first code, returning
  ten recovery codes, kept only as digests (`recovery_codes`) and each good once.
- **Signing in:** with it on, a right password without a code is 401
  `signin.code_required` (not a failure); a wrong code is 401 `signin.code.invalid`
  and counts toward throttling like a wrong password. `POST /api/v1/session` takes
  `code`; the pages ask for it after the password.
- **Turning it on and off:** `POST /api/v1/me/two-step` (the secret and URI), `.../confirm`
  (`code`; the recovery codes), and `.../off` (`password`); `/api/v1/me` says
  `two_step`. The browser's **Account** view, GTK's **Users and devices**, and
  `breadsched two-step on|off|status` use them; an administrator's
  `users reset-two-step` (or GTK's **Turn off two-step**) is for a lost phone. The
  command never prints the secret or the recovery codes: it writes them to a new
  file only the account can read (`--setup-file`), as `--token-file` keeps tokens.

## Hardening rules

The request path (the transport's checks, the user directory, the role guard and
its table, the host and its joined books, server discovery, the HTTP client, and GTK
on a held book) is designed against three adversaries: a household member with a
role, a stranger reaching the proxy, and another program on the computer. These
rules follow, each held by `tests/test_server_hardening.py`:

1. **A document never runs as the app.** `/api/attachment/content` does not trust a
   file's name for its type: an attached web page or script opened in the signed-in
   app would otherwise run with the reader's session. Only PDF, PNG, JPEG, GIF, WebP,
   and plain text are served as themselves (inline); anything else is `application/octet-stream` with
   `Content-Disposition: attachment`, and every document carries a `sandbox`
   content security policy and `no-store`.
2. **Sign-in runs one attempt at a time** (a lock, and an immediate transaction for
   other processes), so attempts sent together cannot all pass the throttle check
   and one two-step code cannot let in two sign-ins.
3. **A change stream ends with its caller's access.** `/api/v1/events` asks again at
   every change and keep-alive (15 seconds), and ends after a sign-out, a revoked
   token, or a removed role.
4. **The client never sends its token in the clear.** `HttpBookClient` requires
   `https://` for anything but loopback.
5. **A server record is trusted only at a loopback address.** Only a server writes a
   record, always at loopback; a record or host record naming any other address is
   ignored, so the computer account's device token is never sent elsewhere.
6. **Nothing is cached.** Every JSON response and document says `no-store`, with
   `Referrer-Policy: no-referrer`.

Also part of the design:
- **Roles:** the web layer reaches the book only through `GuardedBook` (the
  architecture tests list the few routes given the file, and each checks the role);
  every operation has a permission; the transport always sets a role, `none` for an
  unknown caller, so the unchecked in-process path is never reached over HTTP. A
  remote caller names files only inside the uploads and attachment folders, and the
  owner's folders and GnuCash write-back need the computer account on this computer.
- **Sign-in and sessions:** `scrypt` passwords, digests of random tokens, the same
  cost and answer for an unknown name, a temporary password changed before anything
  else, sessions ended by a password change or reset, an `HttpOnly`,
  `SameSite=Strict` cookie, and writes only as `application/json` (so another site
  cannot send one without the browser asking first).
- **Accepted risks:** throttling by name lets anyone delay one person's sign-in (at
  most 15 minutes between attempts; their sessions go on); a session lasts while it
  is used, 30 days after its last use; an entry user's suggestions show the
  descriptions and amounts of earlier matching entries; the server's own token and
  `--token-file` are the computer account, owner of every book, and must stay
  private; and a book GTK or a command opens in process is checked by nothing but
  its file's permissions.
- **What the proxy must do** (see the configuration below): set `Host` to the
  loopback address, overwrite `X-Real-IP` with the client's address (or throttling
  by address can be avoided; throttling by name still holds), and serve only over
  TLS.

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
2. **Who may connect:** every request needs a device token or a signed-in session (above).
   On a network, the proxy adds an authentication of its own. Client certificates (`ssl_verify_client on`) are
   preferred; HTTP basic authentication over TLS with an address allow-list is the
   minimum. A book should never be exposed to the internet.
3. **Host:** nginx's default upstream `Host` is `127.0.0.1:PORT`, which the server's
   Host check (a DNS-rebinding defence) accepts. The proxy's own `server_name`
   takes over that defence for the public name, so a default server that answers
   any other name with 444 is needed.
4. **Origin:** a page served through the proxy sends `Origin: https://books.example.lan`.
   The server accepts loopback origins, and the one public origin it is started with
   (`--trusted-origin https://books.example.lan`); any other origin is refused with
   403. The proxy passes `Origin` through unchanged. Cross-site requests stay blocked
   twice over: the server checks the origin, and it requires the token in a custom
   header, which a foreign page cannot send without a CORS preflight the server never
   grants.
5. **Request limits:** `client_max_body_size 32m` matches the import upload limit
   (the server still enforces its own limits).
6. **Change stream:** `/api/v1/events` must not be buffered, and its read timeout
   must exceed the 15-second keep-alive.
7. **Logs:** the token travels in the `X-BreadSched-Token` header, and in the URL
   fragment only when the page first opens. Fragments are never sent to a server, so
   the proxy must not log request headers.
8. **Other local users:** they can reach the loopback port, and are kept out because
   they hold no credential of their own.

An illustrative configuration (replace the names, port, and certificate paths):

```nginx
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

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header X-Real-IP $remote_addr;   # whom sign-in throttling counts
    }

    location = /api/v1/events {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header X-Real-IP $remote_addr;   # whom sign-in throttling counts
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}
```

Start the server with:

```bash
breadsched serve BOOK --port 8765 --trusted-origin https://books.example.lan \
    --token-file ~/.config/breadsched/books.token
```

The token is kept in that file (created `0600` if absent), is not printed, and stays
the same across restarts. Open `https://books.example.lan/#token=TOKEN` in the
browser. Without `--token-file`, the printed address carries a new token each start.

From another machine's command line, name the proxy as the server:

```bash
breadsched balance BOOK --server https://books.example.lan --token-file books.token \
    --ca-file household-ca.pem --client-cert laptop.pem --client-key laptop.key
```

`HttpBookClient` verifies the proxy's certificate (against `--ca-file`, or the
system's authorities), requires TLS 1.2 or later, and presents the client
certificate the proxy asks for. On the serving host itself, commands keep using the
loopback address from the server record, so the proxy is never in their way.

**Linked documents over HTTP.** A server reached over HTTP (the browser's routes and
`/api/v1` alike) links or relinks a document only by web address or inside the
attachment folder (`contained_location`), so a token holder cannot link a file such
as `~/.ssh/id_ed25519` and then read it back through the document route; only an
in-process caller (GTK, or a command with no server) or a request carrying the local
token may link any file.
Copying a file in by path (`attach_file`) and changing the attachment folder, which
would have let a token holder link any file through it, are local only too; a device elsewhere sends the document's bytes.
