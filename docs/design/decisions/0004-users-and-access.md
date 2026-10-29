# 0004: People, not processes, use a book server

Part of the [BreadSched design](../../../DESIGN.md); builds on
[decision 0003](0003-book-server.md).

- **Status:** accepted for the experimental branch, 2026-10-28, with the answers to
  its review questions (at the end) folded in. It is built on the
  `claude/experimental-book-server` branch in the phases below.
- **Decision:** the book server knows *users*. Every request is made by a user
  (signed in, or the computer's own account acting for itself), every book is shared
  with named users in a role, and every display choice is personal. Used alone on one
  computer, nothing of this shows: no sign-in, no password, and the user settings
  live in a **Users** page of Preferences.

## Context

Decision 0003 gave each book one server that every interface reaches. Who may use it
is decided only by a token:

- **One shared token.** Whoever holds it can do anything, so it cannot tell a family
  member from the owner, a phone from the laptop, or say who made a change.
- **A second, local token** (#358) marks programs on the server's own computer,
  which may name files there. It does not stop other accounts on the same computer:
  the loopback port is open to them, and only the shared token keeps them out.
- **Undo is the book's.** One history per open book: whoever undoes, undoes the last
  change anyone made.
- **Preferences are split in two.** GTK keeps window and view choices per computer
  account (`settings.ini`, `views.ini`); some presentation choices (the Plan's range
  and scenario, `plan.view`) are kept in the book and so are shared by everyone.
- **One server per book.** A person with several books runs, and signs in to,
  several servers.

Use from an iPad and quick entry from a phone (the project's goals) make these gaps
real: a household shares books between people, not only between programs.

## Decision

### 1. A book host with a user directory

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
  *file*. The guide must say so plainly.

### 2. Who a request comes from

| Client | How it proves who it is |
| --- | --- |
| GTK or a command, book opened in process | Nobody to ask: the file's permissions are the access control, as today |
| GTK or a command on the host's computer, same account | A *device token* in the account's private config folder (`0600`, beside the preferences), made automatically the first time |
| Another account on the same computer | Its own device token, made when it first signs in with its password; the port being reachable no longer matters |
| The page `breadsched web` opens | The launcher's device token, in the URL fragment as today |
| A phone, iPad, or laptop through the proxy | Signs in with name, password, and (if turned on) a TOTP code; gets a session |
| A script with `--server` | A device token it was given (`breadsched token create`), from `--token-file` |

- **Device tokens and sessions are the same thing:** random, named ("Howard's
  iPad"), stored on the host only as a hash, with a creation time, last use, and an
  optional expiry, and listed and revocable in **Preferences → Users**.
- **The browser** keeps a session in a cookie that is `HttpOnly`, `Secure` (through
  the proxy), and `SameSite=Strict`. The existing Host, Origin, and JSON
  content-type checks stay, so the cookie adds no cross-site request risk. A phone
  stays signed in, which answers "resume sessions" for the web.
- **The shared API token goes.** `--token-file` names a device token instead, and
  the server record carries only the address. The local token of #358 becomes a
  property of a device token: **may name files on the host**, given only to device
  tokens made on the host's computer by the host's own account. A password session,
  even the administrator's from a phone, sends files instead, so a stolen phone
  cannot read the host's disk.

### 3. Passwords and TOTP

- **Hashing:** `hashlib.scrypt` (the standard library; no new dependency, as
  decision 0002 requires of the core), with a per-user salt and parameters stored
  with the hash so they can be raised later.
- **Sign-in throttling:** after repeated failures, a growing delay per account and
  per client address, and a log entry. Failures never say whether the name exists.
- **TOTP (RFC 6238):** optional; each user turns it on for themselves. It needs only `hmac` and `hashlib` (about
  thirty lines). Enrolment shows the secret and an `otpauth://` link; a QR code would
  need a dependency (or a small built-in encoder) and can come later. Ten one-time
  recovery codes are shown once at enrolment.
- **Passwords travel only over TLS or loopback.** A sign-in through a non-TLS,
  non-loopback route is refused, so a misconfigured proxy fails closed.
- **No email.** A forgotten password is reset by the administrator, who sets a
  temporary one that must be changed at next sign-in. A forgotten administrator
  password is reset from the host's own computer account (`breadsched users
  reset-password`), which needs no password: that account owns the files anyway.

### 4. Accounts

- **Creating:** the administrator creates a user (name, display name, temporary
  password). Only the administrator can; there is no self-registration.
- **Deleting:** removes the user's sessions, device tokens, and grants. Their past
  changes stay in the books (attributed to "former user *name*"). A book's owner
  cannot be deleted until ownership passes to someone else; the last administrator
  cannot be deleted.
- **Changing credentials:** each user changes their own password (old password
  required), turns TOTP on or off (current code required), and revokes their own
  devices; changing a password ends the user's other sessions.

### 5. Books and roles

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
  read, entry, write, or owner (and *names host files* for the path-taking ones of
  #358);
  the transport checks the tag before calling. An architecture test requires a tag
  on every operation, so a new one cannot be added open.
- **A user's book list** is the books they hold a grant on. Opening another book
  from the host, from a phone, is choosing one from that list.

### 6. Undo stays the book's, and names who

- **One undo and redo history per book,** as today, in the host's memory (lost when
  the host stops). Editors and owners undo and redo any step, whoever made it: an
  editor could make the reversing change by hand anyway, so keeping undo to one's own
  changes would protect nothing. Viewers and entry users cannot undo.
- **Each step names its user,** so Undo says whose change it reverses ("Undo Alice's
  Groceries"), and a change notice says whose change it was.
- **A durable "entered by"** on each transaction is optional and could come later.

### 7. Preferences: three kinds

| Kind | Examples | Where it lives |
| --- | --- | --- |
| Device | window size, column widths, banding | Per computer account or browser, as now |
| Personal | every display choice: the Plan's range, period, measure, scenario, and comparison; the whole Dashboard configuration (its groups and their accounts, sections shown, liquidity and emergency horizons, reserve, and minimum bill); register double line; the selected scenario; last books | In the user directory, per user (and per book where it is about one book), so the phone and laptop agree |
| Book-wide | the data and the rules that make it: accounts, transactions, schedules, scenarios and assumptions, categorization rules, tax marks, payroll templates, receivables and goals, the attachment and GnuCash link folders, write-back settings | In the book, as now; changing them needs the editor role (the folders and write-back, the owner) |

The rule: anything that only changes how a user *sees* shared data is personal, so
each person can look at the book the way that makes sense to them. `plan.view` and
the Dashboard configuration move from the book to the user directory; a new user
starts from the book's current choices. Used alone, personal preferences are simply
stored for the one user.

### 8. Where it shows: Preferences → Users

- **One user, not shared:** nothing changes. No sign-in, no password, and the Users
  page shows "Only you use this computer's books", a button to set a password (needed
  only before using a book from another device), and the device list.
- **When sharing is turned on** (Preferences → Sharing, today), the Users page lists
  users (administrator: add, remove, reset password), the user's own password, TOTP,
  and devices; each book's sharing (in the book's properties) lists who may use it
  and in which role.
- **The browser** shows a sign-in page only to a client without a device token or
  session; the page `breadsched web` opens is already signed in.
- **The command line** gains `breadsched users` (list, add, remove, reset-password),
  `breadsched password`, `breadsched totp`, and `breadsched token` (create, list,
  revoke), and `breadsched share BOOK USER --role editor`.

## Ramifications

- **It closes decision 0003's remaining items:** other accounts on the same computer
  are kept out by their lack of a credential, not by the port, so no Unix socket is
  needed; undo is decided (the book's, for editors and owners); and anything wider
  than loopback now has real authentication, with TLS still from the proxy.
- **Every operation is touched once,** to declare its permission, and every write
  carries a user through the service layer to the undo history and change stream.
  That is mechanical but wide.
- **A host for several books** changes how the server is found (the record names the
  host, the request names the book id) and how GTK opens a book that the host already
  holds: it joins the host as the computer account's user, without a prompt.
- **Read-only users** need every read to be served without a write lock, which
  0003's read snapshots already do for `GET`; reads sent as `POST` (projection,
  previews) must be tagged read, not write.
- **More state to keep safe:** the user directory needs backing up and restoring
  with the host's settings (not with a book), and its loss must not lock the
  administrator out (the computer account can always reset).
- **Concurrency gets visible:** with several people editing, refusals ("changed
  since") become ordinary, so the interfaces must show them calmly and offer to
  reload, not as errors.
- **Flatpak:** the user directory and device token live in the sandbox's data and
  config folders; another computer account's GTK in its own sandbox signs in like any
  other client.
- **Release:** existing token files keep working (each becomes the owner's device
  token on first start), so a household already using a proxy is not locked out.
- **Cost:** several weeks of work across phases, a security review before the proxy
  is used with it, and new tests for every refusal (wrong role, revoked token, expired
  session, throttling, TOTP window, an entry user's undo).

## Phases

1. **Identity without visible change:** the user directory, the host's own user,
   automatic device tokens for the computer account, and the transport checking a
   user on every request in place of the shared token. Single-user use is unchanged;
   the server record stops carrying a token.
2. **Signing in:** passwords, sessions and the browser's cookie, throttling, the
   sign-in page, Preferences → Users, and `breadsched users`, `password`, `token`.
3. **Sharing books:** book ids, the multi-book host, roles and grants, the permission
   tag on every operation, and each book's sharing settings.
4. **Personal state:** undo and redo for editors and owners naming each step's user,
   change attribution, and personal display preferences (`plan.view` and the
   Dashboard configuration first).
5. **TOTP** and recovery codes, optional per user.

Phases 1 and 2 already make the server safe to reach from other devices and from
other accounts; 3 and 4 make it a household application.

## Where it stands

**Phase 1 is built** (`breadsched.api.users`):
- **The user directory** is `users.sqlite` in the computer account's data folder
  (`0600`, in a `0700` folder). The computer account's own user is made, as its
  administrator, the first time it is needed.
- **The device token** is made the first time a program of the account needs it,
  kept `0600` as `device-token` in the account's config folder, and replaced if the
  host no longer knows it. It may name files on the host. Tokens are kept only as
  their SHA-256 digests, with when they were made and last used, and can be revoked.
- **The server** answers a request for the user its token belongs to: its own
  per-process token (and the local token of the page it opens) is the computer
  account; any other token must be one the directory issued. A token from another
  account's directory, or a revoked one, is refused (403).
- **The server record** carries only the address, process, and lock. A program that
  finds it connects with the account's device token
  (`HttpBookClient(..., host_files=True)`), which also lets it name files on the host
  (#358).
- Single-user use is unchanged; `--token-file` still keeps the server's own token
  for devices behind the proxy until phase 2 gives them device tokens of their own.

**Phase 2 has begun** with accounts in the directory:
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
  throttled one 429; the client's address is the proxy's `X-Real-IP` (added to the
  nginx example), else the connection's.
- **A temporary password first:** while a user must change their password, every
  other request is refused (403).
- **The pages:** the full page and quick entry ask `/api/v1/me` first; without a
  token or session they show a sign-in form, and then the choice of a new password
  when one is due. The cookie keeps a phone signed in across reloads.
- **Preferences → Users** in GTK says "Only you" until there are others; **Users and
  devices…** (`gui/dialogs/users_dialog.py`) sets your own password, adds, resets,
  and removes people, and lists, makes (shown once), and revokes your device tokens,
  on the computer account's directory directly.
- **Phase 2 is built.** The server's own `--token-file` token keeps working for
  devices already given it.

**Phase 3 has begun** with roles in each book and a host for several books:
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
  transaction); an editor all but `OWNER`; an owner everything. Saving the
  Dashboard configuration is `WRITE` until phase 4 makes it personal; saving one's
  own Plan view is `READ` (see phase 4).
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
- **GTK opening a book the host holds** waits on GTK being wholly a client of the
  protocol: about fifty GUI modules still reach the book, its services, or its
  engines directly (and its storage signals redraw the views), so GTK cannot yet run
  over `HttpBookClient`. Until then GTK opens a book itself, and a book the host
  holds is refused on its writer lock as before. Phase 3 is otherwise built.

**Phase 4 has begun** with personal preferences:
- **Kept with the person,** in the user directory's `preferences` table (user, book id,
  key, JSON value), removed with the user, never in the book. The transport serves
  every request for its caller (`api.users.acting_for`); in process it is the computer
  account.
- **The Plan view** (`plan.view`: range, period, measure, scenario, comparison) is the
  first: `plan_settings`, `save_plan_settings`, and the Plan and Expense Explorer
  opened on the saved view use the caller's own, else the view a book saved before,
  else the defaults. Saving it changes nothing in the book (no revision), so it is a
  `READ` operation: a viewer keeps their own. A book without an id (read-only, in
  memory) still keeps its own.
- **Still to come in phase 4:** the Dashboard configuration (with its groups'
  members, now kept on the accounts), undo and redo naming each step's user, and
  change attribution.

## Answers to the review questions (2026-10-28)

1. **Roles:** owner, editor, and viewer, and an *entry* role as well.
2. **Grants:** with the host; a copied book carries no access.
3. **Undo:** editors and owners undo anything, whoever made it; restricting it would
   protect nothing an editor cannot do by hand.
4. **TOTP:** optional.
5. **Personal or book-wide:** every display choice is personal, the whole Dashboard
   configuration included: each user sees shared data the way that makes sense to
   them.
6. **One host per computer account;** no system service.
