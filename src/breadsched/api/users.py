"""The people a book host serves, and how they prove who they are (decision 0004).

**Experimental.** A *book host* (every book server one computer account runs) keeps
one user directory, ``users.sqlite`` in the account's data folder, never in a book:
a book file may be copied or synced and must not carry anyone's credentials.

- **The computer account** is the host's first user and its administrator, made the
  first time it is needed. Its programs prove who they are with its *device token*,
  kept ``0600`` in its config folder (``device-token``) and made when first needed;
  that token may also name files on this computer (``host_files``, #358).
- **Other users** are made by an administrator, with a temporary password they must
  change when they first sign in. Passwords are kept as ``scrypt`` hashes (the
  standard library; no dependency) with their salt and parameters.
- **Tokens** are random and kept only as SHA-256 digests (a slow hash adds nothing
  to a random token), with when they were made and last used. A *device* token lasts
  until it is revoked; a *session* from signing in ends after
  :data:`SESSION_DAYS` without use, and when its user's password changes.
- **Personal preferences** (decision 0004, phase 4) are kept per user and book:
  each person's own view of shared data (the Plan's period and range, say), never
  in the book. :func:`acting_for` names the user a request is served for; in process
  it is the computer account.
- **Two-step sign-in** (optional, per user; decision 0004, phase 5) asks, after the
  password, for a code from an authenticator app (RFC 6238: SHA-1, six digits, 30
  seconds, one step either side, never the same step twice) or one of ten single-use
  recovery codes, kept only as digests.
- **Signing in is throttled:** after :data:`FREE_FAILURES` failures for one name or
  from one address, each further attempt must wait twice as long as the last (up to
  :data:`MAX_DELAY`). A failure never says whether the name exists.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..gen.db.base import authored_by
from ..gen.services.contracts import ServiceError
from ..gen.utils.user_paths import config_directory, data_directory
from ..presentation import service_error_message

__all__ = [
    "Caller",
    "DeviceToken",
    "Grant",
    "HostedBook",
    "FREE_FAILURES",
    "MAX_DELAY",
    "MIN_PASSWORD_LENGTH",
    "SESSION_DAYS",
    "SignedIn",
    "TwoStepSetup",
    "totp_code",
    "User",
    "UserDirectory",
    "UserError",
    "acting_for",
    "acting_user",
    "device_token",
    "device_token_path",
    "users_path",
]

#: Bytes of randomness in a token (``secrets.token_urlsafe``).
_TOKEN_BYTES = 32
#: A session ends after this many days without use.
SESSION_DAYS = 30
#: The shortest password accepted.
MIN_PASSWORD_LENGTH = 10
#: Failures allowed before signing in must wait.
FREE_FAILURES = 5
#: The longest wait between attempts.
MAX_DELAY = timedelta(minutes=15)
#: The roles a user may be granted in a book (``api.permissions.ROLES``).
_ROLES = ("owner", "editor", "viewer", "entry")
#: Sign-in attempts are checked one at a time (see ``UserDirectory.sign_in``).
_SIGNING_IN = threading.Lock()
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: scrypt cost: 16 MiB and well under a second per attempt on a household computer.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
#: RFC 6238 time step, in seconds, and the digits of a code.
TOTP_STEP = 30
TOTP_DIGITS = 6
#: Recovery codes made when two-step sign-in is turned on.
RECOVERY_CODES = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    administrator INTEGER NOT NULL DEFAULT 0,
    host_account INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    password TEXT,
    must_change INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    digest TEXT NOT NULL UNIQUE,
    host_files INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    last_used TEXT,
    kind TEXT NOT NULL DEFAULT 'device',
    expires TEXT
);
CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
    book_id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    PRIMARY KEY (book_id, user_id)
);
CREATE TABLE IF NOT EXISTS preferences (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (user_id, book_id, key)
);
CREATE TABLE IF NOT EXISTS second_factors (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    secret TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0,
    last_step INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS recovery_codes (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    digest TEXT NOT NULL,
    PRIMARY KEY (user_id, digest)
);
CREATE TABLE IF NOT EXISTS failures (
    key TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    last TEXT NOT NULL
);
"""
_USER_COLUMNS = "id, name, display_name, administrator, host_account, password, must_change"


def users_path() -> Path:
    """Where this computer account's user directory is kept."""
    return data_directory() / "users.sqlite"


