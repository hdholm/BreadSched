# 0004: People, not processes, use a book server

Part of the [BreadSched design](../../../DESIGN.md); builds on
[decision 0003](0003-book-server.md).

- **Status:** proposed, 2026-10-28, for review before any code. If accepted, it is
  built on the `claude/experimental-book-server` branch in the phases below.
- **Proposal:** the book server knows *users*. Every request is made by a user
  (signed in, or the computer's own account acting for itself), every book is shared
  with named users in a role, and undo history and personal preferences belong to a
  user. Used alone on one computer, nothing of this shows: no sign-in, no password,
  and the user settings live in a **Users** page of Preferences.

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

## Proposal

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
- **TOTP (RFC 6238):** optional per user, required by host policy for sign-in through
  the proxy if the administrator chooses. It needs only `hmac` and `hashlib` (about
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

- **Roles per book:** *owner*, *editor* (reads and writes), and *viewer* (reads).
  Possibly also *entry*: may add transactions (quick entry) and read nothing else, for
  a child's phone; this is an open question below.
- **The creator is the owner.** The owner grants and revokes access for users the
  host knows, may pass ownership on, and alone may: delete or restore the book,
  write back to GnuCash, change the attachment and GnuCash link folders, and set
  book-wide settings.
- **Every operation declares its permission.** Each `BookApi` operation is tagged
  read, write, or owner (and *names host files* for the path-taking ones of #358);
  the transport checks the tag before calling. An architecture test requires a tag
  on every operation, so a new one cannot be added open.
- **A user's book list** is the books they hold a grant on. Opening another book
  from the host, from a phone, is choosing one from that list.

### 6. Undo per user

- **Each user has their own undo and redo history** of their own changes, per book,
  in the host's memory (lost when the host stops, as today).
- **Undoing is refused if someone changed the same records since.** It is the check
  undo already makes against an invalid book, applied to the records' current state;
  the refusal says who changed them ("Alice edited this transaction since").
- **A change notice says whose change it was,** and whether *the subscriber* can
  undo or redo (decision 0003's `can_undo` becomes per subscriber).
- **Recording who made a change:** the change stream and undo label name the user. A
  durable per-transaction "entered by" field is optional and could come later.

### 7. Preferences: three kinds

| Kind | Examples | Where it lives |
| --- | --- | --- |
| Device | window size, column widths, banding | Per computer account or browser, as now |
| Personal | Plan range and scenario, register double line, the selected scenario, last books, dashboard sections shown | In the user directory, per user (and per book where it is about one book), so the phone and laptop agree |
| Book-wide | base assumptions, categorization rules, dashboard groups, attachment folder, payroll templates | In the book, as now; changing them needs the owner role (or editor, for rules) |

`plan.view` moves from book-wide to personal; the others named above stay where they
are. Used alone, personal preferences are simply stored for the one user.

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
  needed; undo is decided (per user); and anything wider than loopback now has real
  authentication, with TLS still from the proxy.
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
  session, throttling, TOTP window, undo conflict).

## Phases

1. **Identity without visible change:** the user directory, the host's own user,
   automatic device tokens for the computer account, and the transport checking a
   user on every request in place of the shared token. Single-user use is unchanged;
   the server record stops carrying a token.
2. **Signing in:** passwords, sessions and the browser's cookie, throttling, the
   sign-in page, Preferences → Users, and `breadsched users`, `password`, `token`.
3. **Sharing books:** book ids, the multi-book host, roles and grants, the permission
   tag on every operation, and each book's sharing settings.
4. **Personal state:** undo and redo per user with conflict refusal, change
   attribution, and personal preferences (starting with `plan.view`).
5. **TOTP** and recovery codes, and the host policy that requires it through the
   proxy.

Phases 1 and 2 already make the server safe to reach from other devices and from
other accounts; 3 and 4 make it a household application.

## Questions for review

1. **Roles:** are owner, editor, and viewer enough, or is an *entry* role (add
   transactions only, as from a child's phone) wanted?
2. **Grants:** with the host (proposed: a copied book carries no access), or in the
   book, so that access travels with the file to another host?
3. **Undo:** only one's own changes, refused when someone changed them since
   (proposed), or also undoing others' changes for the owner?
4. **TOTP:** optional per user, or required for anyone signing in from another
   device?
5. **Book-wide or personal:** is the proposed split right, for example the dashboard's
   groups shared but which sections are shown personal?
6. **One host per computer account** (proposed), or one host for the whole computer,
   serving several computer accounts' books? The second needs a system service and
   is not proposed.