def device_token_path() -> Path:
    """Where this computer account keeps its own device token."""
    return config_directory() / "device-token"


class UserError(Exception):
    """A refused account or sign-in request, with a stable code."""

    def __init__(self, code: str, fields: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.fields = fields

    @property
    def message(self) -> str:
        """The wording the presentation layer gives this refusal."""
        return service_error_message(ServiceError(self.code, self.fields))


@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str
    display_name: str
    administrator: bool
    #: The computer account that runs the host.
    host_account: bool
    has_password: bool = False
    #: Signed in with a temporary password that must be changed.
    must_change: bool = False


@dataclass(frozen=True, slots=True)
class Caller:
    """Who a request comes from, and whether it may name files on this computer."""

    user: User
    host_files: bool


@dataclass(frozen=True, slots=True)
class DeviceToken:
    """A token as the directory lists it (never the token itself)."""

    id: str
    user: str
    name: str
    host_files: bool
    created: str
    last_used: str | None
    kind: str = "device"
    expires: str | None = None


@dataclass(frozen=True, slots=True)
class Grant:
    """A user's role in one book."""

    user: User
    role: str


@dataclass(frozen=True, slots=True)
class HostedBook:
    """A book the host has served, by its permanent id, where it was last seen."""

    id: str
    path: str


@dataclass(frozen=True, slots=True)
class SignedIn:
    """A session made by signing in; ``token`` is shown only this once."""

    user: User
    token: str
    expires: str


def _now() -> datetime:
    # Kept to the microsecond: the first waits after failed sign-ins are a second or two.
    return datetime.now(UTC)


def _text(when: datetime) -> str:
    return when.isoformat()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    encode = base64.b64encode
    return "scrypt${n}${r}${p}${salt}${hash}".format(
        **_SCRYPT, salt=encode(salt).decode(), hash=encode(derived).decode()
    )


def _password_matches(password: str, stored: str | None) -> bool:
    if not stored:
        # Spend the same effort, so a missing name or password is not told apart.
        _hash_password(password)
        return False
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.b64decode(expected)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, base64.b64decode(expected))


@dataclass(frozen=True, slots=True)
class TwoStepSetup:
    """What an authenticator app needs: the secret (base32) and the same as a URI
    (``otpauth://``) for a QR code."""

    secret: str
    uri: str


def totp_code(secret: str, step: int) -> str:
    """The RFC 6238 code for ``secret`` (base32) in time step ``step``."""
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, step.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(number % 10**TOTP_DIGITS).zfill(TOTP_DIGITS)


def _step(moment: datetime) -> int:
    return int(moment.timestamp()) // TOTP_STEP


def _matching_step(secret: str, code: str, moment: datetime, after: int) -> int | None:
    """The step within one of ``moment``'s whose code ``code`` is, if later than
    ``after`` (a code is never used twice)."""
    digits = "".join(code.split())
    if not digits.isdigit() or len(digits) != TOTP_DIGITS:
        return None
    now = _step(moment)
    for step in (now - 1, now, now + 1):
        if step > after and hmac.compare_digest(totp_code(secret, step), digits):
            return step
    return None


def _recovery_text(code: str) -> str:
    return "".join(char for char in code.casefold() if char.isalnum())


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError("users.password.too_short", ("password",))


def _private_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass


class UserDirectory:
    """The users and tokens of one computer account's book host."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else users_path()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if not self.path.exists():
            _private_parent(self.path)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(_SCHEMA)
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _user(row: tuple) -> User:
        return User(row[0], row[1], row[2], bool(row[3]), bool(row[4]), bool(row[5]), bool(row[6]))

    def _find(self, connection: sqlite3.Connection, name: str) -> tuple | None:
        return connection.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE name = ?", (name,)
        ).fetchone()

    # ----------------------------------------------------------------- users

    def host_user(self) -> User:
        """The computer account's own user, made (as the administrator) if absent."""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE host_account = 1"
            ).fetchone()
            if row is not None:
                return self._user(row)
            name = getpass.getuser()
            user = User(uuid.uuid4().hex, name, name, True, True)
            connection.execute(
                "INSERT INTO users (id, name, display_name, administrator, host_account,"
                " created) VALUES (?, ?, ?, 1, 1, ?)",
                (user.id, name, name, _text(_now())),
            )
            return user

    def users(self) -> tuple[User, ...]:
        self.host_user()
        with self._connect() as connection:
            rows = connection.execute(f"SELECT {_USER_COLUMNS} FROM users ORDER BY name").fetchall()
        return tuple(self._user(row) for row in rows)

    def user(self, name: str) -> User:
        with self._connect() as connection:
            row = self._find(connection, name)
        if row is None:
            raise UserError("users.user.not_found", ("name",))
        return self._user(row)

    def add_user(
        self,
        name: str,
        password: str,
        *,
        display_name: str = "",
        administrator: bool = False,
    ) -> User:
        """A new user with a temporary password, to be changed at first sign-in."""
        if not _NAME.match(name):
            raise UserError("users.name.invalid", ("name",))
        _check_password(password)
        self.host_user()
        user = User(uuid.uuid4().hex, name, display_name.strip() or name, administrator, False)
        with self._connect() as connection:
            if self._find(connection, name) is not None:
                raise UserError("users.name.taken", ("name",))
            connection.execute(
                "INSERT INTO users (id, name, display_name, administrator, host_account,"
                " created, password, must_change) VALUES (?, ?, ?, ?, 0, ?, ?, 1)",
                (
                    user.id,
                    name,
                    user.display_name,
                    int(administrator),
                    _text(_now()),
                    _hash_password(password),
                ),
            )
        return self.user(name)

    def remove_user(self, name: str) -> None:
        """Remove a user, their tokens and sessions with them. The computer account
        and the last administrator stay."""
        user = self.user(name)
        if user.host_account:
            raise UserError("users.user.host_account", ("name",))
        with self._connect() as connection:
            if user.administrator:
                others = connection.execute(
                    "SELECT COUNT(*) FROM users WHERE administrator = 1 AND id != ?",
                    (user.id,),
                ).fetchone()[0]
                if not others:
                    raise UserError("users.user.last_administrator", ("name",))
            connection.execute("DELETE FROM users WHERE id = ?", (user.id,))

    def set_password(
        self, name: str, new: str, *, current: str | None = None, keep: str | None = None
    ) -> User:
        """Change a user's own password. ``current`` is required once there is one;
        every session of the user ends but ``keep`` (the session asking, if any)."""
        _check_password(new)
        with self._connect() as connection:
            row = self._find(connection, name)
            if row is None:
                raise UserError("users.user.not_found", ("name",))
            stored = connection.execute(
                "SELECT password FROM users WHERE id = ?", (row[0],)
            ).fetchone()[0]
            if stored and not _password_matches(current or "", stored):
                raise UserError("users.password.wrong", ("current",))
            connection.execute(
                "UPDATE users SET password = ?, must_change = 0 WHERE id = ?",
                (_hash_password(new), row[0]),
            )
            connection.execute(
                "DELETE FROM tokens WHERE user_id = ? AND kind = 'session' AND digest != ?",
                (row[0], _digest(keep) if keep else ""),
            )
        return self.user(name)

    def reset_password(self, name: str, temporary: str) -> User:
        """An administrator's reset: a temporary password the user must change, and
        every session of the user ends."""
        _check_password(temporary)
        user = self.user(name)
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET password = ?, must_change = 1 WHERE id = ?",
                (_hash_password(temporary), user.id),
            )
            connection.execute(
                "DELETE FROM tokens WHERE user_id = ? AND kind = 'session'", (user.id,)
            )
        return self.user(name)

    # ---------------------------------------------------------------- tokens

    def issue_token(self, user: User, name: str, *, host_files: bool = False) -> str:
        """A new device token for ``user``; only its digest is kept, so this is the
        one chance to see it."""
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO tokens (id, user_id, name, digest, host_files, created, kind)"
                " VALUES (?, ?, ?, ?, ?, ?, 'device')",
                (uuid.uuid4().hex, user.id, name, _digest(token), int(host_files), _text(_now())),
            )
        return token

    def authenticate(self, token: str, *, now: datetime | None = None) -> Caller | None:
        """Who ``token`` belongs to, or None for a token this host never issued, has
        revoked, or whose session has lapsed. Using a session keeps it alive."""
        if not token:
            return None
        moment = now or _now()
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {', '.join('u.' + c for c in _USER_COLUMNS.split(', '))},"
                " t.id, t.host_files, t.kind, t.expires"
                " FROM tokens t JOIN users u ON u.id = t.user_id WHERE t.digest = ?",
                (_digest(token),),
            ).fetchone()
            if row is None:
                return None
            token_id, host_files, kind, expires = row[7:]
            if expires is not None and datetime.fromisoformat(expires) <= moment:
                connection.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
                return None
            renewed = _text(moment + timedelta(days=SESSION_DAYS)) if kind == "session" else None
            connection.execute(
                "UPDATE tokens SET last_used = ?, expires = COALESCE(?, expires) WHERE id = ?",
                (_text(moment), renewed, token_id),
            )
        return Caller(self._user(row[:7]), bool(host_files))

    def tokens(self, user: User) -> tuple[DeviceToken, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, user_id, name, host_files, created, last_used, kind, expires"
                " FROM tokens WHERE user_id = ? ORDER BY created",
                (user.id,),
            ).fetchall()
        return tuple(
            DeviceToken(row[0], row[1], row[2], bool(row[3]), row[4], row[5], row[6], row[7])
            for row in rows
        )

    def revoke(self, token_id: str, user: User | None = None) -> bool:
        """Revoke a token (only one of ``user``'s, when given)."""
        with self._connect() as connection:
            if user is None:
                cursor = connection.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
            else:
                cursor = connection.execute(
                    "DELETE FROM tokens WHERE id = ? AND user_id = ?", (token_id, user.id)
                )
            return cursor.rowcount > 0

    def sign_out(self, token: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM tokens WHERE digest = ? AND kind = 'session'", (_digest(token),)
            )
            return cursor.rowcount > 0

    # ---------------------------------------------------------------- books

    def remember_book(self, book_id: str, path: str) -> None:
        """Note that the host serves ``book_id`` from ``path``, for listing its books."""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO books VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE"
                " SET path = excluded.path, seen = excluded.seen",
                (book_id, path, _text(_now())),
            )

    def books(self) -> tuple[HostedBook, ...]:
        with self._connect() as connection:
            rows = connection.execute("SELECT id, path FROM books ORDER BY path").fetchall()
        return tuple(HostedBook(row[0], row[1]) for row in rows)

    def role(self, book_id: str | None, user: User) -> str | None:
        """``user``'s role in ``book_id``: the computer account owns every book it
        holds; anyone else has the role they were granted, or none."""
        if user.host_account:
            return "owner"
        if not book_id:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT role FROM grants WHERE book_id = ? AND user_id = ?", (book_id, user.id)
            ).fetchone()
        return row[0] if row is not None else None

    def grant(self, book_id: str, name: str, role: str) -> Grant:
        """Give ``name`` ``role`` in the book (replacing any role they had)."""
        if role not in _ROLES:
            raise UserError("users.role.invalid", ("role",))
        user = self.user(name)
        if user.host_account:
            raise UserError("users.grant.host_account", ("name",))
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO grants VALUES (?, ?, ?) ON CONFLICT(book_id, user_id)"
                " DO UPDATE SET role = excluded.role",
                (book_id, user.id, role),
            )
        return Grant(user, role)

    def revoke_grant(self, book_id: str, name: str) -> bool:
        user = self.user(name)
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM grants WHERE book_id = ? AND user_id = ?", (book_id, user.id)
            )
            return cursor.rowcount > 0

    def grants(self, book_id: str) -> tuple[Grant, ...]:
        """Who besides the computer account may use the book, and in which role."""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {', '.join('u.' + c for c in _USER_COLUMNS.split(', '))}, g.role"
                " FROM grants g JOIN users u ON u.id = g.user_id WHERE g.book_id = ?"
                " ORDER BY u.name",
                (book_id,),
            ).fetchall()
        return tuple(Grant(self._user(row[:7]), row[7]) for row in rows)

    # ------------------------------------------------------- personal preferences

    def preference(self, user: User, book_id: str, key: str) -> object | None:
        """``user``'s own ``key`` for the book, or None if they have kept none."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM preferences WHERE user_id = ? AND book_id = ? AND key = ?",
                (user.id, book_id, key),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def set_preference(self, user: User, book_id: str, key: str, value: object) -> None:
        """Keep ``value`` (anything JSON can hold) as ``user``'s own ``key`` for the book."""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO preferences VALUES (?, ?, ?, ?)"
                " ON CONFLICT(user_id, book_id, key) DO UPDATE SET value = excluded.value",
                (user.id, book_id, key, json.dumps(value, sort_keys=True)),
            )

    # ---------------------------------------------------------- two-step sign-in

    def two_step_on(self, user: User) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT active FROM second_factors WHERE user_id = ?", (user.id,)
            ).fetchone()
        return bool(row and row[0])

    def start_two_step(self, user: User) -> TwoStepSetup:
        """A new secret for ``user``'s authenticator app; nothing changes at sign-in
        until :meth:`confirm_two_step` is given a code from it."""
        if self.two_step_on(user):
            raise UserError("two_step.already_on", ())
        secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO second_factors (user_id, secret) VALUES (?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET secret = excluded.secret, active = 0,"
                " last_step = 0",
                (user.id, secret),
            )
        label = f"BreadSched:{user.name}"
        uri = f"otpauth://totp/{label}?secret={secret}&issuer=BreadSched"
        return TwoStepSetup(secret, f"{uri}&digits={TOTP_DIGITS}&period={TOTP_STEP}")

    def confirm_two_step(
        self, user: User, code: str, *, now: datetime | None = None
    ) -> tuple[str, ...]:
        """Turn two-step sign-in on with a first code from the app; the recovery codes,
        shown once."""
        moment = now or _now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT secret, active FROM second_factors WHERE user_id = ?", (user.id,)
            ).fetchone()
            if row is None or row[1]:
                raise UserError("two_step.not_started", ())
            step = _matching_step(row[0], code, moment, 0)
            if step is None:
                raise UserError("two_step.code.invalid", ("code",))
            codes = tuple(
                f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}"
                for _ in range(RECOVERY_CODES)
            )
            connection.execute(
                "UPDATE second_factors SET active = 1, last_step = ? WHERE user_id = ?",
                (step, user.id),
            )
            connection.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user.id,))
            connection.executemany(
                "INSERT INTO recovery_codes VALUES (?, ?)",
                [(user.id, _digest(_recovery_text(code))) for code in codes],
            )
        return codes

    def stop_two_step(self, user: User, password: str) -> None:
        """Turn ``user``'s two-step sign-in off; their password is asked for again."""
        with self._connect() as connection:
            stored = connection.execute(
                "SELECT password FROM users WHERE id = ?", (user.id,)
            ).fetchone()
            if stored is None or (stored[0] and not _password_matches(password, stored[0])):
                raise UserError("users.password.wrong", ("password",))
            connection.execute("DELETE FROM second_factors WHERE user_id = ?", (user.id,))
            connection.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user.id,))

    def reset_two_step(self, name: str) -> None:
        """An administrator turns someone's two-step sign-in off (a lost phone)."""
        user = self.user(name)
        with self._connect() as connection:
            connection.execute("DELETE FROM second_factors WHERE user_id = ?", (user.id,))
            connection.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user.id,))

    def _second_step(
        self, connection: sqlite3.Connection, user_id: str, code: str | None, moment: datetime
    ) -> str | None:
        """None when the code (or a recovery code) lets the user in, else the refusal."""
        row = connection.execute(
            "SELECT secret, last_step FROM second_factors WHERE user_id = ? AND active = 1",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        if not code or not code.strip():
            return "signin.code_required"
        step = _matching_step(row[0], code, moment, row[1])
        if step is not None:
            connection.execute(
                "UPDATE second_factors SET last_step = ? WHERE user_id = ?", (step, user_id)
            )
            return None
        used = connection.execute(
            "DELETE FROM recovery_codes WHERE user_id = ? AND digest = ?",
            (user_id, _digest(_recovery_text(code))),
        )
        return None if used.rowcount else "signin.code.invalid"

    # --------------------------------------------------------------- signing in

    def _wait(self, connection: sqlite3.Connection, key: str, moment: datetime) -> timedelta:
        row = connection.execute(
            "SELECT count, last FROM failures WHERE key = ?", (key,)
        ).fetchone()
        if row is None or row[0] < FREE_FAILURES:
            return timedelta(0)
        delay = min(MAX_DELAY, timedelta(seconds=2 ** (row[0] - FREE_FAILURES)))
        return max(timedelta(0), datetime.fromisoformat(row[1]) + delay - moment)

    def sign_in(
        self,
        name: str,
        password: str,
        *,
        client: str,
        device: str = "Signed in",
        code: str | None = None,
        now: datetime | None = None,
    ) -> SignedIn:
        """A session for ``name``, or ``signin.failed`` (whatever was wrong) or
        ``signin.throttled`` while too many recent attempts failed. ``client`` is
        the address the attempt came from. With two-step sign-in on, a right password
        without ``code`` is ``signin.code_required`` and a wrong code
        ``signin.code.invalid`` (a failure like any other)."""
        moment = now or _now()
        keys = (f"name:{name.casefold()}", f"address:{client}")
        # One attempt at a time, from the throttle check to recording its outcome:
        # otherwise attempts sent together all pass the check before any failure is
        # counted, and one code could be used twice. The lock keeps this process's
        # attempts in turn; the immediate transaction, any other process's.
        with _SIGNING_IN, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if any(self._wait(connection, key, moment) for key in keys):
                raise UserError("signin.throttled", ())
            row = self._find(connection, name)
            stored = None
            if row is not None:
                stored = connection.execute(
                    "SELECT password FROM users WHERE id = ?", (row[0],)
                ).fetchone()[0]
            # Checked even for a name that does not exist, at the same cost.
            matches = _password_matches(password, stored)
            refusal = "signin.failed"
            if row is not None and matches:
                second = self._second_step(connection, row[0], code, moment)
                if second == "signin.code_required":
                    # The password was right: asking for the code is no failure.
                    raise UserError(second, ("code",))
                if second is not None:
                    matches, refusal = False, second
            if row is None or not matches:
                for key in keys:
                    connection.execute(
                        "INSERT INTO failures VALUES (?, 1, ?) ON CONFLICT(key) DO UPDATE"
                        " SET count = count + 1, last = excluded.last",
                        (key, _text(moment)),
                    )
                failed = True
            else:
                failed = False
                connection.executemany("DELETE FROM failures WHERE key = ?", [(k,) for k in keys])
                token = secrets.token_urlsafe(_TOKEN_BYTES)
                expires = _text(moment + timedelta(days=SESSION_DAYS))
                connection.execute(
                    "INSERT INTO tokens (id, user_id, name, digest, host_files, created,"
                    " kind, expires) VALUES (?, ?, ?, ?, 0, ?, 'session', ?)",
                    (uuid.uuid4().hex, row[0], device, _digest(token), _text(moment), expires),
                )
        # Raised outside the block, so the failure counts are committed.
        if failed or row is None:
            raise UserError(refusal, ("code",) if refusal != "signin.failed" else ())
        return SignedIn(self._user(row), token, expires)


_ACTING: ContextVar[tuple[UserDirectory, User] | None] = ContextVar(
    "breadsched_acting_user", default=None
)


@contextmanager
def acting_for(directory: UserDirectory | None, user: User | None) -> Iterator[None]:
    """Serve the calls inside the block (in this thread) for ``user``, whose personal
    preferences ``directory`` keeps; None for the computer account, in process."""
    token = _ACTING.set((directory, user) if directory is not None and user else None)
    try:
        # The changes they make are theirs (decision 0004: undo names each step's user).
        with authored_by(user.display_name or user.name if user else None):
            yield
    finally:
        _ACTING.reset(token)


def acting_user() -> tuple[UserDirectory, User | None]:
    """The directory and user calls are served for: the request's, else the computer
    account's own directory (its user is made when first asked for)."""
    acting = _ACTING.get()
    if acting is not None:
        return acting
    return UserDirectory(), None


def device_token(directory: UserDirectory | None = None, path: Path | None = None) -> str:
    """This computer account's own device token, made the first time it is needed.

    It is kept ``0600`` in the account's config folder, beside its preferences, and
    replaced if the host no longer knows it (revoked, or the directory was reset).
    """
    directory = directory if directory is not None else UserDirectory()
    target = Path(path) if path is not None else device_token_path()
    try:
        kept = target.read_text(encoding="utf-8").strip()
    except OSError:
        kept = ""
    if kept and directory.authenticate(kept) is not None:
        return kept
    user = directory.host_user()
    token = directory.issue_token(user, f"{socket.gethostname()} ({user.name})", host_files=True)
    _private_parent(target)
    temporary = target.with_name(f".{target.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    os.replace(temporary, target)
    return token
